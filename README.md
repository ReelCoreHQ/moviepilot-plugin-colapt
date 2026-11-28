# 可乐PT · MoviePilot 辅种插件

这个仓库只有一个插件：可乐PT 辅种的 `ColaCrossSeed`。

> 保种组是**另一个仓库**：[moviepilot-plugin-colaseedgroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup)（同一份源码也提供独立 Docker 版，端口 3006）

| 插件 | 作用 | 一句话 |
|---|---|---|
| **可乐PT辅种** `ColaCrossSeed` | 把下载器里**其它站已下完**、可乐PT 上也有的种子拉回来辅种 | 「别站有的，可乐也有 → 在可乐一起做种」 |

> 插件固定为「只补可乐」一个方向：只把可乐的种子补进下载器，不做反向补种，也不依赖 IYUU / ReseedPuppy。

> 使用说明（截图 + 常见问题 + 匹配原理）：<https://www.colapt.com/wiki/moviepilot-crossseed>

## 安装

### 方式一：插件市场（推荐）

MoviePilot → 设置 → 插件市场 → **添加仓库**，填：

```
https://github.com/ReelCoreHQ/moviepilot-plugin-colapt
```

刷新插件市场，找到 **可乐PT辅种**（`ColaCrossSeed`）安装即可。

### 方式二：一键脚本（直接拷进容器）

