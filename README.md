<div align="center">

# sim-at-home

**English** · [中文](README.zh-CN.md)

**Leave the physical SIM you use for verification codes in a 4G USB modem on your home router, and forward its messages to your phone.**

Python standard library + curl · OpenWrt · SQLite · [MIT](LICENSE)

</div>

## Personal use

**One SIM registered in your own name, installed in your own home, forwarding only to you. Do not receive or forward other people's messages.**

## Why this exists

A spare SIM used for verification codes does not need to travel with your phone. A home router can read it and send notifications while keeping a local message history.

Push notifications can be missed. This project writes each message to local SQLite **before** attempting notification delivery. The database is the authoritative copy; a notification is a reminder to check it.

## What you get

- A durable SQLite inbox and a separate notification outbox, with independent retries for each message and notification target.
- A first-run baseline: existing messages are saved without sending old notifications.
- ntfy notifications, preferably through your own server, or Bark.
- Optional PIN auto-unlock using a root-only credential file and a persistent attempt guard.
- Optional device cleanup: at 90 messages, remove the oldest 30 only after verifying the database, its read-only snapshot, and notification state.
- An optional read-only inbox page and API, listening on `127.0.0.1` by default.
- No SMS body in normal service logs. Showing messages requires the explicit `--show-messages` option.

## Tested hardware

| Component | Tested setup |
| --- | --- |
| Router | FriendlyElec NanoPi R3S |
| OS | iStoreOS, based on OpenWrt |
| USB modem | ZTE MF833U1, USB ID `19d2:1706` |
| Network driver | `cdc_ether` |
| Modem management address | `192.168.0.1` (factory default) |

Only one MF833U1 has been tested. Other routers, modems, and firmware combinations have not been validated.

## How it works

```text
Your SIM → MF833U1 → Python poller → SQLite inbox + notification outbox
                                         ├─→ ntfy / Bark → your phone
                                         └─→ read-only SQLite snapshot
                                                   └─→ optional inbox page
```

The forwarder retries pending notifications even when the modem is temporarily unavailable. An `accepted_upstream` state means the notification service accepted the request; it does not prove the phone displayed it. Delivery is at least once: if the process exits after the service accepts a message but before recording success, a reminder can be repeated.

## Installation

Run the following on the router as root, from a copy of this repository. The installed service and configuration paths retain the name `sim-card-forwarding`.

### 1. Install dependencies

The application uses Python's standard library and curl, with no pip dependencies. OpenWrt splits the standard library into packages:

```sh
opkg update
opkg install python3-light python3-urllib python3-openssl python3-codecs python3-sqlite3 curl
```

The router firmware also needs the `cdc_ether` driver for the modem.

### 2. Create the dedicated modem interface

Identify the modem's actual network device; `eth2` is the tested example. The helper checks for `cdc_ether`, backs up `/etc/config/network`, and creates `mf833u1_1` without starting it:

```sh
sh openwrt/restore-interface.sh eth2
```

Before starting it, verify these settings, including when the interface already existed (the helper leaves existing interfaces unchanged):

```text
network.mf833u1_1.proto=dhcp
network.mf833u1_1.device=eth2
network.mf833u1_1.defaultroute=0
network.mf833u1_1.peerdns=0
network.mf833u1_1.delegate=0
```

**Keep `defaultroute=0`, `peerdns=0`, and `delegate=0`.** Otherwise the modem may take over the router's default route or DNS. Keep this interface separate from the home LAN; do not bridge it into LAN or expose the modem management page.

```sh
ifup mf833u1_1
```

Check that your normal default route is still in use.

### 3. Install and configure

```sh
sh openwrt/install.sh
cp /etc/sim-card-forwarding/instances.d/modem-1.conf.example \
   /etc/sim-card-forwarding/instances.d/modem-1.conf
chmod 600 /etc/sim-card-forwarding/instances.d/modem-1.conf
```

The installer does not start or enable either service. Edit `modem-1.conf` on the router:

- Keep `id=modem-1` and choose your own `label`.
- Set `interface`, `expected_mac`, and `expected_usb_path` to the actual modem values. The all-zero MAC is a placeholder; replace it before using automatic PIN unlock. Keep `expected_usb_id=19d2:1706` for the tested modem.
- `cellular_data=off` enforces manual dialing with cellular data disconnected. This is useful for a SIM used only for SMS; `keep` leaves the data connection unchanged.
- Choose a notification mode below. The example starts with `mode=none` and `content=summary`.
- Keep `first_run=baseline` to save existing messages without notifying them. `retention_days=0` keeps the local history until you explicitly choose otherwise.

### 4. Choose notifications

For **ntfy**, configure your own HTTPS server, a private topic, and a publisher token. Give your phone read access to the same topic. The server URL must have no account credentials, subpath, query, or fragment.

```ini
[notifications]
mode=ntfy
content=full
ntfy_server=https://ntfy.example.com
ntfy_topic=personal-sms
ntfy_token=REPLACE_WITH_YOUR_PUBLISHER_TOKEN
```

