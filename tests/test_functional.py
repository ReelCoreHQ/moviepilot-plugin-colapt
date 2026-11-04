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


if __name__ == "__main__":
    unittest.main(verbosity=2)
