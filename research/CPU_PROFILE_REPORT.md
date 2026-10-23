# CPU 来源定位报告（正式版 70%+ CPU）

日期：2026-09-22
测量环境：本机 24 逻辑核 / 24 物理核，Python 3.12.10，onnxruntime 1.30.0，
rapidocr_onnxruntime 1.4.4，屏幕 2560x1440，`scan_interval = 0.25s`

**本报告只做定位，没有改动任何业务逻辑。**

测量脚本（均可重复运行）：

| 脚本 | 作用 | 输出 |
| --- | --- | --- |
| `research/cpu_profile_probe.py` | 全量探针：ONNX 配置、OCR 延迟、截图成本、三场景 CPU、cProfile | `research/cpu_profile.txt` |
| `research/cpu_spin_test.py` | ONNX 线程池自旋 / 限流代价 | `research/cpu_spin_test.txt` |
| `research/cpu_steady_test.py` | 稳态分桶（判断是尖峰还是持续） | `research/cpu_steady_test.txt` |
| `research/cpu_watch.py` | **在真实对局中运行**，统计 ROI 像素变化率与 OCR 频率 | `research/cpu_watch.txt` |

---

## 1. 结论速览

```
CPU 根因：OCR（RapidOCR / ONNX Runtime 推理）
          + 主循环没有任何"是否该 OCR"的门控，导致对局中持续 OCR
          + ONNX 默认 24 线程且推理后线程池自旋，把单次 OCR 的 CPU 成本再放大

普通对局 CPU：50% ~ 86%（像素每轮都变时实测 20.7 核 / 86.4%）
海克斯界面 CPU：首轮 5 秒 4.5 核 / 18.9%，之后 0.00 核 / 0.0%
OCR 调用：9.9 ~ 10.5 次/秒（像素持续变化）
          0.0 次/秒（牌面稳定，30 秒实测只有首轮 3 次）
ONNX threads：intra_op_num_threads=0 / inter_op_num_threads=0
              （0 = 交给 ORT 自动决定 → 按物理核 24 开线程池）
              3 个会话（det/cls/rec），进程线程数 99（未加载时 30）
最高 CPU 函数：onnxruntime InferenceSession.run（cProfile 占比 98.9%）
```

---

## 2. OCR 实际调用频率与延迟（问题 1）

| 场景 | 迭代/秒 | OCR 次数/秒 | 单次延迟 平均 / P95 | CPU |
| --- | --- | --- | --- | --- |
| 普通对局（ROI 像素每轮变化） | 3.3 | **9.9 ~ 10.5** | 91.3 ms / 191.8 ms | **20.7 核 / 86.4%** |
| 海克斯界面（牌面刚出现） | 4.0 | 3 次（仅首轮，每槽 1 次） | 51.6 ms / 66.9 ms | 2.2 核 / 9.1% |
| 海克斯界面稳定 5 秒以后 | 4.0 | **0.0**（30 秒实测总 OCR = 3） | - | **0.00 核 / 0.0%** |

单槽重复识别 20 次：平均 87.5 ms，P50 68.8，P95 209.9（24 线程 + 自旋争抢，抖动大）。

稳态分桶（`cpu_steady_test.py`，像素完全不变）：

```
第  0~ 5 秒: 4.54 核 / 18.9%   OCR=3      <- 首轮识别 + 线程池自旋尾巴
第  5~10 秒: 0.00 核 /  0.0%   OCR=0
第 10~30 秒: 0.00 核 /  0.0%   OCR=0
```

**稳定后不会持续 OCR** —— 说明 hash 复用逻辑本身是对的。

---

## 3. 是否误触发 OCR（问题 2）

**是。主循环没有任何阶段门控。**

`play_one_game` 的对局循环里，`reader.read_once()` 每 `scan_interval`（0.25s）无条件执行一次，
只有"单个 slot 像素哈希不变"才会跳过该槽的 OCR，**与"现在是不是海克斯界面"无关**。

证据 A（代码）：`src/live.py` 循环内 `flow.set_state(SCANNING); offer = reader.read_once()`
前面只有 `_stopping()` 和"对局结束"两个 `break`，没有任何 augment 可见性判断。

证据 B（本机实测，当前屏幕 = 英雄联盟客户端界面，**不在对局、没有海克斯**）：

```
research/cpu_watch.py --seconds 6
  轮数=24（4.0 次/秒）
  slot 像素变化次数: slot0=0  slot1=0  slot2=23
```

