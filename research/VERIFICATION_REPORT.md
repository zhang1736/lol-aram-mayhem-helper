# Day 1 技术验证报告：LCU + OP.GG

日期：2026-09-18　验证对象：本机真实环境（国服 / WeGame 客户端）

---

## 0. 结论速览

| 验证项 | 结果 |
| --- | --- |
| LCU 接口本身是否可用 | **可用**（有本机历史连接实证 + 连接方式已确认），但当前客户端未运行，暂时无法在线握手 |
| LCU 客户端类型 | 国服 Tencent 客户端，`region=TENCENT`，`rsoPlatformId=TJ100` |
| OP.GG 能否拿到 ARAM: Mayhem 海克斯数据 | **可以，而且不需要 Selenium** |
| OP.GG 是否需要浏览器 / Cookie / JS | **都不需要**，纯 HTTP GET + 解析服务端渲染数据 |
| OP.GG 是否提供“胜率”字段 | **不提供**（需求文档的关键误判，详见 §3.3） |
| 是否需要修正需求文档 | 需要，至少 3 处（见 §6.1） |

---

## 1. 本机环境事实（实测）

| 项目 | 实测值 | 证据来源 |
| --- | --- | --- |
| 游戏安装目录 | `D:\lol(26)` | 目录扫描 |
| 客户端类型 | 国服 / WeGame + Tencent 定制版 | `wegame_auth.dll`、`wxlogin.dll`、`TASLoginGuard.dll`、`ACE-*.dll` |
| 游戏版本 | **16.18**（`16.18.8174437+…publictencent…`） | Riot Client 日志 `gameVersion` |
| 常玩游戏模式 | **ARAM: Mayhem**（内部代号 `KIWI`，`queueId=2400`） | Riot Client 日志 `gameMode:"KIWI"` |
| 昨日对局 | 2026-09-17 多局 KIWI | Riot Client 日志 + LeagueAkari 写入的物品集 |
| LCU 端口（昨日） | League `7729`；Riot Client `7586` | LeagueAkari 日志 |
| Python 环境 | Anaconda base **3.7.0** / tf38 **3.8.20**，**没有 3.12** | `python -c` 实测 |
| 已装同类工具 | LeagueAkari 1.5.1、Seraphine | 目录 + 日志 |

> `16.18` 是真实版本号；需求文档里的 `26.18` 是笔误。

---

## 2. LCU 实测结果

### 2.1 已经确证的部分

1. **LCU 在国服确实可用**。LeagueAkari 1.5.1 在 2026-09-17 22:24 成功连接本机客户端：

   ```
   [league-client-main] Target client {
     "port": 7729, "pid": 41496, "authToken": "…",
     "rsoPlatformId": "TJ100", "region": "TENCENT",
     "riotClientPort": 7586, "riotClientAuthToken": "…"
   }
   ```

   它随后成功读取对局信息，并把大乱斗物品集写入
   `d:\lol(26)\Game\Config\Global\Recommended\akari1-…-aram-…-16.18.json`。

2. **“LeagueAkari 不支持腾讯服”要打折理解**。
   README 原文是 `Tencent-operated servers are not supported.`，但源码里存在完整腾讯区服定义
   （`TENCENT_TJ100/TJ101/HN1/GZ100…`，`isTencent: true`），且本机日志证明它连上了。
   结论：**LCU 本地接口在国服完全可用**；所谓“不支持”指依赖 Riot 官方 SGP/战绩接口的功能。
   本项目只用 LCU 本地接口，不受影响。

3. **国服客户端启动方式已确认**（决定连接方式）：

   ```
   "partner_product": {
     "arguments": [
       "--riotclient-auth-token={remoting-auth-token}",
       "--riotclient-app-port={remoting-app-port}",
       "--riotclient-tencent", "--no-rads",
       "--disable-self-update", "--region=TENCENT", "--locale={locale}"
     ],
     "executable": "LeagueClient.exe",
     "relative_path": "../LeagueClient"
   }
   ```

   → 客户端由 WeGame 以 `-src=tgp` 拉起，LeagueClientUx **通过命令行参数携带端口与 token**，
   LeagueAkari 的实现正是解析 `LeagueClientUx.exe` 的命令行。

### 2.2 当前状态：无法在线握手

实测 `python research/test_lcu.py`：

```
[RESULT] CLIENT_NOT_FOUND
  no usable LCU lockfile and no running LeagueClientUx process
```

补充实测：

