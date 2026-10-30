import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import server


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ServerIntegrationTests(unittest.TestCase):
    def test_existing_text_tool_keeps_its_contract_and_validation(self):
        questions = {"ok": {"type": "noul", "instructions": "Is it complete?"}}
        with patch.object(server, "_request_jev", return_value={"answers": {"ok": {"noul": 0.9}}}) as request:
            self.assertEqual(server.jev_decide("Facts", questions)["answers"]["ok"]["noul"], 0.9)
            request.assert_called_once_with("Facts", questions)
            with self.assertRaises(ValueError):
                server.jev_decide("", questions)

    def test_text_size_limit_precedes_key_lookup_and_network(self):
        settings = {"provider_name": "fake", "endpoint": "https://example.invalid/v1/systemone", "model": "fixture"}
        with patch.object(server, "read_settings", return_value=settings), patch.object(server, "api_key") as key:
            with self.assertRaises(ValueError):
                server._request_jev("x" * 33000, {})
            key.assert_not_called()

    def test_image_request_uses_same_config_and_the_required_header(self):
        settings = {"provider_name": "fake", "endpoint": "https://example.invalid/v1/systemone", "model": "fixture"}
        opener = Mock()
        opener.open.return_value = Reply(json.dumps({"answers": {"a": {"type": "noul", "noul": 0.8}}}).encode())
        assets = {"a": {"type": "image", "format": "jpg", "data_base64": "AA=="}}
        with patch.object(server, "read_settings", return_value=settings), patch.object(server, "api_key", return_value="fake-key"), patch.object(server.urllib.request, "build_opener", return_value=opener), patch.object(server, "record_call") as record:
            answer = server._request_jev({"image": "a"}, {"a": {"type": "noul", "instructions": "Is it visible?"}}, assets)
        request = opener.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["typesafe-danger-zone-images"], "true")
        self.assertEqual(json.loads(request.data)["assets"], assets)
        self.assertEqual(answer["answers"]["a"]["noul"], 0.8)
        self.assertEqual(record.call_args.kwargs["outcome"], "success")

    def test_activity_write_failure_never_breaks_successful_request(self):
        settings = {"provider_name": "fake", "endpoint": "https://example.invalid/v1/systemone", "model": "fixture"}
        opener = Mock()
        opener.open.return_value = Reply(b'{"answers":{"ok":{"noul":0.8}}}')
        with patch.object(server, "read_settings", return_value=settings), patch.object(server, "api_key", return_value="fake-key"), patch.object(server.urllib.request, "build_opener", return_value=opener), patch.object(server, "record_call", side_effect=RuntimeError("local audit failure")):
            self.assertEqual(server._request_jev("x", {})["answers"]["ok"]["noul"], 0.8)

    def test_http_failure_records_the_operation_without_payload(self):
        settings = {"provider_name": "fake", "endpoint": "https://example.invalid/v1/systemone", "model": "fixture"}
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(settings["endpoint"], 503, "detail", None, None)
        with patch.object(server, "read_settings", return_value=settings), patch.object(server, "api_key", return_value="fake-key"), patch.object(server.urllib.request, "build_opener", return_value=opener), patch.object(server, "record_call") as record:
            with server._operation("jev_route_advice"), self.assertRaises(ValueError):
                server._request_jev("private input", {})
        self.assertEqual(record.call_args.kwargs["operation"], "jev_route_advice")
        self.assertEqual(record.call_args.kwargs["outcome"], "http_error")
        self.assertEqual(record.call_args.kwargs["http_status"], 503)
        self.assertNotIn("private input", str(record.call_args))
        self.assertNotIn("fake-key", str(record.call_args))

    def test_missing_answers_are_not_recorded_as_success(self):
        settings = {"provider_name": "fake", "endpoint": "https://example.invalid/v1/systemone", "model": "fixture"}
        opener = Mock()
        opener.open.return_value = Reply(b'{"answers":{}}')
        with patch.object(server, "read_settings", return_value=settings), patch.object(server, "api_key", return_value="fake-key"), patch.object(server.urllib.request, "build_opener", return_value=opener), patch.object(server, "record_call") as record:
            self.assertEqual(server._request_jev("fixture", {"ok": {"type": "noul"}}), {"answers": {}})
        self.assertEqual(record.call_args.kwargs["outcome"], "partial_response")

    def test_frame_tool_refuses_external_call_without_authorization(self):
        with patch.object(server, "_request_jev") as request:
            with self.assertRaises(ValueError):
                server.jev_screen_frames([], "goal")
            request.assert_not_called()

    def test_transcript_tool_batches_and_requires_external_authorization(self):
        candidates = [
            {"id": "first", "start_seconds": 1, "end_seconds": 3, "transcript": "A complete idea."},
            {"id": "second", "start_seconds": 4, "end_seconds": 6, "transcript": "Another idea."},
        ]
        def fake_decide(_state, questions):
            return {"answers": {key: {"type": "noul", "noul": 0.8} for key in questions}}
        with patch.object(server, "jev_decide", side_effect=fake_decide) as request:
            with self.assertRaises(ValueError):
                server.jev_rank_transcripts(candidates, "goal")
            request.assert_not_called()
            result = server.jev_rank_transcripts(candidates, "goal", allow_external=True)
            self.assertEqual(result["status"], "ok")
            request.assert_called_once()

    def test_evidence_tool_local_mode_uses_real_temporary_file(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "example.log").write_text("start\nERROR: fixture failed\n", encoding="utf-8")
            with patch.object(server, "jev_decide") as request:
                result = server.jev_select_evidence(root, ["example.log"], "error")
            request.assert_not_called()
            self.assertTrue(any("ERROR: fixture failed" in item["text"] for item in result["snippets"]))


if __name__ == "__main__":
    unittest.main()
