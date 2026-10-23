# 数据链路修复：英雄识别不依赖皮肤/OCR + OP.GG 数值真正到位

日期：2026-09-22

## 一、问题一：英雄识别慢 / 显示"识别中"

### 完整链路检查结论

英雄链路本身是 LCU/Live Client（没有 OCR、没有皮肤识别），**真正的阻塞点在 OP.GG**：

```
ChampionTracker._activate()
  -> provider.activate_champion()        # await
       -> preload_champion()             # 2 个 HTTP 请求（英雄统计 + 海克斯统计）
            hero stats  timeout 20s
            augments    timeout 20s
```

`_activate()` 被 `poll_candidate()` / `confirm_final()` await，
也就是说**网络慢/失败时，整个扫描循环都要等它**：

* 英雄名虽然先发布了（`_publish` 在 `_activate` 之前），
* 但循环进不去 → 海克斯牌面识别被推迟 → 用户看到的就是"英雄定了却迟迟没有反应 / 长时间识别中"。

另外 "识别中…" 之前会被这些情况触发（都是错的）：
OP.GG 请求中、请求失败、Live Client 暂时没数据、OCR 普通单帧失败。

### 修复

| 位置 | 内容 |
| --- | --- |
| `src/opgg/provider.py` | 新增 `switch_active_champion()`（**同步**切换：废弃旧 snapshot、清空 active 数据引用）+ `start_preload()`（后台预取，同一英雄只跑一个任务） |
| `src/opgg/provider.py` | 新增 `StatsState`：`NOT_REQUESTED / LOADING / READY / ERROR`；`stats_version()` 在 READY 时 +1 |
| `src/opgg/provider.py` | 退避重试 1s → 2s → 5s（`RETRY_BACKOFF`），由主循环 `maybe_retry()` 驱动；**READY 后不再请求** |
| `src/live.py` | `ChampionTracker._activate()` 改成 `activate_champion_background()`：**只同步切换 + 后台请求**，循环再也不等网络；完成后回调更新 `cache_status/data_patch` |
| `src/ocr/card_reader.py` | "识别中…"只保留给**真换牌**：像素差异属于动画/光照（实测平均差 ≤8、强变化 <3%）时保持 RECOGNIZED；OCR 单帧失败也保持已识别结果 |

英雄名链路的真实诊断（真 LCU，2026-09-22 19:11）：

```
真实 LCU champion-summary: 242 条，耗时 27 ms
  championId=157 -> 疾风剑豪 / Yasuo
  championId=110 -> 惩戒之箭 / Varus
  championId=131 -> 皎月女神 / Diana
```

## 二、问题二：强度/欢迎度经常拿不到

### 完整链路检查结论

逐层排查（真机日志 + 真实网络诊断）：

```
HTTP      : 13:07 那局是 ConnectError（同一分钟前一个请求还成功 → 瞬时网络/限流）
            打包版还漏带了 certs/windows_ca_bundle.pem（会退回 certifi）
JSON parse: 正常（真响应 193 条解析成功）
index     : augment_id 统一 int（新增强制转换，防止 "1325" 字符串混入）
match     : 正常（match augment_id=1113 performance=77.86 popular=4.26）
payload   : 正常
UI        : 正常（缺失显示 "—"）

真正让"数值经常拿不到"的原因有两个：
  1) 失败后不会重试（旧实现只查内存，失败一次整局都没有数据）
  2) 数据晚到不会重画：牌面稳定后循环不再发布 payload，
     即使 OP.GG 后面成功了，卡片上的 "—" 也不会更新
```

### 修复

* 失败重试：`ERROR` 状态按 1s/2s/5s 退避重试，直到 `READY`（不阻塞 OCR）
* **数据晚到自动补上**：主循环每轮比较 `provider.stats_version(active_champion_id)`，
  变化时用当前牌面的 augment_id 重新查本地表并重画 Overlay
  （`refresh_slot_stats()` + `published_stats_version`）
* 打包带上 `certs/windows_ca_bundle.pem`；`verify_option()` 在 bundle 缺失时
  退回**系统证书库**（而不是 httpx 自带的 certifi）
* 日志按 §F 对齐：`active_champion_id=` / `request_start` / `url=` / `status=` /
  `stats_count=` / `match augment_id=... performance=... popular=...` / `MISS augment_id=...`

## 三、真实网络诊断（不是 mock）

```
& ".venv\Scripts\python.exe" research\opgg_flow_check.py --live 110

[OPGG] request_start
[OPGG] url=/api/contents/stats/champions/110/aram-augments
[OPGG] status=200
JSON 层：拿到响应，data 条数 = 193
索引层：stats_count=193，类型检查 augment_id={'int'}
[OPGG] match augment_id=1113 performance=77.86 popular=4.26
[OPGG] match augment_id=1356 performance=83.3  popular=3.84
[OPGG] match augment_id=1071 performance=82.96 popular=3.48
payload -> Overlay: slot0 强度：77.86 欢迎度：4.26% [READY] ★ 推荐 ...
结论：HTTP / JSON / 索引 / 匹配 / payload 全部通过（真实网络）
```

## 四、测试与性能

```
273 passed（无 skip；LCU 在线用例这次也真跑了）
新增 tests/test_champion_recognition.py（9 个）：
  championId 0→157 立即显示中文名且不等待 OP.GG / A→B→C candidate 实时更新 /
  InProgress Live Client 覆盖 candidate / 皮肤后缀不影响 championId /
  英雄链路禁止出现 OCR（结构性断言）/ OP.GG 按 active_champion_id 请求 /
  状态机 NOT_REQUESTED→LOADING→READY
扩展 tests/test_opgg_display.py（14 个）：失败重试 1s/2s/5s、
  第一次失败第二次成功 → 卡片 "—" 自动变成数值（不用 reroll）、
  missing/cosmetic/reroll 三类状态、推荐只算 popular

CPU 优化（重跑 A~E，未回滚）：
  普通游戏 30s 0.00 核 / 0.00% OCR 0 ｜ 首次出现 10s 0.10 核 / 0.44% OCR 3
  稳定 30s 0.00 核 / 0.00% OCR 0 ｜ 刷新 changed_slots=[1]→OCR 1 次 ｜ 结束 OCR 0
```

## 五、还没做的一件事

真机"整局实况"验证：需要你在下一局用正式版跑一次。
要看的日志顺序（现在每一步都有明确独立日志）：

```
[CHAMPION] 候选英雄确定: <中文名> / <id>          <- 英雄名（不等网络）
[OPGG] active_champion_id=<id>
[OPGG] request_start / url=... / status=... / stats_count=...
[OPGG] match augment_id=... performance=... popular=...   （或 MISS augment_id=...）
[OVERLAY] show 3 cards
[AUGMENT] OP.GG 数据到位（version=...），已刷新强度/欢迎度：3/3 槽
```

若仍缺数值，日志会直接指出是哪一层：
`request_failed`（HTTP）→ `stats_count=0`/SchemaMismatch（JSON）→
`MISS augment_id=`（匹配）→ 有 match 但 payload/UI 没值（payload/UI 层）。
