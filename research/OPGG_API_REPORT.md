# OP.GG API 调查报告（LeagueAkari 实现路线）

日期：2026-09-18　环境：本机真实网络 + `D:\League Akari-1.4.3-win`（实为 1.5.1）
数据版本：`meta.version = 16.18`

---

## 0. 最终结论

**方案 B。**

```
OP.GG
├── 英雄胜率 API          ✅ 存在
└── 海克斯强度 API        ✅ 存在（performance + popular）
    └── 海克斯胜率 API    ❌ 不存在（ARAM: Mayhem）
```

LeagueAkari 当前使用：

> **英雄胜率 API + 海克斯强度 API，但没有使用、也找不到海克斯胜率 API。**

补充一个必须写清楚的“坑”：API 里**确实存在**一个带 `win_rate` 的海克斯接口
（`/api/global/champions/{mode}/{championId}/augments`），但它返回的是 **Arena（斗魂竞技场）** 的数据，
与 ARAM: Mayhem 无关。详见 §4.2，**不要拿它当海克斯胜率**。

---

## 1. LeagueAkari 的 OP.GG 客户端（源码 + 本机 1.5.1 双向确认）

### 1.1 源码确认（`src/shared/http-api-axios-helper/opgg/index.ts`）

```ts
static BASE_URL = 'https://lol-api-champion.op.gg'
```

| 方法 | 实际请求 |
| --- | --- |
| `getChampions(region, mode)` | `GET /api/{region}/champions/{mode}` |
| `getChampion(region, mode, id, position)` | `GET /api/{region}/champions/{mode}/{id}/{position ?? 'none'}` |
| `getVersions(region, mode)` | `GET /api/{region}/champions/{mode}/versions` |
| `getAramBalance()` | `GET /api/contents/aram-balance` |
| `getAramMayhemChampionAugments(id)` | `GET /api/contents/stats/champions/{id}/aram-augments` |
| `getAramMayhemTiers()` | `GET /api/contents/tiers?type=aram_mayhem` |
| `getTiers(type)` | `GET /api/contents/tiers?type={type}` |

### 1.2 本机 1.5.1 安装包确认（`resources/app.asar`）

反编译出来的打包代码与源码完全一致：

