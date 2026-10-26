#!/bin/sh
# Securely store one instance's SIM PIN without exposing it in argv or terminal output.
set -eu

INSTANCE_ID=${1:-}
CREDENTIALS_DIR=/etc/sim-card-forwarding/credentials.d

if [ "$(id -u)" -ne 0 ]; then
	echo "This script must run as root" >&2
	exit 1
fi

case "$INSTANCE_ID" in
	""|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*)
		echo "Usage: $0 INSTANCE_ID" >&2
		exit 2
		;;
esac
if [ "${#INSTANCE_ID}" -gt 64 ]; then
	echo "INSTANCE_ID is too long" >&2
	exit 2
fi

if [ ! -t 0 ]; then
	echo "A terminal is required for hidden PIN input" >&2
	exit 2
fi

mkdir -p "$CREDENTIALS_DIR"
chmod 700 /etc/sim-card-forwarding "$CREDENTIALS_DIR"
umask 077

saved_stty=$(stty -g)
pin_value=
temporary_file=
cleanup() {
	stty "$saved_stty" 2>/dev/null || true
	pin_value=
	if [ -n "$temporary_file" ] && [ -f "$temporary_file" ]; then
		rm -f "$temporary_file"
	fi
}
trap cleanup EXIT HUP INT TERM

printf 'SIM PIN for %s: ' "$INSTANCE_ID" >&2
stty -echo
IFS= read -r pin_value
stty "$saved_stty"
printf '\n' >&2

case "$pin_value" in
	*[!0-9]*)
		echo "PIN must contain digits only" >&2
		exit 1
		;;
esac
case ${#pin_value} in
	4|5|6|7|8) ;;
	*)
		echo "PIN must contain 4 to 8 digits" >&2
		exit 1
		;;
esac

temporary_file=$(mktemp "${CREDENTIALS_DIR}/.${INSTANCE_ID}.pin.XXXXXX")
printf '%s\n' "$pin_value" >"$temporary_file"
chmod 600 "$temporary_file"
mv "$temporary_file" "${CREDENTIALS_DIR}/${INSTANCE_ID}.pin"
temporary_file=
pin_value=

echo "Stored ${CREDENTIALS_DIR}/${INSTANCE_ID}.pin with mode 0600"
