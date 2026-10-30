import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from gemini_media import MAX_MEDIA_BYTES, inspect_media


class GeminiMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "clip.mp4"
        self.file.write_bytes(b"synthetic media bytes")
        self.profile = {"name": "示例", "protocol": "gemini_native", "base_url": "https://example.invalid"}

    def test_video_is_sent_as_native_inline_data_without_exposing_key(self):
        def transport(request, timeout):
            self.assertEqual(request.full_url, "https://example.invalid/v1beta/models/gemini-test:generateContent")
            self.assertEqual(request.get_header("X-goog-api-key"), "synthetic-key")
            body = json.loads(request.data)
            parts = body["contents"][0]["parts"]
            self.assertEqual(parts[0]["text"], "描述视频")
            self.assertEqual(parts[1]["inlineData"]["mimeType"], "video/mp4")
            self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), b"synthetic media bytes")
            return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [
                {"text": "hidden", "thought": True}, {"text": "可见结果"}]}, "finishReason": "STOP"}]}).encode())
        result = inspect_media(self.profile, "synthetic-key", "gemini-test", str(self.file), "描述视频", transport=transport)
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], "可见结果")
        self.assertNotIn("synthetic-key", str(result))

        result = inspect_media(self.profile, "synthetic-key", "models/gemini-test", str(self.file), "描述视频", transport=transport)
        self.assertTrue(result["ok"])

    def test_audio_uses_audio_mime_type(self):
        self.file = Path(self.tmp.name) / "sample.wav"
        self.file.write_bytes(b"synthetic audio")
        def transport(request, timeout):
            part = json.loads(request.data)["contents"][0]["parts"][1]["inlineData"]
            self.assertEqual(part["mimeType"], "audio/wav")
            self.assertEqual(base64.b64decode(part["data"]), b"synthetic audio")
            return io.BytesIO(b'{"candidates":[{"content":{"parts":[{"text":"heard"}]},"finishReason":"STOP"}]}')
        self.assertTrue(inspect_media(self.profile, "key", "gemini-test", str(self.file), "听音频", transport=transport)["ok"])

    def test_invalid_or_large_files_never_send_a_request(self):
        def no_request(*args, **kwargs):
            self.fail("invalid media must not be transmitted")
        self.assertEqual(inspect_media(self.profile, "key", "gemini-test", "relative.mp4", "看", transport=no_request)["code"], "MEDIA_UNREADABLE")
        self.file = Path(self.tmp.name) / "other.txt"
        self.file.write_text("not media")
        self.assertEqual(inspect_media(self.profile, "key", "gemini-test", str(self.file), "看", transport=no_request)["code"], "MEDIA_FORMAT_UNSUPPORTED")
        self.file = Path(self.tmp.name) / "large.mp4"
        with self.file.open("wb") as f:
            f.truncate(MAX_MEDIA_BYTES + 1)
        self.assertEqual(inspect_media(self.profile, "key", "gemini-test", str(self.file), "看", transport=no_request)["code"], "MEDIA_TOO_LARGE")

    def test_rejected_and_incomplete_responses_are_not_success(self):
        def rejected(request, timeout):
            raise HTTPError(request.full_url, 403, "synthetic-secret", {}, io.BytesIO(b"synthetic-secret"))
        result = inspect_media(self.profile, "key", "gemini-test", str(self.file), "看", transport=rejected)
        self.assertEqual((result["ok"], result["http_status"]), (False, 403))
        self.assertNotIn("synthetic-secret", str(result))
        def incomplete(request, timeout):
            return io.BytesIO(b'{"candidates":[{"content":{"parts":[{"text":"partial"}]},"finishReason":"MAX_TOKENS"}]}')
        result = inspect_media(self.profile, "key", "gemini-test", str(self.file), "看", transport=incomplete)
        self.assertEqual(result["code"], "MEDIA_RESPONSE_INCOMPLETE")
        self.assertEqual(result["answer"], "partial")

    def test_non_native_connection_does_not_send(self):
        self.profile["protocol"] = "chat_completions"
        self.assertEqual(inspect_media(self.profile, "key", "gemini-test", str(self.file), "看")["code"], "GEMINI_NATIVE_REQUIRED")


if __name__ == "__main__":
    unittest.main()
