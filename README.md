# 可乐PT辅种 · MoviePilot 插件

把 MoviePilot / qBittorrent / Transmission 里**已经下载完成**的资源，与 [可乐PT](https://www.colapt.com) 对照，
命中就自动把可乐PT 的那颗种子拉回来辅种；反过来也能去你配置的其他 PT 站补种。

> 这不是 IYUU / ReseedPuppy 的替代品：它只服务可乐PT 用户，配置更少，跑在 MoviePilot 里。

> 使用说明（截图 + 常见问题）：<https://www.colapt.com/wiki/moviepilot-crossseed>

## 安装

### 方式一：插件市场（推荐）

MoviePilot → 设置 → 插件市场 → **添加仓库**，填：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
```

刷新插件市场，找到「可乐PT辅种」安装、启用。

### 方式二：一键脚本（直接拷进容器）

在跑 MoviePilot 的宿主机上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash
# 卸载
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash -s uninstall
```

容器升级重建后要重跑一次；想跟随升级请用方式一。

## 它做什么

1. 扫描你配置的 qBittorrent / Transmission（可多个，也可直接用 MoviePilot 已配置的下载器）里**已完成**的种子；
2. 把这些种子的 info_hash 批量交给可乐PT：
   - `via=source_info_hash`：客户端里有同一份文件（来源别的站）→ **拉可乐这颗种子回原下载器辅种**；
   - `via=info_hash`：客户端里已经是可乐这颗种子 → 跳过；
3. 可选方向二：对可乐的种子，用 IYUU 云端匹配（或站点 `/api/pieces-hash`）去其他站找同一份文件，命中就拉回来辅种；
4. 每小时最多添加 N 个（滚动 60 分钟窗口，默认 20），是否跳过校验由你决定（默认校验）。

## 配置

| 字段 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 可乐PT 用户名 / 密码 | 你的可乐PT 账号（登录一次后会保存会话 token，不用反复填） |
| 验证码 | **先打开插件详情页**，页面会显示一张验证码图片，把 4 位数字填到这里，保存即完成登录 |
| 两步验证码 | 账号开了 2FA 时填（可选） |
| 使用 MoviePilot 已配置下载器 | 勾选后使用 MP 里的 qB/TR |
| 下载器 | 每行一个：`地址,用户名,密码`；Transmission 写 `tr,地址,用户名,密码`（qB 可写 `qb,地址,用户名,密码`） |
| 其他站 | 每行一个站点，位置写法和键值写法都支持（见下） |
| 直接导入 IYUU 站点配置 | 把 IYUU 导出的站点 JSON 整个粘贴进来，自动取「启用且有 passkey」的站点 |
| IYUU Token | 填了就用 IYUU 云端做辅种匹配（覆盖没有 pieces-hash 的站） |
| 辅种方向 | 双向 / 只补可乐 / 只补其他站 |
| 每小时最多添加 | 默认 20，可调（1–200） |
| 跳过校验 | 默认关闭（默认校验）；Transmission 无此选项，等价于总是校验 |
| 辅种标签 | 给补进来的种子打的标签（qB 用标签、TR 用 label）；留空则沿用种子原来的分类 |
| 校验失败降级 | 默认开。校验态下失败最常见的原因是文件不完全一致（多一个 nfo/字幕），开启后自动用「跳过校验」重试一次 |
| 失败下一轮重试 | 默认开。真正失败的种子进队列（最多 40 条），下一轮优先重试，插件页能看到待重试清单 |
| 执行周期 | cron，默认每小时 |

## 插件页与接口

- 插件页显示：上次运行统计（匹配 / 可乐补种 / 其他站补种 / 重复 / 降级成功 / 失败）、待重试清单、连通性检测结果；
- `POST /api/v1/plugin/ColaCrossSeed/run` 手动跑一次（仍守每小时上限）；
- `POST /api/v1/plugin/ColaCrossSeed/test` 检测可乐、下载器、其他站连通性。

## 每个站点单独配置

「其他站」一行一个站点，字段按名字写，不写的位置留空即可：

| 字段 | 作用 |
|---|---|
| `passkey` | 站点密钥（必填），下载和 pieces-hash 认证都用它 |
| `sid` | IYUU 的站点编号，**走 IYUU 匹配必须有**（IYUU 导出的 JSON 里 `"sid": 数字`） |
| `rsskey` | 站点 RSS 密钥，没有 pieces-hash 的站靠它做精确匹配 |
| `uid` | 站点用户 id（少数站下载链接要用） |
| `channel` | 只允许这个站用哪条通道：`pieces` / `rss` / `iyuu` / `auto`（默认） |
| `enabled` | 写 `enabled=0` 临时停掉这个站，不用删行 |

```
观众,https://audiences.me,passkey=<32位>,rsskey=<32位>,channel=rss
麒麟,https://www.hdkyl.in,passkey=<32位>,channel=pieces
朋友,https://pt.keepfrds.com,passkey=<32位>,sid=12
老站,https://old.example.com,passkey=<32位>,enabled=0
```

「站点来源」开关：

- **行文本 + IYUU 导入**（默认）：手写的行优先，同一个域名下没写的 `passkey/sid/rsskey` 会从 IYUU 导入里补齐；
- **只用手写的行**：完全忽略 IYUU 导入，适合只维护少数几个站的用户。

插件页会列出每个站点、它能用哪几条通道（IYUU / RSS / pieces-hash）以及上次补种数。

## 旧写法（仍然兼容）

```
# 只有 passkey：走 pieces-hash / RSS（如果还配了 rsskey）
https://pt.example.com,abcdef1234567890abcdef1234567890

# 名称 + passkey
朋友,https://pt.keepfrds.com,abcdef1234567890abcdef1234567890

# 没有 pieces-hash 的站：第 4 段写 RSS 密钥（观众就是这个写法）
观众,https://audiences.me,<passkey>,<rsskey>

# 要走 IYUU 云端匹配：必须有 sid（IYUU 导出的站点 JSON 里 `"sid": 数字`）
观众,https://audiences.me,passkey=<passkey>,sid=42

# 也可以把几种信息一次写全
麒麟,https://www.hdkyl.in,passkey=<passkey>,sid=7,uid=30695
```

`sid` 只在 IYUU 通道用得上：IYUU 云端按 sid 回结果，插件靠 sid 找到对应站点去下载。
不知道自己的 sid 时先不写，跑一轮后日志会打印「IYUU 返回了插件里没有配置的站点 sid：42」，
照着补 `sid=42` 即可。

## 三条匹配通道

| 通道 | 依赖 | 适用 |
|---|---|---|
| 可乐 `/api/v1/plugin/match` | 可乐PT 账号 | 客户端里的种子在可乐有没有（主通道） |
| 站点 `/api/pieces-hash` | 站点开放 pieces-hash 接口 | 有该接口的 NexusPHP 站 |
| **站点 RSS（`rsskey`）** | 站点 RSS 密钥 | 没有 pieces-hash 的站（如 观众 audiences.me）。NexusPHP 的 `torrentrss.php` 每条 item 带 `guid=info_hash` 和 `enclosure` 直链，按 info_hash 精确匹配，不靠标题猜 |
| IYUU 云端 | IYUU token | 收录在 IYUU 的站，按 info_hash 全站匹配 |

RSS 通道的覆盖范围是「RSS 里能看到的种子」（一般是最近发布的），刚好是辅种最该抢的那批；插件按 10 分钟缓存一次（站点侧限流是 2 分钟一次）。

## 注意

- 会话过期（站点返回 401）时插件会发站内通知：重新打开插件页面取验证码、填一次即可；
- 插件只保存可乐的会话 token，其他站只需要域名 + passkey；
- 可乐PT 接口有限流（列表/match 60 次/分/凭据，下载 120 次/分），插件已按批调用；
- 默认 20 个/小时是为了避免一次辅种太多触发源站风控，可自行调低。

## 安全与隐私

仓库里**不含任何凭据**；账号、passkey、IYUU token、下载器密码全部只存在你自己的 MoviePilot 里。
详见 [SECURITY.md](./SECURITY.md)。

## 许可证

[MIT](./LICENSE)