- `D:\lol(26)\LeagueClient\lockfile` **不存在**
- 唯一 lockfile 在 `D:\lol(26)\Riot Client Data\Metadata\riot client\lockfile`，
  **0 字节且时间戳为 2022 年**（陈旧残留）
- 进程列表**没有任何** League / Riot 进程（只有 `wegame.exe`、`wegame_env.exe`）

**结论：要完成“连上去读一次 gameflow / champ-select”的在线验证，必须先启动并登录英雄联盟客户端。**

### 2.3 连接层设计结论

`research/test_lcu.py` 已按实测结论实现两条路径：

1. **进程命令行解析（国服主路径）**：扫 `LeagueClientUx.exe`，取
   `--app-port=`、`--remoting-auth-token=`、`--app-pid=`、`--rso[_-]platform[_-]id=`、`--region=`
2. **lockfile 兜底**：`<install>\lockfile` → `name:pid:port:password:protocol`，Basic Auth `riot:<password>`

再访问 `https://127.0.0.1:{port}`（自签证书必须关闭校验；LeagueAkari 内置固定 CA，可作 V2 增强）。

实测确认可用端点（与 LeagueAkari 源码一致）：

| 端点 | 用途 |
| --- | --- |
| `/lol-gameflow/v1/gameflow-phase` | 阶段检测（None / Lobby / ChampSelect / InProgress …） |
| `/lol-gameflow/v1/session` | 详细会话（含 queueId，可判断 2400=KIWI） |
| `/lol-champ-select/v1/session` | `localPlayerCellId` + `myTeam[].championId` |
| `/lol-champ-select/v1/current-champion` | 直接取当前英雄 ID（快捷接口） |
| `/lol-game-data/assets/v1/champion-summary.json` | **英雄字典正解**：id → 中英文名 / alias |

### 2.4 待补的在线验证清单

客户端启动后跑一次（`test_lcu.py` 覆盖前 4 项）：

1. LCU 握手是否 200
2. `gameflow-phase` 是否返回 `ChampSelect` / `InProgress`
3. `/lol-champ-select/v1/session` 能否取到 `championId`
4. `champion-summary.json` 能否取到英雄名与 alias
5. （进入对局后）`https://127.0.0.1:2999/liveclientdata/allgamedata` 是否可用
   —— **若可用，`activePlayer.level` 可直接判定海克斯阶段（3/7/11/15），不必靠 OCR 猜等级**

---

## 3. OP.GG 实测结果

### 3.1 数据来源

目标页面：`https://op.gg/lol/modes/aram-mayhem/{champion-slug}/augments`

- **HTTP 200，纯 HTML 服务端渲染**（Next.js App Router / RSC）
- **没有 `__NEXT_DATA__`，也没有 `/api/` 接口**
- 全量数据藏在 `<script>self.__next_f.push([1,"…"])</script>` 的 RSC flight 流里
- 把 flight 解码拼接后可定位 `"data":[{…}]` —— 该英雄的**全部海克斯数据**

单个海克斯对象的真实结构：

```json
{
  "id": 1077,
  "tier": 0,
  "performance": 97.24,
  "popular": 6.82,
  "name": "Soul Siphon",
  "key": "ARAM_SoulSiphon",
  "largeIcon": "https://opgg-static.akamaized.net/…/SoulSiphon_large.png",
  "smallIcon": "https://opgg-static.akamaized.net/…/SoulSiphon_small.png",
  "rarity": 4,
  "desc": "<lifeSteal>Heal for 12% of damage done</lifeSteal> by <crit>Critical Strikes</crit>…",
  "tooltip": "Gain <crit>25% Crit Chance</crit> and <lifeSteal>12% Lifesteal…"
}
```

字段语义（实测推断）：

| 字段 | 含义 | 依据 |
| --- | --- | --- |
| `id` | 海克斯数字 ID | — |
| `key` | 字符串 key（`ARAM_SoulSiphon`） | — |
| `name` | 名称（随 locale 变：英文 / 中文） | 见 §3.2 |
| `rarity` | `1=白银 / 4=黄金 / 8=棱彩` | 与页面 Silver / Gold / Prism 三个 Tab 对应 |
| `popular` | **选用率（%）** | 页面默认列表按它降序；亚索第一项 `Soul Siphon` = 6.82 |
| `tier` | **按选用率分的排名档位**（0 最高 … 5 最低） | 实测 tier0 平均 popular 2.80，tier5 平均 0.00 |
| `performance` | OP.GG 表现分（0–170，约 100 像基准线） | **不是胜率**，页面也没展示它 |

### 3.2 中文名可以直接拿

