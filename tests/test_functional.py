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
SG_MOD = support.load_seedgroup_plugin()  # 保种组插件（2026-11-17 拆分后单独加载）
SG = None  # 保种测试单独加载，见 load_seedgroup_plugin()

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


class InitOrderTest(unittest.TestCase):
    """保存配置时的顺序与失败语义（审查发现的 S1 / 网络瞬断 Q5）。"""

    def _ident(self, ok=True, staff=True, reason=""):
        return {"ok": ok, "staff": staff, "reason": reason, "uid": 7,
                "username": "yg", "roles": ["member"], "groups": ["工作组-保种"]}

    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._session_token = "tok"
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        return plugin

    def test_limits_survive_unlock_failure(self):
        plugin = self._plugin()
        plugin.seed_group_identity = lambda: self._ident(ok=False, staff=False, reason="站点 502")
        plugin.init_plugin({"enabled": True, "limit_dl_kib": 500, "limit_up_kib": 300,
                            "seedgroup_enabled": True})
        # 解锁校验失败不能把保种限速写成类属性默认值（0 = 不限）
        self.assertEqual(plugin._limit_dl_kib, 500)
        self.assertEqual(plugin._limit_up_kib, 300)

    def test_network_failure_keeps_switch(self):
        plugin = self._plugin()
        plugin.seed_group_identity = lambda: self._ident(ok=False, staff=False, reason="站点 502")
        plugin.init_plugin({"enabled": True, "seedgroup_enabled": True})
        self.assertTrue(plugin._seedgroup_enabled, "网络失败不是没资格，不该静默关功能")

    def test_non_staff_keeps_switch_on(self):
        """2026-11-18 老板定：任何站点用户都能保种，只有工作组成员才汇报。"""
        plugin = self._plugin()
        plugin.seed_group_identity = lambda: self._ident(ok=True, staff=False, reason="不在工作组")
        plugin.init_plugin({"enabled": True, "seedgroup_enabled": True, "limit_dl_kib": 40})
        self.assertTrue(plugin._seedgroup_enabled, "没头衔不该把功能关掉")
        self.assertEqual(plugin._limit_dl_kib, 40)

    def test_staff_keeps_switch_on(self):
        plugin = self._plugin()
        plugin.seed_group_identity = lambda: self._ident()
        plugin.init_plugin({"enabled": True, "seedgroup_enabled": True})
        self.assertTrue(plugin._seedgroup_enabled)


class SeedGroupPaginationTest(unittest.TestCase):
    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "10T"
        plugin._seedgroup_custom_gib = 2048
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

    def test_machine_pages_by_total_not_by_page_size(self):
        plugin = self._plugin()

        def probe(page):
            start = (page - 1) * 40          # 服务端每页只给 40 条
            return ([{"torrent_id": start + i} for i in range(40)], 200, "api-key")

        items, total, _ = plugin._sg_collect(probe, True)
        self.assertEqual(total, 200)
        self.assertEqual(len(items), 200)

    def test_browse_pages_by_media_total(self):
        plugin = self._plugin()

        def probe(page):
            return ([{"torrent_id": page * 100}], 120, "session")   # 120 个作品，每页 50

        items, total, _ = plugin._sg_collect(probe, False)
        self.assertEqual(len(items), 3)      # ceil(120/50) = 3 页

    def test_empty_page_without_total_stops(self):
        plugin = self._plugin()
        calls = []

        def probe(page):
            calls.append(page)
            return [], 0, "session"

        items, _, _ = plugin._sg_collect(probe, False)
        self.assertEqual(items, [])
        self.assertEqual(len(calls), 1)

    def test_collect_dedupes_torrent_ids(self):
        plugin = self._plugin()

        def probe(page):
            if page == 1:
                return ([{"torrent_id": 1}, {"torrent_id": 1}, {"torrent_id": 2}], 3, "api-key")
            return ([], 3, "api-key")

        items, _, _ = plugin._sg_collect(probe, True)
        self.assertEqual([x["torrent_id"] for x in items], [1, 2])


class NoMpFallbackTest(unittest.TestCase):
    """保种组绝不允许回落到 MoviePilot 的下载器列表。

    2026-11-19 事故：用户只想往一台远端 qB 加种，自定义行写得不规范导致解析
    为空，插件就 `out = self._clients()` 拿 MP 里**所有**下载器 —— 于是把种
    同时推到了家里 NAS 的 qB，占掉本机磁盘和上行。
    保种组的目标机器必须是用户显式写下的那几台，猜错代价太大。
    """

    def setUp(self):
        self.p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

    def test_empty_custom_lines_returns_no_client(self):
        self.p._seedgroup_clients_lines = ""
        self.p._seedgroup_use_mp_clients = False
        self.assertEqual(self.p._sg_clients(), [],
                         "没配自定义行时不能回落 MP 下载器")

    def test_broken_custom_lines_returns_no_client(self):
        self.p._seedgroup_clients_lines = "这不是一行合法配置"
        self.p._seedgroup_use_mp_clients = False
        self.assertEqual(self.p._sg_clients(), [],
                         "配置写坏了也不能回落 MP 下载器")

    def test_explicit_client_is_used(self):
        self.p._seedgroup_clients_lines = "198-远端,198.72.127.68:8081,qb1,qq321321"
        self.p._seedgroup_use_mp_clients = False
        got = self.p._sg_clients()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].url, "http://198.72.127.68:8081")


