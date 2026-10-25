# 可乐PT 自动辅种（MoviePilot 插件）

参考 [ReseedPuppy](https://github.com/tt12580/Reseed-Puppy) 与 MoviePilot 的「青蛙辅种助手」思路，做一个**只服务可乐PT 用户**的辅种插件。

## 逻辑

1. 扫描你配置的 qBittorrent / Transmission（可多个，也可直接用 MoviePilot 已配置的下载器）里**已完成**的种子；
2. 把这些种子的 info_hash 批量发给可乐PT：
   - `via=source_info_hash`：客户端里有同一份文件（来源别的站），**拉可乐这颗种子回原下载器辅种**；
   - `via=info_hash`：客户端里已经是可乐这颗种子，跳过；
3. 可选反向：对 `via=info_hash` 的可乐种子，下载可乐的 `.torrent` 算出 `pieces_hash`，去你配置的其他 NexusPHP 站点 `/api/pieces-hash` 查同 pieces 的种子，命中就拉对方种子回来辅种；
4. 每小时最多添加 **20** 个（滚动 60 分钟，可调）；是否跳过校验由你决定，**默认校验**。

## 安装

### 方式一：手动放入插件目录（推荐自用）

把 `plugins/colacrossseed/` 整个目录放到 MoviePilot 的插件目录（`app/plugins/`，或挂载的 `/config/plugins/`），重启 MoviePilot，在「插件」里启用「可乐PT辅种」。

### 方式二：作为插件仓库

把本仓库加到 MoviePilot 的插件仓库地址（设置 → 插件市场 → 添加仓库），即可在市场中看到。

## 配置

| 字段 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 可乐PT 用户名 / 密码 | 你的可乐PT 账号（登录一次后会保存会话 token，不用反复填） |
| 验证码 | **先打开插件详情页**，页面会显示一张验证码图片，把 4 位数字填到这里，保存即完成登录 |
| 两步验证码 | 账号开了 2FA 时填（可选） |
| 使用 MoviePilot 已配置下载器 | 勾选后使用 MP 里的 qB/TR；再配合下面「额外下载器」可同时管多个 |
| 额外下载器（JSON） | `[{"name":"QB-2","type":"qbittorrent","url":"http://ip:8080","username":"admin","password":"xxx"}]`，`type` 也可以是 `transmission` |
| 辅种方向 | 双向 / 只补可乐 / 只补其他站 |
| 每小时最多添加 | 默认 20，可调（1–200） |
| 跳过校验 | 默认关闭（默认校验）；Transmission 无跳过选项，等价于总是校验 |
| 执行周期 | cron，默认每小时 |
| 其他站（JSON） | `[{"name":"Pter","url":"https://pt.example.com/","passkey":"xxx"}]`，方向二用；站点需支持 `/api/pieces-hash` |

## 注意

- 会话过期（站点返回 401）时插件会发站内通知：重新打开插件页面取验证码、填一次即可；
- 插件不保存可乐密码以外的站点 cookie；其他站只需要域名 + passkey；
- 可乐PT 接口限流：列表/match 60 次/分/凭据，下载 120 次/分，插件已按批（500 hash/次）调用；
- 默认 20 个/小时是为了避免一次辅种太多触发源站风控，用户可自行调低。
