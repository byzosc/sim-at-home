#!/usr/bin/env python3
"""Poll one or more ZTE MF833U1 devices and forward newly received SMS."""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from sms_store import (
    InboxStore,
    MigrationRecord,
    PendingNotification,
    StoreError,
    readonly_snapshot_ready_for_device_cleanup,
)


SMS_API_PATH = "/goform/goform_get_cmd_process"
SET_API_PATH = "/goform/goform_set_cmd_process"
# 连续失败多久才告警：短于此的抖动（一次超时、一次重启）不打扰用户。
HEALTH_ALERT_AFTER_SECONDS = 1800
# 故障未解除时的重复提醒间隔，避免刷屏但保证不会被忘记。
HEALTH_ALERT_REPEAT_SECONDS = 21600
# 蜂窝数据关闭的纠正动作最短间隔：棒子不接受时不至于每 10 秒狂发一次。
CELLULAR_ENFORCE_MIN_INTERVAL_SECONDS = 300
SMS_QUERY = {
    "isTest": "false",
    "cmd": "sms_data_total",
    "page": "0",
    "data_per_page": "500",
    "mem_store": "1",
    "tags": "10",
    "order_by": "order by id desc",
}
STATUS_QUERY = {
    "isTest": "false",
    "multi_data": "1",
    "cmd": (
        "modem_main_state,pin_status,pinnumber,network_type,"
        "signalbar,sms_unread_num,ppp_status,dial_mode,roam_setting_option"
    ),
}
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
PIN_RE = re.compile(r"^[0-9]{4,8}$")
VALID_MODES = {"none", "bark", "telegram", "both", "ntfy", "bark_ntfy"}


class ForwarderError(RuntimeError):
    pass