`https://op.gg/zh-cn/lol/modes/aram-mayhem/yasuo/augments` 同样 200、同样结构，只是名称为中文：

| id | 英文 | 中文 |
| --- | --- | --- |
| 1077 | Soul Siphon | 灵魂虹吸 |
| 1336 | Upgrade Infinity Edge | 升级：无尽之刃 |
| … | Critical Rhythm | 暴击律动 |
| … | Mystic Punch | 秘术冲拳 |
| … | Transmute: Prismatic | 质变：棱彩阶 |

→ **海克斯字典可用“同一个 id 抓 en + zh-cn 两个页面”自动生成**，无需手工维护中英对照。

顺带纠正：需求文档举例的“灵魂吸取 / 亮出你的剑”在真实数据里都不存在，亚索当前第一是“灵魂虹吸”。

### 3.3 ⚠️ OP.GG 没有“海克斯胜率”

用真实浏览器打开页面并读取 DOM 实测：

- 每行只有：图标 + 稀有度徽章 + **名称**；页面上的百分比文本数量 = **0**
- 页面控件：`All` / `Silver` / `Gold` / `Prism` 四个 Tab + `Search augments` 搜索框
- 即：**当前 OP.GG 的 ARAM: Mayhem Augments 页面是“排行榜列表”，不是数据表格**
- payload 里不存在 `winRate` / `win_rate` / `games` / `win` 之类字段（只有 `popular`、`performance`）
- 也**没有 stage（第 1/2/3/4 次海克斯）维度**

**结论：需求文档 §1.1 / §14 / §50 中“显示 OP.GG 选用率 + 胜率”按现版数据无法直接满足。**

可选处理（按推荐度）：

1. **改需求**：展示 `选用率(popular)` + `OP.GG 排名档位(tier)` + `OP.GG 表现分(performance，明确标注为表现分)`，不自造评分 —— 符合文档“只展示原始数据”的精神
2. 只展示 `popular`，并说明“OP.GG 暂无胜率数据”
3. 另找提供胜率的数据源（属新需求，不在本 MVP）

### 3.4 覆盖度与“查不到”的情况

- 每个英雄的海克斯条数**不同**：yasuo=180、sett=184、lux=181、garen=172、ziggs=166、ahri=195、amumu=177
- 有些海克斯**在某些英雄页面根本不存在**（Garen 无 `Adamant`；Lux 无 `Echo Cast`）
- `popular=0` 是**真实取值**（亚索 180 条里有 46 条）

→ 直接印证需求文档 §30：必须有“OP.GG 暂无数据”状态，**绝不能显示 0%**。

### 3.5 英雄 slug 规则（已实测）

- 规则：**LCU 的 champion alias 全小写**，无连字符
- 验证通过：`aurelionsol` / `monkeyking` / `reksai` / `ksante` / `belveth` / `chogath` /
  `leblanc` / `velkoz` / `drmundo` / `jarvaniv` / `missfortune` / `twistedfate` / `xinzhao` /
  `kogmaw` / `nunu` / `renata` 全部 200 且有数据
- 反例：`aurelion-sol`、`wukong`、`cho-gath`、`asol` 全部 **404**

→ 英雄字典直接来自 LCU 的 `champion-summary.json`（id / 中英文名 / alias），**不需要手工表**。

### 3.6 性能实测

| 指标 | 实测值 |
| --- | --- |
| 单页耗时 | 1.1 – 3.7 s |
| 单页体积 | 约 0.8 MB HTML |
| 解析耗时 | < 50 ms |
| 进程内缓存命中 | 0.0000 s |

---

## 4. 可能遇到的反爬问题（实测）

实测结果**非常宽松**：

| 检测项 | 实测结果 |
| --- | --- |
| 是否需要 Cookie / 登录 | 否，裸请求即 200 |
| 是否需要 JS | 否，HTML 里就有全量数据 |
| 是否有 Cloudflare 盾 / 挑战 | 否。响应头为 `Server: nginx` + **AWS CloudFront**（`X-Amz-Cf-Id`、`X-Amz-Cf-Pop`、`X-Cache`） |
| 是否校验 User-Agent | 否，**完全不带 UA 也返回 200 且数据完整** |
| 是否有 RateLimit 响应头 | 没有 |
| 12 次连续请求（无间隔） | **12/12 全部 200**，单次 1.1–2.2 s |

仍需注意（属工程防护，非当前已发生问题）：