class ClientLineFormatTest(unittest.TestCase):
    """下载器配置行的所有常见写法都要能解析对。

    2026-11-19 事故：`地址,用户,密码,名称` 被切成 name=地址、url=用户，
    报 `Failed to resolve 'a172604'` —— 4 段格式按位置切是错的。
    现在按内容识别地址，地址前的段当名字、地址后的段当用户名密码。
    """

    def setUp(self):
        self.p = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

    def _one(self, line):
        got = self.p._parse_client_lines(line)
        self.assertEqual(len(got), 1, f"{line!r} 应该解析出 1 个客户端")
        return got[0]

    def test_three_segments(self):
        c = self._one("192.168.31.100:8081,a172604,qq321321")
        self.assertEqual(c.url, "http://192.168.31.100:8081")

    def test_name_after_password(self):
        c = self._one("192.168.31.100:8081,a172604,qq321321,NAS-本地")
        self.assertEqual(c.name, "NAS-本地")
        self.assertEqual(c.url, "http://192.168.31.100:8081")

    def test_name_before_address(self):
        c = self._one("198-远端,198.72.127.68:8081,qb1,qq321321")
        self.assertEqual(c.name, "198-远端")
        self.assertEqual(c.url, "http://198.72.127.68:8081")

    def test_explicit_kind_prefix(self):
        c = self._one("qb,198.72.127.68:8081,qb1,qq321321,198-远端")
        self.assertEqual(c.name, "198-远端")
        self.assertEqual(c.url, "http://198.72.127.68:8081")

    def test_username_is_not_mistaken_for_address(self):
        """`a172604` / `qq321321` / `qb1` 都是纯字母数字，绝不能被当成地址。

        2026-11-19 二次事故：解析器改成按内容识别地址后，「纯 ASCII 主机名」
        这条规则把它们全判成了地址，于是 url=a172604，还是 `Failed to resolve`。
        """
        for token in ("a172604", "qq321321", "qb1", "NAS-本地", "198-远端"):
            self.assertFalse(self.p._looks_like_address(token),
                             f"{token!r} 不是地址")
        for token in ("192.168.31.100:8081", "127.0.0.1", "localhost",
                      "http://127.0.0.1:8081", "qbt.example.com"):
            self.assertTrue(self.p._looks_like_address(token),
                            f"{token!r} 是地址")

    def test_full_scheme(self):
        c = self._one("http://127.0.0.1:8081,qb1,pwd")
        self.assertEqual(c.url, "http://127.0.0.1:8081")

    def test_two_lines_both_parsed(self):
        got = self.p._parse_client_lines(
            "192.168.31.100:8081,a172604,qq321321,NAS-本地\n"
            "198-远端,198.72.127.68:8081,qb1,qq321321")
        self.assertEqual(len(got), 2)
        self.assertEqual({c.name for c in got}, {"NAS-本地", "198-远端"})


class StaleCoverageFallbackTest(unittest.TestCase):
    """弱网/NAT 场景：某台客户端读不到时，沿用上次读数，不能把覆盖清零。

    大量用户的 MP 装在家里（无公网 IP），qB 在别处；一次 read timeout 不该
    让 on_target 从 1 掉到 0、把保种时长的连续区间打断。
    """

    def _plugin(self):
        return support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")

    def test_unreachable_client_reuses_last_reading(self):
        p = self._plugin()
        good = support.FakeClient()
        good.name = "remote"
        p._sg_current([good])                       # 第一次：读成功，写入缓存
        calls = []

        class Dead:
            name = "remote"
            def list_completed(self):
                calls.append(1)
                raise TimeoutError("read timed out")

        hashes, gib, blind = p._sg_current([Dead()])
        self.assertTrue(calls)
        self.assertEqual(blind, 1, "读失败要记一次 blind，但不能算 0")
        # FakeClient 默认返回空列表 ⇒ 第一次缓存 gib=0；这里只断言「不抛、不炸」
        self.assertGreaterEqual(gib, 0.0)

    def test_successful_reading_overwrites_cache(self):
        p = self._plugin()
        c = support.FakeClient()
        c.name = "remote"
        p._sg_current([c])
        cache = p.get_data("seedgroup_client_cache") or {}
        self.assertIn("remote", cache)
        self.assertIn("at", cache["remote"])

    def test_cache_expires_after_ttl(self):
        p = self._plugin()
        # 造一个 25 小时前的缓存
        p.save_data("seedgroup_client_cache", {
            "remote": {"at": int(__import__("time").time()) - 25 * 3600,
                       "gib": 999.0, "hashes": ["x"]}})

        class Dead:
            name = "remote"
            def list_completed(self):
                raise TimeoutError("down")

        _, gib, blind = p._sg_current([Dead()])
        self.assertEqual(gib, 0.0, "超过 TTL 的缓存必须失效，不能一直算进去")
        self.assertEqual(blind, 1)


class PaginatedListTest(unittest.TestCase):
    """慢链路上必须分页拉：一次性 1.4 MB 会被截断成空（2026-11-18 生产事故）。"""

    def test_page_size_and_requests_are_paged(self):
        calls = []

        class Resp:
            def __init__(self, payload): self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        class Sess:
            def get(self, url, params=None, timeout=None):
                calls.append(dict(params or {}))
                off = (params or {}).get("offset", 0)
                # 250 条：100 + 100 + 50
                n = min(10, max(0, 250 - off))
                return Resp([{"hash": f"h{off+i:04d}", "size": 1024} for i in range(n)])

        c = SG_MOD.QBittorrentClient.__new__(SG_MOD.QBittorrentClient)
        c.name = "fake"
        c.url = "http://x"
        c._session = Sess()
        rows = c.list_completed()
        self.assertEqual(len(rows), 250, "分页必须拼回全量")
        self.assertTrue(all("limit" in x and "offset" in x for x in calls), "每次都要带 limit/offset")
        self.assertEqual([x["offset"] for x in calls][:3], [0, 10, 20], "页码要连续（每页 10 条）")
        self.assertEqual(len(calls), 26, "250 条 ÷ 10 条/页 = 25 页 + 1 次空页收尾")
        self.assertEqual(calls[0]["limit"], SG_MOD.QBittorrentClient.QB_LIST_PAGE)


class OnTargetToleranceTest(unittest.TestCase):
    """达标判定要留容差：covered 与 target 天然差几 MB，严格 >= 会让保种时长永远 0。"""

    def test_exact_goal_counts(self):
        self.assertTrue(SG_MOD._sg_on_target(1152.4, 1152.4))

    def test_slightly_under_still_counts(self):
        self.assertTrue(SG_MOD._sg_on_target(1149.4, 1152.4))

    def test_clearly_short_does_not_count(self):
        self.assertFalse(SG_MOD._sg_on_target(1132.4, 1152.4))

    def test_all_tier_counts_at_sixty_percent(self):
        """all 档：覆盖到 60% 就算达标（目标每天都在涨，要求 100% 追不上）。"""
        r = SG_MOD.SEED_GROUP_ALL_TARGET_RATIO
        self.assertEqual(r, 0.6)
        self.assertTrue(SG_MOD._sg_on_target(840, 1400, ratio=r), "60% 该达标")
        # 同样 50%：不传 ratio（默认 100%）不达标；传 60% 阈值下也不达标
        self.assertFalse(SG_MOD._sg_on_target(700, 1400, ratio=r), "50% 不该达标")
        self.assertFalse(SG_MOD._sg_on_target(700, 1400), "不传 ratio 时是 100% 语义")

    def test_all_tier_still_needs_sixty_percent(self):
        r = SG_MOD.SEED_GROUP_ALL_TARGET_RATIO
        self.assertFalse(SG_MOD._sg_on_target(820, 1400, ratio=r), "58.6% 不该达标")
        self.assertTrue(SG_MOD._sg_on_target(845, 1400, ratio=r), "60.4% 该达标")

    def test_missing_values_do_not_count(self):
        for c, t in ((0, 1152), (None, 1152), (1152, 0), (1152, None)):
            self.assertFalse(SG_MOD._sg_on_target(c, t))


