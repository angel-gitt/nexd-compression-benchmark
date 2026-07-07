#!/usr/bin/env python3
"""
Creative HAR crawler.

Designed to run either as a standalone CLI task or inside AWS Lambda (for
example, behind an S3 creation trigger). The crawler:

1. Reads creative JSON descriptors from a configured S3 bucket/prefix or from
   explicit S3 keys provided via event.
2. Skips creatives that already have a HAR stored in the output bucket.
3. Launches a managed Chrome session, loads each creative's HTML or URL, and
   captures the resulting network traffic using selenium-wire.
4. Persists a HAR 1.2 payload per creative plus an execution log that is
   appended to on every run.

Environment variables allow overriding most configuration values so the same
file can run locally with stateful profiles or headless within Lambda.

Requirements:
    - boto3
    - selenium>=4
Optional:
    - webdriver-manager (for local execution when chromedriver is not provided)
    - Packaged headless Chrome + chromedriver binaries for Lambda

CLI usage:
    python scripts/creative_har_crawler.py [--headless]
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, unquote_plus, urlencode, urlparse, urlunparse

import boto3
from botocore.exceptions import ClientError

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException

from selenium_stealth import stealth

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None

from scripts.har_utils import enrich_har_with_metadata
from scripts import youtube_utils


# Desktop user agent and viewport (1080p)
DESKTOP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DESKTOP_VIEWPORT = "1920,1080"

# Mobile user agent and viewport (1080x1920 for fair comparison with desktop in physical pixels)
MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
MOBILE_VIEWPORT = "1080,1920"


# =============================================================================
# Configuration
# =============================================================================


@dataclasses.dataclass(frozen=True)
class CrawlerConfig:
    region = os.getenv("AWS_REGION")
    ssm = boto3.client("ssm", region_name=region)
    
    input_bucket: str = ssm.get_parameter(Name="/generic/hiiliDataBucket")["Parameter"]["Value"]
    # Dynamic prefix for the input bucket (can be set via environment or derived from the Lambda event).
    input_prefix: str = ""

    output_bucket: str = ssm.get_parameter(Name="/generic/hiiliCreatives")["Parameter"]["Value"]
    # Dynamic prefix for the output bucket (can be set via environment or derived from the Lambda event).
    output_prefix: str = ""

    #: Prefix inside the output bucket for the accumulated log file.
    log_key: str = "logs/creative_crawler.log"

    #: Optional key in the *input* bucket with YouTube cookies JSON
    #: used to authenticate against YouTube without interactive login.
    #: By defecto se busca en la raíz del bucket de input.
    cookies_key: Optional[str] = "youtube_cookies.json"

    #: Whether Selenium should run headless by default (overridable by CLI).
    default_headless: bool = True

    #: Seconds to let the page load before we start polling for network logs.
    warmup_seconds: float = 0.0

    #: Total capture time in seconds after navigation.
    capture_seconds: float = 10.0

    #: Timeout (s) for Selenium page loads.
    page_load_timeout: int = 60

    #: Custom user-agent string used for browser launches.
    # Default to Chrome's native UA. Override via env USER_AGENT when needed.
    user_agent: str = ""

    #: Maximum creatives to process per run (None for unlimited).
    max_creatives_per_run: Optional[int] = None

    #: Optional path to the Chrome binary (useful inside Lambda layers).
    chrome_binary_path: Optional[str] = None

    #: Optional path to the chromedriver binary.
    chromedriver_path: Optional[str] = None

    #: Optional allowlist of user segments to process. When set, only creatives
    #: under these first-level user folders are crawled. Accepts a comma- or
    #: whitespace-separated list via environment variable.
    allowed_users: Optional[set] = dataclasses.field(default=None)

    def with_prefixes(self, input_prefix: str, output_prefix: str) -> "CrawlerConfig":
        """
        Return a new configuration based on this one, but with updated
        input and output prefixes.
        """
        # Ensure prefixes end with "/" when they are not empty.
        normalized_input = input_prefix or ""
        if normalized_input and not normalized_input.endswith("/"):
            normalized_input = f"{normalized_input}/"

        normalized_output = output_prefix or ""
        if normalized_output and not normalized_output.endswith("/"):
            normalized_output = f"{normalized_output}/"

        return dataclasses.replace(
            self,
            input_prefix=normalized_input,
            output_prefix=normalized_output,
        )

    @staticmethod
    def _bool_env(key: str, default: bool) -> bool:
        raw = os.environ.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _int_env(key: str, default: Optional[int]) -> Optional[int]:
        raw = os.environ.get(key)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @classmethod
    def from_env(cls) -> "CrawlerConfig":
        prefix = os.environ.get("INPUT_PREFIX", cls.input_prefix)
        if prefix and not prefix.endswith("/"):
            prefix = f"{prefix}/"

        cookies_key_env = os.environ.get("YOUTUBE_COOKIES_KEY", cls.cookies_key or "")
        cookies_key = cookies_key_env or None

        # Parse optional users allowlist from env (e.g., "user1,user2 user3")
        raw_users = os.environ.get("CRAWL_USERS") or os.environ.get("INCLUDE_USERS")
        users_set = None
        if raw_users:
            tokens = [t.strip() for t in raw_users.replace(",", " ").split() if t.strip()]
            users_set = set(tokens) if tokens else None

        return cls(
            input_bucket=os.environ.get("INPUT_BUCKET", cls.input_bucket),
            input_prefix=prefix,
            output_bucket=os.environ.get("OUTPUT_BUCKET", cls.output_bucket),
            output_prefix=os.environ.get("OUTPUT_PREFIX", cls.output_prefix),
            log_key=os.environ.get("LOG_KEY", cls.log_key),
            default_headless=cls._bool_env("DEFAULT_HEADLESS", cls.default_headless),
            warmup_seconds=float(os.environ.get("WARMUP_SECONDS", cls.warmup_seconds)),
            capture_seconds=float(os.environ.get("CAPTURE_SECONDS", cls.capture_seconds)),
            page_load_timeout=int(os.environ.get("PAGE_LOAD_TIMEOUT", cls.page_load_timeout)),
            max_creatives_per_run=cls._int_env("MAX_CREATIVES_PER_RUN", cls.max_creatives_per_run),
            chrome_binary_path=os.environ.get("CHROME_BINARY_PATH", cls.chrome_binary_path),
            chromedriver_path=os.environ.get("CHROMEDRIVER_PATH", cls.chromedriver_path),
            user_agent=os.environ.get("USER_AGENT", cls.user_agent),
            allowed_users=users_set,
            cookies_key=cookies_key,
        )


# =============================================================================
# Logging helper
# =============================================================================


class ExecutionLog:
    """Accumulates log lines and syncs them to S3."""

    def __init__(self, bucket: str, key: str, s3_client):
        self.bucket = bucket
        self.key = key
        self.s3 = s3_client
        self.lines: List[str] = []
        self._load_existing()

    def _load_existing(self) -> None:
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self.key)
            existing = resp["Body"].read().decode("utf-8", errors="replace")
            if existing:
                self.lines.append(existing.rstrip("\n"))
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                raise
        except Exception:
            pass  # Non-fatal; a new log will be created on flush.

    def info(self, message: str) -> None:
        stamp = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        self.lines.append(f"[{stamp}] {message}")

    def flush(self) -> None:
        payload = "\n".join(line for line in self.lines if line) + "\n"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=payload.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )


class LocalExecutionLog:
    """Writes log lines to a local file (used for test runs without S3 writes)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lines: List[str] = []
        try:
            if self.path.exists():
                existing = self.path.read_text(encoding="utf-8")
                if existing:
                    self.lines.append(existing.rstrip("\n"))
        except Exception:
            pass

    def info(self, message: str) -> None:
        stamp = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        self.lines.append(f"[{stamp}] {message}")

    def flush(self) -> None:
        payload = "\n".join(line for line in self.lines if line) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload, encoding="utf-8")