class RejectRedirectHandler(HTTPRedirectHandler):
    """Fail notification requests instead of forwarding credentials to redirects."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


@dataclass(frozen=True)
class Instance:
    path: Path
    instance_id: str
    label: str
    base_url: str
    interface: str
    expected_mac: str
    expected_usb_path: str
    expected_usb_id: str
    mode: str
    bark_server: str
    bark_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    ntfy_server: str
    ntfy_topic: str
    ntfy_token: str
    notification_content: str
    first_run: str
    include_sent: bool
    retention_days: int
    device_sms_capacity: int
    device_cleanup_threshold: int
    device_cleanup_batch: int
    # keep = 不管棒子的数据连接(旧行为);off = 每轮确认手动拨号且未连数据,不对就纠正。
    cellular_data: str = "keep"


@dataclass(frozen=True)
class Message:
    key: str
    modem_id: str
    number: str
    content: str
    date: str
    direction: str


def log(label: str, text: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [{label}] {text}", file=stream, flush=True)


def _get_required(parser: configparser.ConfigParser, section: str, key: str) -> str:
    value = parser.get(section, key, fallback="").strip()
    if not value:
        raise ForwarderError(f"{section}.{key} is required")
    return value


def load_instance(path: Path) -> Instance:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise ForwarderError(f"cannot read {path}: {exc}") from exc

    instance_id = _get_required(parser, "device", "id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", instance_id):
        raise ForwarderError(
            f"{path}: device.id must contain only letters, digits, dot, dash or underscore"
        )

    base_url = _get_required(parser, "device", "base_url").rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ForwarderError(f"{path}: device.base_url must be an HTTP(S) URL")

    mode = parser.get("notifications", "mode", fallback="none").strip().lower()
    if mode not in VALID_MODES:
        raise ForwarderError(f"{path}: notifications.mode must be one of {sorted(VALID_MODES)}")

    first_run = parser.get("runtime", "first_run", fallback="baseline").strip().lower()
    if first_run not in {"baseline", "notify"}:
        raise ForwarderError(f"{path}: runtime.first_run must be baseline or notify")
    try:
        retention_days = parser.getint("runtime", "retention_days", fallback=0)
        device_sms_capacity = parser.getint(
            "runtime", "device_sms_capacity", fallback=100
        )
        device_cleanup_threshold = parser.getint(
            "runtime", "device_cleanup_threshold", fallback=0
        )
        device_cleanup_batch = parser.getint(
            "runtime", "device_cleanup_batch", fallback=30
        )
    except ValueError as exc:
        raise ForwarderError(f"{path}: runtime numeric values must be integers") from exc
    if retention_days < 0 or retention_days > 3650:
        raise ForwarderError(
            f"{path}: runtime.retention_days must be between 0 and 3650"
        )
    if device_sms_capacity < 1 or device_sms_capacity > 10000:
        raise ForwarderError(
            f"{path}: runtime.device_sms_capacity must be between 1 and 10000"
        )
    if (
        device_cleanup_threshold < 0
        or device_cleanup_threshold > device_sms_capacity
    ):
        raise ForwarderError(
            f"{path}: runtime.device_cleanup_threshold must be 0 or no greater "
            "than device_sms_capacity"
        )
    if device_cleanup_batch < 1 or device_cleanup_batch > device_sms_capacity:
        raise ForwarderError(
            f"{path}: runtime.device_cleanup_batch must be between 1 and "
            "device_sms_capacity"
        )
    if (
        device_cleanup_threshold > 0
        and device_cleanup_batch >= device_cleanup_threshold
    ):
        raise ForwarderError(
            f"{path}: runtime.device_cleanup_batch must be smaller than the "
            "enabled cleanup threshold"
        )

    notification_content = parser.get(
        "notifications",
        "content",
        fallback="summary",
    ).strip().lower()
    if notification_content not in {"summary", "full"}:
        raise ForwarderError(
            f"{path}: notifications.content must be summary or full"
        )

    cellular_data = parser.get(
        "device", "cellular_data", fallback="keep"
    ).strip().lower()
    if cellular_data not in {"keep", "off"}:
        raise ForwarderError(f"{path}: device.cellular_data must be keep or off")

    instance = Instance(
        path=path,
        instance_id=instance_id,
        label=parser.get("device", "label", fallback=instance_id).strip() or instance_id,
        base_url=base_url,
        interface=parser.get("device", "interface", fallback="").strip(),
        expected_mac=parser.get("device", "expected_mac", fallback="").strip().lower(),
        expected_usb_path=parser.get(
            "device", "expected_usb_path", fallback=""
        ).strip(),
        expected_usb_id=parser.get(
            "device", "expected_usb_id", fallback=""
        ).strip().lower(),
        mode=mode,
        bark_server=parser.get(
            "notifications", "bark_server", fallback="https://api.day.app"
        ).strip().rstrip("/"),
        bark_key=parser.get("notifications", "bark_key", fallback="").strip(),
        telegram_bot_token=parser.get(
            "notifications", "telegram_bot_token", fallback=""
        ).strip(),
        telegram_chat_id=parser.get(
            "notifications", "telegram_chat_id", fallback=""
        ).strip(),
        ntfy_server=parser.get(
            "notifications", "ntfy_server", fallback=""
        ).strip().rstrip("/"),
        ntfy_topic=parser.get(
            "notifications", "ntfy_topic", fallback=""
        ).strip(),
        ntfy_token=parser.get(
            "notifications", "ntfy_token", fallback=""
        ).strip(),
        notification_content=notification_content,
        first_run=first_run,
        include_sent=parser.getboolean("runtime", "include_sent", fallback=False),
        retention_days=retention_days,
        device_sms_capacity=device_sms_capacity,
        device_cleanup_threshold=device_cleanup_threshold,
        device_cleanup_batch=device_cleanup_batch,
        cellular_data=cellular_data,
    )

    if instance.mode in {"bark", "both"} and not instance.bark_key:
        raise ForwarderError(f"{path}: Bark mode requires notifications.bark_key")
    if instance.mode in {"telegram", "both"} and (
        not instance.telegram_bot_token or not instance.telegram_chat_id
    ):
        raise ForwarderError(
            f"{path}: Telegram mode requires bot token and chat ID"
        )
    if instance.mode in {"ntfy", "bark_ntfy"}:
        ntfy_url = urlparse(instance.ntfy_server)
        if (
            ntfy_url.scheme != "https"
            or not ntfy_url.netloc
            or ntfy_url.username is not None
            or ntfy_url.password is not None
            or ntfy_url.path not in {"", "/"}
            or ntfy_url.query
            or ntfy_url.fragment
        ):
            raise ForwarderError(
                f"{path}: ntfy mode requires an HTTPS notifications.ntfy_server"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instance.ntfy_topic):
            raise ForwarderError(
                f"{path}: notifications.ntfy_topic has an invalid topic name"
            )
        if not instance.ntfy_token:
            raise ForwarderError(
                f"{path}: ntfy mode requires notifications.ntfy_token"
            )
    if instance.mode == "bark_ntfy" and not instance.bark_key:
        raise ForwarderError(
            f"{path}: bark_ntfy mode requires notifications.bark_key"
        )
    return instance


def discover_instances(config_dir: Path, selected: Optional[str]) -> List[Instance]:
    paths = sorted(config_dir.glob("*.conf"))
    instances = [load_instance(path) for path in paths]
    ids = [item.instance_id for item in instances]
    if len(ids) != len(set(ids)):
        raise ForwarderError("device.id values must be unique across all instance files")
    if selected:
        instances = [item for item in instances if item.instance_id == selected]
        if not instances:
            raise ForwarderError(f"instance not found: {selected}")
    if not instances:
        raise ForwarderError(f"no *.conf files found in {config_dir}")
    return instances


def build_sms_url(base_url: str) -> str:
    query = dict(SMS_QUERY)
    query["_"] = str(int(time.time() * 1000))
    return f"{base_url}{SMS_API_PATH}?{urlencode(query)}"


def build_status_url(base_url: str) -> str:
    query = dict(STATUS_QUERY)
    query["_"] = str(int(time.time() * 1000))
    return f"{base_url}{SMS_API_PATH}?{urlencode(query)}"


def _fetch_modem_json(
    instance: Instance, url: str, timeout: int = 12
) -> Dict[str, Any]:
    command = [
        "/usr/sbin/curl" if Path("/usr/sbin/curl").exists() else "curl",
        "--disable",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "5",
        "--max-time",
        str(timeout),
        "--header",
        f"Referer: {instance.base_url}/index.html",
        "--header",
        "X-Requested-With: XMLHttpRequest",
        "--header",
        "Accept: application/json, text/javascript, */*; q=0.01",
    ]
    if instance.interface:
        command.extend(["--interface", instance.interface])
    command.append(url)

    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForwarderError(f"cannot execute curl: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"curl exit {result.returncode}"
        raise ForwarderError(f"modem API request failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ForwarderError("modem API did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise ForwarderError("modem API JSON has an unexpected shape")
    return payload


def _post_modem_form(
    instance: Instance, fields: Dict[str, str], timeout: int = 12
) -> Dict[str, Any]:
    command = [
        "/usr/sbin/curl" if Path("/usr/sbin/curl").exists() else "curl",
        "--disable",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "5",
        "--max-time",
        str(timeout),
        "--header",
        f"Referer: {instance.base_url}/index.html",
        "--header",
        "X-Requested-With: XMLHttpRequest",
        "--header",
        "Content-Type: application/x-www-form-urlencoded",
        "--data-binary",
        "@-",
    ]
    if instance.interface:
        command.extend(["--interface", instance.interface])
    command.append(f"{instance.base_url}{SET_API_PATH}")

    try:
        result = subprocess.run(
            command,
            input=urlencode(fields),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForwarderError(f"cannot execute modem update request: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"curl exit {result.returncode}"
        raise ForwarderError(f"modem update request failed: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ForwarderError("modem update API did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise ForwarderError("modem update API JSON has an unexpected shape")
    return payload


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_pin_credential(path: Path, *, expected_uid: int = 0) -> str:
    if expected_uid == 0 and os.geteuid() != 0:
        raise ForwarderError("PIN auto-unlock must run as root")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise ForwarderError(f"cannot access PIN credential directory {path.parent}") from exc
    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ForwarderError(f"PIN credential parent is not a directory: {path.parent}")
        if directory_stat.st_uid != expected_uid:
            raise ForwarderError(f"PIN credential directory must be root-owned: {path.parent}")
        if stat.S_IMODE(directory_stat.st_mode) & 0o077:
            raise ForwarderError(
                f"PIN credential directory permissions must be 0700 or stricter: {path.parent}"
            )

        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ForwarderError(f"cannot access PIN credential file {path}") from exc
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise ForwarderError(
                    f"PIN credential must be a regular, non-linked file: {path}"
                )
            if file_stat.st_uid != expected_uid:
                raise ForwarderError(f"PIN credential must be root-owned: {path}")
            if stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise ForwarderError(
                    f"PIN credential permissions must be 0600 or stricter: {path}"
                )
            value_bytes = os.read(file_fd, 32)
            if os.read(file_fd, 1):
                raise ForwarderError(f"PIN credential is unexpectedly long: {path}")
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)

    if not re.fullmatch(rb"[0-9]{4,8}\n?", value_bytes):
        raise ForwarderError(f"PIN credential must contain only 4 to 8 digits: {path}")
    return value_bytes.rstrip(b"\n").decode("ascii")


def verify_modem_identity(
    instance: Instance, *, sys_class_net: Path = Path("/sys/class/net")
) -> None:
    if not instance.interface or not re.fullmatch(r"[A-Za-z0-9_.:-]+", instance.interface):
        raise ForwarderError("auto-unlock requires a safe, explicit network interface")
    if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", instance.expected_mac):
        raise ForwarderError("auto-unlock requires device.expected_mac")
    if not re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", instance.expected_usb_path):
        raise ForwarderError("auto-unlock requires device.expected_usb_path")
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", instance.expected_usb_id):
        raise ForwarderError("auto-unlock requires device.expected_usb_id")

    interface_root = sys_class_net / instance.interface
    try:
        current_mac = (interface_root / "address").read_text(encoding="ascii").strip().lower()
        device_path = (interface_root / "device").resolve(strict=True)
        driver_name = (interface_root / "device" / "driver").resolve(strict=True).name
        usb_device_path = device_path.parent
        current_usb_id = (
            f"{(usb_device_path / 'idVendor').read_text(encoding='ascii').strip().lower()}:"
            f"{(usb_device_path / 'idProduct').read_text(encoding='ascii').strip().lower()}"
        )
    except (OSError, RuntimeError) as exc:
        raise ForwarderError("cannot verify the configured modem identity") from exc

    if current_mac != instance.expected_mac:
        raise ForwarderError("modem identity mismatch: network MAC changed")
    if usb_device_path.name != instance.expected_usb_path:
        raise ForwarderError("modem identity mismatch: USB path changed")
    if current_usb_id != instance.expected_usb_id:
        raise ForwarderError("modem identity mismatch: USB vendor/product changed")
    if driver_name != "cdc_ether":
        raise ForwarderError("modem identity mismatch: expected cdc_ether driver")


def _guard_identity(instance: Instance) -> Dict[str, str]:
    return {
        "instance": instance.instance_id,
        "interface": instance.interface,
        "expected_mac": instance.expected_mac,
        "expected_usb_path": instance.expected_usb_path,
        "expected_usb_id": instance.expected_usb_id,
    }


def _load_unlock_guard(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwarderError(f"cannot read automatic-unlock protection file {path}") from exc
    if not isinstance(payload, dict):
        raise ForwarderError(f"invalid automatic-unlock protection file {path}")
    return payload


def _guard_matches_instance(payload: Dict[str, Any], instance: Instance) -> bool:
    identity = _guard_identity(instance)
    return all(payload.get(key) == value for key, value in identity.items())


def _write_unlock_attempt_guard(path: Path, instance: Instance, attempts: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {
        **_guard_identity(instance),
        "phase": "inflight",
        "attempted_at": datetime.now().isoformat(timespec="seconds"),
        "attempts_before": attempts,
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ForwarderError(
            "自动解锁保护已触发：本轮已尝试过 PIN，拒绝再次提交；"
            f"检查后手动删除 {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _update_unlock_guard_phase(path: Path, phase: str) -> Dict[str, Any]:
    payload = _load_unlock_guard(path)
    if payload is None:
        raise ForwarderError("automatic-unlock protection file disappeared unexpectedly")
    payload["phase"] = phase
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(path, payload)
    return payload


def _remove_unlock_guard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _wait_for_unlock_confirmation(
    instance: Instance,
    guard_path: Path,
    *,
    timeout: int,
) -> Dict[str, Any]:
    for _ in range(30):
        time.sleep(2)
        current = _fetch_modem_json(
            instance, build_status_url(instance.base_url), timeout=timeout
        )
        current_state = str(current.get("modem_main_state", "")).strip()
        if current_state == "modem_init_complete":
            _remove_unlock_guard(guard_path)
            log(instance.label, "SIM PIN 自动解锁成功")
            return current
        current_attempts = str(current.get("pinnumber", "")).strip()
        if current_state in {
            "modem_waitpuk",
            "modem_sim_undetected",
            "modem_sim_destroy",
        } or (current_state == "modem_waitpin" and current_attempts != "3"):
            _update_unlock_guard_phase(guard_path, "blocked")
            raise ForwarderError(
                "SIM 自动解锁后的设备状态异常；已永久锁定自动重试，需人工检查"
            )
    raise ForwarderError(
        "SIM PIN 请求已被设备接受，但 60 秒内未完成初始化；已锁定自动重试"
    )


def unlock_sim_if_needed(
    instance: Instance,
    status: Dict[str, Any],
    credentials_dir: Path,
    state_root: Path,
    *,
    timeout: int = 12,
) -> Dict[str, Any]:
    state = str(status.get("modem_main_state", "")).strip()
    guard_path = state_root / "unlock-attempts" / f"{instance.instance_id}.lock"
    guard = _load_unlock_guard(guard_path)

    if state == "modem_init_complete":
        if (
            guard is not None
            and guard.get("phase") == "accepted"
            and _guard_matches_instance(guard, instance)
        ):
            verify_modem_identity(instance)
            _remove_unlock_guard(guard_path)
        return status
    if state != "modem_waitpin":
        return status

    attempts_text = str(status.get("pinnumber", "")).strip() or "unknown"

    if guard is not None:
        if guard.get("phase") == "accepted" and _guard_matches_instance(guard, instance):
            verify_modem_identity(instance)
            return _wait_for_unlock_confirmation(
                instance, guard_path, timeout=timeout
            )
        if (
            guard.get("phase") == "inflight"
            and _guard_matches_instance(guard, instance)
            and attempts_text == "3"
        ):
            # 锁在提交 PIN 之前就落盘，之后请求的结果可能永远无从得知
            # （进程被杀、断电、或 POST 本身抛异常）。但此刻设备仍停在
            # modem_waitpin 且剩余次数满额，足以断定那次请求没有送达：
            # 送达且正确会变成 modem_init_complete，送达但错误会让
            # pinnumber 掉到 2。两者都不成立，所以清锁重试是安全的。
            log(
                instance.label,
                "自动解锁保护：上次请求未送达设备（仍等待 PIN 且剩余次数满额 3），已清锁重试",
            )
            _remove_unlock_guard(guard_path)
            guard = None
        else:
            raise ForwarderError(
                "自动解锁保护已触发：此前请求未被安全确认，拒绝再次提交；"
                f"人工检查后才能删除 {guard_path}"
            )

    if attempts_text != "3":
        raise ForwarderError(
            f"SIM PIN 剩余尝试次数为 {attempts_text}，只有满额 3 次时才允许自动解锁"
        )

    if os.geteuid() != 0:
        raise ForwarderError("PIN auto-unlock must run as root")
    verify_modem_identity(instance)
    credential_path = credentials_dir / f"{instance.instance_id}.pin"
    pin = read_pin_credential(credential_path)
    _write_unlock_attempt_guard(guard_path, instance, attempts_text)
    try:
        response = _post_modem_form(
            instance,
            {
                "isTest": "false",
                "goformId": "ENTER_PIN",
                "PinNumber": pin,
            },
            timeout=timeout,
        )
    finally:
        pin = ""

    if response.get("result") != "success":
        _update_unlock_guard_phase(guard_path, "rejected")
        raise ForwarderError(
            "SIM PIN 自动解锁请求失败；已锁定自动重试以保护剩余尝试次数"
        )

    _update_unlock_guard_phase(guard_path, "accepted")
    return _wait_for_unlock_confirmation(instance, guard_path, timeout=timeout)


def check_modem_status(instance: Instance, timeout: int = 12) -> Dict[str, Any]:
    payload = _fetch_modem_json(instance, build_status_url(instance.base_url), timeout)
    state = str(payload.get("modem_main_state", "")).strip()
    blocking_states = {
        "modem_waitpin": "SIM 卡正在等待 PIN 解锁",
        "modem_waitpuk": "SIM 卡正在等待 PUK 解锁",
        "modem_sim_undetected": "未检测到 SIM 卡",
        "modem_undetected": "未检测到蜂窝模块",
        "modem_sim_destroy": "SIM 卡状态异常",
    }
    if state in blocking_states:
        raise ForwarderError(
            f"{blocking_states[state]}（modem_main_state={state}）"
        )
    return payload


def fetch_sms_payload(instance: Instance, timeout: int = 12) -> Dict[str, Any]:
    check_modem_status(instance, timeout)
    payload = _fetch_modem_json(instance, build_sms_url(instance.base_url), timeout)
    if not isinstance(payload.get("messages", []), list):
        raise ForwarderError("modem SMS API JSON has an unexpected shape")
    return payload


def _looks_readable(value: str) -> bool:
    value = value.strip()
    if not value or "\x00" in value:
        return False
    printable = sum(character.isprintable() for character in value)
    return printable / len(value) >= 0.75


def _text_score(value: str) -> float:
    """给候选解码结果打分,越高越像真实短信文本。

    用于在 UTF-8 与 UTF-16BE 两种解法都「看起来可读」时做取舍。
    典型的误判长这样:
      - 把 UCS2 的汉字当 UTF-8 读 → 「你」(U+4F60) 变成可打印 ASCII 的 "O`";
      - 把 ASCII 十六进制当 UTF-16BE 读 → "123456" 变成 CJK 扩展区的生僻字。
    两者都能骗过 _looks_readable,所以这里按 Unicode 区块区分常见与生僻。
    """
    value = value.strip()
    if not value:
        return -1.0
    total = 0.0
    for character in value:
        code = ord(character)
        if character in " \n\r\t":
            total += 1.0
        elif 0x20 <= code < 0x7F:  # ASCII 可打印
            total += 1.0
        elif 0x4E00 <= code <= 0x9FFF:  # CJK 常用区
            total += 1.0
        elif 0x3000 <= code <= 0x301F:  # 常用 CJK 标点:。、【】「」《》等
            total += 1.0
        elif 0xFF00 <= code <= 0xFFEF:  # 全角字符
            total += 1.0
        elif 0x3400 <= code <= 0x4DBF:  # CJK 扩展 A,真实短信几乎不会出现
            total -= 1.0
        elif (
            0x3020 <= code <= 0x303F  # 〰 等生僻符号:"000000" 误读成 U+3030 就在这
            or 0x3130 <= code <= 0x318F  # 谚文兼容字母
            or 0x3300 <= code <= 0x33FF  # CJK 兼容符号
        ):
            total -= 1.0
        elif not character.isprintable():
            total -= 2.0
        else:
            total += 0.2
    return total / len(value)


def _decode_utf8(data: bytes) -> Optional[str]:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return decoded.strip() if _looks_readable(decoded) else None


def _decode_utf16be(data: bytes) -> Optional[str]:
    # 部分固件会在正文前带若干字节前缀,逐位偏移试探。
    for start in range(0, min(16, len(data))):
        candidate = data[start:]
        if len(candidate) % 2:
            continue
        try:
            decoded = candidate.decode("utf-16-be")
        except UnicodeDecodeError:
            continue
        if _looks_readable(decoded):
            return decoded.strip()
    return None


def decode_content(raw: Any) -> str:
    """把调制解调器返回的十六进制正文解成文本。

    MF833U1 的 Web API 一律以 **UCS2(UTF-16BE)** 返回短信正文,纯数字验证码
    也不例外(参见 tests 中 "2" → "0032" 的用例)。因此这里以 UTF-16BE 为主,
    UTF-8 仅作兜底。

    旧实现先试 UTF-8 再试 UTF-16BE,而 _looks_readable 只要求 75% 可打印,
    于是当某个汉字的 UTF-16BE 两字节恰好都是可打印 ASCII 时,UTF-8 分支会
    「成功」返回乱码,UTF-16BE 根本没机会执行:

        你 U+4F60 → 字节 4F 60 → 按 ASCII 读成 "O`"
        是 U+662F → 字节 66 2F → 读成 "f/"

    「你是好在吧个中」等常用字全部中招;而「我哦吗了人」因低字节是控制字符
    或破坏了 UTF-8 合法性而侥幸正确。
    对应行为由 tests/test_forwarder.py 中的解码用例覆盖。

    两种解法都可读时按 _text_score 取舍,平分归 UTF-16BE(符合上述固件行为)。
    """
    value = "" if raw is None else str(raw)
    if len(value) < 2 or len(value) % 2 or not HEX_RE.fullmatch(value):
        return value
    try:
        data = bytes.fromhex(value)
    except ValueError:
        return value

    utf16_value = _decode_utf16be(data)
    utf8_value = _decode_utf8(data)

    if utf16_value is not None and utf8_value is not None:
        if _text_score(utf16_value) >= _text_score(utf8_value):
            return utf16_value
        return utf8_value
    if utf16_value is not None:
        return utf16_value
    if utf8_value is not None:
        return utf8_value
    return value


def parse_date(value: Any) -> str:
    raw = "" if value is None else str(value)
    parts = raw.split(",")
    if len(parts) >= 6:
        try:
            year = int(parts[0])
            if year < 100:
                year += 2000
            parsed = datetime(
                year,
                int(parts[1]),
                int(parts[2]),
                int(parts[3]),
                int(parts[4]),
                int(parts[5]),
            )
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
    return raw


def build_message_key(
    instance_id: str,
    modem_id: str,
    number: str,
    content: str,
    date: str,
    direction: str,
) -> str:
    digest = hashlib.sha256()
    for value in (
        "fingerprint-v1",
        instance_id,
        modem_id,
        number,
        content,
        date,
        direction,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def message_from_dict(instance: Instance, raw: Dict[str, Any]) -> Message:
    modem_id = str(raw.get("id", "")).strip()
    number = str(raw.get("number", ""))
    content = decode_content(raw.get("content", ""))
    date = parse_date(raw.get("date", ""))
    direction = "sent" if str(raw.get("tag", "")) == "2" else "received"
    key = build_message_key(
        instance.instance_id,
        modem_id,
        number,
        content,
        date,
        direction,
    )
    # Modem message IDs may be reused after device-side deletion. Including the
    # complete stable payload prevents a reused numeric ID from hiding a new SMS.
    return Message(
        key=key,
        modem_id=modem_id,
        number=number,
        content=content,
        date=date,
        direction=direction,
    )


def concat_sms_is_complete(raw: Dict[str, Any]) -> bool:
    """Return false while the modem is still assembling a multipart SMS.

    MF833U1 exposes a message before all segments have necessarily arrived.
    Its content then changes under the same modem ID as further segments are
    appended. Because the durable message fingerprint includes the content,
    ingesting that intermediate record would create and notify both a partial
    and a final message.

    Ordinary one-part messages report total/received as zero, or omit these
    fields entirely. A positive total is treated as multipart and must have an
    explicit completion flag plus at least the advertised number of segments.
    Malformed multipart counters fail closed.
    """

    total_raw = raw.get("concat_sms_total")
    received_raw = raw.get("concat_sms_received")
    complete_flag = str(raw.get("received_all_concat_sms", "")).strip()

    if complete_flag == "0":
        return False
    if total_raw in (None, "") and received_raw in (None, ""):
        return True
    try:
        total = int(str(total_raw).strip())
        received = int(str(received_raw).strip())
    except (TypeError, ValueError):
        return False
    if total < 0 or received < 0:
        return False
    if total == 0:
        return True
    return complete_flag == "1" and received >= total


def parse_messages(instance: Instance, payload: Dict[str, Any]) -> List[Message]:
    messages: List[Message] = []
    for item in payload.get("messages", []):
        if not isinstance(item, dict):
            continue
        if not concat_sms_is_complete(item):
            continue
        message = message_from_dict(instance, item)
        if message.direction == "sent" and not instance.include_sent:
            continue
        messages.append(message)
    return messages


def _device_messages_by_id(
    instance: Instance,
    payload: Dict[str, Any],
) -> Dict[str, Message]:
    raw_messages = payload.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ForwarderError("modem SMS API JSON has an unexpected shape")
    messages: Dict[str, Message] = {}
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise ForwarderError("modem SMS API contains a malformed message")
        message = message_from_dict(instance, raw)
        if not re.fullmatch(r"[0-9]+", message.modem_id):
            raise ForwarderError("modem SMS API returned an unsafe message ID")
        if message.modem_id in messages:
            raise ForwarderError("modem SMS API returned duplicate message IDs")
        messages[message.modem_id] = message
    return messages


def _device_cleanup_sort_key(message: Message) -> tuple[datetime, int]:
    try:
        parsed = datetime.strptime(message.date, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ForwarderError(
            "cannot order modem messages safely because a date is invalid"
        ) from exc
    return parsed, int(message.modem_id)


def build_sms_command_status_url(instance: Instance, command: int) -> str:
    query = {
        "isTest": "false",
        "cmd": "sms_cmd_status_info",
        "sms_cmd": str(command),
        "_": str(int(time.time() * 1000)),
    }
    return f"{instance.base_url}{SMS_API_PATH}?{urlencode(query)}"


def _wait_for_device_deletion(
    instance: Instance,
    selected_keys: Set[str],
    retained_keys: Set[str],
    *,
    timeout_seconds: int = 30,
) -> int:
    """Wait for exact post-conditions; command status alone is not sufficient."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = _fetch_modem_json(
            instance,
            build_sms_command_status_url(instance, 6),
        )
        command_result = str(status.get("sms_cmd_status_result", "")).strip()
        if command_result == "2":
            raise ForwarderError("modem reported that SMS deletion failed")

        payload = fetch_sms_payload(instance)
        current_messages = _device_messages_by_id(instance, payload)
        current_keys = {message.key for message in current_messages.values()}
        retained_missing = retained_keys - current_keys
        if retained_missing:
            raise ForwarderError(
                "device deletion verification failed because retained messages changed"
            )
        if not (selected_keys & current_keys):
            return len(current_messages)
        time.sleep(1)
    raise ForwarderError(
        "device accepted SMS deletion but exact removal was not confirmed in time"
    )


