# 两个实机问题修复：OP.GG 数值缺失 + "识别中…"抖动

日期：2026-09-22
机器：2560x1440，24 核，Python 3.12.10，rapidocr_onnxruntime 1.4.4

## 一、问题一：识别成功但没有强度/欢迎度

### 根因（真机日志 13:07~13:08）

```
13:07:49 [WARNING] [OPGG] 英雄统计获取失败: OP.GG 请求失败: ConnectError:
13:07:50 [WARNING] [OPGG] OP.GG request failed (championId=222): ConnectError
13:08:25 [OPGG] QueryStats champion=Jinx(222) augment=1328
13:08:25 [OPGG] CacheStatus=MISSING        <- 该英雄一条数据都没有
13:08:25 [OPGG] popular=暂无
13:08:25 [FLOW] Recommended=[]             <- 三个槽都没有数值
```

链路本身没断：**是 OP.GG 请求失败（ConnectError）且该英雄没有本地缓存**
→ `stats=None` → 卡片只剩名称（名称来自腾讯字典，不依赖 OP.GG），
强度/欢迎度没有数值；而且旧实现只有内存查表，**失败后整局都不会再试**。

（用真实保存的响应样本跑同一链路，三个 augment 都能正确匹配到 performance/popular，
所以不是解析或字段映射问题。）

### 修复

| 位置 | 内容 |
| --- | --- |
| `src/overlay/widgets.py` | 数据缺失占位符 `暂无` → **`—`**（与"识别中…"彻底区分） |
| `src/opgg/provider.py` | ① 补齐 §7 要求的日志：`champion_id=xxx request start` / `augment stats count=xxx` / `matched augment_id=xxx performance=xx popular=xx` / `augment_id=xxx not found`；② 新增 `maybe_retry_missing()`：拿不到数据时按英雄低频重试（30 秒一次，后台任务，不阻塞 OCR） |
| `src/opgg/client.py` | 请求日志：`request start <path>` / `response status=xxx` / `request failed` |
| `src/live.py` | payload 增加 `data_state`（READY / MISSING），日志同步打印 |
| `src/ui/debug_window.py` | Debug 页显示每张卡的"数据="状态 |

接口与字段口径**未改**：`GET /api/contents/stats/champions/{championId}/aram-augments`，
`id / tier / performance / popular`；`performance`=强度，`popular`=欢迎度，
按英雄只请求一次，本地按 augment_id 匹配（不逐卡请求 API）。

## 二、问题二：识别成功后频繁变"识别中…"

### 根因（真机日志 13:08:57~13:09:07）

```
13:08:57 [AUGMENT] 槽位刷新中（pending=[1]），Overlay 保持显示
13:08:57 [AUGMENT] 槽位刷新中（pending=[0, 1]），Overlay 保持显示
13:08:57 [AUGMENT] 槽位刷新中（pending=[0, 2]），Overlay 保持显示
... 每秒钟 2~4 条，持续 10 秒，pending 组合一直在跳
```

旧实现里"像素哈希有任何变化"就进 `CHANGED_PENDING`（显示"识别中…"），
而卡面本身有动画/光照/噪点 → 0.75s 后 OCR 回来又变回名称 → 反复抖动。
也就是把**识别状态**和**数据状态**混在了一起。

### 修复：两个状态彻底分开

```
识别状态：RECOGNIZED / CHANGED_PENDING（只有真换牌才 pending）
数据状态：READY / MISSING（缺失显示 "—"）
```

`src/ocr/card_reader.py` 的逐槽状态机（阈值来自 2026-09-22 实测）：

```
真换牌（不同海克斯）：平均差 16.0~22.1，强变化像素 8.1~13.6%
噪点 ±6 / 亮度 +8%：平均差 3.2~3.9，强变化 0%
中部高光动画：       平均差 5.9，强变化 0%

1. 像素与上次确认一致          -> RECOGNIZED，0 次 OCR
2. 变化属于动画/光照/噪点      -> 保持 RECOGNIZED，不 OCR、不显示"识别中…"
3. 变化明显但还没过 0.75s      -> 只有"像真换牌"才 pending
4. 过了间隔 -> 只 OCR 该槽：
     认出同一个 augmentID      -> 保持 RECOGNIZED（零 UI 抖动）
     认出不同 augmentID        -> RECOGNIZED(新 id)
     没认出来                  -> 变化大才 pending，否则保持 RECOGNIZED
```

推荐逻辑不变（popular 最大者），**pending 槽不参与推荐**，
另外两个已确认的槽继续显示推荐星标；隐藏条件仍然只看"三个槽一个都确认不了"。

## 三、日志验证（`research/opgg_flow_check.py`，用真实响应样本）

```
[OPGG] champion_id=157 request start（aram-augments）
[OPGG] request start /api/contents/stats/champions/157/aram-augments
[OPGG] response status=200
[OPGG] augment stats count=202（champion_id=157）
[OPGG] matched augment_id=1325 performance=63.75 popular=0.0
[OPGG] matched augment_id=1115 performance=83.97 popular=0.68
[OPGG] matched augment_id=1079 performance=88.23 popular=1.68
[OPGG] augment_id=999999 not found（champion=Yasuo(157), 无法计算强度/欢迎度）
OP.GG 请求次数 = 1（按英雄只请求一次）

slot0  玻璃大炮    强度：63.75  欢迎度：0.00%  [READY]
slot1  最万用的瞄准镜 强度：83.97  欢迎度：0.68%  [READY]
slot2  战争交响乐   强度：88.23  欢迎度：1.68%  [READY]  ★ 推荐

reroll slot1 → slot1 显示"识别中… 强度：— 欢迎度：—"，其它两槽保持，
推荐用剩余两槽临时计算（[1079]）→ 新卡识别完成后推荐重算

失败路径：名称照常显示，强度/欢迎度 = —，[data_state=MISSING]，已安排后台重试
```

## 四、测试与性能

```
259 passed, 2 skipped
  （跳过：OP.GG 在线用例——当前网络确实不可达；互斥体用例——正式版正在运行）
新增 tests/test_opgg_display.py（12 个用例）：已识别+加载中/OCR 失败保持 id/
  OP.GG 失败显示 —/OP.GG 成功显示数值/popular 10·25·18 推荐第二个/
  单槽 reroll 其它槽不 pending/加载中不隐藏 Overlay/新局不串英雄数据/英雄切换换数据
新增 tests/replay_harness.py（真实截图回放工具，供两个测试文件共用）

CPU 优化（重跑 A~E，未回滚）：
  [A] 普通游戏 30 秒  0.01 核 / 0.02%   OCR 0
  [B] 首次出现 10 秒  0.16 核 / 0.65%   OCR 3（原来 9，动画帧被正确忽略）
  [C] 稳定 30 秒      0.01 核 / 0.03%   OCR 0
  [D] 刷新一次        changed_slots=[1] -> OCR slots=[1]，1 次
  [E] 游戏结束        OCR 0 / 抓屏 0
```
