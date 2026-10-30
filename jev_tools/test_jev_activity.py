import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import jev_activity


class JevActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CODEX_HOME": self.temp.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_empty_status_does_not_create_a_database(self):
        result = jev_activity.status()
        self.assertEqual(result["status"], "no_records")
        self.assertEqual(result["requests"], 0)
        self.assertFalse(jev_activity.activity_path().exists())

    def test_real_attempt_metadata_only_and_clear_limitations(self):
        self.assertTrue(jev_activity.record_call(operation="jev_decide", source="cli", outcome="success",
            model="vendor/fixture-jev-v1", duration_ms=1200,
            usage={"input_tokens": 376, "output_tokens": 52, "secret": "never-store-this"}, http_status=200))
        self.assertTrue(jev_activity.record_call(operation="jev_screen_frames", source="mcp", outcome="http_error",
            model="fixture-jev", duration_ms=55, http_status=503))
        result = jev_activity.status()
        self.assertEqual((result["requests"], result["successful"], result["failed"]), (2, 1, 1))
        self.assertEqual(result["recent"][1]["operation"], "jev_decide")
        self.assertIn("不区分真实任务和测试", result["note"])
        self.assertNotIn(b"never-store-this", jev_activity.activity_path().read_bytes())

    def test_concurrent_writers_do_not_lose_records(self):
        def write(_):
            return jev_activity.record_call(operation="jev_rank_transcripts", source="cli", outcome="success",
                                             model="fixture-jev", duration_ms=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(write, range(20)))
        self.assertTrue(all(outcomes))
        self.assertEqual(jev_activity.status()["requests"], 20)

    def test_bad_database_is_reported_without_modifying_it(self):
        path = jev_activity.activity_path()
        path.write_bytes(b"not a database")
        result = jev_activity.status()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(path.read_bytes(), b"not a database")

    def test_invalid_window_is_rejected(self):
        for value in (0, 721, True, "24"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                jev_activity.status(value)


if __name__ == "__main__":
    unittest.main()