def cleanup_modem_messages(
    instance: Instance,
    state_root: Path,
    inbox_snapshot: Path,
) -> int:
    """Delete one exact oldest batch after two-copy durability verification."""

    if instance.device_cleanup_threshold == 0:
        return 0

    payload = fetch_sms_payload(instance)
    messages_by_id = _device_messages_by_id(instance, payload)
    if len(messages_by_id) < instance.device_cleanup_threshold:
        return 0

    ordered = sorted(messages_by_id.values(), key=_device_cleanup_sort_key)
    selected = ordered[: instance.device_cleanup_batch]
    selected_keys = [message.key for message in selected]

    try:
        with InboxStore(state_root / "inbox.sqlite3") as store:
            quick_check = store.connection.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise StoreError("authoritative inbox failed SQLite quick_check")
            if not store.messages_ready_for_device_cleanup(
                instance.instance_id,
                selected_keys,
            ):
                raise StoreError(
                    "oldest device messages are not fully durable and dispatched"
                )
        if not readonly_snapshot_ready_for_device_cleanup(
            inbox_snapshot,
            instance.instance_id,
            selected_keys,
        ):
            raise StoreError(
                "oldest device messages are not present in the read-only snapshot"
            )
    except StoreError as exc:
        raise ForwarderError(f"device SMS cleanup refused: {exc}") from exc

    # Re-read immediately before the destructive request. New SMS may arrive
    # while SQLite is checked, but the exact selected IDs must still identify
    # the same payloads. Newer records are added to the retained proof set.
    current_payload = fetch_sms_payload(instance)
    current_by_id = _device_messages_by_id(instance, current_payload)
    for message in selected:
        current = current_by_id.get(message.modem_id)
        if current is None or current.key != message.key:
            raise ForwarderError(
                "device SMS cleanup refused because selected records changed"
            )
    selected_key_set = set(selected_keys)
    retained_keys = {
        message.key
        for message in current_by_id.values()
        if message.key not in selected_key_set
    }

    response = _post_modem_form(
        instance,
        {
            "isTest": "false",
            "goformId": "DELETE_SMS",
            "msg_id": ";".join(message.modem_id for message in selected) + ";",
            "notCallback": "true",
        },
    )
    if response.get("result") != "success":
        raise ForwarderError("modem rejected the SMS deletion request")

    after_count = _wait_for_device_deletion(
        instance,
        selected_key_set,
        retained_keys,
    )
    log(
        instance.label,
        f"设备短信达到 {len(current_by_id)} 条；已验证并删除最旧 "
        f"{len(selected)} 条，设备现有 {after_count} 条",
    )
    return len(selected)


