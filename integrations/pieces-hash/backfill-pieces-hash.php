<?php
/**
 * 回填 torrents.pieces_hash（命令行运行）。
 *
 *   php backfill-pieces-hash.php --dir=/path/to/torrents --limit=500 --dry-run
 *
 * 站点把种子文件存在 $torrent_dir 下，文件名一般是 <id>.torrent；如果你的命名不同，
 * 用 --pattern 改成例如 %s.torrent 或 %d 的写法（%d 会被替换成种子 id）。
 *
 * 特点：
 *   · 只补 pieces_hash 为空的行动，幂等，可以反复跑；
 *   · 单文件失败不影响整体，最后打印失败清单；
 *   · --dry-run 只统计不写库。
 */

declare(strict_types=1);

require __DIR__ . '/pieces-hash.config.php';

$opts = getopt('', ['dir:', 'limit::', 'pattern::', 'dry-run', 'verbose']);
$dir = rtrim((string) ($opts['dir'] ?? ''), '/');
if ($dir === '' || !is_dir($dir)) {
    fwrite(STDERR, "用法：php backfill-pieces-hash.php --dir=/path/to/torrents [--limit=500] [--pattern='%d.torrent'] [--dry-run]\n");
    exit(2);
}
$limit   = (int) ($opts['limit'] ?? 0);
$pattern = (string) ($opts['pattern'] ?? '%d.torrent');
$dryRun  = array_key_exists('dry-run', $opts);
$verbose = array_key_exists('verbose', $opts);

/** 解析 bencode，取出 info.pieces。返回原始字节，失败返回 null。 */
function ph_extract_pieces(string $raw): ?string
{
    $pos = 0;
    $parse = function () use (&$pos, &$raw) {
        $c = $raw[$pos] ?? '';
        if ($c === 'i') {
            $end = strpos($raw, 'e', $pos);
            if ($end === false) {
                throw new RuntimeException('int 未闭合');
            }
            $v = substr($raw, $pos + 1, $end - $pos - 1);
            $pos = $end + 1;
            return (int) $v;
        }
        if ($c === 'l') {
            $pos++;
            $out = [];
            while (($raw[$pos] ?? '') !== 'e') {
                $out[] = $parse();
            }
            $pos++;
            return $out;
        }
        if ($c === 'd') {
            $pos++;
            $out = [];
            while (($raw[$pos] ?? '') !== 'e') {
                $k = $parse();
                $out[is_int($k) ? (string) $k : $k] = $parse();
            }
            $pos++;
            return $out;
        }
        $colon = strpos($raw, ':', $pos);
        if ($colon === false) {
            throw new RuntimeException('字符串长度缺失');
        }
        $len = (int) substr($raw, $pos, $colon - $pos);
        $v = substr($raw, $colon + 1, $len);
        $pos = $colon + 1 + $len;
        return $v;
    };

    try {
        $data = $parse();
    } catch (Throwable $e) {
        return null;
    }
    return isset($data['info']['pieces']) && is_string($data['info']['pieces']) ? $data['info']['pieces'] : null;
}

$pdo = new PDO($cfg['dsn'], (string) $cfg['user'], (string) $cfg['password'], [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$table  = preg_replace('/[^a-z0-9_]/i', '', (string) ($cfg['table'] ?? 'torrents')) ?: 'torrents';
$column = preg_replace('/[^a-z0-9_]/i', '', (string) ($cfg['key_column'] ?? 'pieces_hash')) ?: 'pieces_hash';

$sql = "SELECT id FROM {$table} WHERE {$column} IS NULL ORDER BY id";
if ($limit > 0) {
    $sql .= ' LIMIT ' . $limit;
}
$ids = $pdo->query($sql)->fetchAll(PDO::FETCH_COLUMN);
printf("待回填 %d 行（目录 %s，文件名规则 %s）\n", count($ids), $dir, $pattern);

$upd = $pdo->prepare("UPDATE {$table} SET {$column} = ? WHERE id = ?");
$done = 0;
$failed = [];
foreach ($ids as $id) {
    $file = $dir . '/' . sprintf($pattern, (int) $id);
    if (!is_file($file)) {
        $failed[] = [$id, '找不到种子文件'];
        continue;
    }
    $pieces = ph_extract_pieces((string) file_get_contents($file));
    if ($pieces === null || $pieces === '') {
        $failed[] = [$id, '种子里没有 pieces'];
        continue;
    }
    $hash = sha1($pieces);
    if (!$dryRun) {
        $upd->execute([$hash, (int) $id]);
    }
    $done++;
    if ($verbose) {
        printf("  %d -> %s\n", $id, $hash);
    }
}

printf("%s：成功 %d，失败 %d\n", $dryRun ? '试运行（未写库）' : '已写入', $done, count($failed));
foreach (array_slice($failed, 0, 20) as [$id, $why]) {
    printf("  失败 id=%d：%s\n", $id, $why);
}
exit($failed && $done === 0 ? 1 : 0);
