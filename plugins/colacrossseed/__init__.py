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
import time
import uuid
from datetime import datetime
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger
from bencode import bdecode

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

COLA_BASE = "https://www.colapt.com"
COLA_MATCH_BATCH = 500
REQUEST_TIMEOUT = 20


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
    plugin_version = "0.2.0"
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
    _event = Event()
    _csrf = ""

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

    def _cola(self, method: str, path: str, **kwargs) -> Optional[requests.Response]:
        headers = kwargs.pop("headers", {})
        headers["Cookie"] = f"reelcore_session={self._session_token}"
        try:
            rsp = requests.request(method, f"{COLA_BASE}{path}", headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
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
            rsp = requests.post(
                f"{COLA_BASE}/api/v1/torrents/{torrent_id}/authorizations",
                headers={
                    "Cookie": f"reelcore_session={self._session_token}",
                    "X-CSRF-Token": csrf,
                    "Origin": COLA_BASE,
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
                json={},
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
        return rsp.content

    def _site_lookup(self, site: Dict[str, Any], pieces_hash: str) -> Tuple[Optional[str], str]:
        url = (site.get("url") or "").rstrip("/") + "/api/pieces-hash"
        try:
            rsp = requests.post(url, json={"passkey": site.get("passkey", ""), "pieces_hash": [pieces_hash]},
                                timeout=REQUEST_TIMEOUT)
            if rsp.status_code != 200:
                return None, f"HTTP {rsp.status_code}"
            data = ((rsp.json() or {}).get("data") or {})
            if isinstance(data, dict):
                return data.get(pieces_hash), ""
            return None, "response data is not a dict"
        except Exception as err:
            return None, str(err)

    def _site_download(self, site: Dict[str, Any], torrent_id: str) -> bytes:
        url = f"{(site.get('url') or '').rstrip('/')}/download.php"
        try:
            rsp = requests.get(url, params={"id": torrent_id, "passkey": site.get("passkey", "")},
                               timeout=REQUEST_TIMEOUT)
            if rsp.status_code == 200:
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
            out.append({"name": name or url.split("://", 1)[-1].split("/")[0], "url": url + "/", "passkey": passkey})
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
            out.append({"name": item.get("nickname") or item.get("site") or url, "url": url + "/", "passkey": passkey})
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

    def _take_slot(self) -> bool:
        times = self._add_times()
        if len(times) >= self._max_per_hour:
            return False
        times.append(time.time())
        self.save_data("add_times", times)
        return True

    def _done(self) -> List[str]:
        return self.get_data("done") or []

    def _remember(self, key: str) -> None:
        done = self._done()
        if key not in done:
            done.append(key)
        self.save_data("done", done[-10000:])

    # ---------------- 任务 ----------------

    def auto_seed(self):
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
                    if not self._take_slot():
                        logger.info("已达到每小时辅种上限，结束本次")
                        self._finish(stats)
                        return
                    data = self._download_cola(int(hit.get("id")))
                    if not data:
                        stats["failed"] += 1
                        continue
                    if client.add(data, torrent.get("save_path", ""), self._skip_check, torrent.get("category", "")):
                        stats["added"] += 1
                        done.add(key)
                        self._remember(key)
                    else:
                        stats["failed"] += 1
                # 方向二：客户端已是可乐种子，去别的站补同 pieces 的种
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
                        if not self._take_slot():
                            logger.info("已达到每小时辅种上限，结束本次")
                            self._finish(stats)
                            return
                        remote = self._site_download(site, str(remote_id))
                        if not remote:
                            stats["failed"] += 1
                            continue
                        if client.add(remote, torrent.get("save_path", ""), self._skip_check, torrent.get("category", "")):
                            stats["filled"] += 1
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
        return [{"component": "div", "text": f"已登录可乐PT。上次运行：{last.get('at', '未运行')}；"
                                            f"可乐补种 {last.get('added', 0)}，其他站 {last.get('filled', 0)}，失败 {last.get('failed', 0)}。"}]

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
        return [{
            "path": "/run",
            "endpoint": self.auto_seed,
            "methods": ["POST"],
            "summary": "立即执行一次辅种",
            "description": "手动触发一次辅种任务（仍然遵守每小时上限）",
        }]

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
            "use_mp_clients": True, "clients_json": "", "clients_lines": "", "sites_json": "", "sites_lines": "", "iyuu_json": "",
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