1. **不要并发轰炸**：串行 + 间隔 ≥1 s；全量预载建议随机 0.3–0.8 s（参考项目做法）
2. **HTML 结构可能改版**：今天的 `"data":[{…}]` 明天可能改名。解析逻辑必须隔离在 `opgg/parser.py`，
   并加“解析到 0 条 = 失败”的显式告警
3. **RSC flight 不是官方接口**：属逆向行为，可能被改（改成客户端 fetch、加签、加 cookie）
4. **地区策略**：不同地区可能内容不同；建议锁定 `op.gg`（非 www 会 308 跳转）
5. **条款风险**：OP.GG 服务条款对自动化有保留；本地个人工具风险低，分发量大时需评估
6. **必须有兜底**：保留“本地缓存 + 离线 CSV”设计；本机已有现成
   `hero_augments.csv`（31133 行，中文英雄名 / 等级 / 排名）可作种子数据

---

## 5. 推荐的数据获取方案

### 5.1 选 Level 2：直接 HTTP + 解析 SSR 数据

> 实测后应选 **Level 2（requests + HTML 解析）**，**完全不需要 Level 3 的 Selenium / Playwright**。

```
champion_slug = lowercase(LCU champion-summary.json 的 alias)
GET https://op.gg/lol/modes/aram-mayhem/{slug}/augments
  ↓
提取所有 self.__next_f.push([1,"…"]) → JSON 解码 → 拼接
  ↓
定位 "data":[ … ] → json.JSONDecoder().raw_decode()   ← 不要用正则抠单个对象
  ↓
得到该英雄全部海克斯：id / key / name / rarity / tier / popular / performance
  ↓
写入本地缓存（SQLite，key = patch + champion + augment_id）
```

**踩坑记录**：用正则 `\{"id":\d+,"tier":\d+,.*?"rarity":\d+.*?\}` 抠对象会**静默漏掉约 11 条**
（描述里带嵌套花括号的行）。改成 `raw_decode` 整个 `data` 数组后条数与页面完全一致（180/180）。
证据脚本：`research/opgg_parse_check.py`。

### 5.2 时机与缓存（对应文档 §40 / §41）

- **进入对局后立刻按当前英雄预载一次**（1 个请求、~0.8 MB、~1.5 s），写入缓存
- 海克斯界面出现时 → **纯本地查表**，做到“识别到即显示”
- 缓存 key：`patch + champion + augment_id`（patch 从页面直接解析，实测 `16.18`）
- OP.GG 失败 → 用缓存；无缓存 → 显示“OP.GG 暂时无法访问”，绝不用 0 顶替

### 5.3 两条与文档不同的实现建议

1. **不要用 Selenium**：纯 HTTP 更快（1.5 s vs 起浏览器 10 s+）、更稳、打包更小（不必带 ChromeDriver）
2. **中文别名无需手工维护**：zh-cn 与 en 页面同 `id`，可自动对齐中英名；
   `augments.json` 的 `aliases` 只需补 **OCR 易错形近字**

---

## 6. MVP 技术风险

### 6.1 必须先修的需求问题

| # | 文档原文 | 实测结论 | 建议 |
| --- | --- | --- | --- |
| 1 | §1.1 / §14 / §50 要求显示“选用率 + 胜率” | OP.GG 页面**没有胜率**，只有 `popular` + `performance` | 改为“选用率 + OP.GG 表现分/档位”，或另找胜率源 |
| 2 | 举例海克斯名“灵魂吸取 / 亮出你的剑” | 真实数据里不存在（亚索第一是“灵魂虹吸”） | 用抓取数据建字典，不要用文档示例 |
| 3 | patch 举例 `26.18` | 真实 patch = **16.18** | 修正 |
| 4 | §18 用固定表 `stage→level(3/7/11/15)` | 未实测；且**不应由 OCR 识别等级** | 优先用 Live Client API 的 `level`，OCR 只识别海克斯名 |

### 6.2 技术风险清单

