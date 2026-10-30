"""Synthetic Jev credential binding tests; no CC Switch or network access."""
import os
import tempfile
import unittest

from jev_hub import JevCredentials, load_jev
from storage import ProfileStore


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class JevHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="provider-jev-test-")
        self.store = ProfileStore(self.temp.name)
        self.profile = self.store.save("Jev", "system_one", "https://example.invalid/v1/systemone",
                                       "fixture-jev", "synthetic-jev-key")
        self.credentials = JevCredentials(self.store, self.profile["id"])

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_is_bound_to_the_same_endpoint_and_model(self):
        settings = self.credentials.read_settings()
        self.assertEqual(settings["endpoint"], "https://example.invalid/v1/systemone")
        self.assertEqual(self.credentials.api_key(settings["provider_name"]), "synthetic-jev-key")
        self.assertNotIn("synthetic-jev-key", str(settings))
        self.store.save("Jev", "system_one", "https://other.invalid/v1/systemone", "fixture-jev",
                        "other-synthetic-key", self.profile["id"])
        with self.assertRaisesRegex(ValueError, "连接已更新"):
            self.credentials.api_key(settings["provider_name"])

    def test_existing_six_tools_use_the_new_credentials(self):
        module = load_jev(self.credentials)
        self.assertIs(getattr(module.read_settings, "__self__", None), self.credentials)
        self.assertIs(getattr(module.api_key, "__self__", None), self.credentials)
        names = set(module.server._tool_manager._tools)
        self.assertTrue({"jev_decide", "jev_route_advice", "jev_screen_frames", "jev_select_evidence",
                         "jev_rank_transcripts", "jev_usage_status"} <= names)

    def test_dynamic_selection_switches_without_restarting_the_jev_server(self):
        self.store.set_jev_profile(self.profile["id"])
        active = JevCredentials(self.store)
        first = active.read_settings()
        other = self.store.save("Jev backup", "system_one", "https://other.invalid/v1/systemone", "other-model", "other-key")
        self.store.set_jev_profile(other["id"])
        second = active.read_settings()
        self.assertEqual(active.api_key(first["provider_name"]), "synthetic-jev-key")
        self.assertEqual(active.api_key(second["provider_name"]), "other-key")
        self.assertNotEqual(first["endpoint"], second["endpoint"])


if __name__ == "__main__":
    unittest.main()
