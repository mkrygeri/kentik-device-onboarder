#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib import error, parse as urlparse, request


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
DEFAULT_DNS_TIMEOUT = 2.0
DEFAULT_DNS_CACHE_TTL = 3600.0
DEFAULT_DNS_NEGATIVE_CACHE_TTL = 300.0
DEFAULT_DNS_SERVER = ""
MAX_DEVICE_NAME_LENGTH = 60
DEVICE_NAME_INVALID_CHARS = re.compile(r"[^a-z0-9._-]+")

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


class DNSLookupError(OnboarderError):
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
    dns_timeout: float = DEFAULT_DNS_TIMEOUT
    dns_cache_ttl: float = DEFAULT_DNS_CACHE_TTL
    dns_negative_cache_ttl: float = DEFAULT_DNS_NEGATIVE_CACHE_TTL
    dns_server: str = DEFAULT_DNS_SERVER
    verify: bool = False

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
        if self.dns_timeout <= 0:
            raise ValueError("dns timeout must be greater than zero")
        if self.dns_cache_ttl < 0 or self.dns_negative_cache_ttl < 0:
            raise ValueError("dns cache ttls must not be negative")
        if self.dns_server and self.dns_server.strip().lower() != "auto":
            # Accept either "host" or "host:port"; validate the host part.
            host, _ = parse_dns_server(self.dns_server)
            if not host:
                raise ValueError("dns server must not be empty when set")
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


