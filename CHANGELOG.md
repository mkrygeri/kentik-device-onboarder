# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.0] - 2026-05-12

### Added

- **Bounded reverse-DNS lookups**: each PTR lookup now runs with a hard timeout (`KENTIK_ONBOARDER_DNS_TIMEOUT`, default 2 s) and results are cached (`KENTIK_ONBOARDER_DNS_CACHE_TTL` / `KENTIK_ONBOARDER_DNS_NEGATIVE_CACHE_TTL`) so a slow or broken DNS server can no longer stall an onboarder cycle.
- **Optional explicit DNS resolver** via `KENTIK_ONBOARDER_DNS_SERVER` / `--dns-server`. The onboarder issues DNS PTR queries directly to the configured server, bypassing the system resolver. Set the value to `auto` to probe for the cloud metadata server at startup and configure it automatically: GCE → `169.254.169.254`, Azure → `168.63.129.16`, AWS → `169.254.169.253` (Amazon-provided DNS). The probe fails fast on non-cloud hosts and falls back to the system resolver.
- **`--verify` self-test mode**: checks healthcheck reachability, Kentik API DNS, Kentik API authentication, and reverse DNS for sample unregistered IPs. Exits non-zero if anything fails. Safe to run against production (read-only).
- **Device-name sanitization**: PTR results are coerced to lowercase `[a-z0-9._-]` (max 60 chars) before being sent to the Kentik API, so unusual hostnames no longer cause permanent onboarding failures.

### Changed

- `read_healthcheck` now raises a clean `TransientAPIError` (with the offending hostname) on `socket.gaierror`, instead of crashing the cycle. This surfaces "Name or service not known" errors against the configured `KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS` directly in the service log and triggers normal global back-off.
- Reverse-DNS failures are now logged at `INFO` with the underlying error message (`timed out`, `Name or service not known`, etc.) instead of being silently swallowed.

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
