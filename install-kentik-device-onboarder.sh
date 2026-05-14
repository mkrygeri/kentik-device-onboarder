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
PLAN_API_URL=${PLAN_API_URL:-https://grpc.api.kentik.com/plans/v202501alpha1}

discover_kproxy_credentials() {
  local pid environ_path line key value
  local found_email=""
  local found_token=""

  pid=$(pgrep -n kproxy 2>/dev/null || true)
  if [[ -z "$pid" ]]; then
    return 1
  fi

  environ_path="/proc/$pid/environ"
  if [[ ! -r "$environ_path" ]]; then
    return 1
  fi

  while IFS='=' read -r key value; do
    case "$key" in
      KENTIK_API_EMAIL) found_email="$value" ;;
      KENTIK_API_TOKEN) found_token="$value" ;;
    esac
  done < <(tr '\0' '\n' < "$environ_path")

  if [[ -n "$found_email" && -n "$found_token" ]]; then
    KPROXY_KENTIK_API_EMAIL="$found_email"
    KPROXY_KENTIK_API_TOKEN="$found_token"
    return 0
  fi

  return 1
}

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

populate_credentials_in_config() {
  local config_file="$1"
  local email=""
  local token=""
  local source=""
  local email_escaped token_escaped

  # Preferred: pick credentials up from this script's own environment so
  # that `KENTIK_API_EMAIL=... KENTIK_API_TOKEN=... ./install-...sh`
  # works for unattended bootstrap. This is the only path that succeeds
  # with the new Kentik universal agent (kagent), which authenticates via
  # K_COMPANY_ID/K_REGISTER_PROVISIONING_TOKEN and does not export the
  # legacy KENTIK_API_* pair into the kproxy process environment.
  if [[ -n "${KENTIK_API_EMAIL:-}" && -n "${KENTIK_API_TOKEN:-}" ]]; then
    email="$KENTIK_API_EMAIL"
    token="$KENTIK_API_TOKEN"
    source="installer environment (KENTIK_API_EMAIL / KENTIK_API_TOKEN)"
  elif discover_kproxy_credentials; then
    email="$KPROXY_KENTIK_API_EMAIL"
    token="$KPROXY_KENTIK_API_TOKEN"
    source="legacy kproxy process environment"
  else
    echo "Kentik API credentials not provided. Set KENTIK_API_EMAIL and"
    echo "KENTIK_API_TOKEN before running this installer, or edit"
    echo "$config_file before starting the service. Leaving placeholders unchanged."
    return 0
  fi

  email_escaped=$(escape_sed_replacement "$email")
  token_escaped=$(escape_sed_replacement "$token")

  sed -i \
    -e "s|^KENTIK_API_EMAIL=.*$|KENTIK_API_EMAIL=${email_escaped}|" \
    -e "s|^KENTIK_API_TOKEN=.*$|KENTIK_API_TOKEN=${token_escaped}|" \
    "$config_file"

  echo "populated KENTIK_API_EMAIL and KENTIK_API_TOKEN from ${source}"
}

get_config_value() {
  local config_file="$1"
  local key="$2"
  sed -n "s/^${key}=//p" "$config_file" | tail -n 1
}

set_config_value() {
  local config_file="$1"
  local key="$2"
  local value="$3"
  local escaped

  escaped=$(escape_sed_replacement "$value")
  if grep -q "^${key}=" "$config_file"; then
    sed -i -e "s|^${key}=.*$|${key}=${escaped}|" "$config_file"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$config_file"
  fi
}

fetch_default_flowpak_plan_id() {
  local email="$1"
  local token="$2"

  KENTIK_API_EMAIL="$email" \
  KENTIK_API_TOKEN="$token" \
  PLAN_API_URL="$PLAN_API_URL" \
  "$PYTHON_BIN" - <<'PY'
import json
import os
import sys
from urllib import request, error

email = os.environ.get("KENTIK_API_EMAIL", "").strip()
token = os.environ.get("KENTIK_API_TOKEN", "").strip()
url = os.environ.get("PLAN_API_URL", "").strip()

if not email or not token or not url:
    raise SystemExit(1)

req = request.Request(
    url=url,
    method="GET",
    headers={
        "accept": "application/json",
        "X-CH-Auth-Email": email,
        "X-CH-Auth-API-Token": token,
        "User-Agent": "kentik-device-onboarder-installer/1.1.4",
    },
)

try:
    with request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError):
    raise SystemExit(2)