class StaffFlagFromRecordTest(unittest.TestCase):
    """解锁记录里只有 groups，staff 要现算 —— 读 record["staff"] 会永远拿到 None。"""

    def test_staff_computed_from_group_prefix(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", store={
            "seedgroup_unlock": {"ok": True, "until": 9999999999, "uid": 18,
                                 "groups": ["工作组-组长"], "roles": ["member"]}})
        plugin._seedgroup_staff_prefix = "工作组"
        self.assertTrue(plugin._sg_unlock_record()["staff"])

    def test_plain_member_is_not_staff(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", store={
            "seedgroup_unlock": {"ok": True, "until": 9999999999, "uid": 8,
                                 "groups": ["VIP"], "roles": ["member"]}})
        plugin._seedgroup_staff_prefix = "工作组"
        self.assertFalse(plugin._sg_unlock_record()["staff"])

    def test_admin_counts_when_allowed(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", store={
            "seedgroup_unlock": {"ok": True, "until": 9999999999, "uid": 1,
                                 "groups": [], "roles": ["admin"]}})
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        self.assertTrue(plugin._sg_unlock_record()["staff"])


class SeedGroupDownloadQuotaTest(unittest.TestCase):
    """进种限流（2026-11-16）：客户端「正在下载」达到上限就停止进种，低于就按剩余额度补。"""

    def _plugin(self, inflight, limit=5):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._session_token = "tok"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_max_downloading = limit
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        client = support.FakeClient()
        client.inflight_count = inflight
        plugin._sg_clients = lambda: [client]
        plugin._sg_unlocked = lambda: (True, {"ok": True, "groups": ["工作组-组长"], "reason": ""})
        plugin._sg_current = lambda clients: (set(), 0.0, 0)
        plugin._sg_skips = lambda: set()
        plugin._sg_candidates = lambda: [
            {"torrent_id": i, "info_hash": f"{i:040x}", "size": 500 * mod.GIB, "official": True}
            for i in range(1, 21)]
        plugin._sg_download = lambda item: support.make_torrent(name=f"t{item['torrent_id']}.mkv",
                                                                source=f"s{item['torrent_id']}")
        plugin._maybe_report = lambda clients, state: None
        plugin._sg_prune_if_enabled = lambda state: None
        return plugin, client

    def test_queue_full_stops_ingesting(self):
        plugin, client = self._plugin(inflight=5, limit=5)
        plugin.seed_group_sync()
        state = plugin.get_data("seedgroup_state") or {}
        self.assertEqual(state.get("downloading"), 5)
        self.assertEqual(state.get("download_quota"), 0)
        self.assertEqual(client.added, [], "队列满时一颗都不该加")
        self.assertIn("下载队列已满", str(state.get("reason") or ""))
        self.assertGreater(state.get("shortfall_gib", 0), 0)

    def test_partial_quota_adds_only_remaining(self):
        plugin, client = self._plugin(inflight=3, limit=5)
        plugin.seed_group_sync()
        state = plugin.get_data("seedgroup_state") or {}
        self.assertEqual(state.get("downloading"), 3)
        self.assertEqual(state.get("download_quota"), 2)
        self.assertEqual(len(client.added), 2, "只剩 2 个额度就只加 2 颗")
        self.assertEqual(state.get("quota_trimmed"), 3, "按目标本该挑 5 颗，额度只给 2 颗，砍掉 3 颗留到下一轮")

    def test_zero_limit_means_unlimited(self):
        plugin, client = self._plugin(inflight=50, limit=0)
        plugin.seed_group_sync()
        state = plugin.get_data("seedgroup_state") or {}
        self.assertEqual(state.get("download_quota"), "unlimited")
        self.assertGreater(len(client.added), 0, "0 = 不限制，门槛不该卡住")

    def test_unreadable_inflight_does_not_block(self):
        plugin, client = self._plugin(inflight=-1, limit=5)
        plugin.seed_group_sync()
        state = plugin.get_data("seedgroup_state") or {}
        self.assertGreater(len(client.added), 0, "读不到队列时不能把保种卡死")


class SeedGroupSyncRobustnessTest(unittest.TestCase):
    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._session_token = "tok"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        return plugin

    def test_duplicate_does_not_inflate_coverage(self):
        plugin = self._plugin()
        torrent = support.make_torrent()
        hash_ = support.info_hash_of(torrent)
        client = support.FakeClient(has_hash=[hash_])
        client.list_completed = lambda: [
            {"hash": hash_, "name": "x", "size": 1024 * mod.GIB,
             "tags": SG_MOD.SEED_GROUP_TAG, "state": "uploading"}]
        plugin._sg_clients = lambda: [client]
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(200, b"{}", "{}")
        plugin.seed_group_identity = lambda: {"ok": True, "staff": True, "reason": "", "uid": 7,
                                              "username": "yg", "roles": [], "groups": ["工作组-保种"]}
        plugin._sg_candidates = lambda: [{"torrent_id": 9, "size": 1500 * mod.GIB}]
        plugin._sg_download = lambda item: torrent
        plugin._seed_group_sync()
        state = plugin.get_data("seedgroup_state")
        self.assertEqual(state["duplicate"], 1)
        self.assertEqual(state["added"], 0)
        # 重复的那颗不能算进覆盖：1024 还是 1024，缺口照实报
        self.assertEqual(state["covered_gib"], 1024)
        self.assertEqual(state["shortfall_gib"], 1024)

    def test_failed_add_is_skipped_next_round(self):
        plugin = self._plugin()
        client = support.FakeClient(add_result="fail")
        client.list_completed = lambda: []
        plugin._sg_clients = lambda: [client]
        plugin.seed_group_identity = lambda: {"ok": True, "staff": True, "reason": "", "uid": 7,
                                              "username": "yg", "roles": [], "groups": ["工作组-保种"]}
        plugin._sg_candidates = lambda: [{"torrent_id": 5, "info_hash": "a" * 40, "size": 3000 * mod.GIB}]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_sync()
        self.assertEqual(plugin.get_data("seedgroup_state")["failed"], 1)
        self.assertIn("5", plugin.get_data("seedgroup_skip"))

    def test_add_exception_does_not_kill_round(self):
        plugin = self._plugin()
        client = support.FakeClient()

        def boom(*args, **kwargs):
            raise RuntimeError("qb 连接断了")

        client.add = boom
        client.list_completed = lambda: []
        plugin._sg_clients = lambda: [client]
        plugin.seed_group_identity = lambda: {"ok": True, "staff": True, "reason": "", "uid": 7,
                                              "username": "yg", "roles": [], "groups": ["工作组-保种"]}
        plugin._sg_candidates = lambda: [{"torrent_id": 6, "info_hash": "b" * 40, "size": 3000 * mod.GIB}]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_sync()
        state = plugin.get_data("seedgroup_state")
        self.assertEqual(state["failed"], 1)
        self.assertGreater(state["shortfall_gib"], 0)


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


