# 《海克斯数据显示与推荐规则》技术验证

日期：2026-09-18　数据版本：16.18
范围：只验证这份规则文档里**可被实测证伪的技术断言**（§10 / §11 / §13 / §16）。
不涉及代码实现。

---

## 结论速览

| 规则条款 | 断言 | 实测 | 判定 |
| --- | --- | --- | --- |
| §10 | 英雄统计 API 支持 `region` + `tier` 参数 | 支持，且**数值确实变化** | ✅ 正确 |
| §10 | 参数为 `region = kr`、`tier = ibsg` | 合法且有效；非法 tier 返回 422 | ✅ 正确 |
| §11 | `aram-augments` 接口没有 region/tier 参数 | 参数被**完全忽略**（响应逐字节相同） | ✅ 正确 |
| §11 | 无法过滤时应记为 unknown / Aggregate | 必须这样做，否则就是伪造口径 | ✅ 正确 |
| §16 | `popular` 可能是 `null` | 实测 **0 个 null**；API 用 `0` 表示极低占比 | ⚠️ 需微调措辞 |
| §13 | 需要记录 `patch` | `aram-augments` **没有 `meta` 字段**，patch 得从别处取 | ⚠️ 补充数据来源 |

---

## 1. §10 实测：`region=kr` + `tier=ibsg` 是真实生效的

英雄统计接口的 region 是**路径段**，tier 是**查询参数**：

```
GET https://lol-api-champion.op.gg/api/{region}/champions/{mode}/{championId}/{position}?tier={tier}
```

实测（Yasuo = 157，mode = `aram`）：

| 请求 | code | win_rate | pick_rate | play |
| --- | --- | --- | --- | --- |
| `/api/global/champions/aram/157/none` | 200 | 0.542185 | 0.0607854 | 35901 |
| `/api/global/champions/aram/157/none?tier=all` | 200 | 0.542185 | 0.0607854 | 35901 |
| `/api/global/champions/aram/157/none?tier=ibsg` | 200 | 0.542542 | 0.0764027 | 11894 |
| **`/api/kr/champions/aram/157/none`** | 200 | 0.518902 | 0.0498761 | 6560 |
| `/api/kr/champions/aram/157/none?tier=all` | 200 | 0.518902 | 0.0498761 | 6560 |
| **`/api/kr/champions/aram/157/none?tier=ibsg`** | 200 | **0.523834** | **0.0698944** | **1930** |
| `/api/kr/champions/aram/157/none?tier=gold_plus` | 200 | 0.514768 | 0.0763306 | 1659 |
| `/api/kr/champions/aram/157/none?tier=emerald_plus` | 200 | 0.516807 | 0.0833377 | 476 |
| `/api/kr/champions/aram/157/none?tier=master_plus` | 200 | 0.595238 | 0.101498 | 42 |
| `/api/na/champions/aram/157/none?tier=ibsg` | 200 | 0.510659 | 0.0593786 | 1032 |
| `/api/kr/champions/aram/157/none?tier=bogus_tier` | **422** | — | — | — |

关键点：

1. `kr` 与 `global` 的数值**明显不同**（play 6560 vs 35901）→ region 真的生效，不是装饰
2. `ibsg` 与 `all` 的数值**明显不同**（play 1930 vs 6560）→ tier 真的生效
3. `tier=all` 与不传 tier 完全一致 → `all` 就是默认口径
4. 传非法 tier 返回 **422**，说明服务端**校验**了这个参数
5. `kr + ibsg` 样本量 = 1930 场（单英雄单模式），量级可用

→ 规则文档 §10 可以照写：

```python
DATA_REGION = "kr"
DATA_TIER = "ibsg"
```

请求形如：

```
/api/kr/champions/aram/{championId}/none?tier=ibsg
```

---

## 2. §11 实测：海克斯接口确实**没有** region/tier

```
GET /api/contents/stats/champions/157/aram-augments
```

6 种变体实测：

| 请求 | code | rows | 首行 |
| --- | --- | --- | --- |
| `contents/stats/champions/157/aram-augments` | 200 | 202 | `{"id":1077,"tier":0,"performance":97.24,"popular":6.82}` |
| `...?tier=ibsg` | 200 | 202 | 同上 |
| `...?region=kr` | 200 | 202 | 同上 |
| `...?region=kr&tier=ibsg` | 200 | 202 | 同上 |
| `/api/kr/contents/stats/champions/157/aram-augments` | **404** | — | 路由不存在 |
| `/api/kr/contents/stats/champions/157/aram-augments?tier=ibsg` | **404** | — | 路由不存在 |

