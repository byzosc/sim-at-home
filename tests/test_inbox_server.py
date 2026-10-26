import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from inbox_server import (
    DEFAULT_BIND,
    DEFAULT_PORT,
    InboxApplication,
    InboxUnavailable,
    ThreadingHTTPServer,
    build_parser,
    make_handler,
)
from sms_store import InboxStore


class InboxServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "inbox.sqlite3"
        self.allowlist = self.root / "inbox-users.allow"
        self.allowlist.write_text("owner@example.com\n", encoding="utf-8")
        os.chmod(self.allowlist, 0o640)
        self._create_database(self.database)

        application = InboxApplication(
            self.database,
            self.allowlist,
            trusted_owner_uid=os.geteuid(),
        )
        application.validate_startup()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(application),
        )
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    @staticmethod
    def _create_database(path: Path, *, labels: bool = True) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata(key, value) VALUES('schema_version', '3');

            CREATE TABLE messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                message_key TEXT NOT NULL,
                legacy_key TEXT NOT NULL DEFAULT '',
                modem_id TEXT NOT NULL DEFAULT '',
                number TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                message_date TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL,
                discovered_at INTEGER NOT NULL,
                baseline INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'modem',
                payload_available INTEGER NOT NULL DEFAULT 1,
                UNIQUE(instance_id, message_key)
            );

            CREATE TABLE modem_page_status (
                instance_id TEXT PRIMARY KEY,
                message_count INTEGER NOT NULL,
                page_limit INTEGER NOT NULL,
                saturated INTEGER NOT NULL,
                checked_at INTEGER NOT NULL,
                last_alert_at INTEGER
            );
            """
        )
        if labels:
            connection.execute(
                """
                CREATE TABLE instance_labels (
                    instance_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO instance_labels(instance_id, label, updated_at)
                VALUES('modem-1', '测试卡', 100)
                """
            )
        messages = (
            (
                1,
                "modem-1",
                "message-1",
                "1",
                "13800000000",
                "验证码是 甲乙丙",
                "2026-07-27 12:00:01",
                "received",
                100,
                1,
            ),
            (
                2,
                "modem-1",
                "message-2",
                "2",
                "13800000001",
                "第二条短信",
                "2026-07-27 12:00:02",
                "sent",
                101,
                1,
            ),
            (
                3,
                "modem-1",
                "legacy-placeholder",
                "",
                "",
                "",
                "",
                "received",
                102,
                0,
            ),
            (
                4,
                "modem-2",
                "message-4",
                "4",
                "13800000002",
                "示例短信",
                "2026-07-27 12:00:04",
                "received",
                103,
                1,
            ),
        )
        connection.executemany(
            """
            INSERT INTO messages(
                seq, instance_id, message_key, modem_id, number, content,
                message_date, direction, discovered_at, payload_available
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            messages,
        )
        connection.execute(
            """
            INSERT INTO modem_page_status(
                instance_id, message_count, page_limit, saturated,
                checked_at, last_alert_at
            )
            VALUES('modem-1', 500, 500, 1, 123456, 123456)
            """
        )
        connection.commit()
        connection.close()
        os.chmod(path, 0o640)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def request(self, method, target, *, login="owner@example.com", body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=3,
        )
        headers = {}
        if login is not None:
            headers["Tailscale-User-Login"] = login
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {
            key.lower(): value for key, value in response.getheaders()
        }
        status = response.status
        connection.close()
        return status, response_headers, response_body

    def request_json(self, method, target, **kwargs):
        status, headers, body = self.request(method, target, **kwargs)
        return status, headers, json.loads(body.decode("utf-8"))

    def test_every_request_requires_one_exact_allowed_tailscale_login(self):
        status, _headers, payload = self.request_json(
            "GET",
            "/api/v1/status",
            login=None,
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "forbidden"})

        status, _headers, _payload = self.request_json(
            "GET",
            "/api/v1/status",
            login="other@example.com",
        )
        self.assertEqual(status, 403)

        status, _headers, payload = self.request_json(
            "GET",
            "/api/v1/status",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 3)

    def test_messages_are_utf8_paginated_and_labels_fall_back_to_instance(self):
        status, headers, first = self.request_json(
            "GET",
            "/api/v1/messages?after_seq=0&limit=1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            headers["content-type"],
            "application/json; charset=utf-8",
        )
        self.assertEqual(first["messages"][0]["seq"], 1)
        self.assertEqual(first["messages"][0]["label"], "测试卡")
        self.assertEqual(first["messages"][0]["body"], "验证码是 甲乙丙")
        self.assertEqual(first["next_after_seq"], 1)
        self.assertTrue(first["has_more"])

        status, _headers, second = self.request_json(
            "GET",
            "/api/v1/messages?after_seq=1&limit=1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["messages"][0]["seq"], 2)
        self.assertEqual(second["messages"][0]["direction"], "sent")
        self.assertTrue(second["has_more"])

        status, _headers, last = self.request_json(
            "GET",
            "/api/v1/messages?after_seq=2&limit=200",
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["seq"] for item in last["messages"]], [4])
        self.assertEqual(last["messages"][0]["label"], "modem-2")
        self.assertFalse(last["has_more"])

    def test_message_query_rejects_malformed_or_out_of_range_parameters(self):
        targets = (
            "/api/v1/messages?after_seq=-1",
            "/api/v1/messages?after_seq=abc",
            "/api/v1/messages?after_seq=",
            "/api/v1/messages?after_seq=9223372036854775808",
            "/api/v1/messages?limit=0",
            "/api/v1/messages?limit=201",
            "/api/v1/messages?limit=1.5",
            "/api/v1/messages?limit=1&limit=2",
            "/api/v1/messages?unknown=1",
            "/api/v1/messages?broken",
        )
        for target in targets:
            with self.subTest(target=target):
                status, _headers, payload = self.request_json("GET", target)
                self.assertEqual(status, 400)
                self.assertEqual(payload, {"error": "invalid_query"})

    def test_status_reports_database_bounds_timestamp_and_saturation(self):
        status, _headers, payload = self.request_json(
            "GET",
            "/api/v1/status",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["min_seq"], 1)
        self.assertEqual(payload["max_seq"], 4)
        self.assertEqual(payload["count"], 3)
        self.assertTrue(payload["database_timestamp"].endswith("Z"))
        saturation = payload["modem_saturation"]
        self.assertTrue(saturation["any_saturated"])
        self.assertEqual(len(saturation["instances"]), 1)
        self.assertEqual(saturation["instances"][0]["label"], "测试卡")
        self.assertEqual(saturation["instances"][0]["message_count"], 500)
        self.assertEqual(saturation["instances"][0]["page_limit"], 500)

    def test_only_get_is_allowed_and_unauthorized_mutation_stays_forbidden(self):
        for method in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, _body = self.request(
                    method,
                    "/api/v1/messages",
                    body=b"{}" if method != "HEAD" else None,
                )
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET")

        status, _headers, payload = self.request_json(
            "POST",
            "/api/v1/messages",
            login=None,
            body=b"{}",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload, {"error": "forbidden"})

    def test_arbitrary_paths_and_queries_are_not_exposed(self):
        secret = self.root / "secret.txt"
        secret.write_text("must-not-leak", encoding="utf-8")
        for target in (
            "/etc/passwd",
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/debug",
            "/proxy?url=http://192.168.0.1",
        ):
            with self.subTest(target=target):
                status, _headers, payload = self.request_json("GET", target)
                self.assertEqual(status, 404)
                self.assertEqual(payload, {"error": "not_found"})
                self.assertNotIn(
                    b"must-not-leak",
                    json.dumps(payload).encode("utf-8"),
                )

        status, _headers, payload = self.request_json(
            "GET",
            "/api/v1/status?extra=1",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "invalid_query"})

    def test_security_headers_apply_to_html_and_json(self):
        for target in ("/", "/api/v1/status"):
            with self.subTest(target=target):
                status, headers, body = self.request("GET", target)
                self.assertEqual(status, 200)
                self.assertIn("no-store", headers["cache-control"])
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
                self.assertIn(
                    "default-src 'self'",
                    headers["content-security-policy"],
                )
                self.assertIn(
                    "frame-ancestors 'none'",
                    headers["content-security-policy"],
                )
                if target == "/":
                    html = body.decode("utf-8")
                    self.assertIn("localStorage", html)
                    self.assertIn("SIM 短信收件箱", html)
                    self.assertIn("fetchAllMessages", html)
                    self.assertNotIn("<script src=", html)
                    self.assertNotIn("<link ", html)

    def test_schema_without_instance_labels_uses_instance_id(self):
        other_database = self.root / "without-labels.sqlite3"
        self._create_database(other_database, labels=False)
        application = InboxApplication(
            other_database,
            self.allowlist,
            trusted_owner_uid=os.geteuid(),
        )
        application.validate_startup()
        result = application.messages(0, 1)
        self.assertEqual(result["messages"][0]["label"], "modem-1")
        status = application.status()
        self.assertEqual(
            status["modem_saturation"]["instances"][0]["label"],
            "modem-1",
        )

    def test_reads_current_store_snapshot_after_atomic_replacement(self):
        state_database = self.root / "state" / "inbox.sqlite3"
        snapshot_directory = self.root / "exported-view"
        snapshot_directory.mkdir()
        snapshot = snapshot_directory / "inbox.sqlite3"
        with InboxStore(state_database) as store:
            store.set_instance_label("modem-live", "实时测试卡")
            store.store_message(
                instance_id="modem-live",
                message_key="sha256:first",
                modem_id="1",
                number="13800000000",
                content="第一条",
                message_date="2026-07-27 14:00:00",
                direction="received",
                channels=(),
            )
            self.assertTrue(store.export_readonly_snapshot(snapshot))

            application = InboxApplication(
                snapshot,
                self.allowlist,
                trusted_owner_uid=os.geteuid(),
            )
            application.validate_startup()
            self.assertEqual(
                [item["body"] for item in application.messages(0, 200)["messages"]],
                ["第一条"],
            )

            store.store_message(
                instance_id="modem-live",
                message_key="sha256:second",
                modem_id="2",
                number="13800000000",
                content="第二条",
                message_date="2026-07-27 14:01:00",
                direction="received",
                channels=(),
            )
            self.assertTrue(store.export_readonly_snapshot(snapshot))
            self.assertEqual(
                [item["body"] for item in application.messages(0, 200)["messages"]],
                ["第一条", "第二条"],
            )

    def test_startup_validation_fails_closed_for_missing_or_unsafe_files(self):
        missing_database = InboxApplication(
            self.root / "missing.sqlite3",
            self.allowlist,
            trusted_owner_uid=os.geteuid(),
        )
        with self.assertRaises(InboxUnavailable):
            missing_database.validate_startup()

        empty_allowlist = self.root / "empty.allow"
        empty_allowlist.write_text("", encoding="utf-8")
        with self.assertRaises(InboxUnavailable):
            InboxApplication(
                self.database,
                empty_allowlist,
                trusted_owner_uid=os.geteuid(),
            ).validate_startup()

        os.chmod(self.allowlist, 0o660)
        with self.assertRaises(InboxUnavailable):
            InboxApplication(
                self.database,
                self.allowlist,
                trusted_owner_uid=os.geteuid(),
            ).validate_startup()

        os.chmod(self.allowlist, 0o640)
        hardlink = self.root / "allowlist-hardlink"
        os.link(self.allowlist, hardlink)
        try:
            with self.assertRaises(InboxUnavailable):
                InboxApplication(
                    self.database,
                    self.allowlist,
                    trusted_owner_uid=os.geteuid(),
                ).validate_startup()
        finally:
            hardlink.unlink()

        os.chmod(self.root, 0o770)
        try:
            with self.assertRaises(InboxUnavailable):
                InboxApplication(
                    self.database,
                    self.allowlist,
                    trusted_owner_uid=os.geteuid(),
                ).validate_startup()
        finally:
            os.chmod(self.root, 0o700)

    def test_parser_defaults_to_loopback_port_and_stable_paths(self):
        arguments = build_parser().parse_args([])
        self.assertEqual(arguments.bind, DEFAULT_BIND)
        self.assertEqual(arguments.port, DEFAULT_PORT)
        self.assertEqual(arguments.database, Path(
            "/overlay/sim-card-forwarding/inbox-view/inbox.sqlite3"
        ))
        self.assertEqual(
            arguments.allowlist,
            Path("/etc/sim-card-inbox/users.allow"),
        )


if __name__ == "__main__":
    unittest.main()
