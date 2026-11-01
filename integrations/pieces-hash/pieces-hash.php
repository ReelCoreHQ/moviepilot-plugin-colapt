<?php
/**
 * NexusPHP 站点侧的辅种查询接口：POST /api/pieces-hash
 *
 * 客户端（MoviePilot 可乐PT辅种插件 / IYUU / ReseedPuppy 等）把本地种子的 pieces 段 SHA1
 * 发过来，站点回「这些 pieces 属于哪个种子」。跨站辅种里这是最硬的匹配方式：只比 info.pieces，
 * 不看文件名也不看标题，误判率几乎为零。
 *
 * 请求（推荐 JSON）：
 *   POST /api/pieces-hash
 *   Content-Type: application/json
 *   {"passkey":"<32 位密钥>","pieces_hash":["<40 位 sha1>", ...]}
 * 也接受表单：passkey=...&pieces_hash[]=...&pieces_hash[]=...
 *
 * 响应：
 *   {"ret":0,"msg":"ok","data":{"<pieces_hash>":<torrent_id>, ...}}   // 未命中即空对象
 *   {"ret":1,"msg":"原因"}                                            // ret!=0 表示出错
 *
 * 部署：把本文件与 pieces-hash.config.php 放到站点根目录（与 torrents.php 同级），
 * 再把 /api/pieces-hash 指向本文件（见 nginx.conf.example / htaccess.example）。
 */

declare(strict_types=1);

const PH_MAX_HASHES = 200;      // 单次最多查询多少个 pieces_hash
const PH_RATE_PER_MIN = 60;     // 单用户每分钟最多多少次
const PH_RATE_DIR = __DIR__ . '/cache/pieces-hash-rate';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

/** 统一出口。 */
function ph_reply(bool $ok, string $msg, ?array $data = null, int $code = 200): never
{
    http_response_code($code);
    // 空数组必须序列化成 {} 而不是 []：客户端按对象取键，[] 会被判成「返回格式不匹配」
    $payload = ($data === null || $data === []) ? new stdClass() : $data;
    echo json_encode(
        ['ret' => $ok ? 0 : 1, 'msg' => $msg, 'data' => $payload],
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    );
    exit;
}

/** 读配置（同目录 pieces-hash.config.php）。 */
function ph_config(): array
{
    $file = __DIR__ . '/pieces-hash.config.php';
    if (!is_file($file)) {
        ph_reply(false, '服务端未配置 pieces-hash.config.php', null, 500);
    }
    $cfg = require $file;
    if (!is_array($cfg) || empty($cfg['dsn'])) {
        ph_reply(false, 'pieces-hash.config.php 缺少 dsn', null, 500);
    }
    return $cfg + ['user' => '', 'password' => '', 'table' => 'torrents', 'key_column' => 'pieces_hash'];
}

function ph_pdo(array $cfg): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }
    $pdo = new PDO($cfg['dsn'], (string)$cfg['user'], (string)$cfg['password'], [
        PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES   => false,
    ]);
    return $pdo;
}

/** 朴素的按分钟计数限流（不依赖 apcu / redis）。 */
function ph_rate_limit(string $key): bool
{
    if (!is_dir(PH_RATE_DIR) && !@mkdir(PH_RATE_DIR, 0775, true) && !is_dir(PH_RATE_DIR)) {
        return true; // 目录建不出来时不挡人
    }
    $bucket = (int) floor(time() / 60);
    $file = PH_RATE_DIR . '/' . sha1($key . ':' . $bucket) . '.txt';
    $hits = is_file($file) ? (int) @file_get_contents($file) : 0;
    $hits++;
    @file_put_contents($file, (string) $hits, LOCK_EX);
    return $hits <= PH_RATE_PER_MIN;
}

// ---- 1. 方法 ----
if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
    ph_reply(false, '只支持 POST', null, 405);
}

// ---- 2. 入参 ----
$raw = file_get_contents('php://input') ?: '';
$input = [];
if ($raw !== '' && ltrim($raw)[0] === '{') {
    $decoded = json_decode($raw, true);
    if (!is_array($decoded)) {
        ph_reply(false, 'JSON 解析失败', null, 400);
    }
    $input = $decoded;
} else {
    $input = $_POST;
}

$passkey = strtolower(trim((string) ($input['passkey'] ?? ($_GET['passkey'] ?? ''))));
$hashes  = $input['pieces_hash'] ?? [];
if (!is_array($hashes)) {
    $hashes = [$hashes];
}

if (!preg_match('/^[0-9a-f]{32}$/', $passkey)) {
    ph_reply(false, 'passkey 非法（应为 32 位十六进制）', null, 400);
}
if (!$hashes) {
    ph_reply(false, 'pieces_hash 不能为空', null, 400);
}
if (count($hashes) > PH_MAX_HASHES) {
    ph_reply(false, '一次最多 ' . PH_MAX_HASHES . ' 个 pieces_hash', null, 400);
}

$want = [];
foreach ($hashes as $h) {
    $h = strtolower(trim((string) $h));
    if (!preg_match('/^[0-9a-f]{40}$/', $h)) {
        ph_reply(false, 'pieces_hash 格式不对（应为 40 位十六进制）：' . substr($h, 0, 12), null, 400);
    }
    $want[$h] = true;
}

// ---- 3. 认证 + 限流 ----
$cfg = ph_config();
try {
    $pdo = ph_pdo($cfg);
    $st = $pdo->prepare('SELECT id FROM users WHERE passkey = ? LIMIT 1');
    $st->execute([$passkey]);
    $uid = $st->fetchColumn();
} catch (Throwable $e) {
    error_log('pieces-hash: db error ' . $e->getMessage());
    ph_reply(false, '服务端数据库不可用', null, 503);
}
if ($uid === false || $uid === null) {
    ph_reply(false, 'passkey 不存在', null, 403);
}
if (!ph_rate_limit('u' . $uid)) {
    ph_reply(false, '请求过于频繁，请降低频率', null, 429);
}

// ---- 4. 查询（预编译，只回 id） ----
$table = preg_replace('/[^a-z0-9_]/i', '', (string) $cfg['table']) ?: 'torrents';
$column = preg_replace('/[^a-z0-9_]/i', '', (string) $cfg['key_column']) ?: 'pieces_hash';
$in = implode(',', array_fill(0, count($want), '?'));
try {
    $st = $pdo->prepare("SELECT id, {$column} AS ph FROM {$table} WHERE {$column} IN ({$in})");
    $st->execute(array_keys($want));
    $data = [];
    foreach ($st->fetchAll() as $row) {
        $ph = strtolower((string) $row['ph']);
        if (isset($want[$ph]) && !isset($data[$ph])) {
            $data[$ph] = (int) $row['id'];
        }
    }
} catch (Throwable $e) {
    error_log('pieces-hash: query error ' . $e->getMessage());
    ph_reply(false, '查询失败（确认表里有 pieces_hash 列）', null, 503);
}

ph_reply(true, 'ok', $data);
