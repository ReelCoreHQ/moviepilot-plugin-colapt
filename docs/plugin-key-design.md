# 插件密钥方案：最终综合裁决

> **历史文档**：保种组已在 0.11.0 拆到独立插件 [ColaSeedGroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup)，本仓库不再有这部分代码。文中的会话登录、验证码、Cookie + CSRF 写法都已作废：0.12.0 起插件只用可乐PT 开放 API（`X-API-Key`，文档见站点 `/api-docs`）。

三视角（安全 / 协议 / 运维）+ 主模型代码验证，已合并。
事实基础：`docs/plugin-key-facts.md`（逐条读过代码）。
原始提案：`/home/deepseek/cola-plugin-apikey-design.md`（协议视角 899 行完整稿）。

---

## 0. 定论（三方一致，不再讨论）

1. **一把 key = 账号 × 插件实例 × 能力集的一次性票据**
2. **`expires_at = last_accepted_report_at + 7200s`**，只有**被接受的汇报**续命
3. **明确否决"心跳续命"** —— 那让"不汇报但进程活着"的客户端永久持凭据，TTL 当场作废
4. **必须有长寿命自助凭证**，否则 2h TTL 等于"每 2 小时解一次验证码"：
   安全叫 `binding_secret`，协议叫 `signing_key`，运维叫 `DK` —— **同一个东西**
5. **指纹绝不参与硬拒绝**（容器重建/换宽带必误杀）
6. **重签是 UPDATE 不是 INSERT**（否则表无限膨胀）
7. **`user_id` 只来自 key 行，绝不来自 body**（现有代码已正确）

---

## 1. 三方分歧与裁决

| 议题 | 运维 | 安全 | 协议 | **裁决** |
|---|---|---|---|---|
| 续命判据 | 任意已鉴权联系 | 被接受的上报 | 被接受的上报 | **上报**（运维的两条硬证据用第 2 节补丁消解，不必改判据） |
| 过期后能否用旧 token 自愈 | — | 能（gap≤24h） | **不能** | **不能**（安全视角的 24h stale 窗口会让旧 token 永久有效，是自相矛盾） |
| 抗重放 | 无 | body_digest 去重 | **HMAC 签名 + nonce 主键** | **协议**（唯一真正解决"伪造"的） |
| key 面是否拒绝 Cookie | — | 拒绝 | 拒绝 | **忽略，不拒绝**（见 3.3，破坏性太大且收益极小） |
| 寿命上限 | 90 天 | — | **30 天硬顶 + renew≤90** | **30 天硬顶，去掉 renew≤90**（见 2.2） |
| TTL 值 | 建议 6h | 2h | 2h | **2h**，靠 5 分钟级上报覆盖（第 2 节） |
| 表膨胀 | UPDATE | 同 | `active_slot` + 部分唯一索引 | **协议**（MySQL 唯一索引 NULL 互不相等，这是个真坑） |

---

## 2. 必须修补的三处（三视角都没解决的）

### 2.1 非工作组成员会 7.5 天死一次 —— 让所有人上报

`_maybe_report:2072` 现在是 `if not staff: return`（你 11-18 定的）。
协议视角"只有被接受的汇报才续命"的直接后果：

```
非成员永不汇报 → 每 2h 过期 → renew → 90 次 = 180h = 7.5 天
→ renew 耗尽 → 必须重新登录 + 人肉验证码
```

**修补：所有装了插件的人都上报，非成员带 `staff=false`，数据不进考核表。**

现在站点 `rc_site_seed_group_reports` 按"每人最新一条"展示，加一列 `is_staff` 区分即可。
这一步同时解决：非成员死锁、安全视角担心的"空心跳续命"（不再需要心跳）、你的原意字面成立。

### 2.2 `renew_count ≤ 90` 会误杀 cron 改大的用户

```
cron 6h → 每 6h 过期一次 → 每天 renew 4 次 → 90 次撑 22.5 天
但硬顶是 30 天 → 先撞 renew 上限 → 22.5 天就要重登（无缘无故）
```

**修补：删掉 `renew_count` 硬上限，只保留 30 天硬顶 + 速率限制（10/h、3/min）。**
30 天硬顶已经限制了总寿命，速率限制已经防刷，`renew_count` 是多余且会误伤的。

### 2.3 `tier` / `groups` / `on_target` 必须服务端算

安全视角这条最关键，另两个视角都没提：
现在全由插件自报，而 `rc_site_seed_group_standing` 的档位是"取当月出现最多的"。
**只收紧凭据、不收紧它写什么 = 没收紧。** 一把被盗的 key 仍能把自己刷成 `on_target=1`。

**修补：服务端算 `on_target`（覆盖体积 vs 档位）、`groups`（取站点自身工作组数据）；
插件只报实测 `covered_gib` + 各客户端限速快照。**

---

## 3. 最终架构

