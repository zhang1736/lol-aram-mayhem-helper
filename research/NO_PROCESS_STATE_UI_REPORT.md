# 彻底取消"识别中"作为用户可见状态

日期：2026-09-22

## 1. "识别中"是哪个代码路径触发的

全项目搜索 `识别中 / PENDING_TEXT / pending` 后，只有下面这些路径会在**正式 UI** 上写出"识别中…"：

| # | 路径 | 触发条件 |
| --- | --- | --- |
| 1 | `src/overlay/widgets.py: PENDING_TEXT = "识别中…"` | 文案常量本身 |
| 2 | `src/overlay/window.py: AugmentCard.update_offer()` `if offer["pending"] → setText(PENDING_TEXT)` | 卡片收到 `pending=True` 的 payload |
| 3 | `src/live.py`「pending 分支」：`if pending_slots and slot_offers: publish(payload with pending)` | 刷新/动画期间只要某个槽处于 `CHANGED_PENDING` 就发布并显示 |
| 4 | `src/live.py: build_offer_payload()` 把 pending 槽的 `performance/popular` 置 None 且排除出推荐 | 同一路径，导致"名称还在但数值没了/星标乱跳" |
| 5 | `src/live.py: print_offers()` 打印"识别中…" | stdout（打包版不可见，仍清理掉） |
| 6 | `src/ui/debug_window.py` 显示"识别中" | Debug 页（内部），现已改为 `PENDING` |

真正让用户看到的是 **1 → 2 → 3** 这条链：`card_reader` 把"刷新中的槽"标成 `CHANGED_PENDING`
→ `live.py` 立刻用这个状态发布 payload → `window.py` 把卡片文字改成"识别中…"。

## 2. 修改了哪些文件

| 文件 | 修改 |
| --- | --- |
| `src/overlay/widgets.py` | **删除** `PENDING_TEXT`；`NO_DATA_TEXT` 保持 "—" |
| `src/overlay/window.py` | 卡片渲染**不再有 pending 分支**：永远显示快照内容；`pending_slots` 只作为 Debug 标记 |
| `src/overlay/manager.py` | 文档更新：只接受"已确认快照" |
| `src/live.py` | ① 删除"pending 分支"发布；② 只有 **三槽全部确认 + 签名变化** 才原子发布；③ 新增 `publish_offers()`：offers 与 recommended 在**同一条 HUB 更新**里（避免"卡片新/推荐旧"的中间态）；④ `build_offer_payload()` 不再清空 pending 槽数值、不再把它排除出推荐（推荐绑定完整快照）；⑤ 新增兜底：本 stage 还没有快照且某槽 >6s 确认不出来 → 降级为"未识别"（终结态，不是过程态）；⑥ stdout 文案改成"[后台待确认]" |
| `src/ocr/card_reader.py` | 内部仍保留 `CHANGED_PENDING` 用于调度，新增 `pending_age()` 与 `drop_unresolved()`（兜底降级）；**不再对外驱动 UI 文案** |
| `src/ui/debug_window.py` | Debug 页显示 `PENDING`（内部） |
| `tests/test_no_process_state_ui.py` | 新增守卫：AST 扫描 UI 模块字符串字面量 + 卡片/主窗口文案 + 整局回放 |
| `tests/test_overlay_refresh.py` | 按新契约重写（原子更新、无过程状态、推荐同步） |
| `tests/test_opgg_display.py` | 相关用例改为"保持上一份快照"语义 |

## 3. 修改前后状态机

```
修改前（用户可见过程态）
  slot hash 变化 → CHANGED_PENDING → payload.pending=True → 卡片"识别中…"
                                   → pending 槽数值清空、推荐临时重算
  → 0.75s 后 OCR 回来 → 又变回名称（界面闪动）

修改后（前台只认稳定快照）
  后台：CHANGED_PENDING / OCR 重试 / stats LOADING|ERROR（仅日志、Debug 页）
  前台：confirmed_offers = 最后一次三槽全部确认的快照
        ★ 刷新期间：界面完全不动（名称/强度/欢迎度/★推荐都是旧快照）
        ★ 新卡确认后：一次性原子替换 [A,X,C] → 再重算推荐
        ★ 三槽一个都确认不了（>6s 且本 stage 无快照）→ 该槽显示"无法识别海克斯" + "—"
        ★ 只有"三个槽都确认不了"才隐藏 Overlay

数据状态与识别状态完全解耦：
  OP.GG loading/error → 强度/欢迎度显示 "—"（或上一份数值），绝不出现"识别中"
```

## 4. 测试

```
283 passed, 1 skipped（跳过的是"正式版正在运行占用互斥体"那条）

新增 tests/test_no_process_state_ui.py（10 个）：
  UI 模块字符串字面量 AST 扫描无"识别中" / widgets 不再导出 PENDING_TEXT /
  payload.pending 标记不改变任何卡片文案 / 主窗口全部文案函数无过程状态 /
  整局回放（OP.GG 全失败 + 动画帧）所有 UI 文本无过程状态

tests/test_overlay_refresh.py 按新契约重写（12 个）：
  刷新全程只出现两份稳定快照 [A,B,C] 与 [A,D,C] / 不隐藏 / 另外两卡原样 /
  新卡确认不出来时保持旧快照 / 推荐原子（刷新中不换 ★）/
  GUI 层 LIVE→LIVE→LIVE 且卡片文字无"识别中"

CPU 优化（未回滚）：普通游戏 0.00 核/0.00% OCR 0 ｜ 首次出现 0.09 核/0.38% OCR 3 ｜
  稳定 0.00 核/0.00% OCR 0 ｜ 刷新只有 changed_slots=[1]→OCR 1 次 ｜ 结束 OCR 0
```

## 5. 皮肤场景与实机验证

英雄链路本来就只走 LCU / Live Client（championId → champion-summary.json → 中文名），
皮肤是纯表现层，不参与识别：

* 真机诊断（19:11）：真 LCU champion-summary 242 条，27ms，
  `157 -> 疾风剑豪 / Yasuo`、`110 -> 惩戒之箭 / Varus`
* 回放测试 `test_skin_does_not_affect_champion_id`：raw 名带皮肤后缀
  （`Varus_Skin42`）仍解析出 110
* 回放测试 `test_champion_id_then_name_shown_without_waiting_opgg`：
  OP.GG 慢 0.8s 时，英雄名在 OP.GG 数据之前就发布
* 回放测试 `test_no_process_state_ui.py::test_full_game_events_never_show_process_state`：
  OP.GG 全程失败 + 动画帧，卡片名称/数值/主界面文案都不含"识别中"

> 真机整局验证需要在有对局时进行（当前客户端处于 Lobby，无法开一局）。
> 下一局可看日志确认：`[CHAMPION] 候选英雄确定: <中文名> / <id>` 之后
> 不会出现任何 `识别中`；卡片要么显示稳定快照，要么数值为 "—"。
