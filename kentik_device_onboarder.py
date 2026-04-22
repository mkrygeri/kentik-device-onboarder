#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib import error, request


MAX_BATCH_SIZE = 100
DEFAULT_POLL_INTERVAL = 300.0
DEFAULT_HEALTHCHECK_TIMEOUT = 5.0
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_SUCCESS_COOLDOWN = 86400.0
DEFAULT_BACKOFF_BASE = 300.0
DEFAULT_BACKOFF_MAX = 7200.0
DEFAULT_GLOBAL_BACKOFF_BASE = 60.0
DEFAULT_GLOBAL_BACKOFF_MAX = 1800.0
DEFAULT_API_RATE_PER_MINUTE = 12.0
DEFAULT_API_RATE_BURST = 2
DEFAULT_STATE_FILE = "/var/lib/kentik-device-onboarder/state.json"

LOGGER = logging.getLogger("kentik-device-onboarder")
STOP_REQUESTED = False


class OnboarderError(Exception):
    pass


class RateLimitedError(OnboarderError):
    def __init__(self, retry_after: float | None, message: str) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientAPIError(OnboarderError):
    pass


@dataclass
class Config:
    flowpak_id: int
    api_email: str
    api_token: str
    healthcheck_address: str = "127.0.0.1:9996"
    poll_interval: float = DEFAULT_POLL_INTERVAL
    healthcheck_timeout: float = DEFAULT_HEALTHCHECK_TIMEOUT
    api_root: str = "https://api.kentik.com"
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    batch_size: int = MAX_BATCH_SIZE
    success_cooldown: float = DEFAULT_SUCCESS_COOLDOWN
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_max: float = DEFAULT_BACKOFF_MAX
    global_backoff_base: float = DEFAULT_GLOBAL_BACKOFF_BASE
    global_backoff_max: float = DEFAULT_GLOBAL_BACKOFF_MAX
    api_rate_per_minute: float = DEFAULT_API_RATE_PER_MINUTE
    api_rate_burst: int = DEFAULT_API_RATE_BURST
    state_file: str = DEFAULT_STATE_FILE
    log_level: str = "INFO"
    run_once: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        if self.flowpak_id <= 0:
            raise ValueError("flowpak id must be greater than zero")
        if not self.api_email.strip():
            raise ValueError("api email must not be empty")
        if not self.api_token.strip():
            raise ValueError("api token must not be empty")
        if self.poll_interval <= 0:
            raise ValueError("poll interval must be greater than zero")
        if self.healthcheck_timeout <= 0:
            raise ValueError("healthcheck timeout must be greater than zero")
        if self.request_timeout <= 0:
            raise ValueError("request timeout must be greater than zero")
        if self.batch_size <= 0 or self.batch_size > MAX_BATCH_SIZE:
            raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
        if self.success_cooldown < 0:
            raise ValueError("success cooldown must not be negative")
        if self.backoff_base <= 0 or self.backoff_max < self.backoff_base:
            raise ValueError("backoff values must be positive and max must be >= base")
        if self.global_backoff_base <= 0 or self.global_backoff_max < self.global_backoff_base:
            raise ValueError("global backoff values must be positive and max must be >= base")
        if self.api_rate_per_minute <= 0:
            raise ValueError("api rate per minute must be greater than zero")
        if self.api_rate_burst <= 0:
            raise ValueError("api rate burst must be greater than zero")
        parse_host_port(self.healthcheck_address)


@dataclass
class DeviceAttemptState:
    failures: int = 0
    next_attempt: float = 0.0
    last_success: float = 0.0


@dataclass
class AttemptTracker:
    state_file: Path
    states: dict[str, DeviceAttemptState] = field(default_factory=dict)

    def load(self) -> None:
        if not self.state_file.exists():
            return

        raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        states = raw.get("states", {})
        for ip_address, item in states.items():
            self.states[ip_address] = DeviceAttemptState(
                failures=int(item.get("failures", 0)),
                next_attempt=float(item.get("next_attempt", 0.0)),
                last_success=float(item.get("last_success", 0.0)),
            )

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "states": {
                ip_address: {
                    "failures": state.failures,
                    "next_attempt": state.next_attempt,
                    "last_success": state.last_success,
                }
                for ip_address, state in self.states.items()
            }
        }
        temp_path = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.state_file)

    def can_attempt(self, ip_address: str, now: float) -> bool:
        state = self.states.get(ip_address)
        return state is None or now >= state.next_attempt

    def mark_success(self, ip_address: str, now: float, cooldown: float) -> None:
        self.states[ip_address] = DeviceAttemptState(failures=0, next_attempt=now + cooldown, last_success=now)

    def mark_failure(self, ip_address: str, now: float, base_delay: float, max_delay: float) -> float:
        state = self.states.get(ip_address, DeviceAttemptState())
        state.failures += 1
        delay = min(max_delay, base_delay * (2 ** (state.failures - 1)))
        state.next_attempt = now + delay
        self.states[ip_address] = state
        return delay