def load_state(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {"initialized": False, "seen": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwarderError(f"cannot read state {path}: {exc}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("seen", []), list):
        raise ForwarderError(f"invalid state file: {path}")
    state.setdefault("initialized", False)
    return state


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def post_json(
    url: str,
    payload: Dict[str, Any],
    timeout: int = 10,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "r3s-sim-card-forwarding/1.0",
    }
    if headers:
        request_headers.update(headers)
    request = Request(
        url,
        data=data,
        headers=request_headers,
        method="POST",
    )
    try:
        opener = build_opener(RejectRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            response_data = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ForwarderError(f"notification request failed: {exc}") from exc
    if not response_data:
        return {}
    try:
        parsed = json.loads(response_data)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def send_bark(instance: Instance, message: Message) -> None:
    payload: Dict[str, Any] = {
        "device_key": instance.bark_key,
        "title": f"[{instance.label}] 新短信",
        "body": "有新短信，请打开短信收件箱查看",
        "group": f"SIM-{instance.label}",
    }
    if instance.notification_content == "full":
        payload["body"] = f"{message.number}\n{message.content}"
        payload["copy"] = message.content
    response = post_json(
        f"{instance.bark_server}/push",
        payload,
    )
    code = response.get("code")
    if code != 200:
        raise ForwarderError(f"Bark rejected the push (code {code})")


def send_telegram(instance: Instance, message: Message) -> None:
    text = f"[{instance.label}] 有新短信，请打开短信收件箱查看"
    if instance.notification_content == "full":
        text = f"[{instance.label}] {message.number}\n{message.content}"
    response = post_json(
        f"https://api.telegram.org/bot{instance.telegram_bot_token}/sendMessage",
        {
            "chat_id": instance.telegram_chat_id,
            "text": text,
        },
    )
    if response.get("ok") is not True:
        raise ForwarderError("Telegram rejected the push")


def send_ntfy(instance: Instance, message: Message) -> None:
    text = "有新短信，请打开短信收件箱查看"
    if instance.notification_content == "full":
        text = f"{message.number}\n{message.content}"
    response = post_json(
        instance.ntfy_server,
        {
            "topic": instance.ntfy_topic,
            "title": f"[{instance.label}] 新短信",
            "message": text,
            "priority": 4,
            "tags": ["incoming_envelope"],
        },
        headers={"Authorization": f"Bearer {instance.ntfy_token}"},
    )
    response_id = response.get("id")
    if (
        response.get("event") != "message"
        or not isinstance(response_id, str)
        or not response_id
        or response.get("topic") != instance.ntfy_topic
    ):
        raise ForwarderError("ntfy rejected the push")


def notification_channels(instance: Instance) -> List[str]:
    channels: List[str] = []
    if instance.mode in {"bark", "both", "bark_ntfy"}:
        channels.append("bark")
    if instance.mode in {"telegram", "both"}:
        channels.append("telegram")
    if instance.mode in {"ntfy", "bark_ntfy"}:
        channels.append("ntfy")
    return channels


def deliver_channel(instance: Instance, channel: str, message: Message) -> None:
    if channel == "bark":
        send_bark(instance, message)
        return
    if channel == "telegram":
        send_telegram(instance, message)
        return
    if channel == "ntfy":
        send_ntfy(instance, message)
        return
    raise ForwarderError(f"unsupported notification channel: {channel}")


def deliver(instance: Instance, message: Message) -> None:
    for channel in notification_channels(instance):
        deliver_channel(instance, channel, message)


def send_operational_alert(instance: Instance, title: str, body: str) -> None:
    """推送转发器自身的故障告警。

    走的是 ntfy / Bark 这类 IP 通道，与 SIM 卡状态无关，因此 SIM 卡挂了、
    模组失联、PIN 锁死时这条告警照样送得出去——这正是它存在的意义。
    正文只描述故障，绝不包含短信内容或号码。
    """
    channels = notification_channels(instance)
    for channel in channels:
        if channel == "ntfy":
            response = post_json(
                instance.ntfy_server,
                {
                    "topic": instance.ntfy_topic,
                    "title": title,
                    "message": body,
                    "priority": 5,
                    "tags": ["rotating_light"],
                },
                headers={"Authorization": f"Bearer {instance.ntfy_token}"},
            )
            if response.get("topic") != instance.ntfy_topic:
                raise ForwarderError("ntfy rejected the operational alert")
            return
        if channel == "bark":
            response = post_json(
                f"{instance.bark_server}/push",
                {
                    "device_key": instance.bark_key,
                    "title": title,
                    "body": body,
                    "group": f"SIM-{instance.label}",
                    "level": "timeSensitive",
                },
            )
            if response.get("code") != 200:
                raise ForwarderError("Bark rejected the operational alert")
            return
    raise ForwarderError("no IP-based channel available for operational alerts")


def _health_state_path(state_root: Path, instance_id: str) -> Path:
    return state_root / "health" / f"{instance_id}.json"


def _load_health_state(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def record_instance_health(
    instance: Instance,
    state_root: Path,
    *,
    ok: bool,
    error: str = "",
    now: Optional[datetime] = None,
    alert_after: int = HEALTH_ALERT_AFTER_SECONDS,
    alert_repeat: int = HEALTH_ALERT_REPEAT_SECONDS,
) -> None:
    """跟踪单个实例的连续失败，并在超过阈值时主动告警。

    持续故障需要独立于 syslog 的通知提醒，
    以便及时发现短信转发中断。
    日志不是告警，这个函数负责把故障推到人的手机上。
    """
    path = _health_state_path(state_root, instance.instance_id)
    state = _load_health_state(path)
    moment = now or datetime.now()

    if ok:
        if state is not None:
            if state.get("alerted_at"):
                failing_since = _parse_iso(state.get("failing_since"))
                outage = ""
                if failing_since is not None:
                    minutes = int((moment - failing_since).total_seconds() // 60)
                    outage = f"，中断约 {minutes} 分钟"
                try:
                    send_operational_alert(
                        instance,
                        f"[{instance.label}] 短信转发已恢复",
                        f"转发器重新正常工作{outage}。",
                    )
                except ForwarderError as exc:
                    log(instance.label, f"恢复告警发送失败: {exc}", error=True)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return

    if state is None:
        state = {"failing_since": moment.isoformat(timespec="seconds")}
    state["last_error"] = error
    state["last_failure_at"] = moment.isoformat(timespec="seconds")

    failing_since = _parse_iso(state.get("failing_since")) or moment
    failing_seconds = (moment - failing_since).total_seconds()
    last_alert = _parse_iso(state.get("alerted_at"))
    due = failing_seconds >= alert_after and (
        last_alert is None or (moment - last_alert).total_seconds() >= alert_repeat
    )

    if due:
        minutes = int(failing_seconds // 60)
        try:
            send_operational_alert(
                instance,
                f"[{instance.label}] 短信转发已中断",
                f"转发器已连续失败 {minutes} 分钟，期间的短信可能收不到。\n"
                f"最后一次错误：{error or '未知'}",
            )
        except ForwarderError as exc:
            log(instance.label, f"故障告警发送失败: {exc}", error=True)
        else:
            state["alerted_at"] = moment.isoformat(timespec="seconds")
            state["alert_count"] = int(state.get("alert_count", 0)) + 1
            log(instance.label, f"已推送故障告警（连续失败 {minutes} 分钟）")

    try:
        atomic_write_json(path, state)
    except OSError as exc:
        log(instance.label, f"健康状态写入失败: {exc}", error=True)


def message_from_pending(pending: PendingNotification) -> Message:
    return Message(
        key=pending.message_key,
        modem_id=pending.modem_id,
        number=pending.number,
        content=pending.content,
        date=pending.message_date,
        direction=pending.direction,
    )


def dispatch_pending_notifications(
    instance: Instance,
    store: InboxStore,
) -> tuple[int, int]:
    accepted_count = 0
    retry_count = 0
    for pending in store.lease_notifications(
        instance.instance_id,
        notification_channels(instance),
    ):
        try:
            deliver_channel(
                instance,
                pending.channel,
                message_from_pending(pending),
            )
        except ForwarderError as exc:
            retry_count += 1
            store.mark_notification_retry(pending, str(exc))
            log(instance.label, str(exc), error=True)
        else:
            store.mark_notification_accepted(
                pending.seq,
                pending.target_id,
                pending.lease_token,
            )
            accepted_count += 1
    return accepted_count, retry_count


def append_message(path: Path, message: Message) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["key", "modem_id", "number", "content", "date", "direction"],
        )
        if new_file:
            writer.writeheader()
        writer.writerow(
            {
                "key": message.key,
                "modem_id": message.modem_id,
                "number": message.number,
                "content": message.content,
                "date": message.date,
                "direction": message.direction,
            }
        )
    os.chmod(path, 0o600)


def _read_legacy_file(path: Path) -> tuple[Optional[bytes], str]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        raise ForwarderError(f"cannot read legacy file {path}: {exc}") from exc
    return content, hashlib.sha256(content).hexdigest()


def legacy_key_for_message(message: Message) -> str:
    if message.modem_id:
        return f"id:{message.modem_id}"
    stable_material = "\0".join(
        [
            message.modem_id,
            message.number,
            message.content,
            message.date,
            message.direction,
        ]
    )
    digest = hashlib.sha256(stable_material.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _legacy_placeholder_key(instance_id: str, legacy_key: str) -> str:
    digest = hashlib.sha256()
    for value in ("legacy-seen-v1", instance_id, legacy_key):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"legacy:{digest.hexdigest()}"


def load_legacy_state(path: Path) -> tuple[bool, Set[str], str]:
    content, digest = _read_legacy_file(path)
    if content is None:
        state: Dict[str, Any] = {"initialized": False, "seen": []}
    else:
        try:
            state = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ForwarderError(f"cannot read state {path}: {exc}") from exc
        if not isinstance(state, dict) or not isinstance(
            state.get("seen", []),
            list,
        ):
            raise ForwarderError(f"invalid state file: {path}")
    initialized = state.get("initialized", False)
    if not isinstance(initialized, bool):
        raise ForwarderError(
            f"legacy initialized flag must be boolean: {path}"
        )
    seen_values = state.get("seen", [])
    if any(not isinstance(value, str) or not value for value in seen_values):
        raise ForwarderError(f"invalid seen key in legacy state file: {path}")
    return initialized, set(seen_values), digest


def load_legacy_csv(
    instance: Instance,
    path: Path,
) -> tuple[List[MigrationRecord], str]:
    content, digest = _read_legacy_file(path)
    if content is None:
        return [], digest
    required = {
        "key",
        "modem_id",
        "number",
        "content",
        "date",
        "direction",
    }
    records: List[MigrationRecord] = []
    seen_legacy_keys: Set[str] = set()
    seen_message_keys: Set[str] = set()
    try:
        with io.StringIO(content.decode("utf-8"), newline="") as handle:
            reader = csv.DictReader(handle)
            if (
                reader.fieldnames is None
                or len(reader.fieldnames) != len(set(reader.fieldnames))
                or set(reader.fieldnames) != required
            ):
                raise ForwarderError(
                    f"legacy CSV columns do not exactly match the expected schema: {path}"
                )
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise ForwarderError(
                        f"legacy CSV has extra fields at line {line_number}: {path}"
                    )
                direction = str(row.get("direction", "")).strip()
                if direction not in {"received", "sent"}:
                    raise ForwarderError(
                        f"legacy CSV has invalid direction at line {line_number}: {path}"
                    )
                modem_id = str(row.get("modem_id", "")).strip()
                number = str(row.get("number", ""))
                content = str(row.get("content", ""))
                message_date = str(row.get("date", ""))
                legacy_key = str(row.get("key", "")).strip()
                if not legacy_key:
                    raise ForwarderError(
                        f"legacy CSV has an empty key at line {line_number}: {path}"
                    )
                message_key = build_message_key(
                    instance.instance_id,
                    modem_id,
                    number,
                    content,
                    message_date,
                    direction,
                )
                if legacy_key in seen_legacy_keys:
                    raise ForwarderError(
                        f"legacy CSV repeats key {legacy_key!r} "
                        f"at line {line_number}: {path}"
                    )
                if message_key in seen_message_keys:
                    raise ForwarderError(
                        f"legacy CSV repeats a message payload "
                        f"at line {line_number}: {path}"
                    )
                seen_legacy_keys.add(legacy_key)
                seen_message_keys.add(message_key)
                records.append(
                    MigrationRecord(
                        message_key=message_key,
                        legacy_key=legacy_key,
                        modem_id=modem_id,
                        number=number,
                        content=content,
                        message_date=message_date,
                        direction=direction,
                        baseline=True,
                        source="legacy_csv",
                        payload_available=True,
                    )
                )
    except UnicodeError as exc:
        raise ForwarderError(f"legacy CSV is not valid UTF-8: {path}") from exc
    except csv.Error as exc:
        raise ForwarderError(f"cannot parse legacy CSV {path}: {exc}") from exc
    return records, digest


def build_legacy_migration_records(
    instance: Instance,
    current_messages: Sequence[Message],
    csv_records: Sequence[MigrationRecord],
    seen: Set[str],
    state_initialized: bool,
) -> List[MigrationRecord]:
    records = list(csv_records)
    covered_fingerprints = {record.message_key for record in csv_records}
    covered_legacy_keys = {record.legacy_key for record in csv_records}
    matched_seen = seen.intersection(covered_legacy_keys)
    channels = tuple(notification_channels(instance))

    for message in reversed(current_messages):
        legacy_key = legacy_key_for_message(message)
        if message.key in covered_fingerprints:
            matched_seen.add(legacy_key)
            continue
        if legacy_key in seen:
            baseline = True
            source = "legacy_seen"
            matched_seen.add(legacy_key)
        elif not state_initialized and instance.first_run == "baseline":
            baseline = True
            source = "baseline"
        else:
            baseline = False
            source = "modem"
        records.append(
            MigrationRecord(
                message_key=message.key,
                legacy_key=legacy_key,
                modem_id=message.modem_id,
                number=message.number,
                content=message.content,
                message_date=message.date,
                direction=message.direction,
                baseline=baseline,
                source=source,
                payload_available=True,
                channels=() if baseline else channels,
            )
        )

    for legacy_key in sorted(seen - matched_seen):
        records.append(
            MigrationRecord(
                message_key=_legacy_placeholder_key(
                    instance.instance_id,
                    legacy_key,
                ),
                legacy_key=legacy_key,
                modem_id=(
                    legacy_key.removeprefix("id:")
                    if legacy_key.startswith("id:")
                    else ""
                ),
                number="",
                content="",
                message_date="",
                direction="received",
                baseline=True,
                source="legacy_seen",
                payload_available=False,
            )
        )
    return records


def migrate_instance(instance: Instance, state_root: Path) -> bool:
    payload = fetch_sms_payload(instance)
    current_messages = parse_messages(instance, payload)
    page_count = len(payload.get("messages", []))
    page_limit = int(SMS_QUERY["data_per_page"])
    if page_count >= page_limit:
        raise ForwarderError(
            f"短信查询页已达到 {page_limit} 条上限；"
            "不能证明更早短信已完整读取，拒绝写入迁移 marker"
        )
    state_path = state_root / "state" / f"{instance.instance_id}.json"
    csv_path = state_root / "messages" / f"{instance.instance_id}.csv"
    state_initialized, seen, state_digest = load_legacy_state(state_path)
    csv_records, csv_digest = load_legacy_csv(instance, csv_path)
    records = build_legacy_migration_records(
        instance,
        current_messages,
        csv_records,
        seen,
        state_initialized,
    )
    try:
        with InboxStore(state_root / "inbox.sqlite3") as store:
            store.set_instance_label(instance.instance_id, instance.label)
            result = store.migrate_legacy(
                instance_id=instance.instance_id,
                records=records,
                state_sha256=state_digest,
                csv_sha256=csv_digest,
            )
            page_alert = store.record_modem_page_status(
                instance.instance_id,
                page_count,
                page_limit,
            )
            quick_check = store.connection.execute("PRAGMA quick_check").fetchone()[0]
            if quick_check != "ok":
                raise StoreError(f"SQLite quick_check failed: {quick_check}")
    except StoreError as exc:
        raise ForwarderError(f"legacy migration failed: {exc}") from exc

    if result.already_complete:
        log(instance.label, "旧状态迁移已完成且文件哈希一致；没有重复导入")
    else:
        log(
            instance.label,
            f"旧状态迁移完成；入库 {result.inserted_messages} 条，"
            f"待通知 {result.inserted_notifications} 个目标",
        )
    if page_alert:
        log(
            instance.label,
            f"短信查询页已达到 {page_limit} 条上限；"
            "可能存在未读取的更早短信，暂不自动删除设备副本",
            error=True,
        )
    return True


def enforce_cellular_data_off(
    instance: Instance,
    status: Dict[str, Any],
    state_root: Path,
    *,
    now: Optional[datetime] = None,
    min_interval: int = CELLULAR_ENFORCE_MIN_INTERVAL_SECONDS,
) -> bool:
    """让棒子保持「手动拨号、不连数据」。返回是否执行了纠正动作。

    不依赖棒子自己记住设置：断电、拔插或固件恢复默认后，下一轮就会被改回来。
    纠正动作和检测对象是同一根棒子的同一个设置，没有升级链、不碰路由器其他配置。
    """
    dial_mode = str(status.get("dial_mode", "")).strip()
    ppp_status = str(status.get("ppp_status", "")).strip()
    needs_manual = bool(dial_mode) and dial_mode != "manual_dial"
    needs_disconnect = ppp_status in {"ppp_connected", "ppp_connecting"}
    if not needs_manual and not needs_disconnect:
        return False

    current = now or datetime.now()
    marker = state_root / "cellular" / f"{instance.instance_id}.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(str(previous.get("last_action_at", "")))
        if (current - last).total_seconds() < min_interval:
            return False
    except (OSError, ValueError, TypeError):
        pass
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"last_action_at": current.isoformat(timespec="seconds"),
                    "dial_mode": dial_mode, "ppp_status": ppp_status}),
        encoding="utf-8",
    )

    if needs_manual:
        response = _post_modem_form(
            instance,
            {
                "isTest": "false",
                "goformId": "SET_CONNECTION_MODE",
                "ConnectionMode": "manual_dial",
                "roam_setting_option": str(
                    status.get("roam_setting_option", "")
                ).strip() or "off",
            },
        )
        if response.get("result") != "success":
            raise ForwarderError("棒子拒绝把拨号模式改为手动")
    if needs_disconnect:
        response = _post_modem_form(
            instance, {"isTest": "false", "goformId": "DISCONNECT_NETWORK"}
        )
        if response.get("result") != "success":
            raise ForwarderError("棒子拒绝断开数据连接")
    log(
        instance.label,
        f"蜂窝数据已纠正为关闭：拨号模式 {dial_mode or '未知'} → manual_dial，"
        f"数据连接 {ppp_status or '未知'}" + (" → 已断开" if needs_disconnect else ""),
    )
    return True


def process_instance(
    instance: Instance,
    state_root: Path,
    *,
    dry_run: bool,
    show_messages: bool,
    auto_unlock: bool = False,
    credentials_dir: Path = Path("/etc/sim-card-forwarding/credentials.d"),
) -> bool:
    if dry_run:
        payload = fetch_sms_payload(instance)
        messages = parse_messages(instance, payload)
        log(instance.label, f"API 正常；读取到 {len(messages)} 条符合条件的短信（只读检查）")
        if len(payload.get("messages", [])) >= instance.device_sms_capacity:
            log(
                instance.label,
                f"设备短信存储已达到 {instance.device_sms_capacity} 条容量上限",
                error=True,
            )
        if show_messages:
            for message in messages:
                log(instance.label, f"{message.date} {message.number}: {message.content}")
        return True

    db_path = state_root / "inbox.sqlite3"
    try:
        with InboxStore(db_path) as store:
            store.set_instance_label(instance.instance_id, instance.label)
            legacy_state_path = (
                state_root / "state" / f"{instance.instance_id}.json"
            )
            legacy_csv_path = (
                state_root / "messages" / f"{instance.instance_id}.csv"
            )
            if (
                legacy_state_path.exists() or legacy_csv_path.exists()
            ) and not store.legacy_migration_complete(instance.instance_id):
                raise ForwarderError(
                    "检测到旧 CSV/seen 状态但尚未迁移；"
                    "请先停止服务并运行 --migrate-only"
                )
            accepted_count, failed_count = dispatch_pending_notifications(
                instance,
                store,
            )

            cellular_error = ""
            if auto_unlock or instance.cellular_data == "off":
                status = _fetch_modem_json(
                    instance, build_status_url(instance.base_url), timeout=12
                )
                if auto_unlock:
                    unlocked = unlock_sim_if_needed(
                        instance,
                        status,
                        credentials_dir,
                        state_root,
                    )
                    if isinstance(unlocked, dict):
                        status = unlocked
                if instance.cellular_data == "off":
                    try:
                        enforce_cellular_data_off(instance, status, state_root)
                    except ForwarderError as exc:
                        cellular_error = str(exc)
                        log(instance.label, f"关闭蜂窝数据失败：{exc}", error=True)
            payload = fetch_sms_payload(instance)
            messages = parse_messages(instance, payload)
            page_count = len(payload.get("messages", []))
            page_limit = instance.device_sms_capacity
            if store.record_modem_page_status(
                instance.instance_id,
                page_count,
                page_limit,
            ):
                log(
                    instance.label,
                    f"设备短信存储已达到 {page_limit} 条容量上限",
                    error=True,
                )

            if not store.instance_initialized(instance.instance_id):
                if instance.first_run == "baseline":
                    for message in reversed(messages):
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
                    store.set_instance_initialized(instance.instance_id)
                    store.run_daily_maintenance(
                        instance.instance_id,
                        instance.retention_days,
                    )
                    log(
                        instance.label,
                        f"首次基线完成；入库 {len(messages)} 条已有短信，不推送",
                    )
                    return failed_count == 0
                store.set_instance_initialized(instance.instance_id)

            channels = notification_channels(instance)
            inserted_messages: List[Message] = []
            for message in reversed(messages):
                result = store.store_message(
                    instance_id=instance.instance_id,
                    message_key=message.key,
                    modem_id=message.modem_id,
                    number=message.number,
                    content=message.content,
                    message_date=message.date,
                    direction=message.direction,
                    channels=channels,
                )
                if result.inserted:
                    inserted_messages.append(message)
                    if show_messages:
                        log(
                            instance.label,
                            f"{message.date} {message.number}: {message.content}",
                        )

            newly_accepted, newly_failed = dispatch_pending_notifications(
                instance,
                store,
            )
            accepted_count += newly_accepted
            failed_count += newly_failed

            deleted_count = store.run_daily_maintenance(
                instance.instance_id,
                instance.retention_days,
            )
    except StoreError as exc:
        raise ForwarderError(f"inbox database error: {exc}") from exc

    if inserted_messages or accepted_count or failed_count:
        log(
            instance.label,
            f"本轮新入库 {len(inserted_messages)} 条；"
            f"上游接受 {accepted_count}，待重试 {failed_count}",
            error=failed_count > 0,
        )
    if deleted_count:
        log(instance.label, f"按保留策略清理 {deleted_count} 条历史短信")
    if cellular_error:
        raise ForwarderError(f"关闭蜂窝数据失败：{cellular_error}")
    return failed_count == 0


def run_cycle(
    instances: Iterable[Instance],
    state_root: Path,
    *,
    dry_run: bool,
    show_messages: bool,
    auto_unlock: bool = False,
    credentials_dir: Path = Path("/etc/sim-card-forwarding/credentials.d"),
    inbox_snapshot: Optional[Path] = None,
) -> bool:
    successful = True
    instance_list = list(instances)
    cleanup_candidates: List[Instance] = []
    for instance in instance_list:
        instance_ok = False
        instance_error = ""
        try:
            instance_ok = process_instance(
                instance,
                state_root,
                dry_run=dry_run,
                show_messages=show_messages,
                auto_unlock=auto_unlock,
                credentials_dir=credentials_dir,
            )
            successful = successful and instance_ok
            if instance_ok:
                cleanup_candidates.append(instance)
            else:
                instance_error = "本轮处理未成功"
        except ForwarderError as exc:
            successful = False
            instance_error = str(exc)
            log(instance.label, instance_error, error=True)
        if not dry_run:
            record_instance_health(
                instance,
                state_root,
                ok=instance_ok,
                error=instance_error,
            )
    snapshot_ready = False
    if not dry_run and inbox_snapshot is not None:
        try:
            with InboxStore(state_root / "inbox.sqlite3") as store:
                if store.export_readonly_snapshot(inbox_snapshot):
                    log("inbox", "只读短信快照已更新")
            snapshot_ready = True
        except StoreError as exc:
            successful = False
            log("inbox", f"只读短信快照更新失败: {exc}", error=True)
    elif not dry_run and any(
        instance.device_cleanup_threshold > 0 for instance in instance_list
    ):
        successful = False
        log(
            "inbox",
            "设备短信自动清理已启用，但没有配置只读短信快照；拒绝删除",
            error=True,
        )

    if snapshot_ready and inbox_snapshot is not None:
        for instance in cleanup_candidates:
            if instance.device_cleanup_threshold == 0:
                continue
            try:
                cleanup_modem_messages(
                    instance,
                    state_root,
                    inbox_snapshot,
                )
            except ForwarderError as exc:
                successful = False
                log(instance.label, str(exc), error=True)
    return successful


def run_migrations(
    instances: Iterable[Instance],
    state_root: Path,
    inbox_snapshot: Optional[Path] = None,
) -> bool:
    successful = True
    for instance in instances:
        try:
            successful = migrate_instance(instance, state_root) and successful
        except ForwarderError as exc:
            successful = False
            log(instance.label, str(exc), error=True)
    if inbox_snapshot is not None and successful:
        try:
            with InboxStore(state_root / "inbox.sqlite3") as store:
                if store.export_readonly_snapshot(inbox_snapshot):
                    log("inbox", "迁移后的只读短信快照已更新")
        except StoreError as exc:
            successful = False
            log("inbox", f"迁移后的只读短信快照更新失败: {exc}", error=True)
    return successful


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("/etc/sim-card-forwarding/instances.d"),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/overlay/sim-card-forwarding/state-data"),
    )
    parser.add_argument(
        "--credentials-dir",
        type=Path,
        default=Path("/etc/sim-card-forwarding/credentials.d"),
    )
    parser.add_argument(
        "--inbox-snapshot",
        type=Path,
        help="原子导出的低权限只读 SQLite 副本；目录必须预先安全创建",
    )
    parser.add_argument(
        "--auto-unlock",
        action="store_true",
        help="仅在设备等待 PIN 时，从 root-only 凭据文件自动解锁",
    )
    parser.add_argument("--instance", help="只运行指定 device.id")
    parser.add_argument("--once", action="store_true", help="只轮询一次")
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="只读拉取调制解调器快照并把旧 CSV/seen 状态迁移到 SQLite；不发送通知",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="循环轮询间隔秒数，默认 30",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读检查 API，不写状态、不发送推送",
    )
    parser.add_argument(
        "--show-messages",
        action="store_true",
        help="在控制台显示短信正文（默认隐藏）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.interval < 10:
        print("--interval 不能小于 10 秒", file=sys.stderr)
        return 2
    if args.migrate_only and (
        args.dry_run or args.auto_unlock or args.inbox_snapshot is not None
    ):
        print(
            "--migrate-only 不能与 --dry-run、--auto-unlock "
            "或 --inbox-snapshot 同时使用",
            file=sys.stderr,
        )
        return 2
    try:
        instances = discover_instances(args.config_dir, args.instance)
    except ForwarderError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.migrate_only:
        return (
            0
            if run_migrations(
                instances,
                args.state_dir,
                inbox_snapshot=args.inbox_snapshot,
            )
            else 1
        )

    while True:
        ok = run_cycle(
            instances,
            args.state_dir,
            dry_run=args.dry_run,
            show_messages=args.show_messages,
            auto_unlock=args.auto_unlock,
            credentials_dir=args.credentials_dir,
            inbox_snapshot=args.inbox_snapshot,
        )
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
