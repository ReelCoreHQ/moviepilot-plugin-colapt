"""
可乐PT 自动辅种（MoviePilot 插件）

逻辑（2026-10-25 定稿）：
  1. 扫用户配置的 qBittorrent / Transmission（可多个，也可用 MoviePilot 已配置的默认下载器）里**已完成**的种子；
  2. 把这些种子的 info_hash 批量交给可乐PT 开放 API 的哈希反查（POST /api/v1/torrents/hashes）：
       - via=source_info_hash：客户端里有同一份文件（来源是别的站），拉可乐这颗种子回原下载器辅种；
       - via=info_hash：客户端里已经是可乐这颗种子（跳过）；
  3. 可选方向二：对 via=info_hash 的可乐种子，下载可乐的 .torrent 算出 pieces_hash，
     去用户配置的其他 NexusPHP 站点 /api/pieces-hash 查同 pieces 的种子，命中就拉对方种子回来辅种；
  4. 每小时最多添加 N 个（默认 20，滚动 60 分钟窗口），是否跳过校验由用户设置（默认校验）。

凭据：可乐PT 开放 API 的 API Key（站点「账号 → Key → API Key」创建，勾「读取」+「下载」），
只放在请求头 X-API-Key 里；不再用账号密码 + 验证码登录，也不带 Cookie / CSRF / Origin。
Key 来源二选一：① 从 MP「站点管理」里可乐站的 apikey 字段读取（只需在设置里选站点 id，
换 Key 后自动跟上）；② 插件设置里手填兜底。接口说明见站点的 /api-docs。
"""
import base64
import hashlib
import json
import re
import subprocess
import time
from urllib.parse import urlencode, urljoin
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
COLA_MATCH_BATCH = 500   # matchTorrentHashes 每种 key 一次最多 500 条，一次调用只计一次读预算
# 可乐PT 开放 API（契约里带 x-open-api 的操作，文档在站点 /api-docs）
COLA_MATCH_API = "/api/v1/torrents/hashes"            # matchTorrentHashes（read）
COLA_STATS_API = "/api/v1/me/stats"                   # personalStats（read）
COLA_DOWNLOAD_LINK_API = "/api/v1/torrents/{id}/download-links"   # createDownloadLink（download）
COLA_KEY_PAGE = "https://www.colapt.com/account/tracker?tab=api"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
# colapt.com 在部分国内网络会被解析到连不通的 Cloudflare 段；这些是实测可用的备用段。
COLA_FALLBACK_IPS = ("104.21.66.130", "172.67.132.88", "104.26.10.0")
REQUEST_TIMEOUT = 20
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

SIZE_UNITS_GIB = {"M": 1.0 / 1024, "G": 1.0, "T": 1024.0, "P": 1024.0 * 1024}


