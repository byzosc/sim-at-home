#!/usr/bin/env python3
"""Loopback-only, read-only HTTP inbox for exported SMS SQLite snapshots."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit


DEFAULT_SNAPSHOT = Path(
    "/overlay/sim-card-forwarding/inbox-view/inbox.sqlite3"
)
DEFAULT_ALLOWLIST = Path("/etc/sim-card-inbox/users.allow")
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
MAX_SQLITE_SEQUENCE = (1 << 63) - 1

_COMMON_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#155eef">
  <title>SIM 短信收件箱</title>
  <style nonce="__CSP_NONCE__">
    :root {
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #182230;
      --muted: #667085;
      --line: #e4e7ec;
      --accent: #155eef;
      --danger: #b42318;
      --new: #eff4ff;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #101828;
        --panel: #1d2939;
        --text: #f2f4f7;
        --muted: #98a2b3;
        --line: #344054;
        --accent: #84adff;
        --danger: #fda29b;
        --new: #173b6c;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }
    main { width: min(900px, 100%); margin: 0 auto; padding: 18px 14px 48px; }
    header {
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }
    h1 { font-size: 1.35rem; margin: 0; }
    button, input {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
    }
    button { cursor: pointer; padding: 9px 13px; }
    button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    .toolbar { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-bottom: 12px; }
    input { min-width: 0; width: 100%; padding: 10px 12px; }
    .status {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      color: var(--muted);
      font-size: .88rem;
      margin-bottom: 12px;
      padding: 10px 12px;
    }
    .status.danger { color: var(--danger); }
    #messages { display: grid; gap: 10px; }
    article {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 13px;
      overflow-wrap: anywhere;
    }
    article.unread { background: var(--new); border-color: var(--accent); }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 12px;
      color: var(--muted);
      font-size: .82rem;
      margin-bottom: 8px;
    }
    .sender { color: var(--text); font-weight: 650; }
    .body { margin: 0; white-space: pre-wrap; }
    .empty { color: var(--muted); padding: 32px 10px; text-align: center; }
    .copy { float: right; margin: 8px 0 0 10px; padding: 5px 9px; font-size: .8rem; }
    @media (max-width: 520px) {
      main { padding-top: 12px; }
      header { align-items: flex-start; }
      .toolbar { grid-template-columns: 1fr; }
      button { min-height: 42px; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>SIM 短信收件箱</h1>
      <div id="updated" class="meta">正在读取本地队列…</div>
    </div>
    <button id="refresh" class="primary" type="button">刷新</button>
  </header>
  <div class="toolbar">
    <input id="filter" type="search" placeholder="搜索号码、设备或短信内容"
           autocomplete="off" aria-label="搜索短信">
    <button id="mark-read" type="button">全部标为已读</button>
  </div>
  <div id="status" class="status" role="status" aria-live="polite">正在连接…</div>
  <section id="messages" aria-label="短信列表"></section>
</main>
<script nonce="__CSP_NONCE__">
(() => {
  "use strict";
  const cursorKey = "sim-inbox-last-seen-seq-v1";
  const state = { messages: [], maxSeq: 0, lastSeen: Number(localStorage.getItem(cursorKey) || 0) };
  const messagesNode = document.getElementById("messages");
  const statusNode = document.getElementById("status");
  const updatedNode = document.getElementById("updated");
  const filterNode = document.getElementById("filter");
  const refreshNode = document.getElementById("refresh");

  function createText(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  }

  function render() {
    const query = filterNode.value.trim().toLocaleLowerCase();
    const visible = state.messages.filter((message) =>
      [message.label, message.instance_id, message.sender, message.body, message.date]
        .join("\\n").toLocaleLowerCase().includes(query)
    );
    messagesNode.replaceChildren();
    if (!visible.length) {
      messagesNode.appendChild(createText("div", "empty", query ? "没有匹配的短信" : "暂无可显示的短信"));
      return;
    }
    for (const message of [...visible].reverse()) {
      const card = document.createElement("article");
      if (message.seq > state.lastSeen) card.classList.add("unread");
      const meta = createText(
        "div",
        "meta",
        `${message.label || message.instance_id} · ${message.direction === "received" ? "收到" : "发出"} · ${message.date || "时间未知"}`
      );
      const sender = createText("div", "sender", message.sender || "未知号码");
      const body = createText("p", "body", message.body || "");
      const copy = createText("button", "copy", "复制");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(message.body || "");
          copy.textContent = "已复制";
        } catch (_error) {
          copy.textContent = "复制失败";
        }
      });
      card.append(meta, sender, copy, body);
      messagesNode.appendChild(card);
    }
  }

  async function fetchJson(path) {
    const response = await fetch(path, { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function fetchAllMessages() {
    const collected = [];
    let afterSeq = 0;
    for (;;) {
      const page = await fetchJson(`/api/v1/messages?after_seq=${afterSeq}&limit=200`);
      collected.push(...page.messages);
      if (!page.has_more) return collected;
      const next = Number(page.next_after_seq);
      if (!Number.isSafeInteger(next) || next <= afterSeq) {
        throw new Error("invalid inbox cursor");
      }
      afterSeq = next;
    }
  }

  async function refresh() {
    refreshNode.disabled = true;
    statusNode.classList.remove("danger");
    statusNode.textContent = "正在刷新…";
    try {
      const status = await fetchJson("/api/v1/status");
      state.maxSeq = Number(status.max_seq || 0);
      state.messages = await fetchAllMessages();
      const saturation = status.modem_saturation || { any_saturated: false, instances: [] };
      const saturatedNames = saturation.instances
        .filter((item) => item.saturated)
        .map((item) => item.label || item.instance_id);
      statusNode.textContent = saturatedNames.length
        ? `共保存 ${status.count} 条；警告：${saturatedNames.join("、")} 的短信页已满`
        : `共保存 ${status.count} 条；设备短信页未报告满载`;
      if (saturatedNames.length) statusNode.classList.add("danger");
      updatedNode.textContent = `数据快照：${status.database_timestamp}`;
      render();
    } catch (_error) {
      statusNode.classList.add("danger");
      statusNode.textContent = "读取失败。请确认已通过 Tailscale 访问，稍后再试。";
      updatedNode.textContent = "未能读取本地队列";
    } finally {
      refreshNode.disabled = false;
    }
  }

  filterNode.addEventListener("input", render);
  refreshNode.addEventListener("click", refresh);
  document.getElementById("mark-read").addEventListener("click", () => {
    state.lastSeen = state.maxSeq;
    localStorage.setItem(cursorKey, String(state.lastSeen));
    render();
  });
  refresh();
})();
</script>
</body>
</html>
"""


