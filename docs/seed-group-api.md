# 可乐保种组 · 站点接口契约（v1，预留）

> 面向可乐PT 站点后端。插件端已按本契约实现完毕（`plugins.v2/colacrossseed` 的「可乐保种组」区块），
> 站点侧一上线，插件把「保种组 API Token」填进设置就能激活，**不需要再发版**。
> 站点没上线时接口返回 404/501，插件整块锁定、只在插件页显示未激活原因，不影响辅种主流程。

---

## 0. 它解决什么问题

辅种（现有功能）是「我本地已有文件 → 去站点找同一颗种子」；保种组相反：

> 站点给授权成员开放一个资源池，成员按档位（2T / 5T / 10T / 20T+）把池子搬进自己的下载器长期做种，
> **体积只能超、不能少**。

因此站点侧只需要提供两件事：**这个账号有资格吗、额度多大**（profile），
以及**池子里有什么**（torrents）。其余（挑哪些、怎么下、加进哪个下载器、缺口怎么报）都在插件里。

---

## 1. 鉴权

| 项 | 约定 |
|---|---|
| 主凭据 | 请求头 `X-Seed-Group-Token: <token>`，由站点在「账号 → 保种组」页签发（可轮换、可吊销） |
| 备用凭据 | 插件已登录时会**同时**带 `Cookie: reelcore_session=...`；站点可任选其一作为身份来源 |
| 作用域 | token 绑定单个 uid；只能读自己档位内的资源，不能跨账号 |
| 失败语义 | `401` = 未携带/无效 token；`403` = 有 token 但账号不在保种组 / 档位未开通 |

> 为什么不用 session 就够：插件可能在「没登录」的状态下只靠 token 跑保种组；
> 反过来，token 让站点可以在不改密码的情况下单独吊销保种权限。

---

## 2. `GET /api/v1/plugin/seed-group/profile`

**账号资格 + 档位 + 配额**。插件每轮同步前先调它，`enabled=false` 就整块不动作。

### 响应 200

```json
{
  "api_version": "1",
  "enabled": true,
  "tier": "5T",
  "tiers": ["2T", "5T", "10T", "20T+"],
  "quota_gib": 5120,
  "used_gib": 1200.5,
  "member": { "uid": 10086, "username": "yg" },
  "reason": ""
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `api_version` | string | 是 | 契约版本，当前 `"1"`；站点加字段不改主版本 |
| `enabled` | bool | 是 | 该账号是否已开通保种组 |
| `tier` | string | 是 | 当前档位，必须属于 `2T` / `5T` / `10T` / `20T+` 之一；插件不认识的档位视为未激活 |
| `tiers` | string[] | 否 | 该账号可选的档位（插件目前只展示四档，字段留作后续放开） |
| `quota_gib` | number | 否 | 站点侧额度（GiB）。**插件取 `max(本地档位下限, quota_gib)`**，站点额度优先 |
| `used_gib` | number | 否 | 站点统计的已保种体积，仅展示 |
| `member` | object | 否 | 展示用 |
| `reason` | string | 否 | `enabled=false` 时的原因，插件原样显示在面板上（如「档位未开通」） |

### 档位下限（插件侧硬编码，站点可覆盖）

| 档位 | 体积下限 |
|---|---|
| `2T` | 2048 GiB |
| `5T` | 5120 GiB |
| `10T` | 10240 GiB |
| `20T+` | 20480 GiB |

---

## 3. `GET /api/v1/plugin/seed-group/torrents`

**资源池分页**。插件按游标翻完（每页 200，最多 50 页），再在本地做挑选。

### 请求

| 参数 | 类型 | 说明 |
|---|---|---|
| `limit` | int | 每页条数，插件固定传 `200`（站点可下调，最小 1） |
| `cursor` | string | 上一页返回的 `next_cursor`；首页不传 |

### 响应 200

```json
{
  "api_version": "1",
  "items": [
    {
      "torrent_id": 26124,
      "info_hash": "a22ff8dc0f0c4c1b9a4d5e6f7081920310344c8d",
      "name": "Saving.Grace.S01.2160p.WEB-DL",
      "size": 42949672960,
      "official": true,
      "download_url": "https://www.colapt.com/api/v1/torrents/26124/download?authorization_id=..."
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjIwMH0=",
  "total_gib": 560
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items[].torrent_id` | int | 是 | 站点种子 id；`download_url` 缺失时插件走「签发 authorization → 下载」老路径 |
| `items[].info_hash` | string | 是 | 40 位 hex，用于和客户端已有种子去重（不靠名字） |
| `items[].name` | string | 是 | 展示 |
| `items[].size` | number | 是 | **字节**。插件按 `< 1e6` 视为已经是 GiB 做兼容，但请始终给字节 |
| `items[].official` | bool | 否 | 官方种优先（插件排序第一优先级） |
| `items[].download_url` | string | 否 | 带签名直链；给了就省掉一次授权往返，`GET` 需返回 `application/x-bittorrent` |
| `next_cursor` | string | 否 | 空/缺省表示到底 |
| `total_gib` | number | 否 | 仅展示 |

> 池子**只返回当前账号档位可保的资源**：权限过滤在站点侧做，插件不做二次鉴权。

---

## 4. 错误码约定

| HTTP | 插件行为 |
|---|---|
| `200` | 正常解析 |
| `401` | 面板显示「token 缺失或无效」，本轮不加种 |
| `403` | 面板显示「账号不在保种组 / 档位未开通」 |
| `404` / `405` / `501` | 视为**站点尚未上线**，面板显示「v1 契约已预留」，本轮静默跳过（不报警、不重试风暴） |
| `429` | 本轮放弃、下一轮再试（插件不实现客户端退避，靠调度周期兜底） |
| 其它 | 记录 `HTTP xxx` 到面板，本轮跳过 |

---

## 5. 站点实现清单（给后端）

1. 建表：`rc_site_seed_group_members(uid, tier, quota_gib, token_hash, enabled, expires_at)`；
   token 只存哈希，签发时明文返回一次。
2. `profile`：读成员表 + 统计已保种体积（可由 tracker 侧的做种数据聚合）。
3. `torrents`：按 `tier/quota` 过滤资源池，按 `torrent_id` 稳定排序 + 游标分页；
   池子决定「谁该保」，插件只决定「怎么填满」。
4. 签名直链：复用现有 `authorization` 机制即可，签名有效期建议 ≥ 30 分钟（插件一轮最多拉几百条）。
5. 可选 `POST /api/v1/plugin/seed-group/report`（**插件当前未使用**，留作站点侧统计已覆盖体积；
   实现时插件再加一行上报即可，不影响现有契约）。

---

## 6. 兼容性

- 本契约只用 GET + 两个只读接口，失败不影响辅种：保种组整块是「站点说行才动」的旁路。
- 站点上线后如需改字段：加字段不改主版本；删字段/改语义请升 `api_version` 到 `"2"`，
  插件在 `seed_group_probe()` 里已保留 `api_version` 分支位置。