best = None
for plan in payload.get("plans", []):
    metadata = plan.get("metadata") or {}
    if str(metadata.get("type", "")).lower() != "flowpak":
        continue
    try:
        max_fps = int(plan.get("maxFps", 0))
    except (TypeError, ValueError):
        max_fps = 0
    if best is None or max_fps > best[0]:
        best = (max_fps, str(plan.get("id", "")).strip())

if not best or not best[1].isdigit():
    raise SystemExit(3)

print(best[1])
PY
}

choose_flowpak_id_interactively() {
  local current_value="$1"
  local entered=""

  if [[ ! -r /dev/tty || ! -w /dev/tty ]]; then
    return 1
  fi

  while true; do
    printf 'Unable to auto-discover flowpak plan ID. Enter default flowpak plan ID [%s]: ' "$current_value" > /dev/tty
    if ! IFS= read -r entered < /dev/tty; then
      return 1
    fi
    entered=${entered:-$current_value}
    if [[ "$entered" =~ ^[0-9]+$ && "$entered" != "0" ]]; then
      printf '%s\n' "$entered"
      return 0
    fi
    printf 'Invalid plan ID. Please enter a positive integer.\n' > /dev/tty
  done
}

populate_flowpak_id_in_config() {
  local config_file="$1"
  local email token current_value discovered chosen

  current_value=$(get_config_value "$config_file" "KENTIK_ONBOARDER_FLOWPAK_ID")
  current_value=${current_value:-12345}
  email=$(get_config_value "$config_file" "KENTIK_API_EMAIL")
  token=$(get_config_value "$config_file" "KENTIK_API_TOKEN")

  discovered=""
  if [[ -n "$email" && -n "$token" ]]; then
    discovered=$(fetch_default_flowpak_plan_id "$email" "$token" 2>/dev/null || true)
  fi

  if [[ "$discovered" =~ ^[0-9]+$ && "$discovered" != "0" ]]; then
    set_config_value "$config_file" "KENTIK_ONBOARDER_FLOWPAK_ID" "$discovered"
    echo "auto-selected flowpak plan ID from API"
    return 0
  fi

  if chosen=$(choose_flowpak_id_interactively "$current_value"); then
    set_config_value "$config_file" "KENTIK_ONBOARDER_FLOWPAK_ID" "$chosen"
    echo "set flowpak plan ID from installer prompt"
    return 0
  fi

  echo "could not auto-discover flowpak plan ID and no interactive input available; leaving current value unchanged"
}

add_config_value_if_missing() {
  local config_file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "$config_file"; then
    return 0
  fi
  printf '%s=%s\n' "$key" "$value" >> "$config_file"
  echo "added missing ${key} to $(basename "$config_file")"
}

migrate_config_to_v1_1_0() {
  # Append the new v1.1.0 DNS knobs to existing configs without modifying any
  # existing values. Skipped silently if a key is already present.
  local config_file="$1"
  add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_TIMEOUT 2s
  add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_CACHE_TTL 1h
  add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_NEGATIVE_CACHE_TTL 5m
  add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_SERVER auto
}

