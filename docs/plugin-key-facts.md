# 插件密钥方案：代码事实（已验证，2026-11-19）

本文件只记录**读过代码/线上 schema 确认**的事实，不含设计意见。设计讨论见
`docs/plugin-key-design.md`。

## 1. 站点侧现状

### 1.1 插件登录面已存在，且强制验证码

`internal/identity/handlers.go:179-180` 注册：

- `POST /api/v1/plugin/sessions` → `pluginLogin`
- `GET  /api/v1/plugin/captcha` → `pluginCaptchaImage`

`internal/identity/pluginlogin.go`：

- 图形验证码**先于**密码校验，缺失即 `captcha_required`；
- 验证码寿命 `pluginCaptchaLifetime = 120s`，答案存 base64Captcha，**从不回显**；
- 支持 TOTP 账号，只收 6 位动态码，恢复码明确拒绝；
- 登录限流 `AllowLoginAttempt` + `loginGate`；
- 201 响应体：`{status, session_token, csrf_token, expires_at, username}`。

结论：**任何"登录成功"都花掉一次人工验证码**。"自动签发"不是零交互，
只是"搭在已经发生的那次人工登录上"。

### 1.2 会话寿命 7 天

`internal/identity/session.go:154`：`const sessionLifetime = 7 * 24 * time.Hour`。

这决定了 2 小时 TTL 是否构成死锁（见设计文档）。

### 1.3 上报接口走会话面，不是机器面

`internal/seedgroup/seedgroup.go:40`：

```
POST /api/v1/plugin/seed-group/report
```

认证是 `s.Gate.Check(w, r, "reportSeedGroup")` → `authSession` 分支
（`internal/gateway/gateway.go:226`）。

`gateway.check`（`gateway.go:218-278`）实际规则：

```go
machine := kernel.Machine(r.Context())
if write && !machine && r.Header.Get("Origin") != g.core.SiteOrigin {
    fail(w, apierr.OriginDenied, ...)
}
```

即：**写请求要求 `Origin` 精确等于站点 Origin**，然后要求 session + `X-CSRF-Token`
（常量时间比较 `u.Session.CSRF`），再查权限，最后过限流预算。

### 1.4 机器面明确拒绝三种头

`internal/apikeys/automation.go:69`：

```go
if len(values) != 1 || !tokenPattern.MatchString(values[0]) ||
   len(r.Header.Values("Cookie")) != 0 ||
   len(r.Header.Values("Authorization")) != 0 ||
   len(r.Header.Values("Origin")) != 0 { ... }
```

机器面（`/api/v1/automation/*`）**只要出现 Cookie / Authorization / Origin 就拒**。

插件上报带 `Origin` + `Cookie` + `X-CSRF-Token`（`colaseedgroup/__init__.py:2132-2139`），
所以插件密钥**不能**挂在 `internal/apikeys` 机器面入口上，否则必然被拒。

### 1.5 现有 key 的形状与 scope

`internal/apikeys/keys.go:63`：`^rcapi_[A-Za-z0-9_-]{43}$`（43 字符 base64url = 32 字节随机）。

`internal/apikeys/scope.go`：

```go
ScopeUpload = "torrent.upload"
ScopeCompat = "compat"
scopeBoth   = "torrent.upload,compat"
```

`NormalizeScopes` 对未知取值**直接失败**，不"尽量接受"。

库表 `rc_site_api_keys`（`migrations/0000_baseline.sql:361-380`）关键列：
`token_hash binary(32) UNIQUE`、`scope varchar(48)`、`expires_at`、`last_used_at`、
`revoked_at`、`session_token_hash`，并有：

```sql
CONSTRAINT `site_api_key_scope_values`
  CHECK (`scope` in ('compat','torrent.upload','torrent.upload,compat'))
```

新增插件 scope 必须新迁移放宽这个 CHECK，且 `NormalizeScopes` 同步接受。

### 1.6 站点从未校验 X-Report-Token

`grep -rn "X-Report-Token|ReportToken|report_token" --include=*.go` → **零命中**。

但插件**已经在发**这个头：`colaseedgroup/__init__.py:2138`

```python
if self._seedgroup_report_token:
    headers["X-Report-Token"] = self._seedgroup_report_token
```

配置项 `seedgroup_report_token` 已读（:1390）、已持久化（:1456）。

结论：**客户端半成品已在线上，服务端是空的。** 这是改动最小的挂载点。

### 1.7 body 里的身份不被信任（已正确）

`seedgroup.go` report 写入用 `user.ID, user.Username`（来自认证），
注释明确："行是谁写的由会话决定，body 里的 uid 不参与"。

这条必须保留 —— 插件密钥上线后同样只能写自己那一行。

## 2. 插件侧现状

### 2.1 登录必须人工验证码

`colacrossseed/__init__.py:1226`、`colaseedgroup/__init__.py:1461`：

`_login(captcha_code)` 需要 `captcha_id`（来自 `get_data("captcha_id")`，
由 `_fetch_captcha()` 取图）+ 4 位数字。成功后 `captcha_code` 清空。

`init_plugin` 里同步调用 `_login`（crossseed:1162-1163、seedgroup:1406-1407）。

### 2.2 两个插件各自持有一套凭据

`colacrossseed` 与 `colaseedgroup` 是**两个独立插件**，各自有
`username/password/session_token`，各自登录。一个站点账号 + 一台机器
= 至少 2 次登录 = 至少 2 把 key。

### 2.3 密码明文落盘

`colacrossseed:1196`、`colaseedgroup` 同类位置：`_persist()` 的数据字典里
直接放 `"password": self._password`。MP 存在 `/config/user.db` 的
`plugininstance.config_data` / `plugindata`。

这是"改用密钥"的真实收益：不是省字节，是**停止明文存密码**。

### 2.4 上报请求头（seedgroup）

`colaseedgroup/__init__.py:2132-2139`：

```python
headers = {"Content-Type": "application/json", "Origin": COLA_BASE}
if self._session_token:
    headers["Cookie"] = f"reelcore_session={self._session_token}"
if csrf:
    headers["X-CSRF-Token"] = csrf
if self._seedgroup_report_token:
    headers["X-Report-Token"] = self._seedgroup_report_token
```

注意 `Cookie` 是**有条件的**（`if self._session_token`）。

`colacrossseed` 的同类写请求（:1465-1467）三件套写法一致。

### 2.5 两个不同的 key 字段，语义不同

- `seedgroup_api_key` / 旧名 `seedgroup_api_token`（:1351）：**用户手填**的机器面 key，
  走 `X-API-Key` 拉官方清单（`:2847`）；
- `seedgroup_report_token`（:1390）：上报用的独立头。

设计时**不能复用 `seedgroup_api_key`**：自动签发写进去会静默覆盖用户手填的
`rcapi_` key，两者是不同授权面。

## 3. Origin 匹配性（已闭环）

`SiteOrigin` 来自环境变量 `REELCORE_SITE_ORIGIN`（`internal/kernel/client.go:70`）；
测试夹具用 `https://www.colapt.com`（`internal/media/http_preview_localization_test.go:193`），
与插件 `COLA_BASE`（两插件 :46/48）一致。

线上上报当前返回 200（`covered_gib=1348.778` 正常入库），而写请求必须通过
`Origin == SiteOrigin` 校验 —— 因此可**反推**生产值已经相等，无需再查。

## 4. 仍未确认

- 生产 23 的 `rc_site_schema_migrations` 是否已到 0230（新迁移必须幂等）。

