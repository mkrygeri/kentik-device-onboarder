# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.1] - 2026-04-28

### Added

- Installer now auto-selects `KENTIK_ONBOARDER_FLOWPAK_ID` by calling the Kentik plans API and choosing the `flowpak` plan with the highest `maxFps`.
- If plans API lookup fails during interactive install, installer prompts for a default flowpak ID.
- DEB/RPM upgrades now refresh `KENTIK_ONBOARDER_FLOWPAK_ID` in existing config using the same selection logic.

### Changed

- Installer credential-to-config population now works together with automatic flowpak plan discovery in manual install and package post-install scripts.

---

## [1.0.0] - 2026-04-22

### Added

- Initial release of `kentik-device-onboarder`.
- Polls a Kentik flow-pak healthcheck TCP socket to discover unregistered devices.
- Batch-creates devices via the Kentik `/device/v202504beta2/device/batch_create` API.
- Reverse-DNS lookup for human-readable device names with fallback to IP address.
- Per-device exponential back-off with configurable base and maximum delay.
- Global back-off on healthcheck or API failures.
- Success cooldown to avoid re-submitting already-registered devices.
- Client-side token-bucket rate limiter for API requests.
- JSON state file persisted across restarts.
- `--dry-run` and `--run-once` flags for testing and scripting.
- Configurable via environment variables or CLI flags.
- systemd unit with strict sandboxing (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`).
- `install-kentik-device-onboarder.sh` manual installer.
- Linux package support:
  - `.deb` (Debian / Ubuntu) via fpm and native `dpkg-buildpackage`.
  - `.rpm` (RHEL / Rocky / Alma Linux) via fpm and native `rpmbuild`.
- Docker support:
  - 2-stage `Dockerfile` (`python:3.12-slim`) running as an unprivileged user.
  - `docker-compose.yml` for single-command container deployment.
  - `make docker` and `make docker-push` Makefile targets.
- Installer enhancements:
  - Installers now attempt to read `KENTIK_API_EMAIL` and `KENTIK_API_TOKEN` from the newest `kproxy` process environment (`/proc/$(pgrep -n kproxy)/environ`).
  - When `onboarder.env` is created for the first time, these credentials are injected automatically.
  - Installers now call `https://grpc.api.kentik.com/plans/v202501alpha1`, choose the `flowpak` plan with the highest `maxFps`, and set `KENTIK_ONBOARDER_FLOWPAK_ID` automatically.
  - If the plans API cannot be reached during an interactive install, the installer prompts for a default flowpak plan ID.
  - On package upgrades, the installer refreshes `KENTIK_ONBOARDER_FLOWPAK_ID` in existing configs.
- `Makefile` with `all`, `deb`, `rpm`, `native-deb`, `native-rpm`, `docker`, `docker-push`, `lint`, and `clean` targets.