只要某个 slot 的 ROI 里**有动画元素**（客户端动画、游戏画面、特效），
该槽就会每轮都重新 OCR。

证据 C（真机日志反推，2026-09-21 23:16:01 ~ 23:18:35 普通对局时段）：
用 `stage 回退被忽略` 警告（每次迭代打一条）统计主循环真实速率：

```
平均每轮迭代 ≈ 0.393 s（sleep 0.25s + 真实工作 0.14s）
每秒迭代次数分布：1 次/秒 x25 秒、2 x62 秒、3 x27 秒、4 x41 秒
```

若完全不做 OCR，每轮工作只有 mss 7ms + HTTP 15ms ≈ 0.30 s/轮；
实测 0.39 s/轮，多出来的 ~0.14 s 恰好是 1 次 OCR（≈90ms）的量级
→ **对局中确实在持续 OCR（约 1~3 次/轮，2~5 次/秒）**。

---

## 4. 截图成本（问题 3）

当前实现**不是**全屏截图，而是"三卡名称带"一次抓取（`RoiSet.capture_bounds()`）：

| 抓取区域 | 尺寸 | 平均耗时 |
| --- | --- | --- |
| 全屏 | 2560x1440 | 28.8 ms |
| **名称带（当前实现）** | **1410x58 @ (583,562)** | **7.0 ms** |
| 单卡 TITLE_ROI | 430x58 | 6.8 ms |

结论：截图方式已经是最省的形态（比全屏快 4 倍，和单卡几乎同价）。
4 次/秒 × 7 ms ≈ 2.8% 单核 ≈ 0.1% 整机 —— **截图不是 CPU 根因**。

像素哈希 + numpy 切片：三槽 `tobytes()+blake2b` 共 **0.257 ms/轮** —— 可忽略。

---

## 5. 三个 slot 是否重复 OCR（问题 4）

用标记像素区分 slot 后实测：

```
[A] 牌面稳定 10 秒：迭代 40 次，OCR 共 3 次 -> slot0=1, slot1=1, slot2=1（只有首轮）
[B] 像素每轮变化 6 秒：迭代 20 次，OCR 共 60 次 -> slot0=20, slot1=20, slot2=20
[C] 稳定后继续 6 秒：迭代 24 次，OCR 共 3 次 -> 稳定期间三槽都是 0
```

符合设计：**稳定期间 slot0/1/2 = 0**；只有像素变化的槽才 OCR。
问题不在"重复 OCR 稳定槽"，而在"像素变化时没有任何频率上限"。

---

## 6. ONNX Runtime 线程（问题 5）

直接读取真实会话对象（`InferenceSession.get_session_options()`）：

```
det(检测): providers=['CPUExecutionProvider']  intra_op=0  inter_op=0  execution_mode=ORT_SEQUENTIAL
cls(方向): providers=['CPUExecutionProvider']  intra_op=0  inter_op=0  execution_mode=ORT_SEQUENTIAL
rec(识别): providers=['CPUExecutionProvider']  intra_op=0  inter_op=0  execution_mode=ORT_SEQUENTIAL
graph_optimization_level=ORT_ENABLE_ALL   enable_cpu_mem_arena=False
进程线程数：未加载 30 -> 加载 3 个 ONNX 会话后 99
```

来源：`rapidocr_onnxruntime/config.yaml` 里 `intra_op_num_threads: -1 / inter_op_num_threads: -1`，
`utils/infer_engine.py::_init_sess_opts()` 对 -1 不设置 → 用 ONNX 默认值 0 = 自动（物理核 24）。

线程池自旋实测（`cpu_spin_test.py`）：

```
未加载模型                        0.00 核 /  0.0%   线程=30
加载后 idle 第1个4s               3.60 核 / 15.0%   线程=99   <- 自旋
加载后 idle 第2个4s               0.00 核 /  0.0%             <- 会衰减
1 次推理后 idle 第1个4s           2.17 核 /  9.0%             <- 每次推理后又自旋
1 次推理后 idle 第2个4s           0.00 核 /  0.0%
```

也就是说：**每次 OCR 之后，24 个线程还会空转 2~4 秒**，
在"每秒 4~10 次 OCR"的持续调用下，线程池根本没有机会冷却 → 一直满转。

限流代价（同一次实验）：

```
RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    单次识别 21~26 ms（默认 24 线程是 16~23 ms，基本无差别）
    推理后空转 0.09 核 / 0.4%（默认 1.88~3.60 核 / 7.8~15.0%）
原生会话 2 线程 + session.intra_op.allow_spinning=0
    空转 0.00 核 / 0.0%
```

