import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mf833u1_forwarder as forwarder
from sms_store import InboxStore, MigrationRecord, StoreError


class LegacyMigrationTests(unittest.TestCase):
    def instance(self):
        return forwarder.Instance(
            path=Path("test.conf"),
            instance_id="modem-1",
            label="测试卡",
            base_url="http://192.168.0.1",
            interface="eth2",
            expected_mac="00:00:00:00:00:00",
            expected_usb_path="2-1",
            expected_usb_id="19d2:1706",
            mode="bark",
            bark_server="https://api.day.app",
            bark_key="test-key",
            telegram_bot_token="",
            telegram_chat_id="",
            ntfy_server="",
            ntfy_topic="",
            ntfy_token="",
            notification_content="summary",
            first_run="baseline",
            include_sent=False,
            retention_days=0,
            device_sms_capacity=100,
            device_cleanup_threshold=0,
            device_cleanup_batch=30,
        )

    def payload(self):
        return {
            "messages": [
                {
                    "id": "3",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "new",
                    "date": "26,07,27,03,00,00,+32",
                },
                {
                    "id": "2",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "seen-only",
                    "date": "26,07,27,02,00,00,+32",
                },
                {
                    "id": "1",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "csv",
                    "date": "26,07,27,01,00,00,+32",
                },
            ]
        }

    def write_legacy_files(self, root: Path):
        state_path = root / "state" / "modem-1.json"
        csv_path = root / "messages" / "modem-1.csv"
        state_path.parent.mkdir(parents=True)
        csv_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "initialized": True,
                    "seen": ["id:1", "id:2", "id:99"],
                }
            ),
            encoding="utf-8",
        )
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "key",
                    "modem_id",
                    "number",
                    "content",
                    "date",
                    "direction",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "key": "id:1",
                    "modem_id": "1",
                    "number": "13800000000",
                    "content": "csv",
                    "date": "2026-07-27 01:00:00",
                    "direction": "received",
                }
            )
        return state_path, csv_path

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_migration_is_idempotent_and_only_queues_unseen_current_sms(
        self, fetch
    ):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path, csv_path = self.write_legacy_files(root)
            before = (
                hashlib.sha256(state_path.read_bytes()).hexdigest(),
                hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            )

            self.assertTrue(forwarder.migrate_instance(self.instance(), root))
            self.assertTrue(forwarder.migrate_instance(self.instance(), root))

            after = (
                hashlib.sha256(state_path.read_bytes()).hexdigest(),
                hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(before, after)

            with InboxStore(root / "inbox.sqlite3") as store:
                self.assertEqual(store.message_count("modem-1"), 4)
                jobs = store.connection.execute(
                    """
                    SELECT m.modem_id, n.state
                    FROM notifications AS n
                    JOIN messages AS m ON m.seq = n.message_seq
                    """
                ).fetchall()
                self.assertEqual(
                    [(row["modem_id"], row["state"]) for row in jobs],
                    [("3", "pending")],
                )
                sources = {
                    row["source"]: row["count"]
                    for row in store.connection.execute(
                        """
                        SELECT source, COUNT(*) AS count
                        FROM messages GROUP BY source
                        """
                    ).fetchall()
                }
                self.assertEqual(
                    sources,
                    {"legacy_csv": 1, "legacy_seen": 2, "modem": 1},
                )

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_corrupt_legacy_state_fails_before_database_write(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state" / "modem-1.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{", encoding="utf-8")

            with self.assertRaisesRegex(forwarder.ForwarderError, "cannot read state"):
                forwarder.migrate_instance(self.instance(), root)
            self.assertFalse((root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_bad_legacy_csv_fails_before_database_write(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "messages" / "modem-1.csv"
            csv_path.parent.mkdir(parents=True)
            csv_path.write_text("wrong,columns\n1,2\n", encoding="utf-8")

            with self.assertRaisesRegex(
                forwarder.ForwarderError,
                "expected schema",
            ):
                forwarder.migrate_instance(self.instance(), root)
            self.assertFalse((root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_legacy_csv_rejects_extra_columns(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "messages" / "modem-1.csv"
            csv_path.parent.mkdir(parents=True)
            csv_path.write_text(
                "key,modem_id,number,content,date,direction,unexpected\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                forwarder.ForwarderError,
                "expected schema",
            ):
                forwarder.migrate_instance(self.instance(), root)
            self.assertFalse((root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_non_boolean_legacy_initialized_fails_before_database_write(
        self,
        fetch,
    ):
        fetch.return_value = self.payload()
        for invalid in ("false", 0, 1, None, [], {}):
            with self.subTest(initialized=invalid):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    state_path = root / "state" / "modem-1.json"
                    state_path.parent.mkdir(parents=True)
                    state_path.write_text(
                        json.dumps({"initialized": invalid, "seen": []}),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        forwarder.ForwarderError,
                        "must be boolean",
                    ):
                        forwarder.migrate_instance(self.instance(), root)
                    self.assertFalse((root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_duplicate_legacy_csv_key_fails_before_database_write(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, csv_path = self.write_legacy_files(root)
            with csv_path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "key",
                        "modem_id",
                        "number",
                        "content",
                        "date",
                        "direction",
                    ],
                )
                writer.writerow(
                    {
                        "key": "id:1",
                        "modem_id": "different",
                        "number": "13800000000",
                        "content": "conflict",
                        "date": "2026-07-27 02:00:00",
                        "direction": "received",
                    }
                )

            with self.assertRaisesRegex(
                forwarder.ForwarderError,
                "repeats key",
            ):
                forwarder.migrate_instance(self.instance(), root)
            self.assertFalse((root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_duplicate_legacy_csv_payload_fails_before_database_write(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, csv_path = self.write_legacy_files(root)
            with csv_path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "key",
                        "modem_id",
                        "number",
                        "content",
                        "date",
                        "direction",
                    ],
                )
                writer.writerow(
                    {
                        "key": "different-key",
                        "modem_id": "1",
                        "number": "13800000000",
                        "content": "csv",
                        "date": "2026-07-27 01:00:00",
                        "direction": "received",
                    }
                )

            with self.assertRaisesRegex(
                forwarder.ForwarderError,
                "repeats a message payload",
            ):
                forwarder.migrate_instance(self.instance(), root)
            self.assertFalse((root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.InboxStore.export_readonly_snapshot")
    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_saturated_modem_page_blocks_marker_and_snapshot(self, fetch, export):
        fetch.return_value = {
            "messages": [
                {
                    "id": str(index),
                    "tag": "0",
                    "number": "13800000000",
                    "content": f"message-{index}",
                    "date": "26,07,27,03,00,00,+32",
                }
                for index in range(500)
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = forwarder.run_migrations(
                [self.instance()],
                root,
                inbox_snapshot=root / "view" / "inbox.sqlite3",
            )
            self.assertFalse(result)
            self.assertFalse((root / "inbox.sqlite3").exists())
            export.assert_not_called()

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_page_below_saturation_limit_can_migrate(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": str(index),
                    "tag": "0",
                    "number": "13800000000",
                    "content": f"message-{index}",
                    "date": "26,07,27,03,00,00,+32",
                }
                for index in range(499)
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertTrue(forwarder.migrate_instance(self.instance(), root))
            with InboxStore(root / "inbox.sqlite3") as store:
                self.assertTrue(store.legacy_migration_complete("modem-1"))
                self.assertEqual(store.message_count("modem-1"), 499)

    @patch("mf833u1_forwarder.InboxStore.export_readonly_snapshot")
    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.deliver_channel")
    @patch("mf833u1_forwarder.fetch_sms_payload")
    @patch("mf833u1_forwarder.discover_instances")
    def test_cli_migrate_only_never_delivers_unlocks_or_exports(
        self,
        discover,
        fetch,
        deliver,
        post_modem_form,
        export,
    ):
        discover.return_value = [self.instance()]
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = forwarder.main(
                [
                    "--migrate-only",
                    "--state-dir",
                    temp_dir,
                ]
            )
        self.assertEqual(result, 0)
        deliver.assert_not_called()
        post_modem_form.assert_not_called()
        export.assert_not_called()

    @patch("mf833u1_forwarder.discover_instances")
    def test_cli_migration_rejects_snapshot_export(self, discover):
        result = forwarder.main(
            [
                "--migrate-only",
                "--inbox-snapshot",
                "/tmp/should-not-exist.sqlite3",
            ]
        )
        self.assertEqual(result, 2)
        discover.assert_not_called()

    @patch("mf833u1_forwarder.discover_instances")
    def test_cli_migration_rejects_auto_unlock_before_discovery(self, discover):
        result = forwarder.main(["--migrate-only", "--auto-unlock"])
        self.assertEqual(result, 2)
        discover.assert_not_called()

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_normal_poll_fails_closed_until_legacy_migration(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_legacy_files(root)

            with patch("mf833u1_forwarder.deliver_channel") as deliver:
                with self.assertRaisesRegex(
                    forwarder.ForwarderError,
                    "--migrate-only",
                ):
                    forwarder.process_instance(
                        self.instance(),
                        root,
                        dry_run=False,
                        show_messages=False,
                    )
            deliver.assert_not_called()
            with InboxStore(root / "inbox.sqlite3") as store:
                self.assertEqual(store.message_count("modem-1"), 0)

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_changed_legacy_file_is_rejected_after_migration(self, fetch):
        fetch.return_value = self.payload()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path, _ = self.write_legacy_files(root)
            self.assertTrue(forwarder.migrate_instance(self.instance(), root))
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["updated_at"] = "changed"
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                forwarder.ForwarderError,
                "changed after",
            ):
                forwarder.migrate_instance(self.instance(), root)
            with InboxStore(root / "inbox.sqlite3") as store:
                self.assertEqual(store.message_count("modem-1"), 4)

    @patch("mf833u1_forwarder.InboxStore")
    @patch("mf833u1_forwarder.migrate_instance")
    def test_multi_instance_failure_does_not_export_partial_snapshot(
        self,
        migrate_instance,
        inbox_store,
    ):
        migrate_instance.side_effect = [
            True,
            forwarder.ForwarderError("second modem unavailable"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = forwarder.run_migrations(
                [self.instance(), self.instance()],
                Path(temp_dir),
                inbox_snapshot=Path(temp_dir) / "view" / "inbox.sqlite3",
            )
        self.assertFalse(result)
        inbox_store.assert_not_called()

    def test_migration_transaction_rolls_back_on_invalid_record(self):
        good = MigrationRecord(
            message_key="sha256:good",
            legacy_key="id:1",
            modem_id="1",
            number="13800000000",
            content="good",
            message_date="2026-07-27 01:00:00",
            direction="received",
            baseline=True,
            source="legacy_csv",
            payload_available=True,
        )
        bad = MigrationRecord(
            message_key="sha256:bad",
            legacy_key="id:2",
            modem_id="2",
            number="13800000000",
            content="bad",
            message_date="2026-07-27 02:00:00",
            direction="invalid",
            baseline=True,
            source="legacy_csv",
            payload_available=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with InboxStore(Path(temp_dir) / "inbox.sqlite3") as store:
                with self.assertRaisesRegex(StoreError, "CHECK constraint"):
                    store.migrate_legacy(
                        instance_id="modem-1",
                        records=(good, bad),
                        state_sha256="state",
                        csv_sha256="csv",
                    )
                self.assertEqual(store.message_count("modem-1"), 0)
                self.assertFalse(store.legacy_migration_complete("modem-1"))

    def test_migration_keeps_more_than_five_thousand_legacy_keys(self):
        records = tuple(
            MigrationRecord(
                message_key=f"legacy:{index}",
                legacy_key=f"id:{index}",
                modem_id=str(index),
                number="",
                content="",
                message_date="",
                direction="received",
                baseline=True,
                source="legacy_seen",
                payload_available=False,
            )
            for index in range(10000)
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with InboxStore(Path(temp_dir) / "inbox.sqlite3") as store:
                result = store.migrate_legacy(
                    instance_id="modem-1",
                    records=records,
                    state_sha256="state",
                    csv_sha256="missing",
                )
                self.assertEqual(result.inserted_messages, 10000)
                self.assertEqual(store.message_count("modem-1"), 10000)

                repeated = store.migrate_legacy(
                    instance_id="modem-1",
                    records=records,
                    state_sha256="state",
                    csv_sha256="missing",
                )
                self.assertTrue(repeated.already_complete)
                self.assertEqual(store.message_count("modem-1"), 10000)


if __name__ == "__main__":
    unittest.main()
