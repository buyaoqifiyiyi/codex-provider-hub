import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from storage import OFFICIAL_CODEX_URL, ProfileStore, validate_base_url


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="provider-hub-test-")
        self.store = ProfileStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_encrypted_key_roundtrip_and_metadata_never_expose_secret(self):
        secret = "synthetic-provider-key-4ae9"
        result = self.store.save("测试线路", "chat_completions", "https://example.com/v1", "fixture-chat", secret)
        self.assertEqual(self.store.get_secret(result["id"]), secret)
        self.assertTrue(result["has_key"])
        self.assertNotIn(secret, str(result))
        self.assertNotIn(secret, str(self.store.list()))
        self.assertNotIn(secret.encode(), self.store.path.read_bytes())

    def test_update_retains_key_only_for_same_origin(self):
        provider = self.store.save("A", "responses", "https://example.com/v1", "model-a", "synthetic-key")
        updated = self.store.save("B", "responses", "https://example.com/v2", "model-b", "", provider["id"])
        self.assertEqual(self.store.get_secret(updated["id"]), "synthetic-key")
        with self.assertRaises(ValueError):
            self.store.save("B", "responses", "https://other.example/v1", "model-b", "", provider["id"])
        self.assertEqual(self.store.list()[0]["base_url"], "https://example.com/v2")

    def test_subscription_uses_official_endpoint_and_no_api_key(self):
        result = self.store.save("Subscription", "codex_subscription", "https://ignored.example", "native-alpha")
        self.assertEqual(result["base_url"], OFFICIAL_CODEX_URL)
        self.assertFalse(result["has_key"])
        with self.assertRaises(ValueError):
            self.store.get_secret(result["id"])

    def test_model_settings_persist_and_invalid_values_do_not_change_profile(self):
        settings = {"context_window": 128000, "compact_limit": 110000,
                    "reasoning_levels": ["medium", "high"], "default_effort": "high"}
        profile = self.store.save("A", "responses", "https://example.com/v1", "model-a", "key", settings=settings)
        self.assertEqual(self.store.model_settings(profile["id"]), settings)
        with self.assertRaisesRegex(ValueError, "同时填写上下文"):
            self.store.save("B", "responses", "https://example.com/v1", "model-a", "", profile["id"],
                            settings=dict(settings, context_window=None))
        self.assertEqual(self.store.list()[0]["context_window"], 128000)
        with self.assertRaises(ValueError):
            self.store.save("B", "responses", "https://example.com/v1", "model-a", "", profile["id"],
                            settings=dict(settings, compact_limit=129000))
        self.assertEqual(self.store.list()[0]["name"], "A")
        self.assertEqual(self.store.model_settings(profile["id"]), settings)
        profile = self.store.save("External", "chat_completions", "https://example.com/v1", "fixture-chat", "key",
                                  settings=dict(settings, reasoning_levels=["low"], default_effort="low"))
        self.assertEqual(self.store.model_settings(profile["id"])["reasoning_levels"], ["low"])

    def test_invalid_provider_and_missing_keys_do_not_create_profile(self):
        with self.assertRaises(ValueError):
            self.store.save("A", "chat_completions", "https://example.com/v1", "model-a")
        with self.assertRaises(ValueError):
            self.store.save("A", "responses", "https://example.com/v1", "model-a", "x", "not-a-uuid")
        self.assertEqual(self.store.list(), [])

    def test_route_and_key_are_a_single_snapshot_during_concurrent_update(self):
        provider = self.store.save("External", "chat_completions", "https://old.example/v1", "model", "old-synthetic-key")
        self.store.set_route("fixture-model", provider["id"])
        original = storage._dpapi
        def update_during_decrypt(data, decrypt=False):
            if decrypt:
                self.store.save("External", "chat_completions", "https://new.example/v1", "model",
                                "new-synthetic-key", provider["id"])
            return original(data, decrypt=decrypt)
        with patch.object(storage, "_dpapi", side_effect=update_during_decrypt):
            route, key = self.store.resolve_route("fixture-model")
        self.assertEqual((route["base_url"], key), ("https://old.example/v1", "old-synthetic-key"))
        self.assertEqual(self.store.get_route("fixture-model")["base_url"], "https://new.example/v1")

    def test_protocol_change_is_rejected_while_a_model_is_bound(self):
        provider = self.store.save("External", "chat_completions", "https://example.com/v1", "model", "synthetic-key")
        self.store.set_route("fixture-model", provider["id"])
        with self.assertRaisesRegex(ValueError, "正在用于模型路由"):
            self.store.save("Official", "codex_subscription", "", "native-alpha", "", provider["id"])
        self.assertEqual(self.store.get_route("fixture-model")["protocol"], "chat_completions")

    def test_effort_evidence_follows_credential_and_endpoint_revision(self):
        provider = self.store.save("External", "chat_completions", "https://old.example/v1", "model", "old-key")
        self.store.set_route("fixture-model", provider["id"])
        original, _ = self.store.resolve_route("fixture-model")
        self.assertEqual(self.store.confirmed_efforts(original), set())
        self.store.confirm_effort("fixture-model", original["_revision"], "high")
        self.assertEqual(self.store.confirmed_efforts(original), {"high"})
        self.store.save("External", "chat_completions", "https://new.example/v1", "model", "new-key", provider["id"])
        changed, _ = self.store.resolve_route("fixture-model")
        self.assertNotEqual(original["_revision"], changed["_revision"])
        self.assertEqual(self.store.confirmed_efforts(changed), set())
        with self.assertRaisesRegex(ValueError, "线路在验证期间已改变"):
            self.store.confirm_effort("fixture-model", original["_revision"], "max")

    def test_engine_binding_is_invalidated_when_the_profile_changes(self):
        provider = self.store.save("External", "chat_completions", "https://old.example/v1", "model", "old-key")
        profile, _ = self.store.resolve_profile(provider["id"])
        account = "fixture-route/7b1c22c4-1151-418a-a137-6341ff54fcaf"
        self.store.bind_engine_route("fixture-model", profile["id"], profile["_revision"], account)
        self.assertEqual(self.store.get_engine_binding("fixture-model")["base_url"], "https://old.example/v1")
        self.store.save("External", "chat_completions", "https://new.example/v1", "model", "new-key", provider["id"])
        self.assertIsNone(self.store.get_engine_binding("fixture-model"))
        with self.assertRaisesRegex(ValueError, "连接在启用期间已改变"):
            self.store.bind_engine_route("fixture-model", profile["id"], profile["_revision"], account)


class UrlTests(unittest.TestCase):
    def test_only_https_or_explicit_loopback_and_no_embedded_credentials(self):
        self.assertEqual(validate_base_url("https://example.com/v1/"), "https://example.com/v1")
        self.assertEqual(validate_base_url("http://127.0.0.1:8765/v1"), "http://127.0.0.1:8765/v1")
        for value in ("http://example.com/v1", "https://user:pass@example.com", "https://example.com/v1?key=x",
                      "https://example.com/a/../b", "https://example.com/v1#part"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_base_url(value)


if __name__ == "__main__":
    unittest.main()
