# 可乐PT · MoviePilot 辅种插件

这个仓库只有一个插件：可乐PT 辅种的 `ColaCrossSeed`。

> 保种组是**另一个仓库**：[moviepilot-plugin-colaseedgroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup)（同一份源码也提供独立 Docker 版，端口 3006）

| 插件 | 作用 | 一句话 |
|---|---|---|
| **可乐PT辅种** `ColaCrossSeed` | 把你**已经下完**的资源拿去可乐PT 对照，命中就拉回来辅种 | 「我有的，站上也有 → 一起做种」 |

> 这不是 IYUU / ReseedPuppy 的替代品：它只服务可乐PT 用户，配置更少，跑在 MoviePilot 里。

> 使用说明（截图 + 常见问题 + 匹配原理）：<https://www.colapt.com/wiki/moviepilot-crossseed>

## 安装

### 方式一：插件市场（推荐）

MoviePilot → 设置 → 插件市场 → **添加仓库**，填：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
```

刷新插件市场，会看到两个插件，按需安装：

- **可乐PT辅种**（`ColaCrossSeed`）

### 方式二：一键脚本（直接拷进容器）

在跑 MoviePilot 的宿主机上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash
# 卸载
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash -s uninstall
```

容器升级重建后要重跑一次；想跟随升级请用方式一。

## 可乐PT辅种 `ColaCrossSeed` 做什么

1. 扫描你配置的 qBittorrent / Transmission（可多个，也可直接用 MoviePilot 已配置的下载器）里**已完成**的种子；
2. 把这些种子的 info_hash 批量交给可乐PT：
   - `via=source_info_hash`：客户端里有同一份文件（来源别的站）→ **拉可乐这颗种子回原下载器辅种**；
   - `via=info_hash`：客户端里已经是可乐这颗种子 → 跳过；
3. 可选方向二：对可乐的种子，用 IYUU 云端匹配（或站点 `/api/pieces-hash`）去其他站找同一份文件，命中就拉回来辅种；
4. 默认每日 00:00 自动跑一次（cron 可改）；每小时最多添加 N 个（滚动 60 分钟窗口，默认不限），是否跳过校验由你决定（默认校验）。

## 配置

