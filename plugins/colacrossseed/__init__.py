"""
可乐PT 自动辅种（MoviePilot 插件）

逻辑（2026-10-25 定稿）：
  1. 扫用户配置的 qBittorrent / Transmission（可多个，也可用 MoviePilot 已配置的默认下载器）里**已完成**的种子；
  2. 把这些种子的 info_hash 批量交给可乐PT 的 /api/v1/plugin/match 做反向匹配：
       - via=source_info_hash：客户端里有同一份文件（来源是别的站），拉可乐这颗种子回原下载器辅种；
       - via=info_hash：客户端里已经是可乐这颗种子（跳过）；
  3. 可选方向二：对 via=info_hash 的可乐种子，下载可乐的 .torrent 算出 pieces_hash，
     去用户配置的其他 NexusPHP 站点 /api/pieces-hash 查同 pieces 的种子，命中就拉对方种子回来辅种；
  4. 每小时最多添加 N 个（默认 20，滚动 60 分钟窗口），是否跳过校验由用户设置（默认校验）。

登录：可乐PT 用户名 + 密码 + 图形验证码（插件页面显示图片，设置里填 4 位数字后保存）。
"""
import base64
import hashlib
import json
import re
import subprocess
import time
from urllib.parse import urlencode, urljoin
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger
from bencode import bdecode

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

COLA_BASE = "https://www.colapt.com"
# 已关闭 / 已换域名的站点：IYUU 导出里常见旧地址，这里做一次纠正（2026-10-25 核对）。
SITE_REMAP = {
    "www.hddolby.com": "https://www.uhddolby.com",
    "hddolby.com": "https://www.uhddolby.com",
}
SITE_SKIP = {
    "ptsbao.club",       # 烧包：站点已关闭
    "www.ptsbao.club",
}
COLA_MATCH_BATCH = 500
REQUEST_TIMEOUT = 20


class _HttpResult:
    def __init__(self, status: int, content: bytes, text: str):
        self.status_code = status
        self.content = content
        self.text = text

    def json(self):
        return json.loads(self.text)


def _curl(method: str, url: str, headers=None, params=None, json_body=None, data=None, timeout: int = 20) -> _HttpResult:
    """用系统 curl 发请求。

    为什么不用 requests：这台 NAS 上 python/OpenSSL 的 TLS 指纹被 Cloudflare 卡住（握手超时），
    而 curl 同一台机器可以正常访问；插件里对可乐PT 的请求因此走 curl，其它站点仍用 requests。
    """
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", method]
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


class _Client:
    """下载器的最小接口：列出已完成种子、添加种子。"""

    name = ""

    def list_completed(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "") -> bool:
        raise NotImplementedError

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
            if "Ok" not in (rsp.text or ""):
                logger.warn(f"qBittorrent ({self.name}) 登录失败：{rsp.status_code} {rsp.text[:80]}")
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 连接失败：{err}")

    def list_completed(self) -> List[Dict[str, Any]]:
        try:
            rsp = self._session.get(
                f"{self.url}/api/v2/torrents/info",
                params={"filter": "completed"},
                timeout=REQUEST_TIMEOUT,
            )
            rsp.raise_for_status()
            out = []
            for item in rsp.json():
                out.append({
                    "hash": (item.get("hash") or "").lower(),
                    "name": item.get("name") or "",
                    "save_path": item.get("save_path") or item.get("content_path") or "",
                    "category": item.get("category") or "",
                    "size": item.get("size") or item.get("total_size") or 0,
                })
            return out
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 读取种子失败：{err}")
            return []

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "") -> bool:
        data = {
            "savepath": save_path,
            "skip_checking": "true" if skip_check else "false",
            "paused": "false",
            "autoTMM": "false",
        }
        if label:
            data["category"] = label
        try:
            rsp = self._session.post(
                f"{self.url}/api/v2/torrents/add",
                files={"torrents": ("crossseed.torrent", torrent)},
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
            if rsp.status_code == 200 and "Ok" in (rsp.text or "Ok"):
                return True
            logger.warn(f"qBittorrent ({self.name}) 添加种子失败：{rsp.status_code} {rsp.text[:120]}")
        except Exception as err:
            logger.error(f"qBittorrent ({self.name}) 添加种子异常：{err}")
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

    def list_completed(self) -> List[Dict[str, Any]]:
        rsp = self._rpc({
            "method": "torrent-get",
            "arguments": {"fields": ["hashString", "name", "downloadDir", "labels", "percentDone", "totalSize"]},
        })
        if not rsp:
            return []
        out = []
        for item in (rsp.get("arguments") or {}).get("torrents") or []:
            if item.get("percentDone") != 1:
                continue
            out.append({
                "hash": (item.get("hashString") or "").lower(),
                "name": item.get("name") or "",
                "save_path": item.get("downloadDir") or "",
                "category": ",".join(item.get("labels") or []),
                "size": item.get("totalSize") or 0,
            })
        return out

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "") -> bool:
        # Transmission 没有「跳过校验」参数：torrent-add 后由客户端自己校验，
        # 所以这里的 skip_check 只是语义占位，实际等价于总是校验。
        args = {
            "metainfo": base64.b64encode(torrent).decode(),
            "paused": False,
            "download-dir": save_path,
        }
        if label:
            args["labels"] = [label]
        rsp = self._rpc({"method": "torrent-add", "arguments": args})
        if rsp and (rsp.get("result") == "success"):
            return True
        logger.warn(f"Transmission ({self.name}) 添加种子失败：{rsp}")
        return False


