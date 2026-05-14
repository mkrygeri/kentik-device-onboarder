# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.1.4] - 2026-05-14

### Added

- **HTTP proxy support.** The runtime now installs `urllib.request.ProxyHandler()` in its API client, so standard `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` environment variables (and their lower-case forms) are honored for all calls to the Kentik API. Set them in `/etc/kentik-device-onboarder/onboarder.env` and the existing `EnvironmentFile=` directive in the systemd unit picks them up automatically. The active proxy is logged once at startup with embedded credentials redacted. Cloud metadata probes (169.254.169.254, 168.63.129.16) explicitly bypass the proxy so `KENTIK_ONBOARDER_DNS_SERVER=auto` keeps working.
- **Install-time DNS preflight.** Both the package postinst (DEB and RPM) and `install-kentik-device-onboarder.sh` now resolve the configured Kentik API host, healthcheck host, and (when set) HTTP proxy host via `getent hosts` immediately after writing `onboarder.env`. Failures produce a clear `WARNING: cannot resolve ... (name or service not known).` line so operators see the problem at install time instead of having to dig it out of `journalctl -u kentik-device-onboarder` later. Non-fatal: the install still completes.
- `kentik-device-onboarder.env.example` documents the proxy variables.
- README has a new "Outbound HTTP proxy" section and a "name or service not known during install" troubleshooting section.

---

## [1.1.3] - 2026-05-13

### Fixed

- Installer credential bootstrap now honors `KENTIK_API_EMAIL` and `KENTIK_API_TOKEN` from the install command's environment. Previously the postinst (DEB and RPM) and the standalone `install-kentik-device-onboarder.sh` only knew how to read credentials from a running legacy `kproxy` process's `/proc/<pid>/environ`. That discovery silently fails under the new Kentik universal agent (`kagent`), which authenticates via `K_COMPANY_ID`/`K_REGISTER_PROVISIONING_TOKEN` and never exports the legacy `KENTIK_API_*` pair. After this change, the documented unattended install path works:

  ```
  KENTIK_API_EMAIL=... KENTIK_API_TOKEN=... dnf install -y ./kentik-device-onboarder-*.rpm
  KENTIK_API_EMAIL=... KENTIK_API_TOKEN=... apt-get install -y ./kentik-device-onboarder_*.deb
  KENTIK_API_EMAIL=... KENTIK_API_TOKEN=... ./install-kentik-device-onboarder.sh
  ```

  Order of precedence: installer-environment variables → legacy kproxy `/proc/<pid>/environ` → unchanged placeholders. When no source provides credentials the installer now logs an explicit, actionable message naming the env vars and the config path, instead of the previous misleading "kproxy credentials not found in process environment" warning.

- The auto-discovery of `KENTIK_ONBOARDER_FLOWPAK_ID` (which calls the plans API) is now reachable on a fresh install whenever credentials are supplied via env vars, since the credentials are written to `onboarder.env` before the plan lookup runs.

### Changed

- `terraform/gcp-test/startup.sh` now exports `KENTIK_API_EMAIL` / `KENTIK_API_TOKEN` into the `dnf install` invocation so the new postinst path is exercised end-to-end on the GCE test VM.

---

## [1.1.2] - 2026-05-13

### Fixed

- Default `KENTIK_ONBOARDER_API_ROOT` corrected from `https://api.kentik.com` to `https://grpc.api.kentik.com`. The v202504beta2 device endpoints are only served from the gRPC host; the previous default produced HTML 404s and `failedDevices` responses with no per-device error.
- Device-create payload now includes `minimizeSnmp: true`. The v202504beta2 API rejects `device_type=router` payloads without `minimize_snmp` set, but the batch endpoint hides the validation error and returns 200 OK with every device in `failedDevices`. Devices were never created and operators had no log signal as to why.

### Added

- When `batch_create` reports any failed devices, the onboarder now retries one of them through the singular `/device` endpoint and logs the API's structured error message (e.g. `_exceeds_plan_limits`, `Internal IP (...) Already Exists`, `minimize_snmp ... is required`). This turns silent batch failures into actionable log lines.
- 401/403 errors during normal cycles now include a credential-check hint pointing at `KENTIK_API_EMAIL` / `KENTIK_API_TOKEN`.

---

## [1.1.1] - 2026-05-12

### Changed

- Installers (DEB postinst, RPM `%post`, manual `install-kentik-device-onboarder.sh`) now run a backwards-compatible config migration on every install/upgrade: missing `KENTIK_ONBOARDER_DNS_SERVER=auto`, `KENTIK_ONBOARDER_DNS_TIMEOUT=2s`, `KENTIK_ONBOARDER_DNS_CACHE_TTL=1h`, and `KENTIK_ONBOARDER_DNS_NEGATIVE_CACHE_TTL=5m` lines are appended to existing `onboarder.env` files. Existing values are never touched, so user customizations are preserved.
- Installer `User-Agent` bumped to `kentik-device-onboarder-installer/1.1.0`.

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
