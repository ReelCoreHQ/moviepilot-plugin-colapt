# 可乐PT · MoviePilot 插件（辅种 + 保种组）

这个仓库有**两个互相独立的插件**，可以只装一个，也可以都装：

| 插件 | 作用 | 一句话 |
|---|---|---|
| **可乐PT辅种** `ColaCrossSeed` | 把你**已经下完**的资源拿去可乐PT 对照，命中就拉回来辅种 | 「我有的，站上也有 → 一起做种」 |
| **可乐PT保种组** `ColaSeedGroup` | 站点给工作组一个**官方资源池**，按档位搬进下载器长期做种 | 「站上要的，我搬过来保着」 |

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
- **可乐PT保种组**（`ColaSeedGroup`）— 只有站点工作组能用，见下文

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
| 额外标签 | 逗号分隔，追加到补进来的种子上（例如站点名 `可乐PT`） |
| 继承原种子标签 | 默认开：原种子上已有的标签（站点标签、已整理、订阅…）一并带到补进来的种子 |
| 校验失败降级 | 默认开。校验态下失败最常见的原因是文件不完全一致（多一个 nfo/字幕），开启后自动用「跳过校验」重试一次 |
| 失败下一轮重试 | 默认开。真正失败的种子进队列（最多 40 条），下一轮优先重试，插件页能看到待重试清单 |
| 执行周期 | cron，默认每小时 |
| **可乐保种组** | 见下节；站点开放保种组权限 API 后填 Token 即可启用 |

## 可乐保种组 `ColaSeedGroup`（独立插件，仅工作组可解锁）

辅种是「我已有 → 去站点找同一颗」；保种组反过来：**站点给工作组一个官方资源池，成员按档位把它搬进下载器长期做种**。

> 从 0.2.x 起它是**独立插件**，不再依附于辅种。装它不用先装辅种，反之亦然。

| 项 | 说明 |
|---|---|
| 解锁资格 | **只认站点工作组**：账号密码登录后读本人资料里的工作组头衔（组名以「工作组」开头，即 `工作组-审核 / 工作组-保种 / 工作组-组长 / 工作组-转载`）。不是工作组的账号，保存配置时插件会把开关**强制关掉**，面板写明原因 |
| 管理员 | 默认也放行（可在设置里关） |
| 档位 | **`全站`**（`all`，有多少保多少）/ `2T` / `5T` / `10T` / `15T` / `20T` / **自定义**（填 `100M` `100G` `1T` `1P` 均可，**不允许低于 2048 GiB**，低于会被抬到 2T） |
| 达标线 | 固定档位要求**保满 100%**；**`全站` 档只要到 60% 就算达标**（目标 = 站点所有官方种的总体积，它每天都在涨，要求 100% 等于永远追不上）。站点只按这个布尔值累计保种时长 |
| 体积规则 | **只能超、不能少**：官方优先 → 能一颗盖住就挑最小的那颗 → 否则挑放得下的最大颗 → 池子不够如实报缺口，不虚报达标 |
| 资源池 | 官方发布清单。填了「站点 API Key」就走机器面 `/official/torrents`（100 条/页，最快）；不填就走会话面 `/media?official=true`，账号登录即可 |
| 下载器 | 默认用 MoviePilot 已配置的那台 —— 种子交给 MP 自己的规则**刮削/整理**；也可以单独填 qB/TR（只给保种组用，不影响辅种），再指定加到哪一台。**配不出客户端就本轮跳过，绝不回落到 MP 的下载器列表** —— 免得把种推到没被点名的机器上 |
| 慢链路 | 读客户端种子列表**分页 10 条 / 超时 90 秒**：上行几百 KiB/s 还夹丢包的家宽，全量 1.4 MB 会被截断成空（看起来像 qB 挂了），分页慢传 12 分钟能拉完 700 条，零失败。读不到某一台时**沿用它上次的读数**（按客户端名缓存，最多 24 小时），不让一次超时把覆盖清零、把保种时长的连续区间打断 |
| 动态保种 | **体积达标后**才轮换：删「做种人数最多」的 N 颗、补「做种人数最少」的 N 颗（默认 5，可调 1–50；默认连文件一起删）。没达标就只补满、不轮换 | 
| 下架清理 | 每轮同步后回站点逐个核对 `status/official`：`withdrawn/rejected/deleted`、不再是 `approved`、或官方标记被撤掉的，直接从下载器删掉（默认只删种子、保留文件；可在设置里改成连文件一起删）。站点上已经 404 的也删 |
| 任务 | 独立任务 `可乐保种组`（自己的 cron，默认每小时），在 MoviePilot「任务」里单独可见、可手动跑 |
| 接口 | 见 [docs/seed-group-api.md](./docs/seed-group-api.md)（全部走站点现有 API，不需要后端改动） |