| 风险 | 等级 | 说明与缓解 |
| --- | --- | --- |
| **OP.GG 数据结构改版** | 高 | 解析依赖 RSC flight 的 `"data":[{…}]`。缓解：解析隔离 + 单测 + 解析到 0 条即告警 + 本地缓存/CSV 兜底 |
| **没有 Python 3.12** | 高 | 本机只有 3.7 / 3.8。`rapidocr_onnxruntime` 要求 3.9–3.12。Day 2 第一件事就是装 3.12 并建独立 venv |
| **OCR 识别中文海克斯名** | 高 | 海克斯名有“升级：无尽之刃”“质变：棱彩阶”等冒号/易错字；且不同英雄海克斯池不同（166–195 条）。缓解：ROI + 快速模糊匹配 + 只在该英雄的候选集里匹配（把 180 条缩到 3 张卡片的候选范围） |
| **阶段判定（stage 1–4）** | 中 | 屏幕 OCR 无法得知是第几次海克斯。缓解：优先用 Live Client API 等级；退化方案是按“本局已确认轮次”计数，绝不让刷新误加 stage |
| **刷新检测可靠** | 中 | 需要 3 张卡片哈希 + 连续 2–3 帧稳定判定；动画期间 OCR 抖动是主要误报源 |
| **Overlay 与反作弊** | 中 | 客户端目录内含 `ACE-BASE.sys` 等 ACE 反作弊组件。**只做外部截图 + 透明置顶窗口**（不注入、不读写游戏内存）是安全的；但要避免使用会 hook 游戏进程的方案 |
| **全屏 / HDR / 多显示器** | 中 | 文档已限定“无边框 + 单显示器 + 2K 基准缩放”，与参考项目一致。需要实测 1920×1080 / 2560×1440 / 3840×2160 三个分辨率 |
| **点击穿透与焦点** | 中 | PySide6 需要 `WA_TransparentForMouseEvents` + `WS_EX_TRANSPARENT|WS_EX_LAYERED`，否则会抢鼠标导致无法选海克斯 |
| **国服客户端启动方式** | 低 | 已确认：必须走 WeGame，程序不能自己拉起客户端；程序只做“发现并连接” |
| **OP.GG 访问不稳定** | 低 | 实测 12/12 成功；但必须有缓存兜底 |
| **性能占用** | 低 | 平时 300–500 ms 低频截图，进入海克斯界面才 100–200 ms 高频；全量预载只在进对局时做一次 |

### 6.3 待实测项（Day 3–4）

1. 国服客户端 LCU 在线握手 + `championId` 读取（需先启动客户端）
2. `/lol-champ-select/v1/current-champion` 在国服是否可用
3. Live Client Data API（2999）是否可用、是否给 `level`
4. 海克斯选择界面的真实 ROI 与卡片坐标（2560×1440 基准）
5. 实际屏幕截图 → RapidOCR 的中文识别准确率（目标 ≥95%）

---

## 7. 结论与下一步

1. **OP.GG 这条路完全打通**，而且比预期简单得多：纯 HTTP + 解析 SSR，无需浏览器、无需 Cookie，反爬压力极低。
2. **LCU 这条路技术上可行**（有本机实证），只差一次“客户端运行中”的在线握手。
3. **需求文档需要按 §6.1 修正 4 点**，其中“胜率”缺口最关键 —— 建议先确认展示口径再进 Day 2。
4. **Day 2 的工程代码可以在确认展示口径后开始**；在此之前建议先做客户端在线验证（本报告 §2.4 清单）。

### 立即可以执行的两条命令

```powershell
# 1) 启动并登录英雄联盟客户端后，验证 LCU
& D:\Anaconda3\python.exe "D:\lol海克斯选择\research\test_lcu.py"

# 2) 验证 OP.GG 取数
& D:\Anaconda3\python.exe "D:\lol海克斯选择\research\test_opgg.py"
```

---

## 8. 本次验证产出的文件

| 文件 | 说明 |
| --- | --- |
| `research/test_lcu.py` | LCU 探针（Day-1 验收脚本），支持 `--wait` / `--watch` |
| `research/test_opgg.py` | OP.GG Provider + 验收测试（5 英雄 / 11 海克斯） |
| `research/opgg_probe.py` | OP.GG 页面数据源探测（多英雄 + 信号统计） |
| `research/opgg_decode.py` / `opgg_schema.py` | RSC flight 解码与字段结构分析 |
| `research/opgg_order_check.py` | 验证“页面排序 = popular 降序” |
| `research/opgg_parse_check.py` | 证明正则抠对象会漏 11 条、`raw_decode` 才完整 |
| `research/opgg_fields.py` | `rarity` / `tier` / `popular` / `performance` 语义分析 |
| `research/opgg_locale_and_headers.py` | 中文页 / 响应头 / 无 UA / 连续请求实测 |
| `research/opgg_slug_check.py` | 英雄 slug 规则实测 |
| `research/opgg/*.html` | 抓取到的原始页面（证据留档） |
| `research/upstream/LeagueAkari` | LeagueAkari 源码（LCU 连接方式参考） |
| `research/upstream/hextech-helper` | Nyx0ra 参考项目源码 + `hero_augments.csv` 种子数据 |

