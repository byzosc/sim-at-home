#!/bin/sh
set -eu

INTERFACE_NAME=${1:-mf833u1_1}
CONFIG_DIR=${2:-/etc/sim-card-forwarding/instances.d}
STATE_DIR=${3:-/overlay/sim-card-forwarding/state-data}

echo "OpenWrt interface:"
ifstatus "$INTERFACE_NAME"

echo
echo "USB network devices:"
for netdev in /sys/class/net/*; do
	driver=$(readlink -f "$netdev/device/driver" 2>/dev/null || true)
	case "$driver" in
		*/cdc_ether)
			echo "$(basename "$netdev"): $driver"
			;;
	esac
done

echo
echo "Forwarder API dry run:"
/usr/bin/python3 /usr/libexec/sim-card-forwarding/mf833u1_forwarder.py \
	--config-dir "$CONFIG_DIR" \
	--state-dir "$STATE_DIR" \
	--once \
	--dry-run
