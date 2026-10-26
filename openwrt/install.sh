#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TARGET_LIB=/usr/libexec/sim-card-forwarding
TARGET_CONFIG=/etc/sim-card-forwarding/instances.d
TARGET_CREDENTIALS=/etc/sim-card-forwarding/credentials.d
TARGET_STATE=/overlay/sim-card-forwarding/state-data
TARGET_VIEW=/overlay/sim-card-forwarding/inbox-view
TARGET_INBOX_CONFIG=/etc/sim-card-inbox

if ! command -v python3 >/dev/null 2>&1 || \
	! python3 -c 'import encodings.idna, http.server, ipaddress, socketserver, sqlite3, ssl, urllib.request' >/dev/null 2>&1; then
	echo "Python modules are missing; run:" >&2
	echo "  opkg install python3-light python3-urllib python3-openssl python3-codecs python3-sqlite3" >&2
	exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
	echo "curl is required" >&2
	exit 1
fi

mkdir -p "$TARGET_LIB" "$TARGET_CONFIG" "$TARGET_CREDENTIALS" "$TARGET_STATE"
chmod 700 /etc/sim-card-forwarding "$TARGET_CONFIG" "$TARGET_CREDENTIALS" "$TARGET_STATE"
if [ ! -d "$TARGET_VIEW" ]; then
	mkdir -p "$TARGET_VIEW"
	chmod 700 "$TARGET_VIEW"
fi
if [ ! -d "$TARGET_INBOX_CONFIG" ]; then
	mkdir -p "$TARGET_INBOX_CONFIG"
	chmod 700 "$TARGET_INBOX_CONFIG"
fi

cp "$SOURCE_DIR/mf833u1_forwarder.py" "$TARGET_LIB/mf833u1_forwarder.py"
cp "$SOURCE_DIR/sms_store.py" "$TARGET_LIB/sms_store.py"
cp "$SOURCE_DIR/inbox_server.py" "$TARGET_LIB/inbox_server.py"
cp "$SOURCE_DIR/openwrt/sim-card-forwarding.init" /etc/init.d/sim-card-forwarding
cp "$SOURCE_DIR/openwrt/sim-card-inbox.init" /etc/init.d/sim-card-inbox
chmod 755 \
	"$TARGET_LIB/mf833u1_forwarder.py" \
	"$TARGET_LIB/inbox_server.py" \
	/etc/init.d/sim-card-forwarding \
	/etc/init.d/sim-card-inbox
chmod 644 "$TARGET_LIB/sms_store.py"

if [ ! -f "$TARGET_CONFIG/modem-1.conf" ]; then
	cp "$SOURCE_DIR/config/instances.d/modem-1.conf.example" \
		"$TARGET_CONFIG/modem-1.conf.example"
	chmod 600 "$TARGET_CONFIG/modem-1.conf.example"
fi

echo "Installed. Create $TARGET_CONFIG/modem-1.conf, then run:"
echo "  $TARGET_LIB/mf833u1_forwarder.py --once --dry-run"
echo "Store an auto-unlock PIN with:"
echo "  $SOURCE_DIR/openwrt/set-pin-credential.sh INSTANCE_ID"
echo "The service was not enabled or started automatically."
echo "The inbox also remains disabled until its account, allowlist and Tailscale Serve"
echo "mapping have been prepared and verified."