class Progress:
    """Lightweight progress bar wrapper (tqdm when available)."""

    def __init__(self, desc: str, total: Optional[int] = None, unit: str = "items"):
        self.desc = desc
        self.total = total
        self.unit = unit
        self.count = 0
        self._bar = None
        self._enabled = (
            tqdm is not None
            and sys.stderr.isatty()
            and not os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        )
        if self._enabled:
            self._bar = tqdm(desc=desc, total=total, unit=unit, leave=False)
        else:
            if desc:
                print(f"{desc}...", flush=True)

    def update(self, amount: int = 1):
        if amount <= 0:
            return
        self.count += amount
        if self._bar is not None:
            self._bar.update(amount)
        else:
            if self.total:
                print(
                    f"{self.desc}: {self.count}/{self.total} {self.unit}",
                    end="\r" if sys.stderr.isatty() else "\n",
                    flush=True,
                )
            elif self.count % 25 == 0:
                print(f"{self.desc}: {self.count} {self.unit}", flush=True)

    def close(self):
        if self._bar is not None:
            self._bar.close()
        else:
            if self.total and self.count >= self.total:
                print(f"{self.desc}: {self.count}/{self.total} {self.unit}")

# =============================================================================
# S3 utilities
# =============================================================================


def iter_creative_objects(config: CrawlerConfig, s3_client) -> Iterable[str]:
    """Yield JSON object keys under the configured input prefix."""
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.input_bucket, Prefix=config.input_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".json"):
                yield key


def download_json(bucket: str, key: str, s3_client) -> Optional[dict]:
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return None
        raise
    body = obj["Body"].read()
    return json.loads(body.decode("utf-8", errors="replace"))


def upload_json(bucket: str, key: str, payload: dict, s3_client) -> None:
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
    )


def object_exists(bucket: str, key: str, s3_client) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _split_path_candidates(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    segments: List[str] = []
    normalized = raw.replace(";", os.pathsep).replace(",", os.pathsep)
    for part in normalized.split(os.pathsep):
        candidate = part.strip()
        if candidate:
            segments.append(candidate)
    return segments


def _first_existing(candidates: Iterable[str]) -> Optional[str]:
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return str(path)
    return None


# =============================================================================
# Selenium / HAR capture
# =============================================================================
def _import_selenium_wire():
    try:
        from seleniumwire import webdriver as wire_webdriver  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            str(exc)
        ) from exc
    return wire_webdriver


def _resolve_chrome_binary(config: CrawlerConfig) -> Optional[str]:
    candidates: List[str] = []
    if config.chrome_binary_path:
        candidates.append(config.chrome_binary_path)
    candidates.extend(_split_path_candidates(os.environ.get("CHROME_BINARY_PATH")))
    candidates.extend(
        [
            "/opt/chrome/chrome",
            "/opt/bin/headless-chromium",
            "/opt/google/chrome/chrome",
        ]
    )
    return _first_existing(candidates)


def _resolve_chromedriver(config: CrawlerConfig) -> Optional[str]:
    candidates: List[str] = []
    if config.chromedriver_path:
        candidates.append(config.chromedriver_path)
    candidates.extend(_split_path_candidates(os.environ.get("CHROMEDRIVER_PATH")))
    candidates.extend(
        [
            "/opt/chromedriver/chromedriver",
            "/opt/bin/chromedriver",
            "chromedriver",
        ]
    )
    return _first_existing(candidates)


class SeleniumSession:
    """Context manager responsible for launching and tearing down Chrome."""

    def __init__(self, config: CrawlerConfig, log, headless: bool, user_agent: str, viewport: str):
        self.config = config
        self.log = log
        self.headless = headless
        self.user_agent = user_agent
        self.viewport = viewport
        self.driver = None
        self.temp_profile: Optional[Path] = None
        self.chrome_log_path: Optional[Path] = None
        self._chrome_log_drained = False

    def __enter__(self):
        chrome_options = Options()

        # Usar siempre un perfil temporal por ejecución
        self.temp_profile = Path(tempfile.mkdtemp(prefix="creative_har_profile_"))
        profile_dir = self.temp_profile
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        chrome_options.add_argument("--disable-application-cache")
        chrome_options.add_argument("--aggressive-cache-discard")
        chrome_options.add_argument("--disable-cache")
        chrome_options.add_argument("--disk-cache-size=0")
        chrome_options.add_argument("--media-cache-size=0")
        chrome_options.add_argument("--disable-background-networking")
        chrome_options.add_argument("--lang=en-US,en")
        chrome_options.add_argument("--disable-features=SameSiteByDefaultCookies,CookiesWithoutSameSiteMustBeSecure,PrivacySandboxAdsAPIs")
        chrome_options.add_argument("--single-process")
        chrome_options.add_argument("--no-zygote")
        chrome_options.add_argument("--disable-software-rasterizer")
        chrome_options.add_argument("--remote-debugging-port=9223")
        chrome_options.add_argument("--enable-logging=stderr")
        chrome_options.add_argument("--v=1")
        fd, chrome_log = tempfile.mkstemp(prefix='chrome_', suffix='.log', dir=tempfile.gettempdir())
        os.close(fd)
        self.chrome_log_path = Path(chrome_log)
        if self.log:
            self.log.info(f"Chrome log file: {self.chrome_log_path}")
        chrome_options.add_argument(f'--log-file={self.chrome_log_path}')
        chrome_binary = _resolve_chrome_binary(self.config)
        if chrome_binary:
            chrome_options.binary_location = chrome_binary
            if self.log:
                self.log.info(f"Chrome binary: {chrome_binary}")
        elif self.log:
            self.log.info("Chrome binary: default (PATH)")
        chrome_options.add_argument(f"--user-agent={self.user_agent}")

        if self.headless:
            chrome_options.add_argument("--headless=new")

        # Use the specified viewport
        chrome_options.add_argument(f"--window-size={self.viewport}")

        service = None
        chromedriver_path = _resolve_chromedriver(self.config)
        if chromedriver_path:
            service = Service(chromedriver_path)
            if self.log:
                self.log.info(f"Chromedriver: {chromedriver_path}")
        elif ChromeDriverManager is not None:
            service = Service(ChromeDriverManager().install())
            if self.log:
                self.log.info("Chromedriver: webdriver-manager auto-install")
        elif self.log:
            self.log.info("Chromedriver: default (PATH)")

        # Always use selenium-wire
        wire_webdriver = _import_selenium_wire()
        seleniumwire_options = {
            "request_storage": "memory",
            "verify_ssl": True,
        }
        kwargs = {"options": chrome_options, "seleniumwire_options": seleniumwire_options}
        if service:
            kwargs["service"] = service
        if self.log:
            self.log.info("Launching Chrome with selenium-wire")
        try:
            driver = wire_webdriver.Chrome(**kwargs)

            # Aplicar selenium-stealth para reducir huella de automatización
            try:
                stealth(
                    driver,
                    languages=["en-US", "en"],
                    vendor="Google Inc.",
                    platform="Win32",
                    webgl_vendor="Intel Inc.",
                    renderer="Intel Iris OpenGL Engine",
                    fix_hairline=True,
                )
            except Exception:
                if self.log:
                    self.log.info("selenium-stealth configuration failed; continuing without it")

        except Exception as exc:
            if self.log:
                self.log.info(f"Chrome launch failed: {exc}")
            self._drain_chrome_log("chrome startup log")
            if self.temp_profile is not None:
                shutil.rmtree(self.temp_profile, ignore_errors=True)
            if self.chrome_log_path:
                with contextlib.suppress(Exception):
                    Path(self.chrome_log_path).unlink()
                self.chrome_log_path = None
            raise

        driver.set_page_load_timeout(self.config.page_load_timeout)
        with contextlib.suppress(Exception):
            driver.execute_cdp_cmd("Network.enable", {"maxResourceBufferSize": 0, "maxTotalBufferSize": 0})
            driver.execute_cdp_cmd("Page.enable", {})
            driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
            driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        self.driver = driver
        return driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver is not None:
            with contextlib.suppress(Exception):
                self.driver.quit()
        if exc_type is not None:
            self._drain_chrome_log("chrome.log")
        if self.temp_profile is not None:
            shutil.rmtree(self.temp_profile, ignore_errors=True)
        if self.chrome_log_path:
            with contextlib.suppress(Exception):
                Path(self.chrome_log_path).unlink()
            self.chrome_log_path = None

    def _drain_chrome_log(self, reason: str) -> None:
        if not self.chrome_log_path or not self.log or self._chrome_log_drained:
            return
        try:
            log_path = Path(self.chrome_log_path)
            if not log_path.exists():
                return
            content = log_path.read_text(encoding='utf-8', errors='replace')
            if not content.strip():
                return
            lines = content.splitlines()
            tail = lines[-200:] if len(lines) > 200 else lines
            for line in tail:
                self.log.info(f"{reason}: {line}")
            self._chrome_log_drained = True
        except Exception:
            pass


