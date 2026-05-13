#!/usr/bin/env bash
# kentik-device-onboarder GCE test VM startup script.
# Rendered by Terraform (templatefile).

set -euo pipefail

PACKAGE_URL="${package_url}"
EMAIL_SECRET="${kentik_email_secret_id}"
TOKEN_SECRET="${kentik_token_secret_id}"
FLOWPAK_ID="${flowpak_id}"
RUN_KPROXY="${run_kproxy}"
KPROXY_IMAGE="${kproxy_image}"
KPROXY_COMPANY_ID="${kproxy_company_id}"
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

export DEBIAN_FRONTEND=noninteractive
log "apt update"
apt-get update -y

log "installing prerequisites"
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg python3 jq

# ─── Install Docker (only if we need kproxy) ───────────────────────────────
if [[ "$${RUN_KPROXY}" == "true" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        log "installing docker"
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/debian/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        . /etc/os-release
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $${VERSION_CODENAME} stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -y
        apt-get install -y --no-install-recommends \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin
        systemctl enable --now docker
    fi
fi

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

# ─── Run kproxy (optional) ─────────────────────────────────────────────────
if [[ "$${RUN_KPROXY}" == "true" ]]; then
    log "starting kproxy container"
    docker rm -f kproxy >/dev/null 2>&1 || true

    KPROXY_ARGS=(
        --name kproxy
        --restart unless-stopped
        --network host
        -e "KENTIK_API_EMAIL=$${KENTIK_API_EMAIL}"
        -e "KENTIK_API_TOKEN=$${KENTIK_API_TOKEN}"
    )
    if [[ -n "$${KPROXY_COMPANY_ID}" ]]; then
        KPROXY_ARGS+=(-e "KPROXY_COMPANY=$${KPROXY_COMPANY_ID}")
    fi

    docker pull "$${KPROXY_IMAGE}"
    docker run -d "$${KPROXY_ARGS[@]}" "$${KPROXY_IMAGE}" || \
        log "kproxy container failed to start (continuing; onboarder --verify still works)"
fi

# ─── Install kentik-device-onboarder .deb ──────────────────────────────────
log "downloading $${PACKAGE_URL}"
PKG_PATH="/tmp/kentik-device-onboarder.deb"
curl -fsSL --retry 5 --retry-delay 5 -o "$${PKG_PATH}" "$${PACKAGE_URL}"

log "installing package"
apt-get install -y "$${PKG_PATH}"

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
