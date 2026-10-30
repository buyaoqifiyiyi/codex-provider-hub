"""Claude Messages connection checks with synthetic responses and credentials."""
import io
import base64
import json
import unittest
from unittest.mock import patch

import probe
from front_proxy import _seal_anthropic_frame, has_foreign_opaque_history
import test_engine_control as fixture
import test_save_jobs as job_fixtures


class Reply(io.BytesIO):
    status = 200


def stream(*events):
    return b"".join(("event: " + event["type"] + "\ndata: " +
                     json.dumps(event) + "\n\n").encode() for event in events)


def complete(reason="end_turn"):
    return (
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PROVIDER_HUB_"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "PROBE_OK"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": reason}},
        {"type": "message_stop"},
    )


class ClaudeProtocolTests(unittest.TestCase):
    def test_signed_reasoning_can_only_replay_to_its_unchanged_route(self):
        payload = {"txt": "fixture thinking", "sig": "synthetic-signature-123"}
        encoded = "ocxr1:" + base64.b64encode(json.dumps(payload).encode()).decode()
        event = {"type": "response.output_item.done",
                 "item": {"type": "reasoning", "encrypted_content": encoded}}
        frame = ("event: response.output_item.done\ndata: " + json.dumps(event) + "\n\n").encode()
        token, alias = "synthetic_local_capability_0123456789", "hub-a/claude-a"
        binding = {"protocol": "anthropic_messages", "_revision": "a" * 64}
        sealed = _seal_anthropic_frame(frame, alias, binding["_revision"], token)
        sealed_event = json.loads(next(line[6:] for line in sealed.decode().splitlines()
                                       if line.startswith("data: ")))
        request = {"input": [sealed_event["item"]]}
        self.assertFalse(has_foreign_opaque_history(request, alias=alias, binding=binding, token=token))
        self.assertTrue(has_foreign_opaque_history(request, alias="hub-b/claude-a", binding=binding, token=token))
        self.assertTrue(has_foreign_opaque_history(request, alias=alias,
                                                  binding=dict(binding, _revision="b" * 64), token=token))
        self.assertTrue(has_foreign_opaque_history(request, alias=alias,
                                                  binding=dict(binding, protocol="chat_completions"), token=token))
        self.assertTrue(has_foreign_opaque_history({"input": [event["item"]]}, alias=alias,
                                                   binding=binding, token=token))
        sealed_payload = probe.json.loads(base64.b64decode(sealed_event["item"]["encrypted_content"][6:]))
        sealed_payload["sig"] = "tampered"
        sealed_event["item"]["encrypted_content"] = "ocxr1:" + base64.b64encode(json.dumps(sealed_payload).encode()).decode()
        self.assertTrue(has_foreign_opaque_history(request, alias=alias, binding=binding, token=token))

    def test_probe_uses_native_auth_and_normalizes_supported_base_urls(self):
        seen = []
        def transport(url, data, headers):
            seen.append((url, json.loads(data), headers))
            return Reply(stream(*complete()))
        for base in ("https://example.invalid", "https://example.invalid/v1",
                     "https://example.invalid/v1/messages"):
            result = probe.probe_snapshot({"protocol": "anthropic_messages", "base_url": base,
                                           "model": "claude-fixture"}, "synthetic-key", None,
                                          transport=transport)
            self.assertTrue(result["ok"], result)
        for url, body, headers in seen:
            self.assertEqual(url, "https://example.invalid/v1/messages")
            self.assertEqual(headers["x-api-key"], "synthetic-key")
            self.assertEqual(headers["anthropic-version"], "2023-06-01")
            self.assertNotIn("Authorization", headers)
            self.assertEqual(body["model"], "claude-fixture")
            self.assertEqual(body["messages"][0]["role"], "user")
            self.assertTrue(body["stream"])
            self.assertGreater(body["max_tokens"], 0)

    def test_probe_requires_a_complete_successful_message(self):
        route = {"protocol": "anthropic_messages", "base_url": "https://example.invalid",
                 "model": "claude-fixture"}
        cases = [(complete()[:-1], "response_missing_completion"),
                 (complete("max_tokens"), "output_limit"),
                 (complete("tool_use"), "response_incomplete"),
                 (complete() + ({"type": "error", "error": {}},), "upstream_stream_error")]
        for events, code in cases:
            with self.subTest(code=code):
                result = probe.probe_snapshot(route, "key", None,
                                              transport=lambda *_: Reply(stream(*events)))
                self.assertEqual(result["code"], code)
        self.assertEqual(probe.probe_snapshot(route, "key", "high", transport=lambda *_: None)["code"],
                         "unsupported_effort")

    def test_model_discovery_uses_native_headers_and_follows_cursor(self):
        seen = []
        class Opener:
            def open(self, request, timeout):
                seen.append((request.full_url, dict(request.header_items())))
                rows = ({"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"}
                        if len(seen) == 1 else
                        {"data": [{"id": "claude-b"}], "has_more": False, "last_id": "claude-b"})
                return Reply(json.dumps(rows).encode())
        result = probe.fetch_model_ids("https://example.invalid/v1/messages", "synthetic-key",
                                       protocol="anthropic_messages", opener=Opener())
        self.assertEqual(result["models"], [{"id": "claude-a"}, {"id": "claude-b"}])
        self.assertEqual(seen[0][0], "https://example.invalid/v1/models")
        self.assertEqual(seen[1][0], "https://example.invalid/v1/models?after_id=claude-a")
        for _, headers in seen:
            self.assertEqual(headers.get("X-api-key"), "synthetic-key")
            self.assertNotIn("Authorization", headers)
        self.assertNotIn("synthetic-key", str(result))

    def test_repeated_model_cursor_returns_partial_list_as_truncated(self):
        class Opener:
            def open(self, request, timeout):
                return Reply(b'{"data":[{"id":"claude-a"}],"has_more":true,"last_id":"claude-a"}')
        result = probe.fetch_model_ids("https://example.invalid", "key",
                                       protocol="anthropic_messages", opener=Opener())
        self.assertEqual(result["models"], [{"id": "claude-a"}])
        self.assertTrue(result["truncated"])


@unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI is required")
class ClaudeSaveTests(unittest.TestCase):
    setUp = fixture.EngineControlTests.setUp
    tearDown = fixture.EngineControlTests.tearDown
    keyring = fixture.EngineControlTests.keyring
    write = fixture.EngineControlTests.write
    jobs = job_fixtures.SaveJobTests.jobs
    start = job_fixtures.SaveJobTests.start
    wait_ready = job_fixtures.SaveJobTests.wait_ready
    authorize = job_fixtures.SaveJobTests.authorize
    finish = job_fixtures.SaveJobTests.finish

    def test_multimodel_commit_creates_native_bindings(self):
        jobs = self.jobs()
        with patch("probe.probe_snapshot", return_value={"ok": True}) as checks, \
             patch("probe.discover_capabilities", return_value={}):
            started = self.start(jobs, protocol="anthropic_messages", model="claude-a",
                                 models=["claude-a", "claude-b"])
            result = self.finish(jobs, started)
        self.assertEqual(result["state"], "succeeded", result)
        self.assertEqual(checks.call_count, 2)
        aliases = result["result"]["activation"]["aliases"]
        self.assertEqual(len(aliases), 2)
        for alias in aliases:
            binding = self.store.get_engine_binding(alias)
            self.assertEqual(binding["protocol"], "anthropic_messages")
            name = binding["account"].split("/", 1)[0]
            self.assertEqual(self.manager.snapshot()["providers"][name]["adapter"], "anthropic")
            self.assertTrue(__import__("front_proxy").named_route_ready(self.store, self.manager.home, alias))

    def test_failed_model_does_not_save_or_activate_anything(self):
        jobs = self.jobs()
        before = self.store.list()
        with patch("probe.probe_snapshot", side_effect=[{"ok": True}, {"ok": False, "code": "marker_missing"}]), \
             patch("probe.discover_capabilities", return_value={}):
            started = self.start(jobs, protocol="anthropic_messages", model="claude-a",
                                 models=["claude-a", "claude-b"])
            result = self.finish(jobs, started)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["failed_model"], "claude-b")
        self.assertEqual(self.store.list(), before)
        self.assertEqual(self.keys, {})


if __name__ == "__main__":
    unittest.main()