class ColaCrossSeed(_PluginBase):
    plugin_name = "可乐PT辅种"
    plugin_desc = "参考 ReseedPuppy / IYUU 辅种插件：把客户端里已有的资源与可乐PT 对照，存在就拉可乐种子自动辅种；反向也支持去别的站补种。"
    plugin_icon = "https://www.colapt.com/favicon.ico"
    plugin_version = "0.4.6"
    plugin_author = "ColaPT"
    author_url = "https://www.colapt.com"
    plugin_config_prefix = "colacrossseed_"
    plugin_order = 21
    auth_level = 1

    _enabled = False
    _notify = True
    _cron = "0 */1 * * *"
    _username = ""
    _password = ""
    _otp_code = ""
    _captcha_code = ""
    _session_token = ""
    _skip_check = False
    _max_per_hour = 20
    _direction = "both"
    _use_mp_clients = True
    _clients_json = ""
    _clients_lines = ""
    _sites_json = ""
    _sites_lines = ""
    _iyuu_json = ""
    _iyuu_token = ""
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
            self._cron = str(config.get("cron") or "0 */1 * * *")
            self._username = str(config.get("username") or "")
            self._password = str(config.get("password") or "")
            self._otp_code = str(config.get("otp_code") or "")
            self._captcha_code = str(config.get("captcha_code") or "")
            self._session_token = str(config.get("session_token") or "")
            self._skip_check = bool(config.get("skip_check", False))
            self._direction = str(config.get("direction") or "both")
            self._use_mp_clients = bool(config.get("use_mp_clients", True))
            self._clients_json = str(config.get("clients_json") or "")
            self._clients_lines = str(config.get("clients_lines") or "")
            self._sites_json = str(config.get("sites_json") or "")
            self._sites_lines = str(config.get("sites_lines") or "")
            self._iyuu_json = str(config.get("iyuu_json") or "")
            self._iyuu_token = str(config.get("iyuu_token") or "")
            try:
                self._max_per_hour = max(1, min(200, int(config.get("max_per_hour", 20) or 20)))
            except Exception:
                self._max_per_hour = 20
        # 保存配置时如果填了验证码，立刻换一次 token（一次性，成功/失败都会清掉验证码字段）。
        if self._captcha_code and self._username and self._password and not self._session_token:
            self._login(self._captcha_code)
            self._captcha_code = ""
            self._persist()
        self._event.clear()
        if self._enabled:
            # 保存后自动检测一次连通性（后台线程，不卡保存）；结果显示在插件页。
            import threading
            threading.Thread(target=self.run_tests, daemon=True).start()

    def _persist(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "username": self._username,
            "password": self._password,
            "otp_code": self._otp_code,
            "captcha_code": "",
            "session_token": self._session_token,
            "skip_check": self._skip_check,
            "max_per_hour": self._max_per_hour,
            "direction": self._direction,
            "use_mp_clients": self._use_mp_clients,
            "clients_json": self._clients_json,
            "clients_lines": self._clients_lines,
            "sites_json": self._sites_json,
            "sites_lines": self._sites_lines,
            "iyuu_json": self._iyuu_json,
            "iyuu_token": self._iyuu_token,
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
            rsp = requests.post(f"{COLA_BASE}/api/v1/plugin/sessions", json=payload, timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 201:
                self._session_token = (rsp.json() or {}).get("session_token") or ""
                self.save_data("captcha_id", "")
                logger.info("可乐PT 登录成功")
                return bool(self._session_token)
            logger.warn(f"可乐PT 登录失败：{rsp.status_code} {rsp.text[:160]}")
        except Exception as err:
            logger.error(f"可乐PT 登录异常：{err}")
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
            self.post_message(mtype=NotificationType.SiteMessage, title="【可乐PT辅种】需要重新登录",
                              text="会话已失效：请打开插件页面获取新的验证码，在插件设置里填写后保存。")
            return None
        if rsp.status_code != 200:
            logger.warn(f"可乐PT {path} 返回 {rsp.status_code}：{rsp.text[:160]}")
        return rsp

    def _match(self, hashes: List[str]) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
        """批量 match，返回 client_hash -> [匹配行]（同一 hash 可能同时命中 info_hash 与 source_info_hash）。"""
        by_hash: Dict[str, List[Dict[str, Any]]] = {}
        for start in range(0, len(hashes), COLA_MATCH_BATCH):
            chunk = hashes[start:start + COLA_MATCH_BATCH]
            rsp = self._cola("GET", "/api/v1/plugin/match", params={"hashes": ",".join(chunk)})
            if rsp is None:
                return by_hash, "可乐PT 匹配请求失败"
            if rsp.status_code != 200:
                return by_hash, f"可乐PT 匹配返回 {rsp.status_code}"
            for item in (rsp.json() or {}).get("matched") or []:
                by_hash.setdefault(item.get("client_hash", ""), []).append(item)
        return by_hash, ""

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
                rsp = requests.post(url, json={"passkey": site.get("passkey", ""), "pieces_hash": [pieces_hash]},
                                    timeout=REQUEST_TIMEOUT)
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
                last = "返回格式不匹配"
                continue
            self._pieces_paths[host] = path
            return data.get(pieces_hash), ""
        # 只有 404/405 才算“不支持”；超时/500 下次还会重试
        if last.startswith("HTTP 404") or last.startswith("HTTP 405"):
            self._pieces_paths[host] = ""
        return None, last

    def _site_download(self, site: Dict[str, Any], torrent_id: str) -> bytes:
        url = f"{(site.get('url') or '').rstrip('/')}/download.php"
        try:
            rsp = requests.get(url, params={"id": torrent_id, "passkey": site.get("passkey", "")},
                               timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200 and _valid_torrent(rsp.content):
                return rsp.content
        except Exception as err:
            logger.warn(f"站点 {site.get('name')} 下载种子失败：{err}")
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
        """每行一个客户端：`地址,用户名,密码`，或 `qb|tr,地址,用户名,密码`，可再带名称。"""
        out: List[_Client] = []
        for line in (self._clients_lines or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = self._split_line(line)
            if len(parts) < 3:
                logger.warn(f"下载器配置行格式不对（至少 地址,用户名,密码）：{line}")
                continue
            kind = "qb"
            name = ""
            if parts[0].lower() in ("qb", "qbittorrent", "tr", "transmission"):
                kind = "tr" if parts[0].lower() in ("tr", "transmission") else "qb"
                parts = parts[1:]
            if len(parts) >= 4:
                name = parts[0]
                parts = parts[1:]
            url = self._normalise_url(parts[0], "http")
            if not name:
                name = url.split("://", 1)[-1]
            if kind == "tr":
                out.append(TransmissionClient(name, url, parts[1], parts[2]))
            else:
                out.append(QBittorrentClient(name, url, parts[1], parts[2]))
        return out

    def _line_sites(self) -> List[Dict[str, str]]:
        """每行一个站点：`域名,passkey`，或 `名称,域名,passkey`。"""
        out: List[Dict[str, str]] = []
        for line in (self._sites_lines or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = self._split_line(line)
            if len(parts) == 2:
                name, url, passkey = "", parts[0], parts[1]
            elif len(parts) >= 3:
                name, url, passkey = parts[0], parts[1], parts[2]
            else:
                logger.warn(f"站点配置行格式不对（域名,passkey）：{line}")
                continue
            url = self._normalise_url(url, "https")
            if not url or not passkey:
                continue
            host = url.split("://", 1)[-1].split("/")[0].lower()
            if host in SITE_SKIP:
                continue
            if host in SITE_REMAP:
                url = SITE_REMAP[host]
            out.append({"name": name or url.split("://", 1)[-1].split("/")[0], "url": url.rstrip("/") + "/",
                        "passkey": passkey, "sid": 0, "download_page": "", "x_api_key": ""})
        return out

    def _iyuu_sites(self) -> List[Dict[str, str]]:
        """直接导入 IYUU 导出的站点 JSON：只取启用且有 passkey 的站点。"""
        raw = (self._iyuu_json or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception as err:
            logger.warn(f"IYUU 站点 JSON 解析失败：{err}")
            return []
        if isinstance(data, dict):
            data = data.get("sites") or list(data.values())
        if not isinstance(data, list):
            return []
        out: List[Dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict) or item.get("disabled"):
                continue
            options = item.get("options") or {}
            if isinstance(options, str):
                try:
                    options = json.loads(options)
                except Exception:
                    options = {}
            if not isinstance(options, dict):
                options = {}
            passkey = str(options.get("passkey") or "").strip()
            if not passkey:
                continue
            base = (item.get("mirror") or item.get("base_url") or "").strip()
            if not base:
                continue
            scheme = "http" if str(item.get("is_https")) == "0" else "https"
            url = self._normalise_url(base, scheme)
            if not url:
                continue
            host = url.split("://", 1)[-1].split("/")[0].lower()
            if host in SITE_SKIP:
                logger.info(f"IYUU 导入跳过已关闭站点：{host}")
                continue
            if host in SITE_REMAP:
                url = SITE_REMAP[host]
            out.append({
                "name": item.get("nickname") or item.get("site") or url,
                "url": url.rstrip("/") + "/",
                "passkey": passkey,
                "sid": int(item.get("sid") or 0),
                "download_page": item.get("download_page") or "",
                "x_api_key": str((options or {}).get("x_api_key") or ""),
            })
        return out

    def _other_sites(self) -> List[Dict[str, str]]:
        """手动行文本 + IYUU 导入，按域名去重。"""
        sites: List[Dict[str, str]] = []
        seen = set()
        for site in self._line_sites() + self._iyuu_sites():
            host = site["url"].split("://", 1)[-1].split("/")[0].lower()
            if not host or host in seen:
                continue
            seen.add(host)
            sites.append(site)
        return sites

    # ---------------- 下载器 ----------------

    def _clients(self) -> List[_Client]:
        out: List[_Client] = []
        if self._use_mp_clients:
            try:
                from app.modules.qbittorrent import Qbittorrent
                qb = Qbittorrent()
                if qb.is_inactive():
                    logger.info("MoviePilot 的 qBittorrent 未启用")
                else:
                    out.append(_MPClient("MP-QB", qb.get_completed_torrents, qb.add_torrent))
            except Exception as err:
                logger.warn(f"读取 MoviePilot qBittorrent 失败：{err}")
            try:
                from app.modules.transmission import Transmission
                tr = Transmission()
                if tr.is_inactive():
                    logger.info("MoviePilot 的 Transmission 未启用")
                else:
                    out.append(_MPClient("MP-TR", tr.get_completed_torrents, tr.add_torrent))
            except Exception as err:
                logger.warn(f"读取 MoviePilot Transmission 失败：{err}")
        line_clients = self._line_clients()
        if line_clients:
            out.extend(line_clients)
        raw = (self._clients_json or "").strip() if not line_clients else ""
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

    # ---------------- 节奏与去重 ----------------

    def _add_times(self) -> List[float]:
        times = self.get_data("add_times") or []
        cutoff = time.time() - 3600
        return [t for t in times if isinstance(t, (int, float)) and t > cutoff]

    def _budget_left(self) -> int:
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

    # ---------------- IYUU 云端辅种（有 token 时优先用，覆盖面比 pieces-hash 大得多） ----------------

    IYUU_API = "http://2025.iyuu.cn"
    IYUU_CHUNK = 100

    def _iyuu_sid_map(self) -> Dict[int, Dict[str, str]]:
        return {int(s["sid"]): s for s in self._other_sites() if s.get("sid")}

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
                    logger.info("IYUU 云端限流，等待 30 秒后重试")
                    time.sleep(30)
                    continue
                logger.warn(f"IYUU 辅种查询返回：{msg}")
                break
            if body.get("code") not in (0, "0", None):
                break
            for client_hash, item in (body.get("data") or {}).items():
                torrents = (item or {}).get("torrent") or []
                if torrents:
                    out[str(client_hash).lower()] = torrents
            time.sleep(1.5)
        return out

    def _site_torrent_url(self, site: Dict[str, str], torrent_id: str) -> str:
        template = (site.get("download_page") or "").strip() or "download.php?id={}&passkey={passkey}"
        path = template.replace("{}", str(torrent_id)).replace("{id}", str(torrent_id)).replace("{passkey}", site.get("passkey", ""))
        return urljoin(site["url"], path)

    def _download_mteam(self, site: Dict[str, str], torrent_id: str) -> bytes:
        """M-Team 没有 pieces-hash，也没有 download.php：先 POST genDlToken 换下载地址，再取。"""
        api_key = site.get("x_api_key") or ""
        if not api_key:
            return b""
        base = (site.get("url") or "").rstrip("/")
        try:
            rsp = requests.post(f"{base}/api/torrent/genDlToken", data={"id": torrent_id},
                                headers={"x-api-key": api_key, "User-Agent": "MoviePilot-ColaCrossSeed/0.5"},
                                timeout=REQUEST_TIMEOUT)
            body = rsp.json() if rsp.status_code == 200 else {}
        except Exception as err:
            logger.warn(f"M-Team 换取下载地址失败：{err}")
            return b""
        if str(body.get("code")) != "0" or not body.get("data"):
            logger.warn(f"M-Team 换取下载地址返回：{body.get('message')}")
            return b""
        try:
            torrent = requests.get(str(body["data"]), timeout=REQUEST_TIMEOUT, allow_redirects=True,
                                   headers={"User-Agent": "Mozilla/5.0"})
            if torrent.status_code == 200 and _valid_torrent(torrent.content):
                return torrent.content
        except Exception as err:
            logger.warn(f"M-Team 下载种子失败：{err}")
        return b""

    def _download_site_torrent(self, site: Dict[str, str], torrent_id: str) -> bytes:
        host = (site.get("url") or "").split("://", 1)[-1].split("/")[0].lower()
        if "m-team" in host:
            return self._download_mteam(site, torrent_id)
        url = self._site_torrent_url(site, torrent_id)
        try:
            rsp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            if rsp.status_code == 200 and _valid_torrent(rsp.content):
                return rsp.content
        except Exception as err:
            logger.warn(f"站点 {site.get('name')} 下载种子失败：{err}")
        return b""

    # ---------------- 连通性检测（保存后自动跑，结果显示在插件页） ----------------

    _test_report: Dict[str, Any] = {}

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

    def _test_one_site(self, site: Dict[str, str]) -> Tuple[str, bool, str]:
        host = site["url"].split("://", 1)[-1].split("/")[0].lower()
        if "m-team" in host:
            if site.get("x_api_key"):
                return site["name"], True, "M-Team：用 IYUU 匹配 + x-api-key 下载（不走 pieces-hash）"
            return site["name"], False, "M-Team：缺少 x_api_key（IYUU 导出里应该有）"
        last = ""
        for path in ("/api/pieces-hash", "/nexusapi/pieces-hash"):
            url = site["url"].rstrip("/") + path
            try:
                rsp = requests.post(
                    url,
                    json={"passkey": site["passkey"], "pieces_hash": ["0" * 40]},
                    timeout=8,
                    headers={"User-Agent": "MoviePilot-ColaCrossSeed/0.3"},
                )
            except Exception as err:
                last = f"连不上：{type(err).__name__}"
                continue
            if rsp.status_code in (404, 405):
                last = "站点没有 pieces-hash 接口（辅种匹配用不了）"
                continue
            if rsp.status_code in (401, 403):
                return site["name"], False, f"passkey 不对或被拒绝（HTTP {rsp.status_code}）"
            if rsp.status_code != 200:
                last = f"HTTP {rsp.status_code}"
                continue
            try:
                body = rsp.json()
            except Exception:
                last = "返回不是 JSON（可能不是 NexusPHP 站）"
                continue
            if isinstance(body.get("data"), (dict, list)):
                self._pieces_paths[host] = path
                return site["name"], True, f"连接成功（{path}）"
            last = "接口返回格式不匹配"
        return site["name"], False, last or "未知错误"

    def _test_sites(self) -> List[str]:
        sites = self._other_sites()
        if not sites:
            return ["ℹ️ 没有配置其他站（可粘贴 IYUU 配置或填「域名,passkey」）"]
        lines: List[str] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for name, ok, msg in pool.map(self._test_one_site, sites):
                lines.append(f"{'✅' if ok else '❌'} {name}：{msg}")
        good = sum(1 for line in lines if line.startswith("✅"))
        lines.insert(0, f"其他站 {len(sites)} 个，成功 {good} 个，失败 {len(sites) - good} 个")
        return lines

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

    def _test_iyuu(self) -> str:
        if not self._iyuu_token:
            return "ℹ️ IYUU Token：未配置（可选；配置后用 IYUU 云端匹配，能覆盖没有 pieces-hash 的站）"
        sid_map = self._iyuu_sid_map()
        if not sid_map:
            return "❌ IYUU Token：已填，但导入的站点没有 sid（请粘贴 IYUU 站点 JSON）"
        sid_sha1 = self._iyuu_sid_sha1(sorted(sid_map))
        if sid_sha1:
            return f"✅ IYUU Token：有效（{len(sid_map)} 个站点已汇报）"
        if getattr(self, "_iyuu_rate_limited", False):
            return "⚠️ IYUU Token：已填，但云端限流（访问频率过快），稍后再试"
        return "❌ IYUU Token：无效或云端拒绝（检查 token）"

    def run_tests(self, persist: bool = True) -> Dict[str, Any]:
        report = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cola": self._test_cola(),
            "iyuu": self._test_iyuu(),
            "clients": self._test_clients(),
            "sites": self._test_sites(),
        }
        self._test_report = report
        if persist:
            self.save_data("test_report", report)
        logger.info(f"连通性检测：{report['cola']}；{report['clients']}；其他站 {report['sites'][0] if report['sites'] else ''}")
        return report

    # ---------------- 任务 ----------------

    def auto_seed(self):
        # 事件是类级 Event：reload/停止会把上一实例的 set 带过来，这里先清掉，
        # 否则本次任务会在第一个 torrent 就 break（表现为 matched=0、耗时 1 秒）。
        self._event.clear()
        if not self._enabled:
            return
        if not self._session_token:
            logger.warn("可乐PT 未登录，跳过本次辅种")
            return
        clients = self._clients()
        if not clients:
            logger.warn("没有可用的下载器")
            return
        sites = []
        if self._direction in ("both", "other_only"):
            sites = self._other_sites()
            if not sites:
                try:
                    sites = [s for s in json.loads(self._sites_json or "[]") if s.get("url") and s.get("passkey")]
                except Exception:
                    sites = []
        if sites:
            logger.info(f"其他站 {len(sites)} 个：{', '.join(s['name'] for s in sites[:6])}{'...' if len(sites) > 6 else ''}")
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0}
        done = set(self._done())
        # 先把所有客户端扫一遍：tracker 不允许同一账号同一颗种子在多个客户端同时做种，
        # 所以“另一边已经有的可乐种”必须跳过，不能只在本客户端内去重。
        scanned = []
        all_hashes = set()
        for client in clients:
            torrents = client.list_completed()
            if torrents:
                scanned.append((client, torrents))
                all_hashes.update(t["hash"] for t in torrents if t.get("hash"))
        # IYUU 云端匹配（有 token 时）：把客户端 hash 发过去，拿回「站点 + torrent_id」。
        sid_map: Dict[int, Dict[str, str]] = {}
        iyuu_hits: Dict[str, List[Dict[str, Any]]] = {}
        if self._iyuu_token and self._direction in ("both", "other_only"):
            sid_map = self._iyuu_sid_map()
            if sid_map:
                hashes_all = sorted(all_hashes)
                sid_sha1 = self._iyuu_sid_sha1(sorted(sid_map))
                if sid_sha1:
                    iyuu_hits = self._iyuu_reseed(hashes_all, sid_sha1)
                    logger.info(f"IYUU 云端查询 {len(hashes_all)} 个 hash，命中 {len(iyuu_hits)} 个可辅种")
        for client, torrents in scanned:
            if self._event.is_set():
                break
            hashes = [t["hash"] for t in torrents if t.get("hash")]
            client_hashes = set(hashes)
            by_hash, err = self._match(hashes)
            if err:
                logger.error(err)
                continue
            for torrent in torrents:
                if self._event.is_set():
                    break
                hits = by_hash.get(torrent["hash"]) or []
                cola_hits = [h for h in hits if h.get("via") == "source_info_hash"]
                own_hits = [h for h in hits if h.get("via") == "info_hash"]
                # 客户端已经有这颗可乐种（同一 info_hash）时不必再拉：qB 会直接回 Fails.（重复种子）。
                cola_hits = [h for h in cola_hits if (h.get("info_hash") or "").lower() not in all_hashes]
                stats["matched"] += 1 if hits else 0
                # 方向一：客户端有同一份文件（来源别的站），拉可乐种子回来辅
                if cola_hits and self._direction in ("both", "cola_only"):
                    hit = cola_hits[0]
                    key = f"{client.name}:cola:{hit.get('id')}"
                    if key in done:
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
                    if client.add(data, torrent.get("save_path", ""), self._skip_check, torrent.get("category", "")):
                        stats["added"] += 1
                        self._record_add()
                        done.add(key)
                        self._remember(key)
                    else:
                        stats["failed"] += 1
                # 方向二：客户端已是可乐种子，去别的站补同 pieces 的种
                elif self._direction in ("both", "other_only") and self._iyuu_token and sid_map:
                    # IYUU 云端匹配：把客户端 hash 发过去，拿回「站点 + torrent_id」。
                    for entry in iyuu_hits.get(torrent["hash"]) or []:
                        site = sid_map.get(int(entry.get("sid") or 0))
                        torrent_id = str(entry.get("torrent_id") or "")
                        if not site or not torrent_id:
                            continue
                        key = f"{client.name}:{site.get('name') or site.get('url')}:{torrent_id}"
                        if key in done:
                            stats["skipped"] += 1
                            continue
                        if self._budget_left() <= 0:
                            logger.info("已达到每小时辅种上限（只统计成功添加的种子），结束本次")
                            self._finish(stats)
                            return
                        remote = self._download_site_torrent(site, torrent_id)
                        if not remote:
                            stats["failed"] += 1
                            continue
                        if client.add(remote, torrent.get("save_path", ""), self._skip_check, torrent.get("category", "")):
                            stats["filled"] += 1
                            self._record_add()
                            done.add(key)
                            self._remember(key)
                        else:
                            stats["failed"] += 1
                elif own_hits and sites and self._direction in ("both", "other_only"):
                    hit = own_hits[0]
                    data = self._download_cola(int(hit.get("id")))
                    pieces = _decode_torrent_pieces(data) if data else ""
                    if not pieces:
                        continue
                    for site in sites:
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
                        if client.add(remote, torrent.get("save_path", ""), self._skip_check, torrent.get("category", "")):
                            stats["filled"] += 1
                            self._record_add()
                            done.add(key)
                            self._remember(key)
                        else:
                            stats["failed"] += 1
        self._finish(stats)

    def _finish(self, stats: Dict[str, int]) -> None:
        self.save_data("last_run", {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **stats})
        logger.info(f"可乐PT 辅种完成：{stats}")
        if self._notify and (stats["added"] or stats["filled"] or stats["failed"]):
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【可乐PT辅种】任务完成",
                text=(f"匹配：{stats['matched']}\n可乐补种：{stats['added']}\n其他站补种：{stats['filled']}\n"
                      f"跳过：{stats['skipped']}\n失败：{stats['failed']}"),
            )

    def get_state(self) -> bool:
        return bool(self._enabled and self._session_token)

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron) if len(str(self._cron).split()) == 5 else self._cron
            except Exception:
                trigger = "0 */1 * * *"
            return [{
                "id": "ColaCrossSeed",
                "name": "可乐PT自动辅种",
                "trigger": trigger,
                "func": self.auto_seed,
                "kwargs": {},
            }]
        return []

    def stop_service(self) -> None:
        self._event.set()

    # ---------------- 插件页面 / API / 表单 ----------------

    def get_page(self) -> Optional[List[dict]]:
        if not self._session_token:
            captcha_id, image = self._fetch_captcha()
            if not image:
                return [{"component": "div", "text": "获取可乐PT 验证码失败，请检查站点可达后重试。"}]
            return [
                {"component": "div", "text": f"可乐PT 登录：验证码 id {captcha_id}（4 位数字），填到插件设置「验证码」后保存。"},
                {"component": "VImg", "props": {"src": image, "max-width": 220, "class": "mt-3"}},
            ]
        last = self.get_data("last_run") or {}
        report = self.get_data("test_report") or self._test_report or {}
        blocks: List[dict] = [
            {"component": "div", "text": f"已登录可乐PT。上次运行：{last.get('at', '未运行')}；"
                                         f"可乐补种 {last.get('added', 0)}，其他站 {last.get('filled', 0)}，失败 {last.get('failed', 0)}。"}
        ]
        if report:
            lines = [f"检测时间 {report.get('at', '')}", report.get("cola", ""), report.get("iyuu", "")]
            lines += report.get("clients") or []
            lines += report.get("sites") or []
            blocks.append({"component": "div", "text": "\n".join([x for x in lines if x]),
                           "props": {"style": "white-space: pre-wrap; line-height: 1.8"}})
        else:
            blocks.append({"component": "div", "text": "保存一次配置会自动做连通性检测，结果显示在这里。"})
        return blocks

    def _fetch_captcha(self) -> Tuple[str, str]:
        try:
            rsp = requests.get(f"{COLA_BASE}/api/v1/plugin/captcha", timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200:
                body = rsp.json() or {}
                captcha_id = body.get("captcha_id") or ""
                self.save_data("captcha_id", captcha_id)
                return captcha_id, body.get("image") or ""
        except Exception as err:
            logger.warn(f"获取可乐PT 验证码失败：{err}")
        return "", ""

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
                "description": "返回可乐登录、每个下载器、每个其他站的连接结果；结果也会显示在插件页",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "use_mp_clients", "label": "使用 MoviePilot 已配置下载器"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "username", "label": "可乐PT 用户名"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "password", "label": "可乐PT 密码", "type": "password"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "captcha_code", "label": "验证码（先在插件页面看图片）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "otp_code", "label": "两步验证码（可选）"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSelect", "props": {"model": "direction", "label": "辅种方向", "items": [
                                {"title": "双向", "value": "both"}, {"title": "只补可乐", "value": "cola_only"},
                                {"title": "只补其他站", "value": "other_only"}]}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {"model": "max_per_hour", "label": "每小时最多添加", "type": "number"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VSwitch", "props": {"model": "skip_check", "label": "跳过校验（默认校验）"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "cron", "label": "执行周期（cron）", "placeholder": "0 */1 * * *"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextarea", "props": {"model": "clients_lines", "rows": 4,
                             "label": "下载器（每行一个：地址,用户名,密码；TR 写成 tr,地址,用户名,密码）",
                             "placeholder": "192.168.1.100:8080,admin,密码\ntr,192.168.1.101:9091,admin,密码"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextarea", "props": {"model": "sites_lines", "rows": 4,
                             "label": "其他站（每行一个：域名,passkey；可写 名称,域名,passkey）",
                             "placeholder": "https://pt.example.com,abcdef123456\n朋友,https://pt.keepfrds.com,abcdef123456"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "iyuu_token", "label": "IYUU Token（可选，填了就用 IYUU 云端辅种匹配）",
                             "placeholder": "IYUU..."}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VTextarea", "props": {"model": "iyuu_json", "rows": 4,
                             "label": "直接导入 IYUU 站点配置（把 IYUU 导出的 JSON 整个粘贴进来，自动取启用的站点）",
                             "placeholder": "[{\"site\":\"keepfrds\",\"nickname\":\"朋友\",\"base_url\":\"pt.keepfrds.com\",\"options\":{\"passkey\":\"...\"}}]"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": False, "notify": True, "cron": "0 */1 * * *",
            "username": "", "password": "", "otp_code": "", "captcha_code": "", "session_token": "",
            "skip_check": False, "max_per_hour": 20, "direction": "both",
            "use_mp_clients": True, "clients_json": "", "clients_lines": "", "sites_json": "", "sites_lines": "", "iyuu_json": "", "iyuu_token": "",
        }


class _MPClient(_Client):
    """把 MoviePilot 的下载器模块包成统一接口。"""

    def __init__(self, name: str, list_func, add_func):
        self.name = name
        self._list = list_func
        self._add = add_func

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
                else:
                    hash_str = getattr(torrent, "hash", None) or getattr(torrent, "hashString", "") or ""
                    name = getattr(torrent, "name", "") or ""
                    save_path = getattr(torrent, "save_path", "") or getattr(torrent, "download_dir", "") or ""
                    category = getattr(torrent, "category", "") or ""
                    size = getattr(torrent, "size", 0) or 0
                if not hash_str:
                    continue
                out.append({"hash": str(hash_str).lower(), "name": name,
                            "save_path": save_path, "category": category, "size": size})
        except Exception as err:
            logger.error(f"MP 下载器 {self.name} 读取失败：{err}")
        return out

    def add(self, torrent: bytes, save_path: str, skip_check: bool, label: str = "") -> bool:
        try:
            return bool(self._add(content=torrent, download_dir=save_path))
        except Exception as err:
            logger.error(f"MP 下载器 {self.name} 添加失败：{err}")
            return False