class RateLimiter:
    def __init__(self, rate_per_minute: float, burst: int) -> None:
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.refill_rate = rate_per_minute / 60.0
        self.updated_at = time.monotonic()

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self.updated_at
            self.updated_at = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            time.sleep((1.0 - self.tokens) / self.refill_rate)


class KentikClient:
    def __init__(self, config: Config) -> None:
        self.api_root = config.api_root.rstrip("/")
        self.api_email = config.api_email.strip()
        self.api_token = config.api_token.strip()
        self.request_timeout = config.request_timeout
        self.rate_limiter = RateLimiter(config.api_rate_per_minute, config.api_rate_burst)
        
        # Create SSL context with proper certificate verification
        ssl_context = ssl.create_default_context()
        self.opener = request.build_opener(request.HTTPSHandler(context=ssl_context))

    def create_devices(self, devices: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
        self.rate_limiter.wait()
        body = json.dumps({"devices": devices}).encode("utf-8")

        # Match the Go client request headers as closely as possible.
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "kentik-device-onboarder/1.0",
            "X-CH-Auth-Email": self.api_email,
            "X-CH-Auth-API-Token": self.api_token,
        }
        
        req = request.Request(
            url=f"{self.api_root}/device/v202504beta2/device/batch_create",
            data=body,
            method="POST",
            headers=headers,
        )

        try:
            with self.opener.open(req, timeout=self.request_timeout) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                created_names = {
                    (item.get("deviceName") or item.get("device_name") or "")
                    for item in payload.get("devices", [])
                    if item.get("deviceName") or item.get("device_name")
                }
                failed_names = set(payload.get("failedDevices") or payload.get("failed_devices") or [])
                return created_names, failed_names
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            LOGGER.debug("request headers: %s", {k: v for k, v in req.headers.items()})
            LOGGER.debug("request body size: %d bytes", len(body))
            LOGGER.debug("response status: %d", exc.code)
            LOGGER.debug("response headers: %s", dict(exc.headers))
            LOGGER.debug("response body: %s", response_body[:500])
            if exc.code == 429:
                retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                raise RateLimitedError(retry_after, f"kentik api rate limited the request: {response_body.strip()}") from exc
            if exc.code in {500, 502, 503, 504}:
                raise TransientAPIError(f"kentik api transient failure {exc.code}: {response_body.strip()}") from exc
            raise OnboarderError(f"kentik api request failed with status {exc.code}: {response_body.strip()}") from exc
        except error.URLError as exc:
            raise TransientAPIError(f"kentik api request failed: {exc.reason}") from exc