| 字段 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 立即运行一次 | 勾上后保存，3 秒后立刻跑一轮辅种；跑完自动关掉，不会每次保存都触发。**无视启用总开关**（和官方 IYUU 插件一致，总开关关着也能手动跑一轮），但需已填 API Key，没 Key 时只提示不执行 |
| MP 站点管理里的可乐站（推荐） | 在 MP「站点管理」里给可乐站填好「令牌（API Key）」，然后在这个下拉里选中它即可——插件只存站点 id，每次保存自动读取该站的 Key；在站点管理里换 Key 后插件下次重载会自动跟上，不用回插件设置改 |
| API Key（手填兜底） | 没在站点管理配过时用这个：到站点「账号 → Key → API Key」（<https://www.colapt.com/account/tracker?tab=api>）新建一把 Key，范围勾**读取**和**下载**，粘到这里。**两者都填时以站点管理的为准**；站点里没读到 Key 才退回手填。插件只把它放在 `X-API-Key` 请求头里，不需要账号密码、验证码 |
| 使用 MoviePilot 已配置下载器 | 勾选后使用 MP 里的 qB/TR |
| 辅种目标：MP 已配置下载器（可多选） | 从 MP「设置 → 下载器」里的实例多选，选中即**只往这几台加种**；只存实例名，MP 里改名/增删后重新选一下即可。名字支持模糊匹配（`tr` 能命中 `MP-tr辅种`） |
| 自定义下载器名（手填） | 可补自定义下载器的名字（例如下面 `下载器` 行里配的非 MP 机器），与上面多选取并集；留空即可。**两个目标都留空=自动**：每台下载器各自匹配自己的种子、各自加回自己，不用配置。指定了目标时，任何一个解析不到或有重名歧义，本轮都不加种 |
| 下载器 | 每行一个：`地址,用户名,密码`；Transmission 写 `tr,地址,用户名,密码`（qB 可写 `qb,地址,用户名,密码`） |
| MP 已配置站点（可多选） | 从 MP「站点管理」里多选要补的站（显示中文站名，只存 id）。passkey/Cookie/代理每次现读——passkey 优先取站点适配器配置，退回站点 RSS 地址里的 `?passkey=`；站点管理里改了自动跟上 |
| IYUU Token | **可选**：不填也能补可乐、也能用 pieces-hash/RSS 补其他站；填了额外走 IYUU 云端（覆盖没有 pieces-hash 的站）。和官方 `iyuuautoseed` 一样：站点与下载模板由 IYUU 返回，MP 站点下拉只决定补哪些站、用哪套凭据 |
| 辅种方向 | 双向 / 只补可乐 / 只补其他站 |
| 每小时上限 | 默认 0 = 不限；按滚动 60 分钟窗口统计成功添加数。要限流可填正整数 |
| 单站每轮上限 | 默认 20，防止单个站点一轮被刷太快 |
| 跳过校验 | 默认关闭（默认校验）；Transmission 无此选项，等价于总是校验 |
| 辅种标签 | 给补进来的种子打的标签（qB 用标签、TR 用 label）；留空则沿用种子原来的分类 |
| 额外标签 | 逗号分隔，追加到补进来的种子上（例如站点名 `可乐PT`） |
| 继承原种子标签 | 默认开：原种子上已有的标签（站点标签、已整理、订阅…）一并带到补进来的种子 |
| 校验失败降级 | 默认开。校验态下失败最常见的原因是文件不完全一致（多一个 nfo/字幕），开启后自动用「跳过校验」重试一次 |
| 失败下一轮重试 | 默认开。真正失败的种子进队列（最多 40 条），下一轮优先重试，插件页能看到待重试清单 |
| 执行周期 | cron（5 段），默认 `0 0 * * *` = 每日 00:00；写错回落到每日 0 点 |

## 插件页与接口

- 插件页从上到下：四张统计卡片（上次运行 / 本轮补种 / 跳过已有 / 失败待重试）→ 运行状态 → 连通性检测（项目/详情两列，可折叠）→ 站点清单（可折叠）→ 待重试；旧版本遗留的检测报告会自动隐去；
- `POST /api/v1/plugin/ColaCrossSeed/run` 手动跑一次辅种（仍守每小时上限）——注意这**不会**刷新连通性检测；
- `POST /api/v1/plugin/ColaCrossSeed/test` 检测可乐、下载器、其他站连通性。每次保存配置也会自动检测一次（有 API Key 即可，不受总开关限制）。
- 升级后老版本遗留的旧检测报告会自动隐去（带格式版本校验），不会再显示「请粘贴 IYUU 站点 JSON」等废弃文案；保存一次配置即生成干净的新报告。

## 站点来源：MoviePilot 站点管理

辅种目标站**只从 MP「站点管理」读取**：在设置③的多选下拉里勾选要补的站即可，
插件只保存站点 id，其余信息每次现读。不再需要粘贴 IYUU 站点 JSON、配置文件路径或手写站点行。

- **passkey**：优先取站点适配器配置，退回站点 RSS 地址里的 `?passkey=...`；只影响 pieces-hash / RSS 这两条本地通道；
- **Cookie / 代理 / apikey**：从 MP 站点对象现读，站点管理里改了自动跟上；
- **IYUU 命中**：IYUU 云端返回的 `sid` 自带站点地址与下载模板，插件把它反查成 MP 站点，再用该站的 Cookie/apikey 下种；
- 已关闭的站会自动跳过；没有 passkey 的站仍可走 IYUU（有 Cookie/apikey 即可），只是不走 pieces-hash / RSS。