class SeleniumWireCollector:
    """Builds a HAR document from selenium-wire's recorded requests."""

    def __init__(self, driver, config: CrawlerConfig):
        self.driver = driver
        self.config = config
        self.anchor_dt = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        # Map to store transfer sizes from CDP events (URL -> encodedDataLength)
        # Will be populated when build_har is called
        self.transfer_sizes: Dict[str, int] = {}

    def _collect_cdp_transfer_sizes(self) -> None:
        """Collect transfer sizes from CDP performance logs.
        Maps requestId -> (url, encodedDataLength) for correlation with selenium-wire requests.
        """
        # Map: requestId -> (url, encodedDataLength)
        request_id_map: Dict[str, Tuple[str, int]] = {}
        try:
            logs = self.driver.get_log("performance")
            for entry in logs:
                try:
                    message = json.loads(entry.get("message", "{}")).get("message", {})
                    method = message.get("method")
                    params = message.get("params", {})
                    request_id = params.get("requestId")
                    
                    if method == "Network.responseReceived" and request_id:
                        # Store URL from response or request
                        response = params.get("response", {})
                        url = response.get("url", "") if isinstance(response, dict) else ""
                        # If URL not in response, try to get from request
                        if not url:
                            request_obj = params.get("request", {})
                            url = request_obj.get("url", "") if isinstance(request_obj, dict) else ""
                        if url:
                            # Initialize or update entry
                            if request_id not in request_id_map:
                                request_id_map[request_id] = (url, -1)
                            else:
                                # Update URL if not set
                                old_url, old_size = request_id_map[request_id]
                                if not old_url:
                                    request_id_map[request_id] = (url, old_size)
                    
                    elif method == "Network.requestWillBeSent" and request_id:
                        # Also capture URL from requestWillBeSent as backup
                        request_obj = params.get("request", {})
                        url = request_obj.get("url", "") if isinstance(request_obj, dict) else ""
                        if url and request_id not in request_id_map:
                            request_id_map[request_id] = (url, -1)
                    
                    elif method == "Network.loadingFinished" and request_id:
                        # Store encoded data length
                        encoded_data_length = params.get("encodedDataLength", -1)
                        if encoded_data_length >= 0:
                            if request_id in request_id_map:
                                url, _ = request_id_map[request_id]
                                request_id_map[request_id] = (url, encoded_data_length)
                            else:
                                # If we don't have URL yet, store with empty URL
                                request_id_map[request_id] = ("", encoded_data_length)
                except (json.JSONDecodeError, KeyError, AttributeError):
                    continue
            
            # Convert to URL -> size mapping
            for request_id, (url, size) in request_id_map.items():
                if url and size >= 0:
                    # Store the size for this URL (may overwrite if same URL appears multiple times)
                    self.transfer_sizes[url] = size
        except Exception:
            pass
    
    def _get_transfer_size(self, url: str, resp_headers: Dict[str, str]) -> Optional[int]:
        """Get transfer size from CDP data or Content-Length header."""
        # First try CDP data
        if url in self.transfer_sizes:
            return self.transfer_sizes[url]
        
        # Try Content-Length header as fallback
        content_length = resp_headers.get("content-length")
        if content_length:
            try:
                return int(content_length)
            except (ValueError, TypeError):
                pass
        
        return None
    
    @staticmethod
    def _lower_headers(headers) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            # selenium-wire uses CaseInsensitiveDict-like mapping
            for k, v in headers.items():
                out[str(k).lower()] = v
        except Exception:
            pass
        return out

    def build_har(self, page_title: str) -> dict:
        # Collect transfer sizes from CDP before processing requests
        self._collect_cdp_transfer_sizes()
        
        entries: List[dict] = []
        page_start = self.anchor_dt.isoformat().replace("+00:00", "Z")
        # requests is a list that grows over time; snapshot it now
        requests = list(getattr(self.driver, "requests", []) or [])
        for req in requests:
            url = getattr(req, "url", "") or ""
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            method = getattr(req, "method", "GET") or "GET"
            # Response
            resp = getattr(req, "response", None)
            status = getattr(resp, "status_code", 0) if resp else 0
            resp_headers_raw = getattr(resp, "headers", {}) if resp else {}
            resp_headers = self._lower_headers(resp_headers_raw)
            # Decoded body size (descomprimido)
            decoded_body_size = 0
            # Transfer size (comprimido, transferido por la red)
            transfer_size = 0

            mime = resp_headers.get("content-type", "")
            if resp:
                try:
                    b = resp.body or b""
                    decoded_body_size = len(b)
                    # Try to get transfer size from CDP or headers
                    transfer_size_result = self._get_transfer_size(url, resp_headers)
                    if transfer_size_result is not None:
                        transfer_size = transfer_size_result
                    
                    content_obj = {"size": decoded_body_size, "mimeType": mime}
                except Exception:
                    content_obj = {"size": -1, "mimeType": mime}
            else:
                content_obj = {"size": -1, "mimeType": mime}

            # httpVersion best-effort
            http_version = "HTTP/1.1"
            try:
                pv = getattr(resp, "http_version", None)
                if pv:
                    pl = str(pv).lower()
                    http_version = "http/2.0" if pl in {"h2", "http/2", "http/2.0"} else ("HTTP/1.1" if pl.startswith("http/1") else str(pv))
            except Exception:
                pass

            request_obj = {
                "method": method,
                "url": url,
                "httpVersion": http_version,
            }
            response_obj = {
                "status": status,
                "httpVersion": http_version,
                "content": content_obj,
                # Omit redirectURL
            }
            
            # bodySize: tamaño descomprimido (decoded body size)
            response_obj["decodedBodySize"] = decoded_body_size
            
            # Tamaño transferido por la red (comprimido)
            response_obj["transferSize"] = transfer_size
            
            # Calcular compresión si tenemos ambos tamaños
            if decoded_body_size >= 0 and transfer_size >= 0 and transfer_size > 0:
                compression = decoded_body_size - transfer_size
                if compression != 0:
                    content_obj["compression"] = compression
            
            response_obj["bodyCaptured"] = resp is not None
            entries.append(
                {
                    "pageref": page_title,
                    "startedDateTime": page_start,  # coarse; selenium-wire lacks per-request timestamps
                    "request": request_obj,
                    "response": response_obj,
                }
            )
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "WebInspector", "version": "537.36"},
                "browser": {"name": "chrome", "version": "unknown"},
                "pages": [
                    {
                        "startedDateTime": page_start,
                        "id": page_title,
                        "title": page_title,
                        "pageTimings": {"onContentLoad": -1, "onLoad": -1},
                    }
                ],
                "entries": entries,
            }
        }


