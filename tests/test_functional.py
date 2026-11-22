"""辅种插件的功能测试：不联网、不依赖 MoviePilot 运行时。

覆盖 2026-11-02 那一轮修的东西：
  · 站点行解析、下载模板占位符
  · RSS 解析（含坏 CDATA 兜底）与「两次拉取 ≥120 秒」的强制间隔
  · 拉取失败不覆盖已有索引
  · info_hash / announce 提取、重复种子短路、单站预算
  · 连通性检测的通道判定与汇总口径
"""

import hashlib
import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402

mod = support.load_plugin()

RSS_OK = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>demo</title>
<item><title>Movie.A.2024</title>
  <guid>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</guid>
  <enclosure url="https://demo.example/download.php?id=11&amp;passkey=K" length="1024" type="application/x-bittorrent"/>
</item>
<item><title>Movie.B.2024</title>
  <guid>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</guid>
  <enclosure url="https://demo.example/download.php?id=12&amp;passkey=K" length="2048" type="application/x-bittorrent"/>
</item>
</channel></rss>"""

RSS_BROKEN_CDATA = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item><title><![CDATA[Movie.C.2024]]></title>
  <guid>cccccccccccccccccccccccccccccccccccccccc</guid>
  <enclosure url="https://demo.example/download.php?id=13&amp;passkey=K" length="4096"/>
  <description><![CDATA[ 这里坏掉了，没有闭合
</item>
<item><title>Movie.D.2024</title>
  <guid>dddddddddddddddddddddddddddddddddddddddd</guid>
  <enclosure url="https://demo.example/download.php?id=14&amp;passkey=K" length="8192"/>
</item>
</channel></rss>"""


def site(**over):
    base = {
        "name": "演示", "url": "https://demo.example/", "passkey": "P" * 32,
        "rsskey": "", "rss_url": "", "rss_interval": "", "sid": 0, "uid": 0,
        "channel": "auto", "enabled": True, "proxy": "", "cookie": "",
        "download_page": "", "x_api_key": "", "downhash": "",
    }
    base.update(over)
    return base


class ManifestTest(unittest.TestCase):
    def test_version_matches_manifest(self):
        manifest = json.loads((support.ROOT / "package.v2.json").read_text("utf-8"))
        entry = manifest["ColaCrossSeed"]
        self.assertEqual(entry["version"], mod.ColaCrossSeed.plugin_version)
        self.assertTrue(re.match(r"^\d+\.\d+\.\d+$", mod.ColaCrossSeed.plugin_version))
        self.assertIn(f"v{entry['version']}", entry["history"])
        self.assertEqual(list(entry["history"])[0], f"v{entry['version']}")
        self.assertTrue(entry.get("v2"))
        self.assertTrue(entry.get("plugin_readme", "").startswith("https://"))


class SiteLineTest(unittest.TestCase):
    def test_keyed_line(self):
        plugin = support.make_plugin(mod, sites_lines=(
            "观众,https://audiences.me,passkey=%s,sid=68,rsskey=%s,channel=rss,rss_interval=200\n"
            % ("a" * 32, "b" * 32)))
        sites = plugin._line_sites()
        self.assertEqual(len(sites), 1)
        s = sites[0]
        self.assertEqual(s["name"], "观众")
        self.assertEqual(s["url"], "https://audiences.me/")
        self.assertEqual(s["passkey"], "a" * 32)
        self.assertEqual(s["rsskey"], "b" * 32)
        self.assertEqual(s["sid"], 68)
        self.assertEqual(s["channel"], "rss")
        self.assertEqual(s["rss_interval"], "200")

    def test_positional_line(self):
        plugin = support.make_plugin(mod, sites_lines="demo.example,%s\n" % ("c" * 32))
        sites = plugin._line_sites()
        self.assertEqual(sites[0]["url"], "https://demo.example/")
        self.assertEqual(sites[0]["passkey"], "c" * 32)
        self.assertEqual(sites[0]["channel"], "auto")

    def test_line_without_passkey_is_skipped(self):
        plugin = support.make_plugin(mod, sites_lines="demo.example\n")
        self.assertEqual(plugin._line_sites(), [])


class DownloadUrlTest(unittest.TestCase):
    def test_placeholders(self):
        plugin = support.make_plugin(mod)
        site_ = site(download_page="download.php?id={}&passkey={passkey}&uid={uid}&hash={hash}",
                     uid=7, downhash="deadbeef")
        url = plugin._site_torrent_url(site_, "123")
        self.assertIn("id=123", url)
        self.assertIn("passkey=" + "P" * 32, url)
        self.assertIn("uid=7", url)
        self.assertIn("hash=deadbeef", url)

    def test_unknown_placeholder_falls_back(self):
        plugin = support.make_plugin(mod)
        site_ = site(download_page="download.php?id={}&downhash={downHash}")
        url = plugin._site_torrent_url(site_, "123")
        self.assertNotIn("{", url)
        self.assertIn("download.php?id=123", url)

    def test_rss_enclosure_wins(self):
        plugin = support.make_plugin(mod, store={"rss_cache": {"demo.example": {"at": 1, "items": {
            "h1": {"url": "https://demo.example/download.php?id=42&downhash=x", "title": "", "size": 0}
        }}}})
        self.assertIn("id=42", plugin._rss_url_for(site(), "42"))
        self.assertEqual(plugin._rss_url_for(site(), "77"), "")


class RssIntervalTest(unittest.TestCase):
    def test_floor_is_two_minutes(self):
        plugin = support.make_plugin(mod)
        self.assertEqual(plugin._rss_interval(site()), 120)
        self.assertEqual(plugin._rss_interval(site(url="https://audiences.me/")), 120)
        self.assertEqual(plugin._rss_interval(site(url="https://ptchdbits.co/")), 120)

    def test_override_only_goes_up(self):
        plugin = support.make_plugin(mod)
        self.assertEqual(plugin._rss_interval(site(rss_interval="600")), 600)
        self.assertEqual(plugin._rss_interval(site(rss_interval="30")), 120)
        self.assertEqual(plugin._rss_interval(site(rss_interval="abc")), 120)

    def test_recent_fetch_never_hits_network(self):
        cached = {"h1": {"url": "u", "title": "t", "size": 1}}
        plugin = support.make_plugin(mod, store={
            "rss_at": {"demo.example": time.time()},
            "rss_cache": {"demo.example": {"at": int(time.time()), "items": cached}},
        })
        original = mod._site_request

        def boom(*args, **kwargs):
            raise AssertionError("间隔没到点却发了请求")

        mod._site_request = boom
        try:
            items, note = plugin._rss_fetch(site())
        finally:
            mod._site_request = original
        self.assertEqual(items, cached)
        self.assertTrue(note.startswith("限流保护"), note)

    def test_fetch_marks_timestamp(self):
        plugin = support.make_plugin(mod)
        original = mod._site_request

        def fake(*args, **kwargs):
            return mod._HttpResult(200, RSS_OK.encode(), RSS_OK)

        mod._site_request = fake
        try:
            items, note = plugin._rss_fetch(site())
        finally:
            mod._site_request = original
        self.assertEqual(len(items), 2)
        self.assertEqual(note, "passkey")
        self.assertIn("demo.example", plugin._rss_at())

    def test_failure_keeps_previous_index(self):
        cached = {"old": {"url": "u", "title": "t", "size": 1}}
        store = {"rss_cache": {"demo.example": {"at": 1, "items": cached}}}
        plugin = support.make_plugin(mod, store=store)
        original = mod._site_request

        def fake(*args, **kwargs):
            return mod._HttpResult(200, b"<html>Prevent refresh and try again after 60s</html>",
                                   "Prevent refresh and try again after 60s")

        mod._site_request = fake
        try:
            items = plugin._rss_index(site())
        finally:
            mod._site_request = original
        self.assertEqual(items, cached)
        self.assertEqual(store["rss_cache"]["demo.example"]["at"], 1)
        self.assertIn("限流", store["rss_notes"]["demo.example"])