> 站点管理里要确保目标站可登录（Cookie/apikey 有效）：家园这类站就靠站点 Cookie 兜底，不需要在插件里手写 sid、uid 或 downhash。

## 三条匹配通道

| 通道 | 依赖 | 适用 |
|---|---|---|
| 可乐哈希反查（开放 API `POST /api/v1/torrents/hashes`） | 可乐PT API Key（读取） | 客户端里的种子在可乐有没有（主通道，按 `info_hash` / `source_info_hash`） |
| `info.source` 归一化 | 同上（qB 4.5+ 能导出 `.torrent`） | 可乐发布时会把 `info.source` 盖成自己的章，同一份文件在不同站 info_hash 必然不同；把 source 换掉再算哈希去反查 |
| **文件清单指纹 `content_key`** | 站点接口 | 「相对路径 + 字节数」的 SHA1，与谁重新打包无关；不需要导出种子，Transmission / 老 qB 也能用 |
| 站点 `/api/pieces-hash` | 站点开放 pieces-hash 接口 | 有该接口的 NexusPHP 站 |
| **站点 RSS（`rsskey`）** | 站点 RSS 密钥 | 没有 pieces-hash 的站（如 观众 audiences.me）。NexusPHP 的 `torrentrss.php` 每条 item 带 `guid=info_hash` 和 `enclosure` 直链，按 info_hash 精确匹配，不靠标题猜 |
| IYUU 云端 | IYUU token | 收录在 IYUU 的站，按 info_hash 全站匹配 |

RSS 通道的覆盖范围是「RSS 里能看到的种子」（一般是最近发布的），刚好是辅种最该抢的那批。插件按 10 分钟缓存一次；并且**强制**两次拉取至少隔 120 秒（观众 audiences.me、彩虹岛 ptchdbits.co 站点明确要求 ≥2 分钟），间隔按站持久化打点，重启、手动点「测试」都守得住。

RSS 命中的是**同一 info_hash**，qB 不允许同 hash 双开，所以这条通道不加种，而是给客户端里已有的那颗种子补上该站的 announce —— 文件一个字节都不用多，站方照样记做种。

## 注意

- 插件只走可乐PT 的**开放 API**（接口说明见 <https://www.colapt.com/api-docs>）：哈希反查 `matchTorrentHashes`、签下载地址 `createDownloadLink`、连通检测 `personalStats`；
- API Key 无效 / 被撤销（站点答 401）或缺范围（403 `api_key_scope_denied`）时插件发一条通知：到站点重建一把勾「读取」「下载」的 Key 填进来即可；
- 插件只保存可乐的 API Key 与 MP 站点 id，其他站的凭据从站点管理现读、不落盘；
- 可乐PT 接口有限流（每把 Key 读 60 次/分、下载 120 次/分；哈希反查一次最多 500 条、一次只算一次读），插件已按批调用；
- 单站每轮上限默认 20，是为了避免一次辅种太多触发源站风控；每小时总上限默认不限，需要更保守可自行调低。

## 安全与隐私

仓库里**不含任何凭据**；账号、passkey、IYUU token、下载器密码全部只存在你自己的 MoviePilot 里。
详见 [SECURITY.md](./SECURITY.md)。

## 开发与发布流程

`main` 开了分支保护：**功能测试全绿 + 必须走 PR** 才能合入，直接 push 会被拒。

```bash
# 离线功能测试：不联网、不依赖 MoviePilot 运行时（app.* / bencode 缺失时自动打桩）
python -m unittest discover -s tests -v
```

CI 见 `.github/workflows/ci.yml`，job 名 `functional-tests`（就是分支保护要求的那项 check）：
语法与清单自检 → 功能测试。发版流程：改代码 → 推分支 → 开 PR → 等 `functional-tests` 绿 → 合并到 `main`。

## 许可证

[MIT](./LICENSE)
