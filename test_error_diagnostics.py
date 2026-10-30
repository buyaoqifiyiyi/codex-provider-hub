"""Error evidence remains useful without exposing upstream response text or keys."""
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import probe
import test_engine_control as fixture
import test_save_jobs as job_fixtures


ROUTE = {"protocol": "anthropic_messages", "base_url": "https://fixture.invalid/v1",
         "model": "claude-fixture"}


def failure(body, headers=None, key="synthetic-private-key"):
    def transport(url, data, request_headers):
        raise HTTPError(url, 500, "synthetic-secret", headers or {}, io.BytesIO(body))
    return probe.probe_snapshot(ROUTE, key, None, transport=transport)


class ErrorDiagnosticsTests(unittest.TestCase):
    def test_known_type_and_request_id_are_retained_but_raw_text_is_not(self):
        body = json.dumps({"type": "error", "error": {"type": "overloaded_error",
                           "message": "synthetic-private-key synthetic-private-details"}}).encode()
        result = failure(body, {"request-id": "req_1234567890ABCDEF"})
        self.assertEqual(result["http_status"], 500)
        self.assertEqual(result["upstream_error_type"], "overloaded_error")
        self.assertEqual(result["request_id"], "req_1234567890ABCDEF")
        message = probe.describe_probe_failure(result)
        self.assertIn("overloaded_error", message)
        self.assertIn("req_1234567890ABCDEF", message)
        self.assertNotIn("synthetic-private-key", str(result) + message)
        self.assertNotIn("synthetic-private-details", str(result) + message)

    def test_unknown_fields_and_credential_containing_request_id_are_discarded(self):
        key = "SecretKey123456"
        body = json.dumps({"error": {"type": "credential_SecretKey123456",
                                     "message": key, "code": key}}).encode()
        result = failure(body, {"request-id": "req_SecretKey123456"}, key=key)
        self.assertNotIn("upstream_error_type", result)
        self.assertNotIn("request_id", result)
        self.assertNotIn(key, str(result) + probe.describe_probe_failure(result))

    def test_uuid_request_id_is_accepted_when_not_part_of_the_key(self):
        request_id = "12345678-abcd-1234-abcd-1234567890ab"
        result = failure(b'{}', {"x-request-id": request_id})
        self.assertEqual(result["request_id"], request_id)
        result = failure(b'{}', {"x-request-id": request_id}, key="prefix-abcd-1234-suffix")
        self.assertNotIn("request_id", result)

    def test_oversized_or_malformed_error_body_remains_a_safe_http_error(self):
        for body in (b"x" * 20000, b"not-json"):
            with self.subTest(size=len(body)):
                result = failure(body, {"request-id": "req_1234567890ABCDEF"})
                self.assertEqual(result["http_status"], 500)
                self.assertNotIn("upstream_error_type", result)
                self.assertEqual(result["request_id"], "req_1234567890ABCDEF")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class ErrorJobTests(unittest.TestCase):
    setUp = fixture.EngineControlTests.setUp
    tearDown = fixture.EngineControlTests.tearDown
    keyring = fixture.EngineControlTests.keyring
    write = fixture.EngineControlTests.write
    jobs = job_fixtures.SaveJobTests.jobs
    start = job_fixtures.SaveJobTests.start
    wait_ready = job_fixtures.SaveJobTests.wait_ready
    authorize = job_fixtures.SaveJobTests.authorize
    finish = job_fixtures.SaveJobTests.finish

    def test_failure_keeps_sanitized_diagnostics_without_saving(self):
        before = self.store.list()
        with patch("probe.probe_snapshot", return_value={"ok": False, "code": "upstream_rejected",
                   "http_status": 500, "upstream_error_type": "overloaded_error",
                   "request_id": "req_1234567890ABCDEF"}):
            jobs = self.jobs()
            started = self.start(jobs, protocol="anthropic_messages", model="claude-fixture",
                                 models=["claude-fixture"])
            status = self.finish(jobs, started)
        self.assertEqual(status["state"], "failed")
        result = status["result"]
        self.assertEqual(result["http_status"], 500)
        self.assertEqual(result["upstream_error_type"], "overloaded_error")
        self.assertEqual(result["request_id"], "req_1234567890ABCDEF")
        self.assertEqual(self.store.list(), before)


if __name__ == "__main__":
    unittest.main()