在跑 MoviePilot 的宿主机上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash
# 卸载
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/moviepilot-plugin-colapt/main/install.sh | bash -s uninstall
```

容器升级重建后要重跑一次；想跟随升级请用方式一。

## 可乐PT辅种 `ColaCrossSeed` 做什么

1. 扫描 MoviePilot「设置 → 下载器」里已配置的 qBittorrent / Transmission 实例中**已完成**的种子；
2. 把这些种子的 info_hash 批量交给可乐PT 反查：命中 `via=source_info_hash`（客户端里有同一份文件、来源是别的站）的，就**把可乐这颗种子拉回来加进下载器**，同一份文件在可乐也开始做种；哈希没直接中的，再用 `info.source` 归一化 / 内容指纹补查一遍；客户端里已经是可乐这颗（`via=info_hash`）则跳过；
3. 默认每日 00:00 自动跑一次（cron 可改）；每小时最多添加 N 个（滚动 60 分钟窗口，默认不限），是否跳过校验由你决定（默认校验）。

> 从 `0.14.0` 起插件固定为「只补可乐」，不再有「双向 / 只补其他站」方向，也不需要选其它站点或填 IYUU Token；下载器只使用 MoviePilot「设置 → 下载器」里已配置的实例，不再支持手填自定义下载器。

## 配置

| 字段 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 立即运行一次 | 勾上后保存，3 秒后立刻跑一轮辅种；跑完自动关掉，不会每次保存都触发。**无视启用总开关**（和官方 IYUU 插件一致，总开关关着也能手动跑一轮），但需已填 API Key，没 Key 时只提示不执行 |
| MP 站点管理里的可乐站 | 默认选中自动识别到的可乐站，请确认一下，不对可以自己改。插件读这个站在 MP「站点管理」里填的令牌，换 Key 后重载插件自动跟上。连通性报告会显示当前用的是哪个站、哪个用户 |
| API Key（手填兜底） | 站点管理没填时才用：到站点「账号 → Key → API Key」（<https://www.colapt.com/account/tracker?tab=api>）新建一把，范围勾**读取**和**下载**，粘到这里。两处都填时以站点管理的为准。插件只把它放在 `X-API-Key` 请求头里，不需要账号密码、验证码 |
| 辅种目标下载器（可多选） | 从 MP「设置 → 下载器」里已配置的实例中选，选中即**只往这几台加种**。名字支持模糊匹配（`tr` 命中 `MP-tr辅种`）。保存时即校验目标是否存在，MP 里改名/删实例会当场告警；任何一个解析不到或有歧义，本轮都不加种 |
| 每小时上限 | 默认 0 = 不限；按滚动 60 分钟窗口统计成功添加数。要限流可填正整数 |
| 跳过校验 | 默认关闭（默认校验）；Transmission 无此选项，等价于总是校验 |
| 辅种标签 | 给补进来的种子打的标签（qB 用标签、TR 用 label）；留空则沿用种子原来的分类 |
| 额外标签 | 逗号分隔，追加到补进来的种子上（例如站点名 `可乐PT`） |
| 继承原种子标签 | 默认开：原种子上已有的标签（站点标签、已整理、订阅…）一并带到补进来的种子 |
| 校验失败降级 | 默认开。校验态下失败最常见的原因是文件不完全一致（多一个 nfo/字幕），开启后自动用「跳过校验」重试一次 |
| 失败下一轮重试 | 默认开。真正失败的种子进队列（最多 40 条），下一轮优先重试，插件页能看到待重试清单 |
| 执行周期 | cron（5 段），默认 `0 0 * * *` = 每日 00:00；写错回落到每日 0 点 |

## 插件页与接口

- 插件页从上到下：四张统计卡片（上次运行 / 本轮补种 / 跳过已有 / 失败待重试）→ 运行状态 → 连通性检测（项目/详情两列，可折叠）→ 待重试 → 目标不可用告警 → 本轮明细（可折叠，含成功/重复/失败与原因）；
- 没勾选下载器时，运行状态会直接提示去设置页选；从旧版升级、只填过自定义下载器的用户，同一行会说明该配置已忽略。旧版本遗留的检测报告会自动隐去；
- `POST /api/v1/plugin/ColaCrossSeed/run` 手动跑一次辅种（仍守每小时上限）——注意这**不会**刷新连通性检测；
- `POST /api/v1/plugin/ColaCrossSeed/test` 检测可乐 API Key 与下载器连通性。每次保存配置也会自动检测一次（有 API Key 即可，不受总开关限制）；
- Key 被判无效（401 / 范围不足）后进入 1 小时冷却，期间定时任务不再发无效请求；在站点管理更新 Key 或做一次连通性检测即解除；
- 单轮互斥：保存触发的「立即运行一次」与 cron 同时到点时只跑一轮，避免重复加种与 API 双倍消耗；
- 通知：开「任务结果通知」后，本轮有新补种或失败才发一条完成通知；全是重复/已有则不发。「立即运行一次」无视总开关照跑，通知也照发。任务异常和 API Key 失效这两类提醒不受通知开关和总开关影响，始终发送；
- 升级后老版本遗留的旧检测报告会自动隐去（带格式版本校验），不会再显示「请粘贴 IYUU 站点 JSON」等废弃文案；保存一次配置即生成干净的新报告。

## 匹配原理

| 步骤 | 依赖 | 说明 |
|---|---|---|
| 可乐哈希反查（开放 API `POST /api/v1/torrents/hashes`） | 可乐PT API Key（读取） | 把下载器里已完成种子的 info_hash 批量交给可乐，判断可乐上有没有同一份文件（按 `info_hash` / `source_info_hash`） |
| `info.source` 归一化 | 同上（qB 4.5+ 能导出 `.torrent`） | 可乐发布时会把 `info.source` 盖成自己的章，同一份文件在不同站 info_hash 必然不同；把 source 换掉再算哈希去反查 |
| 文件清单指纹 `content_key` | 同上 | 「相对路径 + 字节数」的 SHA1，与谁重新打包无关；不需要导出种子，Transmission / 老 qB 也能用 |

命中后走 `createDownloadLink` 签一条 30 分钟直链，不带任何凭据 GET 回 `.torrent`，加回原下载器并带上标签。

## 注意

- 插件只走可乐PT 的**开放 API**（接口说明见 <https://www.colapt.com/api-docs>）：哈希反查 `matchTorrentHashes`、签下载地址 `createDownloadLink`、连通检测 `personalStats`；
- API Key 无效 / 被撤销（401）或缺范围（403 `api_key_scope_denied`）时插件发一条通知：去 MP 站点管理更新可乐站的令牌，或在插件设置里重填一把勾「读取」「下载」的 Key；更新后做一次连通性检测即解除 1 小时冷却；
- 插件只保存可乐的 API Key 与 MP 站点 id，不保存其它站凭据；
- 可乐PT 接口有限流（每把 Key 读 60 次/分、下载 120 次/分；哈希反查一次最多 500 条、一次只算一次读），插件按批调用并在进程内统一节流（55/分，多下载器共享），遇到 429 会短退避重试两次；
- 每小时总上限默认不限，需要更保守可自行调低。

## 安全与隐私

仓库里**不含任何凭据**；可乐 API Key、下载器密码全部只存在你自己的 MoviePilot 里。
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