DOM_SNAPSHOT_SCRIPT = r'''
return (function() {
  const VIEWPORT_DEFAULT = { width: window.innerWidth || 0, height: window.innerHeight || 0, devicePixelRatio: window.devicePixelRatio || 1 };
  const docEl = document.documentElement || document.body;
  const limit = 1200;

  function round(value) {
    if (typeof value !== "number" || !isFinite(value)) {
      return null;
    }
    return Math.round(value * 1000) / 1000;
  }

  function rectOf(el) {
    if (!el || !el.getBoundingClientRect) {
      return null;
    }
    const r = el.getBoundingClientRect();
    return {
      top: round(r.top),
      left: round(r.left),
      right: round(r.right),
      bottom: round(r.bottom),
      width: round(r.width),
      height: round(r.height),
      x: round(r.x),
      y: round(r.y)
    };
  }

  function ancestorInfo(el) {
    const ancestors = [];
    let current = el ? el.parentElement : null;
    let depth = 0;
    while (current && depth < 3) {
      ancestors.push({
        tag: current.tagName ? current.tagName.toLowerCase() : null,
        id: current.id || null,
        classes: current.classList ? Array.from(current.classList) : [],
        rect: rectOf(current)
      });
      current = current.parentElement;
      depth += 1;
    }
    return ancestors;
  }

  function cleanSrc(src) {
    if (!src) {
      return null;
    }
    try {
      return new URL(src, document.baseURI).href;
    } catch (err) {
      return src;
    }
  }

  function unique(list) {
    const seen = new Set();
    const out = [];
    for (const item of list) {
      if (item && !seen.has(item)) {
        seen.add(item);
        out.push(item);
      }
    }
    return out;
  }

  function backgroundUrls(el) {
    const urls = [];
    if (!el) {
      return urls;
    }
    let value = "";
    try {
      value = window.getComputedStyle(el).getPropertyValue("background-image") || "";
    } catch (err) {
      value = "";
    }
    if (!value || value === "none") {
      return urls;
    }
    const regex = /url\((\"|\\')?(.*?)\1\)/g;
    let match;
    while ((match = regex.exec(value))) {
      const url = cleanSrc(match[2]);
      if (url) {
        urls.push(url);
      }
    }
    return unique(urls);
  }

  const nodes = new Set();
  const selectors = ["img", "video", "canvas", "iframe", "picture img", "picture source", "source", "svg image"];
  selectors.forEach(sel => {
    document.querySelectorAll(sel).forEach(el => nodes.add(el));
  });

  const all = document.getElementsByTagName("*");
  const maxBackgroundChecks = Math.min(all.length, limit);
  for (let i = 0; i < maxBackgroundChecks; i++) {
    const el = all[i];
    if (!el) {
      continue;
    }
    const urls = backgroundUrls(el);
    if (urls.length) {
      nodes.add(el);
    }
  }

  const elements = [];
  nodes.forEach(el => {
    const tag = el.tagName ? el.tagName.toLowerCase() : null;
    const rect = rectOf(el);
    const containerRect = el.offsetParent ? rectOf(el.offsetParent) : (el.parentElement ? rectOf(el.parentElement) : null);
    const dataset = {};
    if (el.dataset) {
      for (const key in el.dataset) {
        dataset[key] = el.dataset[key];
      }
    }
    const classes = el.classList ? Array.from(el.classList) : [];
    const sources = [];
    const poster = el.poster ? cleanSrc(el.poster) : null;
    if (tag === "video") {
      if (el.currentSrc) {
        sources.push(cleanSrc(el.currentSrc));
      }
      el.querySelectorAll("source").forEach(node => {
        if (node.src) {
          sources.push(cleanSrc(node.src));
        }
      });
      if (poster) {
        sources.push(poster);
      }
    } else if (tag === "img") {
      if (el.currentSrc) {
        sources.push(cleanSrc(el.currentSrc));
      }
      if (el.src) {
        sources.push(cleanSrc(el.src));
      }
      const srcset = el.getAttribute("srcset");
      if (srcset) {
        srcset.split(",").forEach(part => {
          const candidate = part.trim().split(" ")[0];
          if (candidate) {
            sources.push(cleanSrc(candidate));
          }
        });
      }
    } else if (tag === "iframe" || tag === "source" || tag === "canvas") {
      if (el.src) {
        sources.push(cleanSrc(el.src));
      }
    }
    const bg = backgroundUrls(el);
    const allSources = unique(sources.concat(bg));
    elements.push({
      tag,
      id: el.id || null,
      role: el.getAttribute ? el.getAttribute("role") : null,
      classes,
      dataset,
      rect,
      containerRect,
      viewportRatio: rect ? Number(((rect.width * rect.height) / ((VIEWPORT_DEFAULT.width || 1) * (VIEWPORT_DEFAULT.height || 1))).toFixed(6)) : null,
      offsetWidth: el.offsetWidth || 0,
      offsetHeight: el.offsetHeight || 0,
      clientWidth: el.clientWidth || 0,
      clientHeight: el.clientHeight || 0,
      scrollWidth: el.scrollWidth || 0,
      scrollHeight: el.scrollHeight || 0,
      naturalWidth: typeof el.naturalWidth === "number" ? el.naturalWidth : null,
      naturalHeight: typeof el.naturalHeight === "number" ? el.naturalHeight : null,
      videoWidth: typeof el.videoWidth === "number" ? el.videoWidth : null,
      videoHeight: typeof el.videoHeight === "number" ? el.videoHeight : null,
      poster,
      sources: allSources,
      backgroundImageCount: bg.length,
      ancestors: ancestorInfo(el)
    });
  });

  const sourceMap = {};
  elements.forEach((info, index) => {
    info.sources.forEach(url => {
      if (!url) {
        return;
      }
      if (!sourceMap[url]) {
        sourceMap[url] = [];
      }
      sourceMap[url].push(index);
    });
  });

  return {
    collectedAt: new Date().toISOString(),
    viewport: VIEWPORT_DEFAULT,
    scroll: {
      x: window.scrollX || window.pageXOffset || 0,
      y: window.scrollY || window.pageYOffset || 0
    },
    documentSize: {
      width: docEl ? docEl.scrollWidth : 0,
      height: docEl ? docEl.scrollHeight : 0
    },
    elementCount: elements.length,
    elements,
    sourceMap
  };
})();
'''


def collect_dom_snapshot(driver) -> Optional[dict]:
    try:
        snapshot = driver.execute_script(DOM_SNAPSHOT_SCRIPT)
    except Exception:
        return None
    if isinstance(snapshot, dict):
        return snapshot
    return None


def is_likely_icon_or_logo(element: dict) -> bool:
    """
    Detecta si un elemento es probablemente un icono o logo basándose en:
    - naturalWidth/naturalHeight mucho más grandes que offsetWidth/offsetHeight
    - Dimensiones muy pequeñas
    """
    tag = element.get("tag", "")
    
    # Solo aplicar esta lógica a imágenes
    if tag != "img":
        return False
    
    offset_width = element.get("offsetWidth", 0)
    offset_height = element.get("offsetHeight", 0)
    natural_width = element.get("naturalWidth")
    natural_height = element.get("naturalHeight")
    
    # Si la imagen natural es mucho más grande que el tamaño renderizado,
    # probablemente es un icono/logo escalado
    if natural_width and natural_height and offset_width > 0 and offset_height > 0:
        scale_factor_w = natural_width / offset_width if offset_width > 0 else 1
        scale_factor_h = natural_height / offset_height if offset_height > 0 else 1
        
        # Si la imagen natural es más de 2x el tamaño renderizado, probablemente es un icono
        if scale_factor_w > 2.0 or scale_factor_h > 2.0:
            return True
    
    # Elementos muy pequeños probablemente son iconos
    if offset_width <= 80 and offset_height <= 80:
        return True
    
    return False


