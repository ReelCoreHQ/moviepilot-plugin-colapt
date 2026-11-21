"""
可乐保种组（MoviePilot 插件）

从「可乐PT 辅种」里拆出来的独立插件，**只做保种组**，不碰辅种。

它做的事：
  1. 按档位（2T/5T/10T/15T/20T/自定义/全站官种）从站点官方资源池挑种子，搬进下载器长期保种；
  2. 体积「只超不少」：先挑官方、再挑放得下的最大颗，最后允许多吃一颗顶过档位；
  3. 进种限流：客户端里「正在下载」达到上限就停，等这批下完再补，让覆盖稳步追平；
  4. 达标后动态轮换：删做种人数最多的 N 颗、补最少的 N 颗；
  5. 每轮回站点核对，下架的种子自动清理（可连文件一起删）；
  6. 每轮向站点上报（档位/覆盖/缺口/限速/做种时长），站点据此算月度考核与退休进度。

解锁资格只认站点工作组（登录后读本人资料里的工作组头衔），没有工作组的账号保存配置时会被强制关掉。
辅种请装另一个插件「可乐PT辅种」（ColaCrossSeed），两者可以并存、各配各的下载器。
"""
import base64
import hashlib
import hmac
import json
import re
import subprocess
import time
from urllib.parse import urlencode, urljoin
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from threading import Event, Lock
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger
from bencode import bdecode

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

# 段错误时把当时的线程栈打进日志（MoviePilot 直接被 SIGSEGV 杀掉时，只有内核那一行
# "segfault at 1 ... libpython3.14.so" 是没法定位的）。enable() 只是注册信号处理器，
# 不改变任何业务行为。
try:
    import faulthandler

    faulthandler.enable()
except Exception:  # 老解释器/受限环境忽略
    pass

COLA_BASE = "https://www.colapt.com"
# 可乐PT 发布种子时会给 info.source 盖的章。同一份文件在别站有别的 source，
# info_hash 因此不同，匹配时必须归一化（见 _torrent_info_hash_variants）。
COLA_SOURCE_TAG = f"[{COLA_BASE}] ColaPT"
# 已关闭 / 已换域名的站点：IYUU 导出里常见旧地址，这里做一次纠正（2026-10-25 核对）。
SITE_REMAP = {
    "www.hddolby.com": "https://www.uhddolby.com",
    "hddolby.com": "https://www.uhddolby.com",
}
SITE_SKIP = {
    "ptsbao.club",       # 烧包：站点已关闭
    "www.ptsbao.club",
}
MANDATORY_TAG = "可乐辅种"   # 加进客户端的种子一律带这个标签，方便筛选/清理
COLA_MATCH_BATCH = 500
# colapt.com 在部分国内网络会被解析到连不通的 Cloudflare 段；这些是实测可用的备用段。
COLA_FALLBACK_IPS = ("104.21.66.130", "172.67.132.88", "104.26.10.0")
REQUEST_TIMEOUT = 90
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

# ---------------- 可乐保种组 ----------------
# 「可乐保种组」= 站点侧的定向保种计划：给授权成员开放一组资源池，成员按档位
# （2T / 5T / 10T / 15T / 20T / 自定义≥2T）把池子纳入做种，**体积只能超、不能少**。
# 站点的权限/资源接口尚未上线，这里先按预留的 v1 契约探测（见 docs/seed-group-api.md）：
# 拿不到 200 就整块锁定，面板上直说缺什么，不静默失败。
SEED_GROUP_TAG = "可乐保种组"
GIB = 1024 ** 3
# 站点「工作组」= rc_site_groups.title 以「工作组」开头的组（生产：工作组-审核 / 工作组-保种 /
# 工作组-组长 / 工作组-转载）。插件不看 is_system、不看 vip，只认这个标题前缀，因为「转载组」
# 是自建组（is_system=0）却同样是工作组。前缀可用配置项覆盖。
SEED_GROUP_STAFF_PREFIX = "工作组"
# 官方发布清单（机器面）：X-API-Key，一页最多 100 条，不回 Cookie。
OFFICIAL_API = "/api/v1/automation/official/torrents"
OFFICIAL_PAGE_LIMIT = 100
# 限速上报收集端：走站点域名（HTTPS），不暴露源站 IP
SEED_GROUP_REPORT_URL = "https://www.colapt.com/api/v1/plugin/seed-group/report"
# 站点将来若提供专用的保种组资源池接口，插件优先走它；没有就走官方清单。
SEED_GROUP_API = "/api/v1/plugin/seed-group"
# 档位 -> 体积下限（GiB）。固定档之外还有一个 custom（自定义），下限同样不许低于 20T。
SEED_GROUP_TIERS: Dict[str, int] = {"2T": 2048, "5T": 5120, "10T": 10240, "15T": 15360, "20T": 20480}
SEED_GROUP_CUSTOM_TIER = "custom"
SEED_GROUP_ALL_TIER = "all"        # 全站官种：有多少保多少（target 由池子总量决定）
SEED_GROUP_CUSTOM_MIN_GIB = 1              # 自定义档只要求「不是 0」：100M / 100G / 1T / 1P 都行
SEED_GROUP_CUSTOM_MAX_GIB = 1024 * 1024    # 上限 1 PiB（防手滑填天文数字）
# 老配置里的 “20T+” 就是现在的 20T（档位改名，不改数值）
SEED_GROUP_TIER_ALIASES: Dict[str, str] = {"20T+": "20T"}
SEED_GROUP_TIER_LABELS: Dict[str, str] = {
    "2T": "2T 档（≥ 2048 GiB）",
    "5T": "5T 档（≥ 5120 GiB）",
    "10T": "10T 档（≥ 10240 GiB）",
    "15T": "15T 档（≥ 15360 GiB）",
    "20T": "20T 档（≥ 20480 GiB）",
    SEED_GROUP_CUSTOM_TIER: "自定义档（100M / 100G / 1T / 1P）",
    SEED_GROUP_ALL_TIER: "全站官种（有多少保多少）",
}


def seed_group_tier_key(tier: str) -> str:
    """把配置里的档位归一化：大小写不敏感，老值 20T+ → 20T，未知值 → 2T。"""
    key = str(tier or "").strip()
    key = SEED_GROUP_TIER_ALIASES.get(key, key)
    if key.upper() == SEED_GROUP_CUSTOM_TIER.upper():
        return SEED_GROUP_CUSTOM_TIER
    if key.upper() in (SEED_GROUP_ALL_TIER.upper(), "SITE", "ALL-OFFICIAL"):
        return SEED_GROUP_ALL_TIER
    for name in SEED_GROUP_TIERS:
        if key.upper() == name.upper():
            return name
    return "2T"


SIZE_UNITS_GIB = {"M": 1.0 / 1024, "G": 1.0, "T": 1024.0, "P": 1024.0 * 1024}


def parse_size_gib(value: Any) -> float:
    """把「100M / 100G / 1T / 1P / 2048」解析成 GiB。

    不带单位的数字按 GiB 理解（老配置 seedgroup_custom_gib=2048 仍成立）；
    单位大小写、B/iB 后缀都容忍。解析不出来返回 0。
    """
    text = str(value or "").strip().upper().replace("IB", "").replace("B", "").strip()
    if not text:
        return 0.0
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([MGPT]?)$", text)
    if not match:
        return 0.0
    try:
        number = float(match.group(1))
    except Exception:
        return 0.0
    unit = match.group(2) or "G"
    return max(0.0, number * SIZE_UNITS_GIB.get(unit, 1.0))


def seed_group_target_gib(tier: str, custom_gib: Any = None) -> int:
    """档位 → 体积下限（GiB）。

    · 固定档查表；未知档退回 2T，绝不返回 0；
    · 自定义档支持带单位（100M / 100G / 1T / 1P），不带单位按 GiB；只有 0 会被抬到 1 GiB。
    """
    key = seed_group_tier_key(tier)
    if key == SEED_GROUP_ALL_TIER:
        return 0          # 0 = 动态：由资源池总量决定（调用方自己算）
    if key == SEED_GROUP_CUSTOM_TIER:
        value = int(round(parse_size_gib(custom_gib)))
        # 只要求「大于 0」；上限 1 PiB 只是防手滑
        return max(SEED_GROUP_CUSTOM_MIN_GIB, min(value, SEED_GROUP_CUSTOM_MAX_GIB))
    return SEED_GROUP_TIERS[key]


def _seed_group_key(item: Dict[str, Any]) -> str:
    """条目唯一键：torrent_id 优先（0 也是合法值），否则退回 info_hash。"""
    if not isinstance(item, dict):
        return ""
    tid = item.get("torrent_id")
    if tid is not None and str(tid).strip() != "":
        return str(tid).strip()
    return str(item.get("info_hash") or "").strip()


def _as_float(value: Any) -> float:
    try:
        return max(0.0, float(str(value).strip()))
    except Exception:
        return 0.0


def _site_proxy(proxy: Any, auth: Any) -> str:
    """把 IYUU 的「代理服务器 + 代理验证」拼成插件认的 proxy URL。

    IYUU 存的是 `192.168.1.2:8080` 与 `user:pass`；插件需要 `http://user:pass@host:port`。
    已经是完整 URL 的原样保留。
    """
    host = str(proxy or "").strip()
    if not host:
        return ""
    if "://" not in host:
        host = "http://" + host
    secret = str(auth or "").strip()
    if secret and "@" not in host:
        head, _, tail = host.partition("://")
        host = f"{head}://{secret}@{tail}"
    return host


def _disabled(item: Dict[str, Any]) -> bool:
    """IYUU 的 disabled 有 0/1 也有 "0"/"false" 字符串，统一按真值语义判断。"""
    value = item.get("disabled")
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


def _as_int(value: Any) -> int:
    """sid / uid 这类字段可能是 "abc"：一律降级成 0，别让一行脏数据炸掉整次解析。"""
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def _seed_group_size_gib(size: Any) -> float:
    """站点返回的 size 是字节；也容忍已经是 GiB 的数字（< 1e6 当 GiB 看）。"""
    try:
        value = float(size or 0)
    except Exception:
        return 0.0
    if value <= 0:
        return 0.0
    # 1e6 太低：800 KB 的种子（800000 字节）会被算成 800000 GiB，直接顶穿 target。
    return value if value < 1e5 else value / GIB


# 「全站官种」档位的达标线：只要覆盖到目标的 60% 就算达标（老板 2026-11-18 定）。
# 为什么单独给 all 档放宽：全站官种的目标 = 站点所有官方种的总体积，它每天都在涨
# （站点持续给老种补官方标记），要求 100% 等于永远追不上；而 60% 已经能保证
# 「站点上大部分官方资源有源」，考核意义足够。**其他档位仍是 100%**。
SEED_GROUP_ALL_TARGET_RATIO = 0.6


def _sg_on_target(covered: Any, target: Any, tolerance: float = 0.005,
                  ratio: float = 1.0) -> bool:
    """覆盖是否算「达标」——站点据此累计当月保种时长。

    `ratio` 是达标线：默认 1.0（必须保满目标），`all` 档传
    [`SEED_GROUP_ALL_TARGET_RATIO`]（60%）。

    留 0.5%（最少 1 GiB）容差：covered 来自客户端 size 累加，target 来自站点 size_bytes/GIB，
    两者天然差几个 MB；严格 >= 会让「其实已全保完」的账号一直报 0。
    """
    try:
        c = float(covered or 0)
        t = float(target or 0)
    except (TypeError, ValueError):
        return False
    if c <= 0 or t <= 0:
        return False
    need = t * max(0.0, min(float(ratio), 1.0))
    return c >= need - max(need * tolerance, 1.0)


def seed_group_plan(items: List[Dict[str, Any]], target_gib: float) \
        -> Tuple[List[Dict[str, Any]], float, float]:
    """按档位挑种子：只超不少。

    规则：
      1. 官方标记排前面，同优先按体积从大到小（条数少、覆盖快）；
      2. 若有一颗能一次性盖住剩余空间，就挑其中**最小**的那颗（超额最少）——
         体积只超不少，但也不能为了凑数白塞几百 G；
      3. 否则在放得下的里面挑最大的（best-fit decreasing），继续填；
      4. 池子耗尽仍未到档位：返回缺口，调用方必须把缺口报出来，不能假装达标。

    :return: (挑中的条目, 累计体积 GiB, 缺口 GiB)
    """
    pool: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        size = _seed_group_size_gib(item.get("size"))
        if size <= 0:
            continue
        entry = dict(item)
        entry["size_gib"] = size
        pool.append(entry)
    pool.sort(key=lambda x: (0 if x.get("official") else 1, -x["size_gib"],
                             str(x.get("info_hash") or "")))
    picked: List[Dict[str, Any]] = []
    total = 0.0
    remaining = float(target_gib)
    while remaining > 1e-9 and pool:
        # ① 有能一颗盖住剩余空间的：挑其中最小的（超额最少），收工
        cover = [x for x in pool if x["size_gib"] >= remaining - 1e-9]
        if cover:
            choice = min(cover, key=lambda x: (x["size_gib"], 0 if x.get("official") else 1))
            picked.append(choice)
            total += choice["size_gib"]
            break
        # ② 没有能一次性盖住的：在放得下的里面挑最大的（条数少）
        fit = [x for x in pool if x["size_gib"] <= remaining + 1e-9]
        choice = fit[0]          # 排序已保证这是「放得下的里面最大的」
        picked.append(choice)
        pool.remove(choice)
        total += choice["size_gib"]
        remaining = float(target_gib) - total
    shortfall = max(0.0, round(float(target_gib) - total, 3))
    return picked, round(total, 3), shortfall


def _cron_trigger(expr: Any, fallback: str):
    """把 5 段 cron 表达式变成「触发器对象」，永远不把字符串交给 MoviePilot。

    以前写错时会把裸字符串塞进 service["trigger"]，MP 原样交给 APScheduler 的
    add_job(func, trigger) → LookupError → 任务注册失败、任务页看不到该任务。
    现在：表达式合法就用自己的，非法/段数不对就用 fallback（两者都是硬编码合法值）。
    """
    for candidate in (str(expr or "").strip(), fallback):
        if len(candidate.split()) == 5:
            try:
                return CronTrigger.from_crontab(candidate)
            except Exception:
                continue
    return CronTrigger.from_crontab(fallback)


class _HttpResult:
    def __init__(self, status: int, content: bytes, text: str):
        self.status_code = status
        self.content = content
        self.text = text

    def json(self, default=None):
        """解析响应体；空 body / HTML 错误页 / 截断的 JSON 一律返回 default，不抛异常。

        站点被 Cloudflare 挡一下时经常回「200 + 空 body」或者一页 HTML，
        以前这里直接 json.loads，异常会一路冒到 MoviePilot，
        整轮任务就报「执行失败 Expecting value: line 1 column 1 (char 0)」。
        """
        if not (self.text or "").strip():
            return default
        try:
            return json.loads(self.text)
        except Exception as err:
            logger.warn(f"响应不是合法 JSON（{type(err).__name__}），按空处理：{self.text[:120]}")
            return default


def _curl_cmd(method: str, url: str, headers=None, params=None, json_body=None, data=None,
              timeout: int = 20, proxy: str = "") -> Tuple[List[str], str]:
    """拼出 curl 命令（返回 cmd 和最终 URL），不发请求；并发版和单发版共用。"""
    # -L：站点常把下载地址 302 到另一台机器（M-Team 的签名地址就跳一次），requests 默认跟随，
    # curl 不加就得自己处理，漏了会拿到 0 字节。
    cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-X", method]
    if proxy:
        # socks5h 让 DNS 也在代理侧解析（被墙站的域名在本地解析不到时靠它）
        cmd += ["--proxy", proxy]
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    if json_body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(json_body)]
    elif data is not None:
        cmd += ["--data", urlencode(data)]
    for key, value in (headers or {}).items():
        cmd += ["-H", f"{key}: {value}"]
    cmd += ["-w", "\n%{http_code}", url]
    return cmd, url


def _curl(method: str, url: str, headers=None, params=None, json_body=None, data=None,
          timeout: int = 20, proxy: str = "") -> _HttpResult:
    """用系统 curl 发请求。

    为什么不用 requests：部分 NAS/容器镜像里的 python+OpenSSL 组合会被 Cloudflare 拦（握手超时），
    而 curl 同一台机器可以正常访问；插件里对可乐PT 的请求因此走 curl，其它站点仍用 requests。
    """
    cmd, url = _curl_cmd(method, url, headers=headers, params=params, json_body=json_body,
                         data=data, timeout=timeout, proxy=proxy)
    result = _run_curl(cmd)
    if result.status_code == 0:
        # 有些网络里 colapt.com 会被解析到连不通的 Cloudflare 段（实测 188.114.9x 在国内网络
        # 直接被 reset，而 104.21/172.67 段正常）。这时用 --resolve 逐个试已知可用的段，
        # SNI/Host 仍是 www.colapt.com，证书照样校验。
        host = url.split("://", 1)[-1].split("/")[0].split(":")[0]
        for ip in COLA_FALLBACK_IPS:
            retry = list(cmd)
            # 插在 -w 之前：curl 的 -w 必须紧跟它的格式串，插在中间会把 --resolve 当成格式
            retry[-3:-3] = ["--resolve", f"{host}:443:{ip}"]
            again = _run_curl(retry)
            if again.status_code:
                logger.info(f"{host} 直连失败，已改用 {ip} 连接成功")
                result = again
                break
    return result


def _curl_parallel(cmds: List[List[str]], urls: List[str], workers: int = 3) -> List[_HttpResult]:
    """并发跑多条 curl（每条一个子进程，最多 workers 个同时在飞）。

    刻意不用线程池：2026-11 那次 MoviePilot 段错误就是插件里的 ThreadPoolExecutor 引起的，
    之后插件里不再出现线程；这里靠「网络等待交给子进程」实现并发，Python 侧始终是顺序的。
    """
    results: List[Optional[_HttpResult]] = [None] * len(cmds)
    running: List[Tuple[int, Any]] = []
    pending = list(enumerate(zip(cmds, urls)))
    while pending or running:
        while pending and len(running) < max(1, workers):
            index, (cmd, _url) = pending.pop(0)
            try:
                running.append((index, subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                        stderr=subprocess.DEVNULL)))
            except Exception as err:
                logger.warn(f"curl 启动失败：{err}")
                results[index] = _HttpResult(0, b"", "")
        if not running:
            continue
        index, proc = running.pop(0)
        try:
            out = proc.communicate()[0] or b""
        except Exception as err:
            logger.warn(f"curl 执行失败：{err}")
            out = b""
        body, status = out, 0
        if b"\n" in body:
            body, _, tail = body.rpartition(b"\n")
            try:
                status = int(tail.strip() or 0)
            except ValueError:
                status = 0
        results[index] = _HttpResult(status, body, body.decode("utf-8", "replace"))
    return [r or _HttpResult(0, b"", "") for r in results]


def _run_curl(cmd: List[str]) -> _HttpResult:
    proc = subprocess.run(cmd, capture_output=True)
    body = proc.stdout
    status = 0
    if b"\n" in body:
        body, _, tail = body.rpartition(b"\n")
        try:
            status = int(tail.strip() or 0)
        except ValueError:
            status = 0
    return _HttpResult(status, body, body.decode("utf-8", "replace"))


_CURL_ONLY_HOSTS: Dict[str, bool] = {}


