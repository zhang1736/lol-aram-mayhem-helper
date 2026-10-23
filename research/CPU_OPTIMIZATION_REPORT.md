# CPU 优化报告（P0-1 ONNX 线程 + P0-2 扫描门控）

日期：2026-09-22
机器：24 逻辑核 / 24 物理核，2560x1440，Python 3.12.10，onnxruntime 1.30.0，
rapidocr_onnxruntime 1.4.4，`scan_interval = 0.25s`

## 1. 改了什么

| 项 | 位置 | 内容 |
| --- | --- | --- |
| P0-1 ONNX 线程 | `src/ocr/engine.py` | `RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)`（1.4.4 的真实 kwarg，源码 `UpdateParameters.update_global_to_module` 会传播到 Det/Cls/Rec）；不支持时退回默认并告警，不重写库 |
| P0-2 扫描门控 | `src/ocr/gate.py`（新增） | `FAST / WATCHDOG / STOPPED` 三态；stage 触发 10 秒高频窗口；窗口外 1 次/秒廉价探测；牌面消失后回到低频；保底 20 秒重开，绝不永久错过 |
| slot 级节流 | `src/ocr/card_reader.py` | `MIN_SLOT_OCR_INTERVAL = 0.75s`；`UNCHANGED / CHANGED_PENDING / OCR_IN_PROGRESS / STABLE` 四态；新增 `probe_feature()`（窗口外只抓带算哈希/亮度，不 OCR） |
| 主循环 | `src/live.py` | 按门控模式决定"探测 or 扫描"；等级轮询 4 次/秒 → 1 次/秒；对局结束/界面消失通知门控 |
| HTTP | `src/lcu/live_client.py` | 长期复用同一个 `httpx.AsyncClient`（原来每轮新建，12~16ms/次）；结束时 `aclose()` |
| 可观测 | `src/core/state_hub.py`、`src/ui/debug_window.py` | Debug 页新增 `scan mode` / `ocr calls` |

**没动**：LCU / 英雄识别 / OP.GG / 缓存 / 推荐(popular) / 三槽独立刷新 /
`offer_version` / `rerolled_slots` / Overlay / 截图方式（仍然是 1410x58 名称带）。

## 2. Before / After

### 2.1 ONNX 线程与空转（`research/cpu_spin_test.py` vs 修改后实测）

```
                    修改前                     修改后
ONNX 线程           intra=0 inter=0（自动=24）  det/cls/rec 全部 (2,1)
进程线程            30 -> 99                   27 -> 33
加载后空转          3.60 核 / 15.0%            0.24 核 / 1.0%（推理后 4 秒平均）
单次识别            16~26 ms                  15~16 ms（没有变慢）
```

### 2.2 五种状态（`research/cpu_after_check.py`，真实 RapidOCR + 真实门控）

| 场景 | 修改前 | 修改后 |
| --- | --- | --- |
| 普通游戏 30 秒（像素一直动） | **20.73 核 / 86.4%**，OCR 9.9~10.5 次/秒 | **0.00 核 / 0.00%**，OCR **0** 次/秒（低频探测 30 次 = 1 次/秒，无牌面抓取） |
| 海克斯首次出现 10 秒 | 首轮 3 次 OCR + 之后持续 OCR | **0.28 核 / 1.16%**，OCR 9 次（约 1.5 秒内的短时过程，平均 16ms/次） |
| 海克斯稳定 30 秒 | 首轮 4.54 核后 0.00 核 | **0.00 核 / 0.00%**，OCR **0**，抓屏 99 次（3.3 次/秒） |
| 刷新一次（只换 slot1） | 每轮 3 槽重新 OCR | `changed_slots=[1]` → **OCR slots=[1]，OCR calls=1** |
| 游戏结束 5 秒 | 继续抓屏 + OCR | **0.00 核 / 0.00%**，OCR **0**，抓屏 **0** |

> 说明：`cpu_after_check.py` 用的是内存假屏幕（截图成本≈0）。
> 正式版走 mss 抓名称带，实测 **7.0 ms/次**；海克斯稳定时约 3.3 次/秒
> → 约 2.3% 单核 ≈ **0.1% 整机**。这是"稳定后"唯一的固定成本。

## 3. 门控行为（测试覆盖）

```text
stage 未到              -> WATCHDOG（1 次/秒，只探测，不 OCR）
level -> stage 触发      -> FAST（0.25s 扫描）最多 10 秒
10 秒没看到牌面          -> 回 WATCHDOG
  画面突然静止 / 亮度跳变 -> 立刻重开 FAST（不会错过玩家后来才打开的牌面）
  保底 20 秒            -> 无论如何再开一次 FAST
牌面出现                -> FAST 持续跟踪（像素变才 OCR）
牌面稳定                -> OCR = 0
刷新某个槽              -> 只 OCR 该槽，且同一槽 ≥0.75s
界面消失 / 对局结束      -> 回 WATCHDOG / STOPPED（0 抓屏 0 OCR）
```

## 4. 测试

```
tests/test_ocr_gate.py          13 个用例（你指定的 8 个 + 不会错过的 3 个 + 主循环集成 2 个）
tests/test_ocr_thread_config.py  5 个用例（参数名、传播、真实会话 2/1、不可用时不崩）
tests/test_card_reader.py        更新 test_changed_slot_is_reocrd 以匹配 0.75s 契约
全量：237 passed, 2 skipped（OP.GG 在线用例当前网络不可达被跳过）
```

## 5. 已知边界

- `session.intra_op.allow_spinning = 0` **没有**强开：rapidocr 1.4.4 未暴露该参数，
  按需求"不为这个问题重写 RapidOCR"。限到 2/1 后空转已经 0.24 核 / 1.0%，
  且只发生在推理后几秒，不再持续。
- "牌面出现"的判定仍然依赖像素：如果某天海克斯 UI 本身带持续动画，
  slot 级节流会把它限制为"每个槽最多 1.33 次/秒"，不会回到 10 次/秒。
- 真正的对局验证（A/C 的实测）等下一局用正式版看日志 `[OCR] scan mode` 与
  调试页 `ocr calls` 即可；离线流水线实测已经覆盖同样的代码路径。