```js
static BASE_URL = `https://lol-api-champion.op.gg`
getAramMayhemChampionAugments(e,t={}){return this._http.get(`/api/contents/stats/champions/${e}/aram-augments`,{signal:t.signal})}
getAramMayhemTiers(e={}){return this._http.get(`/api/contents/tiers`,{params:{type:`aram_mayhem`},signal:e.signal})}
```

### 1.3 关键：LeagueAkari 是怎么把国服 KIWI 接到 OP.GG 的

asar 里有一段很有价值的逻辑：

```js
let _ = null
if (s && r === `aram`) {                       // s = championId, r = mode
  let { data: t } = await w.add(`default`, `opgg-load-kiwi-augments`,
      ({ signal: t }) => e.api.getAramMayhemChampionAugments(s, { signal: t }),
      { tags: [`opgg-group`] })
  _ = t
}
```

即：**OP.GG 面板选择 `ARAM` 模式时，就直接去拉 KIWI（= ARAM: Mayhem）的海克斯数据**。
LeagueAkari 的模式下拉只有 `ranked / aram / arena / nexus_blitz / urf / none`，
**没有 `aram_mayhem`** —— 国服 KIWI 就是走 `aram`。

### 1.4 本机 asar 里的海克斯界面（字段标签的权威来源）

英文：

```js
augmentAll:`All`, augmentSilver:`Silver`, augmentGold:`Gold`, augmentPrism:`Prism`,
augmentPerformance:`Perf.`, augmentPopular:`Popular`,
augmentSort:{ default:`Default`, performance:`Performance`, popular:`Popularity` }
```

中文：

```js
augmentPerformance:`强度`, augmentPopular:`欢迎度`,
augmentSort:{ default:`默认`, performance:`强度`, popular:`欢迎度` }
```

渲染代码：

```js
g.value ? [ augmentPerformance + ' ' + e.performance ] : ''
g.value ? [ augmentPopular     + ' ' + e.popular     ] : ''
```

> 这就是 `performance` / `popular` 两个字段语义的**直接证据**，不是猜的。

---

## 2. 任务 1：英雄胜率 API（实测）

### 请求

```http
GET https://lol-api-champion.op.gg/api/global/champions/aram/157/none
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/131.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Origin: https://op.gg
Referer: https://op.gg/
```

**HTTP 200**（0.57s，18249 bytes）

### 实际返回（原样，节选）

```json
{
  "data": {
    "summary": {
      "id": 157,
      "average_stats": {
        "play": 35901,
        "win_rate": 0.542185,
        "pick_rate": 0.0607854,
        "ban_rate": null,
        "kda": 2.386955,
        "tier": 1,
        "rank": 9,
        "tier_data": { "tier": 1, "rank": 9, "rank_prev": 9, "rank_prev_patch": 11 }
      }
    }
  },
  "meta": { "version": "16.18", "cached_at": "2026-09-18 20:41:56" }
}
```

字段路径确认：

```
data.summary.average_stats.win_rate  = 0.542185     (0~1 小数，即 54.22%)
data.summary.average_stats.pick_rate = 0.0607854    (即 6.08%)
data.summary.average_stats.play      = 35901
meta.version                         = "16.18"
```

### 重要更正：`aram_mayhem` 不是合法 mode

你给的 URL：

```
https://lol-api-champion.op.gg/api/global/champions/aram_mayhem/157/none
```

**实测返回 HTTP 422：**

```json
{"message":"Mode was invalid"}
```

实测各 mode 可用性：

| mode | 结果 |
| --- | --- |
| `aram` | **200**（win_rate / pick_rate 正常） |
| `urf` | 200 |
| `nexus_blitz` | 200 |
| `aram_mayhem` | **422 Mode was invalid** |
| `mayhem` / `kiwi` / `arena` / `ranked_solo` / `all` | 422 |
| `ranked` / `classic` | 422，提示 position 必须是 TOP/JUNGLE/MID/ADC/… |

→ **OP.GG 英雄胜率接口没有 `aram_mayhem`，ARAM: Mayhem 的英雄数据走 `aram`。**

同一接口返回的英雄数据里 **没有** `augment_group`（实测 `data` 键为
`boots / core_items / counters / game_lengths / last_items / mythic_items / rune_pages /
runes / skill_evolves / skill_masteries / skills / starter_items / summary / summoner_spells / trends`）。

---

## 3. 任务 2：海克斯 API（实测）

### 请求

```http
GET https://lol-api-champion.op.gg/api/contents/stats/champions/157/aram-augments
```

**HTTP 200**（0.52s，11003 bytes），返回 **202 条**。

### 实际返回（前 3 条原样）

```json
{
  "data": [
    { "id": 1077, "tier": 0, "performance": 97.24, "popular": 6.82 },
    { "id": 1336, "tier": 0, "performance": 91.46, "popular": 5.96 },
    { "id": 1328, "tier": 0, "performance": 93.16, "popular": 4.05 }
  ]
}
```

**全表字段并集只有 4 个**（实测 202/202 条都只有这 4 个键）：

```
id, tier, performance, popular
```

注意：**没有 `rarity`、没有 `name`、没有 `win_rate`、没有 `play`、没有 `pick_rate`。**

### 字段含义（有证据）

| 字段 | 含义 | 证据 |
| --- | --- | --- |
| `id` | 海克斯 ID，与游戏字典 `augmentID` 同源 | `kiwi_augments.json` 中 1077 = `ARAM_SoulSiphon`「灵魂虹吸」 |
| `performance` | **OP.GG 的“海克斯强度”**，实测值域 0–170，约 100 为基准 | LeagueAkari UI 标签 `augmentPerformance: "Perf." / "强度"`，排序项 `Performance` |
| `popular` | **OP.GG 的“欢迎度”（Popularity）**，不是原始 pick rate | UI 标签 `augmentPopular: "Popular" / "欢迎度"`；**实测每个英雄加总正好 = 100.0**（Yasuo/Ahri/Amumu 均为 100.0） |
| `tier` | 按欢迎度分的档位（0 最高 … 5 最低） | tier0 平均 popular 2.80，tier5 平均 0.00；分布 0:19 / 1:49 / 2:9 / 3:14 / 4:30 / 5:81 |

关于 `popular`：因为**每个英雄的所有条目加总恰好 100.0**（202 / 200 / 226 条均是），
它是“该英雄海克斯内部的占比分布（%）”，比“pick rate”更接近“欢迎度占比”。
**建议标注为“欢迎度”，不要直接写成“选用率”。**

关于 `performance`：能确认它是 OP.GG 内部强度分（UI 就叫“强度”），
但 **API 与前端都没有暴露计算公式**，我无法验证它是否由胜率推导。
因此**不能宣称 `performance = 胜率`**，也不宜宣称它的算法。

### 附：API 与网页的差异（202 vs 180）

| 来源 | 条数 | 字段 |
| --- | --- | --- |
| API `aram-augments` | **202** | `id / tier / performance / popular` |
| 网页 augments 页 payload | **180** | 上述 4 字段 + `name / key / rarity / largeIcon / smallIcon / desc / tooltip` |

→ 网页服务端是 **API 统计行 + 游戏字典 join**，`name` / `rarity` / `desc` / 图标都来自字典。

---

## 4. 任务 3：继续寻找“海克斯胜率”

### 4.1 结论先说

**ARAM: Mayhem 的海克斯胜率接口不存在。**

做了 4 类验证：

1. **端点枚举**：`aram-augments` 的 18 种后缀/参数变体全部 404 或字段不变
2. **网页 payload 扫描**：aram-mayhem 的 augments / build / items 三页里数值型 `win_rate` 计数
3. **前端 JS 挖掘**：下载 66 个 chunk（约 1.7 MB）分析其调用的接口
4. **本机 LeagueAkari 1.5.1 打包代码分析**

### 4.2 找到了唯一带 win_rate 的海克斯接口 —— 但它是 Arena

```
GET /api/global/champions/{mode}/{championId}/augments
```

实测 `aram_mayhem` 也能 200，**但这是陷阱**：

| 验证 | 结果 |
| --- | --- |
| 用 `aram_mayhem` 请求 | 200，3 组 × 15 = 45 条 |
| 用 `arena` 请求 | **同一份数据** |
| 用 `aram` / `urf` / `nexus_blitz` 请求 | **同一份数据** |
| 与 `/api/global/champions/arena/157` 的 `data.augment_group` 对比 | **45 个 id 完全相同，逐条 byte-wise 一致（0 处差异）** |

即：**该路由的 `{mode}` 段被忽略，任何 mode 都返回 Arena 的海克斯数据。**

两条硬证据：

1. **Arena 专属字段**：返回体带 `total_place`（平均名次）与 `first_place`（第一名次数），
   这是 Arena 的机制，ARAM 没有名次概念。
2. **ID 空间不同**：

   | 数据集 | 条数 | id 范围 | 样例 |
   | --- | --- | --- | --- |
   | ARAM: Mayhem（`aram-augments`） | 202 | **1001 – 12317** | 1001, 1077, 1336, 1328, 2010 |
   | 上述 `/augments`（= Arena） | 45 | **6 – 2010** | 6, 17, 22, 23, 36, 52 |

   两者**只重合 3 个 id**（1320 / 1344 / 2010），且这 3 个 id 两边数值也不同
   （例：id 1320，Arena `pick_rate=0.0389` vs Mayhem `popular=0.38`）。

**所以：把 `/api/global/champions/aram_mayhem/{id}/augments` 当海克斯胜率是错的。**

如果你确实要 Arena 的海克斯胜率，正确用法：

```http
GET https://lol-api-champion.op.gg/api/global/champions/arena/157
```

```
字段路径: data.augment_group[].augments[]
  win       = 胜场数        (例: 4154)
  play      = 出场数        (例: 7369)
  win_rate  = win / play    (例: 0.5637128511331253，实测 45/45 条完全吻合)
  pick_rate = 选取率        (例: 0.1364)
  total_place / first_place = Arena 名次统计
  rarity    = 1 / 4 / 8（白银 / 黄金 / 棱彩）