def _site_headers(site: Dict[str, Any], accept: str = "") -> Dict[str, str]:
    """站点请求公共头：UA + 可选 Accept + 站点自己的 Cookie（天空/家园这类要登录态的站）。"""
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    cookie = str(site.get("cookie") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _site_request(method: str, url: str, **kw) -> _HttpResult:
    """站点的 HTTP 请求：先试 requests，失败再走系统 curl。

    真实原因（2026-11-01 在老板 NAS 上实测）：这些站大多在 Cloudflare 后面，这台机器只有 IPv6 能通，
    Python 的 requests 先试 IPv4 就超时/重置；curl 自带回落，同一台机器上 curl 302、requests 全挂。
    所以站点侧请求统一走这一层，谁通算谁的。
    """
    timeout = kw.pop("timeout", REQUEST_TIMEOUT)
    proxy = kw.pop("proxy", "") or ""
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    host = url.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
    if not _CURL_ONLY_HOSTS.get(host):
        try:
            rsp = requests.request(method, url, timeout=timeout, **kw)
            return _HttpResult(rsp.status_code, rsp.content, rsp.text)
        except Exception as err:
            logger.info(f"{host} 的 requests 不可用（{type(err).__name__}），之后都走 curl")
            _CURL_ONLY_HOSTS[host] = True
    res = _curl(method, url, params=kw.get("params"), json_body=kw.get("json"),
                data=kw.get("data"), headers=kw.get("headers"), timeout=timeout)
    if (res.status_code == 0 and not res.content) and _CURL_ONLY_HOSTS.get(host):
        _CURL_ONLY_HOSTS[host] = False  # 这次也失败，下次重新试 requests
    return res


def _valid_torrent(data: bytes) -> bool:
    """站点返回登录页/错误页时也常是 200，交给 qB 会得到 415。这里先做一次 bencode 校验。"""
    try:
        torrent = bdecode(data)
        return isinstance(torrent, dict) and isinstance(torrent.get("info"), dict)
    except Exception:
        return False


def _decode_torrent_pieces(data: bytes) -> str:
    """返回 info.pieces 的 SHA1（NexusPHP /api/pieces-hash 用的 pieces_hash）。"""
    try:
        torrent = bdecode(data)
        pieces = torrent["info"]["pieces"]
        if isinstance(pieces, str):
            pieces = pieces.encode("utf-8", "surrogateescape")
        return hashlib.sha1(pieces).hexdigest()
    except Exception:
        return ""


def _bencode_skip(buf: bytes, pos: int) -> int:
    """跳过 bencode 的一个值，返回它结束后的位置（找原始 info 字典用）。"""
    ch = buf[pos:pos + 1]
    if ch == b"i":
        return buf.index(b"e", pos) + 1
    if ch in (b"l", b"d"):
        pos += 1
        while buf[pos:pos + 1] != b"e":
            if ch == b"d":
                pos = _bencode_skip(buf, pos)   # 键
            pos = _bencode_skip(buf, pos)       # 值
        return pos + 1
    colon = buf.index(b":", pos)
    return colon + 1 + int(buf[pos:colon])


def _torrent_info_hash(data: bytes) -> str:
    """算 .torrent 的 info_hash：必须对 info 字典的**原始字节切片**取 SHA1。

    重新 bencode 一遍会因字典顺序变化得到不同的哈希，所以这里只定位、不重建。
    用途：qB 回 "Fails." 时区分「客户端里已经有这颗」和「种子本身无效」。
    """
    try:
        if data[:1] != b"d":
            return ""
        pos = 1
        while data[pos:pos + 1] != b"e":
            kstart = pos
            pos = _bencode_skip(data, pos)
            key = data[kstart:pos]
            vstart = pos
            pos = _bencode_skip(data, pos)
            if key == b"4:info":
                return hashlib.sha1(data[vstart:pos]).hexdigest()
    except Exception:
        return ""
    return ""


def _torrent_announces(data: bytes) -> List[str]:
    """取 .torrent 里的 HTTP(S) announce 地址（加 tracker 用；udp 的没意义）。"""
    try:
        torrent = bdecode(data)
    except Exception:
        return []
    if not isinstance(torrent, dict):
        return []

    def text(value: Any) -> str:
        return value.decode("utf-8", "ignore") if isinstance(value, (bytes, bytearray)) else str(value)

    urls: List[str] = []
    tiers = torrent.get("announce-list") or torrent.get(b"announce-list") or []
    if isinstance(tiers, (list, tuple)):
        for tier in tiers:
            for url in (tier if isinstance(tier, (list, tuple)) else [tier]):
                urls.append(text(url))
    single = torrent.get("announce") or torrent.get(b"announce") or ""
    if single:
        urls.insert(0, text(single))
    out: List[str] = []
    for url in urls:
        url = (url or "").strip()
        if url.startswith("http") and url not in out:
            out.append(url)
    return out


def _bencode_value(value: Any) -> bytes:
    """规范 bencode 编码（字典按字节序排序），用来算「改写 info 后」的 info_hash。"""
    if isinstance(value, bool):
        raise TypeError("bencode 没有 bool")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, (bytes, bytearray)):
        return b"%d:%s" % (len(value), bytes(value))
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(_bencode_value(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = []
        for key, val in value.items():
            key = key.encode() if isinstance(key, str) else key
            items.append((key, val))
        items.sort(key=lambda kv: kv[0])
        return b"d" + b"".join(_bencode_value(k) + _bencode_value(v) for k, v in items) + b"e"
    raise TypeError(f"不支持的 bencode 类型：{type(value).__name__}")


def _torrent_info_hash_variants(data: bytes) -> List[str]:
    """同一份文件在不同站发布时，info 里往往只差 `source` 一个字段。

    可乐PT 发布时会把 info.source 盖成 `[https://www.colapt.com] ColaPT`；同一颗种子在
    M-Team 是 `[kp.m-team.cc] M-Team - TP` —— 于是两边的 info_hash 必然不同，而
    /api/v1/plugin/match 只按 info_hash / source_info_hash 反查，结果就是「内容一模一样
    却匹配不到」。这里把 source 换成可乐的章 / 直接去掉，各算一遍 info_hash，再让站点反查。
    """
    try:
        torrent = bdecode(data)
    except Exception:
        return []
    if not isinstance(torrent, dict):
        return []
    info = torrent.get(b"info")
    if not isinstance(info, dict):
        info = torrent.get("info")
    if not isinstance(info, dict):
        return []
    out: List[str] = []
    for source in (COLA_SOURCE_TAG, None):
        clone = dict(info)
        clone.pop("source", None)
        if source is None:
            clone.pop(b"source", None)
        else:
            clone[b"source"] = source.encode()
        try:
            digest = hashlib.sha1(_bencode_value(clone)).hexdigest()
        except Exception:
            continue
        if digest not in out:
            out.append(digest)
    return out


def _content_key(files: List[Tuple[str, int]]) -> str:
    """文件清单指纹：相对路径 + 字节数，按路径排序后取 SHA1（与站点侧同一规范）。

    用途：跨站辅种按 info_hash 反查会漏 —— 每个站发布时都会把 info.source 盖成自己的章，
    同一份文件在各站的 info_hash 必然不同。文件清单指纹只看「路径 + 字节数」，
    跟谁重新打包无关，所以能跨站对上（先按内容键找候选，下载后再逐字节核对文件清单）。
    """
    items = sorted((str(path), int(size)) for path, size in files if path)
    if not items:
        return ""
    raw = "".join(f"{path}\x00{size}\n" for path, size in items)
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()


class _Client:
    """下载器的最小接口：列出已完成种子、添加种子。"""

    name = ""

    def list_completed(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def inflight(self) -> int:
        """客户端里「正在下载」的种子数（未完成）。

        保种组用它做进种限流：满了就不再往客户端塞新种，等这一批下完再补。
        不知道时返回 -1 —— 调用方据此「不限制」，绝不因为读不到就把保种卡死。
        """
        return -1

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "",
            hash_hint: str = "", category: str = "", auto_tmm: bool = False) -> str:
        """添加一颗种子，返回 "ok" / "dup"（客户端里已经有了）/ "fail"。

        category / auto_tmm 是原种子的分类与自动管理状态：qB 开着「自动种子管理」时
        下载路径由分类决定，辅种必须沿用原种子的分类，否则新种子会被放到别的目录、
        校验全部失败（issue #23）。
        """
        raise NotImplementedError

    def inflight(self) -> int:
        """MP 通道：数进度没满的种子（0.10.57 之前落基类返回 -1，限流完全失效）。"""
        if self._list_all is None:
            return -1
        try:
            rows = self._list_all() or []
        except Exception:
            return -1
        n = 0
        for t in rows:
            try:
                if float(t.get("progress") or 0) < 1:
                    n += 1
            except (TypeError, ValueError):
                continue
        return n

    def delete(self, hashes: List[str], delete_files: bool = False) -> bool:
        """MP 通道删除（0.10.57 之前恒 False，下架清理一直空转）。"""
        if not self._delete or not hashes:
            return False
        ok_all = True
        for h in hashes:
            try:
                try:
                    self._delete(h, delete_files)
                except TypeError:
                    self._delete(h)
            except Exception as err:
                logger.warn(f"{self.name} 删除失败 {h[:12]}：{err}")
                ok_all = False
        return ok_all

    def inflight(self) -> int:
        """数「进度没满」的种子。之前落基类返回 -1（=不限制），进种限流对 MP 模式完全失效。"""
        if self._list_all is None:
            return -1
        try:
            rows = self._list_all() or []
        except Exception:
            return -1
        n = 0
        for t in rows:
            try:
                if float(t.get("progress") or 0) < 1:
                    n += 1
            except (TypeError, ValueError):
                continue
        return n

    def delete(self, hashes: List[str], delete_files: bool = False) -> bool:
        """删除。之前落基类恒 False —— 下架清理在 MP 模式下一直空转。"""
        if not self._delete or not hashes:
            return False
        ok_all = True
        for h in hashes:
            try:
                try:
                    self._delete(h, delete_files)
                except TypeError:
                    self._delete(h)
            except Exception as err:
                logger.warn(f"{self.name} 删除失败 {h[:12]}：{err}")
                ok_all = False
        return ok_all

    def add_tags(self, hashes: List[str], tags: List[str]) -> bool:
        """补标签。MP 各版本方法名不统一，只认构造方注入的；没有就降级 False。"""
        if not hashes or not tags or not self._tag:
            return False
        try:
            self._tag(hashes, ",".join(tags))
            return True
        except Exception as err:
            logger.warn(f"{self.name} 补标签失败：{err}")
            return False

    def recheck(self, hashes: List[str]) -> int:
        """重新校验。同上，注入才生效。"""
        if not hashes or not self._recheck:
            return 0
        try:
            self._recheck(hashes)
            return len(hashes)
        except Exception as err:
            logger.warn(f"{self.name} 重新校验失败：{err}")
            return 0

    def has(self, info_hash: str) -> bool:
        """客户端里是否已有这个 info_hash；不知道时返回 False。"""
        return False

    def add_trackers(self, info_hash: str, urls: List[str]) -> bool:
        """给客户端里**已有**的种子补 announce（同 info_hash 的跨站辅种只能这么干）。"""
        return False

    def export(self, info_hash: str) -> bytes:
        """导出客户端里的 .torrent 原始字节；不支持时返回空。"""
        return b""

    def files(self, info_hash: str) -> List[Dict[str, Any]]:
        """客户端里这颗种子的文件清单（相对保存路径）；不支持时返回空。"""
        return []

    def tagged_missing(self, tag: str) -> List[str]:
        """返回带该标签、但文件缺失（missingFiles）的种子 hash，用于自愈清理。"""
        return []

    def delete(self, hashes: List[str], delete_files: bool = False) -> bool:
        """从客户端移除种子（默认不删文件）。"""
        return False

    def recheck(self, hashes: List[str]) -> int:
        """让客户端对这些种子「强制重新校验」，返回提交成功的条数。

        用途：「跳过校验」时代加进来的保种种子，文件其实不在磁盘上，qB 停在 missingFiles、
        进度 0、永不下载。重新校验后它会发现数据不完整，自动进入下载。
        """
        return 0

    def tags_for(self, hashes: List[str]) -> Dict[str, str]:
        """返回 {info_hash: "tag1, tag2"}；用于自愈删除前把用户/MP 打的标签记下来。"""
        return {}

    def add_tags(self, info_hash: str, tags: str) -> bool:
        """给已有种子追加标签（不会覆盖原有标签）。"""
        return False

    def close(self) -> None:
        pass


class QBittorrentClient(_Client):
    def __init__(self, name: str, url: str, username: str = "", password: str = ""):
        self.name = name
        self._session = requests.Session()
        self.url = (url or "").rstrip("/")
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        try:
            rsp = self._session.post(
                f"{self.url}/api/v2/auth/login",
                data={"username": username, "password": password},
                timeout=REQUEST_TIMEOUT,
            )
            # qB 4.x 回 "Ok."，5.x 回 204 No Content（body 为空）：两种都算成功
            if rsp.status_code in (200, 204) or "Ok" in (rsp.text or ""):
                return
            logger.warn(f"qBittorrent ({self.name}) 登录失败：{rsp.status_code} {rsp.text[:80]}")
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 连接失败：{err}")

    def get_speed_limits(self) -> Dict[str, Any]:
        """读取客户端当前的限速设置（不管插件有没有配）。

        返回 {dl_kib, up_kib, dl_enabled, up_enabled}；不支持/读不到返回 {}。
        """
        return {}

    def get_speed_limits(self) -> Dict[str, Any]:
        """qB：preferences 里的 dl_limit / up_limit 是字节/秒，0 = 不限。"""
        try:
            rsp = self._session.get(f"{self.url}/api/v2/app/preferences", timeout=REQUEST_TIMEOUT)
            if rsp.status_code != 200:
                logger.warn(f"qBittorrent ({self.name}) 读限速返回 {rsp.status_code}")
                return {}
            prefs = rsp.json() or {}
            dl = int(prefs.get("dl_limit") or 0)
            up = int(prefs.get("up_limit") or 0)
            return {"dl_kib": dl // 1024, "up_kib": up // 1024,
                    "dl_enabled": dl > 0, "up_enabled": up > 0, "raw": {"dl": dl, "up": up}}
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 读限速异常：{err}")
            return {}

    def set_speed_limits(self, dl_kib: int, up_kib: int) -> bool:
        """qB：app/setPreferences 的 dl_limit / up_limit，单位是 **字节/秒**，0 = 不限。"""
        payload: Dict[str, Any] = {}
        if dl_kib > 0:
            payload["dl_limit"] = int(dl_kib) * 1024
        if up_kib > 0:
            payload["up_limit"] = int(up_kib) * 1024
        if not payload:
            return False
        try:
            rsp = self._session.post(
                f"{self.url}/api/v2/app/setPreferences",
                data={"json": json.dumps(payload)},
                timeout=REQUEST_TIMEOUT,
            )
            if rsp.status_code == 200:
                return True
            logger.warn(f"qBittorrent ({self.name}) 设置限速返回 {rsp.status_code}")
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 设置限速异常：{err}")
        return False

    def inflight(self) -> int:
        try:
            rsp = self._session.get(
                f"{self.url}/api/v2/torrents/info",
                params={"filter": "downloading"},
                timeout=REQUEST_TIMEOUT,
            )
            rsp.raise_for_status()
            return len(rsp.json() or [])
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 读取下载队列失败：{err}")
            return -1

    # 一次拉多少个：qB 的 `torrents/info` 一页 10 条约 20 KB。
    #
    # 为什么是 10 而不是 100（2026-11-18 实测）：家里 NAS 到远端 qB 的链路
    # 上行只有 ~172 KiB/s 且夹丢包。实测每页 10 条（20 KB）**零失败**、每页 10 秒，
    # 拉完 700 条约 12 分钟；而每页 100 条（200 KB）要 25-30 秒，正好卡在超时上，
    # 表现是返回空数组、覆盖算成 0.0 GiB —— 看起来像"qB 挂了"，其实 qB 完全正常
    # （同机 `torrents/info` 无 filter 能返回 700 条）。
    #
    # 慢是慢，但**能传完**。一轮 12 分钟对每小时跑一次完全可接受 —— 没必要为了
    # 快一点把超时设短、把结果丢光。
    # 一页拿 500 条：907 颗种子只要 2 次请求，而不是 91 次。
    # 这是远端下载器最关键的一个参数（2026-11-20）：MP 装在 NAS、qB 在别人机上时，
    # 那条链路可能 50% 丢包，91 次分页全部成功的概率约等于 0，于是「读不到种子」成了常态。
    QB_LIST_PAGE = 500
    QB_LIST_RETRY = 2          # 单页失败重试次数
    # 列表请求用短超时：它是每分钟一轮的常规动作，不能占着 90 秒让两轮叠在一起。
    # 45 秒在 293ms RTT 的远端链路上足够读完 500 条；真读不完就重试，再不行就沿用上次读数。
    QB_LIST_TIMEOUT = 45

    def _list_page(self, page: int) -> List[Dict[str, Any]]:
        """取一页 completed 种子，网络类失败就地重试。

        拿不到就**抛异常**，绝不返回空列表 —— 返回空会被 `_sg_current` 当成
        「读成功、客户端里一颗都没有」，直接把覆盖清零（生产上覆盖 1543 GiB 变 0
        就是这么来的）。抛出去，上层会沿用上一次成功读数。
        """
        last: Optional[Exception] = None
        for attempt in range(self.QB_LIST_RETRY + 1):
            try:
                rsp = self._session.get(
                    f"{self.url}/api/v2/torrents/info",
                    params={
                        "filter": "completed",
                        "limit": self.QB_LIST_PAGE,
                        "offset": page * self.QB_LIST_PAGE,
                    },
                    timeout=self.QB_LIST_TIMEOUT,
                )
                rsp.raise_for_status()
                return rsp.json() or []
            except Exception as err:
                last = err
                if attempt >= self.QB_LIST_RETRY:
                    break
                delay = 2 ** (attempt + 1)
                logger.info(f"qBittorrent ({self.name}) 第 {page + 1} 页读取失败，{delay}s 后重试：{err}")
                time.sleep(delay)
        raise RuntimeError(f"qBittorrent ({self.name}) 种子列表读取失败：{last}")

    def list_completed(self) -> List[Dict[str, Any]]:
        try:
            out: List[Dict[str, Any]] = []
            for page in range(0, 40):           # 40 页 × 500 = 2 万条，够任何人的库
                batch = self._list_page(page)
                if not batch:
                    break
                for item in batch:
                    out.append({
                        "hash": (item.get("hash") or "").lower(),
                        "name": item.get("name") or "",
                        "save_path": item.get("save_path") or item.get("content_path") or "",
                        "category": item.get("category") or "",
                        "size": item.get("size") or item.get("total_size") or 0,
                        # 标签要带上：辅种时可能要「继承原种子标签」（issue #11）
                        "tags": item.get("tags") or "",
                        # 状态：同一颗种子出现在多个下载器时，优先在「正在做种」的那个里辅
                        "state": item.get("state") or "",
                        # 自动种子管理：开着时下载路径由分类决定，辅种要沿用同一套（issue #23）
                        "auto_tmm": bool(item.get("auto_tmm")),
                        })
                # 不满一页 = 已经是最后一页，不必再请求
                if len(batch) < self.QB_LIST_PAGE:
                    break
            return out
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 读取种子失败：{err}")
            # 不吞异常：让 _sg_current 走「沿用上次读数」，而不是把它当成空客户端。
            raise

    def recheck(self, hashes: List[str]) -> int:
        if not hashes:
            return 0
        try:
            rsp = self._session.post(
                f"{self.url}/api/v2/torrents/recheck",
                data={"hashes": "|".join(hashes)},
                timeout=REQUEST_TIMEOUT,
            )
            rsp.raise_for_status()
            return len(hashes)
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 强制校验失败：{err}")
            return 0

    def has(self, info_hash: str) -> bool:
        if not info_hash:
            return False
        try:
            rsp = self._session.get(
                f"{self.url}/api/v2/torrents/info",
                params={"hashes": info_hash.lower()},
                timeout=REQUEST_TIMEOUT,
            )
            if rsp.status_code == 200:
                return bool(rsp.json())
        except Exception:
            pass
        return False

    def export(self, info_hash: str) -> bytes:
        """导出 .torrent 原始字节（qB 4.5+）。用来算「换掉 source 后」的 info_hash。"""
        if not info_hash:
            return b""
        try:
            rsp = self._session.get(f"{self.url}/api/v2/torrents/export",
                                    params={"hash": info_hash.lower()},
                                    timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200 and _valid_torrent(rsp.content):
                return rsp.content
            logger.info(f"qBittorrent ({self.name}) 导出种子失败：HTTP {rsp.status_code}")
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 导出种子异常：{err}")
        return b""

    def files(self, info_hash: str) -> List[Dict[str, Any]]:
        """qB 的文件清单：name 是相对保存路径（多文件种子带顶层目录名）。"""
        if not info_hash:
            return []
        try:
            rsp = self._session.get(f"{self.url}/api/v2/torrents/files",
                                    params={"hash": info_hash.lower()},
                                    timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200:
                return rsp.json() or []
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 读取文件清单失败：{err}")
        return []

    def add_trackers(self, info_hash: str, urls: List[str]) -> bool:
        """同一 info_hash 已经在本机做种时，唯一能「再补一个站」的办法是补 tracker。

        qB 不允许两颗种子共用一个 info_hash，所以跨站同种复制只能给已有种子加 announce；
        加完它就会去新站汇报，站方看到 passkey 有效 + 文件完整，直接记做种。
        """
        if not info_hash or not urls:
            return False
        try:
            rsp = self._session.post(
                f"{self.url}/api/v2/torrents/addTrackers",
                data={"hash": info_hash.lower(), "urls": "\n".join(urls)},
                timeout=REQUEST_TIMEOUT,
            )
            if rsp.status_code == 200:
                return True
            logger.warn(f"qBittorrent ({self.name}) 补 tracker 返回 {rsp.status_code}")
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 补 tracker 异常：{err}")
        return False

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "",
            hash_hint: str = "", category: str = "", auto_tmm: bool = False) -> str:
        data = {
            "savepath": save_path,
            "skip_checking": "true" if skip_check else "false",
            "paused": "false",
            # 原种子走自动管理时照样交给 qB 管：路径由分类决定，正好和原种子落在同一目录。
            "autoTMM": "true" if auto_tmm else "false",
        }
        if category:
            # 分类是分类、标签是标签：以前把分类塞进 tags，既脏了标签又让路径对不上（issue #23）。
            data["category"] = category
        # 强制标签：辅种一律带 MANDATORY_TAG，方便在 qB 里一眼筛出来/批量清理；
        # 用户自己填的 add_label 作为第二个标签附上（不填就只带强制标签）。
        if label == SEED_GROUP_TAG:
            # 保种组的种子只带自己的标签：套上「可乐辅种」会让 qB 里分不清谁加的
            tags = [SEED_GROUP_TAG]
        else:
            tags = [MANDATORY_TAG] + ([label] if label and label != MANDATORY_TAG else [])
        data["tags"] = ",".join(tags)
        try:
            rsp = self._session.post(
                f"{self.url}/api/v2/torrents/add",
                files={"torrents": ("crossseed.torrent", torrent)},
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
            body = (rsp.text or "").strip()
            if rsp.status_code == 200 and body.startswith("Ok"):
                return "ok"
            # qB 对重复种子和坏种子都回 "Fails."：只有确认客户端里已经有它，才算重复。
            if body.startswith("Fails"):
                if hash_hint and self.has(hash_hint):
                    return "dup"
                logger.warn(f"qBittorrent ({self.name}) 拒绝种子（重复或无效）：{hash_hint[:12] or '未知 hash'}")
                return "fail"
            logger.warn(f"qBittorrent ({self.name}) 添加种子失败：{rsp.status_code} {body[:120]}")
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 添加种子异常：{err}")
        return "fail"


    def tagged_missing(self, tag: str) -> List[str]:
        try:
            rsp = self._session.get(f"{self.url}/api/v2/torrents/info", params={"tag": tag},
                                    timeout=REQUEST_TIMEOUT)
            if rsp.status_code != 200:
                return []
            return [x.get("hash", "") for x in (rsp.json() or []) if x.get("state") == "missingFiles"]
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 查询缺失种子失败：{err}")
            return []

    def tags_for(self, hashes: List[str]) -> Dict[str, str]:
        if not hashes:
            return {}
        try:
            rsp = self._session.get(f"{self.url}/api/v2/torrents/info",
                                    params={"hashes": "|".join(hashes)},
                                    timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200:
                return {str(t.get("hash", "")).lower(): str(t.get("tags") or "")
                        for t in (rsp.json() or []) if t.get("hash")}
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 读取标签失败：{err}")
        return {}

    def add_tags(self, info_hash: str, tags: str) -> bool:
        if not info_hash or not tags:
            return False
        try:
            rsp = self._session.post(f"{self.url}/api/v2/torrents/addTags",
                                     data={"hashes": info_hash.lower(), "tags": tags},
                                     timeout=REQUEST_TIMEOUT)
            return rsp.status_code == 200
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 追加标签失败：{err}")
            return False

    def delete(self, hashes: List[str], delete_files: bool = False) -> bool:
        if not hashes:
            return False
        try:
            rsp = self._session.post(f"{self.url}/api/v2/torrents/delete",
                                     data={"hashes": "|".join(hashes),
                                           "deleteFiles": "true" if delete_files else "false"},
                                     timeout=REQUEST_TIMEOUT)
            return rsp.status_code == 200
        except Exception as err:
            logger.warn(f"qBittorrent ({self.name}) 删除种子失败：{err}")
            return False


class TransmissionClient(_Client):
    def __init__(self, name: str, url: str, username: str = "", password: str = ""):
        self.name = name
        self.url = (url or "").rstrip("/")
        if not self.url.endswith("/transmission/rpc"):
            self.url = f"{self.url}/transmission/rpc"
        self._session = requests.Session()
        if username:
            self._session.auth = (username, password)
        self._session_id = ""

    def get_speed_limits(self) -> Dict[str, Any]:
        """TR：session-get 的 speed-limit-down/up（KB/s）+ 各自的 enabled。"""
        out = self._rpc({"method": "session-get", "arguments": {}})
        if not out:
            return {}
        args = out.get("arguments") or {}
        dl = int(args.get("speed-limit-down") or 0)
        up = int(args.get("speed-limit-up") or 0)
        return {"dl_kib": dl, "up_kib": up,
                "dl_enabled": bool(args.get("speed-limit-down-enabled")),
                "up_enabled": bool(args.get("speed-limit-up-enabled"))}

    def set_speed_limits(self, dl_kib: int, up_kib: int) -> bool:
        """TR：session-set 的 speed-limit-*，单位就是 KB/s，另需 *_enabled=true。"""
        arguments: Dict[str, Any] = {}
        if dl_kib > 0:
            arguments["speed-limit-down"] = int(dl_kib)
            arguments["speed-limit-down-enabled"] = True
        if up_kib > 0:
            arguments["speed-limit-up"] = int(up_kib)
            arguments["speed-limit-up-enabled"] = True
        if not arguments:
            return False
        out = self._rpc({"method": "session-set", "arguments": arguments})
        return bool(out) and str(out.get("result") or "") == "success"

    def _rpc(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for _ in range(2):
            try:
                headers = {}
                if self._session_id:
                    headers["X-Transmission-Session-Id"] = self._session_id
                rsp = self._session.post(self.url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
                if rsp.status_code == 409:
                    self._session_id = rsp.headers.get("X-Transmission-Session-Id", "")
                    continue
                rsp.raise_for_status()
                return rsp.json()
            except Exception as err:
                logger.error(f"Transmission ({self.name}) 请求失败：{err}")
                return None
        return None

    def recheck(self, hashes: List[str]) -> int:
        if not hashes:
            return 0
        rsp = self._rpc({"method": "torrent-verify", "arguments": {"ids": hashes}})
        return len(hashes) if rsp else 0

    def inflight(self) -> int:
        rsp = self._rpc({
            "method": "torrent-get",
            "arguments": {"fields": ["hashString", "percentDone"]},
        })
        if not rsp:
            return -1
        return sum(1 for item in (rsp.get("arguments") or {}).get("torrents") or []
                   if (item.get("percentDone") or 0) < 1)

    def list_completed(self) -> List[Dict[str, Any]]:
        rsp = self._rpc({
            "method": "torrent-get",
            "arguments": {"fields": ["hashString", "name", "downloadDir", "labels",
                                     "percentDone", "totalSize", "status"]},
        })
        if not rsp:
            # 和 qB 那条一样：拿不到就抛，别返回空列表冒充「客户端里没有种子」。
            raise RuntimeError(f"Transmission ({self.name}) 种子列表读取失败")
        out = []
        for item in (rsp.get("arguments") or {}).get("torrents") or []:
            if item.get("percentDone") != 1:
                continue
            status = item.get("status")
            out.append({
                "hash": (item.get("hashString") or "").lower(),
                "name": item.get("name") or "",
                "save_path": item.get("downloadDir") or "",
                "category": ",".join(item.get("labels") or []),
                "size": item.get("totalSize") or 0,
                "tags": ",".join(item.get("labels") or []),
                # Transmission: status 6 = seeding（见 rpc-spec）
                "state": "uploading" if status == 6 else str(status or ""),
            })
        return out

    def has(self, info_hash: str) -> bool:
        if not info_hash:
            return False
        rsp = self._rpc({"method": "torrent-get", "arguments": {"ids": [info_hash.lower()], "fields": ["hashString"]}})
        if not rsp:
            return False
        return bool(((rsp.get("arguments") or {}).get("torrents")) or [])

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "",
            hash_hint: str = "", category: str = "", auto_tmm: bool = False) -> str:
        # Transmission 没有「跳过校验」参数：torrent-add 后由客户端自己校验，
        # 所以这里的 skip_check 只是语义占位，实际等价于总是校验。
        args = {
            "metainfo": base64.b64encode(torrent).decode(),
            "paused": False,
            "download-dir": save_path,
        }
        args["labels"] = [MANDATORY_TAG] + ([label] if label and label != MANDATORY_TAG else [])
        rsp = self._rpc({"method": "torrent-add", "arguments": args})
        if rsp and (rsp.get("result") == "success"):
            return "ok"
        if rsp and rsp.get("result") == "duplicate torrent":
            return "dup"
        logger.warn(f"Transmission ({self.name}) 添加种子失败：{rsp}")
        return "fail"


class ColaSeedGroup(_PluginBase):
    plugin_name = "可乐保种组"
    plugin_desc = "可乐PT 保种组：把站点官方种按档位搬进下载器长期保种（体积只超不少、达标后动态轮换、下架自动清理、多客户端单独限速），需站点工作组头衔解锁。"
    plugin_icon = "https://www.colapt.com/favicon.ico"
    plugin_version = "0.2.16"
    plugin_author = "ColaPT"
    author_url = "https://github.com/ReelCoreHQ"
    plugin_config_prefix = "colacrossseed_"
    plugin_order = 21
    auth_level = 1

    _enabled = False            # MP 插件总开关：关掉后两个功能都不跑
    _notify = True
    _cron = "0 */1 * * *"
    _username = ""
    _password = ""
    _otp_code = ""
    _captcha_code = ""
    _session_token = ""
    _skip_check = False
    _max_per_hour = 0        # 0 = 不限（老板 2026-11-01：不要全局上限）
    _max_per_site = 20       # 单个站点每轮最多补多少
    _site_fail_limit = 3     # 同一个站在同一轮里失败几次后熔断
    _site_counter: Dict[str, int] = {}
    _site_limits: Dict[str, int] = {}      # 站点 URL -> 本站每轮上限（IYUU 的「辅种数量」）
    _site_intervals: Dict[str, float] = {} # 站点 URL -> 每颗之间的间隔秒（IYUU 的「辅种间隔」）
    _site_fails: Dict[str, int] = {}
    _direction = "both"
    _use_mp_clients = True
    _clients_json = ""
    _clients_lines = ""
    _sites_json = ""
    _sites_lines = ""
    _iyuu_json = ""
    _iyuu_json_file = ""       # 容器内路径，例如 /config/cola-sites.json；填了就以文件为准
    _iyuu_token = ""
    _sites_mode = "merge"
    _add_label = ""
    _target_client = ""       # 辅种加到哪个下载器：留空=自动（优先正在做种的那台）
    _inherit_tags = True      # 辅种时继承原种子已有的标签（站点标签、已整理、订阅…）
    _extra_tags = ""          # 额外附加的标签，逗号分隔
    _fallback_skip_check = True
    _retry_failed = True
    # 限速接管：>0 才接管对应方向，0/空 = 不去动客户端原有设置
    _limit_dl_kib = 0
    _limit_up_kib = 0
    # 每客户端覆盖：每行 `客户端名或地址,下载KiB/s,上传KiB/s`（命中就覆盖全局，未命中用全局）
    _limit_lines = ""
    # ---- 可乐保种组（只有工作组账号能解锁；见 docs/seed-group-api.md）----
    _seedgroup_enabled = False
    _seedgroup_tier = "2T"
    # 保种范围：site = 全站官方种（默认）；pool = 站点保种组专用池（未开放时回落到全站官方种）
    _seedgroup_scope = "site"
    _seedgroup_custom_gib = 2048
    _seedgroup_api_key = ""      # 可选：站点机器面 API Key（X-API-Key，拉官方清单更快）
    _seedgroup_staff_prefix = SEED_GROUP_STAFF_PREFIX
    _seedgroup_allow_admin = True
    # 保种组自己的下载器：默认用 MP 已配置的那台（MP 会按自己的规则刮削/整理），
    # 也可以单独填 qB/TR（只给保种组用，不影响辅种的客户端）
    _seedgroup_client_mode = "mp"      # mp = 用 MP 已配置下载器；custom = 用下面那行
    _seedgroup_use_mp_clients = True
    _seedgroup_clients_lines = ""
    _seedgroup_target_client = ""
    _seedgroup_cron = "0 * * * *"
    # 下架核对的最小间隔：它每颗种子发一次 GET，cron 调到每小时之后仍按这个节奏跑
    SG_PRUNE_MIN_INTERVAL = 6 * 3600
    # 每轮最多对多少颗「丢失文件」的保种种子提交强制校验（recheck 很吃磁盘 IO）
    SG_RECHECK_PER_ROUND = 20
    # 进种限流：客户端里「正在下载」的种子数不允许超过这个值，满了本轮就不再进种。
    # 目的：让覆盖慢慢追平目标，而不是一次性把几百颗塞进客户端抢带宽。
    _seedgroup_max_downloading = 5
    _seedgroup_save_path = ""
    # 保种组默认**必须校验**：这些种子是从站点新下载的，文件还不在磁盘上。
    # 开着「跳过校验」qB 会认为文件已完整，立刻变 missingFiles（丢失文件）、进度 0%、永不下载 ——
    # 2026-11-16 实测就是这条把保种卡成「覆盖不涨」。跳过校验只留给「文件已在磁盘、种子丢了」的恢复。
    _seedgroup_skip_check = False
    # 下架清理：off=不清 / seed=只删种子（默认）/ files=连文件一起删
    # 动态保种：体积达标后，删「做种人数最多」的、补「做种人数最少」的
    _seedgroup_rotate = True      # 强制开启（站长要求）：达到档位后必须轮换，不给关
    _seedgroup_rotate_count = 5
    _seedgroup_rotate_files = True      # 轮换通常要腾空间，默认连文件一起删
    _seedgroup_prune_mode = "seed"
    _seedgroup_prune = True
    _seedgroup_prune_files = False
    # 限速情况上报：只统计「开了保种组」的用户，辅种用户不采集、不上报
    _seedgroup_report = True
    _seedgroup_report_url = "https://www.colapt.com/api/v1/plugin/seed-group/report"
    _seedgroup_report_token = ""
    _event = Event()
    _csrf = ""
    # host -> 可用的 pieces-hash 路径（憨憨是 /nexusapi/pieces-hash，其余多为 /api/pieces-hash）；"" 表示不支持
    _pieces_paths: Dict[str, str] = {}
    _iyuu_rate_limited = False

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config is not None:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", True))
            self._username = str(config.get("username") or "")
            self._password = str(config.get("password") or "")
            self._otp_code = str(config.get("otp_code") or "")
            self._captcha_code = str(config.get("captcha_code") or "")
            self._session_token = str(config.get("session_token") or "")
            self._use_mp_clients = bool(config.get("use_mp_clients", True))
            self._clients_json = str(config.get("clients_json") or "")
            self._add_label = str(config.get("add_label") or "").strip()
            self._inherit_tags = bool(config.get("inherit_tags", True))
            self._extra_tags = str(config.get("extra_tags") or "").strip()
            try:
                self._limit_dl_kib = max(0, min(10 ** 7, int(config.get("limit_dl_kib", 0) or 0)))
                self._limit_up_kib = max(0, min(10 ** 7, int(config.get("limit_up_kib", 0) or 0)))
            except Exception:
                self._limit_dl_kib = 0
                self._limit_up_kib = 0
            self._limit_lines = str(config.get("limit_lines") or "")
            # 可乐保种组：档位只认白名单里的四个，写错退回最小档（不能退化成「不保种」）
            self._seedgroup_enabled = bool(config.get("seedgroup_enabled", False))
            # 档位归一化：老值 20T+ → 20T；未知值退回 2T；custom 走自定义数值
            self._seedgroup_tier = seed_group_tier_key(config.get("seedgroup_tier") or "2T")
            try:
                # 注意不能用 `or 5`：老板要的 0 = 不限流，而 `0 or 5` 会把 0 变成 5，
                # 限流永远关不掉（生产实测 quota 一直被压到 2）。
                raw_limit = config.get("seedgroup_max_downloading")
                self._seedgroup_max_downloading = max(0, min(int(raw_limit if raw_limit is not None else 5), 200))
            except Exception:
                self._seedgroup_max_downloading = 5
            # 范围已从 UI 撤掉：统一「全站官种」；老配置里的 pool 也归一成 site（机器面通道只看有没有 Key）
            self._seedgroup_scope = "site"
            # 支持 100M / 100G / 1T / 1P（parse_size_gib 也接受纯数字）
            custom = int(round(parse_size_gib(config.get("seedgroup_custom_gib"))))
            # 自定义档：100M / 100G / 1T / 1P 都行，只有 0 会被抬到 1 GiB
            self._seedgroup_custom_gib = max(SEED_GROUP_CUSTOM_MIN_GIB, custom)
            # 旧配置键 seedgroup_api_token 的语义就是这把站点 API Key，读进来兼容
            self._seedgroup_api_key = str(config.get("seedgroup_api_key")
                                          or config.get("seedgroup_api_token") or "").strip()
            self._seedgroup_staff_prefix = (str(config.get("seedgroup_staff_prefix") or "").strip()
                                            or SEED_GROUP_STAFF_PREFIX)
            self._seedgroup_allow_admin = bool(config.get("seedgroup_allow_admin", True))
            mode = str(config.get("seedgroup_client_mode") or "").strip().lower()
            if mode in ("mp", "custom"):
                self._seedgroup_client_mode = mode
            else:
                self._seedgroup_client_mode = ("mp" if bool(config.get("seedgroup_use_mp_clients", True))
                                               else "custom")
            self._seedgroup_use_mp_clients = self._seedgroup_client_mode == "mp"
            self._seedgroup_clients_lines = str(config.get("seedgroup_clients_lines") or "")
            self._seedgroup_target_client = str(config.get("seedgroup_target_client") or "").strip()
            self._seedgroup_cron = str(config.get("seedgroup_cron") or "0 * * * *").strip()
            self._seedgroup_save_path = str(config.get("seedgroup_save_path") or "").strip()
            self._seedgroup_skip_check = bool(config.get("seedgroup_skip_check", False))
            # 动态保种强制开启：配置里写 false 也按 true 处理（老板 2026-11-15）
            self._seedgroup_rotate = True
            try:
                self._seedgroup_rotate_count = max(1, min(50, int(config.get("seedgroup_rotate_count", 5) or 5)))
            except Exception:
                self._seedgroup_rotate_count = 5
            self._seedgroup_rotate_files = bool(config.get("seedgroup_rotate_files", True))
            prune_mode = str(config.get("seedgroup_prune_mode") or "").strip().lower()
            if prune_mode not in ("off", "seed", "files"):
                if not bool(config.get("seedgroup_prune", True)):
                    prune_mode = "off"
                elif bool(config.get("seedgroup_prune_files", False)):
                    prune_mode = "files"
                else:
                    prune_mode = "seed"
            self._seedgroup_prune_mode = prune_mode
            self._seedgroup_prune = prune_mode != "off"
            self._seedgroup_prune_files = prune_mode == "files"
            # 限速上报是强制的（老板 2026-11-15）：不给用户开关，也不把地址/token 暴露在界面上
            self._seedgroup_report = True
            self._seedgroup_report_url = str(config.get("seedgroup_report_url")
                                             or SEED_GROUP_REPORT_URL).strip()
            self._seedgroup_report_token = str(config.get("seedgroup_report_token") or "").strip()
            # 启用保种组时立刻校验工作组资格。
            # 注意顺序：这段必须在 max_per_hour / max_per_site 赋值之后 —— 它可能 _persist()，
            # 写在前面会把这两个字段写成类属性默认值（0/20），等于把用户的限速悄悄改成「不限」。
            if self._seedgroup_enabled:
                # 未登录时 identity 会返回 ok=False（不是「没资格」），开关原样保留
                ident = self.seed_group_identity()
                if not ident.get("ok"):
                    # 网络/会话问题不是「没资格」：保留开关，下一轮 _sg_unlocked 还会拦
                    logger.warn(f"保种组资格校验暂时失败（保留开关）：{ident.get('reason')}")
                else:
                    # 2026-11-18：任何站点用户都能保种，只有工作组成员才向站点汇报
                    self.seed_group_unlock(ident)
                    if not ident.get("staff"):
                        logger.info("保种组：当前账号没有工作组头衔 —— 照常拉官种，但不向站点汇报")
        # 保存配置时如果填了验证码，立刻换一次 token（一次性，成功/失败都会清掉验证码字段）。
        if self._captcha_code and self._username and self._password and not self._session_token:
            self._login(self._captcha_code)
            self._captcha_code = ""
            self._persist()
        self._event.clear()
        if self._enabled and self._seedgroup_enabled:
            # 保存后自动检测一次连通性（后台线程，不卡保存）；结果显示在插件页。
            import threading
            threading.Thread(target=self.run_tests, daemon=True).start()

    def _persist(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "username": self._username,
            "password": self._password,
            "otp_code": self._otp_code,
            "captcha_code": "",
            "session_token": self._session_token,
            "use_mp_clients": self._use_mp_clients,
            "clients_json": self._clients_json,
            "add_label": self._add_label,
            "inherit_tags": self._inherit_tags,
            "extra_tags": self._extra_tags,
            "limit_dl_kib": self._limit_dl_kib,
            "limit_up_kib": self._limit_up_kib,
            "limit_lines": self._limit_lines,
            "seedgroup_enabled": self._seedgroup_enabled,
            "seedgroup_tier": self._seedgroup_tier,
            "seedgroup_max_downloading": self._seedgroup_max_downloading,
            "seedgroup_scope": self._seedgroup_scope,
            "seedgroup_custom_gib": self._seedgroup_custom_gib,
            "seedgroup_api_key": self._seedgroup_api_key,
            "seedgroup_staff_prefix": self._seedgroup_staff_prefix,
            "seedgroup_allow_admin": self._seedgroup_allow_admin,
            "seedgroup_use_mp_clients": self._seedgroup_use_mp_clients,
            "seedgroup_clients_lines": self._seedgroup_clients_lines,
            "seedgroup_target_client": self._seedgroup_target_client,
            "seedgroup_cron": self._seedgroup_cron,
            "seedgroup_save_path": self._seedgroup_save_path,
            "seedgroup_skip_check": self._seedgroup_skip_check,
            "seedgroup_client_mode": self._seedgroup_client_mode,
            "seedgroup_rotate": self._seedgroup_rotate,
            "seedgroup_rotate_count": self._seedgroup_rotate_count,
            "seedgroup_rotate_files": self._seedgroup_rotate_files,
            "seedgroup_prune_mode": self._seedgroup_prune_mode,
            "seedgroup_prune": self._seedgroup_prune,
            "seedgroup_prune_files": self._seedgroup_prune_files,
            "seedgroup_report": self._seedgroup_report,
            "seedgroup_report_url": self._seedgroup_report_url,
            "seedgroup_report_token": self._seedgroup_report_token,
        })

    # ---------------- 站点登录 / 匹配 / 下载 ----------------

    def _login(self, captcha_code: str) -> bool:
        captcha_id = self.get_data("captcha_id") or ""
        if not captcha_id:
            logger.warn("可乐PT 登录失败：没有验证码 id，请先打开插件页面获取验证码")
            return False
        payload = {
            "username": self._username,
            "password": self._password,
            "captcha_id": captcha_id,
            "captcha_code": captcha_code,
        }
        if self._otp_code:
            payload["code"] = self._otp_code
        try:
            # 走 _curl 而不是 requests：有些机器（含群晖/部分 NAS）Python 的 TLS 握手过不去，
            # 但系统 curl 正常，登录就不会莫名其妙失败。
            rsp = _curl("POST", f"{COLA_BASE}/api/v1/plugin/sessions", json_body=payload,
                        timeout=REQUEST_TIMEOUT)
        except Exception as err:
            logger.error(f"可乐PT 登录异常：{err}")
            return False
        if rsp.status_code == 201:
            self._session_token = (rsp.json() or {}).get("session_token") or ""
            self.save_data("captcha_id", "")
            self.save_data("captcha_cache", {})
            logger.info("可乐PT 登录成功")
            return bool(self._session_token)
        body = rsp.text or ""
        if rsp.status_code == 403 and "captcha" in body:
            # 验证码错/过期：把缓存清掉，下次打开插件页会换一张新的
            self.save_data("captcha_cache", {})
            self.save_data("captcha_error", "验证码不对或已过期，重新打开插件页会换一张")
            logger.warn("可乐PT 登录失败：验证码不对或已过期，已清掉旧验证码")
        else:
            logger.warn(f"可乐PT 登录失败：{rsp.status_code} {body[:160]}")
        return False

    def _cola(self, method: str, path: str, **kwargs) -> Optional[_HttpResult]:
        headers = kwargs.pop("headers", {})
        headers["Cookie"] = f"reelcore_session={self._session_token}"
        try:
            rsp = _curl(method, f"{COLA_BASE}{path}", headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
        except Exception as err:
            logger.error(f"可乐PT 请求失败 {path}：{err}")
            return None
        if rsp.status_code == 401:
            logger.warn("可乐PT 会话已失效，请重新登录")
            self._session_token = ""
            self._persist()
            self.post_message(mtype=NotificationType.SiteMessage, title="【可乐保种组】需要重新登录",
                              text="会话已失效：请打开插件页面获取新的验证码，在插件设置里填写后保存。")
            return None
        if rsp.status_code != 200:
            logger.warn(f"可乐PT {path} 返回 {rsp.status_code}：{rsp.text[:160]}")
        return rsp
    def _export_content_key(data: bytes) -> str:
        """从 .torrent 算内容键：多文件用 info.files 的相对路径，单文件用 info.name。"""
        try:
            torrent = bdecode(data)
        except Exception:
            return ""
        if not isinstance(torrent, dict):
            return ""
        info = torrent.get(b"info") if isinstance(torrent.get(b"info"), dict) else torrent.get("info")
        if not isinstance(info, dict):
            return ""

        def text(value: Any) -> str:
            return value.decode("utf-8", "surrogateescape") if isinstance(value, (bytes, bytearray)) else str(value)

        name = text(info.get(b"name") or info.get("name") or "")
        entries = info.get(b"files") or info.get("files")
        files: List[Tuple[str, int]] = []
        if isinstance(entries, list) and entries:
            for item in entries:
                if not isinstance(item, dict):
                    continue
                parts = item.get(b"path") or item.get("path") or []
                if not isinstance(parts, list):
                    continue
                path = "/".join(text(p) for p in parts)
                size = item.get(b"length") or item.get("length") or 0
                files.append((path, int(size)))
        elif name:
            size = info.get(b"length") or info.get("length") or 0
            files.append((name, int(size)))
        return _content_key(files)

    @staticmethod
    def _client_content_key(name: str, entries: List[Dict[str, Any]]) -> str:
        """从 qB 文件清单算同一个键：qB 的 name 带顶层目录，去掉它才是相对路径。"""
        files: List[Tuple[str, int]] = []
        for item in entries or []:
            path = str(item.get("name") or "")
            if not path:
                continue
            if name and path.startswith(name + "/"):
                path = path[len(name) + 1:]
            files.append((path, int(item.get("size") or 0)))
        return _content_key(files)
    def _csrf_token(self) -> str:
        """会话登录后拿 CSRF（POST /authorizations 要带）。"""
        if self._csrf:
            return self._csrf
        rsp = self._cola("GET", "/api/v1/me")
        if rsp is None or rsp.status_code != 200:
            return ""
        try:
            self._csrf = ((rsp.json() or {}).get("session") or {}).get("csrf_token") or ""
        except Exception:
            self._csrf = ""
        return self._csrf

    def _download_cola(self, torrent_id: int) -> bytes:
        """站点的下载要两步：先签发 authorization，再带 authorization_id 取字节。"""
        csrf = self._csrf_token()
        if not csrf:
            logger.warn("可乐PT 下载失败：拿不到 CSRF（会话可能已过期）")
            return b""
        try:
            rsp = _curl(
                "POST",
                f"{COLA_BASE}/api/v1/torrents/{torrent_id}/authorizations",
                headers={
                    "Cookie": f"reelcore_session={self._session_token}",
                    "X-CSRF-Token": csrf,
                    "Origin": COLA_BASE,
                    "Idempotency-Key": str(uuid.uuid4()),
                },
                json_body={},
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as err:
            logger.error(f"可乐PT 签发下载授权失败：{err}")
            return b""
        if rsp.status_code != 200:
            logger.warn(f"可乐PT 签发下载授权返回 {rsp.status_code}：{rsp.text[:160]}")
            return b""
        try:
            authorization = int((rsp.json() or {}).get("id") or 0)
        except Exception:
            authorization = 0
        if not authorization:
            logger.warn("可乐PT 下载授权响应缺少 id")
            return b""
        rsp = self._cola("GET", f"/api/v1/torrents/{torrent_id}/download",
                         params={"authorization_id": authorization})
        if rsp is None or rsp.status_code != 200:
            return b""
        return rsp.content if _valid_torrent(rsp.content) else b""
    @staticmethod
    def _split_line(line: str) -> List[str]:
        return [p.strip() for p in re.split(r"[,，]", line.strip()) if p.strip()]

    def _normalise_url(self, raw: str, default_scheme: str = "http") -> str:
        raw = (raw or "").strip().rstrip("/")
        if not raw:
            return ""
        if not raw.startswith("http://") and not raw.startswith("https://"):
            raw = f"{default_scheme}://{raw}"
        return raw

    def _line_clients(self) -> List[_Client]:
        return self._parse_client_lines(self._clients_lines)

    @staticmethod
    def _looks_like_address(token: str) -> bool:
        """这一段像不像地址（host / host:port / http://…）。

        用来在 `_parse_client_lines` 里**不靠位置**找地址 —— 4 段写法里
        「名字在前」还是「名字在后」都有人写，硬按位置切会把用户名当地址
        （2026-11-19 真实事故：`地址,用户,密码,名称` 被切成 name=地址、
        url=用户，报 `Failed to resolve 'a172604'`）。
        """
        t = str(token or "").strip().lower()
        if not t:
            return False
        if t.startswith(("http://", "https://")):
            return True
        if "/" in t or " " in t:
            return False
        # 带端口 —— 一定是地址
        if ":" in t:
            return True
        # 含点（IPv4 / 域名）、或 localhost。
        #
        # **不再接受「纯 ASCII 主机名」**：`a172604` / `qq321321` / `qb1`
        # 这些用户名、密码同样是纯字母数字，会被一起判成地址
        # （2026-11-19 二次事故：解析器把自己写对了、判定规则又放太宽，
        #  结果 url=a172604，还是 `Failed to resolve`）。
        # qB 的 WebUI 地址几乎总带端口，不带端口的纯主机名交给用户写 `http://`。
        return "." in t or t == "localhost"

    def _parse_client_lines(self, text: str) -> List[_Client]:
        """每行一个客户端，**地址按内容识别，不靠位置**：

            `地址,用户名,密码`
            `地址,用户名,密码,名称`          ← 名称在后（最常见）
            `名称,地址,用户名,密码`          ← 名称在前也认
            `qb|tr,地址,用户名,密码[,名称]`   ← 显式指定类型

        4 段以上时看哪一段像地址：谁像地址谁就是地址，剩下的是名字。
        """
        out: List[_Client] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = self._split_line(line)
            if len(parts) < 3:
                logger.warn(f"下载器配置行格式不对（至少 地址,用户名,密码）：{line}")
                continue
            kind = "qb"
            if parts[0].lower() in ("qb", "qbittorrent", "tr", "transmission"):
                kind = "tr" if parts[0].lower() in ("tr", "transmission") else "qb"
                parts = parts[1:]
            if len(parts) < 3:
                logger.warn(f"下载器配置行格式不对：{line}")
                continue
            addr_idx = next((i for i, p in enumerate(parts)
                             if self._looks_like_address(p)), None)
            if addr_idx is None:
                logger.warn(f"下载器配置里找不到地址段：{line}")
                continue
            url = self._normalise_url(parts[addr_idx], "http")
            # 地址**后**紧跟用户名、密码；地址**前**的段是名字（`名称,地址,用户,密码`）
            # 地址后再多出来的第 3 段也是名字（`地址,用户,密码,名称`）
            before = parts[:addr_idx]
            after = parts[addr_idx + 1:]
            if len(after) < 2:
                logger.warn(f"下载器配置缺用户名或密码：{line}")
                continue
            user, password = after[0], after[1]
            name = (before[0] if before
                    else (after[2] if len(after) > 2 else url.split("://", 1)[-1]))
            if kind == "tr":
                out.append(TransmissionClient(name, url, user, password))
            else:
                out.append(QBittorrentClient(name, url, user, password))
        return out

    def _line_sites(self) -> List[Dict[str, str]]:
        """每行一个站点。两种写法都支持，可以混用：

        位置写法：`域名,passkey` / `名称,域名,passkey` / `名称,域名,passkey,rsskey`
        键值写法：`名称,https://域名,passkey=xxx,sid=123,rsskey=yyy,uid=456`

        键值写法的意义：**走 IYUU 匹配必须有 sid**。IYUU 云端只按 sid 回结果，插件要能用
        「sid → 站点」找到下载地址；sid 来自 IYUU 导出的站点 JSON（`"sid": 数字`）。
        没有 sid 的站只能走 pieces-hash / RSS 两条通道。

        rsskey 是站点 RSS 密钥，给没有 pieces-hash 接口的站用（见 _rss_index）。
        识别方式是「像域名的那个 token 是 URL」，剩下两个 32 位十六进制按顺序是 passkey、rsskey，
        所以写 `观众,https://audiences.me,passkey=…,sid=…` 这种键值行也不会把名字当成域名。
        """
        out: List[Dict[str, str]] = []
        url_re = re.compile(r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?$")
        hex32 = re.compile(r"^[0-9a-fA-F]{32}$")
        for line in (self._sites_lines or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = self._split_line(line)
            if len(parts) < 1:
                continue
            keyed: Dict[str, str] = {}
            positional: List[str] = []
            for token in parts:
                if "=" in token:
                    key, _, value = token.partition("=")
                    keyed[key.strip().lower()] = value.strip()
                else:
                    positional.append(token)
            url, name = "", ""
            for i, token in enumerate(positional):
                if url_re.match(token):
                    url = token
                    name = positional[i - 1] if i > 0 else ""
                    rest = positional[i + 1:]
                    break
            else:
                # 没有像域名的 token：按老规矩把第一个当域名，交给 _normalise_url 兜底
                url = positional[0] if positional else ""
                name = ""
                rest = positional[1:]
            passkey = keyed.get("passkey", "")
            rsskey = keyed.get("rsskey", "") or keyed.get("rss", "")
            for token in rest:
                if not hex32.match(token):
                    continue
                if not passkey:
                    passkey = token
                elif not rsskey:
                    rsskey = token
            sid = 0
            for raw_sid in (keyed.get("sid"), keyed.get("iyuu_sid")):
                if raw_sid and str(raw_sid).strip().isdigit():
                    sid = int(str(raw_sid).strip())
                    break
            uid = int(keyed["uid"]) if keyed.get("uid", "").strip().isdigit() else 0
            channel = (keyed.get("channel") or "auto").strip().lower()
            if channel not in ("auto", "pieces", "rss", "iyuu"):
                channel = "auto"
            enabled = (keyed.get("enabled", "1").strip().lower() not in ("0", "false", "no", "off"))
            proxy = (keyed.get("proxy") or "").strip()
            cookie = (keyed.get("cookie") or "").strip()
            rss_url = (keyed.get("rss_url") or "").strip()
            downhash = (keyed.get("downhash") or keyed.get("hash") or "").strip()
            url = self._normalise_url(url, "https")
            if not url or not passkey:
                logger.warn(f"站点配置行缺少域名或 passkey：{line}")
                continue
            host = url.split("://", 1)[-1].split("/")[0].lower()
            if host in SITE_SKIP:
                continue
            if host in SITE_REMAP:
                url = SITE_REMAP[host]
            out.append({
                "name": name or url.split("://", 1)[-1].split("/")[0],
                "url": url.rstrip("/") + "/",
                "passkey": passkey,
                "rsskey": rsskey,
                "sid": sid,
                "uid": uid,
                "download_page": keyed.get("download_page", ""),
                "x_api_key": keyed.get("x_api_key", ""),
                "channel": channel,
                "enabled": enabled,
                "proxy": proxy,
                "cookie": cookie,
                "rss_url": rss_url,
                "rss_interval": (keyed.get("rss_interval") or "").strip(),
                "downhash": downhash,
            })
        return out
    def _is_seeding(state: str) -> bool:
        """这个状态算不算「正在做种」。qB 用 uploading/stalledUP/forcedUP…，TR 这里统一映射成 uploading。"""
        text = str(state or "").strip().lower()
        if not text:
            return False
        return text in ("uploading", "stalledup", "forcedup", "queuedup") or "seed" in text

    def _preferred_clients(self, scanned: List[Tuple["_Client", List[Dict[str, Any]]]]) -> Dict[str, str]:
        """同一颗种子出现在多个下载器里时，决定「归谁管」。

        典型场景（用户报的 bug）：qB 负责下载、Transmission 负责保种，两边都有同一颗；
        以前按扫描顺序只认第一个下载器，于是补种全落到 qB 上。现在优先选**正在做种**的那个，
        都没在做种才看扫描顺序。
        """
        first: Dict[str, str] = {}
        seeding: Dict[str, List[str]] = {}
        for client, torrents in scanned:
            for torrent in torrents:
                info_hash = torrent.get("hash")
                if not info_hash:
                    continue
                first.setdefault(info_hash, client.name)
                if self._is_seeding(torrent.get("state")):
                    names = seeding.setdefault(info_hash, [])
                    if client.name not in names:
                        names.append(client.name)
        preferred: Dict[str, str] = {}
        for info_hash, name in first.items():
            candidates = seeding.get(info_hash) or []
            preferred[info_hash] = candidates[0] if candidates else name
        # 用户显式指定了目标下载器时，覆盖自动判断（只对它确实有的种子生效）
        wanted = (self._target_client or "").strip()
        if wanted:
            holders: Dict[str, List[str]] = {}
            for client, torrents in scanned:
                for torrent in torrents:
                    info_hash = torrent.get("hash")
                    if info_hash:
                        holders.setdefault(info_hash, []).append(client.name)
            for info_hash, names in holders.items():
                if wanted in names:
                    preferred[info_hash] = wanted
        return preferred

    def _mp_clients(self) -> List[_Client]:
        """枚举 MoviePilot「设置 → 下载器」里的**每一个**实例。

        以前只取 `Qbittorrent()` / `Transmission()` 这两个默认实例，多下载器用户
        只会看到其中一台（issue #7）。MP v3 暴露了 DownloaderHelper，按名字列出
        所有已启用实例，一台一个适配器。
        """
        out: List[_Client] = []
        try:
            from app.application.downloader import DownloaderHelper
            services = DownloaderHelper().get_services() or {}
        except Exception as err:
            logger.info(f"MoviePilot 多下载器接口不可用（{type(err).__name__}: {err}），退回单实例模式")
            return []
        for name, info in services.items():
            instance = getattr(info, "instance", None)
            if instance is None:
                continue
            if not (hasattr(instance, "get_completed_torrents") and hasattr(instance, "add_torrent")):
                continue
            label = f"MP-{name}"
            out.append(_MPClient(label, instance.get_completed_torrents, instance.add_torrent,
                                 getattr(instance, "delete_torrent", None),
                                 getattr(instance, "get_torrents", None)))
            logger.info(f"MoviePilot 下载器已接入：{label}（{getattr(info, 'type', '?')}）")
        return out

    def _clients(self, use_mp: Optional[bool] = None, lines: Optional[str] = None) -> List[_Client]:
        """列出下载器。`use_mp` / `lines` 可覆盖实例配置——保种组有自己的一套客户端设置。"""
        mp_allowed = self._use_mp_clients if use_mp is None else bool(use_mp)
        line_text = self._clients_lines if lines is None else str(lines or "")
        out: List[_Client] = []
        if mp_allowed:
            mp_clients = self._mp_clients()
            if mp_clients:
                out.extend(mp_clients)
            else:
                # 老版本 MP 或组合根未就绪：退回「默认那个 qB/TR」的老写法
                try:
                    from app.modules.qbittorrent import Qbittorrent
                    qb = Qbittorrent()
                    if qb.is_inactive():
                        logger.info("MoviePilot 的 qBittorrent 未启用")
                    else:
                        out.append(_MPClient("MP-QB", qb.get_completed_torrents, qb.add_torrent,
                                             getattr(qb, "delete_torrent", None),
                                             getattr(qb, "get_torrents", None)))
                except Exception as err:
                    logger.warn(f"读取 MoviePilot qBittorrent 失败：{err}")
                try:
                    from app.modules.transmission import Transmission
                    tr = Transmission()
                    if tr.is_inactive():
                        logger.info("MoviePilot 的 Transmission 未启用")
                    else:
                        out.append(_MPClient("MP-TR", tr.get_completed_torrents, tr.add_torrent,
                                             getattr(tr, "delete_torrent", None)))
                except Exception as err:
                    logger.warn(f"读取 MoviePilot Transmission 失败：{err}")
        line_clients = self._parse_client_lines(line_text)
        if line_clients:
            out.extend(line_clients)
        raw = (self._clients_json or "").strip() if (not line_clients and lines is None) else ""
        if raw:
            try:
                for idx, item in enumerate(json.loads(raw)):
                    if not isinstance(item, dict) or not item.get("url"):
                        continue
                    if item.get("type") == "transmission":
                        out.append(TransmissionClient(item.get("name") or f"TR-{idx + 1}", item["url"],
                                                      item.get("username", ""), item.get("password", "")))
                    else:
                        out.append(QBittorrentClient(item.get("name") or f"QB-{idx + 1}", item["url"],
                                                     item.get("username", ""), item.get("password", "")))
            except Exception as err:
                logger.error(f"clients_json 解析失败：{err}")
        return out

    def _collect_speed_limits(self, clients: List[_Client]) -> List[Dict[str, Any]]:
        """采集每台客户端**当前**的限速设置（不管插件有没有配、有没有接管）。"""
        # 站点契约的 SeedGroupClientLimit 是 additionalProperties:false、整数、1–32 条：
        # 读不到的客户端只把 readable 置 false、数值记 0（「不限速」），**不塞 error 之类的额外字段**，
        # 否则一台读不到会让整份上报 400 —— 那就成了「没上报」，而不是「照实上报没限速」。
        rows: List[Dict[str, Any]] = []
        for client in clients[:32]:
            row: Dict[str, Any] = {"client": str(client.name)[:128],
                                   "kind": "tr" if isinstance(client, TransmissionClient) else "qb",
                                   "dl_kib": 0, "up_kib": 0,
                                   "dl_enabled": False, "up_enabled": False, "readable": False}
            try:
                data = client.get_speed_limits() or {}
            except Exception as err:
                logger.warn(f"读取限速失败（{client.name}）：{type(err).__name__}: {err}")
                data = {}
            if data:
                for key in ("dl_kib", "up_kib"):
                    try:
                        row[key] = max(0, int(data.get(key) or 0))
                    except Exception:
                        row[key] = 0
                for key in ("dl_enabled", "up_enabled"):
                    row[key] = bool(data.get(key))
                row["readable"] = True
            rows.append(row)
        return rows

    def seed_group_rotate(self, **kwargs) -> Dict[str, Any]:
        """手动触发一次动态轮换（体积达标后才有效）。"""
        clients = self._sg_clients()
        state = self.seed_group_status()
        return self._sg_rotate(clients, state, self._sg_candidates())

    def _sg_rotate(self, clients: List[_Client], state: Dict[str, Any],
                   pool_all: List[Dict[str, Any]]) -> Dict[str, Any]:
        """体积已达标时的动态保种：删「做种人数最多」的，补「做种人数最少」的。

        只在达标后执行（没达标就只补满，不轮换）；每次各 N 颗（默认 5）。
        「做种人数」取站点侧的 seeders（跟池子一起拉回来的）。
        """
        out: Dict[str, Any] = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "dropped": 0, "added": 0, "failed": 0,
                               "dropped_names": [], "added_names": [], "reason": ""}
        if not self._seedgroup_rotate:
            out["reason"] = "动态保种已关闭"
            return out
        if not clients:
            out["reason"] = "没有可用的下载器"
            return out
        limit = max(1, min(50, int(self._seedgroup_rotate_count or 5)))
        existing, _, _, _ = self._sg_current(clients)
        items = self._sg_items()
        by_tid = {str(x.get("torrent_id")): x for x in pool_all if x.get("torrent_id") is not None}

        droppable: List[Tuple[int, str, Dict[str, Any]]] = []
        for info_hash, record in items.items():
            if info_hash not in existing:
                continue
            row = by_tid.get(str((record or {}).get("torrent_id") or ""))
            if not row:
                continue                      # 池子里查不到的（已下架/换池）交给 prune，不在轮换里动
            droppable.append((int(row.get("seeders") or 0), info_hash, record))
        # 做种人数最多的先删
        droppable.sort(key=lambda item: (-item[0], item[1]))
        drops = droppable[:limit]
        if not drops:
            out["reason"] = "没有可轮换的已保种子（池子里查不到它们的做种人数）"
            return out

        kept_tids = {str((rec or {}).get("torrent_id")) for _, _, rec in drops}
        skips = self._sg_skips()
        pending = [x for x in pool_all
                   if str(x.get("info_hash") or "").lower() not in existing
                   and str(x.get("torrent_id")) not in kept_tids
                   and _seed_group_key(x) not in skips]
        # 做种人数最少的最该补
        pending.sort(key=lambda x: (int(x.get("seeders") or 0), str(x.get("torrent_id") or "")))
        adds = pending[:len(drops)]

        client = clients[0]
        for seeders, info_hash, record in drops:
            name = str((record or {}).get("name") or info_hash[:12])
            try:
                deleted = client.delete([info_hash], delete_files=self._seedgroup_rotate_files)
            except Exception as err:
                logger.warn(f"动态保种：删除 {name} 异常：{err}")
                deleted = False
            if deleted:
                out["dropped"] += 1
                out["dropped_names"].append(f"{name}（{seeders} 做种）")
                items.pop(info_hash, None)
            else:
                out["failed"] += 1

        for item in adds:
            if self._event.is_set():
                break
            data = self._sg_download(item)
            if not data:
                out["failed"] += 1
                continue
            info_hash = _torrent_info_hash(data) or str(item.get("info_hash") or "")
            result = client.add(data, self._seedgroup_save_path, self._seedgroup_skip_check,
                                label=SEED_GROUP_TAG, hash_hint=info_hash)
            if result in ("ok", "dup"):
                out["added"] += 1
                out["added_names"].append(f"{str(item.get('name') or '')[:48]}"
                                          f"（{int(item.get('seeders') or 0)} 做种）")
                self._sg_remember_item(info_hash, item)
            else:
                out["failed"] += 1
                self._sg_mark_skip(item, "rotate-fail")
            time.sleep(self.SG_DOWNLOAD_GAP)

        self.save_data("seedgroup_items", items)
        self.save_data("seedgroup_rotate", out)
        logger.info(f"动态保种：删 {out['dropped']} 补 {out['added']}（失败 {out['failed']}）")
        return out

    def _sg_prune_if_enabled(self, state: Dict[str, Any]) -> None:
        """下架清理：达标那轮也要跑。

        但核对是**每颗种子一次 GET**（637 颗 = 637 个请求），而 2026-11-17 把 cron 从 6 小时
        改成 1 小时之后，它不该跟着翻 6 倍。所以这里加时间闸：距上次核对不足
        SG_PRUNE_MIN_INTERVAL 就跳过，进种照常每小时跑。
        """
        if not self._seedgroup_prune:
            return
        now = int(time.time())
        try:
            last = int(self.get_data("seedgroup_prune_at") or 0)
        except Exception:
            last = 0
        if last and now - last < self.SG_PRUNE_MIN_INTERVAL:
            state["prune_skipped"] = f"距上次核对 {int((now - last) / 60)} 分钟，未到 {self.SG_PRUNE_MIN_INTERVAL // 3600} 小时"
            return
        try:
            pruned = self._sg_prune()
            state["pruned"] = pruned.get("removed", 0)
            state["prune_checked"] = pruned.get("checked", 0)
            state["prune_failed"] = pruned.get("failed", 0)
            self.save_data("seedgroup_prune_at", int(time.time()))
            self.save_data("seedgroup_state", state)
        except Exception as err:
            logger.warn(f"保种组清理异常（不影响本轮同步）：{type(err).__name__}: {err}")

    # ==================== 插件凭据 ====================
    # 站点给每份插件安装发两样东西：
    #   票据（rcapi_…，两小时）—— 随每次上报走。短命是因为它每 5 分钟过一过一次网络，
    #     泄露窗口按小时算，不按年算。
    #   签名密钥（长期）—— 只留在本机插件数据里，唯一的用途是票据过期后换一张新的。
    #     它驱动不了任何业务端点：站点只认它的 HMAC，不把它当 token。
    #
    # 为什么必须能换票：票据两小时就过期，而站点登录要解图形验证码，那只能在浏览器前解。
    # 没有换票，用户每两小时要回家点一次验证码 —— 那不是安全，那是让人把插件关掉。
    PLUGIN_ID = "colaseedgroup"
    _SIG_MODEL = "COLA-PLUGIN-1"

    def _plugin_instance_id(self) -> str:
        """本机实例标识：首次生成后落盘，容器重建不变（数据卷还在）。"""
        iid = str(self.get_data("plugin_instance_id") or "").strip()
        if not iid:
            iid = uuid.uuid4().hex[:16]
            self.save_data("plugin_instance_id", iid)
        return iid

    def _plugin_credentials(self) -> Tuple[str, str]:
        """返回 (票据, 签名密钥)。只存在插件数据里，不渲染到表单 —— 可编辑的输入框
        只会让用户把自己别的机器的票据粘进来。"""
        return (str(self.get_data("plugin_key") or "").strip(),
                str(self.get_data("plugin_signing_key") or "").strip())

    def _plugin_save_credentials(self, token: str = "", secret: str = "") -> None:
        if token:
            self.save_data("plugin_key", token)
        if secret:
            self.save_data("plugin_signing_key", secret)

    def _plugin_signature(self, method: str, url: str, body: str) -> str:
        """签名串必须与站点逐字节一致：
        COLA-PLUGIN-1 / method / path+query / instance / ts / nonce / sha256hex(body)。
        站点侧有同名实现与固定向量测试，改格式两边会一起红。"""
        _, secret = self._plugin_credentials()
        if not secret:
            return ""
        # 站点签的是「路径 + 查询串」，不含 scheme 与 host。
        host_and_path = url.split("://", 1)[-1]
        slash = host_and_path.find("/")
        path = host_and_path[slash:] if slash >= 0 else "/"
        ts = int(time.time())
        nonce = base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode()
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical = "\n".join([self._SIG_MODEL, method, path, self._plugin_instance_id(),
                               str(ts), nonce, body_hash])
        mac = base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                     hashlib.sha256).digest()).rstrip(b"=").decode()
        return f"v1 ts={ts} nonce={nonce} mac={mac}"

    def _plugin_headers(self, method: str, url: str, body: str) -> Dict[str, str]:
        """带凭据的请求头。

        刻意不带 Cookie / X-CSRF-Token：那是浏览器会话的东西。混进来只会让站点分不清
        这次调用按哪个身份算，票据等于白拿。
        """
        token, _ = self._plugin_credentials()
        headers = {
            "Content-Type": "application/json",
            "Origin": COLA_BASE,
            "X-Plugin-Instance": self._plugin_instance_id(),
            "X-Plugin-Version": self.plugin_version,
        }
        if token:
            headers["X-API-Key"] = token
            sig = self._plugin_signature(method, url, body)
            if sig:
                headers["X-Plugin-Signature"] = sig
        return headers

    def _plugin_enroll(self) -> bool:
        """用站点会话换一把票据（登录成功后调用）。"""
        if not self._session_token:
            return False
        payload = {"plugin_id": self.PLUGIN_ID,
                   "instance_id": self._plugin_instance_id(),
                   "label": ""}
        url = urljoin(COLA_BASE, "/api/v1/plugin/keys")
        headers = {"Content-Type": "application/json", "Origin": COLA_BASE,
                   "Cookie": f"reelcore_session={self._session_token}"}
        csrf = self._csrf_token()
        if csrf:
            headers["X-CSRF-Token"] = csrf
        try:
            rsp = _curl("POST", url, headers=headers, json_body=payload, timeout=REQUEST_TIMEOUT)
        except Exception as err:
            logger.warn(f"插件凭据签发失败：{type(err).__name__}: {err}")
            return False
        if rsp is None or rsp.status_code not in (200, 201):
            logger.warn(f"插件凭据签发失败：站点返回 {0 if rsp is None else rsp.status_code}"
                        "（站点未升级时这里会是 404，插件按会话继续跑）")
            return False
        try:
            data = json.loads(rsp.text or "{}")
        except Exception:
            return False
        token = str(data.get("token") or "")
        secret = str(data.get("signing_key") or "")
        if not token or not secret:
            return False
        self._plugin_save_credentials(token, secret)
        logger.info(f"可乐PT 插件凭据已签发（{token[:12]}…），有效期至 {data.get('expires_at')}")
        return True

    def _plugin_renew(self) -> bool:
        """用签名换一张新票据。不需要站点会话 —— 站点宕机几小时后恢复，插件一次调用就活。"""
        token, secret = self._plugin_credentials()
        if not token or not secret:
            return False
        url = urljoin(COLA_BASE, "/api/v1/plugin/keys/renew")
        sig = self._plugin_signature("POST", url, "")
        if not sig:
            return False
        headers = {"Content-Type": "application/json", "Origin": COLA_BASE,
                   "X-API-Key": token, "X-Plugin-Instance": self._plugin_instance_id(),
                   "X-Plugin-Version": self.plugin_version, "X-Plugin-Signature": sig}
        try:
            rsp = _curl("POST", url, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception as err:
            logger.warn(f"插件凭据换新失败：{type(err).__name__}: {err}")
            return False
        if rsp is None or rsp.status_code != 200:
            return False
        try:
            data = json.loads(rsp.text or "{}")
        except Exception:
            return False
        new_token = str(data.get("token") or "")
        if not new_token:
            return False
        self._plugin_save_credentials(token=new_token)
        logger.info(f"可乐PT 插件凭据已换新（{new_token[:12]}…）")
        return True

    def _maybe_report(self, clients: List[_Client], state: Dict[str, Any]) -> None:
        """上报不抛异常：限速采集/发送失败不能影响保种本身。

        **所有人都上报**（2026-11-19 改）。原先「只有工作组成员才汇报」的规则会让非成员的
        票据永远拿不到续命的业务写入 —— 他们一行都不发，站点两小时后就当他们掉线，
        而换票次数有限，几天之后就必须重新解图形验证码。
        现在站点按上报者当时的身份决定这一行进不进考核（rc_site_seed_group_reports.is_staff），
        非成员的数据只留档，不影响任何人的分数。
        """
        try:
            report = self._report_seedgroup(clients, state)
            state["report_ok"] = report.get("ok")
            state["report_reason"] = report.get("reason")
            state["report_skipped"] = report.get("skipped")
            self.save_data("seedgroup_state", state)
        except Exception as err:
            logger.warn(f"保种组限速上报异常：{type(err).__name__}: {err}")

    def _report_seedgroup(self, clients: List[_Client], state: Dict[str, Any]) -> Dict[str, Any]:
        """把「保种组用户的客户端限速情况」汇报给站点（只统计开了保种组的用户）。

        · 未开保种组 / 关掉上报 / 没配上地址 → 直接跳过，不采集也不发送；
        · 不论成功失败，快照都会写进插件数据，面板能看到；
        · 站点接口不在线（404/连不上）只记日志，不影响保种。
        """
        out: Dict[str, Any] = {"ok": False, "skipped": False, "reason": "", "clients": []}
        if not self._seedgroup_enabled:
            out.update({"skipped": True, "reason": "保种组未启用（不采集）"})
            return out
        if not self._seedgroup_report:
            out.update({"skipped": True, "reason": "上报开关已关闭"})
            return out
        limits = self._collect_speed_limits(clients)
        out["clients"] = limits
        url = (self._seedgroup_report_url or "").strip()
        if not url:
            out.update({"skipped": True, "reason": "没有配置上报地址"})
            return out
        record = self._sg_unlock_record()
        # 站点的契约是 additionalProperties:false：只发它声明的字段，
        # 多一个（plugin/version/at/uid/username…）都会 400。身份由会话决定，不用自己报。
        payload = {
            "plugin_version": self.plugin_version,
            "groups": record.get("groups") or [],
            "tier": state.get("tier") or self._seedgroup_tier,
            "scope": self._seedgroup_scope,
            "target_gib": state.get("target_gib"),
            "covered_gib": state.get("covered_gib"),
            "shortfall_gib": state.get("shortfall_gib"),
            # 站点按这个字段累计「当月达标时长」：covered >= target 才算达标
            # `all` 档按 60% 算达标（目标每天都在涨，要求 100% 永远追不上），
            # 其余档位仍是 100%。站点只认这个布尔值，不自己算。
            "on_target": _sg_on_target(
                state.get("covered_gib"), state.get("target_gib"),
                ratio=SEED_GROUP_ALL_TARGET_RATIO
                if seed_group_tier_key(state.get("tier") or self._seedgroup_tier) == SEED_GROUP_ALL_TIER
                else 1.0,
            ),
            # 客户端里这批种子最老的那颗已经做了多久（交叉验证「保种时长」不是编的）
            "seeded_secs": self._sg_oldest_seeded_secs(clients),
            "enforced": {"dl_kib": self._limit_dl_kib, "up_kib": self._limit_up_kib},
            "clients": limits,
        }
        # 上报只认插件凭据（两小时票据 + 实例签名），站点会话不再能写这一行。
        # 没有票据时先领一把 —— 领不到（站点还没升级）就按会话继续跑，不报错。
        if not self._plugin_credentials()[0] and self._session_token:
            self._plugin_enroll()
        # 签名要对**实际发出去的字节**算，所以按 _curl 里同一个 json.dumps 先算出字符串。
        body_text = json.dumps(payload)
        headers = self._plugin_headers("POST", url, body_text)
        status = 0
        try:
            rsp = _curl("POST", url, headers=headers, json_body=payload, timeout=REQUEST_TIMEOUT)
            status = 0 if rsp is None else rsp.status_code
            # 票据过期才值得换一张：站点把「过期」与「无效/撤销」分开答，撤销了换也没用，
            # 那种情况只能重新登录站点。
            if status == 401 and self._plugin_credentials()[0]:
                if self._plugin_renew():
                    headers = self._plugin_headers("POST", url, body_text)
                    rsp = _curl("POST", url, headers=headers, json_body=payload,
                                timeout=REQUEST_TIMEOUT)
                    status = 0 if rsp is None else rsp.status_code
                    out["renewed"] = True
        except Exception as err:
            out["reason"] = f"{type(err).__name__}: {err}"
        out["status"] = status
        out["ok"] = status in (200, 201, 202, 204)
        if not out["ok"] and not out["reason"]:
            out["reason"] = f"站点返回 HTTP {status}（接口未上线或地址不对）"
        self.save_data("seedgroup_report_last", {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": out["ok"], "status": status,
            "reason": out["reason"], "clients": limits, "url": url,
        })
        logger.info(f"保种组限速上报：{'成功' if out['ok'] else '失败(' + out['reason'] + ')'}，"
                    f"{len(limits)} 台客户端")
        return out

    def _speed_limit_map(self) -> Dict[str, Tuple[int, int]]:
        """解析「每客户端限速」：每行 `客户端名或地址,下载KiB/s,上传KiB/s`。

        匹配用「包含」：写 `198.72`、`198.72.127.68:8081`、`MP-qb` 都能命中对应客户端。
        下载/上传任一 > 0 才算配置了这台。
        """
        out: Dict[str, Tuple[int, int]] = {}
        for line in (self._limit_lines or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in re.split(r"[,，]", line)]
            if len(parts) < 2 or not parts[0]:
                continue
            try:
                dl = max(0, int(float(parts[1] or 0)))
            except Exception:
                dl = 0
            up = 0
            if len(parts) >= 3:
                try:
                    up = max(0, int(float(parts[2] or 0)))
                except Exception:
                    up = 0
            if dl or up:
                out[parts[0].lower()] = (dl, up)
        return out

    def _limit_for(self, name: str, overrides: Dict[str, Tuple[int, int]]) -> Tuple[int, int, str]:
        """这台客户端该用哪组限速：命中覆盖行就用它，否则用全局默认。"""
        lowered = (name or "").lower()
        if lowered in overrides:                      # 精确匹配优先
            pair = overrides[lowered]
            return pair[0], pair[1], lowered
        for key in sorted(overrides, key=len, reverse=True):   # 再按 key 长度降序，宽 key 不会抢精确 key
            if key and key in lowered:
                pair = overrides[key]
                return pair[0], pair[1], key
        return int(self._limit_dl_kib or 0), int(self._limit_up_kib or 0), ""

    def _apply_speed_limits(self, clients: Optional[List[_Client]] = None) -> Dict[str, Any]:
        """强行接管下载/上传限速。

        语义（老板 2026-11-15 定）：**填了才接管，没填就一点不碰**。
          · 下载、上传各填一个 KiB/s；留空或 0 = 这一项完全不干预客户端原有设置；
          · 每次任务开始都会重新下发一次（幂等），所以有人在客户端里手改限速，下一轮会被改回来。
        """
        overrides = self._speed_limit_map()
        defaults = (int(self._limit_dl_kib or 0), int(self._limit_up_kib or 0))
        out: Dict[str, Any] = {"applied": 0, "failed": 0, "skipped": 0,
                               "dl_kib": defaults[0], "up_kib": defaults[1],
                               "clients": [], "skipped_all": False}
        if not overrides and defaults[0] <= 0 and defaults[1] <= 0:
            out["skipped_all"] = True
            return out
        for client in (clients if clients is not None else self._clients()):
            dl, up, hit = self._limit_for(client.name, overrides)
            row = {"client": client.name, "dl_kib": dl, "up_kib": up, "source": hit or "default"}
            if dl <= 0 and up <= 0:
                # 这台没配（全局也没配）→ 不接管它，保持客户端原样
                row["result"] = "skip"
                out["skipped"] += 1
                out["clients"].append(row)
                continue
            try:
                ok = client.set_speed_limits(dl, up)
            except Exception as err:
                logger.warn(f"设置限速失败（{client.name}）：{err}")
                ok = False
            row["result"] = "ok" if ok else "fail"
            out["applied" if ok else "failed"] += 1
            out["clients"].append(row)
        logger.info(f"限速接管：成功 {out['applied']} 台、失败 {out['failed']} 台、"
                    f"未配置跳过 {out['skipped']} 台；明细 "
                    + "；".join(f"{r['client']}={r['dl_kib'] or '-'}/{r['up_kib'] or '-'}"
                               f"({r['source']})" for r in out["clients"]))
        return out

    # ---------------- 节奏与去重 ----------------

    # 失败队列最多留这么多条；每条存 torrent 原文的 base64（典型 20–200KB），不存站点凭据。
    PENDING_LIMIT = 40
    def _save_pending(self, items: List[Dict[str, Any]]) -> None:
        self.save_data("pending", items[-self.PENDING_LIMIT:])
    def _add_times(self) -> List[float]:
        times = self.get_data("add_times") or []
        cutoff = time.time() - 3600
        return [t for t in times if isinstance(t, (int, float)) and t > cutoff]

    def _budget_left(self) -> int:
        """整轮预算。max_per_hour=0 表示不限，返回一个很大的数（调用点只判断 <= 0）。"""
        if self._max_per_hour <= 0:
            return 1 << 30
        return max(0, self._max_per_hour - len(self._add_times()))

    def _site_budget_left(self, site_key: str) -> int:
        cap = self._site_limits.get(site_key) or self._max_per_site
        try:
            cap = max(1, min(200, int(cap)))
        except Exception:
            cap = self._max_per_site
        return max(0, cap - self._site_counter.get(site_key, 0))

    def _site_open(self, site_key: str) -> bool:
        """熔断：这个站本轮失败太多次就先跳过，别让慢站拖住整轮。"""
        return self._site_fails.get(site_key, 0) < self._site_fail_limit

    def _site_fail(self, site_key: str) -> None:
        n = self._site_fails.get(site_key, 0) + 1
        self._site_fails[site_key] = n
        if n == self._site_fail_limit:
            logger.warn(f"{site_key} 本轮已失败 {n} 次，先跳过（下轮再试）")

    def _record_add(self) -> None:
        """只有真正加进客户端的种子才占配额；扫描、匹配、失败都不占。"""
        times = self._add_times()
        times.append(time.time())
        self.save_data("add_times", times)

    def _done(self) -> List[str]:
        return self.get_data("done") or []

    def _remember(self, key: str) -> None:
        done = self._done()
        if key not in done:
            done.append(key)
        self.save_data("done", done[-10000:])

    def _forget(self, key: str) -> None:
        """撤销一条去重记忆：客户端里已经没有这颗种子时，必须允许再补一次。"""
        done = [k for k in self._done() if k != key]
        self.save_data("done", done)

    # 自愈删除时把种子原有的标签记下来：重新补种时原样加回去（issue #11）。
    TAG_MEMORY_LIMIT = 800
    def _dead_hashes(self) -> List[str]:
        """文件确实缺失、被自愈清掉的 hash：这些不再重试，避免每小时加了又删。"""
        try:
            items = self.get_data("dead_hashes") or []
            return [str(h).lower() for h in items if h]
        except Exception:
            return []

    def _mark_dead(self, hashes: List[str]) -> None:
        dead = self._dead_hashes()
        for info_hash in hashes:
            info_hash = str(info_hash or "").lower()
            if info_hash and info_hash not in dead:
                dead.append(info_hash)
        self.save_data("dead_hashes", dead[-2000:])

    # ---------------- IYUU 云端辅种（有 token 时优先用，覆盖面比 pieces-hash 大得多） ----------------

    IYUU_API = "http://2025.iyuu.cn"
    IYUU_CHUNK = 100
    def _parse_rss_items(content: bytes) -> Dict[str, Dict[str, Any]]:
        """把 RSS 解析成 info_hash -> {url,title,size}。

        先用 XML 解析器；解析失败再按 <item> 粗切——有站点在 description 里塞坏 CDATA
        （时光 unclosed CDATA、蝶粉 unclosed token），标准解析器会整体罢工，但这些 item
        的 guid/enclosure 其实是好的。
        """
        items: Dict[str, Dict[str, Any]] = {}

        def take(guid: str, url: str, length: str, title: str) -> None:
            guid = (guid or "").strip().lower()
            if len(guid) != 40 or any(c not in "0123456789abcdef" for c in guid):
                return  # guid 不是 info_hash 的站，这条通道用不了
            try:
                size = int(length or 0)
            except Exception:
                size = 0
            items[guid] = {"url": url or "", "title": (title or "").strip(), "size": size}

        try:
            root = ET.fromstring(content.decode("utf-8", "ignore"))
            for item in root.findall(".//item"):
                enc = item.find("enclosure")
                take(item.findtext("guid") or "",
                     enc.get("url") if enc is not None else "",
                     enc.get("length") if enc is not None else "",
                     item.findtext("title") or "")
            return items
        except Exception:
            items.clear()
        text = content.decode("utf-8", "ignore")
        for block in re.findall(r"<item\b.*?</item>", text, re.S):
            guid = re.search(r"<guid[^>]*>\s*([0-9a-fA-F]{40})\s*</guid>", block)
            enc = re.search(r"<enclosure\b[^>]*>", block)
            url = length = ""
            if enc:
                m_url = re.search(r'url\s*=\s*"([^"]*)"', enc.group(0))
                m_len = re.search(r'length\s*=\s*"(\d+)"', enc.group(0))
                url = m_url.group(1) if m_url else ""
                length = m_len.group(1) if m_len else ""
            title = re.search(r"<title>(.*?)</title>", block, re.S)
            take(guid.group(1) if guid else "", url, length, title.group(1) if title else "")
        return items
    def _rss_at(self) -> Dict[str, float]:
        try:
            data = self.get_data("rss_at") or {}
            return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    # RSS 索引是 6 个线程并发拉的，插件数据是读-改-写，必须串行，否则两边互相覆盖/抢锁
    _rss_lock = Lock()

    def _rss_mark(self, host: str) -> None:
        with self._rss_lock:
            data = self._rss_at()
            data[host] = time.time()
            self.save_data("rss_at", data)

    def _rss_tries(self, site: Dict[str, str]) -> List[Tuple[str, Dict[str, Any], str]]:
        """这个站要按什么顺序、用什么密钥去拉 RSS（第一个候选也用于并发预取）。"""
        host = site.get("url", "").split("://", 1)[-1].split("/")[0].lower()
        rsskey = str(site.get("rsskey") or "")
        passkey = str(site.get("passkey") or "")
        personal = str(site.get("rss_url") or "").strip()
        url = f"{site.get('url') or 'https://' + host}/torrentrss.php"
        tries: List[Tuple[str, Dict[str, Any], str]] = []
        if personal:
            # 站点给的个人 RSS 链接（带签名/有效期）。家园、听听歌这类只认这个，通用参数一律过期。
            tries.append((personal, {}, "rss_url"))
        if rsskey:
            tries.append((
                url,
                {"rsskey": rsskey, "linktype": "dl", "inclbookmarked": 0, "allsec": 1, "rows": self.RSS_ROWS},
                "rsskey",
            ))
        if passkey:
            tries.append((
                url,
                {"passkey": passkey, "linktype": "dl", "inclbookmarked": 0, "allsec": 1, "rows": self.RSS_ROWS},
                "passkey",
            ))
        return tries
    def _rss_cached(self, host: str) -> Tuple[Dict[str, Dict[str, Any]], bool]:
        """返回 (缓存的索引, 缓存是否新鲜)。"""
        cache = self.get_data("rss_cache") or {}
        entry = cache.get(host) or {}
        cached = dict(entry.get("items")) if isinstance(entry.get("items"), dict) else {}
        fresh = bool(cached) and bool(entry.get("at")) and time.time() - int(entry["at"]) < self.RSS_TTL
        return cached, fresh
    def _rss_prefetch(self, sites: List[Dict[str, str]]) -> Dict[str, Dict[str, "_HttpResult"]]:
        """把这一轮要拉的 RSS 用 N 个 curl 子进程并发拉下来（每个站只拉第一个候选）。

        返回 host -> {label: _HttpResult}；_rss_index 会先用这份结果，失败再按原顺序补试。
        """
        specs: List[Tuple[str, str, List[str], str]] = []
        for site in sites:
            tries = self._rss_tries(site)
            if not tries:
                continue
            target, params, label = tries[0]
            host = site.get("url", "").split("://", 1)[-1].split("/")[0].lower()
            cmd, url = _curl_cmd("GET", target, params=params, headers=_site_headers(site),
                                 timeout=40, proxy=site.get("proxy") or "")
            specs.append((host, label, cmd, url))
        if not specs:
            return {}
        logger.info(f"RSS 并发预取 {len(specs)} 个站（{self.RSS_WORKERS} 路 curl）")
        results = _curl_parallel([x[2] for x in specs], [x[3] for x in specs],
                                 workers=self.RSS_WORKERS)
        out: Dict[str, Dict[str, _HttpResult]] = {}
        for (host, label, _cmd, _url), rsp in zip(specs, results):
            out.setdefault(host, {})[label] = rsp
        return out
    def _download_url(self, url: str, site: Optional[Dict[str, str]] = None) -> bytes:
        """按 RSS 里给的完整下载地址取 .torrent（downhash / 签名已在 URL 里）。

        走 _site_request 而不是裸 requests：有些站（家园、天空）要带 Cookie，还有一批站
        只有 curl 能过（NAS 上 requests 走 IPv4 被墙）。
        """
        if not url:
            return b""
        site = site or {}
        try:
            rsp = _site_request("GET", url,
                                headers=_site_headers(site, accept="application/x-bittorrent, */*"),
                                timeout=REQUEST_TIMEOUT, proxy=site.get("proxy") or "")
            if rsp.status_code == 200 and _valid_torrent(rsp.content):
                return rsp.content
            logger.warn(f"按 RSS 下载种子失败：HTTP {rsp.status_code} {len(rsp.content)} 字节")
        except Exception as err:
            logger.warn(f"按 RSS 下载种子异常：{err}")
        return b""

    def _site_torrent_url(self, site: Dict[str, str], torrent_id: str) -> str:
        """按站点模板拼下载地址；模板里认 {} {id} {passkey} {rsskey} {uid} {hash}。"""
        template = (site.get("download_page") or "").strip() or "download.php?id={}&passkey={passkey}"
        path = (template.replace("{}", str(torrent_id))
                .replace("{id}", str(torrent_id))
                .replace("{passkey}", str(site.get("passkey") or ""))
                .replace("{rsskey}", str(site.get("rsskey") or ""))
                .replace("{uid}", str(site.get("uid") or ""))
                .replace("{hash}", str(site.get("downhash") or "")))
        if "{" in path:
            # 模板里还剩没替换掉的占位符（观众 {downHash}、天空 {sign} 这类按种子生成的参数），
            # 硬拼出来一定是 404/空文件，退回 NexusPHP 通用写法，让站点自己判定。
            path = f"download.php?id={torrent_id}&passkey={site.get('passkey') or ''}"
        return urljoin(site["url"], path)

    def _rss_url_for(self, site: Dict[str, str], torrent_id: str) -> str:
        """RSS 缓存里如果有这个种子，直接用它的 enclosure 地址。

        有些站的模板里带 `{downHash}`、`{sign}` 这种**按种子生成**的参数（观众、天空），
        模板拼不出来；而 RSS 的 enclosure 就是站点自己生成的完整下载地址，最准。
        """
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        items = ((self.get_data("rss_cache") or {}).get(host) or {}).get("items") or {}
        if not items or not torrent_id:
            return ""
        pattern = re.compile(r"(?:[?&](?:id|tid)=%s(?:&|$))" % re.escape(str(torrent_id)))
        for hit in items.values():
            url = str(hit.get("url") or "")
            if url and pattern.search(url):
                return url
        return ""
    def _test_clients(self) -> List[str]:
        lines: List[str] = []
        for client in self._clients():
            torrents = client.list_completed()
            if torrents:
                lines.append(f"✅ {client.name}：连接成功，已完成 {len(torrents)} 个种子")
            else:
                # 能连上但列表为空，和连不上要分开说。
                lines.append(f"⚠️ {client.name}：连接成功，但没有已完成种子（或读取失败）")
        if not lines:
            lines.append("❌ 没有可用下载器：检查「下载器」每行格式与地址")
        return lines

    def _probe_pieces(self, site: Dict[str, str]) -> Tuple[bool, str]:
        """探一次 pieces-hash 接口，返回 (可用?, 说明)。"""
        host = site["url"].split("://", 1)[-1].split("/")[0].lower()
        cached = self._pieces_paths.get(host, None)
        if cached == "":
            return False, "没有 pieces-hash 接口"
        paths = [cached] if cached else ["/api/pieces-hash", "/nexusapi/pieces-hash"]
        last = "没有 pieces-hash 接口"
        for path in paths:
            try:
                rsp = _site_request(
                    "POST", site["url"].rstrip("/") + path,
                    json={"passkey": site.get("passkey") or "", "pieces_hash": ["0" * 40]},
                    timeout=12,
                    headers=_site_headers(site, accept="application/json"),
                    proxy=site.get("proxy") or "",
                )
            except Exception as err:
                return False, f"连不上（{type(err).__name__}）"
            if rsp.status_code in (401, 403):
                return False, f"pieces-hash 拒绝 passkey（HTTP {rsp.status_code}）"
            if rsp.status_code in (404, 405):
                last = "没有 pieces-hash 接口"
                continue
            if rsp.status_code != 200:
                last = f"pieces-hash 返回 HTTP {rsp.status_code}"
                continue
            try:
                body = rsp.json() or {}
            except Exception:
                # 家园这类站根本没有这个接口，POST 会被 302 到首页，回来是一张 HTML
                last = "pieces-hash 返回 HTML（没这个接口）"
                continue
            if not isinstance(body.get("data"), (dict, list)):
                last = "pieces-hash 返回格式不对"
                continue
            self._pieces_paths[host] = path
            return True, f"pieces-hash 可用（{path}）"
        self._pieces_paths[host] = ""
        return False, last
    def _rss_note(self, site: Dict[str, str]) -> str:
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        note = (self.get_data("rss_notes") or {}).get(host)
        if note:
            return str(note)
        return str(((self.get_data("rss_cache") or {}).get(host) or {}).get("note") or "")
    def _test_cola(self) -> str:
        if not self._session_token:
            return "❌ 可乐PT：未登录（打开插件页取验证码填一次）"
        rsp = self._cola("GET", "/api/v1/me")
        if rsp is None:
            return "❌ 可乐PT：连不上（网络/防火墙），稍后重试"
        if rsp.status_code == 200:
            try:
                return f"✅ 可乐PT：已登录（{((rsp.json() or {}).get('username') or '')}）"
            except Exception:
                return "✅ 可乐PT：已登录"
        if rsp.status_code == 401:
            return "❌ 可乐PT：会话过期，需要重新登录"
        return f"❌ 可乐PT：HTTP {rsp.status_code}"
    def run_tests(self, persist: bool = True) -> Dict[str, Any]:
        """连通性检测：只测可乐账号与下载器（辅种站点不在这个插件里）。"""
        report = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cola": self._test_cola(),
            "clients": self._test_clients(),
        }
        self._test_report = report
        if persist:
            self.save_data("test_report", report)
        logger.info(f"连通性检测：{report['cola']}；{report['clients']}")
        return report

    def _sg_clients(self) -> List[_Client]:
        """保种组的下载器。

        - 勾「用 MoviePilot 已配置下载器」→ 走 MP 的 add_torrent：种子的落盘/刮削/整理
          由 MP 自己的规则接管（这就是「默认通过 MP 强制刮削」）；
        - 「保种组自定义下载器」每行一台 qB/TR（地址,用户名,密码；TR 写 tr,…），只作用于
          保种组，辅种那套客户端不受影响；
        - 两者都空 → 回落辅种的客户端列表，不让人白开一个跑不动的功能。
        """
        lines = (self._seedgroup_clients_lines or "").strip()
        out: List[_Client] = []
        if lines:
            out = self._parse_client_lines(lines)
            if not out:
                logger.warn("保种组：选了「自定义下载器」但那一行是空的/格式不对，本轮不用任何下载器")
                return []
        elif self._seedgroup_use_mp_clients:
            # 用参数走 MP 那一支，不临时改实例属性（保种组与辅种是两个独立 service，会并发）
            out = self._clients(use_mp=True, lines="")
        if not out:
            # **绝不回落 MP 的下载器列表**（2026-11-19 事故）。
            #
            # 以前这里是 `out = self._clients()`，也就是「配置解析不出来就用
            # MP 里配的所有下载器」。后果：用户只希望往一台远端 qB 加种，
            # 一旦自定义行写得不规范，插件会把种**同时推到家里 NAS 的 qB**，
            # 把本机磁盘和上行都占掉（老板原话：「不要推送任务到 192.168.31.100」）。
            # 保种组的目标机器必须是**用户显式写下的那几台**，猜错代价太大 ——
            # 宁可本轮什么都不加，也不往没被点名的机器上写文件。
            logger.warn("保种组：没有可用的下载器（自定义行空或格式不对），本轮跳过。"
                        "不会回落到 MoviePilot 的下载器列表")
            return []
        target = (self._seedgroup_target_client or "").strip()
        if target:
            named = [c for c in out if c.name == target]
            if named:
                return named
            logger.warn(f"保种组指定的下载器 `{target}` 不存在，改用第一台")
            return out[:1]
        return out

    def _sg_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._session_token:
            headers["Cookie"] = f"reelcore_session={self._session_token}"
        return headers

    def _sg_api(self, method: str, path: str, **kwargs) -> Optional[_HttpResult]:
        """站点将来若提供专用的保种组池接口（/api/v1/plugin/seed-group），优先走它。"""
        try:
            return _curl(method, f"{COLA_BASE}{SEED_GROUP_API}{path}",
                         headers=self._sg_headers(), timeout=REQUEST_TIMEOUT, **kwargs)
        except Exception as err:
            logger.warn(f"保种组专用接口请求失败 {path}：{err}")
            return None

    # ---- 资格：只认工作组 ----

    def seed_group_identity(self) -> Dict[str, Any]:
        """用当前会话读本人资料里的工作组头衔。

        站点把组名放在 rc_site_groups.title，生产上是「工作组-审核 / 工作组-保种 /
        工作组-组长 / 工作组-转载」；`GET /api/v1/me` 的 groups 在服务端是**空数组**
        （identity.Current 直接写 []string{}），所以必须再读 `/api/v1/users/{id}/profile`：
        本人查自己时 privileged=true，groups 一定可见，不受「对外展示」开关影响。
        """
        out: Dict[str, Any] = {
            "ok": False, "staff": False, "uid": 0, "username": "",
            "roles": [], "groups": [], "display_group": "", "reason": "",
        }
        if not self._session_token:
            out["reason"] = "还没登录可乐PT：先在插件页取验证码登录"
            return out
        me = self._cola("GET", "/api/v1/me")
        if me is None or me.status_code != 200:
            out["reason"] = "读取账号资料失败（会话可能已过期）"
            return out
        body = me.json() or {}
        try:
            out["uid"] = int(body.get("id") or 0)
        except Exception:
            out["uid"] = 0
        out["username"] = str(body.get("username") or "")
        out["roles"] = [str(x) for x in (body.get("roles") or [])]
        if not out["uid"]:
            out["reason"] = "账号资料缺少 id"
            return out
        profile = self._cola("GET", f"/api/v1/users/{out['uid']}/profile")
        if profile is not None and profile.status_code == 200:
            data = profile.json() or {}
            out["groups"] = [str(x) for x in (data.get("groups") or []) if str(x).strip()]
            out["display_group"] = str(data.get("display_group") or "")
        prefix = (self._seedgroup_staff_prefix or SEED_GROUP_STAFF_PREFIX).strip()
        titles = list(out["groups"])
        if out["display_group"]:
            titles.append(out["display_group"])
        out["staff"] = any(t.startswith(prefix) for t in titles)
        if not out["staff"] and self._seedgroup_allow_admin and "admin" in out["roles"]:
            # 管理员持有全量权限，等同工作组放行（设置里可关掉）
            out["staff"] = True
        out["ok"] = True
        if not out["staff"]:
            out["reason"] = (f"账号不在工作组：组名需以「{prefix}」开头"
                             f"（当前 {', '.join(titles) or '没有任何组'}），照常拉官种，但不向站点汇报")
        return out

    def seed_group_unlock(self, ident: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """手动解锁：校验工作组资格，通过才把解锁记录写进插件数据。

        `ident` 可以传入已经算好的 `seed_group_identity()` 结果，省掉一轮 /me + profile 请求。
        """
        if not isinstance(ident, dict):
            ident = self.seed_group_identity()
        now = int(time.time())
        if not ident.get("ok"):
            record = {"ok": False, "at": now, "until": 0, "reason": ident.get("reason", ""),
                      "uid": 0, "username": "", "groups": []}
            self.save_data("seedgroup_unlock", record)
            return record
        if not ident.get("staff"):
            record = {"ok": False, "at": now, "until": 0, "reason": ident.get("reason", ""),
                      "uid": ident["uid"], "username": ident["username"], "groups": ident["groups"]}
            self.save_data("seedgroup_unlock", record)
            logger.warn(f"保种组解锁被拒：{ident.get('reason')}")
            return record
        record = {"ok": True, "at": now, "until": now + self.UNLOCK_TTL, "reason": "",
                  "uid": ident["uid"], "username": ident["username"],
                  "groups": ident["groups"], "roles": ident["roles"]}
        self.save_data("seedgroup_unlock", record)
        self.save_data("seedgroup_blocked_reason", "")
        logger.info(f"保种组已解锁：{ident['username']}（工作组 {'/'.join(ident['groups']) or '管理员'}）")
        return record

    def _sg_unlock_record(self) -> Dict[str, Any]:
        """解锁记录，**顺带算出 staff**。

        `seed_group_unlock()` 写进去的记录里只有 groups（没有 staff 字段），而 0.2.0 的
        「只有工作组成员才上报」直接读 `record["staff"]` → 永远取到 None → **一次都没上报过**
        （站点面板因此一直停在旧版本的数字上）。这里统一补算，所有调用点都能拿到。
        """
        record = self.get_data("seedgroup_unlock") or {}
        if not isinstance(record, dict):
            return {}
        groups = [str(g) for g in (record.get("groups") or [])]
        prefix = self._seedgroup_staff_prefix or ""
        record["staff"] = bool(groups) and any(g.startswith(prefix) for g in groups)
        if not record["staff"] and self._seedgroup_allow_admin and "admin" in (record.get("roles") or []):
            record["staff"] = True
        return record

    def _sg_unlocked(self, refresh: bool = True) -> Tuple[bool, Dict[str, Any]]:
        """解锁是否有效：缓存 24 小时，且必须还是同一个账号（换号后旧记录不再沿用）。

        缓存不代表「不再校验资格」：命中也只省掉一次 /me + profile，每轮任务仍会走这里。
        """
        record = self._sg_unlock_record()
        try:
            until = int(record.get("until") or 0)
        except Exception:
            until = 0
        if record.get("ok") and until > int(time.time()):
            if not refresh:
                return True, record
            ident = self.seed_group_identity()
            same_user = bool(ident.get("ok")) and int(ident.get("uid") or 0) == int(record.get("uid") or 0)
            if same_user and ident.get("staff"):
                return True, record
            # 换了账号、或已经离开工作组：作废缓存，按现场结果重写记录
            record = self.seed_group_unlock(ident)
            return bool(record.get("ok")), record
        if not refresh:
            return False, record
        record = self.seed_group_unlock()
        return bool(record.get("ok")), record

    def seed_group_status(self) -> Dict[str, Any]:
        state = self.get_data("seedgroup_state") or {}
        return state if isinstance(state, dict) else {}

    def _seedgroup_panel(self) -> Dict[str, Any]:
        """插件页顶部的「可乐保种组」面板：档位 / 覆盖体积 / 缺口 / 工作组资格。"""
        tier = seed_group_tier_key(self._seedgroup_tier)
        dynamic = tier == SEED_GROUP_ALL_TIER
        target = float(seed_group_target_gib(tier, self._seedgroup_custom_gib))
        state = self.seed_group_status()
        unlock = self._sg_unlock_record()
        # 解锁记录 24 小时过期：过期的不能还显示「已解锁」
        try:
            still_valid = int(unlock.get("until") or 0) > int(time.time())
        except Exception:
            still_valid = False
        staff = bool(unlock.get("ok")) and still_valid
        try:
            if state.get("target_gib"):
                target = float(state["target_gib"])
        except Exception:
            pass
        covered = float(state.get("covered_gib") or state.get("current_gib") or 0.0)
        pct = 0 if target <= 0 else max(0, min(100, int(round(covered / target * 100))))
        if dynamic:
            lines = [f"档位：{SEED_GROUP_TIER_LABELS.get(tier, tier)}；"
                     f"目标 {target:.0f} GiB（上次同步按池子总量算）；实际覆盖 {covered:.1f} GiB"]
        else:
            lines = [f"档位：{SEED_GROUP_TIER_LABELS.get(tier, tier)}；目标 {target:.0f} GiB；"
                     f"实际覆盖 {covered:.1f} GiB（{pct}%）"]
        # 正在下载的（插件刚塞进去、还没下完的）：直接在面板上列出来，
        # 免得用户以为「插件没在下东西」—— qB 的「下载」分类里也能看到同一批。
        try:
            inflight = int(state.get("downloading") or state.get("inflight") or 0)
        except Exception:
            inflight = 0
        quota = state.get("download_quota")
        if inflight or quota is not None:
            lines.append(f"进种状态：客户端正在下载 {inflight} 个"
                         + (f"，本轮额度 {quota}" if quota is not None else "")
                         + f"（上限 {self._seedgroup_max_downloading or '不限'}）")
        if state.get("at"):
            lines.append(f"上次同步 {state['at']}：新增 {state.get('added', 0)} 条"
                         f"（+{state.get('added_gib', 0)} GiB），重复 {state.get('duplicate', 0)}，"
                         f"失败 {state.get('failed', 0)}，缺口 {state.get('shortfall_gib', 0)} GiB")
            if state.get("missing_files"):
                lines.append(f"⚠️ {state['missing_files']} 颗带「{SEED_GROUP_TAG}」标签但丢失文件："
                             f"它们在客户端里进度 0%、永远不会下动，也不计入覆盖。"
                             f"多半是开了「保种组：跳过校验」；"
                             + (f"本轮已对 {state['rechecked']} 颗提交强制校验，校验完会自动开始下载"
                                if state.get("rechecked") else "下一轮会自动对它们提交强制校验"))
            if state.get("downloading") is not None:
                quota = state.get("download_quota")
                lines.append(f"进种限流：客户端正在下载 {state.get('downloading', 0)} 个，"
                             f"本轮额度 {'不限' if quota == 'unlimited' else quota}"
                             + (f"（因额度只挑了 {state.get('quota_trimmed')} 条没加）"
                                if state.get("quota_trimmed") else ""))
        if staff:
            groups = "/".join(unlock.get("groups") or []) or "管理员"
            when = ""
            try:
                when = datetime.fromtimestamp(int(unlock.get("at") or 0)).strftime("%Y-%m-%d %H:%M")
            except Exception:
                when = ""
            lines.append(f"资格：工作组（{groups}）；解锁于 {when}")
        else:
            lines.append("资格：" + str(unlock.get("reason")
                                        or "非工作组成员：保种照常，数据不汇报（申请进组后自动开始）"))
        if not self._seedgroup_enabled:
            lines.append("状态：未启用 —— 勾选顶部「启用保种组（功能二）」并保存，会自动校验工作组资格")
        elif not staff:
            lines.append("状态：保种中（数据不汇报）—— 非工作组成员也能拉官种，"
                         "只是覆盖/时长不进站点考核表；申请进组后自动开始汇报")
        elif float(state.get("shortfall_gib") or 0) > 0:
            lines.append("状态：已解锁，但体积还没到档位 —— "
                         + str(state.get("reason") or "等下一轮同步补齐"))
        else:
            lines.append("状态：已解锁且达到档位下限（只超不少）")
        # 资源池走的是哪条通道：填了 API Key 走机器面清单，没填就用会话面浏览接口。
        # 审查指出「静默降级到网页面」会让用户无法自证，这里把它显式打出来。
        rotate = self.get_data("seedgroup_rotate") or {}
        if rotate:
            lines.append(f"动态保种：{rotate.get('at', '')} 删 {rotate.get('dropped', 0)} 补 "
                         f"{rotate.get('added', 0)}（失败 {rotate.get('failed', 0)}）"
                         + (f" — {rotate.get('reason')}" if rotate.get("reason") else ""))
        report = self.get_data("seedgroup_report_last") or {}
        if report:
            ok = "成功" if report.get("ok") else f"失败({report.get('reason') or report.get('status')})"
            lines.append(f"限速上报（强制）：{report.get('at', '')} → {ok}；客户端 {len(report.get('clients') or [])} 台")
        key_state = "已配置" if (self._seedgroup_api_key or "").strip() else "未配置"
        pool_source = str(self.get_data("seedgroup_pool_source") or "未拉取")
        lines.append(f"资源池通道：{pool_source}（站点 API Key：{key_state}）")
        return {
            "component": "VCard",
            "props": {"class": "mb-4", "variant": "outlined"},
            "content": [
                {"component": "div", "text": "可乐保种组",
                 "props": {"style": "font-weight: 600; font-size: 1.05rem; margin-bottom: 6px"}},
                {"component": "VProgressLinear",
                 "props": {"model-value": pct, "height": 6, "rounded": True, "class": "mb-2"}},
                {"component": "div", "text": "\n".join(lines),
                 "props": {"style": "white-space: pre-wrap; line-height: 1.7"}},
            ],
        }

    # ---- 资源池：官方种子 ----

    def _sg_official_machine_page(self, page: int) -> Tuple[List[Dict[str, Any]], int, str]:
        """机器面官方清单（X-API-Key，一页最多 100 条；不回 Cookie）。"""
        key = (self._seedgroup_api_key or "").strip()
        if not key:
            return [], 0, ""
        try:
            rsp = _curl("GET", f"{COLA_BASE}{OFFICIAL_API}",
                        headers={"Accept": "application/json", "X-API-Key": key},
                        params={"page": page, "page_size": OFFICIAL_PAGE_LIMIT},
                        timeout=REQUEST_TIMEOUT)
        except Exception as err:
            logger.warn(f"官方清单（API Key）请求失败：{err}")
            return [], 0, ""
        if rsp.status_code != 200:
            logger.warn(f"官方清单（API Key）返回 {rsp.status_code}：{rsp.text[:120]}")
            return [], 0, ""
        body = rsp.json() or {}
        items: List[Dict[str, Any]] = []
        for row in body.get("items") or []:
            if not isinstance(row, dict):
                continue
            items.append({
                "torrent_id": row.get("torrent_id"),
                "info_hash": str(row.get("info_hash") or ""),
                "name": str(row.get("release_name") or row.get("name") or ""),
                "size": row.get("size_bytes") or row.get("size") or 0,
                "official": True,
            })
        return items, int(body.get("total") or 0), "api-key"

    def _sg_official_browse_page(self, page: int, official_only: bool = True) \
            -> Tuple[List[Dict[str, Any]], int, str]:
        """会话面通道：/api/v1/media?official=true&view=torrents —— 全站官方种。

        `official_only` 只为兼容旧调用；保种范围在 _sg_candidates 里定，**永远只取官方种**。
        """
        params: Dict[str, Any] = {"view": "torrents", "page": page,
                                  "page_size": self.SG_BROWSE_PAGE_SIZE, "version_limit": 20}
        if official_only:
            params["official"] = "true"
        rsp = self._cola("GET", "/api/v1/media", params=params)
        if rsp is None or rsp.status_code != 200:
            return [], 0, ""
        body = rsp.json() or {}
        items: List[Dict[str, Any]] = []
        for group in body.get("items") or []:
            if not isinstance(group, dict):
                continue
            for version in group.get("versions") or []:
                if not isinstance(version, dict):
                    continue
                if official_only and not version.get("official"):
                    continue
                status = str(version.get("status") or "approved")
                if status != "approved":
                    continue
                items.append({
                    "torrent_id": version.get("id"),
                    "info_hash": "",
                    "name": str(version.get("release_name") or ""),
                    "size": version.get("size_bytes") or version.get("size") or 0,
                    "seeders": int(version.get("seeders") or 0),
                    "official": True,
                })
        return items, int(body.get("total") or 0), "session"

    def _sg_candidates(self) -> List[Dict[str, Any]]:
        """把官方资源池拉全：优先机器面清单，没有 API Key 就走会话面浏览接口。

        两条通道的分页单位不一样，必须分开判终止条件：
          · 机器面 `/official/torrents`：total 是**种子数**，一页 100 条；
          · 会话面 `/media?official=true`：total 是**作品数**，一页 50 个作品，
            每个作品的官方版本数不定 —— 拿种子数跟作品数比会提前收工（实测只拿到 247 条）。
        """
        # 范围只决定「用哪条通道」，不决定「要不要官方」：保种只保官方种（老板 2026-11-15 明确）
        official_only = True
        if (self._seedgroup_api_key or "").strip():
            items, total, source = self._sg_collect(self._sg_official_machine_page, True)
            if items:
                self.save_data("seedgroup_pool_source", source)
                return items
        probe = lambda page: self._sg_official_browse_page(page, official_only)  # noqa: E731
        items, total, source = self._sg_collect(probe, False)
        if items:
            self.save_data("seedgroup_pool_source", "session-official")
            return items
        return []

    def _sg_collect(self, probe, machine: bool) -> Tuple[List[Dict[str, Any]], int, str]:
        """逐页收一种通道的结果，按该通道自己的分页语义停。

        终止只看 total（服务器说了算），不看「这一页收了几条」：
          · 机器面 total 是种子数，收到 total 条就停；
          · 会话面 total 是作品数，翻完 ceil(total / 每页作品数) 页就停；
          · 只有 total 缺失（服务端没给）时才用「空页」兜底。
        以前用 `len(batch) < 100` 判断，服务端把每页调成 40 条就只收到第一页。
        """
        merged: List[Dict[str, Any]] = []
        seen: set = set()
        total = 0
        source = ""
        for page in range(1, self.SG_MAX_PAGES + 1):
            batch, total, source = probe(page)
            for item in batch or []:
                key = _seed_group_key(item)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)
            if total:
                if machine:
                    if len(merged) >= total:
                        break
                elif page >= -(-total // self.SG_BROWSE_PAGE_SIZE):
                    break
            elif not batch:
                # 服务端没给 total：只能靠空页判断到底
                break
        return merged, total, source

    def _sg_download(self, item: Dict[str, Any]) -> bytes:
        """拿种子字节：优先直链，否则按 torrent_id 走站点两步授权。"""
        url = str(item.get("download_url") or "").strip()
        if url:
            try:
                rsp = _curl("GET", url, headers=self._sg_headers(), timeout=REQUEST_TIMEOUT)
                if rsp.status_code == 200 and _valid_torrent(rsp.content):
                    return rsp.content
                logger.warn(f"保种组直链下载失败：HTTP {rsp.status_code} {url[:80]}")
            except Exception as err:
                logger.warn(f"保种组直链下载异常：{err}")
        torrent_id = item.get("torrent_id") or item.get("id")
        if torrent_id:
            try:
                return self._download_cola(int(torrent_id))
            except Exception as err:
                logger.warn(f"保种组按 id 下载失败（{torrent_id}）：{err}")
        return b""

    # 读不到某台客户端时，最多沿用多久之前的那次读数（秒）。
    #
    # 为什么需要（2026-11-18）：大量用户的 MP 装在家里（NAT/CGNAT，没有公网 IP），
    # 而 qB 可能在另一台机器上。家里的上行往往只有几百 KiB/s 还夹丢包 ——
    # 拉一次 `torrents/info`（几百 KB）必然超时，于是覆盖被算成 0、`on_target` 掉到 0、
    # 保种时长的连续区间被白白打断。真实情况是「那台客户端的种一直好好在做」，
    # 只是这一轮问不到它。
    #
    # 取舍：宁可沿用旧值（并把它标出来），也不要因为一次读超时就把覆盖清零。
    # 24 小时是上限 —— 超过一天没读到就不再相信，避免把早已删掉的种一直算进去。
    SG_STALE_COVERAGE_TTL = 24 * 3600

    def _sg_current(self, clients: List[_Client]) -> Tuple[set, float, int, int]:
        """客户端里已带「可乐保种组」标签的种子。

        返回 (hash 集合, 体积 GiB, 沿用上次读数的客户端数, 判为掉线的客户端数)。

        # 读数失败怎么算（2026-11-20 老板要求）
        弱网下一次读不到是常态（他家那条线实测 50% 丢包、RTT 293ms），绝不能让它
        直接把覆盖打成 0 —— 那会让「保种时长」凭空断档，而客户端里其实好好保着。
        所以按**连续失败轮数**分档：

          第 1–2 轮  沿用 `seedgroup_client_cache` 里最后一次成功读数，覆盖不变；
          第 3 轮起  判为掉线（offline），本轮不计入覆盖，日志说清楚。

        这条线画在 3 轮：一轮 cron 一分钟，两轮足够盖住 SSH 抖动/容器重启；
        连断三分钟就是真的连不上了，那时把覆盖记 0 才是对的事实。
        """
        hashes: set = set()
        total = 0.0
        blind = 0
        offline = 0
        cache = self.get_data("seedgroup_client_cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        dirty = False
        for client in clients:
            name = str(getattr(client, "name", "") or "?")
            try:
                torrents = client.list_completed()
            except Exception as err:
                logger.warn(f"保种组：{name} 读不到种子（{type(err).__name__}）")
                torrents = None
            if torrents is None:
                snap = dict(cache.get(name) or {})
                streak = int(snap.get("fail_streak") or 0) + 1
                snap["fail_streak"] = streak
                cache[name] = snap
                dirty = True
                if streak >= self.SG_CLIENT_OFFLINE_AFTER:
                    # 连断这么多轮：这就是掉线，覆盖如实记 0，别再拿旧读数撑场面。
                    offline += 1
                    logger.warn(f"保种组：{name} 连续 {streak} 轮读不到，判为掉线（本轮不计入覆盖）")
                    continue
                try:
                    age = int(time.time()) - int(snap.get("at") or 0)
                except Exception:
                    age = self.SG_STALE_COVERAGE_TTL + 1
                if snap.get("gib") is not None and age <= self.SG_STALE_COVERAGE_TTL:
                    total += float(snap.get("gib") or 0)
                    hashes.update(snap.get("hashes") or [])
                    # blind = 本轮没读到的客户端数（体积用的是上次读数）。
                    # 它与 offline 的区别是程度：blind 是「这轮没读到」，offline 是「连断到判掉线」。
                    blind += 1
                    logger.info(f"保种组：{name} 第 {streak} 轮读不到，沿用 {age // 60} 分钟前的读数 "
                                f"{float(snap.get('gib') or 0):.1f} GiB（连续 {self.SG_CLIENT_OFFLINE_AFTER} 轮才判掉线）")
                else:
                    blind += 1
                continue
            client_gib = 0.0
            client_hashes: List[str] = []
            tagged = False
            for torrent in torrents or []:
                tags = str(torrent.get("tags") or "")
                if tags:
                    tagged = True
                if SEED_GROUP_TAG not in tags:
                    continue
                info_hash = str(torrent.get("hash") or "").lower()
                if not info_hash or info_hash in hashes:
                    continue
                hashes.add(info_hash)
                client_hashes.append(info_hash)
                size_gib = _seed_group_size_gib(torrent.get("size"))
                client_gib += size_gib
            total += client_gib
            if torrents and not tagged:
                # 客户端返回了种子但一颗都没有 tags 字段 —— 极罕见（qB/TR 都一定带）。
                # 不并进 blind（那是「读不到」的计数），单独说一句，免得文案互相污染。
                logger.warn(f"保种组：{name} 的种子没有 tags 字段，无法识别保种标记")
            # 只缓存「读成功的」：读失败就保留上一次成功值，别用空的覆盖掉。
            # fail_streak 归零 —— 读成功即是「这条线又通了」。
            cache[name] = {"at": int(time.time()), "gib": round(client_gib, 3),
                           "hashes": client_hashes[:2000], "fail_streak": 0}
            dirty = True
        if dirty:
            self.save_data("seedgroup_client_cache", cache)
        return hashes, round(total, 3), blind, offline

    UNLOCK_TTL = 86400          # 工作组资格校验结果的缓存时长（秒）
    SG_MAX_PAGES = 50
    SG_BROWSE_PAGE_SIZE = 50
    SG_MACHINE_PAGE_SIZE = 100
    SG_DOWNLOAD_GAP = 0.7        # 每颗之间停一下：站点下载限流 120 次/分，一颗要 2 个请求
    SG_SKIP_TTL = 7 * 86400      # 「加不进去」的种子记 7 天，别再每轮白下载一遍
    # 网络类失败（连接被重置/超时）不是种子的问题，是那条线的问题 —— 只退避几分钟，
    # 不能像坏种那样关 7 天，否则一次抖动就把 371 条全锁死（2026-11-20 就是这么丢的缺口）。
    SG_SKIP_TTL_NET = 300
    SG_ADD_RETRY = 2             # 单条加种的额外重试次数（网络类才重试）
    SG_ADD_RETRY_BACKOFF = 2     # 重试间隔：2s、4s
    SG_CLIENT_OFFLINE_AFTER = 3  # 连续这么多轮读不到客户端 → 判掉线（前两轮沿用上次读数）

    def _sg_skip_ttl(self, reason: str) -> int:
        """这条跳过记录能活多久。网络类是短退避，其余（坏种/重复）才用 7 天。"""
        return self.SG_SKIP_TTL_NET if reason in ("net", "error") else self.SG_SKIP_TTL

    def _sg_skips(self) -> Dict[str, Any]:
        """读的时候也清一遍过期：没有新写入时，过期条目不能永久生效。

        每条按自己记下的 `ttl` 判定 —— 网络类失败只关 5 分钟，坏种关 7 天。
        """
        data = self.get_data("seedgroup_skip") or {}
        if not isinstance(data, dict):
            return {}
        now = int(time.time())
        fresh = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            try:
                ttl = int(v.get("ttl") or self._sg_skip_ttl(str(v.get("reason") or "")))
            except Exception:
                ttl = self.SG_SKIP_TTL
            if now - int(v.get("at") or 0) < ttl:
                fresh[k] = v
        if len(fresh) != len(data):
            self.save_data("seedgroup_skip", fresh)
        return fresh

    def _sg_mark_skip(self, item: Dict[str, Any], reason: str) -> None:
        key = _seed_group_key(item)
        if not key:
            return
        now = int(time.time())
        skips = self._sg_skips()
        skips[key] = {"at": now, "reason": reason, "ttl": self._sg_skip_ttl(reason)}
        self.save_data("seedgroup_skip", skips)

    def _sg_add_with_retry(self, client: "_Client", data: bytes, info_hash: str) -> Tuple[Optional[str], Optional[Exception]]:
        """加一颗种子，网络类失败就地重试两次。

        为什么值得重试：那条线实测 50% 丢包，一次 `Connection reset` 完全不代表这颗种子
        加不进去。不重试的话 371 条里会有几十条被误判成失败，再各自进跳过表 —— 缺口就是这么来的。

        只对「抛异常」重试（连接被重置/超时）；qB 明确回 `Fails` 的那种是内容问题，
        重试没有意义，交给上层判 dup/fail。
        """
        last: Optional[Exception] = None
        for attempt in range(self.SG_ADD_RETRY + 1):
            try:
                return client.add(data, self._seedgroup_save_path, self._seedgroup_skip_check,
                                  label=SEED_GROUP_TAG, hash_hint=info_hash), None
            except Exception as err:
                last = err
                if attempt >= self.SG_ADD_RETRY:
                    break
                delay = self.SG_ADD_RETRY_BACKOFF ** (attempt + 1)
                logger.info(f"保种组加种网络异常（第 {attempt + 1} 次），{delay}s 后重试：{err}")
                time.sleep(delay)
        return None, last

    def _sg_items(self) -> Dict[str, Any]:
        data = self.get_data("seedgroup_items") or {}
        return data if isinstance(data, dict) else {}

    def _sg_remember_item(self, info_hash: str, item: Dict[str, Any]) -> None:
        """记住「本地这颗种子对应站点哪个 torrent_id」——下架清理要靠它对账。"""
        key = str(info_hash or "").lower().strip()
        if not key:
            return
        items = self._sg_items()
        items[key] = {"torrent_id": item.get("torrent_id"), "name": str(item.get("name") or ""),
                      "size_gib": float(item.get("size_gib") or 0),
                      "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if len(items) > 20000:     # 大档位会有几千颗小种子，上限给足，避免映射被淘汰后清不掉
            items = dict(sorted(items.items(), key=lambda kv: str((kv[1] or {}).get("at") or ""))[-20000:])
        self.save_data("seedgroup_items", items)

    def seed_group_prune(self, **kwargs) -> Dict[str, Any]:
        """把「站点已下架 / 撤掉官方标记」的种子从本地删掉（colabot1 的 prune 那一步）。"""
        return self._sg_prune()

    def _sg_oldest_seeded_secs(self, clients: List[_Client]) -> int:
        """保种组这批种子里，最早加进来的那颗在客户端里做了多久（秒）。"""
        oldest = 0
        for client in clients:
            try:
                rows = client.list_completed() or []
            except Exception:
                continue
            for row in rows:
                tags = row.get("tags") or row.get("tag") or ""
                if isinstance(tags, (list, tuple)):
                    tags = ",".join(str(t) for t in tags)
                if SEED_GROUP_TAG not in str(tags):
                    continue
                added = row.get("added_on") or row.get("addedOn") or 0
                try:
                    added = int(added)
                except Exception:
                    continue
                if added > 0:
                    age = int(time.time()) - added
                    if age > oldest:
                        oldest = age
        return oldest

    def _sg_prune(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"checked": 0, "removed": 0, "kept": 0, "unmapped": 0,
                               "failed": 0,
                               "reason": "", "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if not self._seedgroup_enabled:
            out["reason"] = "保种组未启用"
            return out
        if (self._seedgroup_prune_mode or "seed") == "off":
            # 手动端点也要守这个开关：配了「不清理」就不该删任何东西
            out["reason"] = "下架清理已关闭（seedgroup_prune_mode=off）"
            return out
        clients = self._sg_clients()
        if not clients:
            out["reason"] = "没有可用的下载器"
            return out
        items = self._sg_items()
        for client in clients:
            try:
                torrents = client.list_completed()
            except Exception as err:
                logger.warn(f"保种组清理：读 {client.name} 种子失败：{err}")
                continue
            for torrent in torrents or []:
                if SEED_GROUP_TAG not in str(torrent.get("tags") or ""):
                    continue
                info_hash = str(torrent.get("hash") or "").lower()
                if not info_hash:
                    continue
                out["checked"] += 1
                record = items.get(info_hash) or {}
                torrent_id = record.get("torrent_id") or torrent.get("torrent_id")
                if not torrent_id:
                    # 没有 torrent_id 就不知道站点上还是不是官方，宁可不删
                    out["unmapped"] += 1
                    continue
                try:
                    tid = int(torrent_id)
                except Exception:
                    out["unmapped"] += 1
                    logger.warn(f"保种组清理：torrent_id 不是数字（{torrent_id!r}），跳过 {info_hash[:12]}")
                    continue
                detail = self._cola("GET", f"/api/v1/torrents/{tid}")
                if detail is None:
                    continue
                drop = False
                if detail.status_code == 404:
                    drop = True                     # 站点上已经没有了
                elif detail.status_code == 200:
                    body = detail.json() or {}
                    status = str(body.get("status") or "")
                    official = body.get("official")
                    # 只认**明确**的否定：status 缺失/结构漂移（网关 200 页面、{data:...}）一律保留，
                    # 不能因为「读不到 status」就把种子删了。
                    if status in ("withdrawn", "rejected", "deleted"):
                        drop = True
                    elif status and status != "approved":
                        drop = True
                    elif official is False:
                        drop = True
                if not drop:
                    out["kept"] += 1
                    continue
                try:
                    deleted = client.delete([info_hash], delete_files=self._seedgroup_prune_files)
                except Exception as err:
                    logger.warn(f"保种组清理：删除 {info_hash[:12]} 异常：{err}")
                    deleted = False
                if not deleted:
                    # 删除没成功就不能记账、更不能丢映射（否则下次再也删不到）
                    out["failed"] += 1
                    logger.warn(f"保种组清理：{client.name} 删除 {info_hash[:12]} 未成功，保留映射")
                    continue
                out["removed"] += 1
                items.pop(info_hash, None)
                logger.info(f"保种组清理：{client.name} 删除 {info_hash[:12]}"
                            f"（torrent_id={torrent_id}，文件{'一并删除' if self._seedgroup_prune_files else '保留'}）")
        self.save_data("seedgroup_items", items)
        self.save_data("seedgroup_prune", out)
        logger.info(f"保种组清理完成：检查 {out['checked']}，删除 {out['removed']}，保留 {out['kept']}，"
                    f"失败 {out['failed']}，无映射跳过 {out['unmapped']}")
        return out

    def seed_group_probe(self) -> Dict[str, Any]:
        """探针：工作组资格 + 解锁状态 + 资源池可达性（面板与 /seedgroup/probe 共用）。"""
        ident = self.seed_group_identity()
        unlocked, record = self._sg_unlocked(refresh=False)
        pool = self._sg_candidates()
        tier = seed_group_tier_key(self._seedgroup_tier)
        return {
            "ok": bool(ident.get("ok")),
            "activated": bool(ident.get("staff")) and unlocked,
            "staff": bool(ident.get("staff")),
            "groups": ident.get("groups") or [],
            "unlocked": unlocked,
            "reason": ident.get("reason") or ("" if unlocked else "尚未解锁"),
            "tier": tier,
            "quota_gib": float(seed_group_target_gib(tier, self._seedgroup_custom_gib)),
            "pool_count": len(pool),
            "pool_gib": round(sum(_seed_group_size_gib(x.get("size")) for x in pool), 1),
            "pool_source": str(self.get_data("seedgroup_pool_source") or ""),
            "api_key": "present" if (self._seedgroup_api_key or "").strip() else "missing",
        }

    def seed_group_sync(self):
        """保种组任务入口：任何异常只记日志 + 通知，不让 MoviePilot 报「执行失败」。"""
        try:
            self._seed_group_sync()
        except Exception as err:
            logger.error(f"保种组任务异常终止：{type(err).__name__}: {err}")
            # 每轮必上报：站点只在 on_target=1 的相邻区间计时，漏报那段时间白丢
            try:
                self._maybe_report(self._sg_clients(), {
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "tier": seed_group_tier_key(self._seedgroup_tier),
                    "target_gib": 0.0, "current_gib": 0.0, "covered_gib": 0.0,
                    "shortfall_gib": 0.0, "activated": False, "unlocked": False,
                    "reason": f"{type(err).__name__}: {err}"[:200],
                })
            except Exception as report_err:
                logger.warn(f"保种组异常上报也失败了：{report_err}")
            try:
                self.post_message(mtype=NotificationType.Plugin,
                                  title="【可乐保种组】保种组执行异常",
                                  text=f"{type(err).__name__}: {err}\n下一轮会重新开始，详细栈见 MoviePilot 日志。")
            except Exception:
                pass

    def _seed_group_sync(self):
        if not self._seedgroup_enabled:
            return
        tier = seed_group_tier_key(self._seedgroup_tier)
        dynamic = tier == SEED_GROUP_ALL_TIER
        target = 0.0 if dynamic else float(seed_group_target_gib(tier, self._seedgroup_custom_gib))
        clients = self._sg_clients()
        state: Dict[str, Any] = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tier": tier, "target_gib": target, "current_gib": 0.0,
            "activated": False, "unlocked": False, "staff_groups": [], "reason": "",
            "picked": 0, "added": 0, "duplicate": 0, "failed": 0,
            "added_gib": 0.0, "covered_gib": 0.0, "shortfall_gib": 0.0,
        }
        if not self._session_token:
            state["reason"] = "插件还没登录可乐PT：先在插件页取验证码登录"
            self.save_data("seedgroup_state", state)
            logger.warn("保种组：未登录可乐PT，跳过本轮")
            self._maybe_report(clients, state)
            return
        if not clients:
            state["reason"] = "没有可用的下载器"
            self.save_data("seedgroup_state", state)
            logger.warn("保种组：没有可用的下载器")
            return
        self._apply_speed_limits(clients)
        # 解锁只决定「汇不汇报」，不再挡进种：任何登录站点的用户都能拉官种
        unlocked, record = self._sg_unlocked()
        staff = bool(record.get("staff"))
        state.update({"unlocked": unlocked, "staff": staff,
                      "staff_groups": record.get("groups") or []})
        existing, current_gib, blind, offline = self._sg_current(clients)
        # 丢失文件自检：带标签却 missingFiles 的种子既不计入覆盖、也永远不会下动。
        missing_total = 0
        rechecked = 0
        for c in clients:
            try:
                own = c.tagged_missing(SEED_GROUP_TAG) or []
            except Exception:
                continue
            if not own:
                continue
            missing_total += len(own)
            # 自动救：这些种子是「跳过校验」时代加进来的，文件不在磁盘上，客户端停在 missingFiles。
            # 强制重新校验后它会发现数据不完整、自己进入下载。每轮限 SG_RECHECK_PER_ROUND 颗。
            done = c.recheck(own[:self.SG_RECHECK_PER_ROUND])
            if done:
                rechecked += done
        if missing_total:
            state["missing_files"] = missing_total
        if rechecked:
            state["rechecked"] = rechecked
            logger.info(f"保种组：{rechecked} 颗丢失文件的种子已提交强制校验，校验完会自动开始下载")
        state["current_gib"] = current_gib
        skips = self._sg_skips()
        pool_all = self._sg_candidates()
        state["pool_count"] = len(pool_all)
        state["skipped"] = len(skips)
        # 池子一条都取不到：不能算达标（以前 target 会被算成 current，面板直接写「已达标」）
        if not pool_all:
            state["reason"] = "官方资源池取不到（API Key 无效或浏览接口没返回官方版本）"
            state["shortfall_gib"] = max(0.0, round(target - current_gib, 3)) if target > 0 else 0.0
            self.save_data("seedgroup_state", state)
            logger.warn(f"保种组：资源池为空，缺口 {state['shortfall_gib']} GiB")
            self._maybe_report(clients, state)
            return
        # 「全站官种」的目标用**未过滤**的池子算：跳过表里的条目只是「这轮先不加」，
        # 不能因为它们让目标缩水，否则会显示「已达标」而其实还有没保的官方种。
        if dynamic:
            pool_total = round(sum(_seed_group_size_gib(x.get("size")) for x in pool_all), 3)
            target = round(max(current_gib, pool_total), 3)
            state["target_gib"] = target
        candidates = [x for x in pool_all
                      if str(x.get("info_hash") or "").lower() not in existing
                      and _seed_group_key(x) not in skips]
        state["pool_pending"] = len(candidates)
        need = round(target - current_gib, 3)
        state["need_gib"] = max(0.0, need)
        if need <= 0:
            # 稳态：该保的都保了。下架清理仍要跑（UI 写的是「每轮回站点核对」）。
            state.update({"activated": True, "covered_gib": current_gib, "shortfall_gib": 0.0})
            if skips:
                state["reason"] = f"{len(skips)} 条在跳过表里，本轮不重试"
            self.save_data("seedgroup_state", state)
            logger.info(f"保种组已达标：档位 {tier} / 目标 {target} GiB / 现有 {current_gib} GiB")
            try:
                rotated = self._sg_rotate(clients, state, pool_all)
                state["rotate_dropped"] = rotated.get("dropped", 0)
                state["rotate_added"] = rotated.get("added", 0)
                state["rotate_reason"] = rotated.get("reason", "")
                self.save_data("seedgroup_state", state)
            except Exception as err:
                logger.warn(f"动态保种异常（不影响本轮）：{type(err).__name__}: {err}")
            self._sg_prune_if_enabled(state)
            self._maybe_report(clients, state)
            return
        if not candidates:
            # 还有缺口，但池子里剩下的全都「已在客户端」或在跳过表：如实说清是哪种
            state["activated"] = True
            # **必须把本轮实测的覆盖写进 state**（2026-11-19 事故）：
            # 这个分支以前只更新 shortfall_gib，covered_gib 保持上一次的值 ——
            # 一旦上一轮没写过（比如刚启用/刚换客户端），它就一直是 0.0，
            # 于是「池子空了」的每一轮都上报 covered=0、on_target=0，
            # 保种时长的连续区间被凭空打断，而客户端里其实好好保着 1348 GiB。
            state["covered_gib"] = current_gib
            state["shortfall_gib"] = max(0.0, need)
            # 跳过表里混着两种：net（5 分钟后自动重投）与 fail/dup（7 天）。
            # 分开数出来，否则「跳过 410 条」会让人以为整池被锁死了。
            net_n = sum(1 for v in skips.values()
                        if isinstance(v, dict) and v.get("reason") in ("net", "error"))
            state["reason"] = (f"池中 {len(pool_all)} 条全部已在客户端或处于跳过表"
                               f"（跳过 {len(skips)} 条：其中 {net_n} 条是网络重试、"
                               f"{len(skips) - net_n} 条是坏种/重复），本轮没有可加的")
            self.save_data("seedgroup_state", state)
            logger.warn(f"保种组：{state['reason']}，缺口 {state['shortfall_gib']} GiB")
            self._sg_prune_if_enabled(state)
            self._maybe_report(clients, state)
            return
        # 进种限流（2026-11-16 老板要求）：客户端里「正在下载」的种子数不许超过上限。
        # 目的不是省带宽，而是让覆盖**稳步追平**目标 —— 「全站官种」的目标每天在涨，
        # 一次性把几百颗塞进客户端只会互相抢带宽、谁也下不完，看起来像永远追不上。
        limit = int(self._seedgroup_max_downloading or 0)
        inflight = 0
        for c in clients:
            n = c.inflight()
            if n >= 0:
                inflight += n
        quota = -1 if limit <= 0 else max(0, limit - inflight)
        state["downloading"] = inflight
        state["download_quota"] = quota if quota >= 0 else "unlimited"
        if quota == 0:
            state["activated"] = True
            state["shortfall_gib"] = max(0.0, need)
            state["pool_pending"] = len(candidates)
            state["reason"] = (f"下载队列已满（{inflight}/{limit}），本轮不进种；"
                               f"等这一批下完再继续补，还差 {state['shortfall_gib']:.1f} GiB")
            self.save_data("seedgroup_state", state)
            logger.info(f"保种组：{state['reason']}")
            self._maybe_report(clients, state)
            return
        picked, planned_gib, shortfall = seed_group_plan(candidates, need)
        if quota > 0 and len(picked) > quota:
            # 只加额度允许的条数：按 seed_group_plan 的顺序（官方优先、体积大到小）取前 N 条
            state["quota_trimmed"] = len(picked) - quota
            picked = picked[:quota]
            planned_gib = round(sum(_seed_group_size_gib(x.get("size")) for x in picked), 3)
            shortfall = round(max(0.0, need - planned_gib), 3)
        client = clients[0]
        added = duplicate = failed = 0
        added_gib = 0.0
        for item in picked:
            if self._event.is_set():
                break
            if added or failed:
                time.sleep(self.SG_DOWNLOAD_GAP)
            data = self._sg_download(item)
            if not data:
                failed += 1
                self._sg_mark_skip(item, "download")
                continue
            # 两条资源池通道都可能不带 info_hash：以真正拿到的 .torrent 为准算一遍，
            # 客户端里已经有了就直接记「重复」，不要塞进去让 qB 回 Fails 再算失败。
            info_hash = _torrent_info_hash(data) or str(item.get("info_hash") or "")
            if info_hash and client.has(info_hash):
                # 客户端里已经有这颗：补上「可乐保种组」标签，否则 _sg_current 永远算不到它的体积，
                # 固定档位会长期显示缺口。补完记进映射，下一轮就能统计到。
                duplicate += 1
                self._sg_remember_item(info_hash, item)
                # 也记进跳过表：客户端里已经有它，下一轮别再为它下载 .torrent
                # （以前只补标签不记 skip，于是每轮都重复挑中同一颗，duplicate 永远是 2）
                self._sg_mark_skip(item, "dup")
                try:
                    client.add_tags(info_hash, SEED_GROUP_TAG)
                except Exception as err:
                    logger.warn(f"保种组：给已有种子补标签失败（{info_hash[:12]}）：{err}")
                continue
            result, err = self._sg_add_with_retry(client, data, info_hash)
            if result is None:
                # 重试过仍然连不上：这是网络问题，不是这颗种子的问题 —— 记短退避（5 分钟），
                # 下一轮还会试它，而不是像坏种那样关 7 天。
                logger.warn(f"保种组加种异常（{item.get('torrent_id')}，重试 {self.SG_ADD_RETRY} 次后仍失败）：{err}")
                failed += 1
                self._sg_mark_skip(item, "net")
                continue
            if result == "ok":
                added += 1
                added_gib += float(item.get("size_gib") or 0)
                self._sg_remember_item(info_hash, item)
            elif result == "dup":
                duplicate += 1
                self._sg_mark_skip(item, "dup")
                self._sg_remember_item(info_hash, item)
            else:
                failed += 1
                self._sg_mark_skip(item, "fail")
        # 覆盖体积以「客户端实测」为准（刚加进去的种子立刻可见），
        # 不再用「计划值 + dup 的体积」估，避免缺口报 0 的假达标。
        try:
            _, measured_gib, _, offline_now = self._sg_current(clients)
        except Exception:
            measured_gib = 0.0
        # 覆盖取「客户端实测」与「上轮实测 + 本轮新增」的较大者。
        # 掉线的客户端（连续 SG_CLIENT_OFFLINE_AFTER 轮读不到）两处都不计入 —— 它确实
        # 不再提供覆盖；而偶发一两轮读不到的那些，_sg_current 已经沿用上次读数，
        # 所以这里的数不会因为一次超时从 1500 掉到 0。
        covered = round(max(measured_gib, current_gib + added_gib), 3)
        # 与上一轮的覆盖对比：只用来给面板一个「掉线/骤降」的说明，不参与达标判定。
        prev_covered = 0.0
        try:
            prev_covered = float(state.get("covered_gib") or 0)
        except Exception:
            prev_covered = 0.0
        state.update({"activated": True, "picked": len(picked), "planned_gib": planned_gib,
                      "added": added, "duplicate": duplicate, "failed": failed,
                      "added_gib": round(added_gib, 3), "covered_gib": covered,
                      "clients_offline": offline_now,
                      "prev_covered_gib": round(prev_covered, 3),
                      "shortfall_gib": max(0.0, round(target - covered, 3))})
        if state["shortfall_gib"] > 0 and not state["reason"]:
            state["reason"] = (f"实际覆盖还差 {state['shortfall_gib']} GiB"
                               f"（池子可挑 {round(planned_gib, 1)} GiB，失败 {failed} 条）")
        if blind:
            state["reason"] = (state["reason"] + "；" if state["reason"] else "") + \
                              f"{blind} 个下载器本轮读不到，体积沿用上次读数"
        if offline_now:
            state["reason"] = (state["reason"] + "；" if state["reason"] else "") + \
                              f"{offline_now} 个下载器连续读不到（判为掉线，不计入本轮覆盖）"
        self.save_data("seedgroup_state", state)
        logger.info(f"保种组完成：档位 {tier} 目标 {target} GiB，新增 {added} 条 (+{round(added_gib, 1)} GiB)，"
                    f"覆盖 {covered} GiB（上轮 {round(prev_covered, 1)}）"
                    f"，缺口 {state['shortfall_gib']} GiB，失败 {failed}"
                    + (f"，掉线 {offline_now} 台" if offline_now else ""))
        # 站点下架 / 撤掉官方标记的，跟着从本地删（colabot1 的 prune）
        self._sg_prune_if_enabled(state)
        # 只统计开了保种组的用户：把每台客户端此刻的限速设置汇报给站点
        self._maybe_report(clients, state)
        if self._notify and (added or failed):
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【可乐保种组】保种组同步",
                text=(f"档位：{SEED_GROUP_TIER_LABELS.get(tier, tier)}\n"
                      f"目标：{target} GiB / 现有：{current_gib} GiB\n"
                      f"本轮新增：{added} 条（+{round(added_gib, 1)} GiB，重复 {duplicate}，失败 {failed}）\n"
                      f"覆盖：{covered} GiB" +
                      (f"\n缺口：{state['shortfall_gib']} GiB" if state["shortfall_gib"] else "\n已达标")),
            )

    def get_state(self) -> bool:
        # 两个功能任一开启都算「插件在工作」：只开保种组时也要显示为运行中
        return bool(self._enabled and self._seedgroup_enabled and self._session_token)

    def get_service(self) -> List[Dict[str, Any]]:
        services: List[Dict[str, Any]] = []
        if self._enabled and self._seedgroup_enabled and self._seedgroup_cron:
            # 保种组是独立任务：MP「任务」页里能单独看它跑没跑、单独设周期
            services.append({
                "id": "ColaSeedGroup",
                "name": "可乐保种组",
                "trigger": _cron_trigger(self._seedgroup_cron, "0 * * * *"),
                "func": self.seed_group_sync,
                "kwargs": {},
            })
        return services

    def stop_service(self) -> None:
        self._event.set()

    # ---------------- 插件页面 / API / 表单 ----------------

    def get_page(self) -> Optional[List[dict]]:
        """插件页：只有保种组面板（辅种已经在另一个插件里）。"""
        if not self._session_token:
            captcha_id, image = self._fetch_captcha()
            if not image:
                reason = str(self.get_data("captcha_error") or "未知原因")
                return [
                    {"component": "div", "text": f"获取可乐PT 验证码失败：{reason}"},
                    {"component": "div", "text": "确认这台机器能打开 https://www.colapt.com（浏览器里试一下），"
                                                 "再重新打开插件页重试；若一直失败，把上面这行原因贴到 GitHub issue。"},
                ]
            return [
                {"component": "div", "text": f"可乐PT 登录：验证码 id {captcha_id}（4 位数字），填到插件设置「验证码」后保存。"},
                {"component": "VImg", "props": {"src": image, "max-width": 220, "class": "mt-3"}},
            ]
        last = self.get_data("last_run") or {}
        blocks: List[dict] = [
            {"component": "div", "text": f"插件版本 v{self.plugin_version}；已登录可乐PT。上次运行：{last.get('at', '未运行')}；"
                                         f"本轮新增 {last.get('added', 0)} 条，重复 {last.get('duplicate', 0)}，"
                                         f"失败 {last.get('failed', 0)}，覆盖 {last.get('covered_gib', 0)} GiB。"}
        ]
        blocks.insert(0, self._seedgroup_panel())
        return blocks


    # 站点验证码 10 分钟内有效；插件缓存 5 分钟 —— 否则每次打开/刷新插件页都会换一张，
    # 用户看到的那张立刻作废（「第一次看不清，再刷就不出来了」就是这个）。
    CAPTCHA_TTL = 300

    def _fetch_captcha(self, force: bool = False) -> Tuple[str, str]:
        cached = self.get_data("captcha_cache") or {}
        if (not force and cached.get("id") and cached.get("image")
                and 0 <= time.time() - int(cached.get("at") or 0) < self.CAPTCHA_TTL):
            return str(cached["id"]), str(cached["image"])
        captcha_id, image, reason = "", "", ""
        try:
            rsp = _curl("GET", f"{COLA_BASE}/api/v1/plugin/captcha", timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200:
                body = rsp.json() or {}
                captcha_id = str(body.get("captcha_id") or "")
                image = str(body.get("image") or "")
            else:
                reason = f"站点返回 HTTP {rsp.status_code}"
        except Exception as err:
            reason = f"{type(err).__name__}: {err}"
        if not image:
            reason = reason or "站点没有返回验证码图片"
            self.save_data("captcha_error", reason)
            logger.warn(f"获取可乐PT 验证码失败：{reason}")
            return "", ""
        self.save_data("captcha_error", "")
        self.save_data("captcha_id", captcha_id)
        self.save_data("captcha_cache", {"id": captcha_id, "image": image, "at": int(time.time())})
        return captcha_id, image

    def get_api(self) -> List[Dict[str, Any]]:
        return [
                        {
                "path": "/test",
                "endpoint": self.run_tests,
                "methods": ["GET", "POST"],
                "summary": "检测可乐/下载器/其他站连通性",
                "description": "返回可乐登录、每个下载器、每个其他站的连接结果；结果也会显示在插件页",
            },
            {
                "path": "/seedgroup/status",
                "endpoint": self.seed_group_status,
                "methods": ["GET", "POST"],
                "summary": "可乐保种组状态",
                "description": "返回档位、目标体积、实际覆盖、缺口、上次同步结果（面板同款数据）",
            },
            {
                "path": "/seedgroup/probe",
                "endpoint": self.seed_group_probe,
                "methods": ["GET", "POST"],
                "summary": "探测保种组权限 API",
                "description": "检查站点是否已开放保种组权限、当前账号的档位与配额（v1 契约见 docs/seed-group-api.md）",
            },
            {
                "path": "/seedgroup/sync",
                "endpoint": self.seed_group_sync,
                "methods": ["POST"],
                "summary": "立即同步一次保种组",
                "description": "按当前档位把官方资源池补齐到体积下限（只超不少），缺口会如实报出",
            },
                                    {
                "path": "/seedgroup/rotate",
                "endpoint": self.seed_group_rotate,
                "methods": ["POST"],
                "summary": "立即做一次动态轮换",
                "description": "体积达标后，删做种人数最多的 N 颗、补做种人数最少的 N 颗（默认 5）",
            },
            {
                "path": "/seedgroup/prune",
                "endpoint": self.seed_group_prune,
                "methods": ["POST"],
                "summary": "清理站点已下架的保种",
                "description": "逐个回站点核对 status/official，把已下架或撤掉官方标记的种子从下载器里删掉（可选同时删文件）",
            },
            {
                "path": "/seedgroup/identity",
                "endpoint": self.seed_group_identity,
                "methods": ["GET", "POST"],
                "summary": "查看当前账号的工作组资格",
                "description": "登录后读本人资料里的工作组头衔（组名以配置的前缀开头才算），返回是否可解锁",
            },
            {
                "path": "/seedgroup/unlock",
                "endpoint": self.seed_group_unlock,
                "methods": ["POST"],
                "summary": "手动解锁可乐保种组",
                "description": "校验工作组资格；通过则解锁 24 小时，非工作组账号强制拒绝（返回 ok=false 与原因）",
            },
        ]

    @staticmethod
    def _form_section(title: str, text: str) -> Dict[str, Any]:
        """设置页的分区标题：一条 tonal 提示条，比裸标题更容易一眼分段。"""
        return {"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                                                  "density": "compact", "title": title, "text": text}}]},
        ]}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """设置页排版：总开关 → 账号 → 下载器 → 站点 → 辅种行为 → 保种组。

        只重排、不改字段名：每个 `model` 都对应 init_plugin 读的那个键，defaults 与之一一对应。
        """
        return [
            {
                "component": "VForm",
                "content": [
                    # ── 总开关（放最上面，开关一关下面都不跑）──
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件（总开关）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "seedgroup_enabled",
                             "label": "启用保种组"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "notify", "label": "任务结果通知"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VAlert", "props": {
                                "type": "info", "variant": "tonal", "density": "compact",
                                "text": "两个功能互不影响，各有各的开关与运行间隔；"
                                        "「启用插件」是总开关，关掉后两个都不跑。"
                                        "异常与重新登录提醒始终发送，不受「任务结果通知」影响。"}}]},
                    ]},

                    # ── ① 账号 ──
                    self._form_section("① 可乐PT 账号",
                                       "第一次使用先打开插件页取验证码，填到「验证码」保存即完成登录；"
                                       "会话过期插件会发站内通知，重新取一次即可。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "username", "label": "用户名"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "password", "label": "密码", "type": "password"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "captcha_code",
                             "label": "验证码（先在插件页看图片）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "otp_code", "label": "两步验证码（可选）"}}]},
                    ]},

                    # ── ② 下载器 ──
                    self._form_section("② 下载器",
                                       "勾「使用 MoviePilot 已配置下载器」就用 MP 里的那几台；"
                                       "否则在下面按行填地址与账号。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "seedgroup_use_mp_clients",
                             "label": "保种用 MP 已配置下载器"}}]},
                    ]},

                    # ── ⑤ 可乐保种组 ──
                    self._form_section("⑤ 保种组（功能二，仅工作组可解锁）",
                                       "开关在页面顶部「启用保种组（功能二）」。按档位把站点官方资源池搬进下载器，"
                                       "体积只能超不能少；解锁资格只认站点工作组（登录后读本人资料里的工作组头衔），"
                                       "没有工作组的账号保存时会被强制关掉这一块，并写明原因。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VAlert", "props": {
                                "type": "warning", "variant": "tonal", "density": "compact",
                                "title": "用 MP 下载器保种，整理方式请用硬链接",
                                "text": "MP 的整理会改名、移动文件；源文件被移走之后做种的种子会变红、保种中断。"
                                        "建议在 MP「设置 → 目录/整理」里把整理方式设为硬链接（硬链接不占双份空间），"
                                        "或改用下面的「自定义下载器」专门保种。"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                            {"component": "VSelect", "props": {"model": "seedgroup_tier", "label": "保种档位", "items": [
                                {"title": SEED_GROUP_TIER_LABELS[k], "value": k}
                                for k in (list(SEED_GROUP_TIERS)
                                          + [SEED_GROUP_CUSTOM_TIER, SEED_GROUP_ALL_TIER])]}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "seedgroup_cron",
                             "label": "保种运行间隔（cron，5 段）", "persistent-hint": True,
                             "hint": "例如 0 */6 * * * = 每 6 小时；写错会静默按每 6 小时跑",
                             "placeholder": "0 */6 * * *"}},
                            {"component": "VTextField", "props": {"model": "seedgroup_max_downloading",
                             "label": "进种限流：同时下载不超过 N 个", "type": "number",
                             "persistent-hint": True,
                             "hint": "客户端里「正在下载」的种子达到这个数就不再进种，等下完再补；0 = 不限制。"
                                     "默认 5：让覆盖稳步追平，而不是一次塞几百颗互相抢带宽"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "seedgroup_custom_gib",
                             "label": "自定义体积（100M / 100G / 1T / 1P）",
                             "persistent-hint": True,
                             "hint": "仅选「自定义档」时生效；不带单位按 GiB"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSelect", "props": {"model": "seedgroup_client_mode", "label": "保种下载器", "items": [
                                {"title": "用 MP 已配置下载器", "value": "mp"},
                                {"title": "用下面填的自定义下载器", "value": "custom"}]}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "seedgroup_target_client",
                             "label": "保种加到哪台（留空=第一台）",
                             "placeholder": "MP-qb / QB-1 / TR-1"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "seedgroup_save_path",
                             "label": "保存路径（留空=下载器默认）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "seedgroup_skip_check",
                             "label": "保种组：跳过校验（新下载不要开）",
                             "persistent-hint": True,
                             "hint": "只在「文件已经在磁盘上、只是种子丢了」的恢复场景打开。"
                                     "保种是从站点新下载的，开着它 qB 会以为文件已完整，"
                                     "全部变成「丢失文件」、进度 0%、永远下不动"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSelect", "props": {"model": "seedgroup_prune_mode", "label": "下架清理（每轮回站点核对 status/official）", "items": [
                                {"title": "只删种子，保留文件（默认）", "value": "seed"},
                                {"title": "种子和文件一起删", "value": "files"},
                                {"title": "不清理", "value": "off"}]}}]},
                    ]},
                    self._form_section("⑤-2 保种限速（只作用于保种组）",
                                       "留空 = 完全不碰下载器原有设置；填了才强行接管，每轮保种同步都会重新下发。"
                                       "辅种不走这里，也不受这里影响。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "limit_dl_kib",
                             "label": "保种下载限速（KiB/s）", "type": "number", "min": 0,
                             "persistent-hint": True,
                             "hint": "留空/0 = 不接管下载"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "limit_up_kib",
                             "label": "保种上传限速（KiB/s）", "type": "number", "min": 0,
                             "persistent-hint": True,
                             "hint": "留空/0 = 不接管上传"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextarea", "props": {"model": "limit_lines", "rows": 3,
                             "persistent-hint": True,
                             "label": "每客户端单独限速（每行：客户端名或地址,下载KiB/s,上传KiB/s）",
                             "hint": "包含匹配：写 198.72 或 MP-qb 都能命中；没列出的客户端用上面的保种全局值",
                             "placeholder": "198.72.127.68:8081,1024,2048\nMP-qb,0,3000"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "seedgroup_rotate_count",
                             "label": "动态保种：每轮删/补数量（强制开启）", "type": "number", "min": 1,
                             "persistent-hint": True,
                             "hint": "达到档位后自动轮换：删做种人数最多的那几颗，补做种人数最少的同数量"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "seedgroup_rotate_files",
                             "label": "轮换时连文件一起删（不勾只删种子、保留文件）"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextarea", "props": {"model": "seedgroup_clients_lines", "rows": 3,
                             "persistent-hint": True,
                             "label": "保种组自定义下载器（仅「用下面填的自定义下载器」时生效）",
                             "hint": "每行 地址,用户名,密码；TR 写 tr,地址,用户名,密码",
                             "placeholder": "192.168.1.100:8080,admin,密码\ntr,192.168.1.101:9091,admin,密码"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": False, "notify": True,
            "username": "", "password": "", "otp_code": "", "captcha_code": "", "session_token": "",
            "skip_check": False, "max_per_hour": 0, "max_per_site": 20, "direction": "both",
            "use_mp_clients": True, "clients_json": "", "clients_lines": "",
            "sites_json": "", "sites_lines": "", "iyuu_json": "", "iyuu_token": "", "sites_mode": "merge",
            "add_label": "", "extra_tags": "", "inherit_tags": True, "target_client": "",
            "fallback_skip_check": True, "retry_failed": True,
            "limit_dl_kib": 0, "limit_up_kib": 0, "limit_lines": "",
            "seedgroup_enabled": False, "seedgroup_tier": "2T",
            "seedgroup_custom_gib": 2048, "seedgroup_scope": "site",
            "seedgroup_cron": "0 * * * *", "seedgroup_max_downloading": 5,
            "seedgroup_save_path": "", "seedgroup_skip_check": False,
            "iyuu_json_file": "",
            "seedgroup_client_mode": "mp", "seedgroup_prune_mode": "seed",
            "seedgroup_rotate": True, "seedgroup_rotate_count": 5, "seedgroup_rotate_files": True,
            "seedgroup_report": True, "seedgroup_report_url": SEED_GROUP_REPORT_URL,
            "seedgroup_report_token": "",
            "seedgroup_prune": True, "seedgroup_prune_files": False,
            "seedgroup_api_key": "", "seedgroup_staff_prefix": SEED_GROUP_STAFF_PREFIX,
            "seedgroup_allow_admin": True, "seedgroup_use_mp_clients": True,
            "seedgroup_clients_lines": "", "seedgroup_target_client": "",
        }