extract_host() {
  # Extract just the hostname from a value like "https://grpc.api.kentik.com"
  # or "127.0.0.1:9996". Returns empty string on parse failure.
  local value="$1"
  value=${value#http://}
  value=${value#https://}
  value=${value%%/*}
  value=${value%%:*}
  printf '%s' "$value"
}

dns_preflight() {
  # Resolve the configured Kentik API host and healthcheck host so the
  # operator sees "name or service not known" immediately during install
  # instead of discovering it later in `journalctl -u
  # kentik-device-onboarder`. Non-fatal: we warn and continue so an
  # air-gapped or staged install still completes.
  local config_file="$1"
  local api_root healthcheck_addr api_host hc_host
  local failures=0

  api_root=$(get_config_value "$config_file" "KENTIK_ONBOARDER_API_ROOT")
  api_root=${api_root:-https://grpc.api.kentik.com}
  healthcheck_addr=$(get_config_value "$config_file" "KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS")
  healthcheck_addr=${healthcheck_addr:-127.0.0.1:9996}

  api_host=$(extract_host "$api_root")
  hc_host=$(extract_host "$healthcheck_addr")

  if [[ -n "$api_host" ]] && ! getent hosts "$api_host" >/dev/null 2>&1; then
    echo "WARNING: cannot resolve Kentik API host '$api_host' (name or service not known)." >&2
    echo "         the service will fail to onboard devices until DNS works." >&2
    echo "         check /etc/resolv.conf, outbound DNS, and proxy/firewall rules." >&2
    failures=$((failures + 1))
  fi

  # If the operator configured an HTTPS proxy in onboarder.env, resolve it
  # too so misconfigurations surface during install instead of as cryptic
  # connect failures later.
  local proxy_url proxy_host
  proxy_url=$(get_config_value "$config_file" "HTTPS_PROXY")
  [[ -z "$proxy_url" ]] && proxy_url=$(get_config_value "$config_file" "HTTP_PROXY")
  [[ -z "$proxy_url" ]] && proxy_url=$(get_config_value "$config_file" "https_proxy")
  [[ -z "$proxy_url" ]] && proxy_url=$(get_config_value "$config_file" "http_proxy")
  if [[ -n "$proxy_url" ]]; then
    # Strip embedded credentials before host extraction.
    proxy_host=$(extract_host "${proxy_url##*@}")
    if [[ -n "$proxy_host" && ! "$proxy_host" =~ ^[0-9.]+$ ]] && ! getent hosts "$proxy_host" >/dev/null 2>&1; then
      echo "WARNING: cannot resolve HTTP proxy host '$proxy_host' (name or service not known)." >&2
      echo "         the service will fail to reach Kentik until DNS for the proxy works." >&2
      failures=$((failures + 1))
    fi
  fi

  # Skip the healthcheck check for IP literals — getent always succeeds and
  # there's nothing to diagnose.
  if [[ -n "$hc_host" && ! "$hc_host" =~ ^[0-9.]+$ && ! "$hc_host" =~ ^[0-9a-fA-F:]+$ ]]; then
    if ! getent hosts "$hc_host" >/dev/null 2>&1; then
      echo "WARNING: cannot resolve healthcheck host '$hc_host' (name or service not known)." >&2
      echo "         set KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS in $config_file to a reachable address." >&2
      failures=$((failures + 1))
    fi
  fi

  if [[ $failures -eq 0 ]]; then
    echo "DNS preflight OK (api=$api_host healthcheck=$hc_host)"
  fi
}

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
  populate_credentials_in_config "$CONFIG_DIR/onboarder.env"
  populate_flowpak_id_in_config "$CONFIG_DIR/onboarder.env"
  echo "created example environment file at $CONFIG_DIR/onboarder.env"
else
  populate_flowpak_id_in_config "$CONFIG_DIR/onboarder.env"
  migrate_config_to_v1_1_0 "$CONFIG_DIR/onboarder.env"
  echo "updated flowpak plan ID in existing environment file at $CONFIG_DIR/onboarder.env"
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

dns_preflight "$CONFIG_DIR/onboarder.env"

cat <<EOF
installation complete.

next steps:
1. edit $CONFIG_DIR/onboarder.env
2. review the unit with: systemctl cat $SERVICE_NAME
3. start the service with: systemctl start $SERVICE_NAME
4. follow logs with: journalctl -u $SERVICE_NAME -f
EOF