```

### 4.3 网页侧扫描（数值型 `"win_rate":<数字>` 计数）

| 页面 | 数值型 win_rate | 说明 |
| --- | --- | --- |
| `/lol/modes/aram-mayhem/{champ}/augments` | **0** | 只有 3 处 i18n 标签字符串（`Stats.win_rate`） |
| `/lol/modes/aram-mayhem/{champ}/build` | **0** | 同上 |
| `/lol/modes/aram-mayhem/{champ}/items` | **65** | **全部是装备方案胜率**，不是海克斯 |

items 页样本（证明是装备）：

```json
{"ids":[3153,6673,3031],"pick_rate":21.18,"play":"522","win_rate":58.04597701149425,"metaId":...}
```

### 4.4 前端 JS 挖掘结果

- 下载 66 个 chunk（含路由专属 `page-*.js`），**没有任何 chunk 出现 `lol-api-champion`**
- 原因：浏览器端**不直接调用 `lol-api-champion`**，而是走 Next.js **server actions**
  （chunk 里可见 `createServerReference(..., "getMetaChampion")`），由 op.gg 服务端去请求
- 前端出现 `/api/` 的地方只有 `ocm-api.op.gg/api/v2/token`（推送 token），与海克斯无关
- chunk 348 有 Arena 海克斯表格组件，表头是 `Stats.pick_rate` / `Stats.win_rate`，
  且对 `aram_mayhem_classic` 模式**主动隐藏这两列**（`!b &&`）

### 4.5 LeagueAkari 打包代码侧

asar 中 `win_rate` 仅 9 处，全部属于**战绩统计 / Arena 表格**；
ARAM Mayhem 海克斯面板只用了 `e.performance` 与 `e.popular`。

---

## 5. 任务 4：网页 API vs LeagueAkari API 对照

```
OP.GG 网页地址：
  https://op.gg/lol/modes/aram-mayhem/{champion-slug}/augments
  （浏览器 → op.gg 自己的 Next.js 服务端 → lol-api-champion，浏览器不直连）