**逐字节比对结果：4 个 200 响应的 payload 完全相同（distinct payloads = 1）。**

也就是说：给海克斯接口加 `region=kr&tier=ibsg` 是**纯粹的空操作**，
数据仍然是 OP.GG 的全局聚合值。region 甚至不能作为 `/api/contents/...` 的路径段。

同样地，`/api/contents/tiers?type=aram_mayhem` 无论加不加 region/tier 都返回 **173 行同一份数据**。

→ 规则文档 §11 的处理方式**正确且必要**：

```
海克斯统计的 stats_scope 必须标为 unknown / "OP.GG Aggregate"，
绝不能显示成 "KR / IBSG"。
```

同时 §12 的界面要求（英雄数据 = Korea / Iron–Gold，海克斯统计 = OP.GG Aggregate）也是正确的，
因为这个项目里**两类数据的口径天然不一致**，必须分别标注。

---

## 3. §16 微调：API 不会返回 `null`，而是返回 `0`

实测 3 个英雄的全部行：

| 英雄 | 行数 | `popular`/`performance` 为 null | `popular == 0` | 字段集合 |
| --- | --- | --- | --- | --- |
| Yasuo | 202 | **0** | 64 | 一致（1 种 keyset） |
| Garen | 195 | **0** | 44 | 一致 |
| Lux | 205 | **0** | 87 | 一致 |

所以：

- 接口**从不返回 null**；占比极低时返回的是 `0`（真实数值，代表“占比约等于 0”）
- 程序里的 `popular is None` 只会出现在**“该海克斯不在响应列表里”**这种情况
  （实测确有：Garen 页面没有 `Adamant`、Lux 没有 `Echo Cast`）

建议把 §16 的措辞改成：

```
popular 缺失（不在响应中） → 显示「欢迎度：暂无数据」，不得当成 0
popular == 0（接口返回 0）  → 是真实数值，按 0 参与推荐比较
```

这样既满足“不把 null 当 0”，也不会把接口真实返回的 0 误判成缺数据。

---

## 4. §13 补充：`patch` 不能从海克斯接口取

实测 `aram-augments` 的响应**只有 `data`，没有 `meta`**：

```json
{"data":[ ... ]}          ← 没有 meta
```

而英雄统计接口有：

```json
"meta": { "version": "16.18", "cached_at": "2026-09-18 20:41:56" }
```

→ §13 要求记录 `patch` 时，应取**英雄统计接口的 `meta.version`**（或网页 payload），
不要指望海克斯接口给 patch。

---

## 5. 需要提请注意的实现风险（3 条）

### 5.1 字段名撞车：API 的 `tier` ≠ 配置的 `DATA_TIER`

- OP.GG 海克斯响应里的 `tier` = **欢迎度档位 0–5**
- 规则文档里的 `tier`（`ibsg`）= **段位区间**

两者同名但完全无关。建议数据模型里把前者命名为 `opgg_tier`，

```python
class AugmentStats:
    augment_id: int
    performance: float | None
    popular: float | None
    opgg_tier: int | None        # 0~5 欢迎度档位，勿与 DATA_TIER 混淆
    source: str
    stats_scope: str             # "OP.GG Aggregate"
```

避免以后出现 `tier == "ibsg"` 与 `tier == 0` 混用的 bug。

### 5.2 推荐依据与口径不一致要在 UI 上讲清楚

推荐用的是 `popular`，而 `popular` 是**全局聚合**口径，不是 KR/IBSG。
英雄名/强度/欢迎度同屏显示时，建议在卡片或 Debug 面板明确写出
「海克斯数据：OP.GG Aggregate」，否则用户会以为推荐也是韩国黄金以下口径。

### 5.3 `popular` 是“英雄内占比”，跨英雄不可比

实测每个英雄的 `popular` 加总 = 100.0。
因此 §4 的规则**只在同一英雄的三个当前选项之间比较时成立**——
而 MVP 场景正好满足（三个选项必然同英雄），所以规则可用；
但**不要**用它做跨英雄比较（例如“哪个英雄的海克斯更强”）。

---

## 6. 附录：证据文件

| 文件 | 内容 |
| --- | --- |
| `research/opgg_kr_ibsg_check.py` | 本次全部请求的可重跑脚本 |
| `research/opgg_api/_kr_ibsg_check.txt` | 本次实测原始输出 |
| `research/opgg_api/_null_check.txt` | null / 0 统计输出 |
| `research/OPGG_API_REPORT.md` | 上一轮 OP.GG API 总报告 |

