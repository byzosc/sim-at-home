import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mf833u1_forwarder as forwarder
from sms_store import InboxStore


class ForwarderTests(unittest.TestCase):
    def instance(self, **overrides):
        values = {
            "path": Path("test.conf"),
            "instance_id": "modem-1",
            "label": "测试卡",
            "base_url": "http://192.168.0.1",
            "interface": "eth2",
            "expected_mac": "00:00:00:00:00:00",
            "expected_usb_path": "2-1",
            "expected_usb_id": "19d2:1706",
            "mode": "none",
            "bark_server": "https://api.day.app",
            "bark_key": "",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "ntfy_server": "",
            "ntfy_topic": "",
            "ntfy_token": "",
            "notification_content": "summary",
            "first_run": "baseline",
            "include_sent": False,
            "retention_days": 0,
            "device_sms_capacity": 100,
            "device_cleanup_threshold": 0,
            "device_cleanup_batch": 30,
        }
        values.update(overrides)
        return forwarder.Instance(**values)

    def test_build_sms_url(self):
        url = forwarder.build_sms_url("http://192.168.0.1")
        self.assertIn("/goform/goform_get_cmd_process?", url)
        self.assertIn("cmd=sms_data_total", url)
        self.assertIn("order_by=order+by+id+desc", url)

    def test_ntfy_config_requires_https_topic_and_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "modem.conf"
            config.write_text(
                """
[device]
id=modem-1
base_url=http://192.168.0.1

[notifications]
mode=ntfy
ntfy_server=http://ntfy.example.com
ntfy_topic=personal-sms
ntfy_token=test-token
""".lstrip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(forwarder.ForwarderError, "HTTPS"):
                forwarder.load_instance(config)

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "http://ntfy.example.com",
                    "https://ntfy.example.com",
                ).replace("ntfy_token=test-token", "ntfy_token="),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(forwarder.ForwarderError, "ntfy_token"):
                forwarder.load_instance(config)

    def test_cleanup_config_defaults_disabled_and_validates_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "modem.conf"
            config.write_text(
                """
[device]
id=modem-1
base_url=http://192.168.0.1

[notifications]
mode=none

[runtime]
device_sms_capacity=100
device_cleanup_threshold=0
device_cleanup_batch=30
""".lstrip(),
                encoding="utf-8",
            )
            instance = forwarder.load_instance(config)
            self.assertEqual(instance.device_sms_capacity, 100)
            self.assertEqual(instance.device_cleanup_threshold, 0)
            self.assertEqual(instance.device_cleanup_batch, 30)

            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "device_cleanup_threshold=0",
                    "device_cleanup_threshold=30",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(forwarder.ForwarderError, "smaller"):
                forwarder.load_instance(config)

    def test_notification_post_rejects_redirect_without_forwarding_auth(self):
        received_authorization = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/sink")
                    self.end_headers()
                    return
                received_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"event":"message","id":"unexpected"}')

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(
                forwarder.ForwarderError,
                "notification request failed",
            ):
                forwarder.post_json(
                    f"http://127.0.0.1:{server.server_port}/redirect",
                    {"message": "test"},
                    headers={"Authorization": "Bearer must-not-be-forwarded"},
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(received_authorization, [])

    @patch("mf833u1_forwarder._fetch_modem_json")
    def test_locked_sim_is_reported(self, fetch):
        fetch.return_value = {"modem_main_state": "modem_waitpin", "pin_status": "1"}
        with self.assertRaisesRegex(forwarder.ForwarderError, "PIN"):
            forwarder.check_modem_status(self.instance())

    def test_decode_utf16be_chinese(self):
        encoded = "验证码1234".encode("utf-16-be").hex()
        self.assertEqual(forwarder.decode_content(encoded), "验证码1234")

    def test_decode_utf16be_single_character(self):
        encoded = "2".encode("utf-16-be").hex()
        self.assertEqual(forwarder.decode_content(encoded), "2")

    def test_plain_content_is_preserved(self):
        self.assertEqual(forwarder.decode_content("hello"), "hello")

    def test_decode_utf16be_ascii_lookalike_characters(self):
        """UTF-16BE 两字节恰好都是可打印 ASCII 的汉字不能被当成 UTF-8。

        旧实现先试 UTF-8,这些字会被解成乱码:
        你→"O`" 是→"f/" 好→"Y}" 在→"W(" 吧→"T'" 个→"N*" 中→"N-"
        """
        for character in "你是好在吧个中":
            with self.subTest(character=character):
                encoded = character.encode("utf-16-be").hex()
                self.assertEqual(forwarder.decode_content(encoded), character)

    def test_decode_utf16be_short_chinese_phrases(self):
        for phrase in ("你好", "你在吗", "是的", "中国", "好的谢谢"):
            with self.subTest(phrase=phrase):
                encoded = phrase.encode("utf-16-be").hex()
                self.assertEqual(forwarder.decode_content(encoded), phrase)

    def test_decode_utf16be_verification_code_sms(self):
        text = "【测试】您的验证码是123456,请勿泄露给他人。"
        encoded = text.encode("utf-16-be").hex()
        self.assertEqual(forwarder.decode_content(encoded), text)

    def test_decode_utf16be_ascii_only_sms(self):
        for text in ("123456", "OK", "Your code is 8842"):
            with self.subTest(text=text):
                encoded = text.encode("utf-16-be").hex()
                self.assertEqual(forwarder.decode_content(encoded), text)

    def test_decode_utf8_payload_still_supported(self):
        """非 UCS2 固件返回 UTF-8 十六进制时仍要能解出来(兜底路径)。"""
        encoded = "验证码1234".encode("utf-8").hex()
        self.assertEqual(forwarder.decode_content(encoded), "验证码1234")

    def test_decode_ascii_hex_payload(self):
        """纯 ASCII 的十六进制不能被误读成 CJK 扩展区生僻字。"""
        encoded = "123456".encode("ascii").hex()
        self.assertEqual(forwarder.decode_content(encoded), "123456")

    def test_decode_verification_codes_both_encodings(self):
        """验证码是本机最主要的用途,两种可能的固件编码下都必须正确。

        UCS2 情形本身无歧义:ASCII 字符的高字节是 0x00,含 NUL 的 UTF-8 结果
        会被 _looks_readable 直接否决。ASCII 十六进制情形则依赖 _text_score
        ——"000000" 按 UTF-16BE 会读成 U+3030(〰),必须判负。
        """
        codes = ("123456", "8842", "000000", "111111", "0000", "999999")
        for code in codes:
            with self.subTest(code=code, encoding="utf-16-be"):
                self.assertEqual(
                    forwarder.decode_content(code.encode("utf-16-be").hex()), code
                )
            with self.subTest(code=code, encoding="ascii"):
                self.assertEqual(
                    forwarder.decode_content(code.encode("ascii").hex()), code
                )

    def test_decode_synthetic_verification_sms(self):
        messages = (
            "【示例服务】测试验证码123456，5分钟内有效，请勿泄露",
            "验证码：123456，您正在登录，若非本人操作请忽略",
            "Your verification code is 000000. Do not share it.",
        )
        for text in messages:
            with self.subTest(text=text[:16]):
                encoded = text.encode("utf-16-be").hex()
                self.assertEqual(forwarder.decode_content(encoded), text)

    def test_non_hex_content_is_untouched(self):
        for text in ("你好", "验证码 1234", "12345"):
            with self.subTest(text=text):
                self.assertEqual(forwarder.decode_content(text), text)

    def test_pin_credential_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credential = Path(temp_dir) / "modem-1.pin"
            credential.write_text("1234\n", encoding="ascii")
            credential.chmod(0o600)
            self.assertEqual(
                forwarder.read_pin_credential(
                    credential, expected_uid=os.geteuid()
                ),
                "1234",
            )

            credential.chmod(0o644)
            with self.assertRaisesRegex(forwarder.ForwarderError, "0600"):
                forwarder.read_pin_credential(
                    credential, expected_uid=os.geteuid()
                )

    def test_pin_credential_rejects_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credential = root / "real.pin"
            credential.write_text("1234\n", encoding="ascii")
            credential.chmod(0o600)

            symlink = root / "symlink.pin"
            symlink.symlink_to(credential)
            with self.assertRaises(forwarder.ForwarderError):
                forwarder.read_pin_credential(
                    symlink, expected_uid=os.geteuid()
                )

            hardlink = root / "hardlink.pin"
            os.link(credential, hardlink)
            with self.assertRaisesRegex(forwarder.ForwarderError, "non-linked"):
                forwarder.read_pin_credential(
                    credential, expected_uid=os.geteuid()
                )

    @patch("mf833u1_forwarder.time.sleep")
    @patch("mf833u1_forwarder._fetch_modem_json")
    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.read_pin_credential", return_value="1234")
    @patch("mf833u1_forwarder.verify_modem_identity")
    @patch("mf833u1_forwarder.os.geteuid", return_value=0)
    def test_auto_unlock_submits_once_and_removes_guard(
        self, geteuid, verify, read_pin, post, fetch, sleep
    ):
        post.return_value = {"result": "success"}
        fetch.return_value = {
            "modem_main_state": "modem_init_complete",
            "pinnumber": "3",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"

            status = forwarder.unlock_sim_if_needed(
                self.instance(),
                {"modem_main_state": "modem_waitpin", "pinnumber": "3"},
                credentials,
                root / "state",
            )

            self.assertEqual(status["modem_main_state"], "modem_init_complete")
            post.assert_called_once()
            self.assertEqual(post.call_args.args[1]["goformId"], "ENTER_PIN")
            self.assertEqual(post.call_args.args[1]["PinNumber"], "1234")
            self.assertFalse(
                (root / "state" / "unlock-attempts" / "modem-1.lock").exists()
            )

    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.read_pin_credential", return_value="1234")
    @patch("mf833u1_forwarder.verify_modem_identity")
    @patch("mf833u1_forwarder.os.geteuid", return_value=0)
    def test_auto_unlock_failure_guard_blocks_retries(
        self, geteuid, verify, read_pin, post
    ):
        post.return_value = {"result": "failure"}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"
            status = {"modem_main_state": "modem_waitpin", "pinnumber": "3"}

            with self.assertRaisesRegex(forwarder.ForwarderError, "锁定自动重试"):
                forwarder.unlock_sim_if_needed(
                    self.instance(), status, credentials, root / "state"
                )
            with self.assertRaisesRegex(forwarder.ForwarderError, "保护已触发"):
                forwarder.unlock_sim_if_needed(
                    self.instance(), status, credentials, root / "state"
                )
            post.assert_called_once()

    @patch("mf833u1_forwarder.time.sleep")
    @patch("mf833u1_forwarder._fetch_modem_json")
    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.read_pin_credential", return_value="1234")
    @patch("mf833u1_forwarder.verify_modem_identity")
    @patch("mf833u1_forwarder.os.geteuid", return_value=0)
    def test_inflight_guard_self_heals_when_request_never_reached_device(
        self, geteuid, verify, read_pin, post, fetch, sleep
    ):
        # Simulate a request failure before the modem receives the PIN.
        # The inflight guard must recover when no attempt was consumed.
        post.side_effect = [
            forwarder.ForwarderError("connection refused"),
            {"result": "success"},
        ]
        fetch.return_value = {
            "modem_main_state": "modem_init_complete",
            "pinnumber": "3",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"
            state_root = root / "state"
            status = {"modem_main_state": "modem_waitpin", "pinnumber": "3"}
            guard_path = state_root / "unlock-attempts" / "modem-1.lock"

            with self.assertRaises(forwarder.ForwarderError):
                forwarder.unlock_sim_if_needed(
                    self.instance(), status, credentials, state_root
                )
            self.assertTrue(guard_path.exists())
            self.assertEqual(
                json.loads(guard_path.read_text(encoding="utf-8"))["phase"], "inflight"
            )

            # 下一轮：仍等待 PIN 且剩余次数满额，证明上次请求没送达，应清锁重试
            result = forwarder.unlock_sim_if_needed(
                self.instance(), status, credentials, state_root
            )
            self.assertEqual(result["modem_main_state"], "modem_init_complete")
            self.assertFalse(guard_path.exists())
            self.assertEqual(post.call_count, 2)

    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.read_pin_credential", return_value="1234")
    @patch("mf833u1_forwarder.verify_modem_identity")
    @patch("mf833u1_forwarder.os.geteuid", return_value=0)
    def test_inflight_guard_still_blocks_when_an_attempt_was_consumed(
        self, geteuid, verify, read_pin, post
    ):
        # 剩余次数掉了一次，就无法排除「PIN 已送达但是错的」，
        # 此时必须继续阻塞，绝不能自愈重试。
        post.side_effect = forwarder.ForwarderError("connection refused")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"
            state_root = root / "state"
            guard_path = state_root / "unlock-attempts" / "modem-1.lock"

            with self.assertRaises(forwarder.ForwarderError):
                forwarder.unlock_sim_if_needed(
                    self.instance(),
                    {"modem_main_state": "modem_waitpin", "pinnumber": "3"},
                    credentials,
                    state_root,
                )
            self.assertTrue(guard_path.exists())

            with self.assertRaisesRegex(forwarder.ForwarderError, "保护已触发"):
                forwarder.unlock_sim_if_needed(
                    self.instance(),
                    {"modem_main_state": "modem_waitpin", "pinnumber": "2"},
                    credentials,
                    state_root,
                )
            self.assertTrue(guard_path.exists())
            post.assert_called_once()

    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.read_pin_credential", return_value="1234")
    @patch("mf833u1_forwarder.verify_modem_identity")
    @patch("mf833u1_forwarder.os.geteuid", return_value=0)
    def test_inflight_guard_still_blocks_when_device_identity_changed(
        self, geteuid, verify, read_pin, post
    ):
        # 锁是另一根棒子留下的，即使剩余次数满额也不能拿这张卡去重试。
        post.side_effect = forwarder.ForwarderError("connection refused")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            credentials = root / "credentials"
            state_root = root / "state"
            status = {"modem_main_state": "modem_waitpin", "pinnumber": "3"}

            with self.assertRaises(forwarder.ForwarderError):
                forwarder.unlock_sim_if_needed(
                    self.instance(), status, credentials, state_root
                )

            with self.assertRaisesRegex(forwarder.ForwarderError, "保护已触发"):
                forwarder.unlock_sim_if_needed(
                    self.instance(expected_usb_path="1-1"),
                    status,
                    credentials,
                    state_root,
                )
            post.assert_called_once()

    @patch("mf833u1_forwarder.time.sleep")
    @patch("mf833u1_forwarder._fetch_modem_json")
    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.read_pin_credential", return_value="1234")
    @patch("mf833u1_forwarder.verify_modem_identity")
    @patch("mf833u1_forwarder.os.geteuid", return_value=0)
    def test_auto_unlock_never_confirms_puk_state(
        self, geteuid, verify, read_pin, post, fetch, sleep
    ):
        post.return_value = {"result": "success"}
        fetch.return_value = {
            "modem_main_state": "modem_waitpuk",
            "pinnumber": "0",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(forwarder.ForwarderError, "永久锁定"):
                forwarder.unlock_sim_if_needed(
                    self.instance(),
                    {"modem_main_state": "modem_waitpin", "pinnumber": "3"},
                    root / "credentials",
                    root / "state",
                )
            guard = json.loads(
                (
                    root
                    / "state"
                    / "unlock-attempts"
                    / "modem-1.lock"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(guard["phase"], "blocked")
            post.assert_called_once()

    @patch("mf833u1_forwarder._post_modem_form")
    def test_auto_unlock_requires_all_three_attempts(self, post):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(forwarder.ForwarderError, "满额 3 次"):
                forwarder.unlock_sim_if_needed(
                    self.instance(),
                    {"modem_main_state": "modem_waitpin", "pinnumber": "2"},
                    root / "credentials",
                    root / "state",
                )
            post.assert_not_called()

    @patch("mf833u1_forwarder.subprocess.run")
    def test_pin_is_sent_via_stdin_with_curl_config_disabled(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"result":"success"}',
            stderr="",
        )
        forwarder._post_modem_form(
            self.instance(),
            {
                "isTest": "false",
                "goformId": "ENTER_PIN",
                "PinNumber": "1234",
            },
        )
        command = run.call_args.args[0]
        self.assertEqual(command[1], "--disable")
        self.assertIn("--noproxy", command)
        self.assertNotIn("1234", command)
        self.assertIn("PinNumber=1234", run.call_args.kwargs["input"])

    def test_auto_unlock_does_not_clear_guard_while_waiting_for_puk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guard = root / "state" / "unlock-attempts" / "modem-1.lock"
            guard.parent.mkdir(parents=True)
            guard.write_text("{}\n", encoding="utf-8")

            status = forwarder.unlock_sim_if_needed(
                self.instance(),
                {"modem_main_state": "modem_waitpuk", "pinnumber": "0"},
                root / "credentials",
                root / "state",
            )

            self.assertEqual(status["modem_main_state"], "modem_waitpuk")
            self.assertTrue(guard.exists())

    def test_sent_messages_are_filtered_by_default(self):
        payload = {
            "messages": [
                {"id": "1", "tag": "0", "content": "hello"},
                {"id": "2", "tag": "2", "content": "sent"},
            ]
        }
        messages = forwarder.parse_messages(self.instance(), payload)
        self.assertEqual([message.modem_id for message in messages], ["1"])

    def test_multipart_sms_is_only_parsed_after_all_segments_arrive(self):
        incomplete = {
            "id": "1",
            "tag": "0",
            "content": "partial",
            "concat_sms_total": "2",
            "concat_sms_received": "1",
            "received_all_concat_sms": "0",
        }
        complete = dict(
            incomplete,
            content="complete",
            concat_sms_received="2",
            received_all_concat_sms="1",
        )
        self.assertEqual(
            forwarder.parse_messages(self.instance(), {"messages": [incomplete]}),
            [],
        )
        self.assertEqual(
            [
                message.content
                for message in forwarder.parse_messages(
                    self.instance(), {"messages": [complete]}
                )
            ],
            ["complete"],
        )

    def _device_payload(self, count):
        start = datetime(2026, 7, 26, 0, 0, 0)
        messages = []
        for index in range(count):
            value = start + timedelta(minutes=index)
            messages.append(
                {
                    "id": str(index + 1),
                    "tag": "0",
                    "number": "sender",
                    "content": f"message-{index + 1}",
                    "date": value.strftime("%y,%m,%d,%H,%M,%S,+32"),
                }
            )
        return {"messages": list(reversed(messages))}

    @patch("mf833u1_forwarder._wait_for_device_deletion", return_value=60)
    @patch("mf833u1_forwarder._post_modem_form", return_value={"result": "success"})
    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_cleanup_at_threshold_deletes_exact_oldest_durable_batch(
        self, fetch, post, wait
    ):
        payload = self._device_payload(90)
        fetch.return_value = payload
        instance = self.instance(
            device_cleanup_threshold=90,
            device_cleanup_batch=30,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir) / "state"
            snapshot_dir = Path(temp_dir) / "view"
            snapshot_dir.mkdir()
            snapshot = snapshot_dir / "inbox.sqlite3"
            with InboxStore(state_root / "inbox.sqlite3") as store:
                for raw in payload["messages"]:
                    message = forwarder.message_from_dict(instance, raw)
                    store.store_message(
                        instance_id=instance.instance_id,
                        message_key=message.key,
                        modem_id=message.modem_id,
                        number=message.number,
                        content=message.content,
                        message_date=message.date,
                        direction=message.direction,
                        channels=(),
                        baseline=True,
                        source="baseline",
                    )
                store.export_readonly_snapshot(snapshot)

            self.assertEqual(
                forwarder.cleanup_modem_messages(instance, state_root, snapshot),
                30,
            )

        fields = post.call_args.args[1]
        self.assertEqual(fields["goformId"], "DELETE_SMS")
        self.assertEqual(
            fields["msg_id"],
            ";".join(str(value) for value in range(1, 31)) + ";",
        )
        self.assertEqual(len(wait.call_args.args[1]), 30)
        self.assertEqual(len(wait.call_args.args[2]), 60)

    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_cleanup_below_threshold_never_posts(self, fetch, post):
        fetch.return_value = self._device_payload(89)
        instance = self.instance(device_cleanup_threshold=90)
        self.assertEqual(
            forwarder.cleanup_modem_messages(
                instance,
                Path("unused-state"),
                Path("unused-snapshot"),
            ),
            0,
        )
        post.assert_not_called()

    @patch("mf833u1_forwarder._post_modem_form")
    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_cleanup_refuses_unpersisted_oldest_message(self, fetch, post):
        payload = self._device_payload(90)
        fetch.return_value = payload
        instance = self.instance(
            device_cleanup_threshold=90,
            device_cleanup_batch=30,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir) / "state"
            snapshot_dir = Path(temp_dir) / "view"
            snapshot_dir.mkdir()
            snapshot = snapshot_dir / "inbox.sqlite3"
            with InboxStore(state_root / "inbox.sqlite3") as store:
                # Deliberately omit modem ID 1, which is in the oldest batch.
                for raw in payload["messages"][:-1]:
                    message = forwarder.message_from_dict(instance, raw)
                    store.store_message(
                        instance_id=instance.instance_id,
                        message_key=message.key,
                        modem_id=message.modem_id,
                        number=message.number,
                        content=message.content,
                        message_date=message.date,
                        direction=message.direction,
                        channels=(),
                    )
                store.export_readonly_snapshot(snapshot)
            with self.assertRaisesRegex(forwarder.ForwarderError, "refused"):
                forwarder.cleanup_modem_messages(instance, state_root, snapshot)
        post.assert_not_called()

    @patch("mf833u1_forwarder.fetch_sms_payload")
    @patch("mf833u1_forwarder._fetch_modem_json", return_value={})
    def test_delete_wait_accepts_exact_postcondition_when_status_is_empty(
        self, _status, fetch
    ):
        instance = self.instance()
        before = self._device_payload(2)
        first = forwarder.message_from_dict(instance, before["messages"][1])
        second = forwarder.message_from_dict(instance, before["messages"][0])
        fetch.return_value = {"messages": [before["messages"][0]]}
        self.assertEqual(
            forwarder._wait_for_device_deletion(
                instance,
                {first.key},
                {second.key},
            ),
            1,
        )

    @patch("mf833u1_forwarder.cleanup_modem_messages")
    @patch("mf833u1_forwarder.process_instance", return_value=True)
    def test_enabled_cleanup_requires_readonly_snapshot(
        self, _process, cleanup
    ):
        instance = self.instance(device_cleanup_threshold=90)
        self.assertFalse(
            forwarder.run_cycle(
                [instance],
                Path("unused-state"),
                dry_run=False,
                show_messages=False,
                inbox_snapshot=None,
            )
        )
        cleanup.assert_not_called()

    @patch("mf833u1_forwarder.post_json", return_value={"code": 200})
    def test_bark_summary_omits_sender_and_message_body(self, post):
        message = forwarder.Message(
            key="sha256:test",
            modem_id="1",
            number="sensitive-sender",
            content="sensitive-body",
            date="2026-07-27 12:00:00",
            direction="received",
        )
        forwarder.send_bark(
            self.instance(
                mode="bark",
                bark_key="test-key",
                notification_content="summary",
            ),
            message,
        )
        payload = post.call_args.args[1]
        serialized = json.dumps(payload)
        self.assertNotIn(message.number, serialized)
        self.assertNotIn(message.content, serialized)
        self.assertNotIn("copy", payload)

    @patch("mf833u1_forwarder.post_json", return_value={"code": 200})
    def test_bark_full_content_requires_explicit_configuration(self, post):
        message = forwarder.Message(
            key="sha256:test",
            modem_id="1",
            number="sender",
            content="body",
            date="2026-07-27 12:00:00",
            direction="received",
        )
        forwarder.send_bark(
            self.instance(
                mode="bark",
                bark_key="test-key",
                notification_content="full",
            ),
            message,
        )
        payload = post.call_args.args[1]
        self.assertIn(message.number, payload["body"])
        self.assertIn(message.content, payload["body"])
        self.assertEqual(payload["copy"], message.content)

    @patch("mf833u1_forwarder.post_json", return_value={})
    def test_bark_requires_explicit_success_code(self, _post):
        message = forwarder.Message(
            key="sha256:test",
            modem_id="1",
            number="sender",
            content="body",
            date="2026-07-27 12:00:00",
            direction="received",
        )
        with self.assertRaisesRegex(
            forwarder.ForwarderError,
            "Bark rejected",
        ):
            forwarder.send_bark(
                self.instance(mode="bark", bark_key="test-key"),
                message,
            )

    @patch(
        "mf833u1_forwarder.post_json",
        return_value={"id": "ntfy-id", "event": "message", "topic": "personal-sms"},
    )
    def test_ntfy_full_content_uses_bearer_header_not_url(self, post):
        message = forwarder.Message(
            key="sha256:test",
            modem_id="1",
            number="sender",
            content="body",
            date="2026-07-27 12:00:00",
            direction="received",
        )
        token = "tk_test_secret"
        forwarder.send_ntfy(
            self.instance(
                mode="ntfy",
                ntfy_server="https://ntfy.example.com",
                ntfy_topic="personal-sms",
                ntfy_token=token,
                notification_content="full",
            ),
            message,
        )
        self.assertEqual(post.call_args.args[0], "https://ntfy.example.com")
        payload = post.call_args.args[1]
        self.assertEqual(payload["topic"], "personal-sms")
        self.assertIn(message.number, payload["message"])
        self.assertIn(message.content, payload["message"])
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            f"Bearer {token}",
        )
        self.assertNotIn(token, post.call_args.args[0])

    @patch(
        "mf833u1_forwarder.post_json",
        return_value={"id": "ntfy-id", "event": "message", "topic": "personal-sms"},
    )
    def test_ntfy_summary_omits_sender_and_message_body(self, post):
        message = forwarder.Message(
            key="sha256:test",
            modem_id="1",
            number="sensitive-sender",
            content="sensitive-body",
            date="2026-07-27 12:00:00",
            direction="received",
        )
        forwarder.send_ntfy(
            self.instance(
                mode="ntfy",
                ntfy_server="https://ntfy.example.com",
                ntfy_topic="personal-sms",
                ntfy_token="test-token",
                notification_content="summary",
            ),
            message,
        )
        serialized = json.dumps(post.call_args.args[1])
        self.assertNotIn(message.number, serialized)
        self.assertNotIn(message.content, serialized)

    @patch("mf833u1_forwarder.post_json", return_value={})
    def test_ntfy_requires_explicit_message_acceptance(self, _post):
        with self.assertRaisesRegex(forwarder.ForwarderError, "ntfy rejected"):
            forwarder.send_ntfy(
                self.instance(
                    mode="ntfy",
                    ntfy_server="https://ntfy.example.com",
                    ntfy_topic="personal-sms",
                    ntfy_token="test-token",
                ),
                forwarder.Message(
                    key="sha256:test",
                    modem_id="1",
                    number="sender",
                    content="body",
                    date="2026-07-27 12:00:00",
                    direction="received",
                ),
            )

    @patch(
        "mf833u1_forwarder.post_json",
        return_value={"id": "ntfy-id", "event": "message", "topic": "wrong"},
    )
    def test_ntfy_rejects_acceptance_for_a_different_topic(self, _post):
        with self.assertRaisesRegex(forwarder.ForwarderError, "ntfy rejected"):
            forwarder.send_ntfy(
                self.instance(
                    mode="ntfy",
                    ntfy_server="https://ntfy.example.com",
                    ntfy_topic="personal-sms",
                    ntfy_token="test-token",
                ),
                forwarder.Message(
                    key="sha256:test",
                    modem_id="1",
                    number="sender",
                    content="body",
                    date="2026-07-27 12:00:00",
                    direction="received",
                ),
            )

    def test_bark_ntfy_mode_uses_both_channels(self):
        instance = self.instance(
            mode="bark_ntfy",
            bark_key="bark-key",
            ntfy_server="https://ntfy.example.com",
            ntfy_topic="personal-sms",
            ntfy_token="ntfy-token",
        )
        self.assertEqual(
            forwarder.notification_channels(instance),
            ["bark", "ntfy"],
        )

    def test_reused_modem_id_gets_a_distinct_message_key(self):
        first = forwarder.message_from_dict(
            self.instance(),
            {
                "id": "7",
                "tag": "0",
                "number": "13800000000",
                "content": "first",
                "date": "26,07,26,12,00,00,+32",
            },
        )
        second = forwarder.message_from_dict(
            self.instance(),
            {
                "id": "7",
                "tag": "0",
                "number": "13800000000",
                "content": "second",
                "date": "26,07,27,12,00,00,+32",
            },
        )
        self.assertNotEqual(first.key, second.key)

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_first_run_baselines_without_delivery(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": "7",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "hello",
                    "date": "26,07,26,12,00,00,+32",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with patch("mf833u1_forwarder.deliver") as deliver:
                ok = forwarder.process_instance(
                    self.instance(), state_root, dry_run=False, show_messages=False
                )
            self.assertTrue(ok)
            deliver.assert_not_called()
            with InboxStore(state_root / "inbox.sqlite3") as store:
                self.assertTrue(store.instance_initialized("modem-1"))
                self.assertEqual(store.message_count("modem-1"), 1)
                count = store.connection.execute(
                    "SELECT COUNT(*) AS count FROM notifications"
                ).fetchone()["count"]
                self.assertEqual(count, 0)

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_dry_run_does_not_write_state(self, fetch):
        fetch.return_value = {"messages": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            ok = forwarder.process_instance(
                self.instance(), state_root, dry_run=True, show_messages=False
            )
            self.assertTrue(ok)
            self.assertFalse((state_root / "inbox.sqlite3").exists())

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_mode_none_still_stores_new_messages(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": "9",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "new",
                    "date": "26,07,26,12,00,00,+32",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            with InboxStore(state_root / "inbox.sqlite3") as store:
                store.set_instance_initialized("modem-1")

            ok = forwarder.process_instance(
                self.instance(), state_root, dry_run=False, show_messages=False
            )

            self.assertTrue(ok)
            with InboxStore(state_root / "inbox.sqlite3") as store:
                self.assertEqual(store.message_count("modem-1"), 1)
                count = store.connection.execute(
                    "SELECT COUNT(*) AS count FROM notifications"
                ).fetchone()["count"]
                self.assertEqual(count, 0)

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_message_is_committed_before_notification_attempt(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": "10",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "new",
                    "date": "26,07,26,12,00,00,+32",
                }
            ]
        }
        instance = self.instance(mode="bark", bark_key="test-key")
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            db_path = state_root / "inbox.sqlite3"
            with InboxStore(db_path) as store:
                store.set_instance_initialized(instance.instance_id)

            def assert_committed(_instance, channel, _message):
                self.assertEqual(channel, "bark")
                with InboxStore(db_path) as store:
                    self.assertEqual(store.message_count(instance.instance_id), 1)
                    row = store.connection.execute(
                        """
                        SELECT message_seq, state FROM notifications
                        """
                    ).fetchone()
                    self.assertEqual(
                        store.notification_state(
                            int(row["message_seq"]),
                            "bark",
                        ),
                        "leased",
                    )

            with patch(
                "mf833u1_forwarder.deliver_channel",
                side_effect=assert_committed,
            ) as deliver:
                ok = forwarder.process_instance(
                    instance, state_root, dry_run=False, show_messages=False
                )

            self.assertTrue(ok)
            deliver.assert_called_once()

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_crash_after_commit_retries_without_duplicate_message(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": "11",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "new",
                    "date": "26,07,26,12,00,00,+32",
                }
            ]
        }
        instance = self.instance(mode="bark", bark_key="test-key")
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            db_path = state_root / "inbox.sqlite3"
            with InboxStore(db_path) as store:
                store.set_instance_initialized(instance.instance_id)

            with patch(
                "mf833u1_forwarder.deliver_channel",
                side_effect=SystemExit(137),
            ):
                with self.assertRaises(SystemExit):
                    forwarder.process_instance(
                        instance,
                        state_root,
                        dry_run=False,
                        show_messages=False,
                    )

            with InboxStore(db_path) as store:
                self.assertEqual(store.message_count(instance.instance_id), 1)
                row = store.connection.execute(
                    "SELECT message_seq, state FROM notifications"
                ).fetchone()
                seq = int(row["message_seq"])
                self.assertEqual(row["state"], "leased")
                store.connection.execute(
                    "UPDATE notifications SET lease_until = 0"
                )

            with patch("mf833u1_forwarder.deliver_channel") as deliver:
                ok = forwarder.process_instance(
                    instance, state_root, dry_run=False, show_messages=False
                )
            self.assertTrue(ok)
            deliver.assert_called_once()
            with InboxStore(db_path) as store:
                self.assertEqual(store.message_count(instance.instance_id), 1)
                self.assertEqual(
                    store.notification_state(seq, "bark"),
                    "accepted_upstream",
                )

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_failed_notification_stays_pending_and_retries(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": "12",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "new",
                    "date": "26,07,26,12,00,00,+32",
                }
            ]
        }
        instance = self.instance(mode="bark", bark_key="test-key")
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            db_path = state_root / "inbox.sqlite3"
            with InboxStore(db_path) as store:
                store.set_instance_initialized(instance.instance_id)

            with patch(
                "mf833u1_forwarder.deliver_channel",
                side_effect=forwarder.ForwarderError("temporary failure"),
            ):
                ok = forwarder.process_instance(
                    instance, state_root, dry_run=False, show_messages=False
                )
            self.assertFalse(ok)

            with InboxStore(db_path) as store:
                self.assertEqual(store.message_count(instance.instance_id), 1)
                row = store.connection.execute(
                    """
                    SELECT message_seq, state, attempts
                    FROM notifications
                    """
                ).fetchone()
                self.assertEqual(row["state"], "retry")
                self.assertEqual(row["attempts"], 1)
                seq = int(row["message_seq"])
                store.connection.execute(
                    "UPDATE notifications SET next_attempt_at = 0"
                )

            with patch("mf833u1_forwarder.deliver_channel") as deliver:
                ok = forwarder.process_instance(
                    instance, state_root, dry_run=False, show_messages=False
                )
            self.assertTrue(ok)
            deliver.assert_called_once()
            with InboxStore(db_path) as store:
                self.assertEqual(store.message_count(instance.instance_id), 1)
                self.assertEqual(
                    store.notification_state(seq, "bark"),
                    "accepted_upstream",
                )

    @patch("mf833u1_forwarder.fetch_sms_payload")
    def test_partial_channel_failure_does_not_repeat_accepted_channel(self, fetch):
        fetch.return_value = {
            "messages": [
                {
                    "id": "13",
                    "tag": "0",
                    "number": "13800000000",
                    "content": "new",
                    "date": "26,07,26,12,00,00,+32",
                }
            ]
        }
        instance = self.instance(
            mode="both",
            bark_key="test-key",
            telegram_bot_token="test-token",
            telegram_chat_id="test-chat",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            db_path = state_root / "inbox.sqlite3"
            with InboxStore(db_path) as store:
                store.set_instance_initialized(instance.instance_id)

            first_attempts = []

            def first_delivery(_instance, channel, _message):
                first_attempts.append(channel)
                if channel == "telegram":
                    raise forwarder.ForwarderError("telegram unavailable")

            with patch(
                "mf833u1_forwarder.deliver_channel",
                side_effect=first_delivery,
            ):
                ok = forwarder.process_instance(
                    instance, state_root, dry_run=False, show_messages=False
                )
            self.assertFalse(ok)
            self.assertEqual(first_attempts, ["bark", "telegram"])

            with InboxStore(db_path) as store:
                row = store.connection.execute(
                    "SELECT seq FROM messages"
                ).fetchone()
                seq = int(row["seq"])
                self.assertEqual(
                    store.notification_state(seq, "bark"),
                    "accepted_upstream",
                )
                self.assertEqual(
                    store.notification_state(seq, "telegram"),
                    "retry",
                )
                store.connection.execute(
                    """
                    UPDATE notifications SET next_attempt_at = 0
                    WHERE target_id = 'telegram'
                    """
                )

            second_attempts = []

            def second_delivery(_instance, channel, _message):
                second_attempts.append(channel)

            with patch(
                "mf833u1_forwarder.deliver_channel",
                side_effect=second_delivery,
            ):
                ok = forwarder.process_instance(
                    instance, state_root, dry_run=False, show_messages=False
                )
            self.assertTrue(ok)
            self.assertEqual(second_attempts, ["telegram"])

    @patch(
        "mf833u1_forwarder.fetch_sms_payload",
        side_effect=forwarder.ForwarderError("modem unavailable"),
    )
    def test_pending_notification_dispatches_when_modem_poll_fails(self, fetch):
        instance = self.instance(mode="bark", bark_key="test-key")
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            db_path = state_root / "inbox.sqlite3"
            with InboxStore(db_path) as store:
                store.set_instance_initialized(instance.instance_id)
                result = store.store_message(
                    instance_id=instance.instance_id,
                    message_key="sha256:test",
                    modem_id="14",
                    number="13800000000",
                    content="stored",
                    message_date="2026-07-26 12:00:00",
                    direction="received",
                    channels=("bark",),
                )

            with patch("mf833u1_forwarder.deliver_channel") as deliver:
                with self.assertRaisesRegex(
                    forwarder.ForwarderError,
                    "modem unavailable",
                ):
                    forwarder.process_instance(
                        instance,
                        state_root,
                        dry_run=False,
                        show_messages=False,
                    )
            deliver.assert_called_once()
            with InboxStore(db_path) as store:
                self.assertEqual(
                    store.notification_state(result.seq, "bark"),
                    "accepted_upstream",
                )


class CellularDataTests(unittest.TestCase):
    def instance(self, **overrides):
        return ForwarderTests.instance(self, **overrides)

    def test_turns_auto_dial_off_and_disconnects(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            forwarder, "_post_modem_form", return_value={"result": "success"}
        ) as post:
            acted = forwarder.enforce_cellular_data_off(
                self.instance(cellular_data="off"),
                {"dial_mode": "auto_dial", "ppp_status": "ppp_connected",
                 "roam_setting_option": "off"},
                Path(temp_dir),
            )
        self.assertTrue(acted)
        sent = [call.args[1]["goformId"] for call in post.call_args_list]
        self.assertEqual(sent, ["SET_CONNECTION_MODE", "DISCONNECT_NETWORK"])
        self.assertEqual(post.call_args_list[0].args[1]["ConnectionMode"], "manual_dial")
        self.assertEqual(post.call_args_list[0].args[1]["roam_setting_option"], "off")

    def test_does_nothing_when_already_manual_and_disconnected(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            forwarder, "_post_modem_form"
        ) as post:
            acted = forwarder.enforce_cellular_data_off(
                self.instance(cellular_data="off"),
                {"dial_mode": "manual_dial", "ppp_status": "ppp_disconnected"},
                Path(temp_dir),
            )
        self.assertFalse(acted)
        post.assert_not_called()

    def test_disconnects_only_when_mode_already_manual(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            forwarder, "_post_modem_form", return_value={"result": "success"}
        ) as post:
            forwarder.enforce_cellular_data_off(
                self.instance(cellular_data="off"),
                {"dial_mode": "manual_dial", "ppp_status": "ppp_connected"},
                Path(temp_dir),
            )
        self.assertEqual(
            [call.args[1]["goformId"] for call in post.call_args_list],
            ["DISCONNECT_NETWORK"],
        )

    def test_throttles_repeated_corrections(self):
        status = {"dial_mode": "auto_dial", "ppp_status": "ppp_connected"}
        t0 = datetime(2026, 10, 12, 12, 0, 0)
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            forwarder, "_post_modem_form", return_value={"result": "success"}
        ) as post:
            root = Path(temp_dir)
            inst = self.instance(cellular_data="off")
            self.assertTrue(forwarder.enforce_cellular_data_off(inst, status, root, now=t0))
            self.assertFalse(forwarder.enforce_cellular_data_off(
                inst, status, root, now=t0 + timedelta(seconds=60)))
            self.assertTrue(forwarder.enforce_cellular_data_off(
                inst, status, root, now=t0 + timedelta(seconds=301)))
        self.assertEqual(post.call_count, 4)

    def test_raises_when_modem_rejects(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            forwarder, "_post_modem_form", return_value={"result": "failure"}
        ):
            with self.assertRaises(forwarder.ForwarderError):
                forwarder.enforce_cellular_data_off(
                    self.instance(cellular_data="off"),
                    {"dial_mode": "auto_dial", "ppp_status": "ppp_disconnected"},
                    Path(temp_dir),
                )

    def test_config_defaults_to_keep_and_rejects_unknown_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "modem.conf"
            base = "[device]\nid=modem-1\nbase_url=http://192.168.0.1\n{extra}\n[notifications]\nmode=none\n"
            config.write_text(base.format(extra=""), encoding="utf-8")
            self.assertEqual(forwarder.load_instance(config).cellular_data, "keep")
            config.write_text(base.format(extra="cellular_data=off"), encoding="utf-8")
            self.assertEqual(forwarder.load_instance(config).cellular_data, "off")
            config.write_text(base.format(extra="cellular_data=maybe"), encoding="utf-8")
            with self.assertRaisesRegex(forwarder.ForwarderError, "cellular_data"):
                forwarder.load_instance(config)


class HealthAlertTests(unittest.TestCase):
    def instance(self, **overrides):
        return ForwarderTests.instance(
            self,
            mode="ntfy",
            ntfy_server="https://ntfy.example.com/",
            ntfy_topic="personal-sms",
            ntfy_token="token",
            **overrides,
        )

    def health_path(self, state_root):
        return state_root / "health" / "modem-1.json"

    @patch("mf833u1_forwarder.send_operational_alert")
    def test_short_failure_does_not_alert(self, alert):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            start = datetime(2026, 8, 9, 21, 44, 0)
            forwarder.record_instance_health(
                self.instance(), state_root, ok=False, error="boom", now=start
            )
            forwarder.record_instance_health(
                self.instance(),
                state_root,
                ok=False,
                error="boom",
                now=start + timedelta(minutes=29),
            )
            alert.assert_not_called()
            self.assertTrue(self.health_path(state_root).exists())

    @patch("mf833u1_forwarder.send_operational_alert")
    def test_sustained_failure_alerts_once_then_repeats_on_schedule(self, alert):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            start = datetime(2026, 8, 9, 21, 44, 0)

            def tick(minutes):
                forwarder.record_instance_health(
                    self.instance(),
                    state_root,
                    ok=False,
                    error="自动解锁保护已触发",
                    now=start + timedelta(minutes=minutes),
                )

            tick(0)
            tick(30)
            self.assertEqual(alert.call_count, 1)
            body = alert.call_args[0][2]
            self.assertIn("30 分钟", body)
            self.assertIn("自动解锁保护已触发", body)

            tick(60)
            tick(300)
            self.assertEqual(alert.call_count, 1)

            tick(390)  # 距首次告警满 6 小时
            self.assertEqual(alert.call_count, 2)

    @patch("mf833u1_forwarder.send_operational_alert")
    def test_recovery_clears_state_and_notifies_when_alerted(self, alert):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            start = datetime(2026, 8, 9, 21, 44, 0)
            forwarder.record_instance_health(
                self.instance(), state_root, ok=False, error="boom", now=start
            )
            forwarder.record_instance_health(
                self.instance(),
                state_root,
                ok=False,
                error="boom",
                now=start + timedelta(minutes=31),
            )
            self.assertEqual(alert.call_count, 1)

            forwarder.record_instance_health(
                self.instance(),
                state_root,
                ok=True,
                now=start + timedelta(minutes=45),
            )
            self.assertEqual(alert.call_count, 2)
            self.assertIn("已恢复", alert.call_args[0][1])
            self.assertIn("45 分钟", alert.call_args[0][2])
            self.assertFalse(self.health_path(state_root).exists())

    @patch("mf833u1_forwarder.send_operational_alert")
    def test_recovery_without_prior_alert_stays_quiet(self, alert):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            start = datetime(2026, 8, 9, 21, 44, 0)
            forwarder.record_instance_health(
                self.instance(), state_root, ok=False, error="blip", now=start
            )
            forwarder.record_instance_health(
                self.instance(),
                state_root,
                ok=True,
                now=start + timedelta(minutes=1),
            )
            alert.assert_not_called()
            self.assertFalse(self.health_path(state_root).exists())

    @patch("mf833u1_forwarder.send_operational_alert")
    def test_alert_failure_does_not_crash_and_retries_next_round(self, alert):
        alert.side_effect = forwarder.ForwarderError("ntfy unreachable")
        with tempfile.TemporaryDirectory() as temp_dir:
            state_root = Path(temp_dir)
            start = datetime(2026, 8, 9, 21, 44, 0)
            forwarder.record_instance_health(
                self.instance(), state_root, ok=False, error="boom", now=start
            )
            forwarder.record_instance_health(
                self.instance(),
                state_root,
                ok=False,
                error="boom",
                now=start + timedelta(minutes=31),
            )
            # 告警本身失败不能中断主循环，且不得记为已告警，否则会被永久跳过
            self.assertEqual(alert.call_count, 1)
            state = json.loads(self.health_path(state_root).read_text(encoding="utf-8"))
            self.assertNotIn("alerted_at", state)

            forwarder.record_instance_health(
                self.instance(),
                state_root,
                ok=False,
                error="boom",
                now=start + timedelta(minutes=32),
            )
            self.assertEqual(alert.call_count, 2)

    @patch("mf833u1_forwarder.post_json")
    def test_alert_payload_goes_to_ntfy_without_message_data(self, post):
        post.return_value = {"topic": "personal-sms", "event": "message", "id": "x"}
        forwarder.send_operational_alert(
            self.instance(), "[测试卡] 短信转发已中断", "转发器已连续失败 30 分钟。"
        )
        url, payload = post.call_args[0][0], post.call_args[0][1]
        self.assertEqual(url, "https://ntfy.example.com/")
        self.assertEqual(payload["topic"], "personal-sms")
        self.assertEqual(payload["priority"], 5)
        self.assertIn("已连续失败", payload["message"])
        headers = post.call_args[1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer token")

    @patch("mf833u1_forwarder.post_json")
    def test_alert_rejects_wrong_topic_acceptance(self, post):
        post.return_value = {"topic": "other", "event": "message", "id": "x"}
        with self.assertRaises(forwarder.ForwarderError):
            forwarder.send_operational_alert(self.instance(), "t", "b")


if __name__ == "__main__":
    unittest.main()
