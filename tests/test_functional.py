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


class CaptchaTest(unittest.TestCase):
    """验证码要缓存：每刷新一次就换一张，用户看到的那张立刻作废（issue #8）。"""

    def _fake_curl(self, payload, status=200):
        import json as _json
        captured = []

        def fake(method, url, **kwargs):
            captured.append(url)
            text = _json.dumps(payload, ensure_ascii=False)
            return mod._HttpResult(status, text.encode(), text)

        return fake, captured

    def test_cached_between_calls(self):
        plugin = support.make_plugin(mod)
        fake, calls = self._fake_curl({"captcha_id": "cid1", "image": "data:image/png;base64,AA"})
        original, mod._curl = mod._curl, fake
        try:
            first = plugin._fetch_captcha()
            second = plugin._fetch_captcha()
        finally:
            mod._curl = original
        self.assertEqual(first, ("cid1", "data:image/png;base64,AA"))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "第二次应命中缓存，不该再请求站点")

    def test_force_bypasses_cache(self):
        plugin = support.make_plugin(mod)
        fake, calls = self._fake_curl({"captcha_id": "cid2", "image": "data:image/png;base64,BB"})
        original, mod._curl = mod._curl, fake
        try:
            plugin._fetch_captcha()
            plugin._fetch_captcha(force=True)
        finally:
            mod._curl = original
        self.assertEqual(len(calls), 2)

    def test_failure_records_reason(self):
        plugin = support.make_plugin(mod)

        def boom(*args, **kwargs):
            raise RuntimeError("tls handshake failed")

        original, mod._curl = mod._curl, boom
        try:
            cid, image = plugin._fetch_captcha()
        finally:
            mod._curl = original
        self.assertEqual((cid, image), ("", ""))
        self.assertIn("tls handshake failed", plugin.get_data("captcha_error"))

    def test_http_error_records_status(self):
        plugin = support.make_plugin(mod)
        fake, _ = self._fake_curl({}, status=502)
        original, mod._curl = mod._curl, fake
        try:
            self.assertEqual(plugin._fetch_captcha(), ("", ""))
        finally:
            mod._curl = original
        self.assertIn("502", plugin.get_data("captcha_error"))


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
    """手动指定目标下载器时，应覆盖「谁在做种」的自动判断。"""

    def test_override_to_tr(self):
        plugin = support.make_plugin(mod, _target_client="TR")
        h = "ab" * 20
        qb = support.FakeClient(); qb.name = "qB"
        tr = support.FakeClient(); tr.name = "TR"
        scanned = [(qb, [{"hash": h, "state": "uploading"}]),
                   (tr, [{"hash": h, "state": "pausedDL"}])]
        self.assertEqual(plugin._preferred_clients(scanned)[h], "TR")

    def test_override_ignored_when_client_lacks_hash(self):
        plugin = support.make_plugin(mod, _target_client="TR")
        h = "cd" * 20
        qb = support.FakeClient(); qb.name = "qB"
        tr = support.FakeClient(); tr.name = "TR"
        scanned = [(qb, [{"hash": h, "state": "uploading"}]),
                   (tr, [{"hash": "ee" * 20, "state": "uploading"}])]
        self.assertEqual(plugin._preferred_clients(scanned)[h], "qB")


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


class SeedGroupTierTest(unittest.TestCase):
    def test_tiers_are_gib_floor(self):
        self.assertEqual(mod.seed_group_target_gib("2T"), 2048)
        self.assertEqual(mod.seed_group_target_gib("5T"), 5120)
        self.assertEqual(mod.seed_group_target_gib("10T"), 10240)
        self.assertEqual(mod.seed_group_target_gib("20T+"), 20480)

    def test_unknown_tier_falls_back_to_smallest(self):
        # 写错档位不能退化成「不保种」：一律按 2T 下限走
        self.assertEqual(mod.seed_group_target_gib("100T"), 2048)
        self.assertEqual(mod.seed_group_target_gib(""), 2048)
        self.assertEqual(mod.seed_group_target_gib(None), 2048)

    def test_size_bytes_and_gib(self):
        self.assertAlmostEqual(mod._seed_group_size_gib(2 * mod.GIB), 2.0)
        self.assertAlmostEqual(mod._seed_group_size_gib(3.5), 3.5)      # 已经是 GiB
        self.assertEqual(mod._seed_group_size_gib(0), 0.0)
        self.assertEqual(mod._seed_group_size_gib("bad"), 0.0)