class DeviceOnboarder:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.tracker = AttemptTracker(Path(config.state_file))
        self.client = KentikClient(config)
        self.global_failures = 0
        self.global_pause_until = 0.0
        self.tracker.load()

    def run_forever(self) -> int:
        while not STOP_REQUESTED:
            cycle_started = time.time()
            self.run_cycle(cycle_started)
            if self.config.run_once:
                break
            next_wakeup = max(cycle_started + self.config.poll_interval, self.global_pause_until)
            wait_seconds = max(0.0, next_wakeup - time.time())
            if STOP_REQUESTED:
                break
            LOGGER.debug("sleeping for %.1f seconds before next cycle", wait_seconds)
            interruptible_sleep(wait_seconds)

        self.tracker.save()
        return 0

    def run_cycle(self, cycle_started: float) -> None:
        try:
            if cycle_started < self.global_pause_until:
                LOGGER.warning("skipping cycle during global backoff until %s", format_timestamp(self.global_pause_until))
                return

            raw = read_healthcheck(self.config.healthcheck_address, self.config.healthcheck_timeout)
            unregistered_ips = parse_unregistered_devices(raw)
            if not unregistered_ips:
                LOGGER.info("no unregistered devices found")
                self.global_failures = 0
                return

            ready_ips = [ip_address for ip_address in unregistered_ips if self.tracker.can_attempt(ip_address, cycle_started)]
            if not ready_ips:
                LOGGER.info("no unregistered devices are ready for retry")
                self.global_failures = 0
                return

            devices = build_device_payloads(ready_ips, self.config.flowpak_id)
            self.process_batches(devices, cycle_started)
            self.global_failures = 0
        except RateLimitedError as exc:
            self.global_failures += 1
            delay = exc.retry_after or self.compute_global_backoff()
            self.global_pause_until = time.time() + delay
            LOGGER.warning("rate limited by kentik api, backing off for %.1f seconds: %s", delay, exc)
        except (TransientAPIError, OSError, TimeoutError, ValueError, OnboarderError) as exc:
            self.global_failures += 1
            delay = self.compute_global_backoff()
            self.global_pause_until = time.time() + delay
            LOGGER.error("cycle failed, backing off for %.1f seconds: %s", delay, exc)
        finally:
            self.tracker.save()

    def process_batches(self, devices: list[dict[str, Any]], now: float) -> None:
        for batch in chunked(devices, self.config.batch_size):
            if self.config.dry_run:
                LOGGER.info("dry run enabled, would onboard %d devices", len(batch))
                for item in batch:
                    LOGGER.info("would onboard device_name=%s sending_ip=%s", item["deviceName"], item["sendingIps"][0])
                continue

            created_names, failed_names = self.client.create_devices(batch)
            name_to_ip = {item["deviceName"]: item["sendingIps"][0] for item in batch}

            onboarded_count = 0
            for device_name, ip_address in name_to_ip.items():
                if device_name in failed_names:
                    delay = self.tracker.mark_failure(ip_address, now, self.config.backoff_base, self.config.backoff_max)
                    LOGGER.warning(
                        "device onboarding failed for device_name=%s sending_ip=%s, retrying after %.1f seconds",
                        device_name,
                        ip_address,
                        delay,
                    )
                    continue

                self.tracker.mark_success(ip_address, now, self.config.success_cooldown)
                onboarded_count += 1
                if device_name not in created_names:
                    LOGGER.info("device_name=%s sending_ip=%s accepted without explicit create response entry", device_name, ip_address)

            if onboarded_count > 0:
                LOGGER.info("onboarded %d devices", onboarded_count)

    def compute_global_backoff(self) -> float:
        exponent = max(0, self.global_failures - 1)
        return min(self.config.global_backoff_max, self.config.global_backoff_base * (2 ** exponent))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="continuously onboard unregistered devices into kentik")
    parser.add_argument("--flowpak-id", type=int, default=env_int("KENTIK_ONBOARDER_FLOWPAK_ID"), help="kentik plan/flowpak id for new devices")
    parser.add_argument("--api-email", default=os.getenv("KENTIK_API_EMAIL"), help="kentik api email")
    parser.add_argument("--api-token", default=os.getenv("KENTIK_API_TOKEN"), help="kentik api token")
    parser.add_argument("--healthcheck-address", default=os.getenv("KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS", "127.0.0.1:9996"), help="tcp host:port for the healthcheck socket")
    parser.add_argument("--poll-interval", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_POLL_INTERVAL", "5m")), help="poll interval, for example 300, 30s, 5m, 1h")
    parser.add_argument("--healthcheck-timeout", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_HEALTHCHECK_TIMEOUT", "5s")), help="healthcheck socket timeout")
    parser.add_argument("--api-root", default=os.getenv("KENTIK_ONBOARDER_API_ROOT", "https://api.kentik.com"), help="kentik api root")
    parser.add_argument("--request-timeout", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_REQUEST_TIMEOUT", "15s")), help="kentik api request timeout")
    parser.add_argument("--batch-size", type=int, default=env_int("KENTIK_ONBOARDER_BATCH_SIZE", MAX_BATCH_SIZE), help="maximum devices per batch create request")
    parser.add_argument("--success-cooldown", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_SUCCESS_COOLDOWN", "24h")), help="cooldown after a successful onboarding")
    parser.add_argument("--backoff-base", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_BACKOFF_BASE", "5m")), help="per-device backoff base delay")
    parser.add_argument("--backoff-max", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_BACKOFF_MAX", "2h")), help="maximum per-device backoff delay")
    parser.add_argument("--global-backoff-base", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_GLOBAL_BACKOFF_BASE", "1m")), help="global backoff base delay for healthcheck or api failures")
    parser.add_argument("--global-backoff-max", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_GLOBAL_BACKOFF_MAX", "30m")), help="maximum global backoff delay")
    parser.add_argument("--api-rate-per-minute", type=float, default=float(os.getenv("KENTIK_ONBOARDER_API_RATE_PER_MINUTE", DEFAULT_API_RATE_PER_MINUTE)), help="local client-side rate limit for batch requests")
    parser.add_argument("--api-rate-burst", type=int, default=env_int("KENTIK_ONBOARDER_API_RATE_BURST", DEFAULT_API_RATE_BURST), help="local client-side burst size for batch requests")
    parser.add_argument("--state-file", default=os.getenv("KENTIK_ONBOARDER_STATE_FILE", DEFAULT_STATE_FILE), help="json file used to persist retry state")
    parser.add_argument("--log-level", default=os.getenv("KENTIK_ONBOARDER_LOG_LEVEL", "INFO"), help="python logging level")
    parser.add_argument("--run-once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="log the devices that would be onboarded without calling kentik")
    return parser


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        flowpak_id=args.flowpak_id,
        api_email=args.api_email or "",
        api_token=args.api_token or "",
        healthcheck_address=args.healthcheck_address,
        poll_interval=args.poll_interval,
        healthcheck_timeout=args.healthcheck_timeout,
        api_root=args.api_root,
        request_timeout=args.request_timeout,
        batch_size=args.batch_size,
        success_cooldown=args.success_cooldown,
        backoff_base=args.backoff_base,
        backoff_max=args.backoff_max,
        global_backoff_base=args.global_backoff_base,
        global_backoff_max=args.global_backoff_max,
        api_rate_per_minute=args.api_rate_per_minute,
        api_rate_burst=args.api_rate_burst,
        state_file=args.state_file,
        log_level=args.log_level,
        run_once=args.run_once,
        dry_run=args.dry_run,
    )


def configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def install_signal_handlers() -> None:
    def _handle_signal(signum: int, _frame: Any) -> None:
        global STOP_REQUESTED
        STOP_REQUESTED = True
        LOGGER.info("received signal %s, shutting down", signum)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def read_healthcheck(address: str, timeout_seconds: float) -> str:
    host, port = parse_host_port(address)
    chunks: list[bytes] = []
    with socket.create_connection((host, port), timeout=timeout_seconds) as conn:
        conn.settimeout(timeout_seconds)
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        while True:
            data = conn.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode("utf-8", errors="replace")


def parse_unregistered_devices(raw: str) -> list[str]:
    devices: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("* Unregistered:"):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        candidate = parts[2].rstrip(".")
        normalized = str(ipaddress.ip_address(candidate))
        if normalized not in seen:
            seen.add(normalized)
            devices.append(normalized)
    return devices


def build_device_payloads(ip_addresses: list[str], flowpak_id: int) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}
    for ip_address in ip_addresses:
        device_name = lookup_device_name(ip_address)
        key = device_name.lower()
        used_count = used_names.get(key, 0)
        used_names[key] = used_count + 1
        if used_count > 0:
            device_name = f"{device_name}-{sanitize_ip_for_name(ip_address)}"
        devices.append(
            {
                "deviceName": device_name,
                "deviceSubtype": "router",
                "sendingIps": [ip_address],
                "planId": flowpak_id,
                "deviceBgpType": "none",
                "deviceSampleRate": 1000,
            }
        )
    return devices


def lookup_device_name(ip_address: str) -> str:
    try:
        hostname, _, _ = socket.gethostbyaddr(ip_address)
    except (socket.herror, socket.gaierror, TimeoutError, OSError):
        return ip_address

    normalized = normalize_ptr_name(hostname)
    return normalized or ip_address


def normalize_ptr_name(hostname: str) -> str:
    stripped = hostname.strip().rstrip(".")
    lowered = stripped.lower()
    if not stripped:
        return ""
    if lowered.endswith(".in-addr.arpa") or lowered.endswith(".ip6.arpa"):
        return ""
    return stripped


def sanitize_ip_for_name(ip_address: str) -> str:
    return ip_address.replace(".", "-").replace(":", "-")


def parse_host_port(address: str) -> tuple[str, int]:
    if address.startswith("["):
        host, remainder = address[1:].split("]", 1)
        if not remainder.startswith(":"):
            raise ValueError(f"invalid healthcheck address: {address}")
        return host, int(remainder[1:])
    host, port_text = address.rsplit(":", 1)
    return host, int(port_text)


def parse_duration(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    text = value.strip().lower()
    if not text:
        raise ValueError("duration must not be empty")
    if text[-1].isdigit():
        return float(text)

    suffixes = {
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
    }
    suffix = text[-1]
    if suffix not in suffixes:
        raise ValueError(f"unsupported duration suffix in {value!r}")
    return float(text[:-1]) * suffixes[suffix]


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def chunked(items: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def interruptible_sleep(seconds: float) -> None:
    end_time = time.time() + seconds
    while not STOP_REQUESTED and time.time() < end_time:
        time.sleep(min(1.0, end_time - time.time()))


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    config = build_config(args)
    config.validate()

    configure_logging(config.log_level)
    install_signal_handlers()

    LOGGER.info("starting kentik device onboarder")
    LOGGER.info(
        "configured healthcheck=%s poll_interval=%.1fs batch_size=%d dry_run=%s run_once=%s",
        config.healthcheck_address,
        config.poll_interval,
        config.batch_size,
        config.dry_run,
        config.run_once,
    )

    onboarder = DeviceOnboarder(config)
    return onboarder.run_forever()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOGGER.info("interrupted")
        sys.exit(0)
    except Exception as exc:
        print(f"fatal error: {exc}", file=sys.stderr)
        sys.exit(1)