class RssParseTest(unittest.TestCase):
    def test_standard_xml(self):
        items = mod.ColaCrossSeed._parse_rss_items(RSS_OK.encode())
        self.assertEqual(len(items), 2)
        self.assertIn("a" * 40, items)
        self.assertEqual(items["a" * 40]["size"], 1024)

    def test_broken_cdata_falls_back_to_regex(self):
        items = mod.ColaCrossSeed._parse_rss_items(RSS_BROKEN_CDATA.encode())
        self.assertEqual(len(items), 2)
        self.assertIn("d" * 40, items)

    def test_non_hash_guid_ignored(self):
        xml = ("<rss><channel><item><title>x</title>"
               "<guid>https://demo.example/details.php?id=1</guid>"
               "<enclosure url='https://demo.example/download.php?id=1' length='1'/>"
               "</item></channel></rss>")
        self.assertEqual(mod.ColaCrossSeed._parse_rss_items(xml.encode()), {})


class TorrentHelperTest(unittest.TestCase):
    def test_info_hash(self):
        data = support.make_torrent()
        self.assertEqual(mod._torrent_info_hash(data), support.info_hash_of(data))

    def test_announces(self):
        data = support.make_torrent(
            announce="http://tracker.example/announce",
            announce_list=[["http://tracker.example/announce", "udp://tracker.example:80/announce"]])
        urls = mod._torrent_announces(data)
        self.assertEqual(urls, ["http://tracker.example/announce"])

    def test_garbage_returns_empty(self):
        self.assertEqual(mod._torrent_info_hash(b"<html>nope</html>"), "")
        self.assertEqual(mod._torrent_announces(b"<html>nope</html>"), [])


class SourceVariantTest(unittest.TestCase):
    """同一份文件在别站与可乐只有 info.source 不同（2026-11-02 凡人修仙传 实例）。"""

    def test_variant_equals_cola_hash(self):
        data = support.make_torrent(source="[kp.m-team.cc] M-Team - TP")
        variants = mod._torrent_info_hash_variants(data)
        expected = support.info_hash_of(
            support.make_torrent(source=mod.COLA_SOURCE_TAG))
        self.assertIn(expected, variants)
        self.assertNotEqual(expected, mod._torrent_info_hash(data))

    def test_variant_strips_source(self):
        data = support.make_torrent(source="[kp.m-team.cc] M-Team - TP")
        stripped = support.info_hash_of(support.make_torrent(source=""))
        self.assertIn(stripped, mod._torrent_info_hash_variants(data))

    def test_garbage_returns_nothing(self):
        self.assertEqual(mod._torrent_info_hash_variants(b"<html>"), [])


class ContentKeyTest(unittest.TestCase):
    """文件清单指纹：与站点 internal/contentkey 同一规范（否则两侧永远对不上）。"""

    def test_export_key_single_file(self):
        data = support.make_torrent(name="demo.mkv")
        expected = hashlib.sha1(b"demo.mkv\x001024\n").hexdigest()
        self.assertEqual(mod.ColaCrossSeed._export_content_key(data), expected)

    def test_client_key_strips_torrent_dir(self):
        entries = [{"name": "Demo.Pack/dir/a.mkv", "size": 10},
                   {"name": "Demo.Pack/b.nfo", "size": 2}]
        expected = hashlib.sha1(b"b.nfo\x002\ndir/a.mkv\x0010\n").hexdigest()
        self.assertEqual(mod.ColaCrossSeed._client_content_key("Demo.Pack", entries), expected)

    def test_empty_is_unknown(self):
        self.assertEqual(mod._content_key([]), "")
        self.assertEqual(mod.ColaCrossSeed._export_content_key(b"<html>"), "")


class DedupeMemoryTest(unittest.TestCase):
    """「记过」不能当成永久结论：客户端里没有的必须能补回来。"""

    def test_forget_removes_key(self):
        plugin = support.make_plugin(mod, store={"done": ["a", "b", "c"]})
        plugin._forget("b")
        self.assertEqual(plugin._done(), ["a", "c"])

    def test_dead_hashes_round_trip(self):
        plugin = support.make_plugin(mod)
        plugin._mark_dead(["AA" * 20, "bb" * 20, "aa" * 20])
        self.assertEqual(plugin._dead_hashes(), ["aa" * 20, "bb" * 20])

    def test_dead_hashes_capped(self):
        plugin = support.make_plugin(mod, store={"dead_hashes": ["%040x" % i for i in range(2000)]})
        plugin._mark_dead(["ff" * 20])
        self.assertEqual(len(plugin._dead_hashes()), 2000)
        self.assertEqual(plugin._dead_hashes()[-1], "ff" * 20)