class SeedGroupPlanTest(unittest.TestCase):
    def test_never_below_target(self):
        items = [{"info_hash": f"h{i}", "size": i * mod.GIB} for i in (60, 50, 40, 30)]
        picked, total, shortfall = mod.seed_group_plan(items, 100)
        self.assertGreaterEqual(total, 100)
        self.assertEqual(shortfall, 0)
        self.assertEqual(sum(x["size_gib"] for x in picked), total)

    def test_best_fit_fills_exactly(self):
        items = [{"info_hash": "a", "size": 60 * mod.GIB},
                 {"info_hash": "b", "size": 50 * mod.GIB},
                 {"info_hash": "c", "size": 40 * mod.GIB}]
        picked, total, shortfall = mod.seed_group_plan(items, 100)
        self.assertEqual(total, 100)
        self.assertEqual([x["info_hash"] for x in picked], ["a", "c"])
        self.assertEqual(shortfall, 0)

    def test_overflow_single_too_big(self):
        # 一颗就超过档位：照样收下（只超不少）
        picked, total, shortfall = mod.seed_group_plan(
            [{"info_hash": "big", "size": 300 * mod.GIB}], 100)
        self.assertEqual(len(picked), 1)
        self.assertEqual(total, 300)
        self.assertEqual(shortfall, 0)

    def test_shortfall_reported_when_pool_too_small(self):
        items = [{"info_hash": "a", "size": 40 * mod.GIB},
                 {"info_hash": "b", "size": 60 * mod.GIB}]
        picked, total, shortfall = mod.seed_group_plan(items, 1000)
        self.assertEqual(len(picked), 2)
        self.assertEqual(total, 100)
        self.assertEqual(shortfall, 900)

    def test_official_first(self):
        items = [{"info_hash": "plain", "size": 60 * mod.GIB, "official": False},
                 {"info_hash": "official", "size": 50 * mod.GIB, "official": True}]
        picked, _, _ = mod.seed_group_plan(items, 50)
        self.assertEqual(picked[0]["info_hash"], "official")

    def test_empty_pool(self):
        picked, total, shortfall = mod.seed_group_plan([], 2048)
        self.assertEqual((picked, total, shortfall), ([], 0.0, 2048.0))