def extract_ad_size(dom_snapshot: Optional[dict], driver, device_type: str = "desktop") -> Tuple[Optional[int], Optional[int]]:
    """
    Extract ad size in pixels (width, height).
    Uses CSS pixels from DOM snapshot and JavaScript selectors.
    
    Args:
        dom_snapshot: DOM snapshot dictionary
        driver: Selenium driver instance
        device_type: Device type ("mobile" or "desktop") to apply appropriate thresholds
    
    Returns:
        Tuple of (width, height) or (None, None) if not found.
    """
    if not dom_snapshot:
        return None, None
    
    elements = dom_snapshot.get("elements", [])
    if not elements:
        return None, None

    # Configuración según tipo de dispositivo
    is_mobile = device_type.lower() == "mobile"
    if is_mobile:
        # Para móvil, exigir algo más de tamaño para evitar iconos/UI pequeños
        min_width, min_height = 100, 100
    else:
        # Para desktop, permitir creatividades tipo 16:9 pequeñas
        min_width, min_height = 120, 90
    
    # Try to find ad container using JavaScript
    ad_size_script = f"""
    (function() {{
        const isMobile = {str(is_mobile).lower()};
        const minWidth = {min_width};
        const minHeight = {min_height};
        
        // For programmatic ads
        const programmaticSelectors = [
            'div[class*="ad"]', 'div[id*="ad"]', 'div[class*="advertisement"]',
            'div[class*="banner"]', 'iframe[class*="ad"]', 'div[class*="sponsor"]',
            'div[class*="Ad"]', 'div[id*="Ad"]'
        ];
        
        // For Meta ads
        const metaSelectors = [
            'div[data-testid*="ad"]', 'div[class*="fb"]', 'div[class*="meta"]',
            'div[role="article"]', 'div[class*="feed"]', 'div[data-pagelet*="Feed"]'
        ];
        
        // For YouTube ads
        const youtubeSelectors = [
            'div[id*="player"]', 'div[class*="player"]', 'div[id*="movie_player"]',
            'div[class*="ytp-"]', 'div[id*="ad-"]', 'div[class*="ad-"]',
            'div[class*="video-ads"]', 'div[id*="video-ads"]'
        ];
        
        function isLikelyIconOrLogo(el) {{
            if (el.tagName !== 'IMG') return false;
            const offsetW = el.offsetWidth || 0;
            const offsetH = el.offsetHeight || 0;
            const naturalW = el.naturalWidth || 0;
            const naturalH = el.naturalHeight || 0;
            
            if (naturalW > 0 && naturalH > 0 && offsetW > 0 && offsetH > 0) {{
                const scaleW = naturalW / offsetW;
                const scaleH = naturalH / offsetH;
                if (scaleW > 2.0 || scaleH > 2.0) return true;
            }}
            
            if (offsetW <= 80 && offsetH <= 80) return true;
            return false;
        }}
        
        function findAdElement(selectors) {{
            for (const selector of selectors) {{
                try {{
                    const elements = document.querySelectorAll(selector);
                    for (const el of elements) {{
                        if (isLikelyIconOrLogo(el)) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width >= minWidth && rect.height >= minHeight) {{
                            return {{ width: Math.round(rect.width), height: Math.round(rect.height) }};
                        }}
                    }}
                }} catch (e) {{}}
            }}
            return null;
        }}
        
        // Try programmatic first
        let result = findAdElement(programmaticSelectors);
        if (result) return result;
        
        // Try Meta selectors
        result = findAdElement(metaSelectors);
        if (result) return result;
        
        // Try YouTube selectors
        result = findAdElement(youtubeSelectors);
        if (result) return result;
        
        // Improved fallback: exclude full-page containers and find ad-like elements
        const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
        const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
        const viewportArea = viewportWidth * viewportHeight;
        
        // Exclude elements that are too close to viewport size (likely page containers)
        const maxReasonableSize = Math.min(viewportArea * 0.95, 1920 * 1080);
        
        // First, prioritize video elements (most likely to be the ad content)
        const videoElements = document.querySelectorAll('video');
        let largestVideo = null;
        let maxVideoArea = 0;
        
        for (const el of videoElements) {{
            const rect = el.getBoundingClientRect();
            const area = rect.width * rect.height;
            
            // Skip if element is too large (likely page container)
            if (area > maxReasonableSize) continue;
            
            // Skip if element is outside viewport
            if (rect.right < 0 || rect.bottom < 0 || rect.left > viewportWidth || rect.top > viewportHeight) continue;
            
            // Skip very small elements (likely tracking pixels)
            if (rect.width < 5 || rect.height < 5) continue;
            
            // Video elements with reasonable dimensions
            if (rect.width >= minWidth && rect.height >= minHeight && area > maxVideoArea) {{
                maxVideoArea = area;
                largestVideo = {{ width: Math.round(rect.width), height: Math.round(rect.height) }};
            }}
        }}
        
        if (largestVideo) return largestVideo;
        
        // Then try other elements
        const allElements = document.querySelectorAll('div, iframe, img, canvas');
        const candidates = [];
        
        for (const el of allElements) {{
            if (isLikelyIconOrLogo(el)) continue;
            
            const rect = el.getBoundingClientRect();
            const area = rect.width * rect.height;
            
            // Skip if element is too large (likely page container)
            if (area > maxReasonableSize) continue;
            
            // Skip if element is outside viewport
            if (rect.right < 0 || rect.bottom < 0 || rect.left > viewportWidth || rect.top > viewportHeight) continue;
            
            // Skip very small elements
            if (rect.width < 5 || rect.height < 5) continue;
            
            // For mobile, ignore small elements that are likely UI components
            if (isMobile && (rect.width < minWidth || rect.height < minHeight)) continue;
            
            // Prefer elements with reasonable ad-like dimensions
            if (rect.width >= minWidth && rect.height >= minHeight && 
                rect.width <= 1920 && rect.height <= 1080) {{
                candidates.push({{ width: Math.round(rect.width), height: Math.round(rect.height), area: area }});
            }}
        }}
        
        // Sort by area and return largest
        if (candidates.length > 0) {{
            candidates.sort((a, b) => b.area - a.area);
            return {{ width: candidates[0].width, height: candidates[0].height }};
        }}
        
        // If no reasonable element found and not mobile, try with lower threshold
        if (!isMobile) {{
            let largest = null;
            let maxArea = 0;
            
            for (const el of allElements) {{
                if (isLikelyIconOrLogo(el)) continue;
                
                const rect = el.getBoundingClientRect();
                const area = rect.width * rect.height;
                
                if (area > maxReasonableSize) continue;
                if (rect.right < 0 || rect.bottom < 0 || rect.left > viewportWidth || rect.top > viewportHeight) continue;
                if (rect.width < 5 || rect.height < 5) continue;
                
                if (area > maxArea && rect.width >= 50 && rect.height >= 50) {{
                    maxArea = area;
                    largest = {{ width: Math.round(rect.width), height: Math.round(rect.height) }};
                }}
            }}
        
        return largest;
        }}
        
        return null;
    }})();
    """
    
    try:
        size_result = driver.execute_script(ad_size_script)
        if size_result and isinstance(size_result, dict):
            width = size_result.get("width")
            height = size_result.get("height")
            if width and height and width > 0 and height > 0:
                # Validar que no sea un tamaño sospechosamente pequeño para móvil
                if is_mobile and (width < 100 or height < 100):
                    # Para móvil, rechazar dimensiones muy pequeñas
                    pass  # Continuar al fallback del DOM snapshot
                else:
                    return int(width), int(height)
    except Exception:
        pass
    
    # Fallback: use DOM snapshot elements
    # Find the largest visible element, but exclude viewport-sized containers
    # Prioritize video elements first, then other elements
    viewport = dom_snapshot.get("viewport", {})
    viewport_width = viewport.get("width", 1920)
    viewport_height = viewport.get("height", 1080)
    viewport_area = viewport_width * viewport_height
    max_reasonable_size = min(viewport_area * 0.95, 1920 * 1080)
    
    # First, try to find video elements (most likely to be the ad content)
    largest_video = None
    max_video_area = 0
    
    for element in elements:
        tag = element.get("tag", "")
        if tag != "video":
            continue
        rect = element.get("rect")
        if not rect:
            continue
        width = rect.get("width", 0)
        height = rect.get("height", 0)
        if width > 0 and height > 0:
            area = width * height
            
            # Skip elements that are too large (likely page containers)
            if area > max_reasonable_size:
                continue
            
            # Skip very small elements (likely tracking pixels)
            if width < 5 or height < 5:
                continue
            
            # Video elements with reasonable dimensions
            if width >= min_width and height >= min_height and area > max_video_area:
                max_video_area = area
                largest_video = (int(width), int(height))
    
    if largest_video:
        return largest_video
    
    # Then try other elements
    candidates = []
    
    for element in elements:
        # Filtrar iconos/logos
        if is_likely_icon_or_logo(element):
            continue
        
        rect = element.get("rect")
        if not rect:
            continue
        
        width = rect.get("width", 0)
        height = rect.get("height", 0)
        
        if width > 0 and height > 0:
            area = width * height
            
            if area > max_reasonable_size:
                continue
            
            # Skip very small elements
            if width < 5 or height < 5:
                continue
            
            # For mobile, ignore small elements that are likely UI components
            if is_mobile and (width < min_width or height < min_height):
                continue
            
            # Prefer elements with reasonable ad-like dimensions
            if width >= min_width and height >= min_height and width <= 1920 and height <= 1080:
                candidates.append((int(width), int(height), area))
    
    # If no candidates with minimum requirements, try with lower threshold for desktop only
    if not candidates and not is_mobile:
        for element in elements:
            if is_likely_icon_or_logo(element):
                continue
            
            rect = element.get("rect")
            if not rect:
                continue
            
            width = rect.get("width", 0)
            height = rect.get("height", 0)
            
            if width > 0 and height > 0:
                area = width * height
                
                if area > max_reasonable_size:
                    continue
                
                if width < 5 or height < 5:
                    continue
                
                # Lower threshold for desktop
                if width >= 50 and height >= 50 and width <= 1920 and height <= 1080:
                    candidates.append((int(width), int(height), area))
    
    # Return the largest candidate
    if candidates:
        candidates.sort(key=lambda x: x[2], reverse=True)  # Sort by area
        return (candidates[0][0], candidates[0][1])
    
    return None, None


