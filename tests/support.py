"""把插件从 MoviePilot 运行时里剥出来，让功能测试可以离线跑。

插件本身 `from app.log import logger` / `from app.plugins import _PluginBase` /
`from bencode import bdecode` / `from apscheduler...`，直接 import 会连带启动 MP。
这里在缺失时打桩（真实依赖存在时优先用真的），于是同一套测试能跑在：
  · CI（pip 装 requests/bencode/apscheduler）
  · 本机裸 python（只有 requests）
  · MP 容器里（全都有）
"""

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins.v2" / "colacrossseed" / "__init__.py"


def _loadable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _module(name: str, **attrs):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class _SilentLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _StubPluginBase:
    def __init__(self, *args, **kwargs):
        pass


def _mini_bdecode(data: bytes):
    """最小 bencode 解码器（只在 bencode 包没装时用来构造测试数据）。"""

    def parse(pos: int):
        char = data[pos:pos + 1]
        if char == b"i":
            end = data.index(b"e", pos)
            return int(data[pos + 1:end]), end + 1
        if char == b"l":
            pos += 1
            out = []
            while data[pos:pos + 1] != b"e":
                value, pos = parse(pos)
                out.append(value)
            return out, pos + 1
        if char == b"d":
            pos += 1
            out = {}
            while data[pos:pos + 1] != b"e":
                key, pos = parse(pos)
                value, pos = parse(pos)
                out[key] = value
            return out, pos + 1
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        return data[start:start + length], start + length

    value, _ = parse(0)
    return value


