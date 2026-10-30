"""Synthetic memory/temporary files and fake Jev. Run: python -B -m unittest -v.

EvidenceTests mocks filesystem boundaries. RealFileTests uses automatically
cleaned temporary files and the real safe-open path, including Windows handles.
All network entrypoints are blocked and all remote callbacks are fake. No user
materials, hostile filesystem races or live-provider integration are tested.
"""

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.request

import evidence_select as es


def block_network(stack):
    for obj, name in ((socket, "create_connection"), (socket.socket, "connect"),
                      (urllib.request, "urlopen"), (urllib.request, "build_opener")):
        stack.enter_context(patch.object(
            obj, name, side_effect=AssertionError("unexpected network")))


class MemoryFS:
    def __init__(self):
        self.root = Path(os.path.abspath(os.sep + "synthetic-evidence"))
        self.nodes = {}
        self.handles = {}
        self.opened = []
        self.reads = []
        self.next_fd = 100
        self.final_override = None
        self.changed_inode = False
        for path in (*reversed(self.root.parents), self.root):
            self.add_node(path, stat.S_IFDIR)

    def add_node(self, path, mode, data=b"", *, nlink=1, attributes=0):
        key = str(path)
        self.nodes[key] = (SimpleNamespace(
            st_mode=mode | 0o600, st_size=len(data), st_nlink=nlink,
            st_dev=1, st_ino=len(self.nodes) + 1, st_file_attributes=attributes,
        ), data)

    def add(self, relative, text, **kwargs):
        path = self.root.joinpath(*relative.split("/"))
        for directory in reversed(path.parents):
            if str(directory) not in self.nodes:
                self.add_node(directory, stat.S_IFDIR)
        data = text.encode("utf-8") if isinstance(text, str) else text
        self.add_node(path, stat.S_IFREG, data, **kwargs)
        return path

    def lstat(self, path):
        if str(path) not in self.nodes:
            raise FileNotFoundError("synthetic_missing")
        return self.nodes[str(path)][0]

    def open(self, path, flags, *, dir_fd=None):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise AssertionError("write attempted")
        path = Path(path) if dir_fd is None else self.handles[dir_fd] / path
        info = self.lstat(path)
        if flags & getattr(os, "O_NOFOLLOW", 0) and es._linked(info):
            raise OSError("synthetic_link")
        self.next_fd += 1
        self.handles[self.next_fd] = path
        self.opened.append(path)
        return self.next_fd

    def close(self, fd):
        del self.handles[fd]

    def fstat(self, fd):
        info = self.lstat(self.handles[fd])
        if self.changed_inode and stat.S_ISREG(info.st_mode):
            return SimpleNamespace(**{**vars(info), "st_ino": info.st_ino + 999})
        return info

    def fdopen(self, fd, mode):
        if mode != "rb":
            raise AssertionError("not read-only")
        fs = self
        path = self.handles[fd]

        class Source(io.BytesIO):
            def fileno(self):
                return fd

            def read(self, size=-1):
                fs.reads.append((path, size))
                return super().read(size)

            def close(self):
                if fd in fs.handles:
                    fs.close(fd)
                super().close()

        return Source(self.nodes[str(path)][1])

    def final_name(self, handle, buffer, length, flags):
        actual = self.final_override or str(self.handles[handle])
        buffer.value = "\\\\?\\" + actual
        return len(buffer.value)

    def patch_into(self, stack):
        stack.enter_context(patch.object(Path, "lstat", lambda path: self.lstat(path)))
        for name in ("open", "close", "fstat", "fdopen"):
            stack.enter_context(patch.object(es.os, name, getattr(self, name)))
        if os.name == "nt":
            import ctypes
            import msvcrt
            kernel = SimpleNamespace(GetFinalPathNameByHandleW=Mock(side_effect=self.final_name))
            stack.enter_context(patch.object(ctypes, "WinDLL", return_value=kernel))
            stack.enter_context(patch.object(msvcrt, "get_osfhandle", side_effect=lambda fd: fd))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.fs = MemoryFS()
        self.fs.patch_into(self.stack)
        block_network(self.stack)

    def select(self, paths, query="needle", **kwargs):
        return es.select_evidence(str(self.fs.root), paths, query, **kwargs)

    def many(self, count=12, *, errors=(), padding=""):
        text = "".join(
            f"needle {i} {padding}\n{'RuntimeError: broken' if i in errors else 'context'}\nend\n"
            for i in range(count)
        )
        self.fs.add("run.log", text)
        return text

    @staticmethod
    def answers(state, questions):
        sent = json.loads(state)["candidates"]
        return {"answers": {c["id"]: {"noul": i / max(1, len(sent) - 1)}
                            for i, c in enumerate(sent)}}

    def test_original_text_line_numbers_bom_and_line_endings(self):
        text = "\ufeffheader\r\n\r\nneedle α\r\nERROR original\rlast"
        self.fs.add("src/trace.log", text)
        result = self.select(["src\\trace.log", "src/trace.log"])
        self.assertEqual(result["mode"], "direct")
        self.assertEqual(result["stats"]["files_scanned"], 1)
        lines = ["\ufeffheader\r\n", "\r\n", "needle α\r\n", "ERROR original\r", "last"]
        for snippet in result["snippets"]:
            self.assertEqual(snippet["text"], "".join(
                lines[snippet["start_line"] - 1:snippet["end_line"]]))
            self.assertEqual(snippet["path"], "src/trace.log")
        self.assertEqual(result["stats"]["error_lines_returned"], 1)
        self.assertTrue(result["stats"]["error_coverage_complete"])
        self.assertEqual(self.fs.nodes[str(self.fs.root / "src" / "trace.log")][1],
                         text.encode("utf-8"))

    def test_path_boundaries_do_not_open_any_file(self):
        bad = ["../outside.log", "sub/../../outside.log", "/outside.log",
               "C:\\outside.log", "C:outside.log", "\\\\host\\share\\a.log",
               "sub\\..\\outside.log", "run.log:stream", "sub//a.log", "./a.log",
               "sub./a.log", "sub /a.log", "a\x00.log", "CON.txt", "LPT1.log", "*.log"]
        result = self.select(bad)
        self.assertEqual(len(result["skipped"]), len(bad))
        self.assertEqual(self.fs.opened, [])
        self.assertTrue(result["truncated"]["search"])
        self.assertFalse(result["stats"]["error_coverage_complete"])

    def test_symlink_reparse_hardlink_and_root_link(self):
        self.fs.add("symlink.log", "needle")
        self.fs.nodes[str(self.fs.root / "symlink.log")][0].st_mode = stat.S_IFLNK
        self.fs.add("junction/file.log", "needle")
        self.fs.nodes[str(self.fs.root / "junction")][0].st_file_attributes = 0x400
        self.fs.add("hard.log", "needle", nlink=2)
        self.fs.add("pipe.log", "needle")
        self.fs.nodes[str(self.fs.root / "pipe.log")][0].st_mode = stat.S_IFIFO
        result = self.select(["symlink.log", "junction/file.log", "hard.log", "pipe.log"])
        self.assertEqual(len(result["skipped"]), 4)
        self.assertEqual(self.fs.reads, [])
        self.fs.nodes[str(self.fs.root)][0].st_mode = stat.S_IFLNK
        with self.assertRaises(ValueError):
            self.select(["hard.log"])

    def test_open_identity_change_is_rejected_and_fd_closed(self):
        self.fs.add("race.log", "needle")
        self.fs.changed_inode = True
        result = self.select(["race.log"])
        self.assertEqual(result["skipped"][0]["reason"], "file_changed_or_linked")
        self.assertEqual(self.fs.reads, [])
        self.assertEqual(self.fs.handles, {})

    @unittest.skipUnless(os.name == "nt", "Windows handle-path guard")
    def test_windows_opened_handle_outside_root_is_rejected_before_read(self):
        self.fs.add("race.log", "needle")
        self.fs.final_override = str(self.fs.root.parent / "outside.log")
        result = self.select(["race.log"])
        self.assertEqual(result["skipped"][0]["reason"], "opened_path_mismatch")
        self.assertEqual(self.fs.reads, [])
        self.assertEqual(self.fs.handles, {})

    def test_sensitive_binary_unsupported_and_large_files(self):
        for path in (".env", "credentials.json", "settings.json", ".ssh/id_rsa", "key.pem"):
            self.fs.add(path, "needle")
        self.fs.add("private.log", "needle")
        self.fs.add("binary.log", b"needle\x00oops")
        self.fs.add("legacy.log", b"needle\xff")
        self.fs.add("leak.log", 'api_key = "synthetic-only"\nneedle')
        self.fs.add("keydump.log", "-----BEGIN PRIVATE KEY-----\nneedle")
        self.fs.add("big.log", b"x" * (es.MAX_FILE_BYTES + 1))
        paths = [".env", "credentials.json", "settings.json", ".ssh/id_rsa", "key.pem",
                 "private.log", "binary.log", "legacy.log", "leak.log", "keydump.log", "big.log"]
        result = self.select(paths)
        self.assertEqual(len(result["skipped"]), len(paths))
        self.assertEqual(result["snippets"], [])
        self.assertNotIn(self.fs.root / "big.log", [p for p, _ in self.fs.reads])
        self.assertEqual(self.fs.handles, {})

    def test_limits_report_search_truncation(self):
        self.fs.add("a.log", "needle\n" * 5)
        self.fs.add("b.log", "needle\n" * 5)
        self.fs.add("c.log", "needle\n")
        with patch.object(es, "MAX_FILES", 2), patch.object(es, "MAX_TOTAL_BYTES", 50):
            result = self.select(["a.log", "b.log", "c.log"])
        self.assertTrue(result["truncated"]["search"])
        self.assertEqual(result["stats"]["files_scanned"], 1)
        self.assertLessEqual(result["stats"]["bytes_read"], 50)
        self.assertTrue(any("FILE_LIMIT" in w for w in result["warnings"]))
        self.assertEqual(result["skipped"][0]["reason"], "file_or_total_byte_limit")
        self.fs.add("late.log", "ordinary\n" * 6 + "FATAL late\n")
        with patch.object(es, "MAX_SEARCH_LINES", 6):
            limited = self.select(["late.log"])
        self.assertEqual(limited["stats"]["lines_scanned"], 6)
        self.assertEqual(limited["stats"]["error_lines_found"], 1)
        self.assertEqual(limited["snippets"][0]["error_lines"], [7])
        self.assertEqual(limited["scan_ranges"][0]["unscanned"],
                         [{"start_line": 4, "end_line": 4}])
        self.assertFalse(limited["stats"]["error_coverage_complete"])

    def test_rejected_content_also_consumes_total_read_budget(self):
        self.fs.add("binary.log", b"x" * 39 + b"\x00")
        self.fs.add("next.log", "needle" * 5)
        with patch.object(es, "MAX_TOTAL_BYTES", 50):
            result = self.select(["binary.log", "next.log"])
        self.assertEqual(result["stats"]["bytes_read"], 40)
        self.assertEqual(result["stats"]["files_scanned"], 0)
        self.assertEqual(len(self.fs.reads), 1)
        self.assertEqual(result["skipped"][1]["reason"], "file_or_total_byte_limit")

    def test_error_at_end_survives_candidate_and_output_caps(self):
        self.many(50, errors={49})
        result = self.select(["run.log"])
        self.assertEqual(result["mode"], "local")
        self.assertEqual(len(result["snippets"]), es.MAX_OUTPUT_SNIPPETS)
        self.assertIn(149, [line for s in result["snippets"] for line in s["error_lines"]])
        self.assertTrue(result["truncated"]["candidates"])
        self.assertTrue(result["truncated"]["output"])
        self.assertTrue(result["stats"]["error_coverage_complete"])

    def test_too_many_errors_and_long_error_line_have_coverage_warning(self):
        self.many(12, errors=set(range(12)))
        result = self.select(["run.log"])
        self.assertEqual(result["stats"]["error_lines_found"], 12)
        self.assertEqual(result["stats"]["error_lines_omitted"], 4)
        self.assertFalse(result["stats"]["error_coverage_complete"])
        self.assertTrue(any("ERROR_LINES_OMITTED: 4" in w for w in result["warnings"]))
        self.fs.add("long.log", "ERROR " + "x" * es.MAX_SNIPPET_CHARS + "\nneedle\n")
        long = self.select(["long.log"])
        self.assertEqual(long["stats"]["error_lines_omitted"], 1)
        self.assertTrue(long["truncated"]["search"])
        self.assertEqual(long["snippets"][0]["text"], "needle\n")
        self.assertEqual(long["snippets"][0]["start_line"], 2)

    def test_small_result_and_local_mode_never_call_ask_or_network(self):
        self.fs.add("small.log", "needle\n")
        ask = Mock(side_effect=AssertionError("must not call ask"))
        for allowed in (False, True):
            small = self.select(["small.log"], allow_remote=allowed, ask=ask)
            self.assertEqual(small["mode"], "direct")
            self.assertFalse(small["remote"]["attempted"])
        self.many()
        local = self.select(["run.log"], ask=ask)
        self.assertEqual(local["mode"], "local")
        ask.assert_not_called()

    def test_remote_ranking_only_changes_selection_not_source_or_error_priority(self):
        text = self.many(errors={0})
        ask = Mock(side_effect=self.answers)
        result = self.select(["run.log"], allow_remote=True, ask=ask)
        self.assertEqual(result["mode"], "remote")
        self.assertTrue(result["remote"]["used"])
        self.assertEqual(result["snippets"][0]["id"], "c0")
        self.assertEqual(result["snippets"][1]["id"], "c11")
        lines = text.splitlines(keepends=True)
        for snippet in result["snippets"]:
            self.assertEqual(snippet["text"], "".join(
                lines[snippet["start_line"] - 1:snippet["end_line"]]))
        state, questions = ask.call_args.args
        payload = json.loads(state)
        self.assertEqual(set(payload), {"query", "candidates"})
        self.assertEqual(set(payload["candidates"][0]), {"id", "text"})
        self.assertNotIn(str(self.fs.root), state)
        self.assertTrue(all(q["type"] == "noul" for q in questions.values()))
        self.assertEqual(set(questions), {c["id"] for c in payload["candidates"]})
        for candidate_id, question in questions.items():
            self.assertIn(f"candidate ID {candidate_id} in state.candidates", question["instructions"])
            self.assertEqual(question["instructions"].count("candidate ID "), 1)
        self.assertEqual(len({q["instructions"] for q in questions.values()}), len(questions))
        self.assertIn(es.REMOTE_NOTICE, result["warnings"])
        ask.assert_called_once()

    def test_remote_failures_missing_results_and_invalid_scores_fall_back(self):
        self.many()
        local = self.select(["run.log"])["snippets"]

        def bad_score(value):
            def respond(state, questions):
                answer = self.answers(state, questions)
                answer["answers"][next(iter(questions))]["noul"] = value
                return answer
            return respond

        def missing(state, questions):
            answer = self.answers(state, questions)
            answer["answers"].pop(next(iter(questions)))
            return answer

        callbacks = [None, Mock(side_effect=RuntimeError("SENSITIVE exception")),
                     lambda *_: {}, lambda *_: {"code": 4}, lambda *_: [], missing,
                     bad_score(float("nan")), bad_score(float("inf")), bad_score(True),
                     bad_score("0.9"), bad_score(2)]
        for callback in callbacks:
            with self.subTest(callback=callback):
                result = self.select(["run.log"], allow_remote=True, ask=callback)
                self.assertEqual(result["mode"], "local_fallback")
                self.assertFalse(result["remote"]["used"])
                self.assertEqual(result["snippets"], local)
                self.assertNotIn("SENSITIVE exception", json.dumps(result))
                self.assertTrue(any("fallback" in w for w in result["warnings"]))

    def test_multibyte_remote_payload_is_bounded_and_unsent_rank_locally(self):
        self.many(32, padding="🙂" * 250)
        captured = []

        def ask(state, questions):
            captured.append((state, questions))
            return self.answers(state, questions)

        result = self.select(["run.log"], allow_remote=True, ask=ask)
        state, questions = captured[0]
        envelope = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        self.assertLessEqual(len(envelope.encode("utf-8")), es.MAX_REMOTE_BYTES)
        self.assertTrue(result["truncated"]["remote"])
        self.assertEqual(result["mode"], "remote")
        self.assertLess(result["remote"]["candidates_sent"], 32)
        sent_ids = {c["id"] for c in json.loads(state)["candidates"]}
        self.assertIn("c0", sent_ids)
        self.assertIn("c31", sent_ids)
        self.assertLessEqual(sum(len(s["text"]) for s in result["snippets"]), es.MAX_OUTPUT_CHARS)
        with patch.object(es, "MAX_REMOTE_BYTES", 1):
            fallback = self.select(["run.log"], allow_remote=True, ask=Mock())
        self.assertEqual(fallback["mode"], "local_fallback")
        self.assertFalse(fallback["remote"]["attempted"])

    def test_no_match_and_bad_arguments(self):
        self.fs.add("ordinary.txt", "no relevant words\n")
        result = self.select(["ordinary.txt"])
        self.assertEqual(result["snippets"], [])
        self.assertTrue(any("NO_QUERY_MATCHES" in w for w in result["warnings"]))
        for root, paths, query, flag in (
            ("relative", [], "needle", False), (str(self.fs.root), "run.log", "needle", False),
            (str(self.fs.root), [], "", False), (str(self.fs.root), [], "x" * 513, False),
            (str(self.fs.root), [], "needle", 1), ("//host/share", [], "needle", False),
        ):
            with self.subTest(root=root, query=query):
                with self.assertRaises(ValueError):
                    es.select_evidence(root, paths, query, allow_remote=flag)


class RealFileTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        block_network(self.stack)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="jev-evidence-")))

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def select(self, paths, query="needle", **kwargs):
        return es.select_evidence(str(self.root), paths, query, **kwargs)

    def test_real_eight_mib_log_head_tail_and_original_line_numbers(self):
        self.assertGreaterEqual(es.MAX_FILE_BYTES, 8 * 1024 * 1024)
        self.assertGreaterEqual(es.MAX_TOTAL_BYTES, 16 * 1024 * 1024)
        # 65,536 * 128 bytes = exactly 8 MiB. CRLF remains byte-for-byte intact.
        lines = [(f"entry {i:06d} ".ljust(126, "x") + "\r\n") for i in range(65536)]
        lines[0] = "needle HEAD".ljust(126, "x") + "\r\n"
        lines[-1] = "FATAL tail RuntimeError END".ljust(126, "x") + "\r\n"
        self.write("large.log", "".join(lines))
        result = self.select(["large.log"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["stats"]["bytes_read"], 8 * 1024 * 1024)
        self.assertEqual(result["stats"]["lines_available"], 65536)
        self.assertEqual(result["stats"]["lines_scanned"], es.MAX_SEARCH_LINES)
        scan = result["scan_ranges"][0]
        self.assertEqual(scan["scanned"], [
            {"start_line": 1, "end_line": 10000},
            {"start_line": 55537, "end_line": 65536}])
        self.assertEqual(scan["unscanned"], [{"start_line": 10001, "end_line": 55536}])
        self.assertTrue(any("needle HEAD" in s["text"] for s in result["snippets"]))
        self.assertIn(65536, [n for s in result["snippets"] for n in s["error_lines"]])
        for snippet in result["snippets"]:
            self.assertEqual(snippet["text"], "".join(
                lines[snippet["start_line"] - 1:snippet["end_line"]]))
            self.assertFalse(snippet["start_line"] <= 10000 < 55537 <= snippet["end_line"])
        self.assertTrue(result["truncated"]["search"])
        self.assertFalse(result["stats"]["error_coverage_complete"])
        self.assertTrue(any("middle not searched" in w for w in result["warnings"]))

    def test_real_sixteen_mib_budget_and_later_files_tail(self):
        line = b"ordinary entry".ljust(126, b"x") + b"\r\n"
        data = line * 65535 + b"FATAL final entry".ljust(126, b"x") + b"\r\n"
        for name in ("first.log", "second.log"):
            self.write(name, data)
        self.write("third.log", "needle\n")
        result = self.select(["first.log", "second.log", "third.log"])
        self.assertEqual(result["stats"]["bytes_read"], 16 * 1024 * 1024)
        self.assertEqual(result["stats"]["files_scanned"], 2)
        self.assertLessEqual(result["stats"]["lines_scanned"], es.MAX_SEARCH_LINES)
        self.assertEqual(result["skipped"], [
            {"path": "third.log", "reason": "file_or_total_byte_limit"}])
        self.assertEqual({s["path"] for s in result["snippets"]}, {"first.log", "second.log"})
        for snippet in result["snippets"]:
            self.assertIn(65536, snippet["error_lines"])
            self.assertEqual(snippet["text"].splitlines()[-1],
                             "FATAL final entry".ljust(126, "x"))

    def test_chinese_query_uses_uniform_remote_samples_and_keeps_local_evidence(self):
        lines = []
        for i in range(120):
            lines.extend([f"def fetch_record_{i}(record_id):\n",
                          "    return cache.get(record_id)\n", "\n"])
        lines[-2] = "    raise RuntimeError('cache lookup failed')\n"
        self.write("cache.py", "".join(lines))
        captured = []

        def fake(state, questions):
            payload = json.loads(state)
            captured.append(payload)
            ids = {c["id"] for c in payload["candidates"]}
            self.assertEqual(set(questions), ids)
            self.assertEqual(payload["query"], "缓存查询的实现在哪里")
            for candidate_id, question in questions.items():
                self.assertIn(f"candidate ID {candidate_id} in state.candidates", question["instructions"])
                self.assertEqual(question["type"], "noul")
            self.assertEqual(len({q["instructions"] for q in questions.values()}), len(ids))
            return {"answers": {c["id"]: {"noul": 1 if "fetch_record_0(" in c["text"] else 0}
                                for c in payload["candidates"]}}

        result = self.select(["cache.py"], "缓存查询的实现在哪里", allow_remote=True, ask=fake)
        self.assertEqual(result["mode"], "remote")
        self.assertEqual(len(captured), 1)
        sent = captured[0]["candidates"]
        indices = sorted(int(c["id"][1:]) for c in sent)
        self.assertLessEqual(len(sent), es.MAX_CANDIDATES)
        self.assertEqual(indices[0], 0)
        self.assertEqual(indices[-1], 119)
        self.assertTrue(any(50 <= i <= 70 for i in indices))
        self.assertLessEqual(max(b - a for a, b in zip(indices, indices[1:])), 8)
        self.assertTrue(any("RuntimeError" in c["text"] for c in sent))
        self.assertTrue(result["sampling"]["used"])
        self.assertTrue(result["sampling"]["incomplete"])
        self.assertEqual(result["sampling"]["semantic_coverage"], "not_established")
        self.assertTrue(any("SEMANTIC_SAMPLE_INCOMPLETE" in w for w in result["warnings"]))
        self.assertTrue(any("fetch_record_0(" in s["text"] for s in result["snippets"]))
        for snippet in result["snippets"]:
            self.assertEqual(snippet["text"], "".join(
                lines[snippet["start_line"] - 1:snippet["end_line"]]))
        ask = Mock(side_effect=AssertionError("no authorization"))
        local = self.select(["cache.py"], "缓存查询的实现在哪里", ask=ask)
        ask.assert_not_called()
        self.assertFalse(local["sampling"]["used"])
        self.assertFalse(local["remote"]["attempted"])
        self.assertTrue(all(s["error_lines"] for s in local["snippets"]))

    def test_small_cross_language_sample_still_calls_fake_and_failure_is_explicit(self):
        self.write("cache.py", "def fetch_record():\n    return cache.get(1)\n")
        ask = Mock(side_effect=EvidenceTests.answers)
        result = self.select(["cache.py"], "缓存查询", allow_remote=True, ask=ask)
        ask.assert_called_once()
        self.assertEqual(result["mode"], "remote")
        self.assertTrue(result["sampling"]["used"])
        bad = Mock(return_value={"answers": {}})
        fallback = self.select(["cache.py"], "缓存查询", allow_remote=True, ask=bad)
        self.assertEqual(fallback["mode"], "local_fallback")
        self.assertFalse(fallback["remote"]["used"])
        self.assertTrue(any("SEMANTIC_UNVERIFIED" in w for w in fallback["warnings"]))

    def test_semantic_samples_not_crowded_out_by_many_errors_or_payload_limit(self):
        blocks = []
        for i in range(120):
            label = "ERROR" if i < 40 or i == 119 else "def fetch_record"
            blocks.append(f"{label} {i} " + "x" * 200 + "\ncontext\nend\n")
        self.write("many.log", "".join(blocks))
        ask = Mock(side_effect=EvidenceTests.answers)
        result = self.select(["many.log"], "缓存查询的实现", allow_remote=True, ask=ask)
        sent = json.loads(ask.call_args.args[0])["candidates"]
        self.assertTrue(any(c["id"] == "c119" for c in sent))
        self.assertTrue(any("def fetch_record" in c["text"] for c in sent))
        self.assertEqual(result["stats"]["error_lines_found"], 41)
        self.assertEqual(result["stats"]["candidates_found"],
                         41 + sum("def fetch_record" in c["text"] for c in sent))
        with patch.object(es, "MAX_REMOTE_BYTES", 4000):
            limited = self.select(["many.log"], "缓存查询的实现", allow_remote=True, ask=ask)
        sent = json.loads(ask.call_args.args[0])["candidates"]
        self.assertTrue(limited["truncated"]["remote"])
        self.assertTrue(any("ERROR" in c["text"] for c in sent))
        self.assertTrue(any("def fetch_record" in c["text"] for c in sent))

    def test_config_source_allowed_and_sensitive_paths_or_content_still_rejected(self):
        self.write("config.py", "TIMEOUT = 30\nDEBUG = False\n")
        self.write("settings.py", "CACHE_ENABLED = True\n")
        self.write("config/loader.py", "def load_cache():\n    return {}\n")
        allowed = self.select(["config.py", "settings.py", "config/loader.py"], "cache timeout")
        self.assertEqual(allowed["skipped"], [])
        self.assertEqual(allowed["stats"]["files_scanned"], 3)
        self.assertEqual({s["path"] for s in allowed["snippets"]},
                         {"config.py", "settings.py", "config/loader.py"})
        sensitive = {
            "config.py": 'API_KEY = "synthetic-only"\n',
            "settings.py": 'PASSWORD = "synthetic-only"\n',
            "payload.json": '{"client_secret":"synthetic-only"}\n',
            ".env": "TIMEOUT=30\n", "credentials.json": "{}\n",
            "config.json": '{"timeout":30}\n', "settings.json": "{}\n",
        }
        for path, text in sensitive.items():
            self.write(path, text)
        rejected = self.select(list(sensitive))
        self.assertEqual(len(rejected["skipped"]), len(sensitive))
        reasons = {entry["path"]: entry["reason"] for entry in rejected["skipped"]}
        for path in ("config.py", "settings.py", "payload.json"):
            self.assertEqual(reasons[path], "sensitive_content")
        self.assertEqual(rejected["snippets"], [])

    def test_source_credential_references_do_not_hide_entire_file(self):
        self.write("references.mjs", "const apiKey = options.apiKey ?? process.env.TYPESAFE_API_KEY;\n"
                                     "const request = {\n  apiKey: rawOptions.apiKey,\n};\n")
        result = self.select(["references.mjs"], "request")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["stats"]["files_scanned"], 1)
        self.assertTrue(any("const request" in s["text"] for s in result["snippets"]))

    def test_source_reference_followed_by_literal_and_log_reference_remain_rejected(self):
        self.write("fallback.mjs", 'const apiKey = options.apiKey ?? "synthetic-only";\n')
        self.write("capture.log", 'apiKey = options.apiKey\n')
        self.write("literal.py", 'api_key = "synthetic-only"\n')
        result = self.select(["fallback.mjs", "capture.log", "literal.py"], "apiKey")
        self.assertEqual([s["reason"] for s in result["skipped"]], ["sensitive_content"] * 3)
        self.assertEqual(result["snippets"], [])


if __name__ == "__main__":
    unittest.main()
