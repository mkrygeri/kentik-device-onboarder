# kentik-device-onboarder

A lightweight Python daemon that continuously monitors a Kentik kproxy healthcheck socket, discovers unregistered flow-sending devices, and automatically registers (onboards) them with the [Kentik API](https://kb.kentik.com/v4/Cb02.htm). It runs as a systemd service and is distributed as native Linux packages for both Debian/Ubuntu (`.deb`) and Red Hat/Rocky/Alma Linux (`.rpm`).

---

## Table of Contents

- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
  - [Debian / Ubuntu (DEB)](#debian--ubuntu-deb)
  - [Red Hat / Rocky / Alma Linux (RPM)](#red-hat--rocky--alma-linux-rpm)
  - [Manual installation](#manual-installation)
- [Configuration](#configuration)
- [Service management](#service-management)
- [CLI reference](#cli-reference)
- [Building packages](#building-packages)
  - [Quick build with fpm](#quick-build-with-fpm)
  - [Native DEB build](#native-deb-build)
  - [Native RPM build](#native-rpm-build)
- [GitHub Actions Artifacts](#github-actions-artifacts)
- [Docker](#docker)
  - [Build the image](#build-the-image)
  - [Run with Docker](#run-with-docker)
  - [Run with Docker Compose](#run-with-docker-compose)
  - [Push to a registry](#push-to-a-registry)
- [Project layout](#project-layout)
- [Contributing](#contributing)
- [License](#license)

---

## How It Works

```
flow-pak healthcheck socket (127.0.0.1:9996)
         │
         │  TCP read → plain-text "* Unregistered: <ip>" lines
         ▼
kentik-device-onboarder
  1. Parse unregistered IPs from healthcheck response
  2. Reverse-DNS lookup each IP for a human-readable device name
  3. POST /device/v202504beta2/device/batch_create to the Kentik API
  4. Persist per-device retry state to a JSON file
  5. Sleep for poll-interval, then repeat
```

Key resilience features:
- **Per-device exponential back-off** — failed devices are retried with increasing delays (default: 5 min base, 2 h max).
- **Global back-off** — API or healthcheck failures pause all operations briefly (default: 1 min base, 30 min max).
- **Success cooldown** — successfully onboarded devices are not re-submitted for 24 hours.
- **Client-side rate limiting** — limits batch-create requests to 12 per minute with a burst of 2.
- **Bounded reverse-DNS lookups** — each PTR lookup has a hard timeout (default 2 s) and results are cached, so a slow or broken DNS server cannot stall the onboarder cycle.
- **Optional explicit DNS resolver** — set `KENTIK_ONBOARDER_DNS_SERVER` to bypass the system resolver and query a specific server (e.g. `169.254.169.254` on GCP, see [Cloud-specific notes](#cloud-specific-notes)).
- **Sanitized device names** — PTR results are normalized to the lowercase `[a-z0-9._-]` character set Kentik accepts, so unusual hostnames don't get permanently rejected.
- **`--verify` self-test** — validate healthcheck reachability, API DNS, API authentication, and reverse DNS for sample IPs without making any changes.
- **Graceful shutdown** — handles SIGTERM / SIGINT cleanly and saves state before exit.

---

### Cloud-specific notes

Many cloud container images ship with a minimal `/etc/resolv.conf` that does not point at the cloud-provided resolver — the only resolver that can answer PTR queries for internal/private IP ranges. The symptom is `socket.gaierror: [Errno -2] Name or service not known` for every internal IP, and devices fall back to being onboarded by raw IP.

The simplest fix is:

```bash
KENTIK_ONBOARDER_DNS_SERVER=auto
```

in `/etc/kentik-device-onboarder/onboarder.env` (or `--dns-server auto`). At startup the onboarder probes the link-local metadata endpoints and picks the right resolver:

| Cloud | Detected via                                              | DNS server used    |
|-------|-----------------------------------------------------------|--------------------|
| GCE   | `GET http://169.254.169.254/computeMetadata/v1/` + `Metadata-Flavor: Google` | `169.254.169.254`  |
| Azure | `GET http://169.254.169.254/metadata/instance` + `Metadata: true`            | `168.63.129.16`    |
| AWS   | `PUT http://169.254.169.254/latest/api/token` (IMDSv2)    | `169.254.169.253`  |

On non-cloud hosts every probe fails fast (1.5 s timeout each) and the onboarder uses the system resolver. You can also pin a specific server, e.g. `KENTIK_ONBOARDER_DNS_SERVER=169.254.169.254`. Run `kentik_device_onboarder.py --verify` to confirm the configuration works end-to-end before restarting the service.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python ≥ 3.9 | Standard library only — no third-party packages |
| systemd | For service management |
| Kentik account | With API credentials (email + API token) |
| Kentik flow-pak | Running and exposing the healthcheck socket |

---

## Installation

### Debian / Ubuntu (DEB)

Download the latest `.deb` from the [Releases](https://github.com/kentik/kentik-device-onboarder/releases) page, then (recommended unattended form — pass credentials through the env so the postinst can populate `onboarder.env` automatically):

```bash
KENTIK_API_EMAIL=you@example.com KENTIK_API_TOKEN=… \
  sudo -E apt install ./kentik-device-onboarder_1.1.3_all.deb
```

Without the env vars the package still installs cleanly; you just need to edit `/etc/kentik-device-onboarder/onboarder.env` afterwards.

If you previously built a package that fails during unpack with an error like
`unable to open '/lib/systemd/system/kentik-device-onboarder.service.dpkg-new'`,
rebuild the package from the latest source and reinstall:

```bash
make deb
sudo apt install ./dist/kentik-device-onboarder_1.1.3_all.deb
```

### Red Hat / Rocky / Alma Linux (RPM)

Download the latest `.rpm` from the [Releases](https://github.com/kentik/kentik-device-onboarder/releases) page, then:

```bash
KENTIK_API_EMAIL=you@example.com KENTIK_API_TOKEN=… \
  sudo -E dnf install ./kentik-device-onboarder-1.1.3-1.noarch.rpm
```

Both packages:

1. Create a `kentik-onboarder` system user and group.
2. Install the daemon to `/opt/kentik-device-onboarder/`.
3. Install the systemd unit to `/usr/lib/systemd/system/`.
4. Create `/etc/kentik-device-onboarder/onboarder.env` from the bundled example (if not already present).
5. Populate `KENTIK_API_EMAIL` and `KENTIK_API_TOKEN` in the new config file. Sources, in order of precedence:
   1. The installer's own environment (`KENTIK_API_EMAIL` / `KENTIK_API_TOKEN`) — recommended for unattended installs and the only path that works under the new Kentik universal agent (`kagent`).
   2. The newest legacy `kproxy` process environment (`/proc/$(pgrep -n kproxy)/environ`).
   3. None — leaves the placeholder values in place and logs an actionable hint pointing at the env vars and the config path.
6. Query the Kentik plans API (`/plans/v202501alpha1`) with those credentials, pick the `flowpak` plan with the highest `maxFps`, and set `KENTIK_ONBOARDER_FLOWPAK_ID` automatically.
7. Enable the service (not started automatically).

If the plan API is unreachable, the installer prompts for a default flowpak plan ID during interactive installs.
For non-interactive installs (for example unattended package upgrades), it leaves the current flowpak ID unchanged.

### Credential auto-population source

The installer uses the same method below to read credentials from `kproxy`:

```bash
sudo cat /proc/$(pgrep -n kproxy)/environ | tr '\0' '\n' | grep KENTIK
```

Expected keys:

- `KENTIK_API_EMAIL`
- `KENTIK_API_TOKEN`

This auto-population is only applied when `onboarder.env` is created for the first time.

`KENTIK_ONBOARDER_FLOWPAK_ID` is refreshed on installs and upgrades using the plan API logic above.

### Manual installation

```bash
sudo bash install-kentik-device-onboarder.sh
```

The script honours the following environment variables for path overrides:

| Variable | Default |
|---|---|
| `INSTALL_DIR` | `/opt/kentik-device-onboarder` |
| `CONFIG_DIR` | `/etc/kentik-device-onboarder` |
| `STATE_DIR` | `/var/lib/kentik-device-onboarder` |
| `PYTHON_BIN` | `/usr/bin/python3` |

---

## Configuration

All settings are read from `/etc/kentik-device-onboarder/onboarder.env`. A commented example is installed automatically:

```ini
# Required
KENTIK_API_EMAIL=you@example.com
KENTIK_API_TOKEN=your-api-token-here
KENTIK_ONBOARDER_FLOWPAK_ID=12345

# Optional — shown with their defaults
KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS=127.0.0.1:9996
KENTIK_ONBOARDER_POLL_INTERVAL=5m
KENTIK_ONBOARDER_API_ROOT=https://api.kentik.com
KENTIK_ONBOARDER_LOG_LEVEL=INFO
KENTIK_ONBOARDER_STATE_FILE=/var/lib/kentik-device-onboarder/state.json
```

See [`kentik-device-onboarder.env.example`](kentik-device-onboarder.env.example) for the full list of supported variables with descriptions.

> **Security note:** The configuration file is owned by `root:kentik-onboarder` with mode `0640`. It contains API credentials — do not make it world-readable.

---

## Service management

```bash
# Start after editing the configuration
sudo systemctl start kentik-device-onboarder

# Follow live logs
sudo journalctl -u kentik-device-onboarder -f

# Restart after a config change
sudo systemctl restart kentik-device-onboarder

# Stop
sudo systemctl stop kentik-device-onboarder

# Check status
sudo systemctl status kentik-device-onboarder
```

---

## CLI reference

The daemon can also be run directly (useful for one-shot dry runs):

```
usage: kentik_device_onboarder.py [-h]
  [--flowpak-id INT]
  [--api-email EMAIL]
  [--api-token TOKEN]
  [--healthcheck-address HOST:PORT]
  [--poll-interval DURATION]
  [--healthcheck-timeout DURATION]
  [--api-root URL]
  [--request-timeout DURATION]
  [--batch-size INT]
  [--success-cooldown DURATION]
  [--backoff-base DURATION]
  [--backoff-max DURATION]
  [--global-backoff-base DURATION]
  [--global-backoff-max DURATION]
  [--api-rate-per-minute FLOAT]
  [--api-rate-burst INT]
  [--state-file PATH]
  [--log-level LEVEL]
  [--dns-timeout DURATION]
  [--dns-cache-ttl DURATION]
  [--dns-negative-cache-ttl DURATION]
  [--dns-server IP[:PORT]|auto]
  [--run-once]
  [--dry-run]
  [--verify]
```

Duration arguments accept bare seconds (`300`), or a value with a single-character suffix: `s` (seconds), `m` (minutes), `h` (hours). Example: `--poll-interval 5m`.

**Useful one-liners:**

```bash
# Validate config end-to-end without making any changes (read-only)
sudo -u kentik-onboarder python3 /opt/kentik-device-onboarder/kentik_device_onboarder.py --verify

# Dry-run to see what would be onboarded right now
sudo -u kentik-onboarder python3 /opt/kentik-device-onboarder/kentik_device_onboarder.py \
  --run-once --dry-run

# Single cycle with debug logging
sudo -u kentik-onboarder python3 /opt/kentik-device-onboarder/kentik_device_onboarder.py \
  --run-once --log-level DEBUG
```

---

## Docker

The container image is a 2-stage build (`python:3.12-slim` runtime) running as the unprivileged `kentik-onboarder` user. State is persisted to a named volume.

### Build the image

```bash
make docker
# or directly:
docker build -t kentik-device-onboarder:1.1.3 -t kentik-device-onboarder:latest .
```

### Run with Docker

```bash
# Create .env from the example and fill in your credentials
cp kentik-device-onboarder.env.example .env
$EDITOR .env

docker run -d \
  --name kentik-device-onboarder \
  --restart unless-stopped \
  --network host \
  --env-file .env \
  -v onboarder-state:/var/lib/kentik-device-onboarder \
  kentik-device-onboarder:latest
```

> **Network mode:** The daemon connects to the flow-pak healthcheck socket (default `127.0.0.1:9996`). Using `--network host` is the simplest approach when the flow-pak runs on the same host. Alternatively, point `KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS` at a reachable container name or IP and use bridge networking.

### Run with Docker Compose

```bash
cp kentik-device-onboarder.env.example .env
$EDITOR .env   # set KENTIK_API_EMAIL, KENTIK_API_TOKEN, KENTIK_ONBOARDER_FLOWPAK_ID

docker compose up -d
docker compose logs -f
```

### Push to a registry

```bash
make docker-push REGISTRY=registry.example.com/myorg
```

---

## Building packages

### Quick build with fpm

[fpm](https://fpm.readthedocs.io/) is the recommended way to build both package formats from any Linux host.

**Install fpm:**
```bash
gem install fpm          # requires Ruby ≥ 2.5
# On Debian/Ubuntu also install:
sudo apt install rpm     # to build RPMs on a Debian host
```

**Build both packages:**
```bash
make all
# or directly:
bash packaging/build-packages.sh        # deb + rpm
bash packaging/build-packages.sh deb    # deb only
bash packaging/build-packages.sh rpm    # rpm only
```

Packages are written to `dist/`.

---

### Native DEB build

Requires `build-essential`, `debhelper` (≥ 13), and `dh-python`:

```bash
sudo apt install build-essential debhelper dh-python
make native-deb
```

The `debian/` directory lives under `packaging/debian/`. The `packaging/debian/rules` file builds from the repo root so no source tarball is needed.

---

### Native RPM build

Requires `rpm-build`:

```bash
sudo dnf install rpm-build
make native-rpm
```

The spec file is at `packaging/rpm/kentik-device-onboarder.spec` and references source files directly from the repository root.

---

## GitHub Actions Artifacts

This repository includes a workflow at `.github/workflows/build-artifacts.yml` that runs on pushes to `main`, pull requests, tags like `v*`, and manual dispatch.

The workflow builds:

1. Linux packages (`.deb` and `.rpm`) via `make all`
2. A Docker image tarball (`kentik-device-onboarder-image.tar`)

You can download artifacts from the run summary in the GitHub Actions UI.

For tag pushes matching `v*` (for example `v1.1.0`), the workflow also publishes `.deb` and `.rpm` files to the corresponding GitHub Release automatically.

Example release flow:

```bash
git tag v1.1.3
git push origin v1.1.3
```

---

## Project layout

```
kentik-device-onboarder/
├── kentik_device_onboarder.py          # The daemon (single-file, stdlib-only)
├── kentik-device-onboarder.service     # systemd unit file
├── kentik-device-onboarder.env.example # Annotated configuration template
├── install-kentik-device-onboarder.sh  # Manual installer (no package manager)
├── Dockerfile                          # 2-stage Docker image (python:3.12-slim)
├── docker-compose.yml                  # Compose file for container deployment
├── Makefile                            # Build targets: all, deb, rpm, docker, clean, lint
├── VERSION                             # Single source of truth for the version
├── .gitignore
├── CHANGELOG.md
└── packaging/
    ├── build-packages.sh               # fpm-based cross-distro build script
    ├── postinst                        # Shared post-install hook (fpm)
    ├── prerm                           # Shared pre-remove hook (fpm)
    ├── debian/                         # Native Debian packaging
    │   ├── changelog
    │   ├── compat
    │   ├── control
    │   ├── copyright
    │   ├── postinst
    │   ├── prerm
    │   ├── rules
    │   └── source/
    │       └── format
    └── rpm/
        └── kentik-device-onboarder.spec  # Native RPM spec
```

---

## Contributing

1. Fork the repository and create a feature branch.
2. Make your changes and ensure `make lint` passes.
3. Update `CHANGELOG.md` and bump `VERSION` if appropriate.
4. Open a pull request with a clear description.

---

## License

[Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0) — © 2026 Kentik Technologies, Inc.