---

## 7. CPU 时间占比（问题 6）

cProfile（8 轮"像素变化"识别，总 2.07s）：

```
OCR(识别/推理)   2.046 s   98.9%
截图/采集        0.000 s    0.0%（当轮用假屏幕，真实抓屏见 §4：7 ms/轮）
哈希/切片        0.004 s    0.2%
文本匹配         0.000 s    0.0%
其他/框架        0.017 s    0.8%

热点函数 top:
  onnxruntime/capi/onnxruntime_inference_collection.py:372(run)  tottime 2.043s
  rapidocr_onnxruntime/ch_ppocr_rec/text_recognize.py:43(__call__)
```

其他组件实测：

| 组件 | 单次成本 | 评价 |
| --- | --- | --- |
| Overlay `set_offers` + `processEvents` | 2.0 ms | 只在内容变化时调用（相同 payload 直接 return）→ 非瓶颈 |
| `StateHub.snapshot()` | 0.002 ms | 可忽略 |
| 新建 httpx client + GET LCU（每轮一次） | 12~16 ms（首次 694 ms） | 小但可省；`LiveClientDataSource._get` 每次新建 client |
| 三槽 hash + 切片 | 0.257 ms/轮 | 可忽略 |

---

## 8. 优化方案（按收益排序，待你确认后再动代码）

### P0-1 限制 ONNX 线程（1 行改动，收益最大、风险最低）

`src/ocr/engine.py::OcrEngine._ensure_engine()`：

```python
self._engine = rapidocr_cls(intra_op_num_threads=2, inter_op_num_threads=1)
```

- 单次识别延迟不变（21~26 ms，实测甚至更稳，因为少了线程争抢）
- 每次调用后的自旋尾巴从 ~2 核×3s 降到 0.4%
- 进程线程数 99 → 约 35
- 可选进一步：monkeypatch `OrtInferSession._init_sess_opts` 增加
  `session.intra_op.allow_spinning=0`（rapidocr 未暴露该参数）→ 空转 0.0%

### P0-2 给 OCR 加"该不该扫"的门控（真正的根因）

现状：整局 4 次/秒无条件扫描，只要像素动就 OCR。

建议（不改推荐/匹配/缓存的语义）：

1. **只在海克斯窗口内扫描**：由 Live Client 等级触发（3 / 7 / 11 / 15），
   窗口内 4 次/秒；窗口外降到 1~2 秒一次（或完全不抓屏）。
2. **稳定即停**：连续 N 次哈希不变 → 停止 OCR（现有 hash 复用已能做到 0 次/秒）。
3. **变化后限频**：同一槽两次 OCR 之间至少间隔 0.5~1 秒，避免抖动/动画导致连续识别。
4. 保持"只 OCR 变化的槽"（已实现）。

预期：普通对局从 50~86% 降到 **0.1~0.4%**（只剩低频抓屏）；
海克斯界面稳定后 **0.0%**；刷新时只多 1~3 次 OCR（瞬时约 60 ms）。

### P1-1 Live Client 轮询成本

- 复用同一个 `httpx.AsyncClient`（现在每轮新建，12~16 ms/次）
- 等级轮询从 4 次/秒降到 1 次/秒（等级变化本来就慢，不影响 stage 判定）

### P1-2 保持当前截图方式

名称带 7 ms 已经接近最优；如果做了 P0-2 的窗口门控，
窗口外可以完全不抓屏，截图成本直接归零。

### 不做

- 不动推荐算法 / OP.GG / 缓存 / 三槽刷新语义
- 不动 Overlay（实测 2 ms 且仅变化时调用）

---

## 9. 还差一个确认（需要你在真实对局中跑一次）

本机现在不在对局中，无法采集"真实对局里游戏画面 ROI 的像素变化率"。
请在**下一局进行中**执行（只读屏幕、不干预游戏、自身 CPU≈0）：

```powershell
& "D:\lol海克斯选择\.venv\Scripts\python.exe" research\cpu_watch.py --seconds 30
```

（默认只统计像素哈希变化，不加载 OCR 模型；加 `--ocr` 会再起一套 RapidOCR，
≈多占 20 核，需要额外测延迟时才用。）

判读：

- 普通对局：三个 slot 的变化次数接近"轮数" → 确认每轮 3 次 OCR（86% CPU 场景）
- 海克斯界面稳定后：三个 slot 变化次数都应为 0 → 确认稳定后不 OCR