OP.GG API 地址：
  https://lol-api-champion.op.gg

英雄胜率接口：
  GET /api/global/champions/aram/{championId}/none
  → data.summary.average_stats.win_rate / pick_rate / play
  → meta.version

海克斯强度接口：
  GET /api/contents/stats/champions/{championId}/aram-augments
  → data[].performance   （强度）
  → data[].popular       （欢迎度，每个英雄加总 = 100.0）
  → data[].tier          （0–5 档位）
  → data[].id

海克斯胜率接口：
  ❌ 不存在（ARAM: Mayhem）
  ⚠️ 易混淆项：GET /api/global/champions/{任意 mode}/{championId}/augments
     返回的是 Arena 数据，含 win_rate，但不是 Mayhem

选用率接口：
  ❌ 没有独立的“选用率”接口
     popular 是“欢迎度占比”（每个英雄加总 = 100.0），不是原始 pick rate
     pick_rate 只存在于英雄级（average_stats.pick_rate）与 Arena 海克斯级
```

### 每个字段的真实含义

| 字段 | 出处 | 真实含义 | 是否胜率 |
| --- | --- | --- | --- |
| `average_stats.win_rate` | `/champions/aram/{id}/none` | 英雄胜率（0–1 小数） | ✅ 是 |
| `average_stats.pick_rate` | 同上 | 英雄选取率（0–1 小数） | ❌ |
| `average_stats.play` | 同上 | 样本场次 | ❌ |
| `meta.version` | 多数接口 | 数据版本，实测 `"16.18"` | ❌ |
| `performance` | `/contents/stats/champions/{id}/aram-augments` | OP.GG **海克斯强度分**（0–170） | ❌ **不是胜率** |
| `popular` | 同上 | OP.GG **海克斯欢迎度**（英雄内占比，加总 100.0） | ❌ |
| `tier` | 同上 | 欢迎度档位 0–5 | ❌ |
| `augment_group[].augments[].win_rate` | **仅 Arena** `/champions/arena/{id}` | Arena 海克斯胜率（win/play） | ✅ 是，但**仅 Arena** |

---

## 6. 任务 5 / 任务 6：明确声明

关于 ARAM: Mayhem：

```text
LeagueAkari 当前使用：
  英雄胜率 API    GET /api/global/champions/aram/{championId}/none
  +
  海克斯强度 API  GET /api/contents/stats/champions/{championId}/aram-augments