class OpenApiTest(unittest.TestCase):
    """可乐PT 只走开放 API：X-API-Key，不带 Cookie / CSRF / Origin，不再登录。"""

    KEY = "rcapi_test_key"

    def _fake(self, answers):
        """answers: [(status, body)]，按顺序答；body 是 dict 就当 JSON，bytes 原样给。"""
        calls = []
        queue = list(answers)

        def fake(method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            status, body = queue.pop(0)
            if isinstance(body, (bytes, bytearray)):
                return mod._HttpResult(status, bytes(body), "")
            text = json.dumps(body, ensure_ascii=False)
            return mod._HttpResult(status, text.encode(), text)

        return fake, calls

    def _run(self, plugin, answers, func, *args):
        fake, calls = self._fake(answers)
        original, mod._curl = mod._curl, fake
        try:
            return func(*args), calls
        finally:
            mod._curl = original

    def assertKeyOnly(self, call):
        headers = call.get("headers") or {}
        self.assertEqual(headers.get("X-API-Key"), self.KEY)
        for banned in ("Cookie", "X-CSRF-Token", "Origin"):
            self.assertNotIn(banned, headers)

    def test_no_key_makes_no_request(self):
        plugin = support.make_plugin(mod, _api_key="")
        got, calls = self._run(plugin, [], plugin._match, ["a" * 40])
        self.assertEqual(got, ({}, ""))
        self.assertEqual(calls, [])

    def test_match_posts_hashes_in_batches_of_500(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        hashes = [f"{n:040x}" for n in range(501)] + ["NOT-A-HASH", "b" * 64]
        row = {"client_hash": "0" * 40, "id": 7, "info_hash": "c" * 40, "source_info_hash": "0" * 40,
               "size_bytes": "1024", "published_at": None, "via": "source_info_hash"}
        answers = [(200, {"checked": 500, "unmatched": 499, "matched": [row], "content": []}),
                   (200, {"checked": 1, "unmatched": 1, "matched": [], "content": []})]
        (by_hash, _), calls = self._run(plugin, answers, plugin._match, hashes)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["method"], "POST")
            self.assertTrue(call["url"].endswith("/api/v1/torrents/hashes"))
            self.assertKeyOnly(call)
        self.assertEqual(len(calls[0]["json_body"]["info_hashes"]), 500)
        self.assertEqual(len(calls[1]["json_body"]["info_hashes"]), 1)
        sent = calls[0]["json_body"]["info_hashes"] + calls[1]["json_body"]["info_hashes"]
        self.assertTrue(all(re.match(r"^[0-9a-f]{40}$", h) for h in sent), "格式不对的不能发，否则整批 400")
        self.assertEqual(by_hash["0" * 40][0]["id"], 7)

    def test_match_content_uses_content_keys(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        row = {"client_key": "d" * 40, "id": 9, "info_hash": "e" * 40, "size_bytes": "10",
               "release_name": "x", "published_at": None}
        got, calls = self._run(plugin, [(200, {"matched": [], "content": [row]})],
                               plugin._match_content, ["D" * 40])
        self.assertEqual(calls[0]["json_body"], {"content_keys": ["d" * 40]})
        self.assertEqual(got["d" * 40][0]["id"], 9)

    def test_download_signs_link_then_fetches_without_key(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        torrent = support.make_torrent()
        link = "https://www.colapt.com/downloads/tok/42?exp=1&sig=s"
        # 裸 python 下 bencode 是测试桩（键是 bytes），这里只验证「拿到的就是这份字节」
        original, mod._valid_torrent = mod._valid_torrent, (lambda data: data == torrent)
        try:
            got, calls = self._run(plugin, [(201, {"download_url": link}), (200, torrent)],
                                   plugin._download_cola, 42)
        finally:
            mod._valid_torrent = original
        self.assertEqual(got, torrent)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertTrue(calls[0]["url"].endswith("/api/v1/torrents/42/download-links"))
        self.assertKeyOnly(calls[0])
        self.assertEqual(calls[1]["url"], link)
        self.assertNotIn("X-API-Key", calls[1].get("headers") or {}, "签名直链本身就是凭据")

    def test_download_refused_returns_empty(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        got, calls = self._run(plugin, [(404, {"error": {"code": "not_found"}})], plugin._download_cola, 1)
        self.assertEqual(got, b"")
        self.assertEqual(len(calls), 1)

    def test_connection_test_shows_class_name(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        got, calls = self._run(plugin, [(200, {"class_name": "黄金可乐", "seeding": 3})], plugin._test_cola)
        self.assertIn("黄金可乐", got)
        self.assertTrue(got.startswith("✅"))
        self.assertTrue(calls[0]["url"].endswith("/api/v1/me/stats"))
        self.assertKeyOnly(calls[0])

    def test_scope_denied_alerts_once(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY, _key_alerted=False)
        sent = []
        plugin.post_message = lambda **kwargs: sent.append(kwargs)
        denied = {"error": {"code": "api_key_scope_denied", "message": "x"}}
        got, _ = self._run(plugin, [(403, denied)], plugin._test_cola)
        self.assertIn("读取", got)
        self._run(plugin, [(403, denied)], plugin._test_cola)
        self.assertEqual(len(sent), 1, "同一把 Key 的问题只提醒一次")

    def test_missing_key_message(self):
        plugin = support.make_plugin(mod, _api_key="")
        self.assertIn("API Key", plugin._test_cola())
        page = plugin.get_page()
        self.assertIn("account/tracker?tab=api", json.dumps(page, ensure_ascii=False))

    def test_form_has_key_field_and_no_login_fields(self):
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn('"model": "api_key"', text)
        for gone in ("username", "password", "captcha_code", "otp_code", "session_token"):
            self.assertNotIn(f'"model": "{gone}"', text)
            self.assertNotIn(gone, defaults)
        self.assertEqual(defaults["api_key"], "")

    def test_source_has_no_session_endpoints(self):
        source = support.PLUGIN_PATH.read_text("utf-8")
        for gone in ("/api/v1/plugin/match", "/api/v1/plugin/sessions", "/api/v1/plugin/captcha",
                     "/authorizations", "reelcore_session", "X-CSRF-Token"):
            self.assertNotIn(gone, source)


class ParallelCurlTest(unittest.TestCase):
    """并发拉 RSS 靠 curl 子进程，Python 侧不开线程。"""

    def test_parallel_results_in_order(self):
        cmds = [["sh", "-c", "printf 'a\\n200'"], ["sh", "-c", "printf 'b\\n500'"],
                ["sh", "-c", "printf 'c\\n404'"]]
        got = mod._curl_parallel(cmds, [""] * 3, workers=2)
        self.assertEqual([r.status_code for r in got], [200, 500, 404])
        self.assertEqual([r.text for r in got], ["a", "b", "c"])

    def test_bad_command_yields_zero(self):
        got = mod._curl_parallel([["/nonexistent-binary-xyz"]], [""], workers=1)
        self.assertEqual(got[0].status_code, 0)

    def test_rss_needs_fetch_respects_cache(self):
        plugin = support.make_plugin(mod, store={
            "rss_cache": {"pt.example": {"at": int(time.time()), "items": {"ab" * 20: {"url": "u"}}}},
        })
        site = {"name": "示例", "url": "https://pt.example", "passkey": "p" * 32}
        self.assertFalse(plugin._rss_needs_fetch(site), "缓存新鲜时不该再拉")
        plugin2 = support.make_plugin(mod, store={"rss_cache": {"pt.example": {"at": 0, "items": {}}}})
        self.assertTrue(plugin2._rss_needs_fetch(site))

    def test_rss_needs_fetch_respects_interval(self):
        plugin = support.make_plugin(mod, store={"rss_at": {"audiences.me": time.time()}})
        site = {"name": "观众", "url": "https://audiences.me", "rsskey": "r" * 32}
        self.assertFalse(plugin._rss_needs_fetch(site), "2 分钟保护期内不拉")

    def test_prefetch_uses_first_try_only(self):
        plugin = support.make_plugin(mod)
        site = {"name": "朋友", "url": "https://pt.example", "passkey": "p" * 32, "rsskey": "r" * 32}
        tries = plugin._rss_tries(site)
        self.assertEqual(tries[0][2], "rsskey", "有 rsskey 时优先用它")


class CategoryNotTagTest(unittest.TestCase):
    """issue #23：原种子的分类不能被塞进新种子的标签，且要按分类/自动管理落盘。"""

    def _place(self, **attrs):
        plugin = support.make_plugin(mod, **attrs)
        client = support.FakeClient(add_result="ok")
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        torrent = {"save_path": "/downloads/剧流", "category": "剧流", "tags": "NovaHD",
                   "auto_tmm": True}
        plugin._place(client, b"t", torrent, "k", stats, "filled")
        return client.added[0]

    def test_category_not_in_tags(self):
        added = self._place()
        self.assertNotIn("剧流", added["label"], f"分类被塞进标签了：{added['label']}")
        self.assertIn("NovaHD", added["label"], "原种子的标签仍应继承")

    def test_category_and_auto_tmm_passed(self):
        added = self._place()
        self.assertEqual(added["category"], "剧流")
        self.assertIs(added["auto_tmm"], True)

    def test_qb_payload_uses_category_field(self):
        client = mod.QBittorrentClient("qB", "http://127.0.0.1:8080")

        class _Rsp:
            status_code = 200
            text = "Ok."

        captured = {}

        class _Session:
            def post(self, url, files=None, data=None, timeout=None):
                captured.update(data or {})
                return _Rsp()

        client._session = _Session()
        self.assertEqual(client.add(b"t", "/downloads/剧流", True, "NovaHD,辅种",
                                    category="剧流", auto_tmm=True), "ok")
        self.assertEqual(captured.get("category"), "剧流")
        self.assertEqual(captured.get("autoTMM"), "true")
        self.assertEqual(captured.get("savepath"), "/downloads/剧流")
        self.assertNotIn("剧流", captured.get("tags", ""), "分类不该出现在 tags 里")

    def test_qb_payload_without_auto_tmm(self):
        client = mod.QBittorrentClient("qB", "http://127.0.0.1:8080")

        class _Rsp:
            status_code = 200
            text = "Ok."

        captured = {}

        class _Session:
            def post(self, url, files=None, data=None, timeout=None):
                captured.update(data or {})
                return _Rsp()

        client._session = _Session()
        client.add(b"t", "/downloads", False, "", category="", auto_tmm=False)
        self.assertEqual(captured.get("autoTMM"), "false")
        self.assertNotIn("category", captured)


class HttpResultJsonTest(unittest.TestCase):
    """站点回空 body / HTML 时不能把异常抛给 MoviePilot（用户报「执行失败 Expecting value」）。"""

    def test_empty_body_returns_default(self):
        self.assertIsNone(mod._HttpResult(200, b"", "").json())
        self.assertEqual(mod._HttpResult(200, b"", "  \n ").json(default={}), {})

    def test_html_body_returns_default(self):
        html = "<html><body>502 Bad Gateway</body></html>"
        self.assertIsNone(mod._HttpResult(200, html.encode(), html).json())
        self.assertEqual(mod._HttpResult(502, html.encode(), html).json(default={}), {})

    def test_valid_json_still_parsed(self):
        body = '{"matched": [{"client_hash": "ab"}]}'
        self.assertEqual(mod._HttpResult(200, body.encode(), body).json()["matched"][0]["client_hash"], "ab")

    def test_truncated_json_returns_default(self):
        self.assertEqual(mod._HttpResult(200, b'{"a":', '{"a":').json(default={}), {})


class TagMemoryTest(unittest.TestCase):
    """自愈删种子会把用户/MP 打的标签一起抹掉，重新补种时得原样加回去（issue #11）。"""

    def test_remembers_and_returns(self):
        plugin = support.make_plugin(mod)
        plugin._remember_tags({"AA" * 20: "可乐辅种, 已整理,订阅"})
        tags = plugin._take_tags("aa" * 20)
        self.assertIn("已整理", tags)
        self.assertIn("订阅", tags)
        self.assertNotIn(mod.MANDATORY_TAG, tags, "自己的标签不该被记忆")
        self.assertEqual(plugin._take_tags("aa" * 20), "", "取走即删")

    def test_merges_across_rounds(self):
        plugin = support.make_plugin(mod)
        plugin._remember_tags({"bb" * 20: "已整理"})
        plugin._remember_tags({"bb" * 20: "订阅,已整理"})
        tags = plugin._take_tags("bb" * 20)
        self.assertEqual(tags, "已整理,订阅")

    def test_only_own_tag_is_ignored(self):
        plugin = support.make_plugin(mod)
        plugin._remember_tags({"cc" * 20: mod.MANDATORY_TAG})
        self.assertEqual(plugin._take_tags("cc" * 20), "")

    def test_capped(self):
        plugin = support.make_plugin(mod, store={"tag_memory": {("%040x" % i): "t" for i in range(800)}})
        plugin._remember_tags({"ff" * 20: "新标签"})
        self.assertEqual(len(plugin._tag_memory()), plugin.TAG_MEMORY_LIMIT)
        self.assertEqual(plugin._take_tags("ff" * 20), "新标签")


class TagComposeTest(unittest.TestCase):
    """辅种时标签怎么拼：强制标签 + 原种子标签（可关）+ 额外标签（issue #11）。"""

    def _place_label(self, **attrs):
        plugin = support.make_plugin(mod, **attrs)
        client = support.FakeClient(add_result="ok")
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        torrent = {"save_path": "/d", "category": "", "tags": "可乐PT, 已整理,订阅"}
        plugin._place(client, b"t", torrent, "k", stats, "filled")
        return client.added[0]["label"] if client.added else ""

    def test_inherits_source_tags(self):
        label = self._place_label(_inherit_tags=True, _extra_tags="")
        self.assertIn("可乐PT", label)
        self.assertIn("已整理", label)
        self.assertIn("订阅", label)

    def test_can_disable_inherit(self):
        label = self._place_label(_inherit_tags=False, _extra_tags="")
        self.assertEqual(label, "")

    def test_extra_tags(self):
        label = self._place_label(_inherit_tags=False, _extra_tags="可乐PT, 辅种")
        self.assertEqual(label, "可乐PT,辅种")

    def test_merges_without_duplicates(self):
        label = self._place_label(_inherit_tags=True, _extra_tags="可乐PT,已整理")
        parts = label.split(",")
        self.assertEqual(len(parts), len(set(parts)), f"标签重复了：{label}")


class MPClientTest(unittest.TestCase):
    """MP 的下载器模块：跳过校验与标签必须真的传下去（用户报「跳过校验没生效、没标签」）。"""

    def _client(self, module, name="MP-测试"):
        return mod._MPClient(name, module.list, module.add)

    def test_passes_skip_checking_and_tag(self):
        module = support.FakeMPModule()
        client = self._client(module)
        result = client.add(b"torrent", "/downloads", True, "可乐辅种,可乐PT")
        self.assertEqual(result, "ok")
        call = module.calls[0]
        self.assertIs(call["skip_checking"], True)
        self.assertEqual(call["tag"], "可乐辅种,可乐PT")
        self.assertEqual(call["download_dir"], "/downloads")

    def test_skip_check_false_is_explicit(self):
        module = support.FakeMPModule()
        self._client(module).add(b"t", "/d", False)
        self.assertIs(module.calls[0]["skip_checking"], False)
        self.assertNotIn("tag", module.calls[0])

    def test_old_mp_without_kwargs_falls_back(self):
        module = support.FakeMPModule(strict=True)
        client = self._client(module)
        self.assertEqual(client.add(b"t", "/d", True, "x"), "ok")
        self.assertIn("rejected", module.calls[0], "严格模式应先拒掉新参数")
        self.assertEqual(module.calls[-1], {"content": b"t", "download_dir": "/d"})

    def test_duplicate_detected_via_scan(self):
        module = support.FakeMPModule(torrents=[{"hash": "AB" * 20, "name": "x"}], accept=False)
        client = self._client(module)
        self.assertEqual(client.add(b"t", "/d", True, hash_hint="ab" * 20), "dup")

    def test_list_includes_tags(self):
        module = support.FakeMPModule(torrents=[{"hash": "cd" * 20, "name": "x", "tags": "a,b"}])
        got = self._client(module).list_completed()
        self.assertEqual(got[0]["tags"], "a,b")


class PreferredClientTest(unittest.TestCase):
    """同一颗种子在 qB 和 TR 都有时，应该补到「正在做种」的那台。"""

    def _client(self, name, torrents):
        client = support.FakeClient()
        client.name = name
        client.list_completed = lambda: list(torrents)
        return client

    def test_prefers_seeding_client(self):
        plugin = support.make_plugin(mod)
        h = "ab" * 20
        qb = self._client("qB", [{"hash": h, "state": "pausedDL"}])
        tr = self._client("TR", [{"hash": h, "state": "uploading"}])
        preferred = plugin._preferred_clients([(qb, qb.list_completed()), (tr, tr.list_completed())])
        self.assertEqual(preferred[h], "TR")

    def test_falls_back_to_first_when_nobody_seeds(self):
        plugin = support.make_plugin(mod)
        h = "cd" * 20
        qb = self._client("qB", [{"hash": h, "state": "pausedDL"}])
        tr = self._client("TR", [{"hash": h, "state": "stopped"}])
        preferred = plugin._preferred_clients([(qb, qb.list_completed()), (tr, tr.list_completed())])
        self.assertEqual(preferred[h], "qB")

    def test_unique_hash_keeps_owner(self):
        plugin = support.make_plugin(mod)
        qb = self._client("qB", [{"hash": "11" * 20, "state": "pausedDL"}])
        tr = self._client("TR", [{"hash": "22" * 20, "state": "uploading"}])
        preferred = plugin._preferred_clients([(qb, qb.list_completed()), (tr, tr.list_completed())])
        self.assertEqual(preferred["11" * 20], "qB")
        self.assertEqual(preferred["22" * 20], "TR")

    def test_seeding_state_recognised(self):
        for state in ("uploading", "stalledUP", "forcedUP", "seeding"):
            self.assertTrue(mod.ColaCrossSeed._is_seeding(state), state)
        for state in ("pausedDL", "downloading", "stopped", ""):
            self.assertFalse(mod.ColaCrossSeed._is_seeding(state), state)


class TargetClientTest(unittest.TestCase):
    """手动指定目标下载器：只往这一台加，且名字匹配要宽松。

    2026-11-19 现场事故：MP 枚举出来的实例叫 `MP-tr辅种`，用户填 `tr` / `tr辅种`
    都匹配不上（以前 `_preferred_clients` 里是 `wanted in names` 精确比），
    静默退回自动判断 —— 用户看到的就是「指定下载器这个功能不生效」。
    """

    def _clients(self):
        qb = support.FakeClient(); qb.name = "MP-qb下载"
        tr = support.FakeClient(); tr.name = "MP-tr辅种"
        return [qb, tr]

    def test_exact_name(self):
        plugin = support.make_plugin(mod, _target_client="MP-tr辅种")
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_mp_prefix_is_optional(self):
        for wanted in ("tr辅种", "MP-tr辅种", "TR辅种"):
            plugin = support.make_plugin(mod, _target_client=wanted)
            got = plugin._apply_target_client(self._clients())
            self.assertEqual([c.name for c in got], ["MP-tr辅种"], wanted)

    def test_short_prefix_contains(self):
        """用户常只写 `tr`，实例叫 `tr辅种`：包含匹配。"""
        plugin = support.make_plugin(mod, _target_client="tr")
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_unknown_target_adds_nothing(self):
        """填错名字时**一轮都不加**，绝不回落把种子推到没点名的机器上。"""
        plugin = support.make_plugin(mod, _target_client="不存在的机器")
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertFalse(plugin._target_state.get("ok"))
        self.assertEqual(plugin._target_state.get("available"),
                         ["MP-qb下载", "MP-tr辅种"])

    def test_blank_target_keeps_all(self):
        plugin = support.make_plugin(mod, _target_client="")
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-qb下载", "MP-tr辅种"])

    def test_preferred_inside_target_only(self):
        """收窄之后自动判断只在目标那台里挑，不会把别的下载器写在 owner 里。"""
        plugin = support.make_plugin(mod, _target_client="tr辅种")
        clients = plugin._apply_target_client(self._clients())
        h = "ab" * 20
        scanned = [(clients[0], [{"hash": h, "state": "uploading"}])]
        self.assertEqual(plugin._preferred_clients(scanned)[h], "MP-tr辅种")


class TargetAmbiguityTest(unittest.TestCase):
    """名字有歧义时不能瞎挑：宁可这一轮不加，也不能推到没点名的机器上。"""

    def _clients(self):
        a = support.FakeClient(); a.name = "MP-tr辅种"
        b = support.FakeClient(); b.name = "MP-tr备用"
        return [a, b]

    def test_multiple_matches_stop(self):
        plugin = support.make_plugin(mod, _target_client="tr")
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertFalse(plugin._target_state.get("ok"))
        self.assertEqual(sorted(plugin._target_state.get("ambiguous") or []),
                         ["MP-tr备用", "MP-tr辅种"])

    def test_full_name_resolves_even_with_sibling(self):
        plugin = support.make_plugin(mod, _target_client="MP-tr备用")
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr备用"])

    def test_single_char_never_fuzzy(self):
        plugin = support.make_plugin(mod, _target_client="t")
        self.assertEqual(plugin._apply_target_client(self._clients()), [])

    def test_quoted_and_spaced_input(self):
        plugin = support.make_plugin(mod, _target_client='  "MP-tr辅种"  ')
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_pending_for_other_client_is_kept(self):
        """收窄客户端后，别的机器的待重试记录要留着，不能静默丢。"""
        plugin = support.make_plugin(mod, _target_client="MP-tr备用")
        plugin._pending = lambda: [{"client": "MP-qb下载", "key": "k1", "data": ""}]
        plugin._save_pending = lambda x: setattr(plugin, "_saved", x)
        meta = {"added": 0, "filled": 0, "duplicate": 0, "retried": 0, "failed": 0, "skipped": 0}
        plugin._retry_pending({"MP-tr备用": self._clients()[0]}, set(), meta)
        self.assertEqual([x["key"] for x in getattr(plugin, "_saved", [])], ["k1"])


class TargetDuplicateNameTest(unittest.TestCase):
    """两台同名（或只差大小写）时，写全名也分不清，必须判歧义而不是挑一台。"""

    def _clients(self):
        a = support.FakeClient(); a.name = "MP-tr辅种"
        b = support.FakeClient(); b.name = "mp-TR辅种"
        return [a, b]

    def test_same_name_is_ambiguous(self):
        plugin = support.make_plugin(mod, _target_client="MP-tr辅种")
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertFalse(plugin._target_state.get("ok"))

    def test_alias_stage_ambiguity(self):
        plugin = support.make_plugin(mod, _target_client="tr辅种")
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertTrue(plugin._target_state.get("ambiguous"))


class PlaceResultTest(unittest.TestCase):
    """失败不能记 done：记了下一轮就永远跳过这颗（复审 M2）。"""

    def test_fail_returns_false(self):
        plugin = support.make_plugin(mod)
        client = support.FakeClient()
        client.add = lambda *a, **k: "fail"
        torrent = {"hash": "ab" * 20, "save_path": "/d"}
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0,
                 "failed": 0, "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        self.assertFalse(plugin._place(client, b"data", torrent, "k", stats, "added"))

    def test_duplicate_returns_true(self):
        plugin = support.make_plugin(mod)
        client = support.FakeClient()
        client.add = lambda *a, **k: "dup"
        torrent = {"hash": "cd" * 20, "save_path": "/d"}
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0,
                 "failed": 0, "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        self.assertTrue(plugin._place(client, b"data", torrent, "k", stats, "added"))


class CrossSeedClientLinesTest(unittest.TestCase):
    """辅种插件的下载器配置行：4 段写法按内容识别地址（保种组 0.2.8/0.2.9 同款修复）。"""

    def setUp(self):
        self.p = support.make_plugin(mod)

    def test_name_after_password(self):
        got = self.p._parse_client_lines("192.168.31.100:8081,a172604,qq321321,NAS-本地")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "NAS-本地")
        self.assertEqual(got[0].url, "http://192.168.31.100:8081")

    def test_name_before_address(self):
        got = self.p._parse_client_lines("198-远端,198.72.127.68:8081,qb1,qq321321")
        self.assertEqual(got[0].name, "198-远端")
        self.assertEqual(got[0].url, "http://198.72.127.68:8081")

    def test_tr_kind_prefix(self):
        got = self.p._parse_client_lines("tr,198.72.127.68:9091,admin,pwd,198-TR")
        self.assertEqual(got[0].name, "198-TR")
        self.assertEqual(got[0].url, "http://198.72.127.68:9091/transmission/rpc")
        self.assertIn("Transmission", type(got[0]).__name__)

    def test_username_not_address(self):
        for token in ("a172604", "qq321321", "qb1"):
            self.assertFalse(self.p._looks_like_address(token), token)
        for token in ("192.168.31.100:8081", "127.0.0.1", "localhost"):
            self.assertTrue(self.p._looks_like_address(token), token)


class PlaceTest(unittest.TestCase):
    def test_duplicate_short_circuits(self):
        plugin = support.make_plugin(mod)
        client = support.FakeClient(has_hash={"h" * 40})
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        plugin._place(client, b"torrent", {"save_path": "/downloads"}, "key", stats,
                      "filled", hash_hint="h" * 40)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(client.added, [])
        self.assertIn("key", plugin._done())

    def test_success_counts_and_remembers(self):
        plugin = support.make_plugin(mod)
        client = support.FakeClient(add_result="ok")
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        plugin._place(client, b"torrent", {"save_path": "/downloads"}, "key", stats,
                      "filled", site_key="站点A")
        self.assertEqual(stats["filled"], 1)
        self.assertEqual(len(client.added), 1)
        self.assertEqual(len(plugin._add_times()), 1)
        self.assertEqual(plugin._site_counter["站点A"], 1)

    def test_site_budget_blocks(self):
        plugin = support.make_plugin(mod)
        plugin._max_per_site = 1
        plugin._site_counter = {"站点A": 1}
        client = support.FakeClient()
        stats = {"matched": 0, "added": 0, "filled": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0, "trackers": 0}
        plugin._place(client, b"torrent", {"save_path": "/d"}, "key", stats,
                      "filled", site_key="站点A")
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(client.added, [])


class SiteReportTest(unittest.TestCase):
    def test_pieces_ok(self):
        plugin = support.make_plugin(mod, iyuu_token="tok")
        plugin._probe_pieces = lambda s: (True, "pieces-hash 可用（/api/pieces-hash）")
        plugin._probe_rss = lambda s: (0, "不该被调用")
        name, level, msg = plugin._test_one_site(site(channel="pieces", sid=81))
        self.assertEqual((name, level), ("演示", "ok"))
        self.assertIn("pieces-hash", msg)

    def test_rss_counts_reported(self):
        plugin = support.make_plugin(mod, iyuu_token="tok")
        plugin._probe_pieces = lambda s: (False, "没有 pieces-hash 接口")
        plugin._probe_rss = lambda s: (10, "RSS 可匹配 10 条（passkey）")
        _, level, msg = plugin._test_one_site(site(channel="rss", sid=68))
        self.assertEqual(level, "ok")
        self.assertIn("RSS 可匹配 10 条", msg)

    def test_iyuu_channel_is_ok(self):
        # channel=iyuu 是用户显式声明的通道（家园/听听歌这类），IYUU 能用就算可用
        plugin = support.make_plugin(mod, iyuu_token="tok")
        plugin._probe_pieces = lambda s: (False, "没有 pieces-hash 接口")
        plugin._probe_rss = lambda s: (0, "RSS 链接已过期")
        _, level, msg = plugin._test_one_site(site(channel="iyuu", sid=7))
        self.assertEqual(level, "ok")
        self.assertIn("sid=7", msg)

    def test_iyuu_only_fallback_is_warn(self):
        # channel 没指定 iyuu、pieces/RSS 都挂了，只剩 sid 通道 → 标黄而不是绿
        plugin = support.make_plugin(mod, iyuu_token="tok")
        plugin._probe_pieces = lambda s: (False, "没有 pieces-hash 接口")
        plugin._probe_rss = lambda s: (0, "RSS 链接已过期")
        plugin._probe_rss = lambda s: (0, "RSS 链接已过期")
        plugin._other_sites = lambda: []
        _, level, msg = plugin._test_one_site(site(channel="auto", sid=7))
        self.assertEqual(level, "warn")
        self.assertIn("sid=7", msg)

    def test_nothing_available_is_fail(self):
        plugin = support.make_plugin(mod)
        plugin._probe_pieces = lambda s: (False, "没有 pieces-hash 接口")
        plugin._probe_rss = lambda s: (0, "RSS 返回 HTTP 404")
        _, level, msg = plugin._test_one_site(site(channel="rss", sid=0))
        self.assertEqual(level, "fail")
        self.assertIn("404", msg)

    def test_summary_line(self):
        plugin = support.make_plugin(mod)
        plugin._other_sites = lambda: [site(), site(), site()]
        answers = iter([("A", "ok", "x"), ("B", "warn", "y"), ("C", "fail", "z")])
        plugin._test_one_site = lambda s: next(answers)
        lines = plugin._test_sites()
        self.assertIn("可匹配 1 个", lines[0])
        self.assertIn("仅 IYUU 匹配 1 个", lines[0])
        self.assertIn("不可用 1 个", lines[0])
        self.assertTrue(lines[1].startswith("✅"))


class IyuuRobustnessTest(unittest.TestCase):
    GOOD = json.dumps([{"site": "keepfrds", "nickname": "朋友", "base_url": "pt.keepfrds.com",
                        "sid": 1, "disabled": 0, "options": {"passkey": "a" * 32}}], ensure_ascii=False)

    def _plugin(self):
        plugin = support.make_plugin(mod)
        plugin.update_config = lambda cfg: None
        plugin._iyuu_json = ""
        plugin._sites_lines = ""
        plugin._sites_mode = "merge"
        return plugin

    def test_dirty_sid_does_not_crash(self):
        plugin = self._plugin()
        plugin._iyuu_json = json.dumps([{"site": "x", "base_url": "x.example", "sid": "abc",
                                         "disabled": 0, "options": {"passkey": "b" * 32}}])
        sites = plugin._iyuu_sites()
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]["sid"], 0)

    def test_disabled_string_zero_is_enabled(self):
        plugin = self._plugin()
        plugin._iyuu_json = json.dumps([{"site": "x", "base_url": "x.example", "disabled": "0",
                                         "options": {"passkey": "c" * 32}}])
        self.assertEqual(len(plugin._iyuu_sites()), 1)

    def test_empty_import_keeps_previous_config(self):
        plugin = self._plugin()
        plugin._iyuu_json = self.GOOD
        out = plugin._iyuu_import(json.dumps([{"site": "y", "base_url": "y.example",
                                               "disabled": 0}]))   # 没有 passkey
        self.assertFalse(out["ok"])
        self.assertEqual(plugin._iyuu_json, self.GOOD)

    def test_top_level_credentials_also_read(self):
        plugin = self._plugin()
        plugin._iyuu_json = json.dumps([{"site": "x", "base_url": "x.example", "disabled": 0,
                                         "passkey": "d" * 32, "x_api_key": "k"}])
        sites = plugin._iyuu_sites()
        self.assertEqual(sites[0]["passkey"], "d" * 32)
        self.assertEqual(sites[0]["x_api_key"], "k")


class IyuuFileImportTest(unittest.TestCase):
    SAMPLE = json.dumps([{"site": "keepfrds", "nickname": "朋友",
                          "base_url": "pt.keepfrds.com", "sid": 1, "disabled": 0,
                          "options": {"passkey": "a" * 32}}], ensure_ascii=False)

    def _plugin(self):
        plugin = support.make_plugin(mod)
        plugin.update_config = lambda cfg: None
        plugin._iyuu_json = ""
        plugin._iyuu_json_file = ""
        plugin._sites_lines = ""
        plugin._sites_mode = "merge"
        return plugin

    def test_import_from_file(self):
        import tempfile
        plugin = self._plugin()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            handle.write(self.SAMPLE)
            path = handle.name
        try:
            out = plugin._iyuu_import_from_file(path)
        finally:
            import os
            os.unlink(path)
        self.assertTrue(out["ok"])
        self.assertEqual(out["usable"], 1)
        self.assertEqual(out["source"], "file")

    def test_missing_file_reports_reason(self):
        plugin = self._plugin()
        out = plugin._iyuu_import_from_file("/config/definitely-not-there.json")
        self.assertFalse(out["ok"])
        self.assertIn("文件不存在", out["reason"])

    def test_empty_path_reports_reason(self):
        plugin = self._plugin()
        out = plugin._iyuu_import_from_file("")
        self.assertFalse(out["ok"])
        self.assertIn("路径", out["reason"])


class IyuuExtraFieldsTest(unittest.TestCase):
    """IYUU 站点里那些「导入后就能直接跑」的参数。"""

    SAMPLE = json.dumps([{
        "site": "m-team", "nickname": "馒头", "base_url": "api.m-team.cc",
        "sid": 3, "disabled": 0, "download_page": "api/torrent/genDlToken",
        "options": {
            "passkey": "a" * 32, "x_api_key": "k",
            "limit": {"count": "35", "sleep": "5"},
            "url_join": {"https": "1", "ipv4": "1", "ipv6": "1"},
            "rss_url": "https://rss.m-team.cc/api/rss/fetch?dl=1&uid=1",
            "curl_opt_proxy": "192.168.1.2:8080",
            "curl_opt_proxy_auth": "user:pass",
        },
    }], ensure_ascii=False)

    def _plugin(self):
        plugin = support.make_plugin(mod)
        plugin.update_config = lambda cfg: None
        plugin._iyuu_json = self.SAMPLE
        plugin._sites_lines = ""
        plugin._sites_mode = "iyuu_only"
        plugin._site_limits = {}
        return plugin

    def test_rss_url_and_limit_are_imported(self):
        site = self._plugin()._iyuu_sites()[0]
        self.assertEqual(site["max_per_site"], 35)
        self.assertIn("rss.m-team.cc", site["rss_url"])

    def test_interval_imported(self):
        site = self._plugin()._iyuu_sites()[0]
        self.assertEqual(site["interval"], 5.0)

    def test_proxy_is_assembled(self):
        site = self._plugin()._iyuu_sites()[0]
        self.assertEqual(site["proxy"], "http://user:pass@192.168.1.2:8080")

    def test_proxy_helper_variants(self):
        self.assertEqual(mod._site_proxy("", ""), "")
        self.assertEqual(mod._site_proxy("1.2.3.4:8080", ""), "http://1.2.3.4:8080")
        self.assertEqual(mod._site_proxy("1.2.3.4:8080", "u:p"), "http://u:p@1.2.3.4:8080")
        self.assertEqual(mod._site_proxy("socks5h://1.2.3.4:1080", "u:p"),
                         "socks5h://u:p@1.2.3.4:1080")

    def test_per_site_limit_caps_budget(self):
        plugin = self._plugin()
        plugin._max_per_site = 20
        sites = plugin._other_sites()
        self.assertEqual(len(sites), 1)
        url = sites[0]["url"]
        self.assertEqual(plugin._site_limits.get(url), 35)
        self.assertEqual(plugin._site_budget_left(url), 35)
        plugin._site_counter[url] = 30
        self.assertEqual(plugin._site_budget_left(url), 5)

    def test_budget_falls_back_to_global(self):
        plugin = self._plugin()
        plugin._max_per_site = 12
        plugin._site_limits = {}
        self.assertEqual(plugin._site_budget_left("https://x.example/"), 12)


class SpeedLimitTest(unittest.TestCase):
    """限速接管：填了才强行改，不填一点不碰。"""

    def _plugin(self, dl=0, up=0):
        plugin = support.make_plugin(mod)
        plugin._limit_dl_kib = dl
        plugin._limit_up_kib = up
        return plugin

    def test_unset_does_nothing(self):
        plugin = self._plugin()
        client = support.FakeClient()
        out = plugin._apply_speed_limits([client])
        self.assertTrue(out["skipped_all"])
        self.assertIsNone(client.speed_limits)

    def test_apply_sets_both(self):
        plugin = self._plugin(dl=1024, up=2048)
        client = support.FakeClient()
        out = plugin._apply_speed_limits([client])
        self.assertEqual(client.speed_limits, (1024, 2048))
        self.assertEqual(out["applied"], 1)
        self.assertEqual(out["failed"], 0)

    def test_only_one_direction(self):
        plugin = self._plugin(dl=500, up=0)
        client = support.FakeClient()
        plugin._apply_speed_limits([client])
        self.assertEqual(client.speed_limits, (500, 0))

    def test_failure_counted(self):
        plugin = self._plugin(dl=100, up=100)

        class Broken(support.FakeClient):
            name = "broken"

            def set_speed_limits(self, dl_kib, up_kib):
                raise RuntimeError("qb 连不上")

        out = plugin._apply_speed_limits([Broken()])
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["applied"], 0)

    def test_per_client_override(self):
        plugin = self._plugin()
        plugin._limit_lines = "198.72.127.68:8081,1024,2048"
        a = support.FakeClient()
        a.name = "198.72.127.68:8081"
        b = support.FakeClient()
        b.name = "192.168.31.100:8080"
        out = plugin._apply_speed_limits([a, b])
        self.assertEqual(a.speed_limits, (1024, 2048))
        self.assertIsNone(b.speed_limits)     # 没配、全局也是 0 → 不接管
        self.assertEqual(out["applied"], 1)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(out["clients"][0]["source"], "198.72.127.68:8081")

    def test_override_beats_global(self):
        plugin = self._plugin(dl=100, up=100)
        plugin._limit_lines = "mp-qb,3000,4000"
        a = support.FakeClient()
        a.name = "MP-qb"
        b = support.FakeClient()
        b.name = "other"
        plugin._apply_speed_limits([a, b])
        self.assertEqual(a.speed_limits, (3000, 4000))
        self.assertEqual(b.speed_limits, (100, 100))   # 未命中用全局

    def test_override_partial_match(self):
        plugin = self._plugin()
        plugin._limit_lines = "198.72,500,0"
        a = support.FakeClient()
        a.name = "198.72.127.68:8081"
        plugin._apply_speed_limits([a])
        self.assertEqual(a.speed_limits, (500, 0))

    def test_limit_lines_parsing(self):
        plugin = self._plugin()
        plugin._limit_lines = "# 注释\n198.72.127.68:8081,1024,2048\nMP-qb，3000，4000\n坏行\n"
        mapping = plugin._speed_limit_map()
        self.assertEqual(mapping["198.72.127.68:8081"], (1024, 2048))
        self.assertEqual(mapping["mp-qb"], (3000, 4000))
        self.assertEqual(len(mapping), 2)

    def test_config_parsing_clamps(self):
        plugin = support.make_plugin(mod)
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        plugin.init_plugin({"limit_dl_kib": "1500", "limit_up_kib": -5})
        self.assertEqual(plugin._limit_dl_kib, 1500)
        self.assertEqual(plugin._limit_up_kib, 0)


class IyuuImportTest(unittest.TestCase):
    SAMPLE = json.dumps([
        {"id": 1, "sid": 1, "site": "keepfrds", "nickname": "朋友",
         "base_url": "pt.keepfrds.com", "mirror": "", "cookie": "", "cookie_required": 0,
         "download_page": "download.php?id={}&passkey={passkey}", "is_https": 2, "disabled": 0,
         "options": {"passkey": "a" * 32, "uid": "133256"}},
        {"id": 2, "sid": 2, "site": "hdhome", "nickname": "家园",
         "base_url": "hdhome.org", "mirror": "", "cookie": "c=1", "cookie_required": 0,
         "download_page": "download.php?id={}&uid={uid}&hash={hash}", "is_https": 1, "disabled": 0,
         "options": {"passkey": "b" * 32, "uid": "1", "downhash": "abc"}},
        {"id": 3, "sid": 3, "site": "off", "nickname": "关掉的",
         "base_url": "off.example", "disabled": 1, "options": {"passkey": "c" * 32}},
    ], ensure_ascii=False)

    def _plugin(self):
        plugin = support.make_plugin(mod)
        plugin.update_config = lambda cfg: None
        plugin._sites_lines = ""
        plugin._sites_mode = "merge"
        plugin._iyuu_json = ""
        return plugin

    def test_import_carries_uid_downhash_cookie(self):
        plugin = self._plugin()
        out = plugin._iyuu_import(self.SAMPLE)
        self.assertTrue(out["ok"])
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["enabled"], 2)
        self.assertEqual(out["usable"], 2)
        by_host = {s["url"].split("://", 1)[-1].split("/")[0]: s for s in plugin._iyuu_sites()}
        self.assertEqual(by_host["pt.keepfrds.com"]["uid"], 133256)
        self.assertEqual(by_host["hdhome.org"]["downhash"], "abc")
        self.assertEqual(by_host["hdhome.org"]["cookie"], "c=1")

    def test_import_switches_manual_to_merge(self):
        # 原来只用行文本的用户：导入后变成「IYUU + 行文本」，手写行不会被丢掉
        plugin = self._plugin()
        plugin._sites_mode = "manual"
        plugin._iyuu_import(self.SAMPLE)
        self.assertEqual(plugin._sites_mode, "merge")

    def test_iyuu_only_ignores_manual_lines(self):
        plugin = self._plugin()
        plugin._sites_mode = "iyuu_only"
        plugin._sites_lines = "老站,https://old.example.com,passkey=" + "d" * 32
        plugin._iyuu_json = self.SAMPLE
        hosts = [s["url"] for s in plugin._other_sites()]
        self.assertEqual(len(hosts), 2)
        self.assertTrue(all("old.example.com" not in h for h in hosts))

    def test_import_rejects_bad_json(self):
        plugin = self._plugin()
        out = plugin._iyuu_import("{not json")
        self.assertFalse(out["ok"])
        self.assertIn("解析失败", out["reason"])

    def test_import_without_payload_uses_stored(self):
        plugin = self._plugin()
        plugin._iyuu_json = self.SAMPLE
        out = plugin._iyuu_import("")
        self.assertTrue(out["ok"])
        self.assertEqual(out["usable"], 2)


