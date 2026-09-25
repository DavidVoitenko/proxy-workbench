"""F02 and дефект 11: collections, membership, and an isolated personal list.

A personal collection is a different row from the public base and starts empty; the
legacy migration keeps an honest origin instead of labelling collected addresses as
"my own".
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db
from proxy_workbench import proxytool as proxytool_module

PUBLIC_ADDRESSES = ("http://1.2.3.4:8080", "socks5://5.6.7.8:1080")
OWN_ADDRESSES = ("http://10.0.0.1:3128", "http://my-host.example:8080")


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db_path = self.home / "data" / db.DB_FILENAME

    def legacy(self, addresses=PUBLIC_ADDRESSES):
        conn = proxytool_module.open_db(self.db_path)
        for proxy in addresses:
            conn.execute("INSERT INTO candidates VALUES (?)", (proxy,))
        conn.commit()
        conn.close()
        return db.migrate(self.db_path)

    def fresh(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    # --- the default state --------------------------------------------------

    def test_a_fresh_database_has_a_public_base_and_no_personal_collections(self):
        conn = self.fresh()
        collections = db.list_collections(conn)
        self.assertEqual([(row["id"], row["kind"], row["name"]) for row in collections],
                         [(db.PUBLIC_COLLECTION_ID, "public", db.PUBLIC_COLLECTION_NAME),
                          (db.LEGACY_COLLECTION_ID, "public", db.LEGACY_COLLECTION_NAME)])
        self.assertEqual(db.collection_members(conn, db.PUBLIC_COLLECTION_ID), [])

    def test_collection_kind_is_checked(self):
        conn = self.fresh()
        for kind in ("private", "public"):
            db.create_collection(conn, f"List {kind}", kind=kind)
        with self.assertRaises(db.DbError):
            db.create_collection(conn, "Shared", kind="shared")
        with self.assertRaises(db.DbError):
            db.create_collection(conn, "   ")
        with self.assertRaises(db.DbError):
            db.add_member(db.PUBLIC_COLLECTION_ID, db.endpoint_id(OWN_ADDRESSES[0]),
                          conn, origin="telepathy")

    # --- дефект 11: a personal list does not see the old public base -------

    def test_personal_collection_created_after_a_legacy_migration_is_empty(self):
        self.legacy()
        conn = self.fresh()
        self.assertEqual(len(db.collection_members(conn, db.LEGACY_COLLECTION_ID)),
                         len(PUBLIC_ADDRESSES))
        mine = db.create_collection(conn, "Мои прокси", kind="private")
        self.assertEqual(db.collection_members(conn, mine), [])
        self.assertEqual(db.get_collection(conn, mine)["kind"], "private")
        self.assertNotEqual(mine, db.PUBLIC_COLLECTION_ID)
        self.assertNotIn(mine, [row["id"] for row in db.collection_members(conn, mine)])

    def test_import_into_a_personal_collection_does_not_reveal_public_addresses(self):
        self.legacy()
        conn = self.fresh()
        mine = db.create_collection(conn, "Мои прокси", kind="private")
        for proxy in OWN_ADDRESSES:
            endpoint = db.upsert_endpoint(conn, proxy)
            db.add_member(conn, mine, endpoint, origin="import")
        members = db.collection_members(conn, mine)
        self.assertEqual([row["canonical"] for row in members], sorted(OWN_ADDRESSES))
        self.assertEqual({row["origin"] for row in members}, {"import"})
        for row in members:
            self.assertNotIn(row["canonical"], PUBLIC_ADDRESSES)
        # The public side is untouched by the import.
        self.assertEqual([row["canonical"] for row in db.collection_members(conn, db.LEGACY_COLLECTION_ID)],
                         sorted(PUBLIC_ADDRESSES))

    def test_two_personal_collections_do_not_see_each_other(self):
        self.legacy()
        conn = self.fresh()
        first = db.create_collection(conn, "Работа", kind="private")
        second = db.create_collection(conn, "Дом", kind="private")
        shared = db.upsert_endpoint(conn, OWN_ADDRESSES[0])
        db.add_member(conn, first, shared, origin="manual")
        self.assertEqual([row["canonical"] for row in db.collection_members(conn, first)],
                         [OWN_ADDRESSES[0]])
        self.assertEqual(db.collection_members(conn, second), [])
        self.assertEqual(db.collection_members(conn, db.LEGACY_COLLECTION_ID),
                         [{"id": row["id"], "canonical": row["canonical"],
                           "origin": "legacy", "added_at": row["added_at"]}
                          for row in db.collection_members(conn, db.LEGACY_COLLECTION_ID)])

    # --- many-to-many -------------------------------------------------------

    def test_one_endpoint_can_live_in_several_collections(self):
        self.legacy()
        conn = self.fresh()
        mine = db.create_collection(conn, "Мои прокси", kind="private")
        shared = db.upsert_endpoint(conn, OWN_ADDRESSES[0])
        db.add_member(conn, mine, shared)
        db.add_member(conn, db.LEGACY_COLLECTION_ID, shared, origin="public")
        self.assertEqual(db.endpoint_collections(conn, shared),
                         sorted([db.LEGACY_COLLECTION_ID, mine]))

    def test_removing_a_membership_keeps_the_address_in_other_lists(self):
        self.legacy()
        conn = self.fresh()
        mine = db.create_collection(conn, "Мои прокси", kind="private")
        shared = db.upsert_endpoint(conn, OWN_ADDRESSES[0])
        db.add_member(conn, mine, shared)
        db.add_member(conn, db.LEGACY_COLLECTION_ID, shared, origin="public")

        self.assertTrue(db.remove_member(conn, mine, shared))
        self.assertEqual(db.collection_members(conn, mine), [])
        self.assertEqual([row["canonical"] for row in db.collection_members(conn, db.LEGACY_COLLECTION_ID)],
                         sorted(PUBLIC_ADDRESSES + (OWN_ADDRESSES[0],)))
        # The endpoint row itself is still there for the other list to use.
        self.assertEqual(conn.execute("SELECT canonical FROM endpoints WHERE id = ?",
                                      (shared,)).fetchone()[0], OWN_ADDRESSES[0])
        self.assertFalse(db.remove_member(conn, mine, shared))

    def test_membership_is_explicit_many_to_many_with_foreign_keys(self):
        conn = self.fresh()
        self.assertEqual(db.primary_key(conn, "membership"), ["collection_id", "endpoint_id"])
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(membership)")}
        self.assertEqual(info["collection_id"][2], "TEXT")
        foreign = {row[2]: row[4] for row in conn.execute("PRAGMA foreign_key_list(membership)")}
        self.assertEqual(foreign, {"collections": "id", "endpoints": "id"})
        endpoint = db.upsert_endpoint(conn, OWN_ADDRESSES[0])
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_member(conn, "no-such-collection", endpoint)
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_member(conn, db.PUBLIC_COLLECTION_ID, "no-such-endpoint")

    def test_adding_the_same_endpoint_twice_keeps_one_row_and_the_first_origin(self):
        conn = self.fresh()
        mine = db.create_collection(conn, "Мои прокси", kind="private")
        endpoint = db.upsert_endpoint(conn, OWN_ADDRESSES[0])
        db.add_member(conn, mine, endpoint, origin="import", now=100.0)
        db.add_member(conn, mine, endpoint, origin="manual", now=200.0)
        members = db.collection_members(conn, mine)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["origin"], "import")
        self.assertEqual(members[0]["added_at"], 100.0)

    # --- lifecycle ----------------------------------------------------------

    def test_rename_and_archive(self):
        conn = self.fresh()
        mine = db.create_collection(conn, "Черновик", kind="private")
        db.rename_collection(conn, mine, "Рабочие")
        self.assertEqual(db.get_collection(conn, mine)["name"], "Рабочие")
        db.archive_collection(conn, mine, now=500.0)
        self.assertNotIn(mine, [row["id"] for row in db.list_collections(conn)])
        self.assertIn(mine, [row["id"] for row in db.list_collections(conn, include_archived=True)])
        # Members survive the archive: archiving hides a list, it does not delete data.
        endpoint = db.upsert_endpoint(conn, OWN_ADDRESSES[0])
        db.add_member(conn, mine, endpoint)
        db.archive_collection(conn, mine)
        self.assertEqual(len(db.collection_members(conn, mine)), 1)
        with self.assertRaises(db.DbError):
            db.rename_collection(conn, "no-such-collection", "x")
        with self.assertRaises(db.DbError):
            db.archive_collection(conn, "no-such-collection")

    def test_endpoint_identity_is_a_function_of_the_canonical_string(self):
        conn = self.fresh()
        first = db.upsert_endpoint(conn, OWN_ADDRESSES[0], host="my-host.example", port=8080)
        self.assertEqual(first, db.endpoint_id(OWN_ADDRESSES[0]))
        self.assertEqual(db.upsert_endpoint(conn, OWN_ADDRESSES[0]), first)
        self.assertEqual(conn.execute("SELECT count(*) FROM endpoints").fetchone()[0], 1)
        row = conn.execute("SELECT host, port, country FROM endpoints WHERE id = ?",
                           (first,)).fetchone()
        self.assertEqual((row["host"], row["port"], row["country"]),
                         ("my-host.example", 8080, None))
        # One canonical string is one endpoint: the UNIQUE constraint is the last
        # line of defence when two callers disagree about the id.
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO endpoints(id, canonical) VALUES (?,?)",
                         ("some-other-id", OWN_ADDRESSES[0]))
        # A different spelling of the same address is a different row until the caller
        # normalizes it, which is why `canonical` is the caller's job, not this module's.
        self.assertNotEqual(db.endpoint_id("HTTP://MY-HOST.EXAMPLE:8080"),
                            db.endpoint_id(OWN_ADDRESSES[1]))

    def test_legacy_summary_reports_the_migration(self):
        self.legacy()
        conn = self.fresh()
        summary = db.legacy_summary(conn)
        self.assertEqual(summary["candidates"], len(PUBLIC_ADDRESSES))
        self.assertEqual(summary["endpoints"], len(PUBLIC_ADDRESSES))
        self.assertEqual(summary["legacy_members"], len(PUBLIC_ADDRESSES))
        self.assertEqual(summary["public_base_members"], 0)


if __name__ == '__main__':
    unittest.main()
