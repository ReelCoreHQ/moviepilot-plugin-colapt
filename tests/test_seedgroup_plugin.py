"""拆出来的「可乐保种组」插件：结构与身份。"""
import unittest

import support


class SplitPluginTest(unittest.TestCase):
    def test_loads_with_expected_identity(self):
        mod = support.load_seedgroup_plugin()
        cls = mod.ColaSeedGroup
        self.assertEqual(cls.plugin_name, "可乐保种组")
        self.assertEqual(cls.plugin_version, "0.2.3")

    def test_keeps_seed_group_and_drops_crossseed(self):
        mod = support.load_seedgroup_plugin()
        cls = mod.ColaSeedGroup
        for name in ("seed_group_sync", "_seed_group_sync", "_sg_clients", "_maybe_report"):
            self.assertTrue(hasattr(cls, name), name)
        for name in ("auto_seed", "_auto_seed", "_match", "_iyuu_reseed"):
            self.assertFalse(hasattr(cls, name), name)

    def test_registers_only_the_seedgroup_job(self):
        mod = support.load_seedgroup_plugin()
        plugin = support.make_plugin(mod, cls_name="ColaSeedGroup")
        plugin._enabled = True
        plugin._seedgroup_enabled = True
        plugin._seedgroup_cron = "0 * * * *"
        plugin._session_token = "tok"
        services = plugin.get_service()
        self.assertEqual([s["id"] for s in services], ["ColaSeedGroup"])

    def test_api_exposes_no_seeding_routes(self):
        mod = support.load_seedgroup_plugin()
        plugin = support.make_plugin(mod, cls_name="ColaSeedGroup")
        paths = [r["path"] for r in plugin.get_api()]
        self.assertNotIn("/run", paths)
        self.assertNotIn("/sites/import", paths)
        self.assertIn("/seedgroup/sync", paths)


if __name__ == "__main__":
    unittest.main()
