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












class SourceVariantTest(unittest.TestCase):
    """同一份文件在别站与可乐只有 info.source 不同（2026-11-02 凡人修仙传 实例）。"""


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
        self.assertIsNone(calls[0].get("json_body"), "createDownloadLink 不收请求体，发 {} 会被 400（#67）")
        self.assertIsNone(calls[0].get("data"))
        self.assertKeyOnly(calls[0])
        self.assertEqual(calls[1]["url"], link)
        self.assertNotIn("X-API-Key", calls[1].get("headers") or {}, "签名直链本身就是凭据")

    def test_download_refused_returns_empty(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        got, calls = self._run(plugin, [(404, {"error": {"code": "not_found"}})], plugin._download_cola, 1)
        self.assertEqual(got, b"")
        self.assertEqual(len(calls), 1)

    def test_connection_test_shows_username(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        got, calls = self._run(
            plugin,
            [(200, {"user": {"id": 7, "username": "可乐猫"}, "class_name": "黄金可乐"})],
            plugin._test_cola)
        self.assertIn("可乐猫", got)
        self.assertNotIn("黄金可乐", got, "括号里显示用户名，不是等级名")
        self.assertTrue(got.startswith("✅"))
        self.assertTrue(calls[0]["url"].endswith("/api/v1/me/stats"))
        self.assertKeyOnly(calls[0])

    def test_connection_test_without_username(self):
        plugin = support.make_plugin(mod, _api_key=self.KEY)
        got, _ = self._run(plugin, [(200, {"class_name": "黄金可乐"})], plugin._test_cola)
        self.assertIn("未知用户", got)

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

    def test_page_is_card_based_not_plain_text_dump(self):
        """主页改成卡片 + 表格，不再是一大段纯文本堆叠。"""
        plugin = support.make_plugin(mod, _api_key="k")
        plugin.get_data = lambda key, default=None: (
            {"last_run": {"at": "2026-11-23 14:06:00", "added": 4,
                          "duplicate": 0, "already": 424, "failed": 0},
             "test_report": {"schema": 3, "at": "2026-11-23 14:05", "cola": "✅ 有效",
                             "clients": ["✅ MP-保种：正常"]}}.get(key, default))
        plugin._clients = lambda: []
        page = plugin.get_page()
        text = json.dumps(page, ensure_ascii=False)
        self.assertIn("colacrossseed-summary", text, "顶部应有统计卡片网格")
        self.assertIn("colacrossseed-stat", text)
        self.assertIn("colacrossseed-table", text, "站点/检测应用表格呈现")
        self.assertIn("VTable", text)
        self.assertIn("mdi-clock-outline", text, "四张卡片应有图标")
        self.assertNotIn("插件版本 v0.14.3；已填可乐PT API Key", text, "旧的长串纯文本头已移除")



    def test_page_discards_stale_report_without_schema(self):
        """老版本遗留的 test_report（无 schema）必须被丢弃，不能再显示 IYUU JSON 等旧文案。"""
        plugin = support.make_plugin(mod, _api_key="k")
        plugin.get_data = lambda key, default=None: (
            {"last_run": {"at": "2026-11-23 15:12:00", "added": 0},
             "test_report": {"at": "2026-11-19 16:20:18",
                             "cola": "✅ 可乐PT：已登录（glaucus）",
                             "iyuu": "⚠️ IYUU Token：已填，但导入的站点没有 sid（请粘贴 IYUU 站点 JSON）",
                             "clients": [],
                             "sites": ["⚠️ 没有配置其他站（可粘贴 IYUU 配置或填「域名,passkey」）"]}}.get(key, default))
        plugin._clients = lambda: []
        page = plugin.get_page()
        text = json.dumps(page, ensure_ascii=False)
        self.assertNotIn("请粘贴 IYUU 站点 JSON", text, "旧报告的 JSON 提示不应再显示")
        self.assertNotIn("域名,passkey", text, "旧报告的手写站提示不应再显示")
        self.assertNotIn("2026-11-19 16:20:18", text, "旧报告时间戳不应再显示")
        self.assertIn("保存一次配置会自动做连通性检测", text, "应提示用户重新检测")

    def test_refresh_check_ignores_enable_switch(self):
        """保存配置后自动检测只要求有 Key，不受总开关限制——否则旧报告永远刷不掉。"""
        plugin = support.make_plugin(mod)
        started = []
        saved_thread = mod.threading.Thread if hasattr(mod, "threading") else None
        # init_plugin 里 import threading 是局部导入，直接让它真起线程但把 run_tests 换探针
        plugin.run_tests = lambda: started.append(1)
        import time as _time
        try:
            # 总开关关着，但有 Key → 仍触发检测
            plugin.init_plugin({"enabled": False, "crossseed_enabled": False, "api_key": "k"})
            deadline = _time.time() + 2
            while not started and _time.time() < deadline:
                _time.sleep(0.01)
            self.assertEqual(started, [1])
            # 没 Key → 不触发
            started.clear()
            plugin.init_plugin({"enabled": True, "crossseed_enabled": True, "api_key": ""})
            _time.sleep(0.05)
            self.assertEqual(started, [])
        finally:
            pass

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

    def test_form_has_mp_site_dropdown(self):
        """①区保留站点下拉，方便用户确认有没有选对；手填 Key 仍是兜底。"""
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn('"model": "mp_site_id"', text)
        self.assertIn('"model": "api_key"', text)
        self.assertIn("mp_site_id", defaults)

    def test_form_default_selects_detected_cola_site(self):
        """默认选中自动识别到的可乐站，用户看到不对可以自己改。"""
        plugin = support.make_plugin(mod, _mp_site_id="9")
        form, defaults = plugin.get_form()
        self.assertEqual(defaults["mp_site_id"], "9")
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn("默认选中自动识别到的可乐站", text)

    def test_form_downloader_is_plain_select_not_collapsed(self):
        """下载器是直接可见的必选项，不是折叠的「高级设置」。"""
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn('"model": "mp_target_clients"', text)
        self.assertNotIn('"component": "VExpansionPanels"', text)
        self.assertNotIn("高级：指定辅种目标下载器", text)
        self.assertEqual(defaults["mp_target_clients"], [])


    def test_default_cron_is_daily_midnight(self):
        """默认执行频率改为每日 00:00（0 0 * * *），表单默认值也要一致。"""
        self.assertEqual(mod.ColaCrossSeed._cron, "0 0 * * *")
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        self.assertEqual(defaults["cron"], "0 0 * * *")
        self.assertIn("每日 00:00", json.dumps(form, ensure_ascii=False))
        # 空配置初始化也应回落到每日 0 点
        plugin.stop_service = lambda: None
        plugin.init_plugin({"enabled": False})
        self.assertEqual(plugin._cron, "0 0 * * *")




class MpApiKeyTest(unittest.TestCase):
    """API Key 从 MP 站点管理读取：只存站点 id，apikey 每次现读，手填作兜底。"""

    KEY = "rcapi_from_site_management"

    class FakeSite:
        def __init__(self, sid, name, domain, apikey=""):
            self.id, self.name, self.domain, self.apikey = sid, name, domain, apikey

    def _install_mp_modules(self, sites, indexers=None):
        """往 sys.modules 注入假 MP 模块，返回恢复函数。"""
        outer = self

        class FakeSiteOper:
            def list_order_by_pri(self):
                return sites

        class FakeSitesHelper:
            def get_indexer(self, domain):
                return (indexers or {}).get(domain, {})

        injected = {
            "app.db": {"__path__": []},
            "app.db.site_oper": {"SiteOper": FakeSiteOper},
            "app.helper": {"__path__": []},
            "app.helper.sites": {"SitesHelper": FakeSitesHelper},
        }
        saved = {}
        for name, attrs in injected.items():
            saved[name] = (name in sys.modules, sys.modules.get(name))
            stub = sys.modules.get(name) or type(sys)(name)
            for key, value in attrs.items():
                setattr(stub, key, value)
            sys.modules[name] = stub

        def restore():
            for name, (existed, module) in saved.items():
                if existed:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)
        return restore

    def test_reads_apikey_from_indexer(self):
        """首选 SitesHelper().get_indexer(domain)['apikey']。"""
        sites = [self.FakeSite(1, "可乐PT", "www.colapt.com", apikey="rcapi_on_site_object")]
        restore = self._install_mp_modules(
            sites, {"www.colapt.com": {"apikey": self.KEY}})
        try:
            plugin = support.make_plugin(mod, _mp_site_id="1", _api_key="rcapi_manual_fallback")
            self.assertEqual(plugin._api_key_from_mp_site(), self.KEY)
        finally:
            restore()

    def test_falls_back_to_site_object_apikey(self):
        """适配器没给 apikey 时，退回站点对象的 apikey 字段。"""
        sites = [self.FakeSite(2, "可乐PT", "www.colapt.com", apikey=self.KEY)]
        restore = self._install_mp_modules(sites, {"www.colapt.com": {}})
        try:
            plugin = support.make_plugin(mod, _mp_site_id="2")
            self.assertEqual(plugin._api_key_from_mp_site(), self.KEY)
        finally:
            restore()

    def test_missing_site_or_key_returns_empty(self):
        """站点 id 不存在、或站点没配 Key，都返回空串（不伪造）。"""
        sites = [self.FakeSite(3, "别的站", "other.example.com")]
        restore = self._install_mp_modules(sites, {})
        try:
            plugin = support.make_plugin(mod, _mp_site_id="999")   # 不存在的 id
            self.assertEqual(plugin._api_key_from_mp_site(), "")
            plugin._mp_site_id = "3"                              # 存在但没 Key
            self.assertEqual(plugin._api_key_from_mp_site(), "")
        finally:
            restore()

    def test_init_prefers_mp_key_over_manual(self):
        """init_plugin：站点 Key 优先于手填；读不到时退回手填。"""
        sites = [self.FakeSite(4, "可乐PT", "www.colapt.com", apikey="")]
        restore = self._install_mp_modules(
            sites, {"www.colapt.com": {"apikey": self.KEY}})
        try:
            plugin = support.make_plugin(mod)
            plugin.stop_service = lambda: None
            plugin.init_plugin({"enabled": False, "api_key": "rcapi_manual", "mp_site_id": "4"})
            self.assertEqual(plugin._api_key, self.KEY)
            self.assertEqual(plugin._mp_site_id, "4")
        finally:
            restore()

    def test_init_falls_back_to_manual_when_mp_key_missing(self):
        restore = self._install_mp_modules([], {})
        try:
            plugin = support.make_plugin(mod)
            plugin.stop_service = lambda: None
            plugin.init_plugin({"enabled": False, "api_key": "rcapi_manual", "mp_site_id": "nope"})
            self.assertEqual(plugin._api_key, "rcapi_manual")
        finally:
            restore()

    def test_site_options_only_store_id(self):
        """下拉选项 title 用站点名、value 用 str 化的 id（与配置里的 str 类型一致）。"""
        sites = [self.FakeSite(5, "可乐PT", "www.colapt.com")]
        restore = self._install_mp_modules(sites, {})
        try:
            plugin = support.make_plugin(mod)
            options = plugin._mp_site_options()
            self.assertEqual(len(options), 1)
            self.assertEqual(options[0]["value"], "5")
            self.assertIsInstance(options[0]["value"], str,
                                  "value 必须是 str，否则配置里的 str id 匹配不上，回显成数字")
            self.assertEqual(options[0]["title"], "可乐PT")
        finally:
            restore()

    def test_site_option_value_matches_stored_str_id(self):
        """回归：MP 的 site.id 是 int，配置项是 str；VSelect 严格匹配，value 须统一 str。"""
        sites = [self.FakeSite(61, "可乐PT", "www.colapt.com")]
        restore = self._install_mp_modules(sites, {})
        try:
            plugin = support.make_plugin(mod)
            options = plugin._mp_site_options()
            stored = "61"  # init_plugin 里 str(config.get("mp_site_id")) 的结果
            matched = [o for o in options if o["value"] == stored]
            self.assertEqual(len(matched), 1, "存进去的 str id 应能匹配到一个选项，否则回显成裸数字 61")
            self.assertEqual(matched[0]["title"], "可乐PT")
        finally:
            restore()





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

    def test_category_is_separate_from_tag(self):
        """issue #23：分类走独立字段，绝不能混进用户填的辅种标签。"""
        module = support.FakeMPModule()
        client = self._client(module)
        self.assertEqual(client.add(b"t", "/downloads/剧流", True, "NovaHD,辅种",
                                    category="剧流"), "ok")
        call = module.calls[0]
        self.assertEqual(call["category"], "剧流")
        self.assertEqual(call["tag"], "NovaHD,辅种")
        self.assertEqual(call["download_dir"], "/downloads/剧流")

    def test_empty_category_not_sent(self):
        module = support.FakeMPModule()
        self._client(module).add(b"t", "/downloads", False, category="")
        self.assertNotIn("category", module.calls[0])


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
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-tr辅种"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_mp_prefix_is_optional(self):
        for wanted in ("tr辅种", "MP-tr辅种", "TR辅种"):
            plugin = support.make_plugin(mod, _mp_target_clients=[wanted])
            got = plugin._apply_target_client(self._clients())
            self.assertEqual([c.name for c in got], ["MP-tr辅种"], wanted)

    def test_short_prefix_contains(self):
        """用户常只写 `tr`，实例叫 `tr辅种`：包含匹配。"""
        plugin = support.make_plugin(mod, _mp_target_clients=["tr"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_unknown_target_adds_nothing(self):
        """填错名字时**一轮都不加**，绝不回落把种子推到没点名的机器上。"""
        plugin = support.make_plugin(mod, _mp_target_clients=["不存在的机器"])
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertFalse(plugin._target_state.get("ok"))
        self.assertEqual(plugin._target_state.get("available"),
                         ["MP-qb下载", "MP-tr辅种"])

    def test_blank_target_keeps_all(self):
        plugin = support.make_plugin(mod, _mp_target_clients=[""])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-qb下载", "MP-tr辅种"])

    def test_preferred_inside_target_only(self):
        """收窄之后自动判断只在目标那台里挑，不会把别的下载器写在 owner 里。"""
        plugin = support.make_plugin(mod, _mp_target_clients=["tr辅种"])
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
        plugin = support.make_plugin(mod, _mp_target_clients=["tr"])
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertFalse(plugin._target_state.get("ok"))
        self.assertEqual(sorted(plugin._target_state.get("ambiguous") or []),
                         ["MP-tr备用", "MP-tr辅种"])

    def test_full_name_resolves_even_with_sibling(self):
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-tr备用"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr备用"])

    def test_single_char_never_fuzzy(self):
        plugin = support.make_plugin(mod, _mp_target_clients=["t"])
        self.assertEqual(plugin._apply_target_client(self._clients()), [])

    def test_quoted_and_spaced_input(self):
        plugin = support.make_plugin(mod, _mp_target_clients=['  "MP-tr辅种"  '])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_pending_for_other_client_is_kept(self):
        """收窄客户端后，别的机器的待重试记录要留着，不能静默丢。"""
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-tr备用"])
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
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-tr辅种"])
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertFalse(plugin._target_state.get("ok"))

    def test_alias_stage_ambiguity(self):
        plugin = support.make_plugin(mod, _mp_target_clients=["tr辅种"])
        self.assertEqual(plugin._apply_target_client(self._clients()), [])
        self.assertTrue(plugin._target_state.get("ambiguous"))


class TargetMultiSelectTest(unittest.TestCase):
    """辅种目标：只从 MP 已配置下载器里多选，不再支持手填自定义实例。"""

    def _clients(self):
        qb = support.FakeClient(); qb.name = "MP-qb下载"
        tr = support.FakeClient(); tr.name = "MP-tr辅种"
        return [qb, tr]

    def test_multi_select_resolves_all(self):
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-qb下载", "MP-tr辅种"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-qb下载", "MP-tr辅种"])
        self.assertTrue(plugin._target_state.get("ok"))

    def test_dedupe_inside_multiselect(self):
        """多选里填了同一台的别名只加一次。"""
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-tr辅种", "tr辅种"])
        self.assertEqual(plugin._target_wants(), ["MP-tr辅种", "tr辅种"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_all_blank_keeps_auto(self):
        plugin = support.make_plugin(mod, _mp_target_clients=[])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-qb下载", "MP-tr辅种"])

    def test_blank_entry_is_ignored(self):
        plugin = support.make_plugin(mod, _mp_target_clients=[""])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-qb下载", "MP-tr辅种"])

    def test_one_bad_target_stops_all(self):
        """任何一个目标不存在就整轮不加，绝不推到没点名的机器上。"""
        plugin = support.make_plugin(mod, _mp_target_clients=["MP-qb下载", "不存在的机器"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual(got, [])
        self.assertFalse(plugin._target_state.get("ok"))
        self.assertTrue(plugin._target_state.get("problems"))

    def test_short_name_still_works(self):
        plugin = support.make_plugin(mod, _mp_target_clients=["tr"])
        got = plugin._apply_target_client(self._clients())
        self.assertEqual([c.name for c in got], ["MP-tr辅种"])

    def test_form_has_multiselect_only(self):
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn('"model": "mp_target_clients"', text)
        self.assertNotIn('"model": "target_client"', text)
        self.assertNotIn('"model": "clients_lines"', text)
        self.assertIn('"multiple": true', text)
        self.assertEqual(defaults["mp_target_clients"], [])

    def test_mp_downloader_options(self):
        """下拉项从 DownloaderHelper().get_services() 来，value 与 _mp_clients 的 label 一致。"""
        outer = self

        class FakeInfo:
            type = "qbittorrent"

        class FakeHelper:
            def get_services(self):
                return {"qb下载": FakeInfo(), "tr辅种": FakeInfo()}

        saved = {}
        for name, attrs in (("app.application", {"__path__": []}),
                            ("app.application.downloader", {"DownloaderHelper": FakeHelper})):
            saved[name] = (name in sys.modules, sys.modules.get(name))
            stub = sys.modules.get(name) or type(sys)(name)
            for key, value in attrs.items():
                setattr(stub, key, value)
            sys.modules[name] = stub
        try:
            plugin = support.make_plugin(mod)
            options = plugin._mp_downloader_options()
            self.assertEqual(options, [
                {"title": "MP-qb下载（qbittorrent）", "value": "MP-qb下载"},
                {"title": "MP-tr辅种（qbittorrent）", "value": "MP-tr辅种"},
            ])
        finally:
            for name, (existed, module) in saved.items():
                if existed:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)


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
        stats = {"matched": 0, "added": 0, "skipped": 0, "failed": 0,
                 "duplicate": 0, "retried": 0, "already": 0}
        plugin._place(client, b"torrent", {"save_path": "/downloads"}, "key", stats,
                      "added")
        self.assertEqual(stats["added"], 1)
        self.assertEqual(len(client.added), 1)
        self.assertEqual(len(plugin._add_times()), 1)









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






class MpSiteTest(unittest.TestCase):
    """③站点来源：MP 站点下拉（存 id）、凭据现读、IYUU sid 反查（官方模式）。"""

    class FakeSite:
        def __init__(self, sid, name, domain, rss="", apikey="", cookie=""):
            self.id, self.name, self.domain = sid, name, domain
            self.rss, self.apikey, self.cookie = rss, apikey, cookie

    def _install_mp_modules(self, sites, indexers=None):
        """注入假 SiteOper / SitesHelper，返回恢复函数。"""
        outer = self

        class FakeSiteOper:
            def list_order_by_pri(self):
                return sites

        class FakeSitesHelper:
            def get_indexer(self, domain):
                return (indexers or {}).get(domain, {})

        injected = {
            "app.db": {"__path__": []},
            "app.db.site_oper": {"SiteOper": FakeSiteOper},
            "app.helper": {"__path__": []},
            "app.helper.sites": {"SitesHelper": FakeSitesHelper},
        }
        saved = {}
        for name, attrs in injected.items():
            saved[name] = (name in sys.modules, sys.modules.get(name))
            stub = sys.modules.get(name) or type(sys)(name)
            for key, value in attrs.items():
                setattr(stub, key, value)
            sys.modules[name] = stub

        def restore():
            for name, (existed, module) in saved.items():
                if existed:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)
        return restore

    def test_options_title_is_site_name_value_is_id(self):
        sites = [self.FakeSite(11, "观众", "audiences.me"), self.FakeSite(12, "馒头", "pt.keepfrds.com")]
        restore = self._install_mp_modules(sites)
        try:
            plugin = support.make_plugin(mod)
            self.assertEqual(plugin._mp_site_options(), [
                {"title": "观众", "value": "11"}, {"title": "馒头", "value": "12"}])
        finally:
            restore()








class RunOnceTest(unittest.TestCase):
    """参考 iyuuautoseed 的「立即运行一次」：勾一次跑一轮，跑完自动复位。"""

    def test_form_has_once_switch(self):
        plugin = support.make_plugin(mod)
        form, defaults = plugin.get_form()
        text = json.dumps(form, ensure_ascii=False)
        self.assertIn('"model": "onlyonce"', text)
        self.assertIn("立即运行一次", text)
        self.assertIs(defaults["onlyonce"], False)

    def test_init_resets_once_and_reads_limits(self):
        plugin = support.make_plugin(mod)
        plugin.init_plugin({
            "enabled": False,
            "onlyonce": True,
            "max_per_hour": 7,
        })
        self.assertFalse(plugin._onlyonce)
        self.assertFalse(plugin._store["__config__"]["onlyonce"])
        self.assertEqual(plugin._max_per_hour, 7)

    def test_run_once_calls_auto_seed_forced(self):
        """立即运行一次必须 force=True，才能无视启用总开关。"""
        plugin = support.make_plugin(mod)
        calls = []
        plugin.auto_seed = lambda force=False: calls.append(force)
        saved = mod.time.sleep
        mod.time.sleep = lambda _: None
        try:
            plugin._run_once()
        finally:
            mod.time.sleep = saved
        self.assertEqual(calls, [True])

    def test_onlyonce_ignores_enable_switch_but_needs_api_key(self):
        """官方语义：总开关关着也能钩一次跑一轮；只有 API Key 是硬前提。"""
        plugin = support.make_plugin(mod)
        reached = []
        import time as _time
        # _run_once 里本来 sleep(3) 再跑；这里直接把 _run_once 换成探针，避免真等 3 秒/联网
        saved_run_once = getattr(plugin, "_run_once", None)
        plugin._run_once = lambda: reached.append("once")
        try:
            # 关掉所有开关，但有 API Key → 仍然触发（无视 enabled/crossseed）
            plugin.init_plugin({"onlyonce": True, "api_key": "k"})
            deadline = _time.time() + 2
            while not reached and _time.time() < deadline:
                _time.sleep(0.01)
            self.assertEqual(reached, ["once"])
            self.assertFalse(plugin._onlyonce)  # 跑完自动复位

            # 没有 API Key → 不触发
            reached.clear()
            plugin.init_plugin({"onlyonce": True, "api_key": ""})
            _time.sleep(0.05)
            self.assertEqual(reached, [])
        finally:
            if saved_run_once is not None:
                plugin._run_once = saved_run_once

    def test_auto_seed_force_bypasses_enable_check(self):
        """force=True 时即使总开关关着，也不在开头那道 enable 检查处返回。"""
        plugin = support.make_plugin(mod)
        plugin._enabled = False
        plugin._crossseed_enabled = False
        plugin._api_key = "k"
        plugin._event.clear()
        reached = []
        plugin._apply_target_client = lambda clients: clients
        plugin.save_data = lambda *a, **k: None
        # 用抛异常的 _clients 当探针：调到它就说明穿过了 enable 检查。
        # 直接调 _auto_seed（不走 auto_seed 的 try/except，异常才会冒出来）。
        def boom():
            reached.append(1)
            raise RuntimeError("probe")
        plugin._clients = boom
        with self.assertRaises(RuntimeError):
            plugin._auto_seed(force=True)
        self.assertEqual(reached, [1])

        # force=False 时在 enable 检查处直接返回，连 _clients 都不碰
        reached.clear()
        plugin._auto_seed(force=False)
        self.assertEqual(reached, [])


class ImprovementsTest(unittest.TestCase):
    """易用性/稳定性改进：站点自动识别、Key 冷却、单轮互斥、明细、目标校验、限流。"""

    class FakeSite:
        def __init__(self, sid, name, domain, apikey=""):
            self.id, self.name, self.domain, self.apikey = sid, name, domain, apikey

    def _install(self, sites, indexers=None, downloader_names=()):
        class FakeSiteOper:
            def list_order_by_pri(self):
                return sites

        class FakeSitesHelper:
            def get_indexer(self, domain):
                return (indexers or {}).get(domain, {})

        class FakeInfo:
            type = "qbittorrent"

        class FakeDownloaderHelper:
            def get_services(self):
                return {n: FakeInfo() for n in downloader_names}

        injected = {
            "app.db": {"__path__": []},
            "app.db.site_oper": {"SiteOper": FakeSiteOper},
            "app.helper": {"__path__": []},
            "app.helper.sites": {"SitesHelper": FakeSitesHelper},
            "app.application": {"__path__": []},
            "app.application.downloader": {"DownloaderHelper": FakeDownloaderHelper},
        }
        saved = {}
        for name, attrs in injected.items():
            saved[name] = (name in sys.modules, sys.modules.get(name))
            stub = sys.modules.get(name) or type(sys)(name)
            for key, value in attrs.items():
                setattr(stub, key, value)
            sys.modules[name] = stub

        def restore():
            for name, (existed, module) in saved.items():
                if existed:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)
        return restore

    def test_auto_finds_cola_site_and_key(self):
        sites = [self.FakeSite(3, "别的站", "other.example.com"),
                 self.FakeSite(9, "可乐PT", "www.colapt.com", apikey="rcapi_on_site")]
        restore = self._install(sites, {"www.colapt.com": {"apikey": "rcapi_from_indexer"}})
        try:
            plugin = support.make_plugin(mod)
            self.assertEqual(plugin._find_cola_site(), ("9", "可乐PT", "rcapi_from_indexer"))
        finally:
            restore()

    def test_init_auto_fills_site_without_dropdown(self):
        sites = [self.FakeSite(9, "可乐PT", "www.colapt.com", apikey="rcapi_site")]
        restore = self._install(sites, {})
        try:
            plugin = support.make_plugin(mod)
            plugin.stop_service = lambda: None
            plugin.init_plugin({"enabled": False})
            self.assertEqual(plugin._mp_site_id, "9")
            self.assertEqual(plugin._api_key, "rcapi_site")
            self.assertEqual(plugin._cola_site_name, "可乐PT")
        finally:
            restore()

    def test_key_401_starts_cooldown(self):
        plugin = support.make_plugin(mod, _api_key="bad")
        queue = [(401, {"error": {"code": "authentication_required"}})]
        original = mod._curl

        def fake_curl(method, url, **kwargs):
            status, body = queue.pop(0)
            text = json.dumps(body, ensure_ascii=False)
            return mod._HttpResult(status, text.encode(), text)

        mod._curl = fake_curl
        try:
            plugin._test_cola()
        finally:
            mod._curl = original
        self.assertGreater(plugin._key_cooldown_until, mod.time.time())
        self.assertGreater(plugin._store.get("key_cooldown_until", 0), 0)

    def test_auto_seed_respects_cooldown_but_force_bypasses(self):
        plugin = support.make_plugin(mod, _api_key="k")
        plugin._key_cooldown_until = mod.time.time() + 100
        calls = []
        plugin._auto_seed = lambda force=False: calls.append(force)
        plugin.auto_seed(force=False)
        self.assertEqual(calls, [])
        plugin.auto_seed(force=True)
        self.assertEqual(calls, [True])

    def test_successful_test_clears_cooldown(self):
        plugin = support.make_plugin(mod, _api_key="k")
        plugin._key_cooldown_until = mod.time.time() + 100
        queue = [(200, {"user": {"username": "可乐猫"}})]
        original = mod._curl

        def fake_curl(method, url, **kwargs):
            status, body = queue.pop(0)
            text = json.dumps(body, ensure_ascii=False)
            return mod._HttpResult(status, text.encode(), text)

        mod._curl = fake_curl
        try:
            plugin._test_cola()
        finally:
            mod._curl = original
        self.assertEqual(plugin._key_cooldown_until, 0)
        self.assertEqual(plugin._store.get("key_cooldown_until"), 0)

    def test_run_lock_prevents_overlap(self):
        plugin = support.make_plugin(mod, _api_key="k")
        calls = []
        plugin._auto_seed = lambda force=False: calls.append(force)
        self.assertTrue(mod.ColaCrossSeed._run_lock.acquire(blocking=False))
        try:
            plugin.auto_seed(force=True)
        finally:
            mod.ColaCrossSeed._run_lock.release()
        self.assertEqual(calls, [], "上一轮还在跑时必须跳过，不能并发加种")

    def test_details_recorded_and_persisted(self):
        plugin = support.make_plugin(mod)
        client = support.FakeClient(add_result="ok")
        plugin._run_details = []
        stats = {"matched": 1, "added": 0, "filled": 0, "skipped": 0,
                 "failed": 0, "duplicate": 0, "retried": 0, "already": 0}
        torrent = {"hash": "ab" * 20, "name": "剧集", "save_path": "/d"}
        plugin._place(client, b"t", torrent, "k", stats, "added", "ab" * 20)
        self.assertEqual(len(plugin._run_details), 1)
        self.assertEqual(plugin._run_details[0]["result"], "已补种")
        plugin._finish(stats)
        self.assertEqual(len(plugin._store.get("last_details", [])), 1)

    def test_save_validates_target_downloader(self):
        restore = self._install([], {}, downloader_names=("qb下载",))
        try:
            plugin = support.make_plugin(mod)
            plugin.stop_service = lambda: None
            plugin.init_plugin({"enabled": False, "mp_target_clients": ["不存在的机器"]})
            state = plugin._store.get("target_state", {})
            self.assertFalse(state.get("ok"))
            self.assertTrue(state.get("problems"))
            self.assertIn("MP-qb下载", state.get("available", []))
        finally:
            restore()

    def test_page_shows_details_and_target_warning(self):
        store = {
            "last_run": {"at": "2026-11-28 16:00:00", "added": 1},
            "last_details": [{"at": "16:00:00", "client": "MP-qb下载", "name": "剧集",
                              "result": "已补种", "note": ""}],
            "target_state": {"ok": False, "problems": ["目标下载器「X」不存在"]},
        }
        plugin = support.make_plugin(mod, store=store, _api_key="k")
        text = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertIn("本轮明细", text)
        self.assertIn("已补种", text)
        self.assertIn("辅种目标下载器不可用", text)

    def test_rate_limiter_waits_for_window(self):
        limiter = mod._RateLimiter(2, 0.12)
        start = mod.time.time()
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        self.assertGreaterEqual(mod.time.time() - start, 0.10)

    def test_curl_retries_twice_on_429(self):
        results = [mod._HttpResult(429, b"", "{}"), mod._HttpResult(429, b"", "{}"),
                   mod._HttpResult(200, b"", "{}")]
        calls = []
        saved_run, saved_sleep, saved_acquire = mod._run_curl, mod.time.sleep, mod._COLA_LIMITER.acquire
        try:
            mod._run_curl = lambda cmd: (calls.append(1), results.pop(0))[1]
            mod.time.sleep = lambda _: None
            mod._COLA_LIMITER.acquire = lambda: None
            out = mod._curl("GET", f"{mod.COLA_BASE}{mod.COLA_STATS_API}")
            self.assertEqual(out.status_code, 200)
            self.assertEqual(len(calls), 3)
        finally:
            mod._run_curl = saved_run
            mod.time.sleep = saved_sleep
            mod._COLA_LIMITER.acquire = saved_acquire


class UpgradeCompatibilityTest(unittest.TestCase):
    """从 0.13.x 升级：老配置/老落盘数据可读、不报错；删掉的功能静默忽略。"""

    OLD_CONFIG = {
        "enabled": True, "crossseed_enabled": True, "onlyonce": False, "notify": True,
        "cron": "0 */1 * * *",              # 老默认，保留用户自己的选择
        "api_key": "rcapi_old_manual", "mp_site_id": 61,   # 老版本可能存 int
        "direction": "other_site", "iyuu_token": "iyuu_secret",
        "mp_site_ids": ["1", "2"], "max_per_site": 20,
        "use_mp_clients": True, "clients_json": '[{"name":"NAS"}]',
        "clients_lines": "192.168.31.100:8081,user,pwd", "target_client": "NAS-本地",
        "mp_target_clients": ["MP-qb下载"],
        "skip_check": True, "max_per_hour": 30, "add_label": "old-label",
        "extra_tags": "oldtag", "inherit_tags": False, "fallback_skip_check": False,
        "retry_failed": True, "limit_dl_kib": 1024, "limit_up_kib": 2048,
        "limit_lines": "NAS-本地,100,200",
    }

    def _store(self):
        return {
            # 老报告没有 schema（或 schema=2），必须被丢弃
            "test_report": {"at": "2026-11-20 10:00:00",
                            "cola": "❌ 请粘贴 IYUU 站点 JSON",
                            "clients": ["✅ MP-qb下载：连得上"],
                            "iyuu": ["✅ IYUU Token 可用"], "sites": [{"name": "观众"}]},
            "last_run": {"at": "2026-11-20 10:00:00", "added": 2, "filled": 1,
                         "site_counts": {"观众": 1}},
            "done": ["MP-qb下载:cola:7"],
            "pending": [{"key": "k1", "client": "MP-qb下载", "data": "dGVzdA==",
                         "save_path": "/d", "site_key": "观众"}],
            "add_times": [mod.time.time() - 10, "bad"],
            "tag_memory": {"ab" * 40: "oldtag"},
            "dead_hashes": ["cd" * 40],
        }

    def _old_plugin(self, store=None, config=None):
        plugin = support.make_plugin(mod, store=self._store() if store is None else store)
        plugin.stop_service = lambda: None
        plugin.run_tests = lambda *a, **k: None   # 别让 init 里的检测线程真发请求
        plugin.init_plugin(dict(self.OLD_CONFIG) if config is None else config)
        return plugin

    def test_old_config_loads_without_error(self):
        plugin = self._old_plugin()
        self.assertEqual(plugin._api_key, "rcapi_old_manual")
        self.assertEqual(plugin._mp_site_id, "61")       # int → str
        self.assertEqual(plugin._mp_target_clients, ["MP-qb下载"])
        self.assertEqual(plugin._cron, "0 */1 * * *")    # 用户自定义周期保留
        self.assertEqual(plugin._max_per_hour, 30)
        self.assertFalse(plugin._inherit_tags)
        self.assertEqual(plugin._limit_lines, "NAS-本地,100,200")
        for gone in ("_iyuu_token", "_direction", "_max_per_site", "_target_client"):
            self.assertFalse(hasattr(plugin, gone))

    def test_persist_drops_removed_fields(self):
        plugin = self._old_plugin()
        plugin._persist()   # 真实用户下次点保存就会走这里
        cfg = plugin._store["__config__"]
        for gone in ("direction", "iyuu_token", "mp_site_ids", "max_per_site",
                     "use_mp_clients", "clients_json", "clients_lines", "target_client"):
            self.assertNotIn(gone, cfg)
        self.assertIn("mp_target_clients", cfg)

    def test_old_persistence_still_readable(self):
        plugin = self._old_plugin()
        self.assertEqual(len(plugin._pending()), 1)
        self.assertEqual(plugin._done(), ["MP-qb下载:cola:7"])
        self.assertEqual(len(plugin._add_times()), 1)    # 非数字的坏值被过滤
        self.assertEqual(plugin._dead_hashes(), ["cd" * 40])
        self.assertEqual(plugin._tag_memory(), {"ab" * 40: "oldtag"})

    def test_old_report_is_discarded_not_displayed(self):
        plugin = self._old_plugin()
        text = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertNotIn("IYUU", text)
        self.assertNotIn("黄金可乐", text)
        self.assertIn("colacrossseed-page", text)        # 页面本身正常生成

    def test_old_schema2_report_is_also_discarded(self):
        store = self._store()
        store["test_report"]["schema"] = 2
        plugin = self._old_plugin(store=store)
        text = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertNotIn("IYUU", text)

    def test_manual_key_survives_when_no_cola_site_in_mp(self):
        plugin = self._old_plugin(config={"enabled": True, "api_key": "rcapi_manual"})
        self.assertEqual(plugin._api_key, "rcapi_manual")
        self.assertEqual(plugin._mp_site_id, "")

    def test_removed_target_client_falls_back_to_auto(self):
        plugin = self._old_plugin(config={"enabled": True, "api_key": "k",
                                          "target_client": "NAS-本地"})
        self.assertEqual(plugin._mp_target_clients, [])
        self.assertEqual(plugin._target_wants(), [])     # 升级后没勾选任何下载器，不报错
        self.assertIn("自定义下载器", plugin._store.get("upgrade_notice", ""))
        page_text = json.dumps(plugin.get_page(), ensure_ascii=False)
        self.assertIn("没勾选任何下载器", page_text)
        self.assertIn("辅种目标", page_text)
        self.assertNotIn("升级说明", page_text)          # 不再单独弹一段，就在运行状态里

    def test_upgrade_notice_cleared_on_next_save(self):
        plugin = self._old_plugin(config={"enabled": True, "api_key": "k",
                                          "target_client": "NAS-本地"})
        self.assertTrue(plugin._store.get("upgrade_notice"))
        plugin.init_plugin({"enabled": True, "api_key": "k"})   # 新配置已无旧键
        self.assertEqual(plugin._store.get("upgrade_notice"), "")
