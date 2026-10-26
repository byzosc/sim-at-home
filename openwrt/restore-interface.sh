#!/bin/sh
# Recreate the previously verified MF833U1 interface, but do not bring it up.
set -eu

INTERFACE_NAME=mf833u1_1
DEVICE_NAME=${1:-eth2}
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/root/network.backup-before-${INTERFACE_NAME}-restore-${STAMP}"

if uci -q get "network.${INTERFACE_NAME}" >/dev/null 2>&1; then
	echo "network.${INTERFACE_NAME} already exists; no change made"
	exit 0
fi

if [ ! -e "/sys/class/net/${DEVICE_NAME}" ]; then
	echo "${DEVICE_NAME} does not exist; reconnect/authorize the modem first" >&2
	exit 1
fi

DRIVER=$(readlink -f "/sys/class/net/${DEVICE_NAME}/device/driver" 2>/dev/null || true)
case "$DRIVER" in
	*/cdc_ether) ;;
	*)
		echo "${DEVICE_NAME} is not using cdc_ether; refusing to change UCI" >&2
		exit 1
		;;
esac

cp /etc/config/network "$BACKUP"

uci set "network.${INTERFACE_NAME}=interface"
uci set "network.${INTERFACE_NAME}.device=${DEVICE_NAME}"
uci set "network.${INTERFACE_NAME}.proto=dhcp"
uci set "network.${INTERFACE_NAME}.defaultroute=0"
uci set "network.${INTERFACE_NAME}.peerdns=0"
uci set "network.${INTERFACE_NAME}.delegate=0"
uci commit network

echo "Created network.${INTERFACE_NAME}; backup: ${BACKUP}"
echo "The interface was intentionally NOT started."
echo "After diagnostics are running, start it with: ifup ${INTERFACE_NAME}"