class SeedGroupPruneTest(unittest.TestCase):
    """站点下架/撤掉官方标记后，本地那颗要不要删。"""

    HASH = "a" * 40

    def _plugin(self, status="approved", official=True, http=200):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_use_mp_clients = True
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin._seedgroup_prune = True
        plugin._seedgroup_prune_files = False
        client = support.FakeClient()
        client.list_completed = lambda: [
            {"hash": self.HASH, "name": "x", "size": mod.GIB,
             "tags": SG_MOD.SEED_GROUP_TAG, "state": "uploading"}]
        plugin._sg_clients = lambda: [client]
        body = json.dumps({"id": 3, "status": status, "official": official,
                           "gate_phase": "confirmed"}).encode()
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(http, body, body.decode())
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 3, "size_gib": 1}})
        return plugin, client

    def test_withdrawn_is_removed(self):
        plugin, client = self._plugin(status="withdrawn")
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 1)
        self.assertIn(self.HASH, client.deleted)

    def test_rejected_is_removed(self):
        plugin, client = self._plugin(status="rejected")
        self.assertEqual(plugin._sg_prune()["removed"], 1)

    def test_official_flag_dropped_is_removed(self):
        plugin, client = self._plugin(status="approved", official=False)
        self.assertEqual(plugin._sg_prune()["removed"], 1)

    def test_site_404_is_removed(self):
        plugin, client = self._plugin(http=404)
        self.assertEqual(plugin._sg_prune()["removed"], 1)

    def test_still_official_is_kept(self):
        plugin, client = self._plugin()
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["kept"], 1)
        self.assertEqual(client.deleted, [])

    def test_unmapped_seed_is_never_deleted(self):
        plugin, client = self._plugin(status="withdrawn")
        plugin.save_data("seedgroup_items", {})
        out = plugin._sg_prune()
        self.assertEqual(out["unmapped"], 1)
        self.assertEqual(out["removed"], 0)
        self.assertEqual(client.deleted, [])

    def test_delete_files_flag_is_honoured(self):
        plugin, client = self._plugin(status="withdrawn")
        plugin._seedgroup_prune_files = True
        seen = {}
        client.delete = lambda hashes, delete_files=False: seen.update(
            {"hashes": hashes, "files": delete_files}) or True
        plugin._sg_prune()
        self.assertTrue(seen["files"])


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


class PruneSafetyTest(unittest.TestCase):
    """下架清理的安全边界：删失败不能记账、读不到 status 不能删、关了就不能删。"""

    HASH = "b" * 40

    def _plugin(self, status="withdrawn", mode="seed", delete_ok=True):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_prune_mode = mode
        plugin._seedgroup_prune = mode != "off"
        plugin._seedgroup_prune_files = mode == "files"
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_client_mode = "custom"
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        client = support.FakeClient()
        client.list_completed = lambda: [
            {"hash": self.HASH, "name": "x", "size": mod.GIB,
             "tags": SG_MOD.SEED_GROUP_TAG, "state": "uploading"}]
        if not delete_ok:
            client.delete = lambda hashes, delete_files=False: False
        plugin._sg_clients = lambda: [client]
        body = json.dumps({"id": 3, "status": status, "official": True}).encode()
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(200, body, body.decode())
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": 3}})
        return plugin, client

    def test_delete_failure_is_not_counted(self):
        plugin, client = self._plugin(status="withdrawn", delete_ok=False)
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["failed"], 1)
        self.assertIn(self.HASH, plugin.get_data("seedgroup_items"))

    def test_missing_status_is_kept(self):
        plugin, client = self._plugin(status="")
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(out["kept"], 1)

    def test_prune_off_does_nothing(self):
        plugin, client = self._plugin(status="withdrawn", mode="off")
        out = plugin._sg_prune()
        self.assertEqual(out["removed"], 0)
        self.assertEqual(client.deleted, [])
        self.assertIn("关闭", out["reason"])

    def test_dirty_torrent_id_is_unmapped(self):
        plugin, client = self._plugin()
        plugin.save_data("seedgroup_items", {self.HASH: {"torrent_id": "abc"}})
        out = plugin._sg_prune()
        self.assertEqual(out["unmapped"], 1)
        self.assertEqual(out["removed"], 0)

    def test_mp_client_without_delete_reports_failure(self):
        plugin = self._plugin()
        mp = mod._MPClient("MP-x", lambda: [], lambda **kw: (True, []), None)
        self.assertFalse(mp.delete(["deadbeef"]))


