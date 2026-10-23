# 选完英雄后状态回到“等待游戏”的修复报告

## 实际故障

日志显示 LCU 已正确读到英雄：

```text
[CHAMPION] 候选英雄确定: 机械公敌 / 68
[CHAMPION] 最终英雄(Live Client): 机械公敌 / 68 level=3
```

随后每约 5 秒出现：

```text
CardReader.__init__() got an unexpected keyword argument 'engine'
对局清理完成
```

异常发生后 `play_one_game()` 进入 `finally`，调用
`reset_hub_session_state()`，所以主界面英雄被清空，状态回到“等待游戏”。

## 根因

`play_one_game()` 使用了错误的构造参数：

```python
CardReader(capture, engine=shared_ocr_engine(), ...)
```

实际构造函数参数名是 `ocr_engine`。现有集成测试使用假 `CardReader`，没有暴露
这个关键字参数错误。

## 修复

- 改为 `CardReader(capture, ocr_engine=shared_ocr_engine(), ...)`。
- 增加真实调用点回归测试，断言只使用 `ocr_engine`，不再接受错误的 `engine`。
- 保持英雄确认后的 Hub 状态不被无关异常重置。
- 版本号继续使用 1.0.2，不升级版本。

## 验证

- `ChampSelect -> InProgress` 下英雄保持为中文名。
- `CardReader` 创建成功后才进入 OCR 循环。
- 完整测试：`384 passed, 1 skipped`（跳过项为正式助手运行时的全局热键单例测试）。

## 刷新后浮层不变化

实机日志 `00:51:41` 首次识别为：

```text
Offers=[3116, 1996, 1098]
```

随后刷新动画逐帧变化，旧的“同一新帧连续两次完全相同”条件一直无法满足，
因此新牌面没有进入 OCR，浮层只能继续显示旧快照。

修复方式：

- 仍优先使用连续两帧相同来快速确认；
- 检测到真实变化超过 0.35 秒后，即使动画每一帧哈希不同，也强制尝试一次 OCR；
- 同一槽两次 OCR 仍至少间隔 0.45 秒，避免恢复高 CPU；
- OCR 失败后继续按 0.75 秒冷却重试。

## 选择海克斯后浮层不消失

实机日志显示选择完成后三槽都进入待确认，旧逻辑把“全部待确认”始终当作
界面仍存在，导致隐藏判定被跳过。

现在分两种情况：

- 首次识别、还没有发布过浮层：三槽失败时保持扫描和重试；
- 已经显示过浮层：三槽连续无法确认达到 `MISS_SECONDS=1.2` 后立即清空并隐藏。

单槽刷新时只要另外两张仍已确认，浮层继续保留，不会误闪。

## 同等级连续两轮与 KDA 第五轮

原实现把 `stage` 同时当作“等级节点”和“实际轮次”，只能表达 1~4 轮。
现在新增独立字段 `augment_round`：

- `stage` 继续表示等级节点；
- `augment_round` 表示本局实际第几次海克斯；
- 同一等级下界面消失后再次出现，`augment_round` 会递增；
- KDA 额外选取不需要写入等级表，实际第 5 次出现时自然得到 Round 5；
- 每轮都会重置 `offer_version`、`rerolled_slots` 和逐槽刷新计数；
- 上一轮界面消失后会立即重新武装扫描门控，不再受 2 秒冷却阻塞。

界面和 Debug 分别显示“第 N 次海克斯”与 `augment_round`，不会再把等级
Stage 误当成实际轮次。