但没有发现海克斯胜率 API。
```

```text
OP.GG（ARAM: Mayhem）
├── 英雄胜率 API      ✅ /api/global/champions/aram/{id}/none
│                        → data.summary.average_stats.win_rate
├── 海克斯强度 API    ✅ /api/contents/stats/champions/{id}/aram-augments
│                        → data[].performance
└── 海克斯胜率 API    ❌ 不存在
                         （唯一带 win_rate 的海克斯接口返回的是 Arena 数据）
```

另外，ARAM: Mayhem **也没有 stage（第 1/2/3/4 次海克斯）维度**，
`aram-augments` 每个英雄只有一份汇总。

---

## 7. 额外收获

### 7.1 腾讯 CDN 的海克斯字典（强烈建议直接用）

从 LeagueAkari 1.5.1 打包代码里发现：

```js
async getKiwiAugments(){ let {data:e} =
  await this._http.get(`https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json`); return e }
```

实测 **HTTP 200，125400 bytes，248 条**：

```json
{
  "augmentID": 1001,
  "name_en": "ARAM_ImTheJuggernaut",
  "name_cn": "泰坦的坚决",
  "mode": "KIWI, KIWI_JADE",
  "level": "kPrismatic",
  "desc": "...", "tooltip": "...",
  "large_Icon": "https://game.gtimg...", "small_Icon": "...",
  "isNew": 0, "isPBE": 0
}
```

与 OP.GG 的 `id` 完全同源（实测对齐）：

| augmentID | level | name_cn | name_en |
| --- | --- | --- | --- |
| 1077 | kGold | 灵魂虹吸 | ARAM_SoulSiphon |
| 1336 | kGold | 升级：无尽之刃 | ARAM_Upgrade_IE |
| 1320 | kSilver | 升级：收集者 | ARAM_Upgrade_Collector |
| 2010 | kPrismatic | 双发快射 | ARAM_DoubleTap |

`level` 取值：`kSilver / kGold / kPrismatic`，正好对应页面上的 白银 / 黄金 / 棱彩 三个 Tab。

**意义**：海克斯字典完全不需要手工维护。
用 `augmentID` 把「OP.GG 的 performance/popular」与「腾讯字典的中文名/等级/描述/图标」直接 join 即可，
中文名还能直接用于 OCR 匹配。

### 7.2 其他

- OP.GG API 不需要 Cookie / 签名；`Origin`、`Referer` 不加也能通
- `aram-augments` 加任何查询参数（`tier` / `version` / `region` / `type` …）都不改变返回字段
- `meta.cached_at` 是服务端缓存时间，可用于判断数据新鲜度
- OP.GG 的 `aram` 模式在 API 层 = 国服的 KIWI（海克斯大乱斗）

---

## 8. 对项目的直接影响（本次不修改代码）

1. **不要做“海克斯胜率”展示项**。按现版 OP.GG，ARAM: Mayhem 海克斯只有 `performance`（强度）与 `popular`（欢迎度）。
2. 想表达“谁更强”用 `performance`；想表达“多少人爱用”用 `popular`，并且**标注为“欢迎度”**。
3. 英雄胜率用 `average_stats.win_rate`，注意 mode 用 `aram`，不是 `aram_mayhem`。
4. 海克斯字典用 §7.1 的腾讯 JSON，不要再维护 CSV。
5. 不要使用 `/api/global/champions/aram_mayhem/{id}/augments` 的 `win_rate` —— 那是 Arena 数据。

---

## 9. 证据文件

| 文件 | 内容 |
| --- | --- |
| `research/opgg_api/champion_aram_157.json` | 英雄胜率接口原始响应 |
| `research/opgg_api/augments_157.json` | `aram-augments` 原始响应（202 条） |
| `research/opgg_api/mayhem_augments_157.json` | `/champions/aram_mayhem/157/augments` 原始响应（Arena 数据） |
| `research/opgg_api/champion_arena_157.json` | Arena 英雄接口原始响应（含 augment_group + win_rate） |
| `research/opgg_api/kiwi_augments.json` | 腾讯海克斯字典（248 条） |
| `research/opgg_api/page_*.html` | augments / build / items 三页原始 HTML |
| `research/opgg_api/_final_check.txt` | 最后确认项输出 |
| `research/opgg_api_probe.py` 等脚本 | 全部为可重跑的验证脚本 |

