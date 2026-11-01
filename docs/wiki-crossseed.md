# MoviePilot 辅种插件：可乐PT 自动辅种

> **一句话**：把你 NAS 上已经在做种的资源，自动去可乐PT 找同款并补种；反过来也支持——可乐PT 的种子，去其他 PT 站补种。

插件开源、MIT：`https://github.com/ReelCoreHQ/moviepilot-plugin-colapt`（当前版本 **v0.10.5**）

## ① 它能做什么

- **正向**：扫描 qBittorrent / Transmission 里已完成的种子 → 与可乐PT 对照 → 命中就拉可乐的种子加进客户端，直接吃你**已经下好的文件**做种，不重复下载。
- **反向**：你客户端里已有可乐PT 的种子 → 去其他 PT 站找同 `info_hash` 或同 pieces 的种子补种（提高其他站分享率）。
- 每小时自动跑一次，加进去的种子统一打标签 **`可乐辅种`**，方便在 qB 里一眼筛选、批量管理。
- 内置容错：重复种子识别、校验失败自动降级重试、失败队列下一轮优先重试、文件缺失的种子自动清理。

## ② 前提

1. **MoviePilot v3**（容器版或本机版都行）。
2. 至少一台下载器：qBittorrent 或 Transmission，且 MP 能连上。
3. 可乐PT 账号（插件用你的站内账号登录换会话）。

> 免费/优惠不影响辅种：辅种只是把已有文件挂到另一个站的种子上，不产生额外下载流量。

## ③ 安装

### 方式 A：添加插件仓库（推荐）

MP → **设置 → 插件市场 → 添加仓库**，填：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
```

保存后在插件市场里搜「**可乐PT辅种**」→ 安装 → 启用。

### 方式 B：本地仓库（不想连 GitHub 时用）

1. 把仓库里的 `package.v2.json` 和 `plugins.v2/colacrossseed/` 整个目录拷到 NAS 上，例如 `/config/mp-repo/`；
2. MP 的 `app.env` 里加一行 `PLUGIN_LOCAL_REPO_PATHS='/config/mp-repo'`；
3. 重启容器，插件市场里就会出现「可乐PT辅种」。

## ④ 必填配置

| 项 | 说明 |
|---|---|
| 可乐PT 账号 / 密码 | 保存时自动登录换 session；带验证码时把验证码一并填上再保存 |
| 下载器 | 可直接复用 MP 已配置的下载器；也可以自己写 `http://地址:端口,用户名,密码` |
| 其他站（可选） | 想「反向补种」时才需要，见下一节 |

## ⑤ 其他站怎么配（一行一个站）

> 只做「往可乐PT 补种」的话，这一节可以整段跳过。

```
观众,https://audiences.me,passkey=<32位>,rsskey=<32位>,sid=68,channel=rss
家园,https://hdhome.org,passkey=<32位>,uid=<uid>,downhash=<签名串>,sid=7,channel=iyuu,download_page=download.php?id={}&uid={uid}&hash={hash}
听听歌,https://totheglory.im,passkey=<32位>,sid=14,channel=iyuu,download_page=dl/{}/{passkey}
春天,https://springsunday.net,passkey=<32位>,sid=23,channel=iyuu
```

字段含义：

| 字段 | 作用 |
|---|---|
| `passkey` | 站点密钥，下载与鉴权用（必填） |
| `sid` | IYUU 的站点编号，**走 IYUU 匹配必须有**（IYUU 导出的 JSON 里 `"sid": 数字`） |
| `rsskey` | 站点 RSS 密钥；没有 pieces-hash 的站靠它精确匹配 |
| `uid` / `downhash` | 少数站下载要用的用户 id 与签名串（如家园） |
| `download_page` | 该站的下载地址模板，支持 `{id}` `{}` `{passkey}` `{uid}` `{hash}` |
| `channel` | 只允许这个站用哪条通道：`pieces` / `rss` / `iyuu` / `auto`（默认） |
| `cookie` | 站点登录 Cookie，仅访问该站时携带（天空、家园、听听歌、春天这类需要登录态） |
| `proxy` | 站点级代理，例如 `socks5h://192.168.31.254:10800`；**只作用于插件的站点 HTTP 请求，不影响 tracker** |
| `rss_url` | 站点给你的个人 RSS 链接（带签名/有效期），有它就优先用它 |
| `enabled` | 写 `enabled=0` 临时停用这个站 |