def _mini_bencode(value) -> bytes:
    if isinstance(value, bool):
        raise TypeError("bencode 没有 bool")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, (bytes, bytearray)):
        return b"%d:%s" % (len(value), bytes(value))
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(_mini_bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = []
        for key, val in value.items():
            key = key.encode() if isinstance(key, str) else key
            items.append((key, val))
        items.sort(key=lambda kv: kv[0])
        return b"d" + b"".join(_mini_bencode(k) + _mini_bencode(v) for k, v in items) + b"e"
    raise TypeError(f"不支持的 bencode 类型：{type(value).__name__}")


def _install_stubs() -> None:
    if not _loadable("app.log"):
        _module("app").__path__ = []          # 当包用，from app.plugins import … 才走得通
        _module("app.log", logger=_SilentLogger())
    if not _loadable("app.plugins"):
        if "app" in sys.modules and not getattr(sys.modules["app"], "__path__", None):
            sys.modules["app"].__path__ = []
        _module("app.plugins", _PluginBase=_StubPluginBase)
    if not _loadable("app.schemas"):
        _module("app.schemas", NotificationType=type(
            "NotificationType", (), {"SiteMessage": "站点"}))
    if not _loadable("bencode"):
        _module("bencode", bdecode=_mini_bdecode, bencode=_mini_bencode)
    if not _loadable("apscheduler.triggers.cron"):
        _module("apscheduler").__path__ = []
        _module("apscheduler.triggers").__path__ = []
        _module("apscheduler.triggers.cron", CronTrigger=type("CronTrigger", (), {}))


def load_plugin():
    """导入插件模块（带桩），返回 module。"""
    _install_stubs()
    name = "colacrossseed_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    """下载器替身：记录调用，按需返回 ok / dup / fail。"""

    name = "fake-qb"

    def __init__(self, has_hash=(), add_result="ok"):
        self._has = set(has_hash)
        self._add_result = add_result
        self.added = []
        self.trackers = []
        self.deleted = []

    def list_completed(self):
        return []

    def has(self, info_hash):
        return info_hash in self._has

    def add(self, torrent, save_path, skip_check, label="", hash_hint=""):
        self.added.append({"save_path": save_path, "skip_check": skip_check,
                           "label": label, "hash_hint": hash_hint})
        return self._add_result

    def add_trackers(self, info_hash, urls):
        self.trackers.append((info_hash, list(urls)))
        return True

    def tagged_missing(self, tag):
        return []

    def delete(self, hashes, delete_files=False):
        self.deleted.extend(hashes)
        return True


def make_plugin(module, store=None, **attrs):
    """绕过 _PluginBase.__init__ 造一个实例，插件数据换成内存字典。"""
    plugin = object.__new__(module.ColaCrossSeed)
    plugin._store = {} if store is None else store

    def get_data(key, default=None):
        value = plugin._store.get(key, default)
        return value

    def save_data(key, value):
        plugin._store[key] = value

    plugin.get_data = get_data
    plugin.save_data = save_data
    plugin.post_message = lambda **kwargs: None
    plugin._sites_lines = attrs.pop("sites_lines", "")
    plugin._sites_json = attrs.pop("sites_json", "")
    plugin._iyuu_json = attrs.pop("iyuu_json", "")
    plugin._iyuu_token = attrs.pop("iyuu_token", "")
    plugin._sites_mode = attrs.pop("sites_mode", "merge")
    plugin._event = module.Event()
    plugin._pieces_paths = {}
    plugin._site_counter = {}
    plugin._site_fails = {}
    plugin._iyuu_rate_limited = False
    plugin._max_per_hour = 0
    plugin._max_per_site = 20
    plugin._skip_check = True
    plugin._fallback_skip_check = True
    plugin._retry_failed = True
    plugin._add_label = ""
    plugin._inherit_tags = True
    plugin._extra_tags = ""
    plugin._target_client = ""
    for key, value in attrs.items():
        setattr(plugin, key, value)
    return plugin


def make_torrent(announce: str = "http://tracker.example/announce",
                 announce_list=None, name: str = "demo.mkv",
                 source: str = "") -> bytes:
    """拼一颗结构合法的最小 .torrent（键顺序固定，便于断言 info_hash）。"""
    info = {
        b"length": 1024,
        b"name": name.encode(),
        b"piece length": 16384,
        b"pieces": b"\x00" * 20,
        b"private": 1,
    }
    if source:
        info[b"source"] = source.encode()
    blob = b"d" + b"8:announce" + _mini_bencode(announce)
    if announce_list:
        blob += b"13:announce-list" + _mini_bencode(announce_list)
    blob += b"4:info" + _mini_bencode(info) + b"e"
    return blob


def info_hash_of(torrent: bytes) -> str:
    """测试侧独立算一遍 info_hash（不借用插件实现，避免自证）。"""
    import hashlib
    pos = 1
    while torrent[pos:pos + 1] != b"e":
        colon = torrent.index(b":", pos)
        length = int(torrent[pos:colon])
        key = torrent[colon + 1:colon + 1 + length]
        value_start = colon + 1 + length
        value_end = _skip(torrent, value_start)
        if key == b"info":
            return hashlib.sha1(torrent[value_start:value_end]).hexdigest()
        pos = value_end
    raise AssertionError("没有 info 字典")


def _skip(buf: bytes, pos: int) -> int:
    char = buf[pos:pos + 1]
    if char == b"i":
        return buf.index(b"e", pos) + 1
    if char in (b"l", b"d"):
        pos += 1
        while buf[pos:pos + 1] != b"e":
            if char == b"d":
                pos = _skip(buf, pos)
            pos = _skip(buf, pos)
        return pos + 1
    colon = buf.index(b":", pos)
    return colon + 1 + int(buf[pos:colon])


class FakeMPModule:
    """假扮 MoviePilot 的下载器模块：记录 add_torrent 收到了什么参数。"""

    def __init__(self, torrents=None, accept=True, strict=False):
        self.torrents = list(torrents or [])
        self.accept = accept
        self.strict = strict
        self.calls = []

    def list(self):
        return list(self.torrents)

    def add(self, **kwargs):
        # strict 模拟老版本 MP 的 add_torrent：只认 content/download_dir，
        # 多传参数就抛 TypeError（插件的降级分支应该能兜住）。
        if self.strict and set(kwargs) - {"content", "download_dir"}:
            self.calls.append({"rejected": sorted(kwargs)})
            raise TypeError("unexpected keyword arguments")
        self.calls.append(kwargs)
        return (self.accept, ["id"] if self.accept else [])