插件页顶部就是「可乐保种组」面板：档位、目标体积、实际覆盖、进度条、上次同步的新增/重复/失败与缺口，
以及**当前账号的工作组资格与解锁时间**。

接口：

- `POST /api/v1/plugin/ColaCrossSeed/seedgroup/unlock` 手动解锁（校验工作组资格，通过后 24 小时有效）；
- `GET  /api/v1/plugin/ColaCrossSeed/seedgroup/identity` 看当前账号的组与是否够资格；
- `GET  /api/v1/plugin/ColaCrossSeed/seedgroup/status` 看档位与缺口；
- `POST /api/v1/plugin/ColaCrossSeed/seedgroup/sync` 立即同步一次。

## 插件页与接口

- 插件页显示：上次运行统计（匹配 / 可乐补种 / 其他站补种 / 重复 / 降级成功 / 失败）、待重试清单、连通性检测结果；
- `POST /api/v1/plugin/ColaCrossSeed/run` 手动跑一次（仍守每小时上限）；
- `POST /api/v1/plugin/ColaCrossSeed/test` 检测可乐、下载器、其他站连通性。

## 一键导入 IYUU 配置（推荐）

不用手写站点行：把 **IYUU 导出的站点 JSON** 整个粘到「直接导入 IYUU 站点配置」里保存即可。

- 插件自动展开：`域名 / passkey / uid / downhash / rsskey / x_api_key / 下载模板 / sid` 一次全带走；
- `disabled=1` 的站自动跳过，已关闭的站（烧包）也不导入；
- 保存后插件页会显示 `IYUU 导入：N 个启用站点（带 uid x / downhash y / Cookie z）`——这就是「生效了没」的凭据；
- 「站点来源」三档：**只用 IYUU 导入**（不写行）／**IYUU + 手写行补充**（默认，行优先）／只用手写行；
- **本地文件导入**：把导出的 JSON 放到宿主的 MP 配置目录（容器内 `/config/cola-sites.json`），
  在设置里填「IYUU 配置文件路径」，保存即导入；文件为准，粘进文本框的内容会被覆盖；
- 也可以用接口推送（脚本/定时同步 IYUU 配置）：

```bash
curl -X POST "http://<MP地址>/api/v1/plugin/ColaCrossSeed/sites/import" \
  -H "Authorization: Bearer <MP API Token>" -H "Content-Type: application/json" \
  -d "{\"json\": $(cat cn_sites.json)}"
```

返回 `{ok, total, enabled, usable, skipped}`：`total` 是文件里的条数，`enabled` 是未禁用的，
`usable` 是真正带 passkey 能被插件使用的，`skipped` 是被跳过的域名与原因。

> 为什么以前「粘了也不全生效」：IYUU 的凭据不只有 passkey。下载模板里的 `{uid}`、家园的 `downhash`、
> `cookie_required` 站的 Cookie 都分散在 `options` 与顶层字段里，少一个就会出现「导入成功但辅种全失败」。
> 0.10.26 起一次全带走。

## 保种限速接管（只作用于保种组）

| 设置 | 行为 |
|---|---|
| 「保种下载/上传限速」填了数字（KiB/s） | **强行改客户端**：qB 用 `app/setPreferences` 的 `dl_limit/up_limit`（字节/秒），TR 用 `session-set` 的 `speed-limit-*` + `*_enabled=true`；每次任务开始都会重新下发一次，所以有人在客户端里手改限速，下一轮会被改回来 |
| 留空或 0 | **完全不碰**客户端的原有设置（这一项不接管） |
| 只填一边 | 只接管那一边，另一边保持客户端原值 |

### 限速情况上报（强制，界面不提供开关）

**只统计开了保种组的用户，且强制上报**：设置页不显示上报地址与 Token，也没有关闭开关。


开了保种组的用户，每轮同步结束会把**每台客户端此刻的限速**（qB 读 `preferences.dl_limit/up_limit`，
TR 读 `session-get` 的 `speed-limit-*`）连同档位、覆盖体积、缺口、插件是否接管限速，一起
`POST` 到 **`https://www.colapt.com/api/v1/plugin/seed-group/report`**。