class DNSResolver:
    """Reverse-DNS resolver with per-lookup timeout and result caching.

    The system resolver call (`socket.gethostbyaddr`) has no timeout argument,
    so a slow or unreachable DNS server can hang the onboarder cycle for many
    seconds per IP. Running the call on a daemon thread lets us bound the wait,
    and caching avoids repeating the same lookup on every poll.

    When ``server`` is set (e.g. ``169.254.169.254`` for the GCP internal
    metadata resolver) the resolver issues a UDP DNS PTR query directly to
    that server instead of using the system resolver. This is essential in
    environments where the container's /etc/resolv.conf does not point at the
    cloud-provided resolver that knows about private/internal IPs.
    """

    _MISS = object()

    def __init__(
        self,
        timeout: float,
        cache_ttl: float,
        negative_cache_ttl: float,
        server: str = "",
    ) -> None:
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.negative_cache_ttl = negative_cache_ttl
        self.server = server.strip()
        self._cache: dict[str, tuple[float, str | None, str | None]] = {}
        self._lock = threading.Lock()

    def reverse(self, ip_address: str) -> tuple[str | None, str | None]:
        """Return (hostname, error_message). hostname is None on failure."""
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(ip_address)
        if entry is not None:
            cached_at, hostname, err = entry
            ttl = self.cache_ttl if hostname else self.negative_cache_ttl
            if now - cached_at < ttl:
                return hostname, err

        hostname, err = self._reverse_with_timeout(ip_address)
        with self._lock:
            self._cache[ip_address] = (now, hostname, err)
        return hostname, err

    def _reverse_with_timeout(self, ip_address: str) -> tuple[str | None, str | None]:
        if self.server:
            try:
                return ptr_query(ip_address, self.server, self.timeout), None
            except DNSLookupError as exc:
                return None, str(exc)

        result: list[str | None] = [None]
        err: list[str | None] = [None]

        def worker() -> None:
            try:
                hostname, _, _ = socket.gethostbyaddr(ip_address)
                result[0] = hostname
            except (socket.herror, socket.gaierror) as exc:
                err[0] = str(exc)
            except OSError as exc:
                err[0] = f"resolver error: {exc}"

        thread = threading.Thread(target=worker, name=f"rdns-{ip_address}", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            return None, f"timed out after {self.timeout:.1f}s"
        return result[0], err[0]


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

    def _auth_headers(self) -> dict[str, str]:
        return {
            "User-Agent": "kentik-device-onboarder/1.1.1",
            "X-CH-Auth-Email": self.api_email,
            "X-CH-Auth-API-Token": self.api_token,
        }

    def ping(self) -> None:
        """Lightweight authenticated GET used by --verify to confirm credentials work."""
        self.rate_limiter.wait()
        headers = {"Accept": "application/json", **self._auth_headers()}
        req = request.Request(
            url=f"{self.api_root}/device/v202504beta2/devices?page_size=1",
            method="GET",
            headers=headers,
        )
        try:
            with self.opener.open(req, timeout=self.request_timeout) as response:
                response.read(1024)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:200]
            if exc.code in {401, 403}:
                raise OnboarderError(f"kentik api authentication failed ({exc.code}): {body.strip()}") from exc
            if exc.code == 404:
                # Endpoint variant not present; auth is implicitly fine if we got past 401.
                return
            if exc.code == 429:
                raise RateLimitedError(parse_retry_after(exc.headers.get("Retry-After")), f"rate limited: {body.strip()}") from exc
            if exc.code >= 500:
                raise TransientAPIError(f"kentik api transient failure {exc.code}: {body.strip()}") from exc
            raise OnboarderError(f"kentik api request failed with status {exc.code}: {body.strip()}") from exc
        except error.URLError as exc:
            raise TransientAPIError(f"kentik api request failed: {exc.reason}") from exc

    def create_devices(self, devices: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
        self.rate_limiter.wait()
        body = json.dumps({"devices": devices}).encode("utf-8")

        # Match the Go client request headers as closely as possible.
        headers = {
            "Content-Type": "application/json",
            **self._auth_headers(),
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
        self.resolver = DNSResolver(
            config.dns_timeout,
            config.dns_cache_ttl,
            config.dns_negative_cache_ttl,
            server=config.dns_server,
        )
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

            devices = build_device_payloads(ready_ips, self.config.flowpak_id, self.resolver)
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
    parser.add_argument("--dns-timeout", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_DNS_TIMEOUT", "2s")), help="per-lookup timeout for reverse DNS resolution")
    parser.add_argument("--dns-cache-ttl", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_DNS_CACHE_TTL", "1h")), help="cache lifetime for successful reverse DNS results")
    parser.add_argument("--dns-negative-cache-ttl", type=parse_duration, default=parse_duration(os.getenv("KENTIK_ONBOARDER_DNS_NEGATIVE_CACHE_TTL", "5m")), help="cache lifetime for failed reverse DNS results")
    parser.add_argument("--dns-server", default=os.getenv("KENTIK_ONBOARDER_DNS_SERVER", ""), help="explicit DNS resolver IP[:port] for reverse lookups, or 'auto' to detect cloud (e.g. GCE metadata server 169.254.169.254). Empty = use system resolver.")
    parser.add_argument("--run-once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="log the devices that would be onboarded without calling kentik")
    parser.add_argument("--verify", action="store_true", help="run a self-test (healthcheck reachability, DNS, API auth, sample reverse DNS) and exit")
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
        dns_timeout=args.dns_timeout,
        dns_cache_ttl=args.dns_cache_ttl,
        dns_negative_cache_ttl=args.dns_negative_cache_ttl,
        dns_server=args.dns_server,
        verify=args.verify,
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
    try:
        conn = socket.create_connection((host, port), timeout=timeout_seconds)
    except socket.gaierror as exc:
        raise TransientAPIError(
            f"healthcheck host {host!r} could not be resolved: {exc} (check KENTIK_ONBOARDER_HEALTHCHECK_ADDRESS)"
        ) from exc
    except OSError as exc:
        raise TransientAPIError(f"healthcheck connection to {host}:{port} failed: {exc}") from exc
    with conn:
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


def build_device_payloads(
    ip_addresses: list[str],
    flowpak_id: int,
    resolver: DNSResolver,
) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    used_names: dict[str, int] = {}
    for ip_address in ip_addresses:
        raw_name = lookup_device_name(ip_address, resolver)
        device_name = sanitize_device_name(raw_name, ip_address)
        used_count = used_names.get(device_name, 0)
        used_names[device_name] = used_count + 1
        if used_count > 0:
            device_name = sanitize_device_name(f"{device_name}-{sanitize_ip_for_name(ip_address)}", ip_address)
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


def lookup_device_name(ip_address: str, resolver: DNSResolver) -> str:
    hostname, err = resolver.reverse(ip_address)
    if hostname is None:
        if err:
            LOGGER.info("reverse DNS lookup for %s failed (%s); falling back to IP", ip_address, err)
        return ip_address
    normalized = normalize_ptr_name(hostname)
    if not normalized:
        LOGGER.debug("reverse DNS for %s returned non-usable name %r; falling back to IP", ip_address, hostname)
        return ip_address
    return normalized


def normalize_ptr_name(hostname: str) -> str:
    stripped = hostname.strip().rstrip(".")
    lowered = stripped.lower()
    if not stripped:
        return ""
    if lowered.endswith(".in-addr.arpa") or lowered.endswith(".ip6.arpa"):
        return ""
    return stripped


def sanitize_device_name(raw: str, fallback_ip: str) -> str:
    """Coerce a hostname into a Kentik-acceptable device name.

    Kentik device names must be lowercase and contain only letters, digits,
    dots, hyphens, and underscores. Anything else gets collapsed to '-' and
    leading/trailing punctuation is stripped. Falls back to the IP-derived
    name if nothing usable remains.
    """
    candidate = (raw or "").strip().lower()
    candidate = DEVICE_NAME_INVALID_CHARS.sub("-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate)
    candidate = candidate.strip("-.")
    if len(candidate) > MAX_DEVICE_NAME_LENGTH:
        candidate = candidate[:MAX_DEVICE_NAME_LENGTH].rstrip("-.")
    if not candidate:
        candidate = sanitize_ip_for_name(fallback_ip)
    return candidate


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


def parse_dns_server(value: str) -> tuple[str, int]:
    """Parse a DNS server spec like '169.254.169.254' or '8.8.8.8:53'."""
    text = (value or "").strip()
    if not text:
        return "", 53
    if text.startswith("["):
        host, remainder = text[1:].split("]", 1)
        port = int(remainder[1:]) if remainder.startswith(":") else 53
        return host, port
    if text.count(":") == 1:
        host, port_text = text.split(":", 1)
        return host, int(port_text)
    return text, 53


# Probe URLs/headers used to detect the cloud environment. Each metadata
# service only responds when the right header is present, which makes these
# reliable positive signals. GCE and AWS share the 169.254.169.254 endpoint
# but distinguish themselves via the Metadata-Flavor / X-aws-* headers.
GCE_METADATA_URL = "http://169.254.169.254/computeMetadata/v1/"
GCE_METADATA_HEADER = ("Metadata-Flavor", "Google")
GCE_METADATA_DNS = "169.254.169.254"

AWS_IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
AWS_IMDS_TOKEN_HEADER = ("X-aws-ec2-metadata-token-ttl-seconds", "60")
AWS_VPC_DNS = "169.254.169.253"  # Amazon-provided DNS (Route 53 Resolver link-local)

AZURE_IMDS_URL = "http://169.254.169.254/metadata/instance?api-version=2021-02-01"
AZURE_IMDS_HEADER = ("Metadata", "true")
AZURE_VNET_DNS = "168.63.129.16"  # Azure-provided DNS endpoint

DETECTION_TIMEOUT = 1.5


def detect_cloud_dns_server() -> tuple[str | None, str | None]:
    """Return ``(cloud_name, dns_server_ip)`` if a known cloud is detected.

    Probes are run in order GCE -> Azure -> AWS. They are short, link-local
    HTTP requests with a 1.5 s timeout, so it is safe to call this once at
    startup even on bare-metal hosts where every probe will fail fast.
    """
    if _probe_gce():
        return "GCE", GCE_METADATA_DNS
    if _probe_azure():
        return "Azure", AZURE_VNET_DNS
    if _probe_aws():
        return "AWS", AWS_VPC_DNS
    return None, None


def _probe_gce() -> bool:
    return _probe_http(GCE_METADATA_URL, dict([GCE_METADATA_HEADER]), method="GET", expect_header=("Metadata-Flavor", "google"))


def _probe_azure() -> bool:
    # Azure IMDS responds 200 with a JSON body when the Metadata: true header is sent.
    return _probe_http(AZURE_IMDS_URL, dict([AZURE_IMDS_HEADER]), method="GET")


def _probe_aws() -> bool:
    # AWS IMDSv2 requires a PUT to /latest/api/token. A 200 response is proof
    # we are on EC2; we discard the returned token immediately.
    return _probe_http(AWS_IMDS_TOKEN_URL, dict([AWS_IMDS_TOKEN_HEADER]), method="PUT")


def _probe_http(url: str, headers: dict[str, str], *, method: str, expect_header: tuple[str, str] | None = None) -> bool:
    req = request.Request(url, headers=headers, method=method, data=b"" if method == "PUT" else None)
    try:
        with request.urlopen(req, timeout=DETECTION_TIMEOUT) as response:
            if response.status >= 400:
                return False
            if expect_header is not None:
                name, expected = expect_header
                return response.headers.get(name, "").lower() == expected.lower()
            return True
    except (error.URLError, OSError, TimeoutError):
        return False


def resolve_dns_server_setting(value: str) -> str:
    """Expand the special value 'auto' into a concrete DNS server, if any.

    - "" (empty)  -> "" (use system resolver)
    - "auto"      -> probe for known cloud metadata servers; on success returns
                     the discovered server IP, otherwise returns "" and logs.
    - anything    -> returned unchanged.
    """
    text = (value or "").strip()
    if text.lower() != "auto":
        return text
    LOGGER.info("dns-server=auto: probing for cloud metadata server")
    cloud, detected = detect_cloud_dns_server()
    if detected:
        LOGGER.info("dns-server=auto: detected %s, using %s for reverse DNS", cloud, detected)
        return detected
    LOGGER.info("dns-server=auto: no cloud metadata server detected, falling back to system resolver")
    return ""


def _encode_dns_name(name: str) -> bytes:
    out = bytearray()
    for label in name.split("."):
        if not label:
            continue
        encoded = label.encode("ascii")
        if len(encoded) > 63:
            raise DNSLookupError(f"DNS label too long: {label!r}")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _decode_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    original_offset = offset
    jumped = False
    end_offset = offset
    safety = 0
    while True:
        if safety > 255:
            raise DNSLookupError("DNS name decode loop")
        safety += 1
        if offset >= len(data):
            raise DNSLookupError("truncated DNS response")
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end_offset = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise DNSLookupError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end_offset = offset + 2
            offset = pointer
            jumped = True
            continue
        offset += 1
        if offset + length > len(data):
            raise DNSLookupError("truncated DNS label")
        labels.append(data[offset : offset + length].decode("ascii", errors="replace"))
        offset += length
    if original_offset == end_offset:
        end_offset = offset
    return ".".join(labels), end_offset


def _reverse_arpa_name(ip_address: str) -> str:
    addr = ipaddress.ip_address(ip_address)
    if isinstance(addr, ipaddress.IPv4Address):
        return ".".join(reversed(addr.exploded.split("."))) + ".in-addr.arpa"
    nibbles = addr.exploded.replace(":", "")
    return ".".join(reversed(nibbles)) + ".ip6.arpa"


def ptr_query(ip_address: str, server: str, timeout: float) -> str | None:
    """Issue a UDP DNS PTR query to ``server`` for ``ip_address``.

    Returns the PTR target hostname, or None if the server replies with
    NXDOMAIN / no PTR record. Raises DNSLookupError on transport or protocol
    errors so the caller can surface a meaningful diagnostic.
    """
    host, port = parse_dns_server(server)
    if not host:
        raise DNSLookupError("dns server is empty")
    qname = _reverse_arpa_name(ip_address)
    txid = secrets.randbits(16)
    flags = 0x0100  # standard query, recursion desired
    header = struct.pack("!HHHHHH", txid, flags, 1, 0, 0, 0)
    question = _encode_dns_name(qname) + struct.pack("!HH", 12, 1)  # QTYPE=PTR, QCLASS=IN
    packet = header + question

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (host, port))
        except OSError as exc:
            raise DNSLookupError(f"failed to send DNS query to {host}:{port}: {exc}") from exc
        try:
            response, _ = sock.recvfrom(4096)
        except socket.timeout as exc:
            raise DNSLookupError(f"DNS query to {host}:{port} timed out after {timeout:.1f}s") from exc
        except OSError as exc:
            raise DNSLookupError(f"DNS query to {host}:{port} failed: {exc}") from exc
    finally:
        sock.close()

    if len(response) < 12:
        raise DNSLookupError("DNS response too short")
    resp_id, resp_flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", response[:12])
    if resp_id != txid:
        raise DNSLookupError("DNS response transaction id mismatch")
    rcode = resp_flags & 0x000F
    if rcode == 3:  # NXDOMAIN
        return None
    if rcode != 0:
        raise DNSLookupError(f"DNS server returned rcode {rcode}")

    offset = 12
    for _ in range(qd):
        _, offset = _decode_dns_name(response, offset)
        offset += 4  # QTYPE + QCLASS

    for _ in range(an):
        _, offset = _decode_dns_name(response, offset)
        if offset + 10 > len(response):
            raise DNSLookupError("truncated DNS answer")
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", response[offset : offset + 10])
        offset += 10
        if rtype == 12:  # PTR
            name, _ = _decode_dns_name(response, offset)
            return name
        offset += rdlength

    return None


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

    if config.verify:
        return run_verification(config)

    config.dns_server = resolve_dns_server_setting(config.dns_server)
    onboarder = DeviceOnboarder(config)
    return onboarder.run_forever()


def run_verification(config: Config) -> int:
    """Smoke-test the dependencies the onboarder needs at runtime.

    Returns 0 on success, non-zero on any failure. Designed to be safe to run
    against production: it only issues read-only requests and never POSTs.
    """
    failures = 0
    config.dns_server = resolve_dns_server_setting(config.dns_server)
    LOGGER.info("verify: parsing healthcheck address %s", config.healthcheck_address)
    try:
        host, port = parse_host_port(config.healthcheck_address)
    except ValueError as exc:
        LOGGER.error("verify: invalid healthcheck address: %s", exc)
        return 1

    LOGGER.info("verify: connecting to healthcheck at %s:%d", host, port)
    unregistered: list[str] = []
    try:
        raw = read_healthcheck(config.healthcheck_address, config.healthcheck_timeout)
        unregistered = parse_unregistered_devices(raw)
        LOGGER.info("verify: healthcheck OK (%d unregistered device(s) reported)", len(unregistered))
    except (TransientAPIError, OSError, ValueError) as exc:
        LOGGER.error("verify: healthcheck unreachable: %s", exc)
        failures += 1

    api_host = urlparse.urlsplit(config.api_root).hostname or ""
    if api_host:
        LOGGER.info("verify: resolving Kentik API host %s", api_host)
        try:
            socket.getaddrinfo(api_host, 443, type=socket.SOCK_STREAM)
            LOGGER.info("verify: DNS resolution for %s OK", api_host)
        except socket.gaierror as exc:
            LOGGER.error("verify: DNS lookup of %s failed: %s", api_host, exc)
            failures += 1
    else:
        LOGGER.error("verify: api_root %r has no hostname", config.api_root)
        failures += 1

    LOGGER.info("verify: authenticating with Kentik API at %s", config.api_root)
    try:
        KentikClient(config).ping()
        LOGGER.info("verify: Kentik API authentication OK")
    except RateLimitedError as exc:
        LOGGER.warning("verify: Kentik API rate limited (auth cannot be confirmed right now): %s", exc)
    except OnboarderError as exc:
        LOGGER.error("verify: Kentik API check failed: %s", exc)
        failures += 1

    if unregistered:
        sample = unregistered[:5]
        LOGGER.info(
            "verify: testing reverse DNS for %d sample IP(s) using %s",
            len(sample),
            f"DNS server {config.dns_server}" if config.dns_server else "system resolver",
        )
        resolver = DNSResolver(config.dns_timeout, 0.0, 0.0, server=config.dns_server)
        for ip_address in sample:
            hostname, err = resolver.reverse(ip_address)
            if hostname:
                sanitized = sanitize_device_name(normalize_ptr_name(hostname) or hostname, ip_address)
                LOGGER.info("verify: %s -> %s (device_name=%s)", ip_address, hostname, sanitized)
            else:
                LOGGER.warning("verify: reverse DNS for %s failed (%s); will fall back to IP", ip_address, err or "unknown")
    else:
        LOGGER.info("verify: no sample IPs available for reverse DNS test")

    if failures:
        LOGGER.error("verify: %d check(s) failed", failures)
        return 2
    LOGGER.info("verify: all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOGGER.info("interrupted")
        sys.exit(0)
    except Exception as exc:
        print(f"fatal error: {exc}", file=sys.stderr)
        sys.exit(1)