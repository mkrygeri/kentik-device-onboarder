Name:           kentik-device-onboarder
Version:        1.1.4
Release:        1%{?dist}
Summary:        Automatically onboards unregistered devices into the Kentik platform
License:        Apache-2.0
URL:            https://github.com/kentik/kentik-device-onboarder
BuildArch:      noarch

Requires:       python3
Requires(pre):  shadow-utils
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
kentik-device-onboarder is a lightweight Python daemon that polls a Kentik
flow-pak healthcheck socket, discovers unregistered flow-sending devices, and
automatically registers them with the Kentik API. It runs as a systemd service
with exponential back-off, per-device retry state, and client-side rate limiting.

# ── Sources ──────────────────────────────────────────────────────────────────
# To build this package, place the following files next to the spec:
#   kentik_device_onboarder.py
#   kentik-device-onboarder.service
#   kentik-device-onboarder.env.example
#   packaging/postinst
#   packaging/prerm
#
# Then run from the repository root:
#   rpmbuild -bb --define "_sourcedir $PWD" --define "_specdir $PWD/packaging/rpm" \
#            --define "_builddir $PWD/build" --define "_rpmdir $PWD/dist" \
#            --define "_srcrpmdir $PWD/dist" packaging/rpm/kentik-device-onboarder.spec

%prep
# Nothing to unpack — files are referenced directly from %{_sourcedir}.

%build
# Pure Python — nothing to compile.

%install
install -D -m 0755 %{_sourcedir}/kentik_device_onboarder.py \
    %{buildroot}/opt/kentik-device-onboarder/kentik_device_onboarder.py

install -D -m 0644 %{_sourcedir}/kentik-device-onboarder.service \
    %{buildroot}/usr/lib/systemd/system/kentik-device-onboarder.service

install -D -m 0644 %{_sourcedir}/kentik-device-onboarder.env.example \
    %{buildroot}/etc/kentik-device-onboarder/onboarder.env.example

%pre
# Create system group and user before files are laid down.
getent group kentik-onboarder >/dev/null 2>&1 || \
    groupadd --system kentik-onboarder

id -u kentik-onboarder >/dev/null 2>&1 || \
    useradd \
        --system \
        --gid kentik-onboarder \
        --home-dir /opt/kentik-device-onboarder \
        --no-create-home \
        --shell /usr/sbin/nologin \
        kentik-onboarder

exit 0

%post
STATE_DIR=/var/lib/kentik-device-onboarder
CONFIG_DIR=/etc/kentik-device-onboarder
INSTALL_DIR=/opt/kentik-device-onboarder
PLAN_API_URL=https://grpc.api.kentik.com/plans/v202501alpha1

discover_kproxy_credentials() {
    pid=$(pgrep -n kproxy 2>/dev/null || true)
    if [ -z "$pid" ] || [ ! -r "/proc/$pid/environ" ]; then
        return 1
    fi

    KPROXY_KENTIK_API_EMAIL=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^KENTIK_API_EMAIL=//p' | tail -n 1)
    KPROXY_KENTIK_API_TOKEN=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^KENTIK_API_TOKEN=//p' | tail -n 1)

    [ -n "$KPROXY_KENTIK_API_EMAIL" ] && [ -n "$KPROXY_KENTIK_API_TOKEN" ]
}

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