```
session（7 天，人肉验证码）
   └─ POST /api/v1/plugin/keys  enroll（幂等）
        → token「rcpkey_」+ signing_key（明文只此刻出现一次）
             ├─ 每 5–10 分钟：POST /plugin/seed-group/report（带签名）
             │     └─ 被接受 → expires_at = now + 2h（续命，唯一途径）
             ├─ 过期 → POST /plugin/keys/renew（instance_id + 签名，无需 session）
             │     └─ 重置为 now + 2h，旧 token 留 120s 宽限
             └─ 30 天硬顶到 → 必须重新登录（人肉验证码）
```

### 3.1 请求头

```
X-API-Key:           rcpkey_<43>     必填
X-Plugin-Instance:   <uuid>          必填，硬校验
X-Plugin-Version:    0.3.0           必填，版本闸
X-Plugin-Signature:  v1 ts=<unix> nonce=<b64url> mac=<b64url>   必填
```

签名串：`"COLA-PLUGIN-1\n"+METHOD+"\n"+PATH_WITH_QUERY+"\n"+instance_id+"\n"+ts+"\n"+nonce+"\n"+sha256hex(body)`
`|now-ts| ≤ 300`，`(instance_id, nonce)` 主键冲突 = 重放。

### 3.2 401 / 403 划分

**401 = 凭据不可用（换/续之后可能成）；403 = 凭据没问题，是这台机/这个能力不行，重试无用。**

关键码：`plugin_key_expired`(401，停报>2h) / `plugin_signature_invalid`(401) /
`plugin_replay_detected`(401) / `plugin_key_binding_mismatch`(403，**伪造信号，禁止重试**) /
`plugin_key_revoked`(403) / `plugin_version_unsupported`(426)。

### 3.3 key 面忽略 Cookie / Origin，不拒绝

协议视角要求"有 Cookie/Origin 即 401 mixed_credentials"，沿用机器面铁律。
**我裁决不采纳**，理由：
- 插件 `_curl` 共享 cookie jar，清不干净；线上表现为"key 无效"，是最难查的故障
- 收益极小：key 已是独立凭据，无关 Cookie
- 机器面（`automation.go:69`）严格拒绝是因为那是**用户手填**的 key，语境不同

### 3.4 上报 handler 判定顺序（可直接落）

```go
1  路由白名单            （不查库）
2  必填头
3  sha256(token) 查 key 行
4  banned / revoked
5  expires                （带 server-time，区分真过期 vs 时钟歪）
6  instance 绑定          （不符 → 403，禁止重试）
7  版本闸
8  签名 ts / nonce / mac
9  账号 active
10 权限交集               （key.permissions ∩ 账号当前 permissions）
11 双速率桶               （key_id 维度，不是 IP）
12 幂等检查               （key_id, window_start）
13 业务事务：落库 + 续命  （同一事务）
14 派发前复查 revoked
```

---

## 4. 协议视角发现的关键陷阱（必须写进 ADR）

> **`expires_at` 只能放响应头、每次实时算，绝不能进 body。**

因为幂等回放的是旧的 `response_json`。一旦 TTL 进了 body：
`renew → 报 → 回放到旧 expires_at → 插件以为又要过期 → 再 renew` 死循环
→ 每 5 分钟烧一次续期额度 → 30 天硬顶提前到来 → **集体 401，而日志里每次请求都是 200**。

TTL 一律走响应头：`X-Plugin-Key-Expires-At` / `X-Plugin-Server-Time` / `X-Plugin-Deduplicated`。
这也让 0.2.x 的 body 契约零改动。

### 另一个索引坑

MySQL 唯一索引里 NULL 互不相等 → `UNIQUE(owner_id, instance_id, revoked_at)` **挡不住**同实例的第二把活 key。
必须用 `active_slot`（活跃=1，撤销=NULL）或生成列。

---

## 5. 站点侧改动

1. **迁移**（幂等，生产 23 自动跑）：
   - `0231`：放宽 `rc_site_api_keys.scope` 的 CHECK（`0000_baseline.sql:379` 只允许 3 个值）——**扩张不是收缩，不需要 contract-phase**
   - `0232`：加列 `instance_id`/`plugin_id`/`signing_key_sealed`/`expires_at`/`hard_expires_at`/`last_report_at`/`report_count`/`active_slot`/`revoke_reason`
   - 新表 `rc_site_plugin_reports`（PK `key_id+window_start`）、`rc_site_plugin_nonces`（TTL 600s）
2. **`internal/apikeys/scope.go`**：`NormalizeScopes` 严格枚举必须同步加新 scope（它对未知值直接失败）
3. **enroll**：挂 `pluginLogin` 的 201（已带验证码 + 限流，不用新开口子）
4. **renew / report**：report 走 `gateway.check` 会话面里加第三种认证分支（不能挂机器面，会 100% 403）
5. **feature flag**：`systemconfig` 里 `plugin_key_mode = off|log|warn|enforce`，热生效
6. **版本基线是 17.102.0**（协议视角写的 18.5.0 与仓库不符，按实际改）

## 6. 插件侧改动

