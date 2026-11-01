# 站点侧 pieces-hash 接口（给 PT 站加辅种匹配能力）

这是给**站点管理员**用的一小套补丁：装完之后，任何辅种客户端（可乐PT辅种插件、IYUU、ReseedPuppy…）
都能用「pieces 段的 SHA1」在你的站点上**精确**找到同一份文件，从而跨站辅种。

观众（audiences.me）、麒麟（hdkyl.in）这类站现在能被我们的插件识别，靠的就是这个接口
（麒麟用的是 Laravel 版的同一套约定）。

## 为什么是 pieces 而不是 info_hash

同一份文件在不同站点做出来的种子，info_hash 各不相同（tracker、发布时间、发布者都在 info 里），
但 `info.pieces` 那一段是**文件内容本身**算出来的，完全一致。所以按 pieces 匹配不会误判，
也不会因为你换了 tracker 就失配。

## 装什么

| 文件 | 作用 |
|---|---|
| `pieces_hash.sql` | 给 `torrents` 表加 `pieces_hash CHAR(40)` + 索引 |
| `pieces-hash.php` | 接口本体：`POST /api/pieces-hash` |
| `pieces-hash.config.example.php` | 数据库配置模板（复制成 `pieces-hash.config.php` 填写） |
| `backfill-pieces-hash.php` | 回填历史种子（命令行，幂等，可反复跑） |
| `nginx.conf.example` / `htaccess.example` | 把 `/api/pieces-hash` 指到上面那个 php |
| `curl-check.sh` | 部署后的自检脚本 |

## 部署（约 10 分钟）

### 1. 加列

```bash
mysql -u root -p nexusphp < pieces_hash.sql
```

### 2. 建只读账号（推荐，别用站点主账号）

```sql
CREATE USER 'ph_ro'@'localhost' IDENTIFIED BY '强密码';
GRANT SELECT ON `nexusphp`.`users`    TO 'ph_ro'@'localhost';
GRANT SELECT ON `nexusphp`.`torrents` TO 'ph_ro'@'localhost';
```

### 3. 放文件并配置

```bash
cp pieces-hash.php pieces-hash.config.example.php /var/www/你的站点/
cd /var/www/你的站点 && mv pieces-hash.config.example.php pieces-hash.config.php
# 编辑 pieces-hash.config.php 里的 dsn / user / password
mkdir -p cache/pieces-hash-rate && chown www-data:www-data cache/pieces-hash-rate
```

### 4. 挂路由

- Nginx：把 `nginx.conf.example` 里的 `location = /api/pieces-hash` 放进 server 块（改 php-fpm socket 路径）；
- Apache：把 `htaccess.example` 的内容追加到站点根 `.htaccess`。

### 5. 回填历史种子

```bash
php backfill-pieces-hash.php --dir=/var/www/你的站点/torrents --dry-run   # 先看统计
php backfill-pieces-hash.php --dir=/var/www/你的站点/torrents --limit=2000
# 之后每小时跑一批（crontab）：
# 0 4 * * * cd /var/www/你的站点 && php backfill-pieces-hash.php --dir=torrents --limit=5000 >> /var/log/pieces-hash.log 2>&1
```

新发布的种子也要写这个字段，在发种流程里补一句：

```php
$pieces = $CURUSER['torrent']…  // 见下：从上传的 .torrent 里取 info.pieces
$stmt = $pdo->prepare("UPDATE torrents SET pieces_hash=? WHERE id=?");
$stmt->execute([sha1($pieces), $torrentId]);
```

（`backfill-pieces-hash.php` 里有现成的 `ph_extract_pieces()`，可以直接复用。）

### 6. 自检

```bash
./curl-check.sh https://你的站点 <某个用户的32位passkey> <某个种子的pieces_hash>
```

期望：方法检查 405、坏 passkey 403、真实查询 `{"ret":0,"msg":"ok","data":{...}}`。

## 接口约定（客户端侧）

```
POST /api/pieces-hash
Accept: application/json
Content-Type: application/json

{"passkey":"<32位十六进制>","pieces_hash":["<40位sha1>", ...]}
```

```json
{"ret":0,"msg":"ok","data":{"<pieces_hash>":<torrent_id>}}
```

- `ret != 0` 表示出错（错误码用 HTTP 状态同时表达：400 参数、403 认证、405 方法、429 限流）；
- 没命中就是 `"data":{}`（注意要序列化成 `{}`，不能是 `[]`）；
- 单次最多 200 个 hash，单用户 60 次/分钟。

## 常见问题

**Q：为什么只回 torrent_id，不回标题/大小/下载链接？**
A：这是匹配接口，不是查询接口。只回 id 让客户端自己按 id 下载，接口就算被盗刷也只能拿到一串数字。

**Q：站点没有 `torrents` 表 / 表名不同？**
A：改 `pieces-hash.config.php` 里的 `table` 和 `key_column`。

**Q：能不能不做回填，只对新种生效？**
A：可以，接口对新种立刻生效；历史种子查不到而已。回填只是把老种子也纳入匹配。

**Q：能不能不做接口，直接用 RSS？**
A：可以。可乐PT辅种插件支持 `rsskey` 通道（`torrentrss.php` 的 `guid` 就是 info_hash），
但只覆盖 RSS 里能看到的最近若干条，不像 pieces-hash 是全站精确匹配。