class InboxUnavailable(RuntimeError):
    """The read-only view is absent, invalid, or changed during opening."""


class InboxApplication:
    """Read-only access to a replace-in-place SQLite snapshot and login list."""

    def __init__(
        self,
        snapshot_path: Path,
        allowlist_path: Path,
        *,
        trusted_owner_uid: int = 0,
    ):
        self.snapshot_path = Path(
            os.path.abspath(os.fspath(snapshot_path))
        )
        self.allowlist_path = Path(
            os.path.abspath(os.fspath(allowlist_path))
        )
        self.trusted_owner_uid = trusted_owner_uid

    def _validate_file(self, path: Path, description: str) -> os.stat_result:
        try:
            parent_stat = os.lstat(path.parent)
        except OSError as exc:
            raise InboxUnavailable(f"{description} parent is unavailable") from exc
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or stat.S_ISLNK(parent_stat.st_mode)
            or parent_stat.st_uid != self.trusted_owner_uid
            or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise InboxUnavailable(
                f"{description} parent has unsafe ownership or permissions"
            )
        try:
            file_stat = os.lstat(path)
        except OSError as exc:
            raise InboxUnavailable(f"{description} is unavailable") from exc
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or stat.S_ISLNK(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_uid != self.trusted_owner_uid
        ):
            raise InboxUnavailable(
                f"{description} must be a trusted, non-linked regular file"
            )
        if file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise InboxUnavailable(
                f"{description} must not be group- or world-writable"
            )
        return file_stat

    def _load_allowlist(self) -> set[str]:
        self._validate_file(self.allowlist_path, "login allowlist")
        try:
            raw = self.allowlist_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InboxUnavailable("login allowlist cannot be read") from exc

        allowed: set[str] = set()
        for line in raw.splitlines():
            if not line:
                continue
            if line != line.strip() or any(ord(character) < 32 for character in line):
                raise InboxUnavailable("login allowlist has an invalid entry")
            allowed.add(line)
        if not allowed:
            raise InboxUnavailable("login allowlist is empty")
        return allowed

    def login_allowed(self, values: list[str] | None) -> bool:
        if values is None or len(values) != 1:
            return False
        login = values[0]
        if not login or "\x00" in login or "\r" in login or "\n" in login:
            return False
        try:
            allowed = self._load_allowlist()
        except InboxUnavailable:
            return False
        return login in allowed

    @contextmanager
    def open_snapshot(self) -> Iterator[tuple[sqlite3.Connection, os.stat_result]]:
        connection: sqlite3.Connection | None = None
        last_error: BaseException | None = None
        snapshot_stat: os.stat_result | None = None
        for _attempt in range(3):
            try:
                before = self._validate_file(
                    self.snapshot_path, "SMS inbox snapshot"
                )
                connection = sqlite3.connect(
                    self.snapshot_path.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=2,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                query_only = connection.execute("PRAGMA query_only").fetchone()
                if query_only is None or int(query_only[0]) != 1:
                    raise InboxUnavailable("SQLite query_only could not be enabled")
                after = os.lstat(self.snapshot_path)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    connection.close()
                    connection = None
                    continue
                snapshot_stat = after
                break
            except (OSError, sqlite3.Error, InboxUnavailable) as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                    connection = None
                if isinstance(exc, InboxUnavailable):
                    break
        if connection is None or snapshot_stat is None:
            raise InboxUnavailable(
                "SMS inbox snapshot is unavailable"
            ) from last_error
        try:
            yield connection, snapshot_stat
        finally:
            connection.close()

    def validate_startup(self) -> None:
        """Fail before listening when credentials or the DB view are unsafe."""

        self._load_allowlist()
        with self.open_snapshot() as (connection, _snapshot_stat):
            required_columns = {
                "seq",
                "instance_id",
                "number",
                "content",
                "message_date",
                "direction",
                "payload_available",
            }
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(messages)"
                ).fetchall()
            }
            if not required_columns.issubset(columns):
                raise InboxUnavailable(
                    "SMS inbox snapshot has an unsupported messages schema"
                )
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]) != "ok":
                raise InboxUnavailable(
                    "SMS inbox snapshot failed SQLite quick_check"
                )

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (name,),
        ).fetchone()
        return row is not None

    def messages(self, after_seq: int, limit: int) -> dict[str, object]:
        with self.open_snapshot() as (connection, _snapshot_stat):
            has_labels = self._has_table(connection, "instance_labels")
            if has_labels:
                label_expression = (
                    "COALESCE(NULLIF(labels.label, ''), messages.instance_id)"
                )
                label_join = (
                    "LEFT JOIN instance_labels AS labels "
                    "ON labels.instance_id = messages.instance_id"
                )
            else:
                label_expression = "messages.instance_id"
                label_join = ""
            rows = connection.execute(
                f"""
                SELECT
                    messages.seq,
                    messages.instance_id,
                    {label_expression} AS label,
                    messages.number AS sender,
                    messages.content AS body,
                    messages.message_date AS date,
                    messages.direction
                FROM messages
                {label_join}
                WHERE messages.seq > ?
                  AND messages.payload_available = 1
                ORDER BY messages.seq ASC
                LIMIT ?
                """,
                (after_seq, limit + 1),
            ).fetchall()

        has_more = len(rows) > limit
        selected = rows[:limit]
        messages = [
            {
                "seq": int(row["seq"]),
                "instance_id": str(row["instance_id"]),
                "label": str(row["label"]),
                "sender": str(row["sender"]),
                "body": str(row["body"]),
                "date": str(row["date"]),
                "direction": str(row["direction"]),
            }
            for row in selected
        ]
        next_after_seq = messages[-1]["seq"] if messages else after_seq
        return {
            "after_seq": after_seq,
            "next_after_seq": next_after_seq,
            "has_more": has_more,
            "messages": messages,
        }

    def status(self) -> dict[str, object]:
        with self.open_snapshot() as (connection, snapshot_stat):
            message_row = connection.execute(
                """
                SELECT
                    MIN(seq) AS min_seq,
                    MAX(seq) AS max_seq,
                    COUNT(*) AS count
                FROM messages
                WHERE payload_available = 1
                """
            ).fetchone()
            if message_row is None:
                raise InboxUnavailable("snapshot has no messages table")

            labels_available = self._has_table(connection, "instance_labels")
            saturation_available = self._has_table(
                connection, "modem_page_status"
            )
            saturation_rows: list[sqlite3.Row] = []
            if saturation_available:
                if labels_available:
                    label_expression = (
                        "COALESCE(NULLIF(labels.label, ''), status.instance_id)"
                    )
                    label_join = (
                        "LEFT JOIN instance_labels AS labels "
                        "ON labels.instance_id = status.instance_id"
                    )
                else:
                    label_expression = "status.instance_id"
                    label_join = ""
                saturation_rows = connection.execute(
                    f"""
                    SELECT
                        status.instance_id,
                        {label_expression} AS label,
                        status.message_count,
                        status.page_limit,
                        status.saturated,
                        status.checked_at
                    FROM modem_page_status AS status
                    {label_join}
                    ORDER BY status.instance_id
                    """
                ).fetchall()

        saturation_instances = [
            {
                "instance_id": str(row["instance_id"]),
                "label": str(row["label"]),
                "message_count": int(row["message_count"]),
                "page_limit": int(row["page_limit"]),
                "saturated": bool(row["saturated"]),
                "checked_at": int(row["checked_at"]),
            }
            for row in saturation_rows
        ]
        database_timestamp = datetime.fromtimestamp(
            snapshot_stat.st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return {
            "min_seq": (
                int(message_row["min_seq"])
                if message_row["min_seq"] is not None
                else None
            ),
            "max_seq": (
                int(message_row["max_seq"])
                if message_row["max_seq"] is not None
                else None
            ),
            "count": int(message_row["count"]),
            "database_timestamp": database_timestamp,
            "modem_saturation": {
                "any_saturated": any(
                    item["saturated"] for item in saturation_instances
                ),
                "instances": saturation_instances,
            },
        }


class InboxRequestHandler(BaseHTTPRequestHandler):
    """HTTP adapter. A bound subclass receives an InboxApplication instance."""

    application: InboxApplication
    protocol_version = "HTTP/1.1"
    server_version = "SIMInbox"
    sys_version = ""

    def log_message(self, format_string: str, *args: object) -> None:
        # Request targets and response bodies can contain private data. The
        # production supervisor should log lifecycle/errors, not SMS requests.
        return

    def _authorized(self) -> bool:
        values = self.headers.get_all("Tailscale-User-Login")
        if self.application.login_allowed(values):
            return True
        self._send_json(403, {"error": "forbidden"})
        return False

    def _security_headers(self, nonce: str | None = None) -> dict[str, str]:
        headers = dict(_COMMON_SECURITY_HEADERS)
        if nonce is None:
            headers["Content-Security-Policy"] = (
                "default-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'"
            )
        else:
            headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
            )
        return headers

    def _send(
        self,
        status_code: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
        nonce: str | None = None,
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._security_headers(nonce).items():
            self.send_header(key, value)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(
        self,
        status_code: int,
        payload: dict[str, object],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send(
            status_code,
            body,
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _send_html(self) -> None:
        nonce = secrets.token_urlsafe(18)
        body = _HTML_TEMPLATE.replace("__CSP_NONCE__", nonce).encode("utf-8")
        self._send(
            200,
            body,
            "text/html; charset=utf-8",
            nonce=nonce,
        )

    @staticmethod
    def _decimal_parameter(
        query: dict[str, list[str]],
        name: str,
        default: int,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        values = query.get(name)
        if values is None:
            return default
        if len(values) != 1 or not values[0] or not values[0].isascii():
            raise ValueError(name)
        if not all("0" <= character <= "9" for character in values[0]):
            raise ValueError(name)
        value = int(values[0])
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(name)
        return value

    def do_GET(self) -> None:
        if not self._authorized():
            return
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment:
            self._send_json(404, {"error": "not_found"})
            return

        if target.path == "/":
            if target.query:
                self._send_json(400, {"error": "invalid_query"})
                return
            self._send_html()
            return

        if target.path == "/api/v1/messages":
            try:
                query = parse_qs(
                    target.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                if set(query) - {"after_seq", "limit"}:
                    raise ValueError("unknown parameter")
                after_seq = self._decimal_parameter(
                    query,
                    "after_seq",
                    0,
                    0,
                    MAX_SQLITE_SEQUENCE,
                )
                limit = self._decimal_parameter(
                    query,
                    "limit",
                    DEFAULT_LIMIT,
                    1,
                    MAX_LIMIT,
                )
            except (ValueError, UnicodeError):
                self._send_json(400, {"error": "invalid_query"})
                return
            try:
                payload = self.application.messages(after_seq, limit)
            except (InboxUnavailable, sqlite3.Error):
                self._send_json(503, {"error": "inbox_unavailable"})
                return
            self._send_json(200, payload)
            return

        if target.path == "/api/v1/status":
            if target.query:
                self._send_json(400, {"error": "invalid_query"})
                return
            try:
                payload = self.application.status()
            except (InboxUnavailable, sqlite3.Error):
                self._send_json(503, {"error": "inbox_unavailable"})
                return
            self._send_json(200, payload)
            return

        self._send_json(404, {"error": "not_found"})

    def _method_not_allowed(self) -> None:
        if not self._authorized():
            return
        self._send_json(
            405,
            {"error": "method_not_allowed"},
            extra_headers={"Allow": "GET"},
        )

    do_HEAD = _method_not_allowed
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed


def make_handler(application: InboxApplication) -> type[InboxRequestHandler]:
    """Bind an application instance without introducing module-global state."""

    class BoundInboxRequestHandler(InboxRequestHandler):
        pass

    BoundInboxRequestHandler.application = application
    return BoundInboxRequestHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve an exported SMS inbox to Tailscale Serve on loopback"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"read-only SQLite snapshot (default: {DEFAULT_SNAPSHOT})",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help=f"Tailscale login allowlist (default: {DEFAULT_ALLOWLIST})",
    )
    parser.add_argument(
        "--bind",
        default=DEFAULT_BIND,
        help=f"loopback IPv4 address (default: {DEFAULT_BIND})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"loopback TCP port (default: {DEFAULT_PORT})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        bind_address = ipaddress.ip_address(arguments.bind)
    except ValueError:
        parser.error("--bind must be a literal loopback IPv4 address")
    if bind_address.version != 4 or not bind_address.is_loopback:
        parser.error("--bind must be a loopback IPv4 address")
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    application = InboxApplication(arguments.database, arguments.allowlist)
    try:
        application.validate_startup()
    except InboxUnavailable as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer(
        (arguments.bind, arguments.port),
        make_handler(application),
    )
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