For **Bark**, use your own device key and the appropriate server:

```ini
[notifications]
mode=bark
content=full
bark_server=https://api.day.app
bark_key=REPLACE_WITH_YOUR_BARK_KEY
```

These snippets replace settings in the existing `[notifications]` section; do not append a second section. `summary` sends only a new-message reminder; `full` includes the sender and body, so the notification server and phone receive that content. Keep credentials in the root-owned `0600` configuration, never in a repository or command-line argument.

### 5. Configure PIN unlock if needed

For a PIN-protected SIM, store the PIN interactively with hidden input:

```sh
sh openwrt/set-pin-credential.sh modem-1
```

It writes `/etc/sim-card-forwarding/credentials.d/modem-1.pin` with mode `0600` inside a root-only `0700` directory. The service enables `--auto-unlock` and checks the interface, MAC, USB path, and USB ID before submitting a PIN.

A rejected PIN blocks automatic retries. The guard also blocks an uncertain result unless the modem still reports all three attempts remaining and the configured identity matches. Inspect a failure manually; do not repeatedly clear the guard to retry a guessed PIN.

### 6. Check and start

First perform a read-only check, which neither saves messages nor sends notifications or unlocks the SIM:

```sh
python3 /usr/libexec/sim-card-forwarding/mf833u1_forwarder.py --once --dry-run
```

Then start the service:

```sh
/etc/init.d/sim-card-forwarding enable
/etc/init.d/sim-card-forwarding start
logread -e sim-card-forwarding
```

The service polls every 10 seconds and exports a read-only snapshot. Its authoritative database is `/overlay/sim-card-forwarding/state-data/inbox.sqlite3`, kept in persistent storage with mode `0600`. The first successful run establishes the baseline; send a new test message afterward to check delivery on your phone.

## Keeping space on the modem

The tested MF833U1 holds 100 messages. When full, it cannot receive new ones. Device cleanup is **disabled in the example configuration** (`device_cleanup_threshold=0`). To enable the tested policy, change the existing `[runtime]` settings and restart the forwarder:

```ini
[runtime]
device_sms_capacity=100
device_cleanup_threshold=90
device_cleanup_batch=30
```

At 90 messages, the forwarder selects exactly the oldest 30. It checks SQLite integrity, complete payloads in the authoritative database and the read-only snapshot, and that every existing notification task for those messages is `accepted_upstream`. It rechecks the device IDs before deletion and verifies that other messages remain afterward. Missing copies, pending notifications, changed records, or a missing snapshot cause cleanup to stop.

This deletes only the device's copies. Local SQLite history remains subject to the separately configured retention policy. The snapshot is required for cleanup even if you do not enable the inbox web service; the provided forwarding init script already exports it.

## Optional read-only inbox

`inbox_server.py` serves `GET /`, `GET /api/v1/messages`, and `GET /api/v1/status`. The page supports cursor-based history loading. The included service stays disabled until you configure it.

1. Create a dedicated non-root `sim-inbox` user and group using your router's account tools. The tested setup used `shadow-useradd`; the installer does not create the account.
2. Set `/overlay/sim-card-forwarding/inbox-view/` to `root:sim-inbox` mode `2750`. Its exported `inbox.sqlite3` must be `root:sim-inbox` mode `0640`. Set `/etc/sim-card-inbox/` to `root:sim-inbox` mode `0750`.
3. Create `/etc/sim-card-inbox/users.allow`, owned by `root:sim-inbox` with mode `0640`. Put only your own exact Tailscale login on each line. The example `owner@example.com` is a placeholder.
4. Let the forwarder export a fresh snapshot. Check that ownership and permissions remain correct after a subsequent export.
5. Configure a **private Tailscale Serve** HTTPS mapping to `127.0.0.1:8787`, using the syntax supported by your installed version. The backend requires the identity header supplied by Serve and checks it against the allowlist.
6. Enable and start `/etc/init.d/sim-card-inbox`. Verify that only your allowed identity can access it and that it listens only on loopback.

Keep the backend on `127.0.0.1`. Do not expose it directly to LAN or the internet: clients could forge the identity header. Do not use a public Funnel mapping for the inbox. The web service reads only the exported copy and must not have access to PIN credentials, notification credentials, or the authoritative database.

## Limitations

- **Cannot send SMS.** The modem hardware can send messages; this software does not implement it.
- Only the single MF833U1 setup above has been tested.
- Notification acceptance is not a phone delivery receipt; duplicates are possible during retries.
- PIN auto-unlock requires correct device identity and a confirmed PIN; rejected or uncertain attempts are guarded.
- There is no MMS retrieval client. Binary MMS notifications may remain hexadecimal text.

## Development

```sh
python3 -m unittest discover -s tests
```

## Credits and license

Implementation reference: [czwstc/ZTE-MF833U1-SMSForwarder](https://github.com/czwstc/ZTE-MF833U1-SMSForwarder).

[MIT](LICENSE), Copyright (c) 2026 ZerbLion. The upstream copyright notice and complete MIT terms are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
