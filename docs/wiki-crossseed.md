# MoviePilot 辅种插件：可乐PT 自动辅种

> **一句话**：把 NAS 上已经在做种的资源，自动去可乐PT 找同款并补种；反过来也支持——可乐PT 的种子，去其他 PT 站补种。
>
> 开源、MIT：`https://github.com/ReelCoreHQ/moviepilot-plugin-colapt` ｜ 当前版本 **v0.10.15**

## ① 安装

### 方式一：插件市场（推荐）

MoviePilot → 设置 → 插件市场 → **添加仓库**，填：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
```

刷新插件市场，找到「可乐PT辅种」→ 安装 → 启用。

### 方式二：一键脚本（直接拷进容器）

在跑 MoviePilot 的宿主机上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash
# 卸载
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash -s uninstall
```

容器升级重建后要重跑一次；想跟随升级请用方式一。

## ② 它做什么

1. 扫描你配置的 qBittorrent / Transmission（可多个；也可以直接用 MoviePilot「设置 → 下载器」里配置的**每一个**实例）里**已完成**的种子；
2. 把这些种子的 info_hash 批量交给可乐PT：
   - 客户端里有同一份文件（来源别的站）→ **拉可乐这颗种子回原下载器辅种**；
   - 客户端里已经是可乐这颗种子 → 跳过；
3. （可选）反向：对可乐的种子，用 IYUU 云端或站点接口去其他站找同一份文件，命中就拉回来补种；
4. 每小时最多添加 N 个（滚动 60 分钟窗口，默认 20），是否跳过校验由你决定。

## ③ 匹配通道：为什么「内容一致」就能补上

可乐发布种子时会把 `info.source` 盖成自己的章（`[https://www.colapt.com] ColaPT`）。同一份文件在别的站是另一个 source，于是**两边 info_hash 必然不同**——只按哈希反查会漏掉一大半。插件为此叠了四条通道：

| 通道 | 依赖 | 说明 |
|---|---|---|
| 可乐 `/api/v1/plugin/match` | 可乐PT 账号 | 主通道，按 `info_hash` / `source_info_hash` 反查 |
| `info.source` 归一化 | 同上（qB 4.5+） | 用客户端导出的 `.torrent` 把 `source` 换成可乐的章再算一遍哈希去反查；命中即真命中（其余字段逐字节相同） |
| **文件清单指纹 `content_key`** | 站点接口 | 「相对路径 + 字节数」的 SHA1，与谁重新打包无关；**不需要导出种子**，Transmission / 老 qB 也能用 |
| 站点 `/api/pieces-hash` | 站点开放该接口 | 有接口的 NexusPHP 站 |
| 站点 RSS | 站点 RSS 密钥 | 没有 pieces-hash 的站（如 观众 audiences.me）：`torrentrss.php` 的 `guid` 就是 info_hash，按 hash 精确匹配 |
| IYUU 云端 | IYUU token | 收录在 IYUU 的站，按 hash 全站匹配 |

两个容易被误会的点：

- **RSS 命中不加种**：RSS 命中的是**同一 info_hash**，qB 不允许同 hash 双开，所以这条通道是给客户端里已有的那颗种子**补上该站的 announce**——文件一个字节都不用多，站方照样记做种；
- **内容键只负责找候选**：指纹/归一化命中后，插件仍按尺寸与种子内文件清单核对，再决定是否加种。

频率约束：RSS 索引 10 分钟缓存，且**强制**两次拉取至少隔 120 秒（观众、彩虹岛明确要求 ≥2 分钟），按站持久化打点，重启、手动点「测试」都守得住；站点行可用 `rss_interval=秒` 调大，不能调小。

## ④ 配置

| 字段 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 可乐PT 用户名 / 密码 | 你的可乐PT 账号（登录一次后会保存会话 token，不用反复填） |
| 验证码 | **先打开插件详情页**，页面会显示一张验证码图片（5 分钟内不变），把 4 位数字填到这里保存即完成登录 |
| 两步验证码 | 账号开了 2FA 时填（可选） |
| 使用 MoviePilot 已配置下载器 | 勾选后使用 MP「设置 → 下载器」里的**所有**实例（qB / TR） |
| 下载器 | 每行一个：`地址,用户名,密码`；Transmission 写 `tr,地址,用户名,密码` |
| 其他站 | 每行一个站点，位置写法和键值写法都支持（见下） |
| 直接导入 IYUU 站点配置 | 把 IYUU 导出的站点 JSON 整个粘进来，自动取「启用且有 passkey」的站点 |
| IYUU Token | 填了就用 IYUU 云端做匹配（覆盖没有 pieces-hash 的站） |
| 辅种方向 | 双向 / 只补可乐 / 只补其他站 |
| 每小时最多添加 | 默认 20，可调（1–200） |
| 跳过校验 | 默认关闭；Transmission 无此选项，等价于总是校验 |
| 辅种标签 | 给补进来的种子打的标签（qB 用标签、TR 用 label）；留空则沿用原分类 |
| 校验失败降级 | 默认开：校验失败时自动用「跳过校验」重试一次 |
| 失败下一轮重试 | 默认开：失败的种子进队列（最多 40 条），下一轮优先重试 |
| 执行周期 | cron，默认每小时 |