# =============================================================================
# Creative processing
# =============================================================================


@dataclasses.dataclass
class CreativeDescriptor:
    s3_key: str
    creative_id: str
    user_segment: str
    platform: str
    creative_folder: str
    ad_payload: dict


def strip_prefix(key: str, prefix: str) -> str:
    if not prefix:
        return key
    if key.startswith(prefix):
        return key[len(prefix):]
    return key


# El nombre de la carpeta del creativo empieza siempre por "ANCHOxALTO-",
# p.ej. "728x90-leaderboard-caja-galletas-...".
_SCENARIO_SIZE_RE = re.compile(r"^(\d+)x(\d+)(?:-|$)", re.IGNORECASE)


def parse_size_from_scenario(creative_folder: Optional[str]) -> Optional[Tuple[int, int]]:
    """
    Extrae (ancho, alto) del prefijo "ANCHOxALTO" de la carpeta del
    creativo (p.ej. "728x90-leaderboard-..."). Devuelve None si no
    coincide el patrón o las dimensiones son inválidas.
    """
    if not creative_folder:
        return None
    match = _SCENARIO_SIZE_RE.match(creative_folder)
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def parse_creative_path(key: str, config: CrawlerConfig) -> Optional[Tuple[str, str, str]]:
    rel_key = strip_prefix(key, config.input_prefix or "")
    rel_key = rel_key.lstrip("/")
    parts = [p for p in rel_key.split("/") if p]
    if len(parts) < 4:
        return None
    user = parts[0]
    if parts[1] != "scenarios":
        return None
    platform = parts[2]
    creative_folder = parts[3]
    return user, platform, creative_folder


def build_output_key(
    config: CrawlerConfig, 
    descriptor: CreativeDescriptor, 
    device_type: str,
) -> str:
    segments = []
    if config.output_prefix:
        segments.append(config.output_prefix.strip("/"))
    segments.extend([
        descriptor.user_segment,
        descriptor.platform,
    ])
    
    # Build filename with device type only (size is now in JSON, not filename)
    filename_parts = [descriptor.creative_id, device_type]
    filename = "_".join(str(part) for part in filename_parts) + ".json"
    segments.append(filename)
    
    return "/".join(segment for segment in segments if segment)