**认证用站点签发的插件凭据，不是登录会话**（0.2.12 起）：

- 插件登录成功后，用会话换一把**两小时票据** `rcapi_…` + 一把**长寿命签名密钥**；
- 每个上报请求带 `X-API-Key` + `X-Plugin-Instance` + `X-Plugin-Signature`
  （HMAC-SHA256，绑定 method/路径/body/时间窗/nonce），伪造或别的插件拿不到有效签名；
- 票据过期时用签名密钥**自动换新**，不需要重新登录、不需要图形验证码 ——
  所以无人值守的 NAS 不会每两小时掉线；
- 凭据只存在插件数据里，设置页不显示、也不可编辑（可编辑的输入框只会让人把自己的另一把票据粘进来）。

其他约定：

- **所有人都会上报**（不只是工作组成员）：站点按上报者当时的身份决定这一行进不进考核，
  非成员的数据只留档。这样非成员的票据才有续命所需的业务写入；
- 站点**自己算** `target_gib` 与 `on_target`，插件报的这两个值只作参考 ——
  所以谎报「已达标」不会生效；
- 上报失败只记日志与面板，不影响保种。

`_MPClient`（走 MoviePilot 已配置下载器）目前不支持反向设置，会在日志里记「失败 1 台」；要用限速接管请把 qB/TR 写成自定义下载器行。

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
| 可乐 `/api/v1/plugin/match` | 可乐PT 账号 | 客户端里的种子在可乐有没有（主通道，按 `info_hash` / `source_info_hash`） |
| `info.source` 归一化 | 同上（qB 4.5+ 能导出 `.torrent`） | 可乐发布时会把 `info.source` 盖成自己的章，同一份文件在不同站 info_hash 必然不同；把 source 换掉再算哈希去反查 |
| **文件清单指纹 `content_key`** | 站点接口 | 「相对路径 + 字节数」的 SHA1，与谁重新打包无关；不需要导出种子，Transmission / 老 qB 也能用 |
| 站点 `/api/pieces-hash` | 站点开放 pieces-hash 接口 | 有该接口的 NexusPHP 站 |
| **站点 RSS（`rsskey`）** | 站点 RSS 密钥 | 没有 pieces-hash 的站（如 观众 audiences.me）。NexusPHP 的 `torrentrss.php` 每条 item 带 `guid=info_hash` 和 `enclosure` 直链，按 info_hash 精确匹配，不靠标题猜 |
| IYUU 云端 | IYUU token | 收录在 IYUU 的站，按 info_hash 全站匹配 |

RSS 通道的覆盖范围是「RSS 里能看到的种子」（一般是最近发布的），刚好是辅种最该抢的那批。插件按 10 分钟缓存一次；并且**强制**两次拉取至少隔 120 秒（观众 audiences.me、彩虹岛 ptchdbits.co 站点明确要求 ≥2 分钟），间隔按站持久化打点，重启、手动点「测试」都守得住；站点行可用 `rss_interval=秒` 调大，不能调小。

RSS 命中的是**同一 info_hash**，qB 不允许同 hash 双开，所以这条通道不加种，而是给客户端里已有的那颗种子补上该站的 announce —— 文件一个字节都不用多，站方照样记做种。

## 注意

- 会话过期（站点返回 401）时插件会发站内通知：重新打开插件页面取验证码、填一次即可；
- 插件只保存可乐的会话 token，其他站只需要域名 + passkey；
- 可乐PT 接口有限流（列表/match 60 次/分/凭据，下载 120 次/分），插件已按批调用；
- 默认 20 个/小时是为了避免一次辅种太多触发源站风控，可自行调低。

## 安全与隐私

仓库里**不含任何凭据**；账号、passkey、IYUU token、下载器密码全部只存在你自己的 MoviePilot 里。
详见 [SECURITY.md](./SECURITY.md)。

## 开发与发布流程

`main` 开了分支保护：**功能测试全绿 + 必须走 PR** 才能合入，直接 push 会被拒。

```bash
# 44 个离线功能测试：不联网、不依赖 MoviePilot 运行时（app.* / bencode 缺失时自动打桩）
python -m unittest discover -s tests -v
```

CI 见 `.github/workflows/ci.yml`，job 名 `functional-tests`（就是分支保护要求的那项 check）：
语法与清单自检 → 功能测试。发版流程：改代码 → 推分支 → 开 PR → 等 `functional-tests` 绿 → 合并到 `main`。

## 许可证

[MIT](./LICENSE)