**通道怎么选**：看站点有没有 `pieces-hash` 接口——有就 `pieces`（最准，按文件 pieces 精确匹配）；没有就用 `rss`（按 `info_hash` 精确匹配最近发布）；两者都没有就只能 `iyuu`（靠 IYUU 云端索引）。

## ⑥ IYUU 匹配（可选但强烈推荐）

反向补种最省事的方式是 IYUU：

1. 在 [IYUU](https://iyuu.cn) 拿一个 **token**，填进插件；
2. 把 IYUU 导出的**站点 JSON** 粘进「IYUU 导入」；
3. 插件会自动把客户端里的 `info_hash` 批量送 IYUU 云端比对，拿回「站点 + 站内种子 id」，再用该站自己的下载模板取种。

> 实测：一份 200 多个 hash 的库，云端一次能命中 90+ 条可辅种。

## ⑦ 运行规则（重要）

| 规则 | 默认值 | 说明 |
|---|---|---|
| 运行频率 | 每小时整点 | 可在配置里改 cron |
| 全局上限 | **不限** | `max_per_hour=0` |
| 单站上限 | **20 颗 / 轮** | `max_per_site`，防止一次把某个站刷太多 |
| 校验 | **跳过校验** | 跨种文件本来就是完整的，跳过校验快很多；失败会再走一次带校验的重试 |
| 标签 | **`可乐辅种`** | 强制打上，你在 qB 里搜这个标签就能看到插件加的所有种子 |

## ⑧ 支持的站点（v0.10.5 实测）

- **pieces-hash（10）**：织梦、财神、麒麟、蟹黄堡、垃圾堆、红豆饭、优堡、NovaHD、熊猫、星陨阁
- **RSS（14）**：朋友、学校、彩虹岛、时光、皇后、幼儿园、海胆、观众、憨憨、青蛙、蝶粉、天空、猫站、春天
- **IYUU 专用（3）**：家园、听听歌、春天（配合各站下载模板）
- **专用通道**：馒头（`genDlToken` 换签名地址 → CDN 下载）

## ⑨ 常见问题

**Q1：装好、启用、也保存了，但一颗都没补进去？**
先看插件页的「连通性检测」：可乐PT 是否已登录、下载器是否连上。再看日志里有没有「其他站 27 个」。第一次跑只处理客户端里**已完成**的种子，暂停/未完成的会跳过。

**Q2：家园、听听歌、春天 为什么一定要填 cookie？**
这三家的下载接口要登录态，光有 passkey 不够（家园还会直接回 `Old Passkey no Support`）。把浏览器里的 Cookie 整段贴进 `cookie=` 即可，插件只会用它访问这个站。

**Q3：馒头为什么老失败？**
馒头的下载是多跳的：`genDlToken` 换签名地址 → 该地址再 302 到 CDN → 才拿到种子。插件已按这个链路处理（浏览器 UA + `Accept: application/x-bittorrent` + 跟随重定向 + 失败换 token 重试）。如果你的馒头仍报 302/0 字节，多半是网络被中间设备改写，给这个站配 `proxy=` 再试。

**Q4：我的 passkey / Cookie 会被上传吗？**
不会。插件只在你本机 MP 里保存这些凭据，只用于访问对应站点。它开源（MIT），代码可直接审。

**Q5：只想要「往可乐PT 补种」，不想去别的站？**
把「补种方向」设成「只补可乐PT」，其他站配置留空即可。

**Q6：和站内那个「IYUU自动辅种」插件有什么区别？**
那个是 IYUU 官方的通用插件；本插件是**为可乐PT 定制的**：直接对接可乐PT 的两步下载授权、支持各站自定义下载模板、内置缺失文件自愈、强制标签、按站限流，并且对「只有 IPv6 能通 / 被 Cloudflare 挡」的网络做了 requests→curl 自动回落。

**Q7：会不会把我的做种搞乱？**
不会动你已有的种子，只**新增**；加进去的种子都带 `可乐辅种` 标签，随时可以按标签批量删掉（只删种子，不删文件）。

## ⑩ 更新与反馈

- 仓库：`https://github.com/ReelCoreHQ/moviepilot-plugin-colapt`
- 版本历史：仓库 `package.v2.json` 的 `history` 字段，或插件页显示的版本号
- 有问题请发 issue（附上插件页截图 + MP 日志里的 `colacrossseed` 片段）

> 本站只提供插件与文档，不代替你操作客户端；辅种产生的流量、分享率以各站规则为准。