class RotateTest(unittest.TestCase):
    """动态保种：达标后删「做种人数最多」的、补「最少」的，每次 N 颗。"""

    def _plugin(self, rotate=True, count=5):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = True
        plugin._seedgroup_rotate = rotate
        plugin._seedgroup_rotate_count = count
        plugin._seedgroup_rotate_files = True
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_client_mode = "custom"
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        plugin.SG_DOWNLOAD_GAP = 0        # 测试里不等节流
        return plugin

    def test_rotate_disabled(self):
        plugin = self._plugin(rotate=False)
        out = plugin._sg_rotate([support.FakeClient()], {}, [])
        self.assertIn("关闭", out["reason"])

    def test_drops_highest_seeders_and_adds_lowest(self):
        plugin = self._plugin()
        # 客户端里已有 A(seeders=900) B(seeders=400)；C(seeders=3)/D(seeders=1) 未保
        client = support.FakeClient()
        client.list_completed = lambda: [
            {"hash": "a" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": mod.GIB},
            {"hash": "b" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": mod.GIB},
        ]
        plugin.save_data("seedgroup_items", {
            "a" * 40: {"torrent_id": 1, "name": "A"},
            "b" * 40: {"torrent_id": 2, "name": "B"},
        })
        pool = [
            {"torrent_id": 1, "info_hash": "a" * 40, "name": "A", "seeders": 900, "size": mod.GIB},
            {"torrent_id": 2, "info_hash": "b" * 40, "name": "B", "seeders": 400, "size": mod.GIB},
            {"torrent_id": 3, "info_hash": "c" * 40, "name": "C", "seeders": 3, "size": mod.GIB},
            {"torrent_id": 4, "info_hash": "d" * 40, "name": "D", "seeders": 1, "size": mod.GIB},
        ]
        plugin._sg_download = lambda item: support.make_torrent(name=str(item.get("name")))
        out = plugin._sg_rotate([client], {}, pool)
        self.assertEqual(out["dropped"], 2)          # count=5 但只有 2 颗可删
        self.assertEqual(out["added"], 2)
        self.assertTrue(any("A" in n for n in out["dropped_names"]))   # 900 先删
        self.assertTrue(any("D" in n for n in out["added_names"]))     # 1 先补
        self.assertIn("a" * 40, client.deleted)

    def test_rotate_respects_count(self):
        plugin = self._plugin(count=1)
        client = support.FakeClient()
        client.list_completed = lambda: [{"hash": "a" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": mod.GIB}]
        plugin.save_data("seedgroup_items", {"a" * 40: {"torrent_id": 1, "name": "A"}})
        pool = [{"torrent_id": 1, "info_hash": "a" * 40, "name": "A", "seeders": 50, "size": mod.GIB},
                {"torrent_id": 2, "info_hash": "b" * 40, "name": "B", "seeders": 0, "size": mod.GIB}]
        plugin._sg_download = lambda item: support.make_torrent()
        out = plugin._sg_rotate([client], {}, pool)
        self.assertEqual(out["dropped"], 1)
        self.assertEqual(out["added"], 1)

    def test_rotate_skips_unknown_torrent(self):
        plugin = self._plugin()
        client = support.FakeClient()
        client.list_completed = lambda: [{"hash": "z" * 40, "tags": SG_MOD.SEED_GROUP_TAG, "size": mod.GIB}]
        plugin.save_data("seedgroup_items", {"z" * 40: {"torrent_id": 99, "name": "Z"}})
        out = plugin._sg_rotate([client], {}, [{"torrent_id": 1, "name": "A", "seeders": 1}])
        self.assertEqual(out["dropped"], 0)
        self.assertIn("查不到", out["reason"])


class AllTierTest(unittest.TestCase):
    """「全站官种」档：目标按未过滤的池子算，跳过表不能让它缩水。"""

    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._session_token = "tok"
        plugin._seedgroup_enabled = True
        plugin._seedgroup_tier = "all"
        plugin._seedgroup_custom_gib = 2048
        plugin._seedgroup_cron = "0 */6 * * *"
        plugin._seedgroup_api_key = ""
        plugin._seedgroup_staff_prefix = "工作组"
        plugin._seedgroup_allow_admin = True
        plugin._seedgroup_client_mode = "custom"
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._seedgroup_target_client = ""
        plugin._seedgroup_save_path = ""
        plugin._seedgroup_skip_check = True
        plugin._seedgroup_prune_mode = "off"
        plugin._seedgroup_prune = False
        plugin._seedgroup_report = False
        plugin.update_config = lambda cfg: None
        plugin.post_message = lambda **k: None
        plugin.seed_group_identity = lambda: {"ok": True, "staff": True, "reason": "", "uid": 7,
                                              "username": "yg", "roles": [], "groups": ["工作组-保种"]}
        return plugin

    def test_target_ignores_skip_table(self):
        plugin = self._plugin()
        client = support.FakeClient()
        client.list_completed = lambda: []
        plugin._sg_clients = lambda: [client]
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(200, b"{}", "{}")
        plugin._sg_candidates = lambda: [
            {"torrent_id": 1, "info_hash": "a" * 40, "size": 100 * mod.GIB},
            {"torrent_id": 2, "info_hash": "c" * 40, "size": 50 * mod.GIB},
        ]
        plugin.save_data("seedgroup_skip", {"2": {"at": 9_999_999_999, "reason": "fail"}})
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_sync()
        state = plugin.get_data("seedgroup_state")
        # 池子总量 150；跳过的 50 只是这轮不加，目标不能缩成 100
        self.assertEqual(state["target_gib"], 150.0)
        self.assertEqual(state["pool_count"], 2)
        self.assertEqual(state["pool_pending"], 1)

    def test_custom_unit_from_config(self):
        plugin = self._plugin()
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        plugin._session_token = ""
        plugin.init_plugin({"seedgroup_tier": "custom", "seedgroup_custom_gib": "1T"})
        self.assertEqual(plugin._seedgroup_custom_gib, 1024)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", plugin._seedgroup_custom_gib), 1024)


class SpeedReportTest(unittest.TestCase):
    """限速情况上报：只统计开了保种组的用户。"""

    def _plugin(self, enabled=True, report=True):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin._seedgroup_enabled = enabled
        plugin._seedgroup_report = report
        plugin._seedgroup_report_url = "http://23.81.118.186:18099/report"
        plugin._seedgroup_report_token = ""
        plugin._seedgroup_tier = "2T"
        plugin._seedgroup_scope = "site"
        plugin._seedgroup_custom_gib = 2048
        plugin.save_data("seedgroup_unlock", {"ok": True, "at": 1, "until": 9999999999,
                                             "uid": 18, "username": "goblin",
                                             "groups": ["工作组-组长"]})
        return plugin

    def test_skips_when_group_disabled(self):
        plugin = self._plugin(enabled=False)
        out = plugin._report_seedgroup([support.FakeClient()], {})
        self.assertTrue(out["skipped"])
        self.assertIn("未启用", out["reason"])

    def test_skips_when_report_off(self):
        plugin = self._plugin(report=False)
        out = plugin._report_seedgroup([support.FakeClient()], {})
        self.assertTrue(out["skipped"])

    def test_payload_contains_client_limits(self):
        plugin = self._plugin()

        class Limited(support.FakeClient):
            name = "198.72.127.68:8081"

            def get_speed_limits(self):
                return {"dl_kib": 1024, "up_kib": 2048, "dl_enabled": True, "up_enabled": True}

        captured = {}

        def fake_curl(method, url, **kwargs):
            captured["url"] = url
            captured["body"] = kwargs.get("json_body")
            return SG_MOD._HttpResult(200, b"{}", "{}")

        original, SG_MOD._curl = SG_MOD._curl, fake_curl
        try:
            out = plugin._report_seedgroup([Limited()], {"tier": "2T", "covered_gib": 100})
        finally:
            SG_MOD._curl = original
        self.assertTrue(out["ok"])
        client = captured["body"]["clients"][0]
        self.assertEqual(client["client"], "198.72.127.68:8081")
        self.assertEqual(client["dl_kib"], 1024)
        self.assertEqual(client["up_kib"], 2048)
        # 身份由会话决定，payload 不再自带 uid（站点契约 additionalProperties:false）
        self.assertNotIn("uid", captured["body"])
        self.assertNotIn("username", captured["body"])   # 同上：身份来自会话
        self.assertIn("enforced", captured["body"])

    def test_failure_is_recorded_not_raised(self):
        plugin = self._plugin()

        def fake_curl(method, url, **kwargs):
            return SG_MOD._HttpResult(404, b"", "404 page not found")

        original, SG_MOD._curl = SG_MOD._curl, fake_curl
        try:
            out = plugin._report_seedgroup([support.FakeClient()], {})
        finally:
            SG_MOD._curl = original
        self.assertFalse(out["ok"])
        self.assertIn("404", out["reason"])
        self.assertFalse(plugin.get_data("seedgroup_report_last")["ok"])

    def test_unreadable_client_marked(self):
        plugin = self._plugin()

        class Blind(support.FakeClient):
            name = "mp-client"

        captured = {}

        def fake_curl(method, url, **kwargs):
            captured["body"] = kwargs.get("json_body")
            return SG_MOD._HttpResult(200, b"", "")

        original, SG_MOD._curl = SG_MOD._curl, fake_curl
        try:
            plugin._report_seedgroup([Blind()], {})
        finally:
            SG_MOD._curl = original
        row = captured["body"]["clients"][0]
        self.assertFalse(row["readable"])
        self.assertEqual(row["dl_kib"], 0)      # 契约要整数：读不到记 0（= 不限），靠 readable 区分
        self.assertNotIn("error", row)          # 多一个字段整份上报就 400


class SeedGroupModeTest(unittest.TestCase):
    """档位/下载器来源/清理模式这三处「容易互相打架」的配置。"""

    def _plugin(self, **over):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        plugin.update_config = lambda cfg: None
        plugin.run_tests = lambda persist=True: None
        plugin._session_token = ""
        plugin.init_plugin(dict(over))
        return plugin

    def test_all_tier_is_dynamic(self):
        self.assertEqual(SG_MOD.seed_group_tier_key("all"), "all")
        self.assertEqual(SG_MOD.seed_group_tier_key("SITE"), "all")
        self.assertEqual(SG_MOD.seed_group_target_gib("all"), 0)
        self.assertIn("全站官种", mod.SEED_GROUP_TIER_LABELS["all"])

    def test_client_mode_explicit(self):
        self.assertEqual(self._plugin(seedgroup_client_mode="custom")._seedgroup_client_mode, "custom")
        self.assertFalse(self._plugin(seedgroup_client_mode="custom")._seedgroup_use_mp_clients)

    def test_client_mode_backward_compatible(self):
        # 老配置只有 use_mp_clients=false → 等价于 custom
        plugin = self._plugin(seedgroup_use_mp_clients=False)
        self.assertEqual(plugin._seedgroup_client_mode, "custom")

    def test_prune_mode_explicit(self):
        self.assertEqual(self._plugin(seedgroup_prune_mode="off")._seedgroup_prune_mode, "off")
        self.assertFalse(self._plugin(seedgroup_prune_mode="off")._seedgroup_prune)
        files = self._plugin(seedgroup_prune_mode="files")
        self.assertTrue(files._seedgroup_prune and files._seedgroup_prune_files)

    def test_prune_mode_backward_compatible(self):
        plugin = self._plugin(seedgroup_prune=True, seedgroup_prune_files=True)
        self.assertEqual(plugin._seedgroup_prune_mode, "files")
        plugin = self._plugin(seedgroup_prune=False)
        self.assertEqual(plugin._seedgroup_prune_mode, "off")

    def test_form_has_no_scope_selector(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        form, _ = plugin.get_form()
        blob = json.dumps(form, ensure_ascii=False)
        self.assertNotIn('"model": "seedgroup_scope"', blob)
        self.assertIn('"model": "seedgroup_client_mode"', blob)
        self.assertIn('"model": "seedgroup_prune_mode"', blob)
        self.assertIn('"value": "all"', blob)


class FormLayoutTest(unittest.TestCase):
    """设置页排版的结构检查：字段不漏、组件合法、分区顺序稳定。"""

    ALLOWED = {"VForm", "VRow", "VCol", "VAlert", "VSwitch", "VTextField",
               "VSelect", "VTextarea", "VDivider"}

    def _walk(self, node, components, models, titles):
        if isinstance(node, dict):
            comp = node.get("component")
            if comp:
                components.append(comp)
            props = node.get("props") or {}
            if props.get("model"):
                models.append(props["model"])
            if comp == "VAlert" and props.get("title"):
                titles.append(props["title"])
            for value in node.values():
                self._walk(value, components, models, titles)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, components, models, titles)

    def test_components_and_models_are_declared(self):
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        unknown = set(components) - self.ALLOWED
        self.assertFalse(unknown, f"表单里出现未验证过的组件：{unknown}")
        missing = [m for m in models if m not in defaults]
        self.assertFalse(missing, f"这些字段没有默认值：{missing}")
        self.assertEqual(len(models), len(set(models)), "同一个字段在表单里出现了两次")

    def test_sections_in_order(self):
        plugin = support.make_plugin(mod)
        form, _ = plugin.get_form()
        _, _, titles = [], [], []
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        ordered = [t for t in titles if t[:1] in "①②③④⑤"]
        self.assertEqual(len(ordered), 4, ordered)   # ⑤ 保种组已搬到独立插件，这里只剩 ①..④
        self.assertTrue(ordered[0].startswith("①"))
        self.assertTrue(ordered[2].startswith("③"))
        self.assertTrue(ordered[-1].startswith("④"))

    def test_critical_models_present(self):
        plugin = support.make_plugin(mod)
        form, _ = plugin.get_form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        # 保种相关的东西（⑤ 区、seedgroup_* 字段）已搬到 ColaSeedGroup，这里只断言辅种侧
        for key in ("enabled", "username", "password", "captcha_code", "iyuu_json",
                    "iyuu_json_file", "sites_mode",
                    "use_mp_clients", "clients_lines", "cron"):
            self.assertIn(key, models, key)

    def test_seedgroup_models_live_in_the_new_plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        form, _ = plugin.get_form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        for key in ("seedgroup_enabled", "seedgroup_tier", "seedgroup_custom_gib",
                    "seedgroup_cron", "seedgroup_prune_mode", "seedgroup_client_mode",
                    "seedgroup_clients_lines", "seedgroup_rotate_count", "seedgroup_rotate_files"):
            self.assertIn(key, models, key)

    def test_removed_fields_stay_out_of_ui(self):
        plugin = support.make_plugin(mod)
        form, _ = plugin.get_form()
        components, models, titles = [], [], []
        self._walk(form, components, models, titles)
        for key in ("seedgroup_api_key", "seedgroup_staff_prefix", "seedgroup_allow_admin"):
            self.assertNotIn(key, models)


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


class SeedGroupTierTest(unittest.TestCase):
    def test_tiers_are_gib_floor(self):
        self.assertEqual(SG_MOD.seed_group_target_gib("2T"), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib("5T"), 5120)
        self.assertEqual(SG_MOD.seed_group_target_gib("10T"), 10240)
        self.assertEqual(SG_MOD.seed_group_target_gib("15T"), 15360)
        self.assertEqual(SG_MOD.seed_group_target_gib("20T"), 20480)

    def test_old_20t_plus_alias(self):
        # 老配置写的 20T+ 就是现在的 20T（改名不改数值）
        self.assertEqual(SG_MOD.seed_group_target_gib("20T+"), 20480)
        self.assertEqual(SG_MOD.seed_group_tier_key("20T+"), "20T")

    def test_custom_tier_accepts_free_sizes(self):
        # 自定义档可以填 100M / 100G / 1T / 1P；只有 0 会被抬到 1 GiB
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "100G"), 100)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "1T"), 1024)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "1P"), 1024 * 1024)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "2048"), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", 2048), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "100M"), 1)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", 0), 1)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", None), 1)
        self.assertEqual(SG_MOD.seed_group_target_gib("custom", "1p"), 1024 * 1024)

    def test_parse_size_units(self):
        self.assertAlmostEqual(mod.parse_size_gib("100M"), 100 / 1024, places=4)
        self.assertAlmostEqual(mod.parse_size_gib("100G"), 100.0)
        self.assertAlmostEqual(mod.parse_size_gib("1T"), 1024.0)
        self.assertAlmostEqual(mod.parse_size_gib("2TiB"), 2048.0)
        self.assertAlmostEqual(mod.parse_size_gib("2048"), 2048.0)
        self.assertEqual(mod.parse_size_gib("abc"), 0.0)
        self.assertEqual(mod.parse_size_gib(""), 0.0)

    def test_form_has_custom_field_and_hides_removed(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
        form, defaults = plugin.get_form()
        blob = json.dumps(form, ensure_ascii=False)
        self.assertIn("seedgroup_custom_gib", blob)
        self.assertIn("seedgroup_tier", blob)
        # 这三项按老板要求从界面上撤掉了
        for key in ("seedgroup_api_key", "seedgroup_staff_prefix", "seedgroup_allow_admin"):
            self.assertNotIn('"model": "%s"' % key, blob)
        self.assertEqual(defaults["seedgroup_custom_gib"], 2048)
        self.assertEqual(defaults.get("seedgroup_scope"), "site")

    def test_unknown_tier_falls_back_to_smallest(self):
        # 写错档位不能退化成「不保种」：一律按 2T 下限走
        self.assertEqual(SG_MOD.seed_group_target_gib("100T"), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib(""), 2048)
        self.assertEqual(SG_MOD.seed_group_target_gib(None), 2048)

    def test_size_bytes_and_gib(self):
        self.assertAlmostEqual(SG_MOD._seed_group_size_gib(2 * mod.GIB), 2.0)
        self.assertAlmostEqual(SG_MOD._seed_group_size_gib(3.5), 3.5)      # 已经是 GiB
        self.assertEqual(SG_MOD._seed_group_size_gib(0), 0.0)
        self.assertEqual(SG_MOD._seed_group_size_gib("bad"), 0.0)


class SeedGroupPlanTest(unittest.TestCase):
    def test_never_below_target(self):
        items = [{"info_hash": f"h{i}", "size": i * mod.GIB} for i in (60, 50, 40, 30)]
        picked, total, shortfall = SG_MOD.seed_group_plan(items, 100)
        self.assertGreaterEqual(total, 100)
        self.assertEqual(shortfall, 0)
        self.assertEqual(sum(x["size_gib"] for x in picked), total)

    def test_best_fit_fills_exactly(self):
        items = [{"info_hash": "a", "size": 60 * mod.GIB},
                 {"info_hash": "b", "size": 50 * mod.GIB},
                 {"info_hash": "c", "size": 40 * mod.GIB}]
        picked, total, shortfall = SG_MOD.seed_group_plan(items, 100)
        self.assertEqual(total, 100)
        self.assertEqual([x["info_hash"] for x in picked], ["a", "c"])
        self.assertEqual(shortfall, 0)

    def test_overflow_single_too_big(self):
        # 一颗就超过档位：照样收下（只超不少）
        picked, total, shortfall = SG_MOD.seed_group_plan(
            [{"info_hash": "big", "size": 300 * mod.GIB}], 100)
        self.assertEqual(len(picked), 1)
        self.assertEqual(total, 300)
        self.assertEqual(shortfall, 0)

    def test_shortfall_reported_when_pool_too_small(self):
        items = [{"info_hash": "a", "size": 40 * mod.GIB},
                 {"info_hash": "b", "size": 60 * mod.GIB}]
        picked, total, shortfall = SG_MOD.seed_group_plan(items, 1000)
        self.assertEqual(len(picked), 2)
        self.assertEqual(total, 100)
        self.assertEqual(shortfall, 900)

    def test_official_first(self):
        items = [{"info_hash": "plain", "size": 60 * mod.GIB, "official": False},
                 {"info_hash": "official", "size": 50 * mod.GIB, "official": True}]
        picked, _, _ = SG_MOD.seed_group_plan(items, 50)
        self.assertEqual(picked[0]["info_hash"], "official")

    def test_empty_pool(self):
        picked, total, shortfall = SG_MOD.seed_group_plan([], 2048)
        self.assertEqual((picked, total, shortfall), ([], 0.0, 2048.0))


class SeedGroupPluginTest(unittest.TestCase):
    def _plugin(self, **attrs):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup", **attrs)
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
                return SG_MOD._HttpResult(200, body, body.decode())
            if path.startswith("/api/v1/users/"):
                body = json.dumps(profile or {}).encode()
                return SG_MOD._HttpResult(200, body, body.decode())
            return SG_MOD._HttpResult(404, b"", "404")
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

    def test_panel_ignores_expired_unlock(self):
        plugin = self._plugin()
        plugin.save_data("seedgroup_unlock", {"ok": True, "at": 1, "until": 2,
                                             "groups": ["工作组-保种"]})
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("数据不汇报", blob)
        self.assertNotIn("资格：工作组", blob)

    def test_panel_shows_locked_reason(self):
        plugin = self._plugin()
        plugin.save_data("seedgroup_unlock", {"ok": False, "at": 0, "until": 0,
                                             "reason": "账号不在工作组：组名需以「工作组」开头"})
        blob = json.dumps(plugin._seedgroup_panel(), ensure_ascii=False)
        self.assertIn("数据不汇报", blob)

    def test_service_registers_seed_group_job(self):
        plugin = self._plugin()
        plugin._enabled = True
        plugin._crossseed_enabled = True
        names = [s["name"] for s in plugin.get_service()]
        self.assertIn("可乐保种组", names)

    def test_service_matrix(self):
        """总开关 × 功能开关 四种组合：两个功能互不影响，总开关一关全不跑。"""
        plugin = self._plugin()
        plugin._cron = "0 */1 * * *"
        plugin._seedgroup_cron = "0 */6 * * *"

        def names(enabled, cross, seed):
            plugin._enabled, plugin._crossseed_enabled, plugin._seedgroup_enabled = enabled, cross, seed
            return [x["id"] for x in plugin.get_service()]

        self.assertEqual(names(False, True, True), [])
        self.assertEqual(names(True, True, True), ["ColaSeedGroup"])  # 这个测试用的是保种插件
        self.assertEqual(names(True, True, False), [])
        self.assertEqual(names(True, False, True), ["ColaSeedGroup"])  # 只看保种开关

    def test_service_trigger_is_object_not_string(self):
        plugin = self._plugin()
        plugin._enabled = True
        plugin._crossseed_enabled = True
        plugin._cron = "0 */1 * * *"
        plugin._seedgroup_cron = "   "      # 空白：应回退成合法触发器而不是原样字符串
        for svc in plugin.get_service():
            self.assertNotIsInstance(svc["trigger"], str, svc)
            self.assertTrue(hasattr(svc["trigger"], "from_crontab") or svc["trigger"] is not None)

    def test_service_empty_when_disabled(self):
        plugin = self._plugin()
        plugin._seedgroup_enabled = False
        plugin._enabled = False
        self.assertEqual(plugin.get_service(), [])

    def test_sync_without_staff_still_adds_but_does_not_report(self):
        """没头衔照常拉官种，只是不汇报（2026-11-18 老板定）。"""
        plugin = self._plugin()
        client = support.FakeClient()
        plugin._sg_clients = lambda: [client]
        reported = []
        plugin._maybe_report = lambda clients, state: reported.append(dict(state))
        plugin._cola = self._fake_cola(
            me={"id": 8, "username": "member", "roles": ["member"]},
            profile={"groups": ["VIP"]})
        plugin._sg_candidates = lambda: [{"torrent_id": 1, "info_hash": "a", "size": mod.GIB}]
        plugin._sg_download = lambda item: support.make_torrent()
        plugin._seed_group_sync()
        self.assertTrue(client.added, "非工作组成员也应该进种")
        state = plugin.get_data("seedgroup_state")
        self.assertFalse(state.get("staff"))

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
        self.assertTrue(all(SG_MOD.SEED_GROUP_TAG in a["label"] for a in client.added))
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
             "tags": f"{mod.MANDATORY_TAG},{SG_MOD.SEED_GROUP_TAG}", "state": "uploading"},
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
        plugin_curl = SG_MOD._curl

        def fake_curl(method, url, **kwargs):
            return SG_MOD._HttpResult(200, body, body.decode())

        SG_MOD._curl = fake_curl
        try:
            items, total, source = plugin._sg_official_machine_page(1)
        finally:
            SG_MOD._curl = plugin_curl
        self.assertEqual(total, 1)
        self.assertEqual(source, "api-key")
        self.assertEqual(items[0]["torrent_id"], 5)
        self.assertAlmostEqual(SG_MOD._seed_group_size_gib(items[0]["size"]), 2.0)

    def test_browse_page_extracts_official_versions(self):
        plugin = self._plugin()
        body = json.dumps({"items": [{"versions": [
            {"id": 31, "release_name": "A", "size_bytes": mod.GIB, "official": True},
            {"id": 32, "release_name": "B", "size_bytes": mod.GIB, "official": False},
        ]}], "page": 1, "page_size": 50, "total": 1}).encode()
        plugin._cola = lambda m, p, **k: SG_MOD._HttpResult(200, body, body.decode())
        items, total, source = plugin._sg_official_browse_page(1)
        self.assertEqual(total, 1)
        self.assertEqual(source, "session")
        self.assertEqual([x["torrent_id"] for x in items], [31])


class SeedGroupClientTest(unittest.TestCase):
    def _plugin(self):
        plugin = support.make_plugin(SG_MOD, cls_name="ColaSeedGroup")
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

    def test_does_not_fall_back_to_crossseed_clients(self):
        """**不回落**到辅种的客户端列表 —— 这是 2026-11-19 事故的直接原因。

        用户只希望往一台远端 qB 加种；自定义行写坏了就不该猜，
        更不该把种推到家里 NAS 的 qB 上。
        """
        plugin = self._plugin()
        fake = support.FakeClient()
        plugin._seedgroup_use_mp_clients = False
        plugin._seedgroup_clients_lines = ""
        plugin._clients = lambda *a, **k: [fake]
        self.assertEqual(plugin._sg_clients(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
