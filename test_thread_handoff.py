import json
from contextlib import closing
from email.message import Message
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thread_handoff import verified_compacted_thread

THREAD = "11111111-1111-4111-8111-111111111111"
NATIVE = "synthetic-opaque-checkpoint"


class HandoffIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="handoff-id-test-")
        self.home = Path(self.tmp.name)
        self.transcript = self.home/"sessions"/"fixture.jsonl"
        self.transcript.parent.mkdir()
        with closing(sqlite3.connect(self.home/"state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?)", (THREAD, str(self.transcript)))
            db.commit()
        self.headers = Message()
        self.headers['thread-id'] = THREAD
        self.headers['session-id'] = THREAD
        self.request = {"input": [{"type": "compaction", "encrypted_content": NATIVE}]}
        self.write_checkpoint(NATIVE)

    def tearDown(self): self.tmp.cleanup()

    def write_checkpoint(self, value, *, append=False):
        with self.transcript.open("a" if append else "w", encoding="utf8") as output:
            if not append:
                output.write(json.dumps({"type": "session_meta", "payload": {"id": THREAD}})+"\n")
            output.write(json.dumps({"type": "compacted", "payload": {"replacement_history": [
                {"type": "compaction", "encrypted_content": value}]}})+"\n")

    def resolve(self):
        return verified_compacted_thread(self.headers, self.request, codex_home=self.home)

    def test_matching_current_chat_and_checkpoint(self):
        self.assertEqual(self.resolve(), THREAD)

    def test_mismatch_duplicate_or_missing_headers_do_not_identify_a_chat(self):
        for session in ("22222222-2222-4222-8222-222222222222", "../other", ""):
            with self.subTest(session=session):
                self.headers.replace_header('session-id', session)
                self.assertIsNone(self.resolve())
        self.headers.replace_header('session-id', THREAD)
        self.headers['thread-id'] = THREAD
        self.assertIsNone(self.resolve())

    def test_stale_or_different_checkpoint_does_not_identify_a_chat(self):
        self.write_checkpoint("different", append=True)
        self.assertIsNone(self.resolve())

    def test_history_outside_codex_home_is_never_read(self):
        with tempfile.TemporaryDirectory(prefix="handoff-outside-") as outer:
            other = Path(outer)/"other.jsonl"; other.write_bytes(self.transcript.read_bytes())
            with closing(sqlite3.connect(self.home/"state_5.sqlite")) as db:
                db.execute("UPDATE threads SET rollout_path=?", (str(other),))
                db.commit()
            self.assertIsNone(self.resolve())

    def test_local_envelope_needs_no_history_lookup(self):
        self.request['input'][0]['encrypted_content'] = 'ocx1:c3ludGhldGlj'
        self.assertIsNone(self.resolve())

    @unittest.skipUnless(os.name == 'nt', 'Windows extended path')
    def test_extended_windows_transcript_path_is_accepted(self):
        with closing(sqlite3.connect(self.home/"state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", ("\\\\?\\" + str(self.transcript.resolve()),))
            db.commit()
        self.assertEqual(self.resolve(), THREAD)

    def test_every_opaque_checkpoint_must_match_latest_history(self):
        self.request['input'].append({"type":"compaction", "encrypted_content":"not-in-history"})
        self.assertIsNone(self.resolve())

    def test_wrong_session_metadata_and_truncated_history_do_not_match(self):
        self.transcript.write_text(json.dumps({"type":"session_meta", "payload":{"id":"another"}})+"\n"+
                                   json.dumps({"type":"compacted", "payload":{"replacement_history":self.request['input']}})+"\n",
                                   encoding="utf8")
        self.assertIsNone(self.resolve())
        self.write_checkpoint(NATIVE)
        with self.transcript.open("ab") as output: output.write(b'{"partial":')
        self.assertIsNone(self.resolve())

    def test_history_changed_during_read_is_not_attributed(self):
        original = json.loads
        changed = False
        def concurrent_append(raw):
            nonlocal changed
            value = original(raw)
            if value.get("type") == "compacted" and not changed:
                changed = True
                with self.transcript.open("ab") as output:
                    output.write(b'{"type":"event_msg","payload":{}}\n')
            return value
        with patch("thread_handoff.json.loads", side_effect=concurrent_append):
            self.assertIsNone(self.resolve())


if __name__ == '__main__': unittest.main()