1. 新字段 `plugin_key` / `signing_key`，**不复用** `seedgroup_api_key`（那是用户手填的机器面 key，覆盖即丢用户配置）
2. **复用** `_seedgroup_report_token`（`:2138`，站点从未校验过，客户端半成品已在线上）
3. 上报走签名，`_curl` 该调用清 cookie jar
4. 401 `plugin_key_expired` → 自动 renew（退避 0/15/60/300/900s，封顶 30 分钟，**不设重试上限**）
5. **禁止**：key 失效后回落 session 静默上报（等于留后门）
6. key 只读展示 `rcpkey_…f3a9`，**不做成可编辑输入框**
7. 静默迁移：有 `session_token` 就静默 enroll（不要密码、不要验证码）
8. 顺带收益：`password` 现在**明文**进 `_persist()` 落盘（`colacrossseed:1196`），改用 key 后停止落盘

## 7. 实施顺序（不可颠倒）

```
1 迁移（加列 + 放宽 CHECK）
2 站点：enroll / renew / report / 审计，plugin_key_mode=log
3 生产观察 1 小时，确认真实 401 为 0
4 插件 0.2.11 发版（含老站点 404 → legacy 降级，不报错）
5 灰度 20 台 → warn → enforce（阶梯，不跳）
6 回滚：plugin_key_mode=log 一条 SQL；绝不批量删 key（会把用户打回验证码）
```

**铁律：服务端先上线，插件后发版。** MP 插件市场自动更新，客户端发出去回不来。

---

## 8. 待你拍板

1. **非成员是否纳入上报**（决定 2.1）：
   - 纳入 → 不死锁，站点能看到全体保种数据，但面板多一类"非成员上报"
   - 不纳入 → 必须给非成员豁免 TTL（否则装插件即死）
2. **TTL 值**：2h（配 5–10 分钟上报）还是放宽到 6h？我建议 **2h + 上报压到 5 分钟**，语义最贴合你的原话。
3. **30 天硬顶**是否接受：即每 30 天需要人肉验证码登录一次。要更长就把硬顶调大（硬顶是唯一防"无限续期"的东西）。

---

# 9. 最终裁决（2026-11-19 站长拍板）

| 议题 | 裁决 |
|---|---|
| 非工作组成员是否纳入上报 | **纳入**。站点按上报者当时身份决定这一行进不进考核（`is_staff`）。不纳入的话非成员票据永远拿不到续命的业务写入，7.5 天就要重解一次验证码 |
| TTL | **2 小时**，由被接受的汇报续命 |
| 30 天硬顶 | **不做**。权限已收到「只能写自己一行保种数据」，泄露上限就是别人替你报一条数据，影响的是你自己的账号。为它让每个用户每月解一次验证码不划算 |

后果：`renew` 不限次、无硬顶，`signing_key` 长期有效，仅因改密/封号/撤销而失效。

---

# 10. 反作弊（2026-11-19 站长要求「识别作弊用户、虚假汇报」）

先说清楚能做和做不到什么。上报内容是**客户端声明**：覆盖体积、档位、做种时长全由插件自报，
而插件跑在用户自己机器上，站点无法去他磁盘上数一遍。所以不做「证明」，只做两件确实能做的：

## 10.1 能自己算的不采信声明（最关键）

档位到体积的映射是公开常量，达标与否可算：

| 项 | 做法 |
|---|---|
| `target_gib` | 固定档位一律取 `SeedGroupTierGiB`（2T/5T/10T/15T/20T），插件报的值只作参考 |
| `on_target` | 服务端按 `covered >= target × ratio` 自己算；`all` 档 60%，其余 100% |
| 容差 | **固定 0.5 GiB**。写成百分比会在 20T 档放过 102 GiB —— 那是能让作弊穿过去的口子 |
| `is_staff` | 按上报者当时的 `seedgroup.self` 权限判定，不采信 body |

这一条直接消灭了大部分作弊动机：**谎报 on_target 不再有意义**。

## 10.2 标出自相矛盾的上报（只记录、不裁决）

沿用站长既有口径「事实呈现，不裁决」，`rc_site_seed_group_reports.anomalies` 记 code，
面板翻成人话给组长看，处理是人做的。

| code | 判据 | 误报来源 |
|---|---|---|
| `tier_target_mismatch` | 固定档位报了个对不上的 target（偏离 >5%） | 无（诚实的插件只报常量） |
| `on_target_mismatch` | 自报达标但服务端算出来没到 | 无 |
| `zero_seeded` | 覆盖 >50 GiB 但最老那颗只做了 <300 秒 | 刚重建客户端 |
| `no_clients` | 有覆盖体积但没有客户端快照，或全部 `readable=false` | qB 正在重启 |
| `covered_jump` | 体积涨 3 倍以上且增量 >100 GiB，而做种时长没前进 | 换目录、重新校验 |
| `impossible_rate` | 增速 >3 TiB/小时（≈7 Gbps 持续下载） | 一次性搬库 |

上限刻意设得宽松：宁可漏掉慢速刷量，也不要因为用户真的搬了一次几十 TB 的库就打标记。

## 10.3 明确做不到的

「只允许我这一份二进制上报」在密码学上不可达 —— 插件开源、跑在用户自己的机器上。
能做到的是：提高伪造成本（签名）、缩短泄露窗口（2 小时）、异常可审计（anomalies）。
需求不要写成「绝对禁止伪造」，否则第一次被绕就没人再信这套。
