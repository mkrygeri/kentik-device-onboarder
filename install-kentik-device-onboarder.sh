#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=${INSTALL_DIR:-/opt/kentik-device-onboarder}
CONFIG_DIR=${CONFIG_DIR:-/etc/kentik-device-onboarder}
STATE_DIR=${STATE_DIR:-/var/lib/kentik-device-onboarder}
SERVICE_NAME=${SERVICE_NAME:-kentik-device-onboarder.service}
SERVICE_USER=${SERVICE_USER:-kentik-onboarder}
SERVICE_GROUP=${SERVICE_GROUP:-kentik-onboarder}
PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}

if [[ $EUID -ne 0 ]]; then
  echo "this installer must be run as root" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python interpreter not found at $PYTHON_BIN" >&2
  exit 1
fi

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "$SERVICE_GROUP" \
    --home-dir "$INSTALL_DIR" \
    --create-home \
    --shell /usr/sbin/nologin \
    "$SERVICE_USER"
fi

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CONFIG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"

install -m 0755 "$SCRIPT_DIR/kentik_device_onboarder.py" "$INSTALL_DIR/kentik_device_onboarder.py"
install -m 0644 "$SCRIPT_DIR/kentik-device-onboarder.service" "/etc/systemd/system/$SERVICE_NAME"

if [[ ! -f "$CONFIG_DIR/onboarder.env" ]]; then
  install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SCRIPT_DIR/kentik-device-onboarder.env.example" "$CONFIG_DIR/onboarder.env"
  echo "created example environment file at $CONFIG_DIR/onboarder.env"
else
  echo "leaving existing environment file unchanged at $CONFIG_DIR/onboarder.env"
fi

sed -i \
  -e "s|/usr/bin/python3|$PYTHON_BIN|g" \
  -e "s|/opt/kentik-device-onboarder|$INSTALL_DIR|g" \
  -e "s|/etc/kentik-device-onboarder|$CONFIG_DIR|g" \
  -e "s|/var/lib/kentik-device-onboarder|$STATE_DIR|g" \
  "/etc/systemd/system/$SERVICE_NAME"

chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR" "$STATE_DIR"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

cat <<EOF
installation complete.

next steps:
1. edit $CONFIG_DIR/onboarder.env
2. review the unit with: systemctl cat $SERVICE_NAME
3. start the service with: systemctl start $SERVICE_NAME
4. follow logs with: journalctl -u $SERVICE_NAME -f
EOF