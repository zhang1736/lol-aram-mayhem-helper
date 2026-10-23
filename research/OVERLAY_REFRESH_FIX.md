# 刷新单槽时 Overlay 消失 —— 根因与修复

日期：2026-09-22

## 1. 根因（真机日志证据）

正式版日志 `%LOCALAPPDATA%\LOLHexHelper\logs\app.log`：

```
12:53:11  Stage=1 OfferVersion=1 SlotRerolled=[] Offers=[1996, 1152, 2034]
          [OVERLAY] show 3 cards              <- 三卡正常显示
          （玩家点击某一个槽的刷新）
12:53:28  [AUGMENT] 海克斯界面消失（连续 3 次未识别）
          [OVERLAY] hide                      <- 整块 Overlay 被隐藏
          [OCR] 关闭高频扫描窗口 stage=1（reason=OFFERS_GONE）
12:53:40  [OCR] 开启高频扫描窗口 stage=1 (10s, reason=BRIGHTNESS_JUMP)
12:53:48  对局结束（WaitingForStats）        <- 直到结束都没重新显示
```

链路（P0-2 引入的回归）：

```
刷新某个槽
  -> 动画中间帧被 OCR -> 未匹配
  -> 该槽读数被写成"未匹配"（覆盖了原来的好读数）
  -> 0.75s 最小间隔内的后续采样一直复用它
  -> offer.complete == False 连续 3 次
  -> 触发"海克斯界面消失" -> overlay.set_offers([]) + overlay_visible=False
```

另外两槽明明还识别正常，也被一起隐藏了。旧代码（P0-2 之前）因为每轮都重试 OCR，
下一轮通常就能读到稳定后的新卡，所以很少连续 3 次失败 —— 这就是"CPU 优化之后才出现"的原因。

## 2. 修复

| 位置 | 内容 |
| --- | --- |
| `src/ocr/card_reader.py` | ① OCR 未匹配时**保留上一次成功读数**，只标记 `CHANGED_PENDING`（识别中…），不再写入"未匹配"覆盖好数据；② 同一帧失败过就不重试（等像素变化再试），避免空转；③ `OfferSet` 新增 `pending_slots` / `confirmed_slots` |
| `src/live.py` | ① "界面消失"判定从"牌面不完整"改成"**三个槽一个都确认不了**"（`not offer.confirmed_slots`）→ 单槽刷新/失败绝不会隐藏整块 Overlay；② 新增 pending 分支：牌面内容没变但某槽在刷新时，**继续发布 Overlay**（带"识别中…"）而不是跳过；③ 推荐用 `build_offer_payload` 统一计算，pending 槽**不参与**推荐（既不当作 popular=0，也不会让推荐消失） |
| `src/overlay/manager.py` | 新增 `update_offers(offers, pending_slots)`（§11 统一入口） |
| `src/overlay/window.py` | `update_offers()`；pending 卡片显示 `识别中…`，**不隐藏** |
| `src/overlay/widgets.py` | 新增 `PENDING_TEXT = "识别中…"` |

保持不变：`offer_version` 递增、`changed_slots` / `rerolled_slots`、推荐用 popular、
三槽独立刷新、CPU 优化（ONNX 2/1 线程 + 扫描门控 + 只 OCR 变化槽）。

## 3. 回放测试（真实截图 + 真实 RapidOCR）

`tests/test_overlay_refresh.py` 用**真实牌面**回放 `A/B/C -> A/D/C`：

```
Stage=1 OfferVersion=1 SlotRerolled=[] Offers=[1325, 1115, 1079]
  1. 玻璃大炮     强度：70.88  欢迎度：0.49%
  2. 最万用的瞄准镜 强度：81.77  欢迎度：1.93%   ★ 推荐
  3. 战争交响乐    强度：80.07  欢迎度：0.84%
  （玩家刷新中间槽 → 全程 Overlay 保持显示，中间槽显示"识别中…"）
Stage=1 OfferVersion=2 SlotRerolled=[1] Offers=[1325, 1325, 1079]
  1. 玻璃大炮     强度：70.88  欢迎度：0.49%
  2. 玻璃大炮     强度：70.88  欢迎度：0.49%
  3. 战争交响乐    强度：80.07  欢迎度：0.84%   ★ 推荐   <- 推荐重新计算
```

11 个用例覆盖：刷新不隐藏 / 另外两槽保持 / pending 连续多轮不隐藏 /
OCR 失败不隐藏（保留旧读数 + 识别中…）/ 只更新变化槽 / 推荐重算 /
offer_version 递增 / 对局结束仍然隐藏 / GUI 层 `LIVE→LIVE→LIVE` 断言。

## 4. CPU 优化仍然保持（重跑 A~E）

```
[A] 普通游戏 30 秒     CPU 0.00 核 / 0.00%   OCR 0
[B] 海克斯首次出现 10 秒 CPU 0.32 核 / 1.34%  OCR 9 次（约 1.5 秒内）
[C] 海克斯稳定 30 秒    CPU 0.03 核 / 0.12%   OCR 0
[D] 刷新一次           changed_slots=[1] -> OCR slots=[1]，OCR calls=1
[E] 游戏结束 5 秒       CPU 0.00 核 / 0.00%   OCR 0，抓屏 0
```

## 5. 测试

```
248 passed, 2 skipped（OP.GG 在线用例：当前网络不可达）
```
