"""
可乐PT 自动辅种（MoviePilot 插件）—— 只补可乐

逻辑：
  1. 扫下载器（qBittorrent / Transmission，可多个，也可用 MoviePilot 已配置的下载器）里**已完成**的种子；
  2. 把这些种子的 info_hash 批量交给可乐PT 开放 API 的哈希反查（POST /api/v1/torrents/hashes），
     命中 via=source_info_hash 的（客户端里有同一份文件、来源是别的站），就把可乐这颗种子拉回来加进下载器，
     同一份文件在可乐站也开始做种；哈希没直接中的再用 info.source 归一化 / 内容指纹补查一遍；
  3. 每小时最多添加 N 个（0 = 不限，滚动 60 分钟窗口），是否跳过校验由用户设置（默认校验）。

凭据：可乐PT 开放 API 的 API Key（站点「账号 → Key → API Key」创建，勾「读取」+「下载」），
只放在请求头 X-API-Key 里；不再用账号密码 + 验证码登录，也不带 Cookie / CSRF / Origin。
Key 来源二选一：① 自动识别 MP「站点管理」里 domain/站名含 colapt/可乐的站并读它的 apikey
（换 Key 后自动跟上，用户不用在插件里再选一次）；② 插件设置里手填兜底。接口说明见站点的 /api-docs。
"""
import base64
import hashlib
import json
import re
import subprocess
import time
from urllib.parse import urlencode
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


class _RateLimiter:
    """进程内滑动窗口限流：所有下载器、所有批次共用可乐每把 Key 的额度。"""

    def __init__(self, max_calls: int, window: float):
        self.max_calls = max(1, int(max_calls))
        self.window = float(window)
        self._calls: List[float] = []
        self._lock = Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.time()
                cutoff = now - self.window
                self._calls = [t for t in self._calls if t > cutoff]
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0]) + 0.01
            time.sleep(max(wait, 0.01))


# 可乐 Key：读 60 次/分、下载 120 次/分。统一按 55/分保守控制，给插件页手动检测留余量。
_COLA_LIMITER = _RateLimiter(55, 60.0)


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
    _COLA_LIMITER.acquire()
    result = _run_curl(cmd)
    # 429 不是致命错误：短退避重试两次，别让整轮辅种因为瞬时限流全军覆没。
    for attempt in range(2):
        if result.status_code != 429:
            break
        delay = 2 * (attempt + 1)
        logger.warn(f"可乐PT 限流（429），{delay}s 后重试（{attempt + 1}/2）")
        time.sleep(delay)
        _COLA_LIMITER.acquire()
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



def _valid_torrent(data: bytes) -> bool:
    """站点返回登录页/错误页时也常是 200，交给 qB 会得到 415。这里先做一次 bencode 校验。"""
    try:
        torrent = bdecode(data)
        return isinstance(torrent, dict) and isinstance(torrent.get("info"), dict)
    except Exception:
        return False










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






