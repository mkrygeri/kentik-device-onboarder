# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- `Makefile` with `all`, `deb`, `rpm`, `native-deb`, `native-rpm`, `docker`, `docker-push`, `lint`, and `clean` targets.