populate_credentials_in_config() {
    config_file="$1"
    cred_email=""
    cred_token=""
    cred_source=""

    # Preferred: pick credentials up from this script's own environment so
    # that `KENTIK_API_EMAIL=... KENTIK_API_TOKEN=... dnf install ...`
    # works for unattended bootstrap. This is the only path that succeeds
    # with the new Kentik universal agent (kagent), which authenticates via
    # K_COMPANY_ID/K_REGISTER_PROVISIONING_TOKEN and does not export the
    # legacy KENTIK_API_* pair into the kproxy process environment.
    if [ -n "${KENTIK_API_EMAIL:-}" ] && [ -n "${KENTIK_API_TOKEN:-}" ]; then
        cred_email="$KENTIK_API_EMAIL"
        cred_token="$KENTIK_API_TOKEN"
        cred_source="installer environment (KENTIK_API_EMAIL / KENTIK_API_TOKEN)"
    elif discover_kproxy_credentials; then
        cred_email="$KPROXY_KENTIK_API_EMAIL"
        cred_token="$KPROXY_KENTIK_API_TOKEN"
        cred_source="legacy kproxy process environment"
    else
        echo "Kentik API credentials not provided. Set KENTIK_API_EMAIL and"
        echo "KENTIK_API_TOKEN before installing, or edit $config_file before"
        echo "starting the service. Leaving placeholders unchanged."
        return 0
    fi

    email_escaped=$(escape_sed_replacement "$cred_email")
    token_escaped=$(escape_sed_replacement "$cred_token")

    sed -i \
        -e "s|^KENTIK_API_EMAIL=.*$|KENTIK_API_EMAIL=${email_escaped}|" \
        -e "s|^KENTIK_API_TOKEN=.*$|KENTIK_API_TOKEN=${token_escaped}|" \
        "$config_file"

    echo "populated KENTIK_API_EMAIL and KENTIK_API_TOKEN from ${cred_source}"
}

get_config_value() {
    config_file="$1"
    key="$2"
    sed -n "s/^${key}=//p" "$config_file" | tail -n 1
}

set_config_value() {
    config_file="$1"
    key="$2"
    value="$3"
    escaped=$(escape_sed_replacement "$value")

    if grep -q "^${key}=" "$config_file"; then
        sed -i -e "s|^${key}=.*$|${key}=${escaped}|" "$config_file"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$config_file"
    fi
}

fetch_default_flowpak_plan_id() {
    email="$1"
    token="$2"

    KENTIK_API_EMAIL="$email" \
    KENTIK_API_TOKEN="$token" \
    PLAN_API_URL="$PLAN_API_URL" \
    python3 - <<'PY'
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
    current_value="$1"
    entered=""

    if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
        return 1
    fi

    while :; do
        printf 'Unable to auto-discover flowpak plan ID. Enter default flowpak plan ID [%s]: ' "$current_value" > /dev/tty
        if ! IFS= read entered < /dev/tty; then
            return 1
        fi
        [ -z "$entered" ] && entered="$current_value"
        case "$entered" in
            ''|*[!0-9]*|0)
                printf 'Invalid plan ID. Please enter a positive integer.\n' > /dev/tty
                ;;
            *)
                printf '%s\n' "$entered"
                return 0
                ;;
        esac
    done
}

populate_flowpak_id_in_config() {
    config_file="$1"
    current_value=$(get_config_value "$config_file" "KENTIK_ONBOARDER_FLOWPAK_ID")
    [ -z "$current_value" ] && current_value=12345

    email=$(get_config_value "$config_file" "KENTIK_API_EMAIL")
    token=$(get_config_value "$config_file" "KENTIK_API_TOKEN")

    discovered=""
    if [ -n "$email" ] && [ -n "$token" ]; then
        discovered=$(fetch_default_flowpak_plan_id "$email" "$token" 2>/dev/null || true)
    fi

    case "$discovered" in
        ''|*[!0-9]*|0)
            ;;
        *)
            set_config_value "$config_file" "KENTIK_ONBOARDER_FLOWPAK_ID" "$discovered"
            echo "auto-selected flowpak plan ID from API"
            return 0
            ;;
    esac

    chosen=$(choose_flowpak_id_interactively "$current_value" || true)
    case "$chosen" in
        ''|*[!0-9]*|0)
            echo "could not auto-discover flowpak plan ID and no interactive input available; leaving current value unchanged"
            ;;
        *)
            set_config_value "$config_file" "KENTIK_ONBOARDER_FLOWPAK_ID" "$chosen"
            echo "set flowpak plan ID from installer prompt"
            ;;
    esac
}

