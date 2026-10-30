import io
import math
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from frame_candidates import _prepare_image, judge_frames


class FrameCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.frames = []
        for ident, color, time in (("red", "red", 1.25), ("blue", "blue", 8.0)):
            path = self.root / f"{ident}.png"
            Image.new("RGB", (300, 300), color).save(path)
            self.frames.append({"id": ident, "path": str(path), "time_seconds": time})

    def tearDown(self):
        self.tmp.cleanup()

    def test_judges_selected_images_and_preserves_timestamps_without_sending_paths(self):
        seen = {}
        def ask(state, questions, assets):
            seen.update(state=state, questions=questions, assets=assets)
            return {"answers": {"frame_0": {"noul": 0.9}, "frame_1": {"noul": 0.1}}}
        result = judge_frames(self.frames, "red imagery", ask, allow_external=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([(x["id"], x["time_seconds"]) for x in result["frames"]], [("red", 1.25), ("blue", 8.0)])
        self.assertEqual(len(seen["assets"]), 2)
        self.assertNotIn(str(self.root), str(seen["state"]))
        self.assertIn("static_frames_only", result["evidence"])

    def test_missing_permission_prevents_reads_and_calls(self):
        called = []
        with self.assertRaises(ValueError):
            judge_frames([{"id": "x", "path": "missing", "time_seconds": 0}], "goal", lambda *x: called.append(x))
        self.assertEqual(called, [])

    def test_partial_and_unavailable_are_not_fabricated_scores(self):
        result = judge_frames(self.frames, "goal", lambda *_: {"answers": {"frame_0": {"noul": math.nan}}}, allow_external=True)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(item["score"] is None for item in result["frames"]))
        def fail(*_):
            raise TimeoutError("private detail")
        result = judge_frames(self.frames, "goal", fail, allow_external=True)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["warning"], "TimeoutError")
        self.assertNotIn("private detail", str(result))

    def test_duplicate_ids_and_invalid_timestamps_are_rejected(self):
        duplicate = [self.frames[0], dict(self.frames[1], id=self.frames[0]["id"])]
        with self.assertRaises(ValueError):
            judge_frames(duplicate, "goal", lambda *_: {}, allow_external=True)
        with self.assertRaises(ValueError):
            judge_frames([dict(self.frames[0], time_seconds=-1)], "goal", lambda *_: {}, allow_external=True)

    def test_exif_orientation_is_applied_before_metadata_is_removed(self):
        path = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (400, 600), "red").save(path, exif=exif)
        result, size = _prepare_image(str(path))
        self.assertEqual(size, (600, 400))
        with Image.open(io.BytesIO(result)) as image:
            self.assertNotIn(274, image.getexif())

    def test_fully_transparent_rgb_is_not_exposed(self):
        path = self.root / "transparent.png"
        Image.new("RGBA", (300, 300), (255, 0, 0, 0)).save(path)
        result, _ = _prepare_image(str(path))
        with Image.open(io.BytesIO(result)) as image:
            self.assertTrue(all(value < 10 for value in image.getpixel((150, 150))))


if __name__ == "__main__":
    unittest.main()