class SeedGroupPluginTest(unittest.TestCase):
    def _plugin(self, **attrs):
        plugin = support.make_plugin(mod, **attrs)
        plugin._session_token = "token"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        return plugin

    @staticmethod
    def _fake_cola(me=None, profile=None):
        def call(method, path, **kwargs):
            if path == "/api/v1/me":
                body = json.dumps(me or {"id": 0}).encode()
                return mod._HttpResult(200, body, body.decode())
            if path.startswith("/api/v1/users/"):
                body = json.dumps(profile or {}).encode()
                return mod._HttpResult(200, body, body.decode())
            return mod._HttpResult(404, b"", "404")
        return call

    def test_identity_requires_login(self):
        plugin = self._plugin()
        plugin._session_token = ""
        ident = plugin.seed_group_identity()
        self.assertFalse(ident["ok"])
        self.assertIn("登录", ident["reason"])

    def test_identity_staff_detected(self):
        plugin = self._plugin()
        plugin._cola = self._fake_cola(
            me={"id": 7, "username": "yg", "roles": ["member"]},
            profile={"groups": ["VIP", "工作组-保种"], "display_group": "工作组-保种"})
        ident = plugin.seed_group_identity()
        self.assertTrue(ident["ok"])
        self.assertTrue(ident["staff"])
        self.assertIn("工作组-保种", ident["groups"])

    def test_identity_non_staff_rejected(self):
        plugin = self._plugin()
        plugin._cola = self._fake_cola(
            me={"id": 8, "username": "member", "roles": ["member"]},
            profile={"groups": ["VIP"], "display_group": "VIP"})
        ident = plugin.seed_group_identity()
        self.assertTrue(ident["ok"])
        self.assertFalse(ident["staff"])
        self.assertIn("不在工作组", ident["reason"])

    def test_identity_admin_allowed_by_default(self):
        plugin = self._plugin()
        plugin._cola = self._fake_cola(
            me={"id": 1, "username": "root", "roles": ["admin"]},
            profile={"groups": []})
        self.assertTrue(plugin.seed_group_identity()["staff"])

    def test_identity_admin_can_be_denied(self):
        plugin = self._plugin()
        plugin._seedgroup_allow_admin = False
        plugin._cola = self._fake_cola(
            me={"id": 1, "username": "root", "roles": ["admin"]},
            profile={"groups": []})
        self.assertFalse(plugin.seed_group_identity()["staff"])

    def test_unlock_persists_for_staff(self):
        plugin = self._plugin()
        plugin._cola = self._fake_cola(
            me={"id": 7, "username": "yg", "roles": ["member"]},
            profile={"groups": ["工作组-组长"]})
        record = plugin.seed_group_unlock()
        self.assertTrue(record["ok"])
        self.assertGreater(record["until"], record["at"])
        self.assertTrue(plugin.get_data("seedgroup_unlock")["ok"])
        self.assertTrue(plugin._sg_unlocked(refresh=False)[0])

    def test_unlock_refused_for_non_staff(self):
        plugin = self._plugin()
        plugin._cola = self._fake_cola(
            me={"id": 8, "username": "member", "roles": ["member"]},
            profile={"groups": ["VIP"]})
        record = plugin.seed_group_unlock()
        self.assertFalse(record["ok"])
        self.assertFalse(plugin._sg_unlocked(refresh=False)[0])

    def test_panel_shows_staff_status(self):
        plugin = self._plugin()
        plugin.save_data("seedgroup_unlock", {"ok": True, "at": 1763000000, "until": 1999999999,
                                             "groups": ["工作组-保种"], "username": "yg"})
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("可乐保种组", blob)
        self.assertIn("工作组-保种", blob)

    def test_panel_shows_locked_reason(self):
        plugin = self._plugin()
        plugin.save_data("seedgroup_unlock", {"ok": False, "at": 0, "until": 0,
                                             "reason": "账号不在工作组：组名需以「工作组」开头"})
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("无法解锁", blob)

    def test_service_registers_seed_group_job(self):
        plugin = self._plugin()
        names = [s["name"] for s in plugin.get_service()]
        self.assertIn("可乐保种组", names)

    def test_service_empty_when_disabled(self):
        plugin = self._plugin()
        plugin._seedgroup_enabled = False
        plugin._enabled = False
        self.assertEqual(plugin.get_service(), [])

    def test_sync_locked_does_not_add(self):
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        plugin._cola = self._fake_cola(
            me={"id": 8, "username": "member", "roles": ["member"]},
            profile={"groups": ["VIP"]})
        plugin._sg_candidates = lambda: [{"torrent_id": 1, "info_hash": "a", "size": mod.GIB}]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_sync()
        self.assertEqual(client.added, [])
        state = plugin.get_data("seedgroup_state")
        self.assertFalse(state["unlocked"])
        self.assertIn("不在工作组", state["reason"])

    def test_sync_fills_to_tier_and_reports_covered(self):
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        plugin._cola = self._fake_cola(
            me={"id": 7, "username": "yg", "roles": ["member"]},
            profile={"groups": ["工作组-保种"]})
        plugin._sg_candidates = lambda: [
            {"torrent_id": 11, "info_hash": "a" * 40, "size": 1500 * mod.GIB},
            {"torrent_id": 12, "info_hash": "b" * 40, "size": 600 * mod.GIB},
            {"torrent_id": 13, "info_hash": "c" * 40, "size": 100 * mod.GIB},
        ]
        plugin._sg_download = lambda item: support.make_torrent(name=f"t{item['torrent_id']}.mkv")
        plugin._seed_group_sync()
        self.assertEqual(len(client.added), 2)          # 1500 + 600 = 2100 ≥ 2048
        self.assertTrue(all(mod.SEED_GROUP_TAG in a["label"] for a in client.added))
        state = plugin.get_data("seedgroup_state")
        self.assertTrue(state["unlocked"])
        self.assertEqual(state["added"], 2)
        self.assertGreaterEqual(state["covered_gib"], 2048)
        self.assertEqual(state["shortfall_gib"], 0)

    def test_sync_counts_existing_tagged_volume(self):
        plugin = self._plugin()
        client = support.FakeClient()
        client.list_completed = lambda: [
            {"hash": "e" * 40, "name": "old", "size": 2048 * mod.GIB,
             "tags": f"{mod.MANDATORY_TAG},{mod.SEED_GROUP_TAG}", "state": "uploading"},
        ]
        plugin._sg_clients = lambda: [client]
        plugin._cola = self._fake_cola(
            me={"id": 7, "username": "yg", "roles": ["member"]},
            profile={"groups": ["工作组-保种"]})
        plugin._sg_candidates = lambda: [
            {"torrent_id": 21, "info_hash": "f" * 40, "size": 300 * mod.GIB}]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_sync()
        self.assertEqual(client.added, [])
        self.assertGreaterEqual(plugin.get_data("seedgroup_state")["covered_gib"], 2048)

    def test_official_machine_page_parses_items(self):
        plugin = self._plugin()
        plugin._seedgroup_api_key = "rcapi_test"
        body = json.dumps({"items": [{"torrent_id": 5, "release_name": "Movie",
                                      "size_bytes": 2 * mod.GIB, "official": True}],
                           "page": 1, "page_size": 100, "total": 1}).encode()
        plugin_curl = mod._curl

        def fake_curl(method, url, **kwargs):
            return mod._HttpResult(200, body, body.decode())

        mod._curl = fake_curl
        try:
            items, total, source = plugin._sg_official_machine_page(1)
        finally:
            mod._curl = plugin_curl
        self.assertEqual(total, 1)
        self.assertEqual(source, "api-key")
        self.assertEqual(items[0]["torrent_id"], 5)
        self.assertAlmostEqual(mod._seed_group_size_gib(items[0]["size"]), 2.0)

    def test_browse_page_extracts_official_versions(self):
        plugin = self._plugin()
        body = json.dumps({"items": [{"versions": [
            {"id": 31, "release_name": "A", "size_bytes": mod.GIB, "official": True},
            {"id": 32, "release_name": "B", "size_bytes": mod.GIB, "official": False},
        ]}], "page": 1, "page_size": 50, "total": 1}).encode()
        plugin._cola = lambda m, p, **k: mod._HttpResult(200, body, body.decode())
        items, total, source = plugin._sg_official_browse_page(1)
        self.assertEqual(total, 1)
        self.assertEqual(source, "session")
        self.assertEqual([x["torrent_id"] for x in items], [31])