add_config_value_if_missing() {
    config_file="$1"
    key="$2"
    value="$3"
    if grep -q "^${key}=" "$config_file"; then
        return 0
    fi
    printf '%s=%s\n' "$key" "$value" >> "$config_file"
    echo "added missing ${key} to $(basename "$config_file")"
}

migrate_config_to_v1_1_0() {
    # Append v1.1.0 DNS knobs to existing configs without modifying existing
    # values. Idempotent across reinstalls and upgrades.
    config_file="$1"
    add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_TIMEOUT 2s
    add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_CACHE_TTL 1h
    add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_NEGATIVE_CACHE_TTL 5m
    add_config_value_if_missing "$config_file" KENTIK_ONBOARDER_DNS_SERVER auto
}

install -d -m 0750 -o kentik-onboarder -g kentik-onboarder "$STATE_DIR"

chown root:kentik-onboarder "$CONFIG_DIR"
chmod 0750 "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/onboarder.env" ]; then
    install -m 0640 -o kentik-onboarder -g kentik-onboarder \
        "$CONFIG_DIR/onboarder.env.example" \
        "$CONFIG_DIR/onboarder.env"
    populate_credentials_in_config "$CONFIG_DIR/onboarder.env"
    echo "Created initial configuration: $CONFIG_DIR/onboarder.env"
    echo "Edit this file before starting the service."
fi

populate_flowpak_id_in_config "$CONFIG_DIR/onboarder.env"
migrate_config_to_v1_1_0 "$CONFIG_DIR/onboarder.env"

chown root:kentik-onboarder "$INSTALL_DIR"
chown kentik-onboarder:kentik-onboarder "$INSTALL_DIR/kentik_device_onboarder.py"

%systemd_post kentik-device-onboarder.service

%preun
%systemd_preun kentik-device-onboarder.service

%postun
%systemd_postun_with_restart kentik-device-onboarder.service

%files
%attr(0755, root, kentik-onboarder) /opt/kentik-device-onboarder
%attr(0755, kentik-onboarder, kentik-onboarder) /opt/kentik-device-onboarder/kentik_device_onboarder.py
%attr(0755, root, kentik-onboarder) /etc/kentik-device-onboarder
%config(noreplace) %attr(0644, root, root) /etc/kentik-device-onboarder/onboarder.env.example
%attr(0644, root, root) /usr/lib/systemd/system/kentik-device-onboarder.service

%changelog
* Thu May 14 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.1.4-1
- Runtime: install ProxyHandler in the urllib opener so HTTPS_PROXY /
  HTTP_PROXY / NO_PROXY env vars in onboarder.env are honored. Cloud
  metadata probes always bypass the proxy.
- Postinst: DNS preflight resolves the configured Kentik API host,
  healthcheck host, and (when set) HTTP proxy host immediately after
  writing onboarder.env, surfacing 'name or service not known' at
  install time. Non-fatal.

* Wed May 13 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.1.3-1
- Postinst now honors KENTIK_API_EMAIL / KENTIK_API_TOKEN from the
  installer environment (works under the new universal agent).

* Wed May 13 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.1.2-1
- Default api_root corrected to https://grpc.api.kentik.com.
- Device payload now includes minimizeSnmp; failed batches log the
  underlying validation error.

* Tue May 12 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.1.1-1
- Upgrade migration: postinst now appends KENTIK_ONBOARDER_DNS_SERVER=auto and
  the new DNS timeout/cache keys to existing /etc/kentik-device-onboarder/
  onboarder.env files. Existing user-set values are never modified.
* Tue May 12 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.1.0-1
- Bounded reverse-DNS lookups with caching and per-lookup timeout
- Optional explicit DNS resolver (KENTIK_ONBOARDER_DNS_SERVER), with 'auto'
  detection of GCE/Azure/AWS metadata servers
- New --verify self-test mode (read-only)
- Sanitize device names to the Kentik-accepted character set
- read_healthcheck now reports DNS failures cleanly instead of crashing
* Tue Apr 22 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.0.0-1
- Initial package release
