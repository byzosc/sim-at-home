#!/usr/bin/env python3
"""Durable SQLite inbox for SIM messages and notification attempts."""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence


SCHEMA_VERSION = 3


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoreResult:
    seq: int
    inserted: bool


@dataclass(frozen=True)
class MigrationRecord:
    message_key: str
    legacy_key: str
    modem_id: str
    number: str
    content: str
    message_date: str
    direction: str
    baseline: bool
    source: str
    payload_available: bool
    channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationResult:
    inserted_messages: int
    inserted_notifications: int
    already_complete: bool


@dataclass(frozen=True)
class ModemPageStatus:
    message_count: int
    page_limit: int
    saturated: bool
    checked_at: int


@dataclass(frozen=True)
class PendingNotification:
    seq: int
    instance_id: str
    message_key: str
    modem_id: str
    number: str
    content: str
    message_date: str
    direction: str
    target_id: str
    channel: str
    attempts: int
    lease_token: str


def _validate_cleanup_keys(message_keys: Sequence[str]) -> tuple[str, ...]:
    keys = tuple(message_keys)
    if not keys:
        raise StoreError("device cleanup requires at least one message key")
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        raise StoreError("device cleanup message keys must be non-empty and unique")
    return keys


def _cleanup_ready_count(
    connection: sqlite3.Connection,
    instance_id: str,
    message_keys: Sequence[str],
) -> int:
    placeholders = ",".join("?" for _ in message_keys)
    row = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM messages AS m
        WHERE m.instance_id = ?
          AND m.message_key IN ({placeholders})
          AND m.payload_available = 1
          AND NOT EXISTS (
              SELECT 1
              FROM notifications AS n
              WHERE n.message_seq = m.seq
                AND n.state != 'accepted_upstream'
          )
        """,
        (instance_id, *message_keys),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def readonly_snapshot_ready_for_device_cleanup(
    path: Path,
    instance_id: str,
    message_keys: Sequence[str],
) -> bool:
    """Apply the cleanup durability check to the exported read-only snapshot."""

    keys = _validate_cleanup_keys(message_keys)
    path = Path(os.path.abspath(path))
    connection = None
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=3,
        )
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise StoreError("read-only inbox snapshot failed SQLite quick_check")
        placeholders = ",".join("?" for _ in keys)
        row = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM messages
            WHERE instance_id = ?
              AND message_key IN ({placeholders})
              AND payload_available = 1
            """,
            (instance_id, *keys),
        ).fetchone()
        return row is not None and int(row[0]) == len(keys)
    except (OSError, sqlite3.Error) as exc:
        raise StoreError(f"cannot verify read-only inbox snapshot: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


class InboxStore:
    """Own the authoritative message database under the persistent state root."""

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        try:
            self.connection = sqlite3.connect(
                str(self.path),
                timeout=5,
                isolation_level=None,
            )
            self.connection.row_factory = sqlite3.Row
            self._configure()
            self._initialize_schema()
            os.chmod(self.path, 0o600)
        except StoreError:
            try:
                self.connection.close()
            except (AttributeError, sqlite3.Error):
                pass
            raise
        except (OSError, sqlite3.Error) as exc:
            try:
                self.connection.close()
            except (AttributeError, sqlite3.Error):
                pass
            raise StoreError(f"cannot initialize inbox database {self.path}: {exc}") from exc

    def __enter__(self) -> "InboxStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _configure(self) -> None:
        journal_mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise StoreError("SQLite could not enable WAL mode")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA cache_size=-4096")
        self.connection.execute("PRAGMA mmap_size=0")
        self.connection.execute("PRAGMA wal_autocheckpoint=128")
        self.connection.execute("PRAGMA journal_size_limit=8388608")
        self.connection.execute("PRAGMA busy_timeout=3000")

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.execute("COMMIT")
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                try:
                    self.connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise StoreError(f"SQLite transaction failed: {exc}") from exc
        except BaseException:
            if self.connection.in_transaction:
                try:
                    self.connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS instance_state (
                    instance_id TEXT PRIMARY KEY,
                    initialized INTEGER NOT NULL DEFAULT 0
                        CHECK (initialized IN (0, 1)),
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS instance_labels (
                    instance_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    legacy_key TEXT NOT NULL DEFAULT '',
                    modem_id TEXT NOT NULL DEFAULT '',
                    number TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    message_date TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL
                        CHECK (direction IN ('received', 'sent')),
                    discovered_at INTEGER NOT NULL,
                    baseline INTEGER NOT NULL DEFAULT 0
                        CHECK (baseline IN (0, 1)),
                    source TEXT NOT NULL DEFAULT 'modem'
                        CHECK (source IN (
                            'modem', 'baseline', 'legacy_csv', 'legacy_seen'
                        )),
                    payload_available INTEGER NOT NULL DEFAULT 1
                        CHECK (payload_available IN (0, 1)),
                    UNIQUE (instance_id, message_key)
                );

                CREATE INDEX IF NOT EXISTS messages_instance_seq
                    ON messages (instance_id, seq);
                CREATE INDEX IF NOT EXISTS messages_discovered_at
                    ON messages (discovered_at);

                CREATE TABLE IF NOT EXISTS notifications (
                    message_seq INTEGER NOT NULL
                        REFERENCES messages(seq) ON DELETE CASCADE,
                    target_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN (
                            'pending', 'leased', 'retry',
                            'accepted_upstream', 'blocked'
                        )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until INTEGER,
                    last_attempt_at INTEGER,
                    accepted_at INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (message_seq, target_id)
                );

                CREATE INDEX IF NOT EXISTS notifications_pending
                    ON notifications (state, next_attempt_at, message_seq);

                CREATE TABLE IF NOT EXISTS legacy_migrations (
                    instance_id TEXT PRIMARY KEY,
                    migration_version INTEGER NOT NULL,
                    legacy_state_sha256 TEXT NOT NULL,
                    legacy_csv_sha256 TEXT NOT NULL,
                    migrated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS modem_page_status (
                    instance_id TEXT PRIMARY KEY,
                    message_count INTEGER NOT NULL,
                    page_limit INTEGER NOT NULL,
                    saturated INTEGER NOT NULL
                        CHECK (saturated IN (0, 1)),
                    checked_at INTEGER NOT NULL,
                    last_alert_at INTEGER
                );

                COMMIT;
            """
        )
        with self._immediate_transaction():
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                version = int(row["value"])
                if version == 1:
                    columns = {
                        str(item["name"])
                        for item in self.connection.execute(
                            "PRAGMA table_info(messages)"
                        ).fetchall()
                    }
                    if "legacy_key" not in columns:
                        self.connection.execute(
                            """
                            ALTER TABLE messages
                            ADD COLUMN legacy_key TEXT NOT NULL DEFAULT ''
                            """
                        )
                    version = 2
                if version == 2:
                    version = 3
                if version != SCHEMA_VERSION:
                    raise StoreError(
                        "unsupported inbox schema version "
                        f"{row['value']} (expected {SCHEMA_VERSION})"
                    )
                self.connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            self.connection.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('view_revision', '0')
                ON CONFLICT(key) DO NOTHING
                """
            )

    def _bump_view_revision_locked(self) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata(key, value)
            VALUES('view_revision', '1')
            ON CONFLICT(key) DO UPDATE SET
                value = CAST(metadata.value AS INTEGER) + 1
            """
        )

    def set_instance_label(self, instance_id: str, label: str) -> bool:
        """Store a display label and report whether the read-only view changed."""

        if not instance_id:
            raise StoreError("instance_id cannot be empty")
        label = label.strip() or instance_id
        if len(label) > 128 or "\x00" in label:
            raise StoreError("instance label is invalid")
        now = int(time.time())
        with self._immediate_transaction():
            row = self.connection.execute(
                "SELECT label FROM instance_labels WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if row is not None and row["label"] == label:
                return False
            self.connection.execute(
                """
                INSERT INTO instance_labels(instance_id, label, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (instance_id, label, now),
            )
            self._bump_view_revision_locked()
        return True

    def instance_initialized(self, instance_id: str) -> bool:
        row = self.connection.execute(
            "SELECT initialized FROM instance_state WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        return bool(row["initialized"]) if row is not None else False

    def set_instance_initialized(self, instance_id: str) -> None:
        now = int(time.time())
        with self._immediate_transaction():
            self.connection.execute(
                """
                INSERT INTO instance_state(instance_id, initialized, updated_at)
                VALUES(?, 1, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    initialized = 1,
                    updated_at = excluded.updated_at
                """,
                (instance_id, now),
            )

    def store_message(
        self,
        *,
        instance_id: str,
        message_key: str,
        legacy_key: str = "",
        modem_id: str,
        number: str,
        content: str,
        message_date: str,
        direction: str,
        channels: Iterable[str],
        baseline: bool = False,
        source: str = "modem",
        payload_available: bool = True,
    ) -> StoreResult:
        """Commit the message and pending notification rows atomically."""

        now = int(time.time())
        unique_channels = sorted({value for value in channels if value})
        with self._immediate_transaction():
            row = self.connection.execute(
                """
                SELECT seq, payload_available
                FROM messages
                WHERE instance_id = ? AND message_key = ?
                """,
                (instance_id, message_key),
            ).fetchone()
            inserted = row is None
            if inserted:
                cursor = self.connection.execute(
                    """
                    INSERT INTO messages(
                        instance_id, message_key, legacy_key, modem_id, number,
                        content, message_date, direction, discovered_at, baseline,
                        source, payload_available
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        instance_id,
                        message_key,
                        legacy_key,
                        modem_id,
                        number,
                        content,
                        message_date,
                        direction,
                        now,
                        int(baseline),
                        source,
                        int(payload_available),
                    ),
                )
                seq = int(cursor.lastrowid)
                existing_payload_available = bool(payload_available)
            else:
                seq = int(row["seq"])
                existing_payload_available = bool(row["payload_available"])

            payload_updated = False
            if not inserted and not existing_payload_available and payload_available:
                cursor = self.connection.execute(
                    """
                    UPDATE messages
                    SET legacy_key = CASE
                            WHEN legacy_key = '' THEN ? ELSE legacy_key
                        END,
                        modem_id = ?, number = ?, content = ?, message_date = ?,
                        direction = ?, payload_available = 1
                    WHERE seq = ?
                    """,
                    (
                        legacy_key,
                        modem_id,
                        number,
                        content,
                        message_date,
                        direction,
                        seq,
                    ),
                )
                payload_updated = cursor.rowcount == 1

            if inserted and not baseline:
                for channel in unique_channels:
                    self.connection.execute(
                        """
                        INSERT INTO notifications(
                            message_seq, target_id, channel, updated_at
                        )
                        VALUES(?, ?, ?, ?)
                        ON CONFLICT(message_seq, target_id) DO NOTHING
                        """,
                        (seq, channel, channel, now),
                    )
            if inserted or payload_updated:
                self._bump_view_revision_locked()
        return StoreResult(seq=seq, inserted=inserted)

    def legacy_migration_complete(self, instance_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM legacy_migrations
            WHERE instance_id = ? AND migration_version = 1
            """,
            (instance_id,),
        ).fetchone()
        return row is not None

    def record_modem_page_status(
        self,
        instance_id: str,
        message_count: int,
        page_limit: int,
        *,
        alert_interval: int = 21600,
    ) -> bool:
        """Record page saturation and return True when a rate-limited alert is due."""

        if message_count < 0 or page_limit <= 0:
            raise StoreError("invalid modem page count")
        now = int(time.time())
        saturated = message_count >= page_limit
        should_alert = False
        with self._immediate_transaction():
            row = self.connection.execute(
                """
                SELECT message_count, page_limit, saturated, last_alert_at
                FROM modem_page_status WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
            last_alert = (
                int(row["last_alert_at"])
                if row is not None and row["last_alert_at"] is not None
                else None
            )
            if saturated and (
                last_alert is None or now - last_alert >= alert_interval
            ):
                should_alert = True
                last_alert = now
            elif not saturated:
                last_alert = None

            changed = (
                row is None
                or int(row["message_count"]) != message_count
                or int(row["page_limit"]) != page_limit
                or bool(row["saturated"]) != saturated
                or (
                    row["last_alert_at"] is None
                    and last_alert is not None
                )
                or (
                    row["last_alert_at"] is not None
                    and last_alert is None
                )
                or should_alert
            )
            if changed:
                self.connection.execute(
                    """
                    INSERT INTO modem_page_status(
                        instance_id, message_count, page_limit, saturated,
                        checked_at, last_alert_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instance_id) DO UPDATE SET
                        message_count = excluded.message_count,
                        page_limit = excluded.page_limit,
                        saturated = excluded.saturated,
                        checked_at = excluded.checked_at,
                        last_alert_at = excluded.last_alert_at
                    """,
                    (
                        instance_id,
                        message_count,
                        page_limit,
                        int(saturated),
                        now,
                        last_alert,
                    ),
                )
                self._bump_view_revision_locked()
        return should_alert

    def modem_page_status(self, instance_id: str) -> ModemPageStatus | None:
        row = self.connection.execute(
            """
            SELECT message_count, page_limit, saturated, checked_at
            FROM modem_page_status WHERE instance_id = ?
            """,
            (instance_id,),
        ).fetchone()
        if row is None:
            return None
        return ModemPageStatus(
            message_count=int(row["message_count"]),
            page_limit=int(row["page_limit"]),
            saturated=bool(row["saturated"]),
            checked_at=int(row["checked_at"]),
        )

    def migrate_legacy(
        self,
        *,
        instance_id: str,
        records: Sequence[MigrationRecord],
        state_sha256: str,
        csv_sha256: str,
    ) -> MigrationResult:
        now = int(time.time())
        inserted_messages = 0
        inserted_notifications = 0
        with self._immediate_transaction():
            marker = self.connection.execute(
                """
                SELECT legacy_state_sha256, legacy_csv_sha256
                FROM legacy_migrations
                WHERE instance_id = ? AND migration_version = 1
                """,
                (instance_id,),
            ).fetchone()
            if marker is not None:
                if (
                    marker["legacy_state_sha256"] != state_sha256
                    or marker["legacy_csv_sha256"] != csv_sha256
                ):
                    raise StoreError(
                        "legacy files changed after the recorded migration"
                    )
                return MigrationResult(0, 0, True)

            existing = self.connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if existing is not None and int(existing["count"]) != 0:
                raise StoreError(
                    "database already has messages but no legacy migration marker"
                )

            for record in records:
                duplicate = self.connection.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE instance_id = ? AND message_key = ?
                    """,
                    (instance_id, record.message_key),
                ).fetchone()
                if duplicate is not None:
                    continue
                cursor = self.connection.execute(
                    """
                    INSERT INTO messages(
                        instance_id, message_key, legacy_key, modem_id, number,
                        content, message_date, direction, discovered_at,
                        baseline, source, payload_available
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        instance_id,
                        record.message_key,
                        record.legacy_key,
                        record.modem_id,
                        record.number,
                        record.content,
                        record.message_date,
                        record.direction,
                        now,
                        int(record.baseline),
                        record.source,
                        int(record.payload_available),
                    ),
                )
                inserted_messages += 1
                seq = int(cursor.lastrowid)
                if not record.baseline:
                    for channel in sorted(set(record.channels)):
                        job = self.connection.execute(
                            """
                            INSERT INTO notifications(
                                message_seq, target_id, channel, updated_at
                            )
                            VALUES(?, ?, ?, ?)
                            ON CONFLICT(message_seq, target_id) DO NOTHING
                            """,
                            (seq, channel, channel, now),
                        )
                        inserted_notifications += job.rowcount

            self.connection.execute(
                """
                INSERT INTO instance_state(instance_id, initialized, updated_at)
                VALUES(?, 1, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    initialized = 1,
                    updated_at = excluded.updated_at
                """,
                (instance_id, now),
            )
            self.connection.execute(
                """
                INSERT INTO legacy_migrations(
                    instance_id, migration_version,
                    legacy_state_sha256, legacy_csv_sha256, migrated_at
                )
                VALUES(?, 1, ?, ?, ?)
                """,
                (instance_id, state_sha256, csv_sha256, now),
            )
            if inserted_messages:
                self._bump_view_revision_locked()
        return MigrationResult(
            inserted_messages,
            inserted_notifications,
            False,
        )

    def lease_notifications(
        self,
        instance_id: str,
        channels: Iterable[str],
        *,
        limit: int = 100,
        lease_seconds: int = 120,
    ) -> List[PendingNotification]:
        now = int(time.time())
        allowed = sorted({value for value in channels if value})
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        leased: List[PendingNotification] = []
        with self._immediate_transaction():
            self.connection.execute(
                """
                UPDATE notifications
                SET state = 'retry',
                    lease_token = NULL,
                    lease_until = NULL,
                    next_attempt_at = 0,
                    updated_at = ?
                WHERE state = 'leased' AND lease_until <= ?
                """,
                (now, now),
            )
            rows = self.connection.execute(
                f"""
                SELECT
                    m.seq, m.instance_id, m.message_key, m.modem_id, m.number,
                    m.content, m.message_date, m.direction,
                    n.target_id, n.channel, n.attempts
                FROM notifications AS n
                JOIN messages AS m ON m.seq = n.message_seq
                WHERE m.instance_id = ?
                  AND n.state IN ('pending', 'retry')
                  AND n.next_attempt_at <= ?
                  AND n.channel IN ({placeholders})
                ORDER BY m.seq, n.target_id
                LIMIT ?
                """,
                (instance_id, now, *allowed, limit),
            ).fetchall()
            for row in rows:
                lease_token = secrets.token_hex(16)
                cursor = self.connection.execute(
                    """
                    UPDATE notifications
                    SET state = 'leased',
                        attempts = attempts + 1,
                        lease_token = ?,
                        lease_until = ?,
                        last_attempt_at = ?,
                        updated_at = ?
                    WHERE message_seq = ? AND target_id = ?
                      AND state IN ('pending', 'retry')
                    """,
                    (
                        lease_token,
                        now + lease_seconds,
                        now,
                        now,
                        int(row["seq"]),
                        str(row["target_id"]),
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                leased.append(
                    PendingNotification(
                        seq=int(row["seq"]),
                        instance_id=str(row["instance_id"]),
                        message_key=str(row["message_key"]),
                        modem_id=str(row["modem_id"]),
                        number=str(row["number"]),
                        content=str(row["content"]),
                        message_date=str(row["message_date"]),
                        direction=str(row["direction"]),
                        target_id=str(row["target_id"]),
                        channel=str(row["channel"]),
                        attempts=int(row["attempts"]) + 1,
                        lease_token=lease_token,
                    )
                )
        return leased

    def mark_notification_accepted(
        self, seq: int, target_id: str, lease_token: str
    ) -> None:
        now = int(time.time())
        with self._immediate_transaction():
            cursor = self.connection.execute(
                """
                UPDATE notifications
                SET state = 'accepted_upstream',
                    lease_token = NULL,
                    lease_until = NULL,
                    accepted_at = ?,
                    last_error = '',
                    updated_at = ?
                WHERE message_seq = ? AND target_id = ?
                  AND state = 'leased' AND lease_token = ?
                """,
                (now, now, seq, target_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise StoreError("notification lease disappeared before acceptance update")

    def mark_notification_retry(
        self, pending: PendingNotification, error: str
    ) -> None:
        now = int(time.time())
        schedule = (30, 60, 120, 300, 900, 3600)
        delay = schedule[min(max(pending.attempts - 1, 0), len(schedule) - 1)]
        with self._immediate_transaction():
            cursor = self.connection.execute(
                """
                UPDATE notifications
                SET state = 'retry',
                    next_attempt_at = ?,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE message_seq = ? AND target_id = ?
                  AND state = 'leased' AND lease_token = ?
                """,
                (
                    now + delay,
                    error[:500],
                    now,
                    pending.seq,
                    pending.target_id,
                    pending.lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("notification lease disappeared before retry update")

    def message_count(self, instance_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        return int(row["count"])

    def notification_state(self, seq: int, target_id: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT state FROM notifications
            WHERE message_seq = ? AND target_id = ?
            """,
            (seq, target_id),
        ).fetchone()
        return str(row["state"]) if row is not None else None

    def messages_ready_for_device_cleanup(
        self,
        instance_id: str,
        message_keys: Sequence[str],
    ) -> bool:
        """Confirm that exact modem messages are durable and fully dispatched.

        Device-side deletion is deliberately stricter than ordinary inbox
        retention. Every selected payload must exist in SQLite, and every
        notification job attached to it must already have been accepted by its
        upstream. Baseline messages have no notification rows and are eligible.
        """

        keys = _validate_cleanup_keys(message_keys)
        return _cleanup_ready_count(
            self.connection,
            instance_id,
            keys,
        ) == len(keys)

    def run_daily_maintenance(self, instance_id: str, retention_days: int) -> int:
        """Optionally expire one instance and make a non-blocking checkpoint."""

        today = datetime.now(timezone.utc).date().isoformat()
        key = f"last_maintenance_date:{instance_id}"
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is not None and row["value"] == today:
            return 0

        deleted = 0
        with self._immediate_transaction():
            if retention_days > 0:
                cutoff = int(time.time()) - retention_days * 86400
                cursor = self.connection.execute(
                    """
                    DELETE FROM messages
                    WHERE instance_id = ?
                      AND discovered_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM notifications
                          WHERE notifications.message_seq = messages.seq
                            AND notifications.state != 'accepted_upstream'
                      )
                    """,
                    (instance_id, cutoff),
                )
                deleted = cursor.rowcount
                if deleted:
                    self._bump_view_revision_locked()
            self.connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, today),
            )
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return deleted

    def export_readonly_snapshot(self, destination: Path, mode: int = 0o640) -> bool:
        """Atomically export a single-file read-only view for the inbox service.

        The destination directory must already exist. In production it is a
        root-owned, setgid directory readable by the dedicated inbox group.
        Returns False when an existing snapshot already has the current view
        revision, avoiding unnecessary flash writes.
        """

        destination = Path(os.path.abspath(destination))
        if destination == self.path:
            raise StoreError(
                "snapshot destination must differ from the authoritative database"
            )
        if destination.exists():
            try:
                if os.path.samefile(self.path, destination):
                    raise StoreError(
                        "snapshot destination must not alias "
                        "the authoritative database"
                    )
            except OSError as exc:
                raise StoreError(
                    f"cannot compare snapshot destination with source: {exc}"
                ) from exc
        parent = destination.parent
        try:
            parent_stat = os.lstat(parent)
        except OSError as exc:
            raise StoreError(
                f"snapshot directory is unavailable: {parent}"
            ) from exc
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
            raise StoreError(f"snapshot parent must be a real directory: {parent}")
        if parent_stat.st_uid != os.geteuid() or parent_stat.st_mode & 0o022:
            raise StoreError(
                f"snapshot parent must be owner-controlled and not writable by group/other: {parent}"
            )
        if mode & 0o022:
            raise StoreError("snapshot mode must not grant group/other write access")
        destination_permissions_match = False
        if destination.exists() or destination.is_symlink():
            destination_stat = os.lstat(destination)
            if (
                not stat.S_ISREG(destination_stat.st_mode)
                or destination_stat.st_nlink != 1
                or destination_stat.st_uid != os.geteuid()
                or destination_stat.st_mode & 0o022
            ):
                raise StoreError(
                    f"snapshot destination has unsafe ownership or permissions: {destination}"
                )
            destination_permissions_match = (
                destination_stat.st_gid == parent_stat.st_gid
                and stat.S_IMODE(destination_stat.st_mode) == mode
            )

        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'view_revision'"
        ).fetchone()
        if row is None:
            raise StoreError("database has no view revision")
        source_revision = str(row["value"])

        if destination.exists():
            snapshot = None
            try:
                snapshot = sqlite3.connect(
                    destination.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=2,
                )
                snapshot_row = snapshot.execute(
                    "SELECT value FROM metadata WHERE key = 'view_revision'"
                ).fetchone()
                if (
                    snapshot_row is not None
                    and str(snapshot_row[0]) == source_revision
                    and destination_permissions_match
                ):
                    return False
            except sqlite3.Error:
                pass
            finally:
                if snapshot is not None:
                    snapshot.close()

        file_descriptor = -1
        temporary_path: Path | None = None
        snapshot_connection = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".inbox-snapshot-",
                suffix=".sqlite3",
                dir=str(parent),
            )
            temporary_path = Path(temporary_name)
            os.close(file_descriptor)
            file_descriptor = -1
            snapshot_connection = sqlite3.connect(str(temporary_path), timeout=5)
            self.connection.backup(snapshot_connection)
            snapshot_connection.execute("PRAGMA journal_mode=DELETE")
            check = snapshot_connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise StoreError("exported inbox snapshot failed SQLite quick_check")
            snapshot_connection.close()
            snapshot_connection = None

            os.chown(temporary_path, os.geteuid(), parent_stat.st_gid)
            os.chmod(temporary_path, mode)
            with temporary_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"cannot export inbox snapshot: {exc}") from exc
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            if snapshot_connection is not None:
                snapshot_connection.close()
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        return True
