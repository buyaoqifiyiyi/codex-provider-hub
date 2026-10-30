"""Offline, synthetic-only capability evidence contract tests."""
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import storage
from storage import ProfileStore


class CapabilityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-evidence-test-")
        self.addCleanup(self.temp.cleanup)
        # Never use DPAPI, the real profile root, credentials, or a transport.
        def encrypt(data, decrypt=False):
            if decrypt:
                raise AssertionError("Evidence must not decrypt credentials")
            return hashlib.sha256(b"synthetic-ciphertext:" + data).digest()
        self.encrypt = patch.object(storage, "_dpapi", side_effect=encrypt)
        self.encrypt.start()
        self.addCleanup(self.encrypt.stop)
        self.store = ProfileStore(self.temp.name)
        self.pid = self.create()["id"]

    def create(self, **kwargs):
        return self.store.save("Fixture", "responses", "https://fixture.invalid/v1", "model-a",
                               "synthetic-key", **kwargs)

    def edit(self, **kwargs):
        args = dict(name="Fixture", protocol="responses", base_url="https://fixture.invalid/v1",
                    model="model-a", provider_id=self.pid)
        args.update(kwargs)
        return self.store.save(**args)

    def record(self, purpose="tool", status="passed", effort="high", **kwargs):
        identity = self.store.capability_identity(self.pid, "model-a", effort)
        return self.store.record_capability_evidence(self.pid, "model-a", purpose, status,
                                                      effort, identity, **kwargs)

    def evidence(self):
        return self.store.capability_evidence(self.pid, "model-a")

    def receipt(self, revision=None, model="model-a", effort="high", job="fixture-job", tested_at=100):
        with self.store.atomic() as db:
            row = db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?",
                             (self.pid,)).fetchone()
            revision = revision or self.store._content_revision(db, row)
            db.execute("INSERT INTO model_probe_receipts VALUES (?,?,?,?,?,?,?)",
                       (self.pid,model,revision,"fixture-alias",effort,job,tested_at))
        return revision

    def test_persists_and_preserves_across_rename_other_models_and_reorder(self):
        self.record()
        identity = self.store.capability_identity(self.pid, "model-a", "high")
        for change in [dict(name="Renamed"), dict(models=["model-a", "model-b"]),
                       dict(model="model-b", models=["model-b", "model-a"]),
                       dict(models=["model-a"])]:
            self.edit(**change)
            self.assertEqual(self.store.capability_identity(self.pid, "model-a", "high"), identity)
            self.assertEqual(self.evidence()[0]["status"], "passed")
        self.assertEqual(ProfileStore(self.temp.name).capability_evidence(self.pid,"model-a"), self.evidence())

    def test_credential_url_protocol_and_target_changes_are_stale(self):
        self.record()
        self.edit(key="replacement-synthetic-key")
        self.assertEqual(self.evidence()[0]["status"], "stale")
        self.record()
        self.edit(base_url="https://fixture.invalid/v2")
        self.assertTrue(all(r["status"] == "stale" for r in self.evidence()))
        self.record()
        self.edit(protocol="chat_completions", key="replacement-synthetic-key")
        self.assertTrue(all(r["status"] == "stale" for r in self.evidence()))
        self.record()
        self.edit(protocol="chat_completions",model="model-b")
        self.assertTrue(all(r["status"] == "stale" for r in self.evidence()))
        self.assertEqual(self.store.capability_evidence(self.pid,"model-b"), [])
        with self.assertRaises(ValueError):
            self.store.capability_identity(self.pid,"model-a")

    def test_provider_and_effort_are_independent_including_unspecified(self):
        self.record(effort="high")
        self.record(status="failed", effort="max")
        self.record(status="error", effort=None, error_code="timeout")
        self.record(status="passed", effort="none")
        outcomes = {r["effort"]:r["status"] for r in self.evidence()}
        self.assertEqual(outcomes,{"high":"passed","max":"failed",None:"error","none":"passed"})
        other = self.create()["id"]
        self.assertEqual(self.store.capability_evidence(other,"model-a"), [])
        self.assertNotEqual(self.store.capability_identity(other,"model-a","high")["identity"],
                            self.store.capability_identity(self.pid,"model-a","high")["identity"])
        self.edit(settings=dict(reasoning_levels=["high","max"],default_effort="max"))
        self.assertEqual({r["effort"]:r["status"] for r in self.evidence()},outcomes)

    def test_all_purposes_and_errors_do_not_assert_capability_failure(self):
        for purpose in storage.CAPABILITY_PURPOSES:
            self.record(purpose=purpose)
        self.record(status="failed")
        self.record(status="error",error_code="rate_limit")
        tool = next(r for r in self.evidence() if r["purpose"] == "tool")
        self.assertEqual((tool["status"],tool["original_status"],tool["error_code"]),
                         ("error","error","rate_limit"))
        self.record(purpose="audio",status="error")
        self.assertEqual(next(r for r in self.evidence() if r["purpose"]=="audio")["error_code"],"unknown")

    def test_legacy_freezes_before_every_save_without_prior_read(self):
        self.receipt()
        self.edit(name="Renamed", models=["model-a","model-b"])
        # Current full revision differs; only the pre-save migration can retain it.
        self.assertEqual(self.evidence()[0]["status"], "passed")
        self.edit(model="model-b", models=["model-b","model-a"])
        self.edit(models=["model-a"])
        self.assertEqual(len(self.evidence()),1)
        row = self.evidence()[0]
        self.assertEqual((row["purpose"],row["source"],row["tested_at"]),
                         ("connection","legacy_connection",100))
        self.edit(key="rotated-synthetic-key")
        self.assertEqual(self.evidence()[0]["status"],"stale")

    def test_legacy_mismatch_never_laundered_by_edit(self):
        self.receipt(revision="outdated-provider-wide-revision")
        self.edit(models=["model-a","model-b"])
        self.assertEqual(self.evidence(),[])
        self.edit(name="renamed")
        self.assertEqual(self.evidence(),[])

    def test_legacy_read_validates_whole_revision_and_membership(self):
        self.receipt(effort="", job="unspecified")
        self.receipt(effort="max", job="max")
        self.receipt(effort="credential-like-invalid-effort", job="bad")
        self.receipt(model="not-selected", job="missing")
        rows=self.evidence()
        self.assertEqual({r["effort"] for r in rows},{None,"max"})
        self.assertEqual({r["purpose"] for r in rows},{"connection"})
        self.assertEqual(self.store.capability_evidence(self.pid,"not-selected"),[])
        self.record(purpose="connection",status="error",effort="max")
        self.assertEqual(next(r for r in self.evidence() if r["effort"]=="max")["status"],"error")

    def test_failed_save_rolls_back_migration_in_same_transaction(self):
        self.receipt()
        original = self.store._freeze_capability_receipts
        def freeze_then_fail(db, provider_id):
            original(db,provider_id)
            raise RuntimeError("synthetic failure")
        with patch.object(self.store,"_freeze_capability_receipts",side_effect=freeze_then_fail):
            with self.assertRaises(RuntimeError):
                self.edit(name="not-saved")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM capability_evidence").fetchone()[0],0)
        self.assertEqual(self.store.list()[0]["name"],"Fixture")
        self.assertEqual(self.evidence()[0]["status"],"passed")

    def test_concurrent_target_mismatch_rejects_but_unrelated_edit_allows(self):
        identity=self.store.capability_identity(self.pid,"model-a","high")
        other=ProfileStore(self.temp.name)
        other.save("Another name","responses","https://fixture.invalid/v1","model-a",
                   provider_id=self.pid,models=["model-a","model-b"])
        self.store.record_capability_evidence(self.pid,"model-a","tool","passed","high",identity)
        other.save("Fixture","responses","https://fixture.invalid/v1","model-a",
                   "changed-synthetic-key",self.pid)
        with self.assertRaisesRegex(ValueError,"身份已改变"):
            self.store.record_capability_evidence(self.pid,"model-a","image","passed","high",identity)
        with self.assertRaises(ValueError):
            self.store.capability_identity(self.pid,"model-a","high",expected_identity=identity)
        with self.assertRaises(ValueError):
            self.store.record_capability_evidence(self.pid,"model-a","image","passed","max",identity)
        self.assertEqual({r["purpose"] for r in self.evidence()},{"tool"})

    def test_metadata_allowlist_rejects_sensitive_payload_without_echoing(self):
        marker="private-request-and-secret-value"
        for kwargs in [dict(purpose=marker),dict(status=marker),dict(effort=marker),
                       dict(source=marker),dict(error_code=marker),dict(tested_at=marker),
                       dict(error_code="timeout",status="failed")]:
            args=dict(provider_id=self.pid,model="model-a",purpose="tool",status="error")
            args.update(kwargs)
            with self.assertRaises(ValueError) as ctx:
                self.store.record_capability_evidence(**args)
            self.assertNotIn(marker,str(ctx.exception))
        with self.assertRaises(TypeError):
            self.store.record_capability_evidence(self.pid,"model-a","tool","passed",request_body=marker)
        self.record()
        with self.store.connection() as db:
            rows=db.execute("SELECT * FROM capability_evidence").fetchall()
        self.assertNotIn(marker,str(rows))
        self.assertNotIn("synthetic-key",str(rows))
        self.assertNotIn("encrypted_key",json.dumps(self.store.capability_identity(self.pid,"model-a")))

    def test_additive_v2_upgrade_preserves_routes_receipts_and_guards(self):
        self.store.set_route("fixture-route",self.pid,"model-a")
        self.receipt()
        with self.store.connection() as db, db:
            db.execute("DROP TABLE capability_evidence")
        reopened=ProfileStore(self.temp.name)
        self.assertEqual(reopened.get_route("fixture-route")["model"],"model-a")
        self.assertEqual(reopened.capability_evidence(self.pid,"model-a")[0]["status"],"passed")
        with reopened.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM model_probe_receipts").fetchone()[0],1)
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE providers SET name='old writer'")

    def test_channels_are_distinct_in_identity_write_and_filtered_read(self):
        self.edit(protocol="gemini_native",key="synthetic-media-key")
        identities = {}
        for channel, status in [("connection","passed"),("subagent_gateway","failed"),("gemini_media","error")]:
            identity=self.store.capability_identity(self.pid,"model-a",channel=channel)
            identities[channel]=identity
            self.store.record_capability_evidence(self.pid,"model-a","video",status,
                                                  expected_identity=identity,channel=channel)
            rows=self.store.capability_evidence(self.pid,"model-a",channel=channel)
            self.assertEqual(len(rows),1)
            self.assertEqual((rows[0]["channel"],rows[0]["status"],rows[0]["effort"]),(channel,status,None))
        self.assertEqual(len({i["identity"] for i in identities.values()}),3)
        self.assertEqual(len(self.evidence()),3)
        with self.assertRaises(ValueError):
            self.store.record_capability_evidence(self.pid,"model-a","tool","passed",
                                                  expected_identity=identities["connection"],channel="subagent_gateway")
        for effort in ["high","max","none"]:
            with self.assertRaises(ValueError):
                self.store.capability_identity(self.pid,"model-a",effort,channel="gemini_media")
            with self.assertRaises(ValueError):
                self.store.record_capability_evidence(self.pid,"model-a","video","passed",effort,channel="gemini_media")
        self.edit(protocol="responses",key="synthetic-api-key")
        with self.assertRaises(ValueError):
            self.store.capability_identity(self.pid,"model-a",channel="gemini_media")

    def test_legacy_channel_is_only_connection(self):
        self.receipt()
        self.assertEqual(self.store.capability_evidence(self.pid,"model-a",channel="subagent_gateway"),[])
        self.assertEqual(self.store.capability_evidence(self.pid,"model-a",channel="gemini_media"),[])
        self.assertEqual(self.evidence()[0]["channel"],"connection")

    def test_invalid_channel_rejected_without_echo(self):
        marker="private-channel-value"
        for call in [lambda: self.store.capability_identity(self.pid,"model-a",channel=marker),
                     lambda: self.store.capability_evidence(self.pid,"model-a",channel=marker),
                     lambda: self.store.record_capability_evidence(self.pid,"model-a","tool","passed",channel=marker)]:
            with self.assertRaises(ValueError) as ctx:
                call()
            self.assertNotIn(marker,str(ctx.exception))

    def test_legacy_provider_wide_revision_not_old_binding_revision(self):
        with self.store.connection() as db:
            row=db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?",(self.pid,)).fetchone()
        self.receipt(revision=self.store._revision(row))
        self.edit(name="renamed",models=["model-a","model-b"])
        self.assertEqual(self.evidence(),[])

    def test_legacy_migration_keeps_both_models_when_one_removed(self):
        self.edit(models=["model-a","model-b"])
        self.receipt(model="model-a",job="a")
        self.receipt(model="model-b",job="b")
        self.edit(models=["model-a"])
        self.assertEqual(self.evidence()[0]["status"],"passed")
        self.assertEqual(self.store.capability_evidence(self.pid,"model-b")[0]["status"],"stale")

    def test_evidence_does_not_change_route_or_existing_authorization(self):
        self.store.set_route("fixture-route",self.pid,"model-a")
        fingerprint=self.store.edit_fingerprint(self.pid)
        route=self.store.get_route("fixture-route")
        with self.store.connection() as db:
            profile=self.store._read_profile(db,self.pid,"model-a")[0]
        self.record()
        self.assertEqual(self.store.edit_fingerprint(self.pid),fingerprint)
        self.assertEqual(self.store.get_route("fixture-route"),route)
        self.assertEqual(self.store.confirmed_efforts(profile),set())

    def test_older_completion_does_not_replace_newer_outcome(self):
        self.record(status="error",tested_at=200)
        self.record(status="passed",tested_at=100)
        self.assertEqual(self.evidence()[0]["status"],"error")
        self.assertEqual(self.evidence()[0]["tested_at"],200)


if __name__ == "__main__":
    unittest.main()
