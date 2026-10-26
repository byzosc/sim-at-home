import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_store import (
    InboxStore,
    StoreError,
    readonly_snapshot_ready_for_device_cleanup,
)


class InboxStoreTests(unittest.TestCase):
    def store_message(self, store: InboxStore, **overrides):
        values = {
            "instance_id": "modem-1",
            "message_key": "sha256:one",
            "modem_id": "1",
            "number": "13800000000",
            "content": "hello",
            "message_date": "2026-07-27 12:00:00",
            "direction": "received",
            "channels": ("bark",),
        }
        values.update(overrides)
        return store.store_message(**values)

    def test_database_permissions_and_quick_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state" / "inbox.sqlite3"
            with InboxStore(path) as store:
                self.store_message(store)
                result = store.connection.execute("PRAGMA quick_check").fetchone()[0]
                self.assertEqual(result, "ok")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_only_named_unique_conflict_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.sqlite3"
            with InboxStore(path) as store:
                first = self.store_message(store)
                sequence_before = store.connection.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name = 'messages'"
                ).fetchone()[0]
                revision_before = store.connection.execute(
                    "SELECT value FROM metadata WHERE key = 'view_revision'"
                ).fetchone()[0]
                duplicate = self.store_message(store)
                self.assertTrue(first.inserted)
                self.assertFalse(duplicate.inserted)
                self.assertEqual(first.seq, duplicate.seq)
                self.assertEqual(
                    store.connection.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name = 'messages'"
                    ).fetchone()[0],
                    sequence_before,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT value FROM metadata WHERE key = 'view_revision'"
                    ).fetchone()[0],
                    revision_before,
                )

                with self.assertRaisesRegex(StoreError, "CHECK constraint"):
                    self.store_message(
                        store,
                        message_key="sha256:invalid",
                        direction="invalid",
                    )
                self.assertEqual(store.message_count("modem-1"), 1)

    def test_autoincrement_sequence_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.sqlite3"
            with InboxStore(path) as store:
                first = self.store_message(store)
                store.connection.execute(
                    "DELETE FROM messages WHERE seq = ?",
                    (first.seq,),
                )
                second = self.store_message(
                    store,
                    message_key="sha256:two",
                    modem_id="2",
                )
                self.assertGreater(second.seq, first.seq)

    def test_modem_page_saturation_alert_is_rate_limited_and_resets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.sqlite3"
            with InboxStore(path) as store:
                with patch("sms_store.time.time", return_value=1000):
                    self.assertTrue(
                        store.record_modem_page_status("modem-1", 500, 500)
                    )
                    self.assertFalse(
                        store.record_modem_page_status("modem-1", 500, 500)
                    )
                with patch("sms_store.time.time", return_value=1000 + 21600):
                    self.assertTrue(
                        store.record_modem_page_status("modem-1", 500, 500)
                    )
                with patch("sms_store.time.time", return_value=30000):
                    self.assertFalse(
                        store.record_modem_page_status("modem-1", 499, 500)
                    )
                    self.assertTrue(
                        store.record_modem_page_status("modem-1", 500, 500)
                    )

                status = store.modem_page_status("modem-1")
                self.assertIsNotNone(status)
                self.assertEqual(status.message_count, 500)
                self.assertEqual(status.page_limit, 500)
                self.assertTrue(status.saturated)

    def test_instance_label_changes_view_revision_only_when_needed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.sqlite3"
            with InboxStore(path) as store:
                before = int(
                    store.connection.execute(
                        "SELECT value FROM metadata WHERE key = 'view_revision'"
                    ).fetchone()[0]
                )
                self.assertTrue(store.set_instance_label("modem-1", "测试卡"))
                changed = int(
                    store.connection.execute(
                        "SELECT value FROM metadata WHERE key = 'view_revision'"
                    ).fetchone()[0]
                )
                self.assertGreater(changed, before)
                self.assertFalse(store.set_instance_label("modem-1", "测试卡"))
                unchanged = int(
                    store.connection.execute(
                        "SELECT value FROM metadata WHERE key = 'view_revision'"
                    ).fetchone()[0]
                )
                self.assertEqual(unchanged, changed)

    def test_readonly_snapshot_is_atomic_private_and_skips_unchanged_view(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "state" / "inbox.sqlite3"
            snapshot_dir = root / "view"
            snapshot_dir.mkdir()
            snapshot_path = snapshot_dir / "inbox.sqlite3"

            with InboxStore(path) as store:
                store.set_instance_label("modem-1", "测试卡")
                first = self.store_message(store)
                self.assertTrue(store.export_readonly_snapshot(snapshot_path))
                first_stat = snapshot_path.stat()
                self.assertEqual(first_stat.st_mode & 0o777, 0o640)
                self.assertEqual(first_stat.st_gid, snapshot_dir.stat().st_gid)
                self.assertFalse(store.export_readonly_snapshot(snapshot_path))
                self.assertEqual(snapshot_path.stat().st_ino, first_stat.st_ino)

                snapshot_path.chmod(0o600)
                self.assertTrue(store.export_readonly_snapshot(snapshot_path))
                repaired_stat = snapshot_path.stat()
                self.assertEqual(repaired_stat.st_mode & 0o777, 0o640)
                self.assertNotEqual(repaired_stat.st_ino, first_stat.st_ino)

                self.store_message(
                    store,
                    message_key="sha256:two",
                    modem_id="2",
                    content="world",
                )
                self.assertTrue(store.export_readonly_snapshot(snapshot_path))
                self.assertNotEqual(snapshot_path.stat().st_ino, repaired_stat.st_ino)

            connection = sqlite3.connect(
                snapshot_path.resolve().as_uri() + "?mode=ro",
                uri=True,
            )
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT label FROM instance_labels WHERE instance_id = ?",
                        ("modem-1",),
                    ).fetchone()[0],
                    "测试卡",
                )
                self.assertEqual(
                    connection.execute("PRAGMA quick_check").fetchone()[0],
                    "ok",
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(
                        "DELETE FROM messages WHERE seq = ?",
                        (first.seq,),
                    )
            finally:
                connection.close()

    def test_snapshot_requires_existing_real_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with InboxStore(root / "inbox.sqlite3") as store:
                with self.assertRaisesRegex(StoreError, "directory is unavailable"):
                    store.export_readonly_snapshot(
                        root / "missing" / "inbox.sqlite3"
                    )
                real = root / "real"
                real.mkdir()
                link = root / "link"
                link.symlink_to(real, target_is_directory=True)
                with self.assertRaisesRegex(StoreError, "real directory"):
                    store.export_readonly_snapshot(link / "inbox.sqlite3")
                unsafe = root / "unsafe"
                unsafe.mkdir()
                unsafe.chmod(0o770)
                with self.assertRaisesRegex(StoreError, "owner-controlled"):
                    store.export_readonly_snapshot(
                        unsafe / "inbox.sqlite3"
                    )

    def test_snapshot_rejects_authoritative_database_aliases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state" / "inbox.sqlite3"
            with InboxStore(database) as store:
                original = database.stat()
                with self.assertRaisesRegex(
                    StoreError,
                    "must differ",
                ):
                    store.export_readonly_snapshot(database)

                hardlink = database.parent / "hardlink.sqlite3"
                hardlink.hardlink_to(database)
                try:
                    with self.assertRaisesRegex(
                        StoreError,
                        "must not alias",
                    ):
                        store.export_readonly_snapshot(hardlink)
                finally:
                    hardlink.unlink()

                symlink = database.parent / "symlink.sqlite3"
                symlink.symlink_to(database)
                try:
                    with self.assertRaisesRegex(
                        StoreError,
                        "must not alias",
                    ):
                        store.export_readonly_snapshot(symlink)
                finally:
                    symlink.unlink()

                after = database.stat()
                self.assertEqual(after.st_ino, original.st_ino)
                self.assertEqual(after.st_mode & 0o777, 0o600)

    def test_retention_is_scoped_to_one_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inbox.sqlite3"
            with InboxStore(path) as store:
                self.store_message(
                    store,
                    instance_id="modem-1",
                    channels=(),
                )
                self.store_message(
                    store,
                    instance_id="modem-2",
                    message_key="sha256:two",
                    modem_id="2",
                    channels=(),
                )
                store.connection.execute(
                    "UPDATE messages SET discovered_at = 0"
                )

                self.assertEqual(
                    store.run_daily_maintenance("modem-1", 1),
                    1,
                )
                self.assertEqual(store.message_count("modem-1"), 0)
                self.assertEqual(store.message_count("modem-2"), 1)
                self.assertEqual(
                    store.run_daily_maintenance("modem-2", 1),
                    1,
                )
                self.assertEqual(store.message_count("modem-2"), 0)

    def test_device_cleanup_requires_payload_and_accepted_notifications_in_both_copies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state" / "inbox.sqlite3"
            snapshot_dir = root / "view"
            snapshot_dir.mkdir()
            snapshot = snapshot_dir / "inbox.sqlite3"
            with InboxStore(database) as store:
                first = self.store_message(store)
                self.assertFalse(
                    store.messages_ready_for_device_cleanup(
                        "modem-1", ["sha256:one"]
                    )
                )
                row = store.connection.execute(
                    "SELECT lease_token FROM notifications WHERE message_seq=?",
                    (first.seq,),
                ).fetchone()
                self.assertIsNone(row["lease_token"])
                store.connection.execute(
                    """
                    UPDATE notifications
                    SET state='accepted_upstream', accepted_at=1, updated_at=1
                    WHERE message_seq=?
                    """,
                    (first.seq,),
                )
                self.assertTrue(
                    store.messages_ready_for_device_cleanup(
                        "modem-1", ["sha256:one"]
                    )
                )
                store.export_readonly_snapshot(snapshot)

            self.assertTrue(
                readonly_snapshot_ready_for_device_cleanup(
                    snapshot,
                    "modem-1",
                    ["sha256:one"],
                )
            )
            with self.assertRaisesRegex(StoreError, "unique"):
                readonly_snapshot_ready_for_device_cleanup(
                    snapshot,
                    "modem-1",
                    ["sha256:one", "sha256:one"],
                )


if __name__ == "__main__":
    unittest.main()
