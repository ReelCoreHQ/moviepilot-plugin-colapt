# 可乐保种组 · 站点接口对照（v1）

> 这一版**不需要站点新增任何接口**：解锁资格、资源池、取种都走站点已有的 API。
> 原本设想的 `/api/v1/plugin/seed-group/*` 保留为「将来若要做专用池」的位置，插件会优先用它。

---

## 1. 它解决什么问题

辅种是「本地已有文件 → 去站点找同一颗种子」；保种组相反：

> 站点给**工作组**成员开放官方资源池，成员按档位（2T / 5T / 10T / 20T+）把池子搬进下载器长期做种，
> **体积只能超、不能少**。

---

## 2. 解锁资格：只认工作组（本站现有接口）

| 步骤 | 接口 | 用途 |
|---|---|---|
| 1 | `GET /api/v1/me` | 拿 `id` / `username` / `roles`（会话登录后） |
| 2 | `GET /api/v1/users/{id}/profile` | 拿 `groups`（组**标题**数组）与 `display_group` |

判据：

```
staff = 任一 groups[i] 以「工作组」开头
     or display_group 以「工作组」开头
     or (roles 含 admin 且 seedgroup_allow_admin 打开)
```

为什么是这个判据：

- `rc_site_groups.title` 生产上是 `工作组-审核 / 工作组-保种 / 工作组-组长 / 工作组-转载`；
  `工作组-转载` 是自建组（`is_system=0`），所以**不能看 is_system**，也不能看 `vip`。
- **`GET /api/v1/me` 的 `groups` 永远是空数组**（`internal/identity/handlers.go` 里 `Current()` 直接写
  `out.Groups = []string{}`），拿它判资格会永远判失败 —— 必须读 profile。
- 本人查自己的 profile 时 `privileged=true`，`groups` 一定返回，不受「对外展示 → 工作组头衔」开关影响
  （`internal/memberprofiles/profiles.go`：只有 `!privileged` 才会被 `show_groups` 抹成空）。
- 前缀可用插件设置「解锁所需的工作组名称前缀」覆盖（默认 `工作组`）。

### 强制锁定

- 保存配置时若勾了「启用可乐保种组」，插件**立刻**校验一次；不通过就把开关改回关闭并写日志，
  绝不留「打开了但没资格」的状态。
- 每轮任务开始前再校验一次（解锁记录缓存 24 小时）；非工作组直接停，连资源池都不拉。
- 面板上写明原因：`账号不在工作组：组名需以「工作组」开头（当前 VIP），保种组不可解锁`。

---

## 3. 资源池：官方发布清单（两条通道）

### 3.1 首选：机器面清单（需要 API Key）

```
GET /api/v1/automation/official/torrents?page=1&page_size=100
X-API-Key: rcapi_...
```

- 只列 `official=1` 且已过审、已发布的种子；
- 返回 `{items:[{torrent_id, release_name, size_bytes, official:true, published_at, media_id, media_title, category}], page, page_size, total}`；
- **不能带 Cookie / Authorization / Origin**（`internal/apikeys` 的机器面入口会拒），所以插件单独用这把 Key；
- 一页最多 100 条，插件翻页到 `total` 为止（上限 50 页）。

### 3.2 回退：会话面浏览（只要有账号密码就能用）

```
GET /api/v1/media?official=true&view=torrents&page=1&page_size=50&version_limit=20
Cookie: reelcore_session=...
```

- `official=true` 只回官方档；从每个作品的 `versions[]` 里抽 `official=true` 的版本；
- 每条给到 `id`（= torrent_id）、`release_name`、`size_bytes`；
- 分页是**作品**粒度，所以比机器面慢，但不需要 API Key。

### 3.3 取种

`GET /torrents/{id}/authorizations`（签发）→ `GET /torrents/{id}/download?authorization_id=` —— 插件已有这套（`_download_cola`）。

---

## 4. 档位与「只超不少」

| 档位 | 体积下限 |
|---|---|
| `2T` | 2048 GiB |
| `5T` | 5120 GiB |
| `10T` | 10240 GiB |
| `20T+` | 20480 GiB |

`seed_group_plan` 的规则：官方优先、体积降序 → 能一颗盖住剩余空间就挑**最小**那颗（超额最少）→
否则挑放得下的最大颗 → 池子耗尽仍未到档位就如实报缺口（`state.shortfall_gib`）。
缺口按「客户端实测已带『可乐保种组』标签的体积 + 本轮真正 add 成功的体积」算，不用计划值。

---

## 5. 下载器（保种组独立）

| 选项 | 行为 |
|---|---|
| 用 MoviePilot 已配置下载器（默认开） | 走 MP 的 `add_torrent`，种子的落盘、刮削、整理交给 MP 自己的规则 —— 这就是「默认通过 MP 强制刮削」 |
| 保种组自定义下载器 | 每行一台 `地址,用户名,密码`（TR 写 `tr,地址,用户名,密码`），只作用于保种组，不影响辅种那套 |
| 保种加到哪台 | 填下载器名字（`MP-qb` / `QB-1` / `TR-1`…），留空取第一台 |
| 两者都空 | 回落辅种的客户端列表，避免开了功能却跑不动 |

---

## 6. 未来：专用池接口（可选，插件已预留）

若站点要把「保种组资源池」做成独立接口，按下面实现即可，插件会**优先**调用它（`SEED_GROUP_API`）：

```
GET /api/v1/plugin/seed-group/torrents?limit=200&cursor=
→ { api_version:"1", items:[{torrent_id, info_hash, name, size, official, download_url}], next_cursor, total_gib }
```

约定：
- `size` 用**字节**；插件按 `< 1e6` 当 GiB 兼容。
- `401/403` = token/资格问题，`404/405/501` = 未上线（插件静默回退到官方清单）。
- 现在的实现里，`/api/v1/plugin/seed-group/profile` 不再必需 —— 资格已经由 profile 的 `groups` 判断。
