#!/usr/bin/env bash
# kentik-device-onboarder GCE test VM startup script (RHEL family).
# Rendered by Terraform (templatefile).

set -euo pipefail

PACKAGE_URL="${package_url}"
EMAIL_SECRET="${kentik_email_secret_id}"
TOKEN_SECRET="${kentik_token_secret_id}"
FLOWPAK_ID="${flowpak_id}"
INSTALL_UNIVERSAL_AGENT="${install_universal_agent}"
UNIVERSAL_AGENT_INSTALL_URL="${universal_agent_install_url}"
ONBOARDER_LOG_LEVEL="${onboarder_log_level}"

LOG_TAG="onboarder-bootstrap"
log() { logger -t "$${LOG_TAG}" -- "$*"; echo "[$${LOG_TAG}] $*"; }

# ─── Wait for network/DNS ──────────────────────────────────────────────────
for _ in $(seq 1 30); do
    if getent hosts metadata.google.internal >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

log "dnf update metadata"
dnf -y makecache

log "installing prerequisites"
dnf install -y curl jq python3 ca-certificates

# ─── Fetch Kentik credentials from Secret Manager ──────────────────────────
gcloud_token() {
    curl -fsS -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" \
        | jq -r .access_token
}

fetch_secret() {
    local secret_id="$1"
    local token
    token="$(gcloud_token)"
    curl -fsS -H "Authorization: Bearer $${token}" \
        "https://secretmanager.googleapis.com/v1/$${secret_id}/versions/latest:access" \
        | jq -r .payload.data \
        | base64 -d
}

log "fetching credentials from Secret Manager"
KENTIK_API_EMAIL="$(fetch_secret "$${EMAIL_SECRET}")"
KENTIK_API_TOKEN="$(fetch_secret "$${TOKEN_SECRET}")"

if [[ -z "$${KENTIK_API_EMAIL}" || -z "$${KENTIK_API_TOKEN}" ]]; then
    log "FATAL: empty Kentik credentials from Secret Manager"
    exit 1
fi

# ─── Install Kentik universal agent ────────────────────────────────────────
# The universal agent provides the local healthcheck endpoint that the
# onboarder polls. The install script auto-detects the OS and registers a
# systemd unit. Credentials are passed via environment variables so they
# never appear on the command line / in process lists.
if [[ "$${INSTALL_UNIVERSAL_AGENT}" == "true" ]]; then
    log "installing Kentik universal agent from $${UNIVERSAL_AGENT_INSTALL_URL}"
    if KENTIK_API_EMAIL="$${KENTIK_API_EMAIL}" \
       KENTIK_API_TOKEN="$${KENTIK_API_TOKEN}" \
       bash -c "curl -fsSL '$${UNIVERSAL_AGENT_INSTALL_URL}' | sh"; then
        log "universal agent installer succeeded"
    else
        rc=$?
        log "universal agent installer FAILED (rc=$${rc}) - continuing"
    fi
fi

# ─── Install kentik-device-onboarder .rpm ──────────────────────────────────
log "downloading $${PACKAGE_URL}"
PKG_PATH="/tmp/kentik-device-onboarder.rpm"
curl -fsSL --retry 5 --retry-delay 5 -o "$${PKG_PATH}" "$${PACKAGE_URL}"

log "installing package"
# `dnf install` resolves dependencies (e.g. python3, systemd) automatically.
dnf install -y "$${PKG_PATH}"

# ─── Inject credentials & options into onboarder.env ───────────────────────
CONFIG_FILE=/etc/kentik-device-onboarder/onboarder.env

set_kv() {
    local key="$1" value="$2"
    local escaped
    escaped=$(printf '%s' "$${value}" | sed -e 's/[\\&|]/\\&/g')
    if grep -q "^$${key}=" "$${CONFIG_FILE}"; then
        sed -i -e "s|^$${key}=.*$|$${key}=$${escaped}|" "$${CONFIG_FILE}"
    else
        printf '%s=%s\n' "$${key}" "$${value}" >> "$${CONFIG_FILE}"
    fi
}

set_kv KENTIK_API_EMAIL "$${KENTIK_API_EMAIL}"
set_kv KENTIK_API_TOKEN "$${KENTIK_API_TOKEN}"
set_kv KENTIK_ONBOARDER_LOG_LEVEL "$${ONBOARDER_LOG_LEVEL}"
# DNS: explicitly opt into auto-detect on this GCE VM.
set_kv KENTIK_ONBOARDER_DNS_SERVER auto

if [[ "$${FLOWPAK_ID}" != "0" ]]; then
    set_kv KENTIK_ONBOARDER_FLOWPAK_ID "$${FLOWPAK_ID}"
fi

chown root:kentik-onboarder "$${CONFIG_FILE}"
chmod 0640 "$${CONFIG_FILE}"

# ─── Self-test, then start the service ─────────────────────────────────────
log "running --verify"
if /usr/bin/python3 /opt/kentik-device-onboarder/kentik_device_onboarder.py --verify; then
    log "verify OK"
else
    rc=$?
    log "verify FAILED (rc=$${rc}) - starting service anyway so logs are visible"
fi

log "starting kentik-device-onboarder service"
systemctl daemon-reload
systemctl enable --now kentik-device-onboarder.service

log "bootstrap complete"
