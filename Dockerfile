# ── Build stage (validates syntax only — pure stdlib, nothing to compile) ────
FROM python:3.12-slim AS build

WORKDIR /app
COPY kentik_device_onboarder.py .
# Verify the script parses cleanly
RUN python3 -m py_compile kentik_device_onboarder.py

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="kentik-device-onboarder" \
      org.opencontainers.image.description="Automatically onboards unregistered devices into the Kentik platform" \
      org.opencontainers.image.url="https://github.com/kentik/kentik-device-onboarder" \
      org.opencontainers.image.licenses="Apache-2.0"

# Create an unprivileged system user/group
RUN groupadd --system kentik-onboarder && \
    useradd --system \
            --gid kentik-onboarder \
            --home-dir /opt/kentik-device-onboarder \
            --no-create-home \
            --shell /usr/sbin/nologin \
            kentik-onboarder

# Create required directories
RUN install -d -m 0755 /opt/kentik-device-onboarder && \
    install -d -m 0750 -o kentik-onboarder -g kentik-onboarder /var/lib/kentik-device-onboarder

COPY --from=build --chown=root:kentik-onboarder /app/kentik_device_onboarder.py \
    /opt/kentik-device-onboarder/kentik_device_onboarder.py
RUN chmod 0750 /opt/kentik-device-onboarder/kentik_device_onboarder.py

WORKDIR /opt/kentik-device-onboarder
USER kentik-onboarder

# State is written here — mount a volume to persist it across restarts
VOLUME ["/var/lib/kentik-device-onboarder"]

# All configuration is injected via environment variables (no env file mount needed,
# but you can still bind-mount one with --env-file).
# Required:
#   KENTIK_API_EMAIL
#   KENTIK_API_TOKEN
#   KENTIK_ONBOARDER_FLOWPAK_ID
ENV KENTIK_ONBOARDER_STATE_FILE=/var/lib/kentik-device-onboarder/state.json \
    KENTIK_ONBOARDER_LOG_LEVEL=INFO

ENTRYPOINT ["python3", "/opt/kentik-device-onboarder/kentik_device_onboarder.py"]