def parse_size_gib(value: Any) -> float:
    """把「100M / 100G / 1T / 1P / 2048」解析成 GiB。

    不带单位的数字按 GiB 理解（例如 2048）；
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
    站点的哈希反查（matchTorrentHashes）只按 info_hash / source_info_hash 反查，结果就是「内容一模一样
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

    def list_completed(self) -> List[Dict[str, Any]]:
        try:
            rsp = self._session.get(
                f"{self.url}/api/v2/torrents/info",
                params={"filter": "completed"},
                timeout=REQUEST_TIMEOUT,
            )
            rsp.raise_for_status()
            out = []
            for item in (rsp.json() or []):
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
            return out
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 读取种子失败：{err}")
            return []

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
            return []
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


class ColaCrossSeed(_PluginBase):
    plugin_name = "可乐PT辅种"
    plugin_desc = "参考 ReseedPuppy / IYUU 辅种插件：把客户端里已有的资源与可乐PT 对照，存在就拉可乐种子自动辅种；反向也支持去别的站补种。"
    plugin_icon = "https://www.colapt.com/brand/cola/app-red-180.png"
    plugin_version = "0.13.0"
    plugin_author = "ColaPT"
    author_url = "https://www.colapt.com"
    plugin_config_prefix = "colacrossseed_"
    plugin_order = 21
    auth_level = 1

    _enabled = False            # MP 插件总开关：关掉后辅种不跑
    _crossseed_enabled = True   # 辅种开关（保种组已拆到独立插件）
    _onlyonce = False           # 参考 iyuuautoseed：勾一次跑一轮，保存后自动复位
    _notify = True
    _cron = "0 0 * * *"   # 默认每日 00:00 跑一次
    _api_key = ""            # 可乐PT 开放 API 的 Key（read + download），只放在 X-API-Key 请求头里
    _mp_site_id = ""         # MP 站点管理里可乐站的 id（只存 id，apikey 每次初始化现读）
    _key_alerted = False     # 本次加载已经提醒过「Key 不能用」，不每个请求都发一条
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
    _iyuu_token = ""               # IYUU 云端匹配 Token（可选）
    _mp_site_ids: List[Any] = []   # MP 站点管理里勾选的辅种站点 id（只存 id，凭据每次现读）
    _add_label = ""
    _target_client = ""       # 手填的辅种目标下载器名（与下面多选合并）；留空=自动（优先正在做种的那台）
    _mp_target_clients: List[str] = []  # 从 MP 已配置下载器里多选的目标（只存名字，与手填合并）
    _target_state: Dict[str, Any] = {}   # 上一轮「目标下载器解析结果」，页面与日志共用
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
    # 保种范围：site = 全站官方种（默认）；pool = 站点保种组专用池（未开放时回落到全站官方种）
    # 保种组自己的下载器：默认用 MP 已配置的那台（MP 会按自己的规则刮削/整理），
    # 也可以单独填 qB/TR（只给保种组用，不影响辅种的客户端）
    # 下架核对的最小间隔：它每颗种子发一次 GET，cron 调到每小时之后仍按这个节奏跑
    # 每轮最多对多少颗「丢失文件」的保种种子提交强制校验（recheck 很吃磁盘 IO）
    # 进种限流：客户端里「正在下载」的种子数不允许超过这个值，满了本轮就不再进种。
    # 目的：让覆盖慢慢追平目标，而不是一次性把几百颗塞进客户端抢带宽。
    # 保种组默认**必须校验**：这些种子是从站点新下载的，文件还不在磁盘上。
    # 开着「跳过校验」qB 会认为文件已完整，立刻变 missingFiles（丢失文件）、进度 0%、永不下载 ——
    # 2026-11-16 实测就是这条把保种卡成「覆盖不涨」。跳过校验只留给「文件已在磁盘、种子丢了」的恢复。
    # 下架清理：off=不清 / seed=只删种子（默认）/ files=连文件一起删
    # 动态保种：体积达标后，删「做种人数最多」的、补「做种人数最少」的
    # 限速情况上报：只统计「开了保种组」的用户，辅种用户不采集、不上报
    _event = Event()
    # host -> 可用的 pieces-hash 路径（憨憨是 /nexusapi/pieces-hash，其余多为 /api/pieces-hash）；"" 表示不支持
    _pieces_paths: Dict[str, str] = {}
    _iyuu_rate_limited = False

    def init_plugin(self, config: dict = None):
        self.stop_service()
        onlyonce_requested = False
        if config is not None:
            self._enabled = bool(config.get("enabled", False))
            self._crossseed_enabled = bool(config.get("crossseed_enabled", True))
            self._onlyonce = bool(config.get("onlyonce", False))
            onlyonce_requested = self._onlyonce
            self._notify = bool(config.get("notify", True))
            self._cron = str(config.get("cron") or "0 0 * * *").strip()
            self._api_key = str(config.get("api_key") or "").strip()
            # 统一 str：老配置里可能存的是 int（早期下拉 value 是 int），
            # 而选项 value 现在是 str，类型不一致会让 VSelect 回显成裸数字 ID。
            self._mp_site_id = str(config.get("mp_site_id") or "").strip()
            # MP 站点管理里填了可乐站的 API Key 时优先用它：用户在站点管理里换 Key，
            # 插件下次保存/重载自动跟上，不必再来插件设置里改一遍。手填的 api_key 作兜底。
            if self._mp_site_id:
                mp_key = self._api_key_from_mp_site()
                if mp_key:
                    self._api_key = mp_key
                else:
                    logger.warn("MP 站点管理里选的可乐站没读到 API Key，退回插件设置里手填的 Key")
            self._key_alerted = False
            self._skip_check = bool(config.get("skip_check", False))
            self._direction = str(config.get("direction") or "both")
            self._use_mp_clients = bool(config.get("use_mp_clients", True))
            self._clients_json = str(config.get("clients_json") or "")
            self._clients_lines = str(config.get("clients_lines") or "")
            self._iyuu_token = str(config.get("iyuu_token") or "")
            # 多选同样归一化成 str：老配置里的 id 可能是 int，选项 value 已是 str，
            # 不转的话 VSelect 严格匹配不上，回显退化成数字 ID。去重保序。
            self._mp_site_ids = []
            for _sid in (config.get("mp_site_ids") or []):
                _s = str(_sid).strip()
                if _s and _s not in self._mp_site_ids:
                    self._mp_site_ids.append(_s)
            self._add_label = str(config.get("add_label") or "").strip()
            self._target_client = str(config.get("target_client") or "").strip()
            # MP 下载器多选：只存名字（MP-xxx），与手填 target_client 合并成最终目标集合
            self._mp_target_clients = [str(x).strip() for x in (config.get("mp_target_clients") or []) if str(x).strip()]
            self._inherit_tags = bool(config.get("inherit_tags", True))
            self._extra_tags = str(config.get("extra_tags") or "").strip()
            self._fallback_skip_check = bool(config.get("fallback_skip_check", True))
            self._retry_failed = bool(config.get("retry_failed", True))
            # 这两个上限以前只写回配置、没在 init 里读回来 —— 表单改了不生效（2026-11-23 修）。
            try:
                self._max_per_hour = max(0, int(config.get("max_per_hour", 0) or 0))
                self._max_per_site = max(1, int(config.get("max_per_site", 20) or 20))
            except Exception:
                self._max_per_hour = 0
                self._max_per_site = 20
            try:
                self._limit_dl_kib = max(0, min(10 ** 7, int(config.get("limit_dl_kib", 0) or 0)))
                self._limit_up_kib = max(0, min(10 ** 7, int(config.get("limit_up_kib", 0) or 0)))
            except Exception:
                self._limit_dl_kib = 0
                self._limit_up_kib = 0
            self._limit_lines = str(config.get("limit_lines") or "")
        self._event.clear()
        import threading
        if onlyonce_requested:
            # 和官方 iyuuautoseed 一样：勾一次只跑一轮，马上把开关写回关，
            # 否则每次点保存都会再触发一次。
            self._onlyonce = False
            self._persist()
            # 和官方 iyuuautoseed 一样：onlyonce 无视启用总开关（get_state() or _onlyonce），
            # 关着总开关也能手动跑一轮。API Key 是唯一硬性前提——没有它脚本无权调用可乐开放 API。
            if self._api_key:
                logger.info("可乐PT辅种：立即运行一次已触发（无视启用开关），3 秒后开始")
                threading.Thread(target=self._run_once, daemon=True).start()
            else:
                logger.warn("已勾「立即运行一次」，但没有 API Key，跳过本轮")
        # 保存后自动检测一次连通性（后台线程，不卡保存）；结果显示在插件页。
        # 连通性检测只是读状态、刷新报告，不是辅种动作，不该被总开关挡住——
        # 否则用户关着总开关时，老版本的旧报告会永远留在页面上刷不掉。
        if self._api_key:
            threading.Thread(target=self.run_tests, daemon=True).start()

    def _persist(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "crossseed_enabled": self._crossseed_enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "cron": self._cron,
            "api_key": self._api_key,
            "mp_site_id": self._mp_site_id,
            "skip_check": self._skip_check,
            "max_per_hour": self._max_per_hour,
            "max_per_site": self._max_per_site,
            "direction": self._direction,
            "use_mp_clients": self._use_mp_clients,
            "clients_json": self._clients_json,
            "clients_lines": self._clients_lines,
            "iyuu_token": self._iyuu_token,
            "mp_site_ids": self._mp_site_ids,
            "add_label": self._add_label,
            "target_client": self._target_client,
            "mp_target_clients": self._mp_target_clients,
            "inherit_tags": self._inherit_tags,
            "extra_tags": self._extra_tags,
            "fallback_skip_check": self._fallback_skip_check,
            "retry_failed": self._retry_failed,
            "limit_dl_kib": self._limit_dl_kib,
            "limit_up_kib": self._limit_up_kib,
            "limit_lines": self._limit_lines,
        })


    # ---------------- MP 站点管理：读取已配置的 API Key ----------------

    def _api_key_from_mp_site(self) -> str:
        """从 MP 站点管理读取勾选站的 apikey（站点签发的 Key，MP 透明存的列）。

        取法与 MP 主程序调用站点开放 API 一致：
          1) SitesHelper().get_indexer(domain) 适配器配置里的 apikey；
          2) 退回站点对象的 apikey 字段。
        读不到返回空串，调用方回退到手填的 api_key —— 不伪造、不猜测。
        """
        if not self._mp_site_id:
            return ""
        try:
            from app.db.site_oper import SiteOper
            rows = SiteOper().list_order_by_pri() or []
        except Exception as err:
            logger.warn(f"读取 MoviePilot 站点失败：{err}")
            return ""
        site = None
        for row in rows:
            if str(getattr(row, "id", "")) == str(self._mp_site_id):
                site = row
                break
        if site is None:
            return ""
        domain = str(getattr(site, "domain", "") or "").strip()
        key = ""
        if domain:
            try:
                from app.helper.sites import SitesHelper
                indexer = SitesHelper().get_indexer(domain) or {}
                if isinstance(indexer, dict):
                    key = str(indexer.get("apikey") or "").strip()
            except Exception:
                pass
        if not key:
            key = str(getattr(site, "apikey", "") or "").strip()
        return key

    # ---------------- 可乐PT 开放 API / 匹配 / 下载 ----------------

    def _key_alert(self, text: str) -> None:
        """Key 不能用时提醒一次（每次加载插件最多一条），别让每个请求都刷一条通知。"""
        if self._key_alerted:
            return
        self._key_alerted = True
        try:
            self.post_message(mtype=NotificationType.SiteMessage, title="【可乐PT辅种】API Key 不能用", text=text)
        except Exception:
            pass

    @staticmethod
    def _error_code(rsp: Optional[_HttpResult]) -> str:
        """站点错误信封 {"error": {"code": ...}} 里的 code；不是这个形状就返回空串。"""
        if rsp is None:
            return ""
        body = rsp.json({}) or {}
        error = body.get("error") if isinstance(body, dict) else None
        return str(error.get("code") or "") if isinstance(error, dict) else ""

    def _cola(self, method: str, path: str, **kwargs) -> Optional[_HttpResult]:
        """调可乐PT 开放 API：只带 X-API-Key，不带 Cookie / CSRF / Origin（带了反而会被拒）。

        没填 Key 或网络失败返回 None；其余一律把响应交回调用方按状态码处理。
        """
        if not self._api_key:
            return None
        headers = kwargs.pop("headers", {})
        headers["X-API-Key"] = self._api_key
        headers.setdefault("Accept", "application/json")
        try:
            rsp = _curl(method, f"{COLA_BASE}{path}", headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
        except Exception as err:
            logger.error(f"可乐PT 请求失败 {path}：{err}")
            return None
        if rsp.status_code == 401:
            logger.warn(f"可乐PT {path}：API Key 无效、已撤销或已过期")
            self._key_alert(f"API Key 无效、已撤销或已过期。若用「MP 站点管理」读取，请更新该站点的 API Key 后重新保存插件；"
                            f"手填的话到 {COLA_KEY_PAGE} 新建一把勾「读取」「下载」的 Key 再填进插件设置。")
        elif rsp.status_code == 403 and self._error_code(rsp) == "api_key_scope_denied":
            logger.warn(f"可乐PT {path}：这把 API Key 缺少需要的范围")
            self._key_alert(f"这把 API Key 缺少需要的范围：辅种要「读取」（查哈希）和「下载」（取种子）两项。"
                            f"请到 {COLA_KEY_PAGE} 新建一把两项都勾上的 Key。")
        elif rsp.status_code not in (200, 201):
            logger.warn(f"可乐PT {path} 返回 {rsp.status_code}：{rsp.text[:160]}")
        return rsp

    def _lookup(self, field: str, values: List[str]) -> List[Dict[str, Any]]:
        """matchTorrentHashes：按 field（info_hashes / content_keys）分批反查，返回每批的响应体。

        只发 40 位十六进制：一条格式不对，站点会把整批答 400。
        """
        clean = sorted({str(v or "").strip().lower() for v in values} - {""})
        wanted = [v for v in clean if _HEX40.match(v)]
        if len(wanted) < len(clean):
            logger.info(f"{len(clean) - len(wanted)} 个不是 40 位十六进制（如 v2 种子），不参与反查")
        bodies: List[Dict[str, Any]] = []
        for start in range(0, len(wanted), COLA_MATCH_BATCH):
            chunk = wanted[start:start + COLA_MATCH_BATCH]
            rsp = self._cola("POST", COLA_MATCH_API, json_body={field: chunk})
            if rsp is None:
                logger.warn("可乐PT 哈希反查失败（网络或未填 API Key），跳过这批")
                continue
            if rsp.status_code != 200:
                logger.warn(f"可乐PT 哈希反查返回 {rsp.status_code}，跳过这批")
                continue
            body = rsp.json({}) or {}
            if isinstance(body, dict):
                bodies.append(body)
        return bodies

    def _match(self, hashes: List[str]) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
        """批量 match，返回 client_hash -> [匹配行]（同一 hash 可能同时命中 info_hash 与 source_info_hash）。"""
        by_hash: Dict[str, List[Dict[str, Any]]] = {}
        for body in self._lookup("info_hashes", hashes):
            for item in body.get("matched") or []:
                by_hash.setdefault(item.get("client_hash", ""), []).append(item)
        return by_hash, ""

    SOURCE_VARIANT_LIMIT = 400   # 一轮最多为多少个未命中的种子导出 .torrent 做归一化补查


    def _match_content(self, keys: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """按「文件清单指纹」反查站点（matchTorrentHashes 的 content_keys）。"""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for body in self._lookup("content_keys", keys):
            for row in body.get("content") or []:
                key = (row.get("client_key") or "").strip().lower()
                if key:
                    out.setdefault(key, []).append(row)
        return out

    @staticmethod
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

    def _match_with_source_variants(self, client: "_Client", torrents: List[Dict[str, Any]],
                                    hashes: List[str],
                                    by_hash: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
        """哈希反查没中的，再按「换掉 info.source 后」的哈希补查一遍。

        为什么需要这一步（2026-11-02 实例）：同一颗 `凡人修仙传 S01E198 2160p-UBWEB`，
        客户端是从 M-Team 下的（info.source = `[kp.m-team.cc] M-Team - TP`），可乐站上那份
        只是把 source 盖成了自己的章 —— files/name/pieces/private 全部逐字段相同，
        但 info_hash 一个是 9270936b…、一个是 63d0bcd2…，站点按哈希反查自然匹配不到。
        归一化后算出来的哈希与可乐的 info_hash 完全相等，命中就是真命中（文件逐字节一致）。
        """
        missing = [h for h in hashes if h not in by_hash]
        if not missing:
            return by_hash
        by_hash_size = {h: int(t.get("size") or 0) for h in hashes
                        for t in [next((x for x in torrents if x.get("hash") == h), {})]}
        alias: Dict[str, str] = {}
        keys: Dict[str, str] = {}
        for info_hash in missing[:self.SOURCE_VARIANT_LIMIT]:
            data = client.export(info_hash)
            if data:
                for variant in _torrent_info_hash_variants(data):
                    if variant != info_hash and variant not in by_hash and variant not in alias:
                        alias[variant] = info_hash
                key = self._export_content_key(data)
            else:
                # 这个下载器导不出 .torrent（Transmission / 老 qB）：退回文件清单接口，
                # 只算内容键 —— 文件清单指纹不依赖原始种子字节，照样能跨站对上。
                name = next((x.get("name") or "" for x in torrents if x.get("hash") == info_hash), "")
                key = self._client_content_key(name, client.files(info_hash))
            if key and key not in keys:
                keys[key] = info_hash
        found = 0
        if alias:
            extra, _ = self._match(sorted(alias))
            for variant, rows in extra.items():
                owner = alias.get(variant)
                if not owner:
                    continue
                for row in rows:
                    hit = dict(row)
                    hit["client_hash"] = owner
                    # 客户端手里是「同一份文件的另一份种子」，可乐这边才是缺的一侧
                    hit["via"] = "source_info_hash"
                    hit["matched_by"] = "source_normalized"
                    by_hash.setdefault(owner, []).append(hit)
                    found += 1
            if found:
                logger.info(f"info.source 归一化后补中 {found} 个（改 source 再按 info_hash 反查）")
        if keys:
            hit_count = 0
            for key, rows in self._match_content(sorted(keys)).items():
                owner = keys.get(key)
                if not owner:
                    continue
                for row in rows:
                    size = int(row.get("size_bytes") or 0)
                    if by_hash_size.get(owner) and size and size != by_hash_size[owner]:
                        continue  # 字节数不同不可能是同一份文件
                    hit = dict(row)
                    hit["client_hash"] = owner
                    hit["via"] = "source_info_hash"
                    hit["matched_by"] = "content_key"
                    by_hash.setdefault(owner, []).append(hit)
                    hit_count += 1
            if hit_count:
                logger.info(f"内容键（文件清单指纹）补中 {hit_count} 个")
        return by_hash


    def _download_cola(self, torrent_id: int) -> bytes:
        """取可乐的种子：createDownloadLink 签一条 30 分钟的直链，再不带任何凭据 GET 它。"""
        rsp = self._cola("POST", COLA_DOWNLOAD_LINK_API.format(id=int(torrent_id)), json_body={})
        if rsp is None or rsp.status_code not in (200, 201):
            return b""
        url = str((rsp.json({}) or {}).get("download_url") or "")
        if not url.startswith("http"):
            logger.warn(f"可乐PT 下载地址响应缺少 download_url（种子 {torrent_id}）")
            return b""
        try:
            # 签名直链本身就是凭据：不带 X-API-Key，免得 Key 跟着 30 分钟的链接出现在别处的日志里
            got = _curl("GET", url, timeout=REQUEST_TIMEOUT)
        except Exception as err:
            logger.error(f"可乐PT 取种子失败（{torrent_id}）：{err}")
            return b""
        if got.status_code != 200:
            logger.warn(f"可乐PT 取种子返回 {got.status_code}（{torrent_id}）")
            return b""
        return got.content if _valid_torrent(got.content) else b""

    def _site_lookup(self, site: Dict[str, Any], pieces_hash: str) -> Tuple[Optional[str], str]:
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        cached = self._pieces_paths.get(host, None)
        if cached == "":
            return None, "站点不支持 pieces-hash 接口"
        paths = [cached] if cached else ["/api/pieces-hash", "/nexusapi/pieces-hash"]
        last = ""
        for path in paths:
            url = (site.get("url") or "").rstrip("/") + path
            try:
                # Accept: application/json 是必须的：麒麟(hdkyl) 这类 Laravel 站在没有它时
                # 会把未认证/异常请求 302 回首页，插件拿到的是一张 HTML 页而不是 JSON。
                rsp = _site_request("POST", url, json={"passkey": site.get("passkey", ""), "pieces_hash": [pieces_hash]},
                                    headers=_site_headers(site, accept="application/json"),
                                    proxy=site.get("proxy") or "")
            except Exception as err:
                last = str(err)
                continue
            if rsp.status_code == 404 or rsp.status_code == 405:
                last = f"HTTP {rsp.status_code}（无此接口）"
                continue
            if rsp.status_code != 200:
                last = f"HTTP {rsp.status_code}"
                continue
            try:
                data = ((rsp.json() or {}).get("data") or {})
            except Exception:
                last = "返回不是 JSON"
                continue
            if not isinstance(data, dict):
                # 有的站点「没命中」时把 data 序列化成 []（PHP 的空数组），按空结果处理
                if isinstance(data, list) and not data:
                    data = {}
                else:
                    last = "返回格式不匹配"
                    continue
            self._pieces_paths[host] = path
            return data.get(pieces_hash), ""
        # 只有 404/405 才算“不支持”；超时/500 下次还会重试
        if last.startswith("HTTP 404") or last.startswith("HTTP 405"):
            self._pieces_paths[host] = ""
        return None, last

    def _site_download(self, site: Dict[str, Any], torrent_id: str) -> bytes:
        """按站点自己的下载模板取种，不是所有站都用 download.php?id&passkey。

        实测（2026-11-01，老板 NAS）：
          · 家园 hdhome  : download.php?id={}&uid={uid}&hash={hash}（32 位 passkey 会回 "Old Passkey no Support"）
          · 听听歌 ttg   : dl/{id}/{passkey}
          · 春天 springsunday / 观众 / 大多数 NexusPHP : download.php?id={}&passkey={passkey}
        模板来自 IYUU 的站点导出（download_page 字段）；再加上站点自己的 Cookie，这三家都能拿到种子。
        """
        candidates = [self._rss_url_for(site, str(torrent_id)), self._site_torrent_url(site, str(torrent_id))]
        if site.get("cookie"):
            # 有登录态时 download.php?id=N 往往直接给种（家园就是这样），模板反而会把参数拼错
            candidates.append(urljoin(site["url"], f"download.php?id={torrent_id}"))
        url = ""
        for candidate in candidates:
            if not candidate or candidate == url:
                continue
            url = candidate
            try:
                rsp = _site_request("GET", url, headers=_site_headers(site, accept="application/x-bittorrent, */*"),
                                    proxy=site.get("proxy") or "")
                if rsp.status_code == 200 and _valid_torrent(rsp.content):
                    return rsp.content
            except Exception as err:
                logger.warn(f"站点 {site.get('name')} 下载种子失败：{err}")
                continue
            logger.warn(f"站点 {site.get('name')} 下载失败：HTTP {rsp.status_code} {len(rsp.content)} 字节（{url[-60:]}）")
            continue
        return b""

    # ---------------- 配置解析（给不写代码的人用行文本） ----------------

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
        （2026-11-19 事故：`地址,用户,密码,名称` 被切成 name=地址、url=用户，
        报 `Failed to resolve 'a172604'`）。
        """
        t = str(token or "").strip().lower()
        if not t:
            return False
        if t.startswith(("http://", "https://")):
            return True
        if "/" in t or " " in t:
            return False
        if ":" in t:
            return True
        # 只认带点的（IPv4 / 域名）或 localhost：纯主机名与用户名/密码无法区分
        return "." in t or t == "localhost"

    def _parse_client_lines(self, text: str) -> List[_Client]:
        """每行一个客户端，**地址按内容识别，不靠位置**：

            `地址,用户名,密码`
            `地址,用户名,密码,名称`          ← 名称在后（最常见）
            `名称,地址,用户名,密码`          ← 名称在前也认
            `qb|tr,地址,用户名,密码[,名称]`   ← 显式指定类型
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

    # ---- 下载器名字匹配 ----

    @staticmethod
    def _client_aliases(name: str) -> List[str]:
        """一个下载器可能被用户写成的几种名字。

        MP 枚举出来的实例带 `MP-` 前缀（`MP-tr辅种`），而用户在设置里只会写
        `tr辅种`、`tr`。以前是 `c.name == wanted` 精确比，于是「辅种加到哪台」
        填什么都匹配不上，静默退回自动判断 —— 表现为「这个功能不生效」。
        """
        raw = str(name or "").strip()
        if not raw:
            return []
        out = [raw, raw.lower()]
        if raw.lower().startswith("mp-"):
            out += [raw[3:], raw[3:].lower()]
        host = raw.split("://", 1)[-1].strip("/")
        if host and host != raw:
            out += [host, host.lower()]
        # 去重但保持顺序
        seen, uniq = set(), []
        for x in out:
            if x and x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    @staticmethod
    def _normalise_target(raw: str) -> str:
        """用户填的名字：去空白（含全角空格）、去包裹引号。"""
        t = str(raw or "").replace("\u3000", " ").strip()
        for q in ('"', "'", "`", "\u201c", "\u201d", "\u2018", "\u2019"):
            if len(t) >= 2 and t.startswith(q) and t.endswith(q):
                t = t[1:-1].strip()
        return t

    def _resolve_target_clients(self, raw: str, clients: List["_Client"]) -> List["_Client"]:
        """把用户填的「辅种加到哪台」解析成实际客户端。

        匹配优先级：完全一致 → 忽略大小写 → 去掉 `MP-` 前缀后一致 → **唯一**包含。
        - 不做反向包含：`a in wanted` 会让短客户端名吞掉长输入，匹配到没点名的机器；
        - 不做单字符模糊：`t` 会命中一整片；
        - 多命中一律判歧义（返回空，让用户写全名）—— 猜错就把种子推到别的机器上，
          宁可这一轮不加。
        """
        wanted = self._normalise_target(raw)
        self._target_ambiguous = []
        if not wanted:
            return []
        low = wanted.lower()
        exact = [c for c in clients if c.name == wanted or c.name.lower() == low]
        if len(exact) > 1:
            # 两台下载器同名（或只差大小写）：用户写全名也无法区分，不能替他挑一台
            self._target_ambiguous = [c.name for c in exact]
            return []
        if exact:
            return exact
        alias = [c for c in clients
                 if any(a.lower() == low for a in self._client_aliases(c.name))]
        if len(alias) > 1:
            self._target_ambiguous = [c.name for c in alias]
            return []
        if alias:
            return alias
        if len(low) < 2:
            return []
        fuzzy = [c for c in clients
                 if any(low in a.lower() for a in self._client_aliases(c.name))]
        if len(fuzzy) == 1:
            return fuzzy
        if len(fuzzy) > 1:
            self._target_ambiguous = [c.name for c in fuzzy]
        return []


    # ---------------- MP 站点管理：辅种站点下拉与读取 ----------------

    @staticmethod
    def _passkey_from_rss(rss: str) -> str:
        """从站点 RSS 地址里抠 passkey（MP v2 的 Site 模型没有独立 passkey 列）。"""
        match = re.search(r"[?&](?:passkey|pass_key)=([0-9A-Za-z]{16,64})", str(rss or ""))
        return match.group(1) if match else ""

    def _mp_site_options(self) -> List[Dict[str, Any]]:
        """get_form 的辅种站点下拉：title=站点名、value=站点 id（与 iyuuautoseed 官方一致）。"""
        try:
            from app.db.site_oper import SiteOper
            rows = SiteOper().list_order_by_pri() or []
        except Exception as err:
            logger.warn(f"读取 MoviePilot 站点列表失败：{err}")
            return []
        options: List[Dict[str, Any]] = []
        for site in rows:
            # value 必须统一成 str：配置里 _mp_site_id/_mp_site_ids 都是 str（见 init_plugin），
            # 而 SiteOper 的 site.id 是 int。VSelect 做严格类型匹配，int 的 value 配上 str 的
            # 选中值会匹配不上，回显时就退化成直接显示原始数字（例如「61」）。
            sid = str(getattr(site, "id", "") or "").strip()
            name = str(getattr(site, "name", "") or getattr(site, "domain", "") or sid)
            if not sid or not name:
                continue
            options.append({"title": name, "value": sid})
        return options

    def _iyuu_site_index(self) -> Dict[str, Dict[str, Any]]:
        """用 Token 拉 IYUU 全站索引：{sid: {base_url, download_page, ...}}。

        IYUU 命中自带 sid，这里按 sid 反查站点地址与下载模板；MP 站点下拉不参与 sid 推导。
        没有 Token / 请求失败就返回空，不影响 pieces-hash / RSS 两条本地通道。
        """
        if not self._iyuu_token:
            return {}
        cached = self.get_data("iyuu_site_index") or {}
        stamp = int(cached.get("at") or 0)
        if cached.get("sites") and time.time() - stamp < 24 * 3600:
            # 持久化层可能把 int key 变成 str；命中查询一律按 int sid 取，这里统一归一化。
            sites: Dict[int, Dict[str, Any]] = {}
            for key, value in (cached.get("sites") or {}).items():
                sid = _as_int(key)
                if sid:
                    sites[sid] = value
            return sites
        try:
            rsp = requests.get(f"{self.IYUU_API}/reseed/sites/index",
                               headers=self._iyuu_headers(), timeout=30)
            body = rsp.json() if rsp.status_code == 200 else {}
        except Exception as err:
            logger.warn(f"IYUU 站点索引读取失败：{err}")
            return {}
        if str(body.get("code")) not in ("0", "None") and body.get("code") not in (0, None):
            logger.warn(f"IYUU 站点索引返回：{body.get('msg')}")
            return {}
        sites = {}
        for site in ((body.get("data") or {}).get("sites") or []):
            if not isinstance(site, dict):
                continue
            sid = _as_int(site.get("id"))
            if sid:
                sites[sid] = site
        if sites:
            self.save_data("iyuu_site_index", {"at": int(time.time()), "sites": sites})
        return sites

    def _mp_sites(self) -> List[Dict[str, str]]:
        """从 MP 站点管理读取勾选的站点，转成插件统一的站点字典。

        passkey：SitesHelper().get_indexer(domain) 的适配器配置优先，退回 site.rss 的 ?passkey=；
        它只影响 pieces-hash / RSS 这两条本地通道 —— IYUU 路线靠站点 Cookie/apikey 也能下种，
        所以这里不再因为没有 passkey 就把站点丢掉（家园这类站就是例子）。
        sid / download_page：IYUU 站点索引按域名补一份，只作兜底；
        IYUU 命中结果自带 sid，真正下载时会按命中的 sid 重新取 download_page。
        """
        selected = {str(x) for x in (self._mp_site_ids or [])}
        if not selected:
            return []
        try:
            from app.db.site_oper import SiteOper
            rows = SiteOper().list_order_by_pri() or []
        except Exception as err:
            logger.warn(f"读取 MoviePilot 站点失败：{err}")
            return []
        helper = None
        try:
            from app.helper.sites import SitesHelper
            helper = SitesHelper()
        except Exception:
            pass
        iyuu_index = self._iyuu_site_index()
        by_host_index: Dict[str, Dict[str, Any]] = {}
        for info in iyuu_index.values():
            base = str(info.get("base_url") or "").split("://", 1)[-1].strip("/").lower()
            if base:
                by_host_index[base] = info
        out: List[Dict[str, str]] = []
        for site in rows:
            if str(getattr(site, "id", "")) not in selected:
                continue
            domain = str(getattr(site, "domain", "") or "").strip()
            if not domain:
                continue
            url = self._normalise_url(domain, "https")
            host = url.split("://", 1)[-1].split("/")[0].lower()
            if host in SITE_SKIP:
                continue
            if host in SITE_REMAP:
                url = SITE_REMAP[host]
            indexer: Dict[str, Any] = {}
            if helper is not None:
                try:
                    indexer = helper.get_indexer(domain) or {}
                except Exception:
                    indexer = {}
            if not isinstance(indexer, dict):
                indexer = {}
            rss = str(indexer.get("rss") or getattr(site, "rss", "") or "")
            passkey = str(indexer.get("passkey") or "").strip() or self._passkey_from_rss(rss)
            # 没有 passkey 也保留：IYUU 路线可以用 Cookie/apikey 下载，
            # pieces-hash / RSS 通道自己会检查凭据。
            idx = by_host_index.get(host) or {}
            proxy = indexer.get("proxy")
            out.append({
                "mp_site_id": str(getattr(site, "id", "")),
                "name": str(getattr(site, "name", "") or domain),
                "url": url.rstrip("/") + "/",
                "passkey": passkey,
                "rsskey": str(indexer.get("rsskey") or ""),
                "sid": _as_int(idx.get("id")),
                "uid": _as_int(indexer.get("uid")),
                "download_page": str(idx.get("download_page") or ""),
                "x_api_key": str(indexer.get("apikey") or getattr(site, "apikey", "") or ""),
                "channel": "auto",
                "enabled": True,
                "proxy": "" if proxy is True or proxy is False or proxy is None else str(proxy),
                "cookie": str(indexer.get("cookie") or getattr(site, "cookie", "") or ""),
                "rss_url": rss,
                "rss_interval": "",
                "downhash": "",
            })
        return out

    def _other_sites(self) -> List[Dict[str, str]]:
        """站点来源：只从 MP 站点管理读取勾选的站点（凭据每次现读）。

        早期支持 IYUU JSON 导入与手写行，现已移除——MP 站点管理是唯一来源。
        是否要求 passkey 由各条下载通道自己判断；IYUU 路线有 Cookie/apikey 就够。
        """
        return self._mp_sites()


    # ---------------- 下载器 ----------------

    @staticmethod
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
        # 「辅种加到哪台」**不在这里打补丁**：目标下载器在 `_apply_target_client`
        # 里就已经把客户端列表收窄成一台，这里的自动判断天然只在那台里挑。
        return preferred

    def _target_wants(self) -> List[str]:
        """最终目标名单 = MP 下拉多选 + 手填，逐个规范化、去重、保序。

        两者是并集：多选来自 MP「设置 → 下载器」的实例名（MP-xxx），手填可补
        自定义下载器（clients_lines 里的名字）。都留空才表示「自动」。
        """
        wants: List[str] = []
        for raw in list(self._mp_target_clients or []) + [(self._target_client or "")]:
            name = self._normalise_target(raw)
            if name and name not in wants:
                wants.append(name)
        return wants

    def _resolve_targets(self, clients: List["_Client"]) -> Tuple[List["_Client"], List[str], List[str]]:
        """把最终目标名单解析成实际客户端，返回 (匹配到的, 问题描述, 歧义名字)。

        每个目标单独走 `_resolve_target_clients`（保持原有的精确→别名→唯一包含
        与歧义检测），再按名字并集；任何一个目标歧义/找不到都记下来，交给调用方
        「本轮不加」—— 绝不把种子推到没被点名的机器上。
        """
        matched: Dict[str, "_Client"] = {}
        problems: List[str] = []
        ambiguous: List[str] = []
        for name in self._target_wants():
            got = self._resolve_target_clients(name, clients)
            if getattr(self, "_target_ambiguous", None):
                ambiguous.extend(self._target_ambiguous)
                problems.append(f"`{name}` 同时匹配到多台：{', '.join(self._target_ambiguous)}")
            elif not got:
                problems.append(f"`{name}` 没匹配到任何下载器")
            else:
                for client in got:
                    matched.setdefault(client.name, client)
        return list(matched.values()), problems, ambiguous

    def _apply_target_client(self, clients: List["_Client"]) -> List["_Client"]:
        """把「辅种加到哪台」落实到客户端列表上（支持 MP 多选 + 手填）。

        语义是**只要点名的那几台**：留空才自动（谁在做种归谁）。以前这里只是给
        `preferred` 打补丁 —— 目标机器上没有的种子照样会被加到别的机器上，
        所以用户看到「指定了但还是往 qB 推」。名字也更宽松：`tr` 能匹配
        `MP-tr辅种`、`tr辅种`（MP 枚举出来的实例带 `MP-` 前缀）。
        解析不出来就本轮不加，绝不把种子推到没被点名的机器上。
        """
        wants = self._target_wants()
        if not wants:
            self._target_state = {}
            return clients
        matched, problems, ambiguous = self._resolve_targets(clients)
        available = [c.name for c in clients]
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        wanted_text = " + ".join(wants)
        if problems:
            self._target_state = {"wanted": wanted_text, "resolved": [c.name for c in matched],
                                  "available": available, "problems": problems, "ambiguous": ambiguous,
                                  "ok": False, "at": stamp}
            logger.error(f"辅种目标无法解析：{'; '.join(problems)}；当前可用："
                         f"{', '.join(available) or '（无）'}。本轮不向任何下载器加种")
            if matched:
                logger.error("（其中已解析到的目标也会一并停用，避免把种子推到没点名的机器上）")
            return []
        self._target_state = {"wanted": wanted_text, "resolved": [c.name for c in matched],
                              "available": available, "ok": True, "at": stamp}
        if [c.name for c in matched] != wants:
            logger.info(f"辅种目标 `{wanted_text}` → {', '.join(c.name for c in matched)}")
        return matched

    def _mp_clients(self) -> List[_Client]:
        """枚举 MoviePilot「设置 → 下载器」里的**每一个**实例。

        以前只取 `Qbittorrent()` / `Transmission()` 这两个默认实例，多下载器用户
        只会看到其中一台（issue #7）。MP v3 暴露了 DownloaderHelper，按名字列出
        所有已启用实例，一台一个适配器。
        """
        out: List[_Client] = []
        self._mp_enum_error = ""
        try:
            from app.application.downloader import DownloaderHelper
            services = DownloaderHelper().get_services() or {}
        except Exception as err:
            self._mp_enum_error = f"{type(err).__name__}: {err}"
            logger.warn(f"MoviePilot 多下载器接口不可用（{self._mp_enum_error}），"
                        f"只能看到默认下载器；此时「辅种加到哪台」填非默认机器的名字会解析不到")
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

    def _mp_downloader_options(self) -> List[Dict[str, str]]:
        """get_form 的下载器多选下拉：title=实例名+类型、value=MP-xxx（与 _mp_clients 的 label 一致）。"""
        options: List[Dict[str, str]] = []
        try:
            from app.application.downloader import DownloaderHelper
            services = DownloaderHelper().get_services() or {}
        except Exception as err:
            logger.warn(f"读取 MoviePilot 下载器列表失败：{err}")
            return options
        for name, info in services.items():
            label = f"MP-{name}"
            typ = str(getattr(info, "type", "") or "")
            options.append({"title": f"{label}（{typ}）" if typ else label, "value": label})
        return options

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

    def _pending(self) -> List[Dict[str, Any]]:
        try:
            items = self.get_data("pending") or []
            return [x for x in items if isinstance(x, dict) and x.get("data")]
        except Exception:
            return []

    def _save_pending(self, items: List[Dict[str, Any]]) -> None:
        self.save_data("pending", items[-self.PENDING_LIMIT:])

    def _queue_failure(self, client_name: str, key: str, torrent: bytes, save_path: str, label: str) -> None:
        if not self._retry_failed:
            return
        items = self._pending()
        if any(x.get("key") == key for x in items):
            return
        items.append({"key": key, "client": client_name,
                      "data": base64.b64encode(torrent).decode(),
                      "save_path": save_path, "label": label})
        self._save_pending(items)

    def _place(self, client: _Client, data: bytes, torrent: Dict[str, Any], key: str,
               stats: Dict[str, int], bucket: str, hash_hint: str = "", site_key: str = "") -> bool:
        """把一颗种子放进客户端：成功 / 重复 / 失败降级 / 进失败队列，都在这里决定。

        交叉做种最常见的失败是「文件不完全一致」（多一个 nfo、少一条音轨）：校验态下客户端会
        校验失败并把种子停住，所以失败时按「跳过校验」再试一次（可关）。真的失败就进队列，
        下一轮先重试它们，而不是等下一次全量扫描再撞一遍。
        """
        save_path = torrent.get("save_path", "")
        # 标签：合并「原种子已有标签（可关）」+「用户额外指定」+ 原来那个分类/自定义标签，
        # qB 的 tags 字段接受逗号分隔的多个值。
        tags: List[str] = []
        if self._inherit_tags:
            tags += [t.strip() for t in str(torrent.get("tags") or "").split(",")
                     if t.strip() and t.strip() != MANDATORY_TAG]
        tags += [t.strip() for t in str(self._extra_tags or "").split(",") if t.strip()]
        # 用户显式填的「辅种标签」才是标签；原种子的分类走 category 字段，不进 tags。
        if self._add_label and self._add_label != MANDATORY_TAG:
            tags.append(self._add_label)
        label = ",".join(dict.fromkeys(tags))
        category = str(torrent.get("category") or "").strip()
        auto_tmm = bool(torrent.get("auto_tmm"))
        if site_key and self._site_budget_left(site_key) <= 0:
            stats["skipped"] = stats.get("skipped", 0) + 1
            return False  # 这个站这一轮已经补满 max_per_site 颗
        if hash_hint and client.has(hash_hint):
            # 客户端里已经有这颗：qB 一定会回 Fails.，别浪费一次上传，直接记重复
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            self._remember(key)
            return True
        remembered = self._take_tags(hash_hint) if hash_hint else ""
        result = client.add(data, save_path, self._skip_check, label, hash_hint,
                            category=category, auto_tmm=auto_tmm)
        # 站点自己声明的间隔（IYUU 的「辅种间隔」）：只加两颗之间等，别的什么都不影响
        gap = self._site_intervals.get(site_key) if site_key else 0.0
        if gap:
            time.sleep(min(float(gap), 30.0))
        if remembered and result in ("ok", "dup"):
            # 先把种子放回去，再把删之前记下的标签补上（addTags 只追加不覆盖）
            if client.add_tags(hash_hint, remembered):
                logger.info(f"{hash_hint[:8]} 重新补种后已恢复原标签：{remembered}")
        if result == "ok":
            stats[bucket] += 1
            self._record_add()
            self._remember(key)
            if site_key:
                self._site_counter[site_key] = self._site_counter.get(site_key, 0) + 1
            return True
        if result == "dup":
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            self._remember(key)
            # 重复说明客户端里本来就有这颗种子，之前记过的失败可以撤掉
            self._save_pending([x for x in self._pending() if x.get("key") != key])
            return True
        if not self._skip_check and self._fallback_skip_check:
            retry = client.add(data, save_path, True, label, hash_hint)
            if retry == "ok":
                logger.info(f"校验未通过，已按「跳过校验」重试成功：{key}")
                stats[bucket] += 1
                stats["retried"] = stats.get("retried", 0) + 1
                self._record_add()
                self._remember(key)
                if site_key:
                    self._site_counter[site_key] = self._site_counter.get(site_key, 0) + 1
                return True
            if retry == "dup":
                stats["duplicate"] = stats.get("duplicate", 0) + 1
                self._remember(key)
                return True
        stats["failed"] += 1
        self._queue_failure(client.name, key, data, save_path, label)
        return False

    def _retry_pending(self, clients: Dict[str, _Client], done: set, stats: Dict[str, int]) -> None:
        """先重试上一轮失败的种子（最多 max_per_hour 个，占当轮预算）。"""
        pending = self._pending()
        if not pending or not clients:
            return
        left = []
        for item in pending:
            if self._event.is_set() or self._budget_left() <= 0:
                left.append(item)
                continue
            client = clients.get(item.get("client") or "")
            if client is None:
                # 不在本轮名单里（例如指定了别的目标下载器）：留着，别静默丢掉 ——
                # 配置改回来还能继续重试；以前直接 continue，等于把记录删了。
                left.append(item)
                continue
            key = item.get("key") or ""
            if key in done:
                continue
            try:
                data = base64.b64decode(item.get("data") or "")
            except Exception:
                continue
            torrent = {"save_path": item.get("save_path") or "", "category": item.get("label") or ""}
            before = stats["added"] + stats["filled"]
            self._place(client, data, torrent, key, stats, "filled")
            if stats["added"] + stats["filled"] > before:
                done.add(key)
            else:
                left.append(item)
        self._save_pending(left)

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

    def _tag_memory(self) -> Dict[str, str]:
        try:
            data = self.get_data("tag_memory") or {}
            return {str(k).lower(): str(v) for k, v in data.items() if k and v} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _remember_tags(self, mapping: Dict[str, str]) -> None:
        if not mapping:
            return
        memory = self._tag_memory()
        for info_hash, tags in mapping.items():
            extra = [t.strip() for t in str(tags or "").split(",")
                     if t.strip() and t.strip() != MANDATORY_TAG]
            if not extra:
                continue
            merged = [t for t in (memory.get(str(info_hash).lower(), "").split(",") if memory.get(str(info_hash).lower()) else []) if t]
            for tag in extra:
                if tag not in merged:
                    merged.append(tag)
            memory[str(info_hash).lower()] = ",".join(merged)
        if len(memory) > self.TAG_MEMORY_LIMIT:
            keep = list(memory.items())[-self.TAG_MEMORY_LIMIT:]
            memory = dict(keep)
        self.save_data("tag_memory", memory)

    def _take_tags(self, info_hash: str) -> str:
        """取出这颗种子被删前记下的标签（取走即删，避免无限增长）。"""
        info_hash = str(info_hash or "").lower()
        if not info_hash:
            return ""
        memory = self._tag_memory()
        tags = memory.pop(info_hash, "")
        if tags:
            self.save_data("tag_memory", memory)
        return tags

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

    @staticmethod
    def _host_from_url(url: str) -> str:
        return (str(url or "").split("://", 1)[-1].split("/")[0]
                .split(":")[0].strip().lower())

    def _iyuu_sid_map(self) -> Dict[int, Dict[str, Any]]:
        """IYUU 路线要汇报的是 IYUU 全站 sid，不是从 MP 站点里凑出来的 sid。

        和官方 iyuuautoseed 一样：Token 有效时先拉 /reseed/sites/index 拿全部支持站点，
        把这些 sid 全报给 /reseed/sites/reportExisting；MP 站点下拉只用来过滤命中结果，
        不参与“这个站有没有 sid”的判断。家园这类站因此不用在插件里手写任何站点信息。
        """
        out: Dict[int, Dict[str, Any]] = {}
        for key, info in self._iyuu_site_index().items():
            sid = int(key)
            out[sid] = {
                "sid": sid,
                "name": str(info.get("name") or info.get("base_url") or sid),
                "url": str(info.get("base_url") or ""),
                "download_page": str(info.get("download_page") or ""),
            }
        return out

    def _iyuu_site_for_entry(self, entry: Dict[str, Any],
                             sites: List[Dict[str, str]]) -> Tuple[Optional[Dict[str, str]], str]:
        """把 IYUU 命中里的 sid 反查成 MP 站点配置（官方做法）。

        IYUU 返回的 seed 自带 sid 和 torrent_id；sid 对应的站点地址/下载模板来自 IYUU 站点索引，
        Cookie/passkey/apikey 来自 MP 站点管理。这里只做两件事：用 base_url 的域名找到 MP 站点，
        再确认它在③里被勾选。找不到时返回原因，不再让用户“补 sid=数字”。
        """
        sid = int(entry.get("sid") or 0)
        idx = self._iyuu_site_index().get(sid) or {}
        host = self._host_from_url(idx.get("base_url"))
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return None, "IYUU 站点索引里没有这个 sid 的地址"
        for site in sites or []:
            site_host = self._host_from_url(site.get("url"))
            if site_host.startswith("www."):
                site_host = site_host[4:]
            if site_host != host:
                continue
            merged = dict(site)
            merged["sid"] = sid
            if idx.get("download_page"):
                merged["download_page"] = str(idx["download_page"])
            return merged, ""
        return None, "MP 站点管理里没有勾选这个站（或域名对不上）"

    def _iyuu_headers(self) -> Dict[str, str]:
        return {"token": self._iyuu_token, "User-Agent": "MoviePilot-ColaCrossSeed/0.4"}

    def _iyuu_sid_sha1(self, sids: List[int]) -> str:
        if not sids:
            return ""
        # IYUU 的 sid_sha1 有效期 7 天；缓存 6 小时，避免每次检测/运行都去汇报触发限流。
        cached = self.get_data("sid_sha1") or {}
        stamp = int(cached.get("at") or 0)
        if cached.get("sha1") and time.time() - stamp < 6 * 3600 and cached.get("sids") == sids:
            return str(cached["sha1"])
        try:
            rsp = requests.post(
                f"{self.IYUU_API}/reseed/sites/reportExisting",
                data={"sid_list[]": sids},
                headers=self._iyuu_headers(),
                timeout=15,
            )
            body = rsp.json() if rsp.status_code == 200 else {}
        except Exception as err:
            logger.warn(f"IYUU 汇报站点失败：{err}")
            return ""
        if body.get("code") not in (0, "0", None):
            msg = str(body.get("msg") or "")
            if "频率" in msg:
                if cached.get("sha1"):
                    return str(cached["sha1"])
                self._iyuu_rate_limited = True
            logger.warn(f"IYUU 汇报站点返回：{msg}")
            return ""
        sha1 = str((body.get("data") or {}).get("sid_sha1") or "")
        if sha1:
            self.save_data("sid_sha1", {"at": int(time.time()), "sids": sids, "sha1": sha1})
        return sha1

    def _iyuu_reseed(self, hashes: List[str], sid_sha1: str) -> Dict[str, List[Dict[str, Any]]]:
        """返回：客户端 info_hash -> 可辅的站点种子列表。"""
        out: Dict[str, List[Dict[str, Any]]] = {}
        for start in range(0, len(hashes), self.IYUU_CHUNK):
            chunk = sorted(hashes[start:start + self.IYUU_CHUNK])
            payload = json.dumps(chunk, separators=(",", ":"))
            data = {
                "hash": payload,
                "sha1": hashlib.sha1(payload.encode()).hexdigest(),
                "sid_sha1": sid_sha1,
                "timestamp": int(time.time()),
                "version": "4.0.0",
            }
            body = {}
            for attempt in range(3):
                try:
                    rsp = requests.post(f"{self.IYUU_API}/reseed/index/index", data=data,
                                        headers=self._iyuu_headers(), timeout=90)
                    body = rsp.json() if rsp.status_code == 200 else {}
                except Exception as err:
                    logger.warn(f"IYUU 辅种查询失败（第 {attempt + 1} 次）：{err}")
                    body = {}
                    time.sleep(5)
                    continue
                msg = str(body.get("msg") or "")
                if body.get("code") in (0, "0", None):
                    break
                if "频率" in msg or body.get("code") == 429:
                    logger.info("IYUU 云端限流，等待 60 秒后重试")
                    time.sleep(60)
                    continue
                logger.warn(f"IYUU 辅种查询返回：{msg}")
                break
            if body.get("code") not in (0, "0", None):
                break
            for client_hash, item in (body.get("data") or {}).items():
                torrents = (item or {}).get("torrent") or []
                if torrents:
                    out[str(client_hash).lower()] = torrents
            time.sleep(6)   # 每批之间留间隔：IYUU 对连续查询限流很敏感
        return out

    RSS_TTL = 600        # 索引缓存 10 分钟：站点侧普遍要求「≥2 分钟一次」，缓存远大于这个数
    RSS_ROWS = 100
    # 两次拉 RSS 的最小间隔（秒）。这是**插件强制**的硬下限，不靠调用方自觉：
    # 观众 audiences.me 明文要求「调整 RSS 请求间隔时间至少 2 分钟」，连点两次测试就会被怼回来。
    RSS_MIN_INTERVAL = 120
    # 按站点的下限（跟全局取更大值）：名单里的站一律不许比这更快
    RSS_INTERVAL_BY_HOST = {
        "audiences.me": 120,     # 观众：站点明确要求 ≥2 分钟
        "ptchdbits.co": 120,     # 彩虹岛：同样会回 "Prevent refresh and try again after 60s"
    }

    def _rss_usable(self, site: Dict[str, str]) -> bool:
        """这个站能不能读 RSS：有任何一个密钥就行，channel 不再当前置条件。

        RSS 命中的是「同一 info_hash」，qB 不允许同 hash 双开，所以它不走加种，
        走「给已有的那颗种子补这个站的 announce」——这条和 pieces-hash 互不冲突，
        两条通道可以同时开，不用二选一。
        """
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        if "m-team" in host:
            return False
        if site.get("x_api_key") and not site.get("rsskey"):
            return False
        return bool(site.get("rsskey") or site.get("passkey") or site.get("rss_url"))

    @staticmethod
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

    def _rss_interval(self, site: Dict[str, str]) -> int:
        """这个站两次拉 RSS 至少要隔多久：站点行里可以用 rss_interval=秒 覆盖，取更大值。"""
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        floor = max(int(self.RSS_MIN_INTERVAL), int(self.RSS_INTERVAL_BY_HOST.get(host, 0) or 0))
        raw = str(site.get("rss_interval") or "").strip()
        if raw.isdigit():
            floor = max(floor, int(raw))
        return floor

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

    def _rss_fetch(self, site: Dict[str, str], timeout: int = 40,
                   prefetched: Optional[Dict[str, "_HttpResult"]] = None) -> Tuple[Dict[str, Dict[str, Any]], str]:
        """真去拉一次 RSS，返回 (info_hash -> {url,title,size}, 用的是哪个密钥/为什么失败)。

        NexusPHP 的 torrentrss.php 每条 item 带着三样硬信息：
          · guid（40 位）= info_hash
          · enclosure url = download.php?id=..&downhash=<rsskey>（不需要 passkey 也能下）
          · enclosure length = 精确字节数
        参数名各站不同，按 rss_url → rsskey → passkey 的顺序试，谁回 XML 用谁。
        """
        rsskey = str(site.get("rsskey") or "")
        passkey = str(site.get("passkey") or "")
        personal = str(site.get("rss_url") or "").strip()
        if not (rsskey or passkey or personal):
            return {}, "既没有 rsskey 也没有 passkey"
        host = site.get("url", "").split("://", 1)[-1].split("/")[0].lower()
        interval = self._rss_interval(site)
        last = float(self._rss_at().get(host) or 0)
        wait = int(interval - (time.time() - last)) if last else 0
        if last and wait > 0:
            # 没到最小间隔：绝不发请求，有旧索引就用旧的
            cached = ((self.get_data("rss_cache") or {}).get(host) or {}).get("items") or {}
            return (cached if isinstance(cached, dict) else {}), \
                f"限流保护：{wait} 秒后才允许再拉（该站要求 ≥{interval} 秒一次）"
        tries = self._rss_tries(site)
        first_bad = ""
        last = "RSS 没有可用地址"
        self._rss_mark(host)   # 打点放在发请求之前：失败的尝试也算“拉过一次”
        for target, params, label in tries:
            rsp = (prefetched or {}).get(label)
            try:
                if rsp is None:
                    rsp = _site_request("GET", target, params=params, headers=_site_headers(site),
                                        timeout=timeout, proxy=site.get("proxy") or "")
            except Exception as err:
                last = f"RSS 连不上（{type(err).__name__}）"
                first_bad = first_bad or last
                continue
            if rsp.status_code != 200:
                last = f"RSS 返回 HTTP {rsp.status_code}（{label}）"
                first_bad = first_bad or last
                continue
            text = (rsp.text or "").strip()
            if "<rss" not in text[:400]:
                low = text.lower()
                if "prevent refresh" in low or "try again after" in low:
                    last = "RSS 被站点限流（刚拉过，稍后自动恢复）"
                elif "expired" in low:
                    last = "RSS 链接已过期，要在站内重新生成个人 RSS 填 rss_url="
                elif "not found" in low or "missing" in low:
                    last = f"RSS 不认这个参数（{label}）：{text[:40]}"
                else:
                    last = f"RSS 返回的不是 XML：{text[:40]}"
                first_bad = first_bad or last
                continue
            items = self._parse_rss_items(rsp.content)
            if not items:
                last = "RSS 里没有 40 位 info_hash 的条目"
                first_bad = first_bad or last
                continue
            return items, label
        # 第一个失败原因通常最接近真相（后面的 passkey 尝试会被站点直接怼回 missing rsskey）
        return {}, first_bad or last

    RSS_WORKERS = 3   # 并发拉 RSS 的 curl 子进程数（不用线程，见 _curl_parallel）

    def _rss_cached(self, host: str) -> Tuple[Dict[str, Dict[str, Any]], bool]:
        """返回 (缓存的索引, 缓存是否新鲜)。"""
        cache = self.get_data("rss_cache") or {}
        entry = cache.get(host) or {}
        cached = dict(entry.get("items")) if isinstance(entry.get("items"), dict) else {}
        fresh = bool(cached) and bool(entry.get("at")) and time.time() - int(entry["at"]) < self.RSS_TTL
        return cached, fresh

    def _rss_needs_fetch(self, site: Dict[str, str]) -> bool:
        """这一轮要不要真去拉这个站的 RSS：缓存过期 + 不在最小间隔保护期 + 有可用密钥。"""
        host = site.get("url", "").split("://", 1)[-1].split("/")[0].lower()
        _cached, fresh = self._rss_cached(host)
        if fresh:
            return False
        if not self._rss_tries(site):
            return False
        last = float(self._rss_at().get(host) or 0)
        if last and (time.time() - last) < self._rss_interval(site):
            return False
        return True

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

    def _rss_index(self, site: Dict[str, str],
                   prefetched: Optional[Dict[str, Dict[str, "_HttpResult"]]] = None) -> Dict[str, Dict[str, Any]]:
        """带缓存的 RSS 索引（info_hash -> {url,title,size}）：10 分钟内同一站只拉一次。

        拉失败**不覆盖已有索引**：站点偶尔限流一次，不应该把本来好用的通道关掉。
        """
        host = site.get("url", "").split("://", 1)[-1].split("/")[0].lower()
        cached, fresh = self._rss_cached(host)
        if fresh:
            return cached
        prefetched_host = (prefetched or {}).get(host) or {}
        if prefetched_host:
            # 预取已经用 curl 打过这个站了：后面补试别的候选时别再让 requests 先撞一次
            # （那些站大多是 Cloudflare 后面的站，requests 每次都要等到 20 秒超时才回退）。
            _CURL_ONLY_HOSTS[host] = True
        items, note = self._rss_fetch(site, prefetched=prefetched_host)
        with self._rss_lock:
            notes = self.get_data("rss_notes") or {}
            if not isinstance(notes, dict):
                notes = {}
            notes[host] = note
            self.save_data("rss_notes", notes)
            if items and not note.startswith("限流保护"):
                cache = self.get_data("rss_cache") or {}
                cache[host] = {"at": int(time.time()), "items": items, "note": note}
                self.save_data("rss_cache", cache)
        if items and not note.startswith("限流保护"):
            logger.info(f"{host} RSS 索引 {len(items)} 条（{note}）")
            return items
        if items:
            # 处在最小间隔内：继续用旧索引，但不刷新时间戳（下次到点还会真拉一次）
            logger.info(f"{host} RSS {note}，继续用上次的 {len(items)} 条")
            return items
        logger.info(f"{host} RSS 不可用：{note}")
        return cached

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

    def _download_mteam(self, site: Dict[str, str], torrent_id: str) -> bytes:
        """M-Team：genDlToken 换签名地址 → 该地址会 302 到 CDN（fr1.halomt.com）→ 才拿到种子。

        实测（2026-11-01）：
          · 必须带浏览器 UA，脚本 UA 会被 302 到 google；
          · 签名地址自己也是 302，必须跟随重定向（curl 要 -L，requests 默认跟随）；
          · 重定向到 CDN 后要 Accept: application/x-bittorrent，否则可能回一整页 HTML（约 47KB）。
        """
        api_key = site.get("x_api_key") or ""
        if not api_key:
            return b""
        base = (site.get("url") or "").rstrip("/")
        headers = {"x-api-key": api_key, "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA}
        dl_headers = {"User-Agent": UA, "Accept": "application/x-bittorrent, */*",
                      "Referer": base + "/"}
        for attempt in range(2):
            rsp = _site_request("POST", f"{base}/api/torrent/genDlToken", data={"id": torrent_id},
                                headers=headers, proxy=site.get("proxy") or "")
            try:
                body = rsp.json() if rsp.status_code == 200 else {}
            except Exception:
                body = {}
            if str(body.get("code")) != "0" or not body.get("data"):
                logger.warn(f"M-Team 换取下载地址返回：{body.get('message') or rsp.status_code}")
                return b""
            torrent = _site_request("GET", str(body["data"]), headers=dl_headers,
                                    proxy=site.get("proxy") or "")
            if torrent.status_code == 200 and _valid_torrent(torrent.content):
                return torrent.content
            logger.warn(f"M-Team 下载种子第 {attempt + 1} 次失败：HTTP {torrent.status_code} "
                        f"{len(torrent.content)} 字节（换新 token 重试）")
        return b""


    def _download_site_torrent(self, site: Dict[str, str], torrent_id: str) -> bytes:
        """IYUU / pieces-hash 两条路都从这里取种：统一按模板 + Cookie + requests→curl 回落。"""
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        if "m-team" in host:
            return self._download_mteam(site, torrent_id)
        return self._site_download(site, torrent_id)

    # ---------------- 连通性检测（保存后自动跑，结果显示在插件页） ----------------

    _test_report: Dict[str, Any] = {}

    def _test_clients(self) -> List[str]:
        lines: List[str] = []
        clients = self._clients()
        wants = self._target_wants()
        target_names, problems, _ = self._resolve_targets(clients) if (wants and clients) else ([], [], [])
        target_set = {c.name for c in target_names}
        for client in clients:
            mark = " ← 辅种目标" if client.name in target_set else ""
            torrents = client.list_completed()
            if torrents:
                lines.append(f"✅ {client.name}：连接成功，已完成 {len(torrents)} 个种子{mark}")
            else:
                # 能连上但列表为空，和连不上要分开说。
                lines.append(f"⚠️ {client.name}：连接成功，但没有已完成种子（或读取失败）{mark}")
        if not lines:
            lines.append("❌ 没有可用下载器：检查「下载器」每行格式与地址")
        for problem in problems:
            lines.append(f"❌ 辅种目标{problem}；可用：{', '.join(c.name for c in clients) or '（无）'}")
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

    def _test_one_site(self, site: Dict[str, str]) -> Tuple[str, str, str]:
        """一个站到底能用哪条通道：pieces-hash / RSS / IYUU 逐条实测，报实测结果。

        以前这里只认 rsskey 字段，没填就回「这个站目前没法匹配」；实际 2026-11-02 全站复测：
        26 个站里只有观众有 rsskey，另外 11 个站用 passkey 就能读到 RSS（10~100 条全是 40 位
        info_hash）。于是能用 RSS 的站被全部报成 ❌。现在按通道实测，并给出条数。
        """
        name = site["name"]
        host = site["url"].split("://", 1)[-1].split("/")[0].lower()
        if "m-team" in host:
            if site.get("x_api_key"):
                return name, "ok", "M-Team：IYUU 匹配 + x-api-key 下载（不走 pieces-hash）"
            return name, "fail", "M-Team：缺少 x_api_key（IYUU 导出里应该有）"
        channel = (site.get("channel") or "auto").lower()
        good: List[str] = []
        bad: List[str] = []
        pieces_ok = False
        if channel in ("auto", "pieces"):
            pieces_ok, note = self._probe_pieces(site)
            (good if pieces_ok else bad).append(note)
        rss_n = 0
        if channel in ("auto", "rss") and not pieces_ok:
            rss_n, note = self._probe_rss(site)
            (good if rss_n else bad).append(note)
        if not pieces_ok and not rss_n and channel != "pieces":
            # RSS 这会儿不可用（限流/没密钥）时再看一眼 pieces-hash：
            # 很多站「channel 写的是 rss」其实也有 pieces-hash，别让它显示成没通道
            pieces_ok, note = self._probe_pieces(site)
            if pieces_ok:
                good.append(note)
        iyuu_ok = bool(site.get("sid")) and bool(self._iyuu_token)
        if iyuu_ok:
            good.insert(0, f"IYUU 匹配（sid={site['sid']}）")
        elif site.get("sid"):
            bad.append("没填 IYUU Token，sid 用不上")
        if channel == "pieces" and pieces_ok:
            return name, "ok", "；".join(good)
        if channel == "rss" and rss_n:
            return name, "ok", "；".join(good)
        if channel == "iyuu" and iyuu_ok:
            return name, "ok", "；".join(good)
        if pieces_ok or rss_n:
            return name, "ok", "；".join(good)
        if iyuu_ok:
            # 只剩 IYUU：通道在，但没有哪颗种子能证明命中，标黄而不是绿
            return name, "warn", "；".join(good + bad)
        return name, "fail", "；".join(bad) or "没有可用通道"

    def _rss_note(self, site: Dict[str, str]) -> str:
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        note = (self.get_data("rss_notes") or {}).get(host)
        if note:
            return str(note)
        return str(((self.get_data("rss_cache") or {}).get(host) or {}).get("note") or "")

    def _probe_rss(self, site: Dict[str, str]) -> Tuple[int, str]:
        """检查 RSS 通道，走 10 分钟缓存。

        不能每次点「测试」都去真拉：观众/彩虹岛 这类站明说「请求间隔至少 2 分钟」，
        连点两次测试就会被限流，反而把好站报成失败。
        """
        items = self._rss_index(site)
        note = self._rss_note(site)
        if items:
            return len(items), f"RSS 可匹配 {len(items)} 条（{note}）"
        return 0, note or "RSS 没有条目"

    def _test_sites(self) -> List[str]:
        sites = self._other_sites()
        if not sites:
            return ["ℹ️ 没有勾选其他站（在③区「MP 已配置站点」里多选即可）"]
        lines: List[str] = []
        # 以前用线程池并发探测；现在一律顺序跑 —— 见 auto_seed 里 RSS 索引那段的说明：
        # 在解释器层面，多线程里做网络/第三方 C 扩展会放大崩溃面，宁可慢一点。
        for site in sites:
            name, level, msg = self._test_one_site(site)
            mark = {"ok": "✅", "warn": "⚠️"}.get(level, "❌")
            lines.append(f"{mark} {name}：{msg}")
        good = sum(1 for line in lines if line.startswith("✅"))
        warn = sum(1 for line in lines if line.startswith("⚠️"))
        lines.insert(0, f"其他站 {len(sites)} 个，可匹配 {good} 个"
                        f"{f'，仅 IYUU 匹配 {warn} 个' if warn else ''}，"
                        f"不可用 {len(sites) - good - warn} 个")
        return lines


    def _test_cola(self) -> str:
        """personalStats：200 就说明 Key 能读；顺带显示等级名。下载范围要到真取种时才知道。"""
        if not self._api_key:
            return f"❌ 可乐PT：未填 API Key（在 {COLA_KEY_PAGE} 创建，勾「读取」「下载」）"
        rsp = self._cola("GET", COLA_STATS_API)
        if rsp is None:
            return "❌ 可乐PT：连不上（网络/防火墙），稍后重试"
        if rsp.status_code == 200:
            class_name = str((rsp.json({}) or {}).get("class_name") or "").strip()
            return f"✅ 可乐PT：API Key 可用（{class_name or '未定级'}）"
        if rsp.status_code == 401:
            return "❌ 可乐PT：API Key 无效、已撤销或已过期，请重新创建"
        if rsp.status_code == 403:
            if self._error_code(rsp) == "api_key_scope_denied":
                return "❌ 可乐PT：API Key 缺少「读取」范围，请新建一把勾「读取」「下载」的 Key"
            return "❌ 可乐PT：账号没有权限（被停用或等级不够）"
        if rsp.status_code == 429:
            return "⚠️ 可乐PT：请求太频繁（每把 Key 每分钟有额度），稍后自动恢复"
        return f"❌ 可乐PT：HTTP {rsp.status_code}"

    def _test_iyuu(self) -> str:
        if not self._iyuu_token:
            return ("ℹ️ IYUU Token：未配置（可选——不填也能补可乐、也能用 pieces-hash/RSS 补其他站；"
                    "填了额外走 IYUU 云端，命中站点和下载模板由 IYUU 返回，MP 站点下拉只管范围和凭据）")
        sid_map = self._iyuu_sid_map()
        if not sid_map:
            return "⚠️ IYUU Token：已填，但读不到 IYUU 站点索引（检查 Token / 网络）"
        sid_sha1 = self._iyuu_sid_sha1(sorted(sid_map))
        if sid_sha1:
            return f"✅ IYUU Token：有效（{len(sid_map)} 个站点已汇报）"
        if getattr(self, "_iyuu_rate_limited", False):
            return "⚠️ IYUU Token：已填，但云端限流（访问频率过快），稍后再试"
        return "❌ IYUU Token：无效或云端拒绝（检查 token）"

    # 报告格式版本：老版本写下的 test_report 没有这个字段，里面全是已废弃的
    # 「粘贴 IYUU 站点 JSON」「域名,passkey」文案。get_page 读到版本不符的就丢弃，
    # 免得升级后还一直显示几个月前的旧检测结论。
    _TEST_REPORT_SCHEMA = 2

    def run_tests(self, persist: bool = True) -> Dict[str, Any]:
        report = {
            "schema": self._TEST_REPORT_SCHEMA,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cola": self._test_cola(),
            "iyuu": self._test_iyuu(),
            "clients": self._test_clients(),
            "sites": self._test_sites(),
        }
        self._test_report = report
        if persist:
            self.save_data("test_report", report)
        logger.info(f"连通性检测：{report['cola']}；{report['clients']}；"
                    f"{report['sites'][0] if report['sites'] else ''}")
        return report

    # ---------------- 任务 ----------------

    def _run_once(self) -> None:
        """「立即运行一次」：等保存流程结束再跑一轮，避免和配置保存抢资源。"""
        time.sleep(3)
        # force=True：对齐官方 iyuuautoseed 的 get_state() or _onlyonce，
        # 手动触发无条件跑一轮，不受启用总开关/辅种开关限制。
        self.auto_seed(force=True)

    def auto_seed(self, force: bool = False):
        """辅种主流程：任何未预料的异常都只记日志 + 站内通知，不要让 MoviePilot 报「执行失败」。"""
        try:
            self._auto_seed(force=force)
        except Exception as err:
            logger.error(f"辅种任务异常终止：{type(err).__name__}: {err}")
            try:
                self.post_message(mtype=NotificationType.Plugin,
                                  title="【可乐PT辅种】本轮执行异常",
                                  text=f"{type(err).__name__}: {err}\n下一轮会重新开始，详细栈见 MoviePilot 日志。")
            except Exception:
                pass

    def _auto_seed(self, force: bool = False):
        # 事件是类级 Event：reload/停止会把上一实例的 set 带过来，这里先清掉，
        # 否则本次任务会在第一个 torrent 就 break（表现为 matched=0、耗时 1 秒）。
        self._event.clear()
        # force=「立即运行一次」：无视总开关照跑（官方也是 enabled or onlyonce）。
        # 定时/周期任务 force=False，仍受开关控制。
        if not force and not (self._enabled and self._crossseed_enabled):
            return
        if not self._api_key:
            logger.warn("可乐PT 未填 API Key，跳过本次辅种")
            return
        clients = self._clients()
        if not clients:
            logger.warn("没有可用的下载器")
            return
        # 「辅种加到哪台」在这里落地：解析不出来就本轮不加（不回落自动，免得推到没点名的机器）
        all_clients = clients
        clients = self._apply_target_client(all_clients)
        try:
            self.save_data("target_state", self._target_state or {})
        except Exception:
            pass
        if not clients:
            # 目标名写错 = 本轮什么都不做，但**必须留痕**：否则面板「上次运行」停在上一轮，
            # 用户以为任务没跑，其实是名字没对上。
            state = self._target_state or {}
            stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                     "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
            self.save_data("last_run", {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **stats,
                "reason": (f"辅种目标 {state.get('wanted')} 匹配到多台下载器，本轮跳过"
                           if state.get("ambiguous") else
                           f"辅种目标 {state.get('wanted')} 不存在，本轮跳过")})
            return
        # 限速只针对保种组（老板 2026-11-15）：辅种不接管、不改任何客户端限速设置
        # 自愈：上一轮加进去但文件对不上的（state=missingFiles），清掉，别在 qB 里留垃圾
        for client in clients:
            missing = client.tagged_missing(MANDATORY_TAG)
            if missing:
                # 用户/MP 给这些种子打的标签（已整理、订阅、MEDIA-AGENT…）不是我们的，
                # 删种子会把它们一起抹掉；删之前记下来，重新补种时原样加回去（issue #11）。
                self._remember_tags(client.tags_for(missing))
                client.delete(missing)
                self._mark_dead(missing)
                logger.warn(f"{client.name}：清理 {len(missing)} 个文件缺失的辅种种（只删种子，不删文件）")
        sites = []
        if self._direction in ("both", "other_only"):
            sites = self._other_sites()
        if sites:
            logger.info(f"其他站 {len(sites)} 个：{', '.join(s['name'] for s in sites[:6])}{'...' if len(sites) > 6 else ''}")
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        unmatched_iyuu_sids: Dict[int, str] = {}
        by_site: Dict[str, Dict[str, int]] = {}
        self._site_counter = {}   # 每轮重置：单站上限按轮算
        self._site_limits = {}    # 每轮重置：站点自己声明的上限（IYUU limit.count）
        self._site_intervals = {} # 每轮重置：站点自己声明的间隔（IYUU limit.sleep）
        self._site_fails = {}     # 每轮重置：站点失败计数（熔断用）
        done = set(self._done())
        dead = set(self._dead_hashes())   # 文件确实缺失、已被自愈清掉的，不再重试
        # 先把所有客户端扫一遍：tracker 不允许同一账号同一颗种子在多个客户端同时做种，
        # 所以“另一边已经有的可乐种”必须跳过，不能只在本客户端内去重。
        if self._retry_failed:
            self._retry_pending({c.name: c for c in clients}, done, stats)
        # 扫描两件事分开：**加种**只落目标下载器；**去重用的 hash 视图**必须是全部下载器。
        # tracker 不允许同一账号同一颗种子在多台同时做种，收窄扫描范围会把这条守卫弄丢
        # （2026-11-19 复审指出：目标改到 tr 后，qB 里已有的可乐种会被重复拉一遍）。
        target_ids = {id(c) for c in clients}
        scanned = []
        all_hashes = set()
        for client in all_clients:
            torrents = client.list_completed()
            if not torrents:
                continue
            all_hashes.update(t["hash"] for t in torrents if t.get("hash"))
            if id(client) in target_ids:
                scanned.append((client, torrents))
        # 同一颗种子在多个下载器里都有时，只在「正在做种」的那个里补种（见 _preferred_clients）。
        if not scanned:
            # 收窄到目标下载器后，如果这台读不到任何种子（连不上 / 列表为空），
            # 后面所有分支都会静默跳过 —— 用户只会看到「上次运行」停在旧时间。
            wanted = (self._target_client or "").strip()
            logger.warn(f"没有扫描到任何种子（{'目标下载器 ' + wanted if wanted else '全部下载器'}），"
                        f"本轮不会加种。检查下载器连通性与「已完成」列表")
            if wanted and all_hashes:
                logger.warn(f"目标下载器 `{wanted}` 读不到种子，但其它下载器有 {len(all_hashes)} 颗 —— "
                            f"重点查目标机的地址/账号，或它是不是被选成了空客户端")
            self.save_data("scan_empty", {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target": (self._target_client or "").strip(),
                "others": len(all_hashes)})
        preferred = self._preferred_clients(scanned)
        if len(scanned) > 1:
            logger.info(f"下载器 {len(scanned)} 个；同一颗种子跨客户端的归属已按「谁在做种」决定")
        # IYUU 云端匹配（有 token 时）：把客户端 hash 发过去，拿回「站点 + torrent_id」。
        sid_map: Dict[int, Dict[str, Any]] = {}
        iyuu_hits: Dict[str, List[Dict[str, Any]]] = {}
        if self._iyuu_token and self._direction in ("both", "other_only"):
            sid_map = self._iyuu_sid_map()
            if sid_map:
                hashes_all = sorted(all_hashes)
                sid_sha1 = self._iyuu_sid_sha1(sorted(sid_map))
                if sid_sha1:
                    iyuu_hits = self._iyuu_reseed(hashes_all, sid_sha1)
                    logger.info(f"IYUU 云端查询 {len(hashes_all)} 个 hash，命中 {len(iyuu_hits)} 个可辅种")
        # RSS 通道：对配置了 rsskey 的站，RSS 里能看到多少就按 info_hash 精确匹配多少。
        # 这条路不依赖 pieces-hash，也不依赖 IYUU 是否收录（观众这类站就走它）。
        # 只要站里有 passkey 就够读 RSS：26 个站里只有观众需要 rsskey，其余用 passkey 都能拿到 XML，
        # 以前这里卡 rsskey，等于把 13 个 channel=rss 的站直接跳过了。
        rss_sites = ([x for x in sites if self._rss_usable(x)]
                     if self._direction in ("both", "other_only") else [])
        rss_idx: Dict[str, Dict[str, Dict[str, Any]]] = {}
        rss_by_url: Dict[str, Dict[str, str]] = {}
        if rss_sites:
            # 网络并发交给 curl 子进程（_curl_parallel），Python 侧仍然顺序执行：
            # 历史上这里用 ThreadPoolExecutor 并发拉 + 并发写插件数据，在 CPython 3.14 上
            # 会偶发整解释器段错误（MoviePilot 直接 SIGSEGV 重启，见 2026-11 的崩溃排查）。
            # 27 个站顺序拉要 15 分钟以上，3 路并发后降到几分钟。
            need = [site for site in rss_sites if self._rss_needs_fetch(site)]
            prefetched = self._rss_prefetch(need) if need else {}
            for site in rss_sites:
                idx = self._rss_index(site, prefetched)
                if idx:
                    key = site.get("url") or site.get("name") or ""
                    rss_idx[key] = idx
                    rss_by_url[key] = site
            logger.info(f"RSS 通道 {len(rss_idx)}/{len(rss_sites)} 个站有索引，"
                        f"共 {sum(len(v) for v in rss_idx.values())} 条")
        for client, torrents in scanned:
            if self._event.is_set():
                break
            hashes = [t["hash"] for t in torrents if t.get("hash")]
            client_hashes = set(hashes)
            by_hash, err = self._match(hashes)
            if err:
                logger.error(err)
                by_hash = {}
            by_hash = self._match_with_source_variants(client, torrents, hashes, by_hash)
            for torrent in torrents:
                if self._event.is_set():
                    break
                owner = preferred.get(torrent["hash"])
                if owner and owner != client.name:
                    # 这颗归另一个下载器（通常是正在保种的那台），别在这里重复加
                    continue
                # RSS 通道：RSS 的 guid 就是站点侧 info_hash，命中即「这个站有同一颗种子」。
                # 同一 info_hash qB 不可能再加一颗（会回 Fails.），这时正确动作是给**已有**的
                # 那颗种子补上这个站的 announce —— 文件一个字节都不用多，站方照样记做种。
                rss_done = False
                for site_url, idx in rss_idx.items():
                    hit = idx.get(torrent["hash"])
                    if not hit:
                        continue
                    key = f"{client.name}:rss:{site_url}:{torrent['hash']}"
                    if key in done:
                        stats["skipped"] += 1
                        rss_done = True
                        continue
                    if self._budget_left() <= 0 or self._site_budget_left(site_url) <= 0:
                        break
                    data = self._download_url(hit.get("url") or "", rss_by_url.get(site_url))
                    if not data:
                        stats["failed"] += 1
                        continue
                    tracker_hash = _torrent_info_hash(data) or torrent["hash"]
                    announces = _torrent_announces(data)
                    if announces and tracker_hash in client_hashes:
                        if client.add_trackers(tracker_hash, announces):
                            stats["trackers"] += 1
                            self._site_counter[site_url] = self._site_counter.get(site_url, 0) + 1
                            self._remember(key)
                            done.add(key)
                            rss_done = True
                            by_site.setdefault(site_url, {"added": 0, "failed": 0}).setdefault(
                                "trackers", 0)
                            by_site[site_url]["trackers"] += 1
                            logger.info(f"{site_url} 同一 info_hash 已在客户端，已补 tracker：{tracker_hash[:8]}")
                        continue
                    if self._place(client, data, torrent, key, stats, "filled",
                                   hash_hint=tracker_hash, site_key=site_url):
                        done.add(key)
                    rss_done = True
                    by_site.setdefault(site_url, {"added": 0, "failed": 0})["added"] += 1
                if rss_done:
                    continue
                hits = by_hash.get(torrent["hash"]) or []
                cola_hits = [h for h in hits if h.get("via") == "source_info_hash"]
                own_hits = [h for h in hits if h.get("via") == "info_hash"]
                # 客户端已经有这颗可乐种（同一 info_hash）时不必再拉：qB 会直接回 Fails.（重复种子）。
                kept = [h for h in cola_hits if (h.get("info_hash") or "").lower() not in all_hashes]
                if len(kept) != len(cola_hits):
                    stats["already"] = stats.get("already", 0) + (len(cola_hits) - len(kept))
                cola_hits = kept
                stats["matched"] += 1 if hits else 0
                # 方向一：客户端有同一份文件（来源别的站），拉可乐种子回来辅
                if cola_hits and self._direction in ("both", "cola_only"):
                    hit = cola_hits[0]
                    key = f"{client.name}:cola:{hit.get('id')}"
                    if key in done:
                        info_hash = (hit.get("info_hash") or "").lower()
                        if info_hash and info_hash not in client_hashes and info_hash not in dead:
                            # 记过、但客户端里已经没有这颗（用户删了 / 之前被清掉）：
                            # 「记过」不能当成永久结论，否则这颗种子永远补不回来。
                            logger.info(f"{client.name}：{info_hash[:8]} 记过但客户端里已没有，撤销记忆重新补种")
                            self._forget(key)
                            done.discard(key)
                        else:
                            stats["skipped"] += 1
                            continue
                    if self._budget_left() <= 0:
                        logger.info("已达到每小时辅种上限（只统计成功添加的种子），结束本次")
                        self._finish(stats)
                        return
                    data = self._download_cola(int(hit.get("id")))
                    if not data:
                        stats["failed"] += 1
                        continue
                    if self._place(client, data, torrent, key, stats, "added",
                                   (hit.get("info_hash") or "").lower()):
                        done.add(key)
                # 方向二：客户端已是可乐种子，去别的站补同 pieces 的种
                elif self._direction in ("both", "other_only") and self._iyuu_token and sid_map:
                    # IYUU 云端匹配：把客户端 hash 发过去，拿回「站点 + torrent_id」。
                    entries = iyuu_hits.get(torrent["hash"]) or []
                    if entries:
                        logger.info(f"IYUU 待处理 hash={torrent['hash'][:8]} 条目 {len(entries)}")
                    for entry in entries:
                        sid = int(entry.get("sid") or 0)
                        site, why = self._iyuu_site_for_entry(entry, sites)
                        # 注意：channel 只决定 pieces-hash / RSS 这两条「探测型」通道走不走，
                        # 不挡 IYUU。IYUU 是云端内容级匹配，站点和 torrent_id 都是它返回的；
                        # MP 站点下拉只决定“补哪些站”和“用哪套凭据”，不再要求插件里预置 sid。
                        torrent_id = str(entry.get("torrent_id") or "")
                        if not site:
                            # 和官方插件一致：IYUU 已经知道这个站，缺的是 MP 站点管理里的勾选/凭据。
                            unmatched_iyuu_sids.setdefault(sid, why)
                            continue
                        if not torrent_id:
                            continue
                        key = f"{client.name}:{site.get('name') or site.get('url')}:{torrent_id}"
                        if key in done:
                            stats["skipped"] += 1
                            continue
                        if self._budget_left() <= 0:
                            logger.info("已达到每小时辅种上限（只统计成功添加的种子），结束本次")
                            self._finish(stats)
                            return
                        site_key = site.get("name") or site.get("url") or "?"
                        if not self._site_open(site_key):
                            stats["skipped"] += 1
                            continue
                        remote = self._download_site_torrent(site, torrent_id)
                        if not remote:
                            stats["failed"] += 1
                            self._site_fail(site_key)
                            continue
                        # 站点返回的种子自己的 info_hash：qB 回 Fails. 时用来判断是「早就有」
                        # 还是「种子本身坏了」（以前一律打「未知 hash」）
                        if self._place(client, remote, torrent, key, stats, "filled",
                                       hash_hint=_torrent_info_hash(remote),
                                       site_key=site.get("name") or site.get("url") or "?"):
                            done.add(key)
                        by_site.setdefault(site.get("name") or site.get("url") or "?", {"added": 0, "failed": 0})["added"] += 1
                elif own_hits and sites and self._direction in ("both", "other_only"):
                    hit = own_hits[0]
                    data = self._download_cola(int(hit.get("id")))
                    pieces = _decode_torrent_pieces(data) if data else ""
                    if not pieces:
                        continue
                    for site in sites:
                        if (site.get("channel") or "auto").lower() in ("rss", "iyuu"):
                            continue  # 这两个通道的站本来就没有 pieces-hash 接口
                        remote_id, _ = self._site_lookup(site, pieces)
                        if not remote_id:
                            continue
                        key = f"{client.name}:{site.get('name') or site.get('url')}:{remote_id}"
                        if key in done:
                            stats["skipped"] += 1
                            continue
                        if self._budget_left() <= 0:
                            logger.info("已达到每小时辅种上限（只统计成功添加的种子），结束本次")
                            self._finish(stats)
                            return
                        remote = self._site_download(site, str(remote_id))
                        if not remote:
                            stats["failed"] += 1
                            continue
                        if self._place(client, remote, torrent, key, stats, "filled",
                                       site_key=site.get("name") or site.get("url") or "?"):
                            done.add(key)
                        by_site.setdefault(site.get("name") or site.get("url") or "?", {"added": 0, "failed": 0})["added"] += 1
        if by_site:
            stats["by_site"] = by_site
        if unmatched_iyuu_sids:
            detail = ", ".join(f"{sid}（{why}）"
                               for sid, why in sorted(unmatched_iyuu_sids.items()))
            logger.warn("IYUU 命中的站点没有在 MP 站点管理里勾选/配置："
                        + detail + " → 在③里勾选对应 MP 站点即可，无需手写 sid 或站点 JSON")
        self._finish(stats)

    def _finish(self, stats: Dict[str, int]) -> None:
        self.save_data("last_run", {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **stats})
        logger.info(f"可乐PT 辅种完成：{stats}")
        if stats.get("already"):
            logger.info(f"其中 {stats['already']} 个在客户端里已经有了（同一 info_hash），直接跳过")
        if stats.get("trackers"):
            logger.info(f"另有 {stats['trackers']} 个同 info_hash 的种子补上了新站 announce（不占空间）")
        if self._notify and (stats["added"] or stats["filled"] or stats["failed"]):
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【可乐PT辅种】任务完成",
                text=(f"匹配：{stats['matched']}\n可乐补种：{stats['added']}\n其他站补种：{stats['filled']}\n"
                      + (f"补 tracker：{stats['trackers']}\n" if stats.get("trackers") else "")
                      + f"重复跳过：{stats.get('duplicate', 0)}\n校验失败后降级成功：{stats.get('retried', 0)}\n"
                      + f"跳过：{stats['skipped']}\n失败：{stats['failed']}"
                      + (f"\n客户端已有：{stats['already']}" if stats.get("already") else "")
                      + (f"\n待下一轮重试：{len(self._pending())} 个" if self._pending() else "")),
            )

    # ---------------- 运行状态 ----------------

    def get_state(self) -> bool:
        # 保种组已经搬到独立插件（ColaSeedGroup），这里只看辅种开关
        return bool(self._enabled and self._crossseed_enabled and self._api_key)

    def get_service(self) -> List[Dict[str, Any]]:
        services: List[Dict[str, Any]] = []
        if self._enabled and self._crossseed_enabled and self._cron:
            services.append({
                "id": "ColaCrossSeed",
                "name": "可乐PT自动辅种",
                "trigger": _cron_trigger(self._cron, "0 0 * * *"),
                "func": self.auto_seed,
                "kwargs": {},
            })
        return services

    def stop_service(self) -> None:
        self._event.set()

    # ---------------- 插件页面 / API / 表单 ----------------

    # 主页样式：借 MoviePilot 的 app-card-shell 卡片外观与 Vuetify 主题变量，
    # 深浅色/透明主题都能跟上；类名带 colacrossseed- 前缀，不污染别的插件。
    _PAGE_CSS = """
    .colacrossseed-page { display: flex; flex-direction: column; gap: 12px; }
    .colacrossseed-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .colacrossseed-stat { min-width: 0; padding: 10px 12px; }
    .colacrossseed-stat__head { display: flex; align-items: center; gap: 6px; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .colacrossseed-stat__head .v-icon { color: rgb(var(--app-card-accent-rgb)); }
    .colacrossseed-stat__value { margin-top: 8px; font-size: 1.25rem; font-weight: 700; line-height: 1; }
    .colacrossseed-stat__meta { margin-top: 4px; color: rgba(var(--v-theme-on-surface), .56); font-size: .72rem; line-height: 1.25; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .colacrossseed-section { min-width: 0; }
    .colacrossseed-section-head { display: flex; align-items: center; gap: 8px; min-height: 30px; margin-bottom: 6px; }
    .colacrossseed-section-title { font-size: .95rem; font-weight: 600; letter-spacing: 0; }
    .colacrossseed-collapse { min-width: 0; }
    /* 右侧多留 52px：MP 插件页右下角有固定设置 FAB，避免展开箭头/数量被它盖住 */
    .colacrossseed-collapse .v-expansion-panel-title { min-height: 40px; padding: 0 52px 0 12px !important; }
    .colacrossseed-collapse .v-expansion-panel-text__wrapper { padding: 4px 52px 12px 12px; }
    .colacrossseed-table-wrap { overflow-x: auto; border: 1px solid rgba(var(--v-theme-on-surface), .08); border-radius: 8px; }
    .colacrossseed-table { min-width: 480px; }
    .colacrossseed-table th { height: 34px !important; padding: 0 8px !important; color: rgba(var(--v-theme-on-surface), .62); font-size: .75rem; font-weight: 600 !important; white-space: nowrap; }
    .colacrossseed-table td { height: 36px !important; padding: 0 8px !important; font-size: .8rem; vertical-align: middle; }
    .colacrossseed-table tbody tr:last-child td { border-bottom: 0 !important; }
    .colacrossseed-key { width: 130px; color: rgba(var(--v-theme-on-surface), .62); white-space: nowrap; }
    .colacrossseed-site { font-weight: 600; white-space: nowrap; }
    .colacrossseed-check-name { width: 150px; font-weight: 600; white-space: nowrap; }
    .colacrossseed-meta { color: rgba(var(--v-theme-on-surface), .56); font-size: .72rem; }
    html[data-theme="transparent"] .colacrossseed-table-wrap,
    .v-theme--transparent .colacrossseed-table-wrap { backdrop-filter: blur(var(--transparent-blur, 10px)); background-color: rgba(var(--v-theme-surface), 0) !important; }
    html[data-theme="transparent"] .colacrossseed-table,
    html[data-theme="transparent"] .colacrossseed-table .v-table__wrapper,
    html[data-theme="transparent"] .colacrossseed-table table,
    html[data-theme="transparent"] .colacrossseed-table tbody tr,
    .v-theme--transparent .colacrossseed-table,
    .v-theme--transparent .colacrossseed-table .v-table__wrapper,
    .v-theme--transparent .colacrossseed-table table,
    .v-theme--transparent .colacrossseed-table tbody tr { background-color: transparent !important; }
    @media (max-width: 720px) {
        .colacrossseed-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .colacrossseed-stat { padding: 8px 10px; }
        .colacrossseed-stat__value { font-size: 1.08rem; }
        .colacrossseed-table { min-width: 420px; }
    }
    """

    @staticmethod
    def _page_stat(label: str, value: str, meta: str, color: str, icon: str) -> Dict[str, Any]:
        """主页顶部统计卡片（外观与签到插件一致）。"""
        return {
            "component": "div",
            "props": {"class": "colacrossseed-stat app-card-shell app-card-colorful",
                      "style": f"--app-card-accent-rgb: var(--v-theme-{color});"},
            "content": [
                {"component": "div", "props": {"class": "colacrossseed-stat__head"}, "content": [
                    {"component": "VIcon", "props": {"size": "x-small", "color": color}, "text": icon},
                    {"component": "span", "text": label},
                ]},
                {"component": "div", "props": {"class": f"colacrossseed-stat__value text-{color}"}, "text": value},
                {"component": "div", "props": {"class": "colacrossseed-stat__meta"}, "text": meta},
            ],
        }

    @staticmethod
    def _page_section(title: str, icon: str, count: Optional[int], body: Dict[str, Any]) -> Dict[str, Any]:
        """主页分区：小图标 + 标题 + 右侧数量 chip。"""
        head: List[Dict[str, Any]] = [
            {"component": "VIcon", "props": {"size": "small", "color": "primary"}, "text": icon},
            {"component": "span", "props": {"class": "colacrossseed-section-title"}, "text": title},
            {"component": "VSpacer"},
        ]
        if count is not None:
            head.append({"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "primary"},
                         "text": f"{count} 个"})
        return {"component": "div", "props": {"class": "colacrossseed-section"},
                "content": [{"component": "div", "props": {"class": "colacrossseed-section-head"},
                             "content": head}, body]}

    @staticmethod
    def _page_table(headers: List[str], rows: List[List[Dict[str, Any]]],
                    empty_text: str = "暂无数据") -> Dict[str, Any]:
        """主页紧凑表格；无数据时给一条提示。"""
        if not rows:
            return {"component": "VAlert", "props": {"type": "info", "text": empty_text,
                    "variant": "tonal", "density": "compact", "prepend-icon": "mdi-information"}}
        head = {"component": "thead", "content": [{"component": "tr", "content": [
            {"component": "th", "props": {"class": "text-start"}, "text": h} for h in headers]}]}
        body = {"component": "tbody", "content": [
            {"component": "tr", "content": cells} for cells in rows]}
        return {"component": "div", "props": {"class": "colacrossseed-table-wrap"},
                "content": [{"component": "VTable", "props": {"hover": True, "density": "compact",
                             "class": "colacrossseed-table"}, "content": [head, body]}]}

    @staticmethod
    def _page_td(text: str, cls: str = "") -> Dict[str, Any]:
        props: Dict[str, Any] = {}
        if cls:
            props["class"] = cls
        return {"component": "td", "props": props, "text": text}

    @staticmethod
    def _page_collapsible(title: str, icon: str, count: Optional[int],
                          body: Dict[str, Any], meta: str = "") -> Dict[str, Any]:
        """可折叠分节（手风琴、默认收起，点标题展开）。

        MP 插件页支持 VExpansionPanel（见官方 siteuserdetail），这里照它的写法：
        标题栏放图标 + 标题 + 数量 + 右侧时间，详情放 VExpansionPanelText。
        """
        # 不用 VSpacer：时间和数量紧跟标题左对齐，把最右侧让给展开箭头。
        # MP 插件页右下角有固定的设置 FAB，任何靠最右的内容都会被它盖住。
        title_content: List[Dict[str, Any]] = [
            {"component": "VIcon", "props": {"size": "small", "color": "primary"}, "text": icon},
            {"component": "span", "props": {"class": "colacrossseed-section-title"}, "text": title},
        ]
        if count is not None:
            title_content.append({"component": "VChip", "props": {
                "size": "x-small", "variant": "tonal", "color": "primary"}, "text": str(count)})
        if meta:
            title_content.append({"component": "span", "props": {"class": "colacrossseed-meta"},
                                  "text": meta})
        return {
            "component": "VExpansionPanels",
            "props": {"variant": "accordion", "multiple": True, "class": "colacrossseed-collapse"},
            "content": [{
                "component": "VExpansionPanel",
                "content": [
                    {"component": "VExpansionPanelTitle", "content": [
                        {"component": "div", "props": {
                            "class": "d-flex align-center ga-2 w-100"}, "content": title_content},
                    ]},
                    {"component": "VExpansionPanelText", "content": [body]},
                ],
            }],
        }

    def get_page(self) -> Optional[List[dict]]:
        if not self._api_key:
            return [
                {"component": "div", "text": "还没拿到可乐PT 的 API Key。"},
                {"component": "div", "text": f"最省事：在插件设置①里用下拉选 MP「站点管理」里已填好 API Key 的可乐站，保存即可；"
                                             f"没配过的话到 {COLA_KEY_PAGE} 新建一把 Key，范围勾「读取」和「下载」，"
                                             f"粘到「API Key（手填兜底）」里。接口说明见 https://www.colapt.com/api-docs 。"},
            ]
        last = self.get_data("last_run") or {}
        if not isinstance(last, dict):
            last = {}
        report = self.get_data("test_report") or self._test_report or {}
        # 丢弃老版本遗留的检测报告（没有 schema 标记或版本不符）——里面是
        # 「请粘贴 IYUU 站点 JSON」「域名,passkey」等早已删除的旧文案，升级后残留会误导。
        if not isinstance(report, dict) or report.get("schema") != self._TEST_REPORT_SCHEMA:
            report = {}
        pending = self._pending()

        # ── 顶部：四张统计卡片 ──
        added = int(last.get("added", 0) or 0)
        filled = int(last.get("filled", 0) or 0)
        duplicate = int(last.get("duplicate", 0) or 0)
        already = int(last.get("already", 0) or 0)
        failed = int(last.get("failed", 0) or 0)
        at = str(last.get("at", "") or "")
        run_short = at[5:16] if len(at) >= 16 else (at or "未运行")
        summary = {"component": "div", "props": {"class": "colacrossseed-summary"}, "content": [
            self._page_stat("上次运行", run_short, f"v{self.plugin_version}", "info", "mdi-clock-outline"),
            self._page_stat("本轮补种", str(added + filled), f"可乐 {added} · 其他站 {filled}",
                            "success" if (added + filled) else "info", "mdi-download-network"),
            self._page_stat("跳过 / 已有", str(duplicate + already),
                            f"重复 {duplicate} · 客户端已有 {already}", "warning", "mdi-skip-next"),
            self._page_stat("失败 / 待重试", str(failed + len(pending)),
                            f"失败 {failed} · 待重试 {len(pending)}",
                            "error" if (failed or pending) else "info", "mdi-alert-circle-outline"),
        ]}

        sections: List[Dict[str, Any]] = [summary]

        # 上一轮没加种的原因：显眼地放在最上面
        if last.get("reason"):
            sections.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "density": "compact",
                "prepend-icon": "mdi-information", "text": f"上一轮没加种：{last['reason']}"}})

        # ── 运行状态：目标解析 + 在线下载器 + 站点账目 ──
        try:
            live_clients = self._clients()
        except Exception as err:
            live_clients = []
            logger.info(f"读取下载器列表失败：{type(err).__name__}: {err}")
        live_names = [c.name for c in live_clients]
        wants = self._target_wants()
        runtime_rows: List[List[Dict[str, Any]]] = []
        if wants:
            matched, problems, _ = self._resolve_targets(live_clients) if live_clients else ([], [], [])
            if not problems and matched:
                count_text = "这一台" if len(matched) == 1 else f"这 {len(matched)} 台"
                target_text = (f"{' + '.join(wants)} → {', '.join(c.name for c in matched)}"
                               f"（只往{count_text}加种）")
                target_color = "success"
            else:
                detail = '；'.join(problems) if problems else '目标没匹配到任何下载器'
                target_text = f"{' + '.join(wants)}：{detail}，本轮不会加种。可用：{', '.join(live_names) or '（读不到）'}"
                target_color = "error"
        else:
            target_text = "自动 —— 每台下载器各自匹配、各自加回自己"
            target_color = "info"
        target_cell = self._page_td(target_text)
        target_cell["props"]["class"] = f"text-{target_color}"
        runtime_rows.append([self._page_td("辅种目标", "colacrossseed-key"), target_cell])
        runtime_rows.append([self._page_td("在线下载器", "colacrossseed-key"),
                             self._page_td(" · ".join(live_names) if live_names else "（读不到下载器）")])
        sites = self._other_sites()
        if sites:
            sid_n = sum(1 for x in sites if x.get("sid"))
            rss_n = sum(1 for x in sites if x.get("rsskey"))
            runtime_rows.append([self._page_td("站点", "colacrossseed-key"),
                                 self._page_td(f"生效 {len(sites)} / 勾选 {len(self._mp_site_ids or [])}"
                                               f"（IYUU {sid_n} · RSS {rss_n} · pieces-hash {len(sites)}）")])
        if getattr(self, "_mp_enum_error", ""):
            runtime_rows.append([self._page_td("MP 接口", "colacrossseed-key"),
                                 self._page_td(f"多下载器接口不可用（{self._mp_enum_error}），只列默认下载器")])
        sections.append(self._page_section("运行状态", "mdi-information-outline",
                                           len(live_names) if live_names else None,
                                           self._page_table(["项目", "状态"], runtime_rows)))

        # ── 连通性检测：放在运行状态之后，详情可折叠 ──
        if report:
            lines = [report.get("cola", ""), report.get("iyuu", "")]
            lines += report.get("clients") or []
            lines += report.get("sites") or []
            lines = [x for x in lines if x]
            check_rows: List[List[Dict[str, Any]]] = []
            for line in lines:
                mark, color = "✓", "success"
                if line.startswith("ℹ"):
                    mark, color = "i", "info"
                elif line.startswith("⚠") or "⚠" in line[:3]:
                    mark, color = "!", "warning"
                elif line.startswith("❌"):
                    mark, color = "✕", "error"
                # 去掉开头状态符号后按首个冒号拆成「名称 / 详情」，和站点清单一样分列
                # NOTE: ℹ️/⚠️ 带 Unicode 变体选择符 FE0F，须一并去掉，否则名字里残留怪符号
                cleaned = line.lstrip("✅⚠❌ℹ \ufe0f").strip()
                name, detail = cleaned, ""
                for sep in ("：", ":"):
                    if sep in cleaned:
                        name, detail = cleaned.split(sep, 1)
                        name, detail = name.strip(), detail.strip()
                        break
                check_rows.append([
                    {"component": "td", "props": {"class": f"colacrossseed-check-name text-{color}"},
                     "text": f"{mark} {name}"},
                    self._page_td(detail, "colacrossseed-meta"),
                ])
            sections.append(self._page_collapsible(
                "连通性检测", "mdi-check-network", len(check_rows),
                self._page_table(["项目", "详情"], check_rows),
                meta=str(report.get("at", ""))))
        else:
            sections.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "density": "compact",
                "prepend-icon": "mdi-information",
                "text": "保存一次配置会自动做连通性检测，结果显示在这里。"}})

        # ── 站点清单：表格，通道与上次补种数 ──
        if sites:
            per = last.get("by_site") or {}
            site_rows: List[List[Dict[str, Any]]] = []
            for site in sites:
                name = site.get("name") or site.get("url")
                chans = []
                if site.get("sid"):
                    chans.append("IYUU")
                if site.get("rsskey"):
                    chans.append("RSS")
                chans.append("pieces-hash")
                hit = per.get(name) or per.get(site.get("url")) or {}
                added_n = int(hit.get("added", 0) or 0)
                site_rows.append([
                    self._page_td(name, "colacrossseed-site"),
                    self._page_td(" · ".join(chans), "colacrossseed-meta"),
                    {"component": "td", "props": {"class": "text-right text-success" if added_n else "text-right"},
                     "text": f"+{added_n}" if added_n else "—"},
                ])
            sections.append(self._page_collapsible("站点清单", "mdi-web", len(sites),
                                                   self._page_table(["站点", "通道", "上次补种"], site_rows)))

        # ── 待重试 ──
        if pending:
            retry_rows = [
                [self._page_td(str(x.get("client", "?"))), self._page_td(str(x.get("key", "")), "colacrossseed-meta")]
                for x in pending[-8:]
            ]
            sections.append(self._page_section("下一轮优先重试", "mdi-refresh", len(pending),
                                               self._page_table(["下载器", "种子"], retry_rows)))

        return [{"component": "style", "text": self._PAGE_CSS},
                {"component": "div", "props": {"class": "colacrossseed-page"}, "content": sections}]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.auto_seed,
                "methods": ["POST"],
                "summary": "立即执行一次辅种",
                "description": "手动触发一次辅种任务（仍然遵守每小时上限）",
            },
            {
                "path": "/test",
                "endpoint": self.run_tests,
                "methods": ["GET", "POST"],
                "summary": "检测可乐/下载器/其他站连通性",
                "description": "返回可乐 API Key、每个下载器、每个其他站的连接结果；结果也会显示在插件页",
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
        """设置页排版：总开关 → 账号 → 下载器 → 站点 → 辅种行为。

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
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "crossseed_enabled",
                             "label": "启用辅种（功能一）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "notify", "label": "任务结果通知"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次",
                             "persistent-hint": True, "hint": "无视总开关跑一轮（需已填 API Key），3 秒后执行，跑完自动关掉"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VAlert", "props": {
                                "type": "info", "variant": "tonal", "density": "compact",
                                "text": "本插件只做辅种；保种组已拆到独立插件「可乐保种组」。"
                                        "「启用插件」是总开关，关掉后这里不跑。"
                                        "异常与 API Key 失效提醒始终发送，不受「任务结果通知」影响。"}}]},
                    ]},

                    # ── ① 账号 ──
                    self._form_section("① 可乐PT API Key",
                                       "推荐在下拉里选 MP「站点管理」里已填好 API Key 的可乐站，插件每次保存/重载自动读取，换 Key 不用回这里改。"
                                       "也可以手填一把作兜底；两处都填时以站点管理为准。"
                                       "Key 要勾「读取」+「下载」，插件只放在 X-API-Key 请求头里。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSelect", "props": {
                                "model": "mp_site_id", "label": "MP 站点管理里的可乐站（推荐）",
                                "items": self._mp_site_options(), "clearable": True,
                                "hint": "选一个在站点管理「令牌（API Key）」里填了可乐 Key 的站"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "api_key", "label": "API Key（手填兜底）", "type": "password",
                                "persistent-hint": True,
                                "hint": "读取：查哈希、检测连通；下载：取可乐的种子。接口说明见 www.colapt.com/api-docs"}}]},
                    ]},

                    # ── ② 下载器 ──
                    self._form_section("② 下载器",
                                       "勾「使用 MP 已配置下载器」才会加载下面那组 MP 目标；关掉时只用自定义下载器。"
                                       "MP 多选与手填目标名取并集；两个都留空=自动，每台下载器各自匹配自己的种子、各自加回自己。"
                                       "自定义下载器在下方按行填地址与账号；校验相关设置已移到④区。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "use_mp_clients",
                             "label": "使用 MP 已配置下载器", "persistent-hint": True,
                             "hint": "关掉后下面 MP 目标不生效"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSelect", "props": {
                                "model": "mp_target_clients", "label": "MP 已配置下载器（可多选）",
                                "items": self._mp_downloader_options(), "multiple": True, "chips": True,
                                "clearable": True,
                                "hint": "选中即只往这几台加种；留空则看手填目标，两个都空=自动"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VTextField", "props": {"model": "target_client",
                             "label": "自定义下载器名（手填）",
                             "placeholder": "留空即可；与多选取并集"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextarea", "props": {"model": "clients_lines", "rows": 3, "persistent-hint": True,
                             "label": "辅种自定义下载器（每行一台）",
                             "hint": "地址,用户名,密码；Transmission 写成 tr,地址,用户名,密码。会与上面的 MP 下载器一起使用（并集）",
                             "placeholder": "192.168.1.100:8080,admin,密码\ntr,192.168.1.101:9091,admin,密码"}}]},
                    ]},

                    # ── ③ 站点来源 ──
                    self._form_section("③ 站点来源",
                                       "在下面「MP 已配置站点」里多选要补的站（只存 id，passkey/Cookie 从站点管理现读）。"
                                       "IYUU Token 可选：不填也能补可乐、也能用 pieces-hash/RSS 补其他站；"
                                       "填了额外走 IYUU 云端，命中站点和下载模板由 IYUU 返回，这里只决定补哪些站、用哪套凭据。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VSelect", "props": {
                                "model": "mp_site_ids", "label": "MP 已配置站点（推荐，可多选）",
                                "items": self._mp_site_options(), "multiple": True, "chips": True,
                                "clearable": True,
                                "hint": "只在「双向 / 只补其他站」时需要，只补可乐可留空；凭据每次现读"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextField", "props": {"model": "iyuu_token",
                             "label": "IYUU Token（可选）", "persistent-hint": True,
                             "hint": "不填也能用；填了走云端匹配，站点与下载模板由 IYUU 返回",
                             "placeholder": "IYUU..."}}]},
                    ]},

                    # ── ④ 辅种行为 ──
                    self._form_section("④ 辅种（功能一）",
                                       "开关在最上方「启用辅种（功能一）」；这里决定多久跑一次、补哪个方向、"
                                       "每小时补多少，以及标签和校验策略。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VTextField", "props": {"model": "cron",
                             "label": "辅种运行间隔（cron，5 段）", "persistent-hint": True,
                             "hint": "默认 0 0 * * * = 每日 00:00；例 0 */1 * * * = 每小时。只接受 5 段，写错回落到每日 0 点",
                             "placeholder": "0 0 * * *"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSelect", "props": {"model": "direction", "label": "辅种方向",
                             "persistent-hint": True,
                             "hint": "双向=两条都做；只补可乐=别站资源对应的可乐种拉回来；只补其他站=可乐种去其他站补",
                             "items": [
                                {"title": "双向", "value": "both"}, {"title": "只补可乐", "value": "cola_only"},
                                {"title": "只补其他站", "value": "other_only"}]}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VTextField", "props": {"model": "max_per_hour",
                             "label": "每小时上限（0 = 不限）", "type": "number", "min": 0,
                             "persistent-hint": True,
                             "hint": "按滚动 60 分钟窗口统计成功添加数"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VTextField", "props": {"model": "max_per_site",
                             "label": "单站每轮上限", "type": "number", "min": 1, "persistent-hint": True,
                             "hint": "默认 20；填非数字会连同「每小时上限」一起重置"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VTextField", "props": {"model": "add_label",
                             "label": "辅种标签（可留空）", "placeholder": "cross-seed"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VTextField", "props": {"model": "extra_tags",
                             "label": "额外标签（逗号分隔）", "placeholder": "可乐PT,辅种"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "inherit_tags", "label": "继承原种子标签",
                             "persistent-hint": True, "hint": "把原种子的标签一起带过去"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                            {"component": "VSwitch", "props": {"model": "retry_failed", "label": "失败下轮先重试",
                             "persistent-hint": True, "hint": "上一轮失败的种子下轮优先重试，占当轮预算"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "skip_check", "label": "辅种：跳过校验",
                             "persistent-hint": True,
                             "hint": "默认关。开了客户端直接认为文件完整；文件不在磁盘会 missingFiles，不会自动重下。"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "fallback_skip_check",
                             "label": "校验失败降级为跳过校验重试", "persistent-hint": True,
                             "hint": "默认开。第一次加种失败时用 skip_checking=true 再试一次；任务不暂停。TR 不支持跳过校验。"}}]},
                    ]},

                ],
            }
        ], {
            "enabled": False, "crossseed_enabled": True, "onlyonce": False,
            "notify": True, "cron": "0 0 * * *",
            "api_key": "", "mp_site_id": "",
            "skip_check": False, "max_per_hour": 0, "max_per_site": 20, "direction": "both",
            "use_mp_clients": True, "clients_json": "", "clients_lines": "",
            "iyuu_token": "", "mp_site_ids": [],
            "add_label": "", "extra_tags": "", "inherit_tags": True, "target_client": "",
            "mp_target_clients": [],
            "fallback_skip_check": True, "retry_failed": True,
            "limit_dl_kib": 0, "limit_up_kib": 0, "limit_lines": "",
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
        return out

    def has(self, info_hash: str) -> bool:
        """MP 客户端没有现成的「有没有这颗」接口，用扫描结果做一次短缓存。"""
        if not info_hash:
            return False
        cached = getattr(self, "_hash_cache", None)
        if cached is None or time.time() - getattr(self, "_hash_cache_at", 0) > 30:
            cached = {t.get("hash") for t in self.list_completed() if t.get("hash")}
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