def build_creative_manifest(
    config: CrawlerConfig,
    s3_client,
    log: ExecutionLog,
    keys: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> List[CreativeDescriptor]:
    manifest: Dict[Tuple[str, str, str], CreativeDescriptor] = {}
    key_iter = keys if keys is not None else iter_creative_objects(config, s3_client)
    progress_total = limit if (limit is not None and keys is None) else (len(keys) if isinstance(keys, list) else None)
    progress = Progress("Scanning creatives", total=progress_total, unit="creatives")
    try:
        for key in key_iter:
            if limit is not None and len(manifest) >= limit:
                break
            if not key or not key.endswith(".json"):
                continue
            path_info = parse_creative_path(key, config)
            if not path_info:
                progress.update()
                continue
            user, platform, creative_folder = path_info
            if config.allowed_users is not None and user not in config.allowed_users:
                progress.update()
                continue
            payload = download_json(config.input_bucket, key, s3_client)
            if not payload:
                log.info(f"Skipped empty or unreadable JSON at {key}")
                progress.update()
                continue
            creative_id = payload.get("creative")
            if not creative_id:
                log.info(f"Missing 'creative' field in {key}")
                progress.update()
                continue
            if creative_folder != creative_id:
                log.info(
                    f"Creative folder '{creative_folder}' differs from creative id '{creative_id}' in {key}; using creative id."
                )
            composite = (user, platform, creative_id)
            if composite in manifest:
                progress.update()
                continue
            manifest[composite] = CreativeDescriptor(
                s3_key=key,
                creative_id=creative_id,
                user_segment=user,
                platform=platform,
                creative_folder=creative_folder,
                ad_payload=payload,
            )
            progress.update()
            if limit is not None and len(manifest) >= limit:
                break
    finally:
        progress.close()
    return list(manifest.values())


def normalize_html(html_content: str) -> str:
    """
    Normaliza y formatea el HTML:
    1. Reemplaza caracteres escapados (\" por ")
    2. Verifica y agrega estructura HTML completa si falta
    3. Asegura que el HTML sea válido antes de guardarlo
    """
    if not html_content or not isinstance(html_content, str):
        return html_content
    
    # Paso 1: Reemplazar caracteres escapados
    normalized = html_content.replace('\\"', '"').replace("\\'", "'")
    
    # Paso 2: Verificar si tiene estructura HTML completa
    html_lower = normalized.lower().strip()
    has_doctype = html_lower.startswith("<!doctype")
    has_html_tag = "<html" in html_lower
    has_head_tag = "<head" in html_lower
    has_body_tag = "<body" in html_lower
    
    # Si ya tiene estructura completa, retornar normalizado
    if has_doctype and has_html_tag and has_head_tag and has_body_tag:
        return normalized
    
    # Paso 3: Extraer el contenido del body si existe
    body_content = normalized
    
    if has_body_tag:
        # Extraer contenido entre <body> y </body>
        body_start = html_lower.find("<body")
        if body_start != -1:
            body_tag_end = normalized.find(">", body_start) + 1
            body_end = html_lower.find("</body>", body_start)
            if body_end != -1:
                body_content = normalized[body_tag_end:body_end].strip()
            else:
                body_content = normalized[body_tag_end:].strip()
    elif has_html_tag:
        # Si tiene <html> pero no <body>, extraer contenido después de <html>
        html_start = html_lower.find("<html")
        if html_start != -1:
            html_tag_end = normalized.find(">", html_start) + 1
            html_end = html_lower.find("</html>", html_start)
            if html_end != -1:
                body_content = normalized[html_tag_end:html_end].strip()
            else:
                body_content = normalized[html_tag_end:].strip()
    
    # Paso 4: Construir HTML completo
    parts = []
    
    # DOCTYPE
    if has_doctype:
        doctype_end = normalized.find(">") + 1
        parts.append(normalized[:doctype_end])
    else:
        parts.append("<!DOCTYPE html>")
    
    # HTML tag de apertura
    if not has_html_tag:
        parts.append("<html lang=\"en\">")
    
    # Head
    if has_head_tag:
        # Preservar head existente
        head_start = html_lower.find("<head")
        if head_start != -1:
            head_end = html_lower.find("</head>", head_start)
            if head_end != -1:
                parts.append(normalized[head_start:head_end + 7])
    else:
        parts.append("<head>")
        parts.append("    <meta charset=\"UTF-8\">")
        parts.append("    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">")
        parts.append("    <title>Creative Ad</title>")
        parts.append("</head>")
    
    # Body
    if not has_body_tag:
        parts.append("<body>")
        parts.append(body_content)
        parts.append("</body>")
    else:
        # Preservar body existente
        body_start = html_lower.find("<body")
        if body_start != -1:
            body_end = html_lower.find("</body>", body_start)
            if body_end != -1:
                # Tiene cierre completo
                parts.append(normalized[body_start:body_end + 7])
            else:
                # No tiene cierre, agregar el contenido y cerrar
                body_tag_end = normalized.find(">", body_start) + 1
                body_inner = normalized[body_tag_end:].strip()
                parts.append(normalized[body_start:body_tag_end])
                parts.append(body_inner)
                parts.append("</body>")
    
    # Cerrar HTML tag si lo agregamos
    if not has_html_tag:
        # Verificar si ya hay un cierre </html> en el contenido original
        if "</html>" not in html_lower:
            parts.append("</html>")
    
    return "\n".join(parts)


def ensure_temp_html(ad_html: str, platform: str = "", device_type: str = "") -> Tuple[str, Optional[str]]:
    """
    Prepare HTML content for loading. For Meta platform on mobile, modify URL to force mobile version.
    Normalizes HTML content (unescapes characters, ensures proper structure) before saving.
    """
    ad_html = ad_html.strip()
    if ad_html.lower().startswith(("http://", "https://")):
        # For Meta platform on mobile, modify URL to force mobile version
        if platform.lower() in ("meta", "facebook", "instagram") and device_type == "mobile":
            url = ad_html
            try:
                parsed = urlparse(url)
                query_params = parse_qs(parsed.query)
                
                # Add mobile parameter if not present
                if "m" not in query_params:
                    query_params["m"] = ["1"]
                
                # For Facebook/Instagram, try to use mobile subdomain
                hostname = parsed.hostname.lower() if parsed.hostname else ""
                if "facebook.com" in hostname and not hostname.startswith("m."):
                    # Replace www.facebook.com or facebook.com with m.facebook.com
                    # Handle both www.facebook.com and facebook.com cases
                    if hostname.startswith("www."):
                        new_hostname = hostname.replace("www.facebook.com", "m.facebook.com", 1)
                    else:
                        # Direct facebook.com (without www)
                        new_hostname = "m.facebook.com"
                    # Rebuild netloc preserving port if present
                    if parsed.port:
                        new_netloc = f"{new_hostname}:{parsed.port}"
                    else:
                        new_netloc = new_hostname
                    parsed = parsed._replace(netloc=new_netloc)
                elif "instagram.com" in hostname and not hostname.startswith("m."):
                    # For Instagram, use m.instagram.com
                    if hostname.startswith("www."):
                        new_hostname = hostname.replace("www.instagram.com", "m.instagram.com", 1)
                    else:
                        # Direct instagram.com (without www)
                        new_hostname = "m.instagram.com"
                    # Rebuild netloc preserving port if present
                    if parsed.port:
                        new_netloc = f"{new_hostname}:{parsed.port}"
                    else:
                        new_netloc = new_hostname
                    parsed = parsed._replace(netloc=new_netloc)
                
                # Rebuild URL with mobile parameters
                new_query = urlencode(query_params, doseq=True)
                modified_url = urlunparse(parsed._replace(query=new_query))
                return modified_url, None
            except Exception:
                # If URL modification fails, return original URL
                return ad_html, None
        return ad_html, None
    
    # Normalize HTML before saving
    normalized_html = normalize_html(ad_html)
    
    tmp = tempfile.NamedTemporaryFile(prefix="creative_", suffix=".html", delete=False)
    tmp.write(normalized_html.encode("utf-8"))
    tmp.flush()
    tmp.close()
    path = Path(tmp.name).resolve()
    return f"file://{path}", str(path)


def wait_network_settled(
    driver,
    selector: str = "ins.adcads",
    min_dwell: float = 6.0,
    max_dwell: float = 25.0,
    idle_window: float = 2.0,
    poll: float = 0.25,
) -> float:
    """
    Scroll the ad into the viewport, then wait until network activity stops.

    Idle detection uses the Performance API resource entry count, which is
    visible even for cross-origin resources that lack Timing-Allow-Origin.
    A floor (min_dwell) ensures afterload pixels and viewability triggers fire;
    a ceiling (max_dwell) bounds Lambda cost for carousels that keep rotating.

    Returns the actual elapsed dwell time in seconds.
    """
    try:
        driver.execute_script(
            "const e=document.querySelector(arguments[0]);"
            "if(e){e.scrollIntoView({block:'center'});}",
            selector,
        )
    except Exception:
        pass

    start = time.monotonic()
    last_count, last_change = -1, start
    while True:
        now = time.monotonic()
        try:
            count = driver.execute_script(
                "return performance.getEntriesByType('resource').length;"
            )
        except Exception:
            count = last_count
        if count != last_count:
            last_count, last_change = count, now
        elapsed = now - start
        if elapsed >= max_dwell:
            break
        if elapsed >= min_dwell and (now - last_change) >= idle_window:
            break
        time.sleep(poll)
    return time.monotonic() - start


def measure_creative_single(
    driver,
    descriptor: CreativeDescriptor,
    config: CrawlerConfig,
    device_type: str,
    cookies: Optional[List[dict]] = None,
    log=None,
) -> Tuple[Optional[dict], Optional[str], Optional[int], Optional[int]]:
    """
    Measure a single creative for one device type.
    Returns (har, html_content, ad_width, ad_height).
    """
    ad_html = descriptor.ad_payload.get("adHTML")
    if not isinstance(ad_html, str) or not ad_html.strip():
        return None, None, None, None

    # For Meta platform on mobile, modify URL to force mobile version
    target_url, temp_path = ensure_temp_html(ad_html, platform=descriptor.platform, device_type=device_type)
    # Always use selenium-wire collector
    with contextlib.suppress(Exception):
        if hasattr(driver, "requests"):
            driver.requests.clear()
    collector = SeleniumWireCollector(driver, config)
    html_content = None
    ad_width, ad_height = None, None
    try:
        # Limpiar siempre cache, pero solo limpiar cookies cuando NO usamos cookies externas
        with contextlib.suppress(Exception):
            driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            if not (cookies and descriptor.platform.lower() == "youtube"):
                driver.execute_cdp_cmd("Network.clearBrowserCookies", {})

        # Inyectar cookies para YouTube si están configuradas
        if cookies and descriptor.platform.lower() == "youtube":
            try:
                youtube_utils.inject_cookies(driver, cookies, base_url="https://www.youtube.com")
                if log:
                    log.info(
                        f"Inyectadas cookies de YouTube para creative={descriptor.creative_id} ({device_type})"
                    )
            except Exception as exc:
                if log:
                    log.info(
                        f"Fallo al inyectar cookies de YouTube para creative={descriptor.creative_id} ({device_type}): {exc}"
                    )

        try:
            driver.get(target_url)
        except TimeoutException as exc:
            # En algunos casos (especialmente YouTube con login/cookies),
            # la navegación puede tardar más que page_load_timeout.
            # Consideramos aceptable continuar y capturar lo que haya cargado.
            if log:
                log.info(
                    f"Page load timeout for {descriptor.creative_id} ({device_type}); "
                    f"continuing with partial load: {exc}"
                )
        
        # Wait for network to settle (scroll ad into viewport, then idle-detect).
        # warmup_seconds is the minimum dwell floor; capture_seconds is the ceiling.
        actual_dwell = wait_network_settled(
            driver,
            min_dwell=max(config.warmup_seconds, 6.0),
            max_dwell=max(config.capture_seconds, config.warmup_seconds + 6.0),
        )

        html_content = driver.page_source
        
        # Check if page loaded successfully (not an error page)
        page_loaded = True
        if html_content:
            html_lower = html_content.lower()
            # Check for common error indicators
            if any(error in html_lower for error in ["502 bad gateway", "503 service unavailable", "404 not found", "500 internal server error", "protocolException", "name or service not known"]):
                page_loaded = False
                if log:
                    log.info(f"Page load error detected for {descriptor.creative_id} ({device_type}): URL may be invalid or server unreachable")
        
        dom_snapshot = None
        if page_loaded:
            dom_snapshot = collect_dom_snapshot(driver)
        
        har = collector.build_har(page_title=descriptor.creative_id)
        if dom_snapshot:
            har.setdefault("log", {})["_domSnapshot"] = dom_snapshot

        # La carpeta del creativo empieza por "ANCHOxALTO-" (p.ej.
        # "728x90-leaderboard-..."), así que usamos esos valores y nos
        # saltamos el análisis del DOM.
        forced_size = parse_size_from_scenario(descriptor.creative_folder)
        if forced_size is not None:
            ad_width, ad_height = forced_size
            har.setdefault("log", {})["_adSize"] = {
                "width": ad_width,
                "height": ad_height,
                "deviceType": device_type,
                "source": "scenario_name",
            }
            if log:
                log.info(
                    f"Using ad size {ad_width}x{ad_height} from scenario name "
                    f"for {descriptor.creative_id} ({device_type})"
                )
        elif dom_snapshot:
            # Extract ad size (pass device_type to apply appropriate thresholds)
            ad_width, ad_height = extract_ad_size(dom_snapshot, driver, device_type)
            if ad_width and ad_height:
                # Validate size for mobile (reject if too small)
                if device_type.lower() == "mobile" and (ad_width < 100 or ad_height < 100):
                    if log:
                        log.info(f"Rejected small size {ad_width}x{ad_height} for {descriptor.creative_id} ({device_type}) - likely icon/logo")
                    ad_width, ad_height = None, None
                else:
                    har.setdefault("log", {})["_adSize"] = {"width": ad_width, "height": ad_height, "deviceType": device_type}
            elif log:
                log.info(f"Could not extract ad size for {descriptor.creative_id} ({device_type}) - DOM snapshot available but no ad element found")
        elif log:
            log.info(f"No DOM snapshot available for {descriptor.creative_id} ({device_type}) - page may not have loaded correctly")
        
        return har, html_content, ad_width, ad_height
    finally:
        if temp_path:
            with contextlib.suppress(Exception):
                os.unlink(temp_path)


def process_creatives(
    config: CrawlerConfig,
    s3_client,
    log,
    headless: bool,
    keys: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    local_output_dir: Optional[Path] = None,
) -> int:
    # Cargar, si existe, JSON de cookies de YouTube en la raíz del bucket de input
    youtube_cookies: Optional[List[dict]] = youtube_utils.load_youtube_cookies(config, s3_client, log)

    manifest = build_creative_manifest(
        config,
        s3_client,
        log,
        keys=keys,
        limit=limit,
    )
    if not manifest:
        log.info("No creatives found to process.")
        return 0

    pending: List[CreativeDescriptor] = []
    for descriptor in manifest:
        # Check if both desktop and mobile outputs exist
        desktop_key = build_output_key(config, descriptor, "desktop")
        mobile_key = build_output_key(config, descriptor, "mobile")
        
        if local_output_dir is None:
            desktop_exists = object_exists(config.output_bucket, desktop_key, s3_client)
            mobile_exists = object_exists(config.output_bucket, mobile_key, s3_client)
        else:
            desktop_path = Path(local_output_dir) / Path(desktop_key)
            mobile_path = Path(local_output_dir) / Path(mobile_key)
            desktop_exists = desktop_path.exists()
            mobile_exists = mobile_path.exists()
        
        # Only add if at least one is missing
        if not desktop_exists or not mobile_exists:
            pending.append(descriptor)

    effective_limit = limit if limit is not None else config.max_creatives_per_run
    if effective_limit is not None:
        pending = pending[: effective_limit]

    if not pending:
        log.info("All creatives already measured; nothing to do.")
        return 0

    processed = 0
    crawl_progress = Progress("Capturing creatives", total=len(pending) * 2, unit="captures")
    
    # Device configurations
    device_configs = [
        ("desktop", DESKTOP_USER_AGENT, DESKTOP_VIEWPORT),
        ("mobile", MOBILE_USER_AGENT, MOBILE_VIEWPORT),
    ]
    
    try:
        for descriptor in pending:
            for device_type, user_agent, viewport in device_configs:
                device_started_at = time.monotonic()
                try:
                    # Check if this specific device output already exists
                    output_key = build_output_key(config, descriptor, device_type)
                    if local_output_dir is None:
                        if object_exists(config.output_bucket, output_key, s3_client):
                            crawl_progress.update()
                            continue
                    else:
                        local_path = Path(local_output_dir) / Path(output_key)
                        if local_path.exists():
                            crawl_progress.update()
                            continue

                    log.info(
                        f"Starting {device_type} capture for creative={descriptor.creative_id}"
                    )

                    # Create session for this device type
                    with SeleniumSession(config, log, headless=headless, user_agent=user_agent, viewport=viewport) as driver:
                        har, html_content, ad_width, ad_height = measure_creative_single(
                            driver,
                            descriptor,
                            config,
                            device_type,
                            cookies=youtube_cookies,
                            log=log,
                        )
                        
                        if not har:
                            log.info(f"Skipped creative {descriptor.creative_id} ({device_type}): missing adHTML content.")
                            crawl_progress.update()
                            continue

                        # Extraer ytInitialPlayerResponse del HTML para YouTube
                        player_response: Optional[dict] = None
                        if html_content and descriptor.platform.lower() == "youtube":
                            player_response = youtube_utils.extract_initial_player_response(
                                html_content
                            )
                            if player_response:
                                # Guardar un resumen compacto en el HAR
                                summary = youtube_utils.build_player_response_summary(
                                    player_response
                                )
                                har.setdefault("log", {})[
                                    "_youtubeInitialPlayerResponse"
                                ] = summary
                            elif log:
                                log.info(
                                    f"No ytInitialPlayerResponse found for {descriptor.creative_id} ({device_type})"
                                )

                        # Enrich HAR with metadata (width, height, payload_media, payload_total)
                        har = enrich_har_with_metadata(
                            har,
                            ad_width,
                            ad_height,
                            platform=descriptor.platform,
                            player_response=player_response,
                        )
                        
                        # Build output key (no longer includes size in filename)
                        output_key = build_output_key(config, descriptor, device_type)
                        
                        if local_output_dir is not None:
                            local_path = Path(local_output_dir) / Path(output_key)
                            local_path.parent.mkdir(parents=True, exist_ok=True)
                            local_path.write_text(
                                json.dumps(har, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            log.info(
                                f"Captured HAR for user={descriptor.user_segment} platform={descriptor.platform} creative={descriptor.creative_id} device={device_type} size={ad_width}x{ad_height if ad_height else 'unknown'} -> {local_path}"
                            )
                            if html_content:
                                html_output_key = str(Path(output_key).with_suffix('.html'))
                                html_local_path = Path(local_output_dir) / Path(html_output_key)
                                html_local_path.parent.mkdir(parents=True, exist_ok=True)
                                html_local_path.write_text(html_content, encoding="utf-8")
                                log.info(
                                    f"Saved HTML for creative={descriptor.creative_id} device={device_type} -> {html_local_path}"
                                )
                        else:
                            upload_json(config.output_bucket, output_key, har, s3_client)
                            log.info(
                                f"Captured HAR for user={descriptor.user_segment} platform={descriptor.platform} creative={descriptor.creative_id} device={device_type} size={ad_width}x{ad_height if ad_height else 'unknown'} -> {output_key}"
                            )
                            if html_content:
                                html_output_key = str(Path(output_key).with_suffix('.html'))
                                s3_client.put_object(
                                    Bucket=config.output_bucket,
                                    Key=html_output_key,
                                    Body=html_content.encode("utf-8"),
                                    ContentType="text/html; charset=utf-8",
                                )
                                log.info(
                                    f"Saved HTML for creative={descriptor.creative_id} device={device_type} -> s3://{config.output_bucket}/{html_output_key}"
                                )
                        processed += 1
                except Exception as exc:
                    log.info(f"Failed to capture HAR for {descriptor.creative_id} ({device_type}): {exc}")
                finally:
                    elapsed = time.monotonic() - device_started_at
                    log.info(
                        f"Finished {device_type} capture for creative={descriptor.creative_id} "
                        f"in {elapsed:.1f}s"
                    )
                    # Volcar el log tras cada device para no perder trazas si
                    # la Lambda muere por timeout u OOM durante la siguiente
                    # captura.
                    with contextlib.suppress(Exception):
                        log.flush()
                    crawl_progress.update()
    finally:
        crawl_progress.close()
    return processed


# =============================================================================
# Entry points
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure creative traffic and store HAR files in S3.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless regardless of config default.")
    parser.add_argument(
        "--test-limit",
        type=int,
        default=None,
        help="Process only the first N creatives (useful for quick tests).",
    )
    parser.add_argument(
        "--test-output-dir",
        default=None,
        help="Directory to store HARs/logs when running in test mode (default: ./test_output).",
    )
    args = parser.parse_args()

    config = CrawlerConfig.from_env()
    s3_client = boto3.client("s3")
    test_mode = args.test_limit is not None
    local_output_dir: Optional[Path] = None
    if test_mode:
        base_dir = args.test_output_dir or "test_output"
        local_output_dir = Path(base_dir).expanduser().resolve()
        log = LocalExecutionLog(local_output_dir / "crawler.log")
    else:
        log = ExecutionLog(config.output_bucket, config.log_key, s3_client)

    try:
        headless = args.headless or config.default_headless
        processed = process_creatives(
            config,
            s3_client,
            log,
            headless=headless,
            limit=args.test_limit,
            local_output_dir=local_output_dir,
        )
        log.info(f"Processed creatives: {processed}")
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        log.flush()


def extract_keys_from_event(event: dict, config: CrawlerConfig) -> List[str]:
    keys: List[str] = []
    for record in event.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            continue
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        if bucket != config.input_bucket:
            continue
        raw_key = s3_info.get("object", {}).get("key")
        if not raw_key:
            continue
        key = unquote_plus(raw_key)
        # If users are restricted, only include keys for allowed users
        if config.allowed_users is not None:
            path_info = parse_creative_path(key, config)
            if not path_info:
                continue
            user, _, _ = path_info
            if user not in config.allowed_users:
                continue
        keys.append(key)
    return keys


if __name__ == "__main__":
    main()