class ColaCrossSeed(_PluginBase):
    plugin_name = "可乐PT辅种"
    plugin_desc = "把下载器里其它站已完成、而可乐PT 上也有的种子，自动拉可乐种子补进来一起做种。"
    plugin_icon = "https://www.colapt.com/brand/cola/app-red-180.png"
    plugin_version = "0.14.0"
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
    _add_label = ""
    _mp_target_clients: List[str] = []  # 从 MP 已配置下载器里多选的目标（只存名字）
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
    _run_lock = Lock()          # 单轮互斥：保存触发与 cron 同时到点时只跑一轮，避免重复加种
    _key_cooldown_until = 0.0   # Key 失效后的冷却截止时间（epoch 秒）；检测成功即清除
    KEY_COOLDOWN_SEC = 3600     # 失效后 1 小时内不再发无效请求

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
            # 统一 str：老配置里可能存的是 int（早期下拉 value 是 int）。
            self._mp_site_id = str(config.get("mp_site_id") or "").strip()
            # 专用插件：自动识别 MP 站点管理里的可乐站，用户不必在插件里再选一次。
            # 老配置里手选过的 id 继续生效；留空时才自动找。
            if not self._mp_site_id:
                sid, site_name, _ = self._find_cola_site()
                if sid:
                    self._mp_site_id = sid
                    self._cola_site_name = site_name
                    logger.info(f"自动识别 MP 站点管理的可乐站：{site_name}（id={sid}）")
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
            self._add_label = str(config.get("add_label") or "").strip()
            # MP 下载器多选：只存名字（MP-xxx）
            self._mp_target_clients = [str(x).strip() for x in (config.get("mp_target_clients") or []) if str(x).strip()]
            # 升级提示：老版本的自定义下载器字段已忽略，行为变成「自动」，明确告知而不是静默变化。
            # 用户下次保存（新配置里不再有这些旧键）即自动清掉这条提示。
            old_client_cfg = any(str(config.get(k) or "").strip()
                                 for k in ("target_client", "clients_lines", "clients_json"))
            notice = ""
            if old_client_cfg and not self._mp_target_clients:
                notice = "旧版填的自定义下载器已不再支持，目前没勾选任何下载器；请到设置页「② 下载器」选一下。"
            try:
                self.save_data("upgrade_notice", notice)
            except Exception:
                pass
            self._inherit_tags = bool(config.get("inherit_tags", True))
            self._extra_tags = str(config.get("extra_tags") or "").strip()
            self._fallback_skip_check = bool(config.get("fallback_skip_check", True))
            self._retry_failed = bool(config.get("retry_failed", True))
            # 这个上限以前只写回配置、没在 init 里读回来 —— 表单改了不生效（2026-11-23 修）。
            try:
                self._max_per_hour = max(0, int(config.get("max_per_hour", 0) or 0))
            except Exception:
                self._max_per_hour = 0
            try:
                self._limit_dl_kib = max(0, min(10 ** 7, int(config.get("limit_dl_kib", 0) or 0)))
                self._limit_up_kib = max(0, min(10 ** 7, int(config.get("limit_up_kib", 0) or 0)))
            except Exception:
                self._limit_dl_kib = 0
                self._limit_up_kib = 0
            self._limit_lines = str(config.get("limit_lines") or "")
        self._event.clear()
        try:
            self._key_cooldown_until = float(self.get_data("key_cooldown_until") or 0)
        except Exception:
            self._key_cooldown_until = 0.0
        if config is not None:
            self._validate_targets_on_save()
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
        # 只补可乐版本：旧配置里的方向 / 选站 / IYUU / 自定义下载器字段一律不再写入
        #（代码里也已不读取），保存一次后这些遗留键就被覆盖清掉。
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
            "add_label": self._add_label,
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

    def _mp_sites(self) -> List[Any]:
        """MP 站点管理里的全部站点；读不到返回空列表（不猜、不伪造凭据）。"""
        try:
            from app.db.site_oper import SiteOper
            return list(SiteOper().list_order_by_pri() or [])
        except Exception as err:
            logger.warn(f"读取 MoviePilot 站点失败：{err}")
            return []

    @staticmethod
    def _site_api_key(site: Any) -> str:
        """站点 Key：优先索引器适配器里的 apikey，退回站点对象的 apikey 字段。"""
        domain = str(getattr(site, "domain", "") or "").strip()
        if domain:
            try:
                from app.helper.sites import SitesHelper
                indexer = SitesHelper().get_indexer(domain) or {}
                if isinstance(indexer, dict):
                    key = str(indexer.get("apikey") or "").strip()
                    if key:
                        return key
            except Exception:
                pass
        return str(getattr(site, "apikey", "") or "").strip()

    def _find_cola_site(self) -> Tuple[str, str, str]:
        """自动识别 MP 站点管理里的可乐站，返回 (id, 站名, Key)。"""
        for site in self._mp_sites():
            domain = str(getattr(site, "domain", "") or "").lower()
            name = str(getattr(site, "name", "") or "")
            if "colapt" not in domain and "可乐" not in name:
                continue
            sid = str(getattr(site, "id", "") or "").strip()
            if sid:
                return sid, name or domain, self._site_api_key(site)
        return "", "", ""

    def _api_key_from_mp_site(self) -> str:
        """按已记录的站点 id 读 apikey：每次现读，站点管理里换 Key 自动跟上。"""
        if not self._mp_site_id:
            return ""
        for site in self._mp_sites():
            if str(getattr(site, "id", "")) == str(self._mp_site_id):
                return self._site_api_key(site)
        return ""

    # ---------------- 可乐PT 开放 API / 匹配 / 下载 ----------------

    def _set_key_cooldown(self) -> None:
        """Key 被判无效后暂停发请求：避免每轮拿失效 Key 撞接口、刷日志、耗限额。"""
        until = time.time() + self.KEY_COOLDOWN_SEC
        self._key_cooldown_until = until
        try:
            self.save_data("key_cooldown_until", until)
        except Exception:
            pass
        logger.warn(f"可乐PT Key 失效，{int(self.KEY_COOLDOWN_SEC)} 秒内不再发请求；"
                    f"更新 Key 后保存配置或做一次连通性检测即可恢复")

    def _cola_key_source(self) -> str:
        """连通性报告里说明这把 Key 从哪来，便于用户核对是不是站点管理里那把。"""
        if not self._mp_site_id:
            return "插件设置手填"
        name = str(getattr(self, "_cola_site_name", "") or "").strip()
        return f"MP 站点管理（{name or self._mp_site_id}）"

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
            self._set_key_cooldown()
            self._key_alert(f"API Key 无效、已撤销或已过期。若用「MP 站点管理」读取，请更新该站点的 API Key 后重新保存插件；"
                            f"手填的话到 {COLA_KEY_PAGE} 新建一把勾「读取」「下载」的 Key 再填进插件设置。")
        elif rsp.status_code == 403 and self._error_code(rsp) == "api_key_scope_denied":
            logger.warn(f"可乐PT {path}：这把 API Key 缺少需要的范围")
            self._set_key_cooldown()
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
        # createDownloadLink 不收请求体：哪怕发 `{}`，网关也会 400「这个操作不接受请求内容」（#67）
        rsp = self._cola("POST", COLA_DOWNLOAD_LINK_API.format(id=int(torrent_id)))
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



    # ---------------- 配置解析（给不写代码的人用行文本） ----------------

    @staticmethod



    @staticmethod


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


    def _mp_site_options(self) -> List[Dict[str, Any]]:
        """MP 站点列表（内部诊断用；设置页不再让用户选站）。value 统一 str。"""
        options: List[Dict[str, Any]] = []
        for site in self._mp_sites():
            sid = str(getattr(site, "id", "") or "").strip()
            name = str(getattr(site, "name", "") or getattr(site, "domain", "") or sid)
            if not sid or not name:
                continue
            options.append({"title": name, "value": sid})
        return options





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
        """最终目标名单 = 设置页 MP 下载器多选，逐个规范化、去重、保序。留空表示「自动」。"""
        wants: List[str] = []
        for raw in (self._mp_target_clients or []):
            name = self._normalise_target(raw)
            if name and name not in wants:
                wants.append(name)
        return wants

    def _validate_targets_on_save(self) -> None:
        """保存配置时就校验目标下载器：MP 里改名/删实例后当场提示，不等任务静默跑空。"""
        wants = self._target_wants()
        if not wants:
            return
        options = [str(o.get("value") or "") for o in self._mp_downloader_options() if o.get("value")]
        if not options:
            return   # MP 下载器接口暂时读不到时不在保存阶段下结论，交给运行时检测
        lowered = [o.lower() for o in options]
        problems: List[str] = []
        for want in wants:
            w = want.lower()
            if w in lowered or any(w in o or o in w for o in lowered):
                continue
            problems.append(f"目标下载器「{want}」不在 MP「设置 → 下载器」的已配置实例里")
        if problems:
            self._target_state = {
                "wanted": " + ".join(wants), "problems": problems, "available": options,
                "ok": False, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            try:
                self.save_data("target_state", self._target_state)
            except Exception:
                pass
            logger.warn("保存配置时发现辅种目标不可用：" + "；".join(problems))

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
        """把「辅种加到哪台」落实到客户端列表上（只支持 MP 已配置下载器多选）。

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

    def _clients(self) -> List[_Client]:
        """列出下载器：只用 MoviePilot 已配置的 qB/TR 实例。"""
        out: List[_Client] = []
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
        return out

    def _collect_speed_limits(self, clients: List[_Client]) -> List[Dict[str, Any]]:
        """采集每台客户端**当前**的限速设置（不管插件有没有配、有没有接管）。"""
        # 站点契约的 SeedGroupClientLimit 是 additionalProperties:false、整数、1–32 条：
        # 读不到的客户端只把 readable 置 false、数值记 0（「不限速」），**不塞 error 之类的额外字段**，
        # 否则一台读不到会让整份上报 400 —— 那就成了「没上报」，而不是「照实上报没限速」。
        rows: List[Dict[str, Any]] = []
        for client in clients[:32]:
            row: Dict[str, Any] = {"client": str(client.name)[:128],
                                   "kind": "tr" if "tr" in str(client.name).lower() else "qb",
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

    def _detail(self, client: "_Client", torrent: Dict[str, Any], result: str, note: str = "") -> None:
        """本轮逐条明细：失败原因不再只躺在日志里，插件页可自查。"""
        details = getattr(self, "_run_details", None)
        if not isinstance(details, list):
            details = []
            self._run_details = details
        if len(details) >= 40:
            return
        details.append({
            "at": datetime.now().strftime("%H:%M:%S"),
            "client": str(client.name),
            "name": str(torrent.get("name") or torrent.get("save_path") or "")[:120],
            "hash": str(torrent.get("hash") or "")[:12],
            "result": result,
            "note": str(note)[:120],
        })

    def _place(self, client: _Client, data: bytes, torrent: Dict[str, Any], key: str,
               stats: Dict[str, int], bucket: str, hash_hint: str = "") -> bool:
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
        if hash_hint and client.has(hash_hint):
            # 客户端里已经有这颗：qB 一定会回 Fails.，别浪费一次上传，直接记重复
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            self._remember(key)
            self._detail(client, torrent, "重复", "客户端已有这颗")
            return True
        remembered = self._take_tags(hash_hint) if hash_hint else ""
        result = client.add(data, save_path, self._skip_check, label, hash_hint,
                            category=category, auto_tmm=auto_tmm)
        if remembered and result in ("ok", "dup"):
            # 先把种子放回去，再把删之前记下的标签补上（addTags 只追加不覆盖）
            if client.add_tags(hash_hint, remembered):
                logger.info(f"{hash_hint[:8]} 重新补种后已恢复原标签：{remembered}")
        if result == "ok":
            stats[bucket] += 1
            self._record_add()
            self._remember(key)
            self._detail(client, torrent, "已补种", "可乐种子已加入下载器")
            return True
        if result == "dup":
            stats["duplicate"] = stats.get("duplicate", 0) + 1
            self._remember(key)
            # 重复说明客户端里本来就有这颗种子，之前记过的失败可以撤掉
            self._save_pending([x for x in self._pending() if x.get("key") != key])
            self._detail(client, torrent, "重复", "下载器回报重复种子")
            return True
        if not self._skip_check and self._fallback_skip_check:
            retry = client.add(data, save_path, True, label, hash_hint)
            if retry == "ok":
                logger.info(f"校验未通过，已按「跳过校验」重试成功：{key}")
                stats[bucket] += 1
                stats["retried"] = stats.get("retried", 0) + 1
                self._record_add()
                self._remember(key)
                self._detail(client, torrent, "降级补种", "校验失败后跳过校验重试成功")
                return True
            if retry == "dup":
                stats["duplicate"] = stats.get("duplicate", 0) + 1
                self._remember(key)
                self._detail(client, torrent, "重复", "降级重试后回报重复")
                return True
        stats["failed"] += 1
        self._detail(client, torrent, "失败", f"客户端返回 {result}；已进待重试队列")
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
            before = stats["added"]
            self._place(client, data, torrent, key, stats, "added")
            if stats["added"] > before:
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

    @staticmethod
    def _host_from_url(url: str) -> str:
        return (str(url or "").split("://", 1)[-1].split("/")[0]
                .split(":")[0].strip().lower())






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







    def _test_cola(self) -> str:
        """personalStats：200 就说明 Key 能读；顺带显示账号用户名。下载范围要到真取种时才知道。"""
        if not self._api_key:
            return f"❌ 可乐PT：未填 API Key（在 {COLA_KEY_PAGE} 创建，勾「读取」「下载」）"
        rsp = self._cola("GET", COLA_STATS_API)
        if rsp is None:
            return "❌ 可乐PT：连不上（网络/防火墙），稍后重试"
        if rsp.status_code == 200:
            # 检测通过即解除失效冷却，不必等满 1 小时
            self._key_cooldown_until = 0.0
            try:
                self.save_data("key_cooldown_until", 0)
            except Exception:
                pass
            data = rsp.json({}) or {}
            user = data.get("user") if isinstance(data, dict) else None
            username = ""
            if isinstance(user, dict):
                username = str(user.get("username") or "").strip()
            if not username and isinstance(data, dict):
                username = str(data.get("username") or "").strip()
            return f"✅ 可乐PT：API Key 可用（{username or '未知用户'}）· Key 来源：{self._cola_key_source()}"
        if rsp.status_code == 401:
            return (f"❌ 可乐PT：API Key 无效或已过期；去 MP 站点管理更新可乐站的 Key，"
                    f"或在插件设置里重填（{COLA_KEY_PAGE}）")
        if rsp.status_code == 403:
            if self._error_code(rsp) == "api_key_scope_denied":
                return "❌ 可乐PT：API Key 缺少「读取」范围，请新建一把勾「读取」「下载」的 Key"
            return "❌ 可乐PT：账号没有权限（被停用或等级不够）"
        if rsp.status_code == 429:
            return "⚠️ 可乐PT：请求太频繁（每把 Key 每分钟有额度），稍后自动恢复"
        return f"❌ 可乐PT：HTTP {rsp.status_code}"


    # 报告格式版本：schema=3 起只检测可乐 API Key 与下载器（只补可乐版本）；
    # 老版本报告里带 iyuu/sites 字段和「粘贴 IYUU 站点 JSON」等废弃文案，
    # get_page 读到版本不符的就丢弃，免得升级后还显示旧检测结论。
    _TEST_REPORT_SCHEMA = 3

    def run_tests(self, persist: bool = True) -> Dict[str, Any]:
        report = {
            "schema": self._TEST_REPORT_SCHEMA,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cola": self._test_cola(),
            "clients": self._test_clients(),
        }
        self._test_report = report
        if persist:
            self.save_data("test_report", report)
        logger.info(f"连通性检测：{report['cola']}；{report['clients']}")
        return report

    # ---------------- 任务 ----------------

    def _run_once(self) -> None:
        """「立即运行一次」：等保存流程结束再跑一轮，避免和配置保存抢资源。"""
        time.sleep(3)
        # force=True：对齐官方 iyuuautoseed 的 get_state() or _onlyonce，
        # 手动触发无条件跑一轮，不受启用总开关/辅种开关限制。
        self.auto_seed(force=True)

    def auto_seed(self, force: bool = False):
        """辅种主流程：Key 冷却、单轮互斥，异常只记日志 + 站内通知，不报「执行失败」。"""
        if not force and time.time() < (getattr(self, "_key_cooldown_until", 0) or 0):
            logger.warn("可乐PT Key 处于失效冷却期，跳过本轮；更新 Key 后保存配置即可恢复")
            return
        if not self._run_lock.acquire(blocking=False):
            logger.info("上一轮辅种仍在执行，跳过本轮（避免重复加种和重复消耗 API 额度）")
            return
        try:
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
        finally:
            self._run_lock.release()

    def _auto_seed(self, force: bool = False):
        # 事件是类级 Event：reload/停止会把上一实例的 set 带过来，这里先清掉，
        # 否则本次任务会在第一个 torrent 就 break（表现为 matched=0、耗时 1 秒）。
        self._event.clear()
        self._run_details = []   # 本轮逐条明细，_finish 落盘供插件页查看
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
            stats = {"matched": 0, "added": 0, "skipped": 0, "failed": 0,
                     "duplicate": 0, "retried": 0, "already": 0}
            self.save_data("last_run", {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **stats,
                "reason": (f"辅种目标 {state.get('wanted')} 匹配到多台下载器，本轮跳过"
                           if state.get("ambiguous") else
                           f"辅种目标 {state.get('wanted')} 不存在，本轮跳过")})
            return
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
        stats = {"matched": 0, "added": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0}
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
            wants = self._target_wants()
            wanted = "、".join(wants)
            logger.warn(f"没有扫描到任何种子（{f'目标下载器 {wanted}' if wanted else '全部下载器'}），"
                        f"本轮不会加种。检查下载器连通性与「已完成」列表")
            if wanted and all_hashes:
                logger.warn(f"目标下载器 `{wanted}` 读不到种子，但其它下载器有 {len(all_hashes)} 颗 —— "
                            f"重点查目标机的地址/账号，或它是不是被选成了空客户端")
            self.save_data("scan_empty", {
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "target": wanted,
                "others": len(all_hashes)})
        preferred = self._preferred_clients(scanned)
        if len(scanned) > 1:
            logger.info(f"下载器 {len(scanned)} 个；同一颗种子跨客户端的归属已按「谁在做种」决定")
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
                hits = by_hash.get(torrent["hash"]) or []
                # 只补可乐：客户端有同一份文件（来源别的站），把可乐那份种子拉回来补种。
                cola_hits = [h for h in hits if h.get("via") == "source_info_hash"]
                # 客户端已经有这颗可乐种（同一 info_hash）时不必再拉：qB 会直接回 Fails.（重复种子）。
                kept = [h for h in cola_hits if (h.get("info_hash") or "").lower() not in all_hashes]
                if len(kept) != len(cola_hits):
                    stats["already"] = stats.get("already", 0) + (len(cola_hits) - len(kept))
                cola_hits = kept
                stats["matched"] += 1 if hits else 0
                if cola_hits:
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
        self._finish(stats)

    def _finish(self, stats: Dict[str, int]) -> None:
        self.save_data("last_run", {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **stats})
        try:
            self.save_data("last_details", (getattr(self, "_run_details", None) or [])[:20])
        except Exception:
            pass
        logger.info(f"可乐PT 辅种完成：{stats}")
        if stats.get("already"):
            logger.info(f"其中 {stats['already']} 个在客户端里已经有了（同一 info_hash），直接跳过")
        if self._notify and (stats["added"] or stats["failed"]):
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【可乐PT辅种】任务完成",
                text=(f"匹配：{stats['matched']}\n可乐补种：{stats['added']}\n"
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
    /* 折叠区与页面其它区块统一左右 12px，不再为右下角的设置 FAB 单独收窄 */
    .colacrossseed-collapse .v-expansion-panel-title { min-height: 40px; padding: 0 12px !important; }
    .colacrossseed-collapse .v-expansion-panel-text__wrapper { padding: 4px 12px 12px; }
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
        # 不用 VSpacer：时间和数量紧跟标题左对齐，展开箭头由 VExpansionPanelTitle 自己放最右。
        # 不再为右下角的设置 FAB 预留空间，保持折叠区与其它区块同宽。
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
                {"component": "div", "text": f"最省事：在 MP「站点管理」里给可乐站填好 API Key，插件会自动识别，不用回插件里选站；"
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
        duplicate = int(last.get("duplicate", 0) or 0)
        already = int(last.get("already", 0) or 0)
        failed = int(last.get("failed", 0) or 0)
        at = str(last.get("at", "") or "")
        run_short = at[5:16] if len(at) >= 16 else (at or "未运行")
        summary = {"component": "div", "props": {"class": "colacrossseed-summary"}, "content": [
            self._page_stat("上次运行", run_short, f"v{self.plugin_version}", "info", "mdi-clock-outline"),
            self._page_stat("本轮补种", str(added), "可乐补种",
                            "success" if added else "info", "mdi-download-network"),
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
        # 老配置只用过自定义下载器：升级后没勾选任何下载器，就在这一行说清楚，不另起一段提示
        notice = self.get_data("upgrade_notice") or ""
        notice = notice.strip() if isinstance(notice, str) else ""
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
        elif notice:
            target_text = notice
            target_color = "warning"
        else:
            target_text = "还没勾选下载器，请到设置页「② 下载器」选一下"
            target_color = "warning"
        target_cell = self._page_td(target_text)
        target_cell["props"]["class"] = f"text-{target_color}"
        runtime_rows.append([self._page_td("辅种目标", "colacrossseed-key"), target_cell])
        runtime_rows.append([self._page_td("在线下载器", "colacrossseed-key"),
                             self._page_td(" · ".join(live_names) if live_names else "（读不到下载器）")])
        if getattr(self, "_mp_enum_error", ""):
            runtime_rows.append([self._page_td("MP 接口", "colacrossseed-key"),
                                 self._page_td(f"多下载器接口不可用（{self._mp_enum_error}），只列默认下载器")])
        sections.append(self._page_section("运行状态", "mdi-information-outline",
                                           len(live_names) if live_names else None,
                                           self._page_table(["项目", "状态"], runtime_rows)))

        # ── 连通性检测：放在运行状态之后，详情可折叠 ──
        if report:
            lines = [report.get("cola", "")]
            lines += report.get("clients") or []
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

        # ── 待重试 ──
        if pending:
            retry_rows = [
                [self._page_td(str(x.get("client", "?"))), self._page_td(str(x.get("key", "")), "colacrossseed-meta")]
                for x in pending[-8:]
            ]
            sections.append(self._page_section("下一轮优先重试", "mdi-refresh", len(pending),
                                               self._page_table(["下载器", "种子"], retry_rows)))

        # 保存配置时就校出的目标问题：当场可见，不用等任务静默跑空再猜
        tstate = self.get_data("target_state") or {}
        if isinstance(tstate, dict) and not tstate.get("ok") and tstate.get("problems"):
            sections.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal", "density": "compact",
                "title": "辅种目标下载器不可用",
                "text": "；".join(str(x) for x in tstate["problems"])
                        + "。到设置页「② 下载器」重新选一下。"}})

        # 本轮逐条明细：成功/重复/失败与原因都落盘，失败不再只躺在日志里
        details = self.get_data("last_details") or []
        if isinstance(details, list) and details:
            detail_rows = [
                [self._page_td(str(x.get("at", ""))), self._page_td(str(x.get("client", ""))),
                 self._page_td(str(x.get("name", ""))[:60]),
                 self._page_td(str(x.get("result", "")), "colacrossseed-meta"),
                 self._page_td(str(x.get("note", "")), "colacrossseed-meta")]
                for x in details[:20]
            ]
            sections.append(self._page_collapsible(
                "本轮明细", "mdi-list-status", len(detail_rows),
                self._page_table(["时间", "下载器", "种子", "结果", "说明"], detail_rows)))

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
                "summary": "检测可乐 API Key / 下载器连通性",
                "description": "返回可乐 API Key 与每个 MP 下载器的连接结果；结果也会显示在插件页",
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
        """设置页排版：总开关 → API Key → 下载器 → 辅种行为。

        只重排、不改字段名：每个 `model` 都对应 init_plugin 读的那个键，defaults 与之一一对应。
        """
        # 站点下拉默认选上自动识别到的可乐站；用户仍能自己改。
        default_site = self._mp_site_id or self._find_cola_site()[0] or ""
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

                    # ── ① Key ──
                    self._form_section("① 可乐PT API Key",
                                       "优先读取下面选中站点在 MP「站点管理」里填的令牌，也可以手填一把。两处都填时以站点管理为准；Key 要勾「读取」和「下载」。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSelect", "props": {
                                "model": "mp_site_id", "label": "MP 站点管理里的可乐站",
                                "items": self._mp_site_options(), "clearable": True,
                                "hint": "默认选中自动识别到的可乐站；确认一下，不对可以改"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "api_key", "label": "API Key（手填兜底）", "type": "password",
                                "hint": "站点管理没填时才用这里；到 www.colapt.com/api-docs 查看接口"}}]},
                    ]},

                    # ── ② 下载器 ──
                    self._form_section("② 下载器",
                                       "从 MP「设置 → 下载器」里已配置的实例中选，种子只补进选中的下载器。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VSelect", "props": {
                                "model": "mp_target_clients", "label": "辅种目标下载器（可多选）",
                                "items": self._mp_downloader_options(), "multiple": True, "chips": True,
                                "hint": "选中即只往这几台加种；名字解析不到或有歧义时本轮不加种"}}]},
                    ]},

                    # ── ③ 辅种行为 ──
                    self._form_section("③ 辅种行为",
                                       "插件固定「只补可乐」：扫描下载器里其它站已完成、可乐上也有的种子，把可乐那份拉回来一起做种。"
                                       "这里决定多久跑一次、每小时补多少，以及标签和校验策略。"),
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "cron",
                             "label": "辅种运行间隔（cron，5 段）", "persistent-hint": True,
                             "hint": "默认 0 0 * * * = 每日 00:00；例 0 */1 * * * = 每小时。只接受 5 段，写错回落到每日 0 点",
                             "placeholder": "0 0 * * *"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "max_per_hour",
                             "label": "每小时上限（0 = 不限）", "type": "number", "min": 0,
                             "persistent-hint": True,
                             "hint": "按滚动 60 分钟窗口统计成功添加数"}}]},
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
            "api_key": "", "mp_site_id": default_site,
            "skip_check": False, "max_per_hour": 0,
            "add_label": "", "extra_tags": "", "inherit_tags": True,
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
