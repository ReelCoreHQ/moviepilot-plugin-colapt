# 安全说明

## 仓库里不会有任何凭据

本仓库是插件源码，**不包含任何站点账号、passkey、IYUU token、API key 或下载器密码**。
所有凭据都只存在于你自己的 MoviePilot 实例里（插件配置 / 插件数据目录），不会回传给本仓库作者。

提交代码前请确认：不要把你自己的配置、日志、数据库或截图（含 passkey、Cookie、内网地址）提交上来。

## 插件会接触哪些凭据

| 凭据 | 用途 | 存在哪里 |
|---|---|---|
| 可乐PT 用户名 / 密码 | 登录站点（换会话 token） | MoviePilot 插件配置 |
| 可乐PT 会话 token | 之后调用站点接口 | 插件数据目录 |
| 其他站 passkey | 查 `/api/pieces-hash`、拼下载链接 | MoviePilot 插件配置 |
| IYUU token | 用 IYUU 云端做辅种匹配 | MoviePilot 插件配置 |
| 下载器地址/账号/密码 | 列出已完成种子、添加种子 | MoviePilot 插件配置 |

插件只向外发两类请求：可乐PT（`www.colapt.com`）与你配置的其他站/下载器/IYUU；
没有遥测、没有第三方上报。

## 报告问题

发现安全问题请开一个 Issue（不要贴出你自己的凭据）。