class SeedGroupClientTest(unittest.TestCase):
    def _plugin(self):
        plugin = support.make_plugin(mod)
        plugin._use_mp_clients = True
        plugin._clients_lines = ""
        plugin._clients_json = ""
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        return plugin

    def test_custom_lines_used_first(self):
        plugin = self._plugin()
        plugin._seedgroup_clients_lines = "qb,192.168.1.9:8080,admin,pw"
        plugin._mp_clients = lambda: (_ for _ in ()).throw(AssertionError("不该走 MP"))

        class DummyQB:
            def __init__(self, *args, **kwargs):
                self.name = args[0] if args else "qb"

        original, mod.QBittorrentClient = mod.QBittorrentClient, DummyQB
        try:
            clients = plugin._sg_clients()
        finally:
            mod.QBittorrentClient = original
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].name, "192.168.1.9:8080")

    def test_mp_clients_used_when_no_lines(self):
        plugin = self._plugin()
        fake = support.FakeClient()
        fake.name = "MP-qb"
        plugin._mp_clients = lambda: [fake]
        self.assertEqual([c.name for c in plugin._sg_clients()], ["MP-qb"])

    def test_target_client_filters(self):
        plugin = self._plugin()
        a, b = support.FakeClient(), support.FakeClient()
        a.name, b.name = "MP-qb", "MP-tr"
        plugin._mp_clients = lambda: [a, b]
        plugin._seedgroup_target_client = "MP-tr"
        self.assertEqual([c.name for c in plugin._sg_clients()], ["MP-tr"])

    def test_falls_back_to_crossseed_clients(self):
        plugin = self._plugin()
        fake = support.FakeClient()
        plugin._seedgroup_use_mp_clients = False
        plugin._clients = lambda: [fake]
        self.assertEqual(plugin._sg_clients(), [fake])


if __name__ == "__main__":
    unittest.main(verbosity=2)
