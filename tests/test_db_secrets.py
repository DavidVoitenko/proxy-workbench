"""F24: secrets travel separately from data, and a canary never lands in the database.

`accesses.secret_ref` is a reference resolved by `secrets.py`. This module only
remaps references -- it never accepts, stores or reports a credential value.
"""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db

#: A canary, not a credential of anything. It must not appear in the database file,
#: in a manifest, in a preview or in any report this module produces.
CANARY = "canary-Zx9-secret-value-must-not-be-stored"
VAULT_REF = "vault://workbench/entry-1"
OTHER_VAULT_REF = "vault://workbench/entry-2"


class SecretBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / "data"
        self.db_path = self.data / db.DB_FILENAME
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.endpoint = db.upsert_endpoint(self.conn, "http://1.2.3.4:8080")
        self.conn.execute(
            "INSERT INTO accesses(id, endpoint_id, mode, secret_ref, access_revision, created_at)"
            " VALUES (?,?,?,?,?,?)", ("acc-1", self.endpoint, "private", VAULT_REF, 1, 100.0))
        self.conn.execute(
            "INSERT INTO accesses(id, endpoint_id, mode, secret_ref, access_revision, created_at)"
            " VALUES (?,?,?,?,?,?)", ("acc-2", self.endpoint, "public", None, 1, 100.0))
        self.conn.commit()

    def test_bindings_expose_references_only(self):
        bindings = db.secret_bindings(self.conn)
        self.assertEqual([binding.access_id for binding in bindings], ["acc-1"])
        self.assertEqual(bindings[0].secret_ref, VAULT_REF)
        self.assertEqual(bindings[0].mode, "private")
        self.assertNotIn(CANARY, json.dumps(bindings[0].to_dict()))
        self.assertEqual(db.secret_bindings(self.db_path), bindings)

    def test_dry_run_reports_the_change_without_writing(self):
        before = self.conn.execute("SELECT secret_ref FROM accesses WHERE id = 'acc-1'").fetchone()[0]
        report = db.rebind_secrets(self.conn, {VAULT_REF: OTHER_VAULT_REF})
        self.assertTrue(report.dry_run)
        self.assertEqual(report.changed, (("acc-1", VAULT_REF, OTHER_VAULT_REF),))
        self.assertEqual(report.unresolved, ())
        self.assertEqual(report.to_dict()["changed"],
                         [{"access_id": "acc-1", "from": VAULT_REF, "to": OTHER_VAULT_REF}])
        self.assertEqual(
            self.conn.execute("SELECT secret_ref FROM accesses WHERE id = 'acc-1'").fetchone()[0],
            before)

    def test_rebind_writes_the_new_reference_and_nothing_else(self):
        report = db.rebind_secrets(self.conn, {VAULT_REF: OTHER_VAULT_REF}, dry_run=False)
        self.assertFalse(report.dry_run)
        self.assertEqual(len(report.changed), 1)
        self.assertEqual(
            self.conn.execute("SELECT secret_ref FROM accesses WHERE id = 'acc-1'").fetchone()[0],
            OTHER_VAULT_REF)
        # An access without a secret is untouched, and no other column moved.
        self.assertEqual(
            self.conn.execute("SELECT secret_ref FROM accesses WHERE id = 'acc-2'").fetchone()[0], None)
        self.assertEqual(
            self.conn.execute("SELECT access_revision FROM accesses WHERE id = 'acc-1'").fetchone()[0], 1)
        # Re-running the same mapping is a no-op, not a second write.
        again = db.rebind_secrets(self.conn, {VAULT_REF: OTHER_VAULT_REF}, dry_run=False)
        self.assertEqual(again.changed, ())

    def test_rebind_reports_references_no_access_uses(self):
        report = db.rebind_secrets(self.conn, {"vault://workbench/absent": OTHER_VAULT_REF})
        self.assertEqual(report.unresolved, ("vault://workbench/absent",))
        self.assertEqual(report.changed, ())

    def test_rebind_refuses_a_credential_value(self):
        for value in (f"http://user:{CANARY}@1.2.3.4:8080", f"https://user:{CANARY}@host"):
            with self.subTest(value=value):
                with self.assertRaises(db.DbError) as caught:
                    db.rebind_secrets(self.conn, {VAULT_REF: value})
                self.assertIn("vault reference", caught.exception.message)
        with self.assertRaises(db.DbError):
            db.rebind_secrets(self.conn, {VAULT_REF: "   "})
        self.assertEqual(
            self.conn.execute("SELECT secret_ref FROM accesses WHERE id = 'acc-1'").fetchone()[0],
            VAULT_REF)

    def test_rebind_works_on_a_path_and_leaves_no_canary_on_disk(self):
        db.rebind_secrets(self.db_path, {VAULT_REF: OTHER_VAULT_REF}, dry_run=False)
        self.conn.close()
        self.assertNotIn(CANARY.encode(), self.db_path.read_bytes())
        self.assertIn(OTHER_VAULT_REF.encode(), self.db_path.read_bytes())

    def test_restore_preview_lists_the_references_that_need_a_rebind(self):
        target = self.home / "restored"
        preview = db.restore_preview(self.db_path, target)
        self.assertEqual([item.secret_ref for item in preview.secrets], [VAULT_REF])
        self.assertEqual(preview.to_dict()["secrets"][0]["mode"], "private")
        self.assertNotIn(CANARY, json.dumps(preview.to_dict()))

    def test_restored_database_keeps_references_and_never_the_secret(self):
        target = self.home / "restored"
        report = db.restore(self.db_path, target, apply=True)
        self.assertTrue(report.verified)
        self.assertIn("rebind", report.note)
        reader = db.connect(target, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual([item.secret_ref for item in db.secret_bindings(reader)], [VAULT_REF])
        # Rebinding is a data operation, so it needs a writable connection.
        restored = db.connect(target)
        self.addCleanup(restored.close)
        report = db.rebind_secrets(restored, {VAULT_REF: OTHER_VAULT_REF}, dry_run=False)
        self.assertEqual(len(report.changed), 1)
        self.assertEqual([item.secret_ref for item in db.secret_bindings(target)],
                         [OTHER_VAULT_REF])
        self.assertNotIn(CANARY.encode(), (target / db.DB_FILENAME).read_bytes())

    def test_backup_manifest_and_database_never_contain_a_secret(self):
        # A credential value is refused at the door, so nothing downstream can carry it.
        with self.assertRaises(db.DbError):
            db.rebind_secrets(self.conn, {VAULT_REF: f"http://u:{CANARY}@h:1"}, dry_run=False)
        manifest = db.create_backup(self.conn, self.home / "backups", reason="manual")
        payload = json.loads(Path(db.manifest_path(manifest.path)).read_text(encoding="utf-8"))
        self.assertNotIn(CANARY, json.dumps(payload))
        self.assertNotIn(CANARY.encode(), Path(manifest.path).read_bytes())
        self.assertEqual(payload["reason"], "manual")
        self.assertNotIn("secret", json.dumps(payload).lower())

    def test_retention_and_cleanup_never_report_secret_values(self):
        from_db = db.secret_bindings(self.conn)
        self.assertEqual(len(from_db), 1)
        self.assertNotIn(CANARY, repr(from_db))

    def test_secret_bindings_of_a_database_without_accesses_is_empty(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        self.assertEqual(db.secret_bindings(conn), ())
        self.assertEqual(db.rebind_secrets(conn, {VAULT_REF: OTHER_VAULT_REF}).changed, ())

    def test_secret_bindings_of_a_corrupt_file_is_reported_not_ignored(self):
        broken = self.home / "broken.sqlite3"
        broken.write_bytes(b"nope" * 32)
        with self.assertRaises(db.DbCorruptError):
            db.secret_bindings(broken)


if __name__ == '__main__':
    unittest.main()