## ⑤ 每个站点单独配置

「其他站」一行一个站点，字段按名字写，不写的留空：

| 字段 | 作用 |
|---|---|
| `passkey` | 站点密钥（必填），下载与 pieces-hash 认证都用它 |
| `sid` | IYUU 的站点编号，**走 IYUU 匹配必须有**（IYUU 导出的 JSON 里 `"sid": 数字`） |
| `rsskey` | 站点 RSS 密钥，没有 pieces-hash 的站靠它做精确匹配 |
| `uid` | 站点用户 id（少数站下载链接要用） |
| `channel` | 只让这个站用某条通道：`pieces` / `rss` / `iyuu` / `auto`（默认）。注意：IYUU 只要填了 `sid` 就生效，不受 channel 限制 |
| `rss_interval` | 该站 RSS 两次拉取的最小间隔（秒），只能调大 |
| `enabled` | 写 `enabled=0` 临时停掉这个站，不用删行 |

```
观众,https://audiences.me,passkey=<32位>,rsskey=<32位>,channel=rss
麒麟,https://www.hdkyl.in,passkey=<32位>,channel=pieces
朋友,https://pt.keepfrds.com,passkey=<32位>,sid=12
老站,https://old.example.com,passkey=<32位>,enabled=0
```

「站点来源」开关：

- **行文本 + IYUU 导入**（默认）：手写的行优先，同域名下没写的 `passkey/sid/rsskey` 从 IYUU 导入补齐；
- **只用手写的行**：完全忽略 IYUU 导入。

插件页会列出每个站能用哪几条通道（IYUU / RSS / pieces-hash）以及上次补种数。

### 旧写法（仍然兼容）

```
https://pt.example.com,<32位passkey>
朋友,https://pt.keepfrds.com,<32位passkey>
观众,https://audiences.me,<32位passkey>,<32位rsskey>
麒麟,https://www.hdkyl.in,passkey=<passkey>,sid=7,uid=30695
```

不知道 sid 时先不写，跑一轮后日志会打印「IYUU 返回了插件里没有配置的站点 sid：42」，照着补 `sid=42`。

## ⑥ 常见问题

**验证码看不清 / 刷新就不出图？**
0.10.12 起验证码缓存 5 分钟（站点侧 10 分钟有效），反复打开插件页不再是新的一张；取码与登录都走系统 curl，部分机器的 Python TLS 握手问题不再报「获取失败」。仍失败时页面会显示具体原因（HTTP 码 / 异常类型）。

**提示会话过期？**
重新打开插件页取验证码、填一次即可；插件只保存可乐的会话 token，其他站只要域名 + passkey。

**为什么有的种子补上了却没有别的标签？**
辅种进去的是**新的种子**，标签由你/MP 后来打。若某颗之前被插件自动清理过（文件缺失），重新补种时插件会把它原来的标签一并恢复（0.10.15 起）。

**明明内容一致却补不上？**
看插件页「检测」里该站的可用通道；常见原因：站点没开放匹配接口、IYUU 未收录、RSS 里没有这颗（RSS 只覆盖最近发布）。

**MoviePilot 崩溃重启？**
0.10.13 起插件不再使用线程池（此前站点探测/RSS 索引的 6 线程并发会让 CPython 3.14 段错误）；0.10.14 起插件载入时会打开 `faulthandler`，下次若仍崩溃，用下面的命令取线程栈反馈：

```bash
docker logs moviepilot-v2 2>&1 | grep -A 60 "Fatal Python error" | tail -60
```

## ⑦ 安全与隐私

插件仓库里**不含任何凭据**；可乐账号、会话 token、其他站 passkey、IYUU token、下载器密码全部只存在你自己的 MoviePilot 里。

## ⑧ 更新记录（近期）

- **0.10.15**：自愈删除种子前记住其标签，重新补种时恢复（不再丢「已整理 / 订阅」等标签）
- **0.10.14**：插件载入时启用 `faulthandler`，崩溃时可定位到具体线程栈
- **0.10.13**：去掉插件内线程池，全部顺序执行（修 MoviePilot 偶发段错误重启）
- **0.10.12**：验证码缓存 5 分钟、取码/登录改走 curl；支持 MP 里配置的多个下载器
- **0.10.11**：去重记忆自愈（客户端里已没有的种子会重新补）
- **0.10.9–0.10.10**：`info.source` 归一化匹配、文件清单指纹匹配（内容一致即可辅种）
- **0.10.7–0.10.8**：站点连通性检测按通道实测；RSS 强制最小间隔 120 秒