class _MPClient(_Client):
    """把 MoviePilot 的下载器模块包成统一接口。"""

    def __init__(self, name: str, list_func, add_func, delete_func=None, list_all_func=None):
        self.name = name
        self._list = list_func
        self._add = add_func
        self._list_all = list_all_func
        self._delete = delete_func

    def list_completed(self) -> List[Dict[str, Any]]:
        out = []
        try:
            for torrent in self._list() or []:
                # qBittorrent 模块给的是 dict，Transmission 给的是对象，这里统一。
                if isinstance(torrent, dict):
                    hash_str = torrent.get("hash") or torrent.get("hashString") or ""
                    name = torrent.get("name") or ""
                    save_path = torrent.get("save_path") or torrent.get("download_dir") or ""
                    category = torrent.get("category") or ""
                    size = torrent.get("size") or torrent.get("total_size") or 0
                    tags = torrent.get("tags") or torrent.get("tag") or ""
                else:
                    hash_str = getattr(torrent, "hash", None) or getattr(torrent, "hashString", "") or ""
                    name = getattr(torrent, "name", "") or ""
                    save_path = getattr(torrent, "save_path", "") or getattr(torrent, "download_dir", "") or ""
                    category = getattr(torrent, "category", "") or ""
                    size = getattr(torrent, "size", 0) or 0
                    tags = getattr(torrent, "tags", "") or getattr(torrent, "tag", "") or ""
                state = torrent.get("state", "") if isinstance(torrent, dict) else getattr(torrent, "state", "")
                if not hash_str:
                    continue
                if isinstance(tags, (list, tuple)):
                    tags = ",".join(str(t) for t in tags)
                out.append({"hash": str(hash_str).lower(), "name": name,
                            "save_path": save_path, "category": category, "size": size,
                            "tags": str(tags or ""), "state": str(state or "")})
        except Exception as err:
            logger.error(f"MP 下载器 {self.name} 读取失败：{err}")
            # 与其他两条链路一致：读失败就抛，别让上层把它当成「客户端里一颗都没有」。
            raise
        return out

    def has(self, info_hash: str) -> bool:
        """MP 客户端没有现成的「有没有这颗」接口，用扫描结果做一次短缓存。

        list_completed() 现在读失败会抛异常（为了不让上层把「读不到」当成「空客户端」）。
        但这个方法是在加种循环里被调用的，**抛出去会让整轮崩掉** —— 所以这里自己咽下来、
        答 False：最坏结果是多下一次 .torrent（客户端回 dup / Fails，流程照旧能走完），
        比整轮中断好得多。
        """
        if not info_hash:
            return False
        cached = getattr(self, "_hash_cache", None)
        if cached is None or time.time() - getattr(self, "_hash_cache_at", 0) > 30:
            try:
                cached = {t.get("hash") for t in self.list_completed() if t.get("hash")}
            except Exception as err:
                logger.warn(f"MP 下载器 {self.name} 读不到种子列表，本轮按「没有」处理：{err}")
                return False
            self._hash_cache, self._hash_cache_at = cached, time.time()
        return str(info_hash).lower() in cached

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "",
            hash_hint: str = "", category: str = "", auto_tmm: bool = False) -> str:
        """MP 的下载器模块把多余参数透传给 qB/TR，这里必须显式要求「跳过校验」和标签。

        之前只传了 content/download_dir —— 于是「跳过校验」形同虚设（每次都要重校验），
        标签也全丢（补进来的种子在 qB 里是「无标签」）。
        """
        kwargs: Dict[str, Any] = {"content": torrent, "download_dir": save_path}
        kwargs["skip_checking"] = bool(skip_check)
        if label:
            kwargs["tag"] = label
        if category:
            # 分类单独传，别混进标签；qB 开着自动管理时路径由它决定（issue #23）
            kwargs["category"] = category
        try:
            res = self._add(**kwargs)
        except TypeError:
            # 老版本 MP 的 add_torrent 不认 skip_checking/tag：退回最小参数，至少能加上
            try:
                res = self._add(content=torrent, download_dir=save_path)
            except Exception as err:
                logger.error(f"MP 下载器 {self.name} 添加失败：{err}")
                return "fail"
        except Exception as err:
            logger.error(f"MP 下载器 {self.name} 添加失败：{err}")
            return "fail"
        ok = res[0] if isinstance(res, tuple) else res
        # MP 的下载器模块对重复种子一般回 False 或者抛异常，拿不到更细的原因；
        # 有 hash 时再问一次客户端，能区分「已经在」和「真失败」。
        if not ok and hash_hint and self.has(hash_hint):
            return "dup"
        if ok:
            self._hash_cache = None   # 加进去了，缓存作废
        return "ok" if ok else "fail"
