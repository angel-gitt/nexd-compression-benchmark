#!/usr/bin/env python3
"""
crawl_ads.py — CDP-native HAR capture (no selenium-wire).

Captura HAR con timestamps por petición usando Chrome DevTools Protocol.
Equivalente a la lógica de la lambda actualizada (CdpHarCollector) pero con
timing real por request (Network.requestWillBeSent / loadingFinished).

Uso:
  python3 scripts/crawl_ads.py --html-dir ads/nexd_html --ad-type nexd --output-dir hars_ads
  python3 scripts/crawl_ads.py --html-dir ads/html5     --ad-type html5 --output-dir hars_ads
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException

try:
    import brotli
except ImportError:
    brotli = None

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAS_WDM = True
except ImportError:
    _HAS_WDM = False

MIN_DWELL_S = 8.0
MAX_DWELL_S = 25.0
IDLE_WINDOW_S = 2.0
POLL_S = 0.25
VIEWPORT = "1920,1080"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _build_driver(headless: bool):
    opts = Options()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--enable-gpu-rasterization")
    opts.add_argument("--enable-oop-rasterization")
    opts.add_argument("--ignore-gpu-blocklist")
    opts.add_argument("--enable-webgl")
    opts.add_argument("--enable-webgl2")
    opts.add_argument("--enable-accelerated-2d-canvas")
    opts.add_argument("--use-gl=angle")
    opts.add_argument(f"--window-size={VIEWPORT}")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_argument("--disable-application-cache")
    opts.add_argument("--aggressive-cache-discard")
    opts.add_argument("--disable-cache")
    opts.add_argument("--disk-cache-size=0")
    opts.add_argument("--disable-background-networking")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if headless:
        opts.add_argument("--headless=new")

    profile_dir = Path(tempfile.mkdtemp(prefix="crawl_ads_profile_"))
    opts.add_argument(f"--user-data-dir={profile_dir}")

    service = None
    if _HAS_WDM:
        try:
            service = Service(ChromeDriverManager().install())
        except Exception:
            pass

    driver = webdriver.Chrome(options=opts, service=service) if service else webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(60)
    with contextlib.suppress(Exception):
        driver.execute_cdp_cmd("Network.enable", {"maxResourceBufferSize": 0, "maxTotalBufferSize": 0})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
    return driver, profile_dir


def _wait_network_settled(driver, min_dwell: float, max_dwell: float) -> float:
    with contextlib.suppress(Exception):
        driver.execute_script(
            "const e = document.querySelector('ins.adcads');"
            "if (e) { e.scrollIntoView({block:'center'}); }"
        )
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
        if elapsed >= min_dwell and (now - last_change) >= IDLE_WINDOW_S:
            break
        time.sleep(POLL_S)
    return time.monotonic() - start


def _build_har_from_cdp(driver, page_title: str) -> dict:
    """
    Build HAR from CDP performance logs with per-request timestamps.
    Captures ALL network requests (including cross-origin iframes, SW, etc.)
    """
    UTC = dt.timezone.utc
    anchor = dt.datetime.now(UTC)

    rid_map: Dict[str, dict] = {}

    try:
        logs = driver.get_log("performance")
    except Exception:
        logs = []

    for entry in logs:
        try:
            msg = json.loads(entry.get("message", "{}")).get("message", {})
            method = msg.get("method", "")
            params = msg.get("params", {})
            rid = params.get("requestId")
            if not rid:
                continue

            if method == "Network.requestWillBeSent":
                if rid not in rid_map:
                    req = params.get("request", {})
                    ts_ms = params.get("timestamp", 0) * 1000.0
                    rid_map[rid] = {
                        "url": req.get("url", ""),
                        "method": req.get("method", "GET"),
                        "status": 0,
                        "mime": "",
                        "transferSize": 0,
                        "start_ms": ts_ms,
                        "end_ms": -1.0,
                        "headers": {},
                        "resourceType": params.get("type", ""),
                    }

            elif method == "Network.responseReceived":
                if rid not in rid_map:
                    rid_map[rid] = {
                        "url": "", "method": "GET", "status": 0, "mime": "",
                        "transferSize": 0, "start_ms": -1.0, "end_ms": -1.0,
                        "headers": {}, "resourceType": "",
                    }
                resp = params.get("response", {})
                rid_map[rid]["url"] = resp.get("url", "") or rid_map[rid]["url"]
                rid_map[rid]["status"] = resp.get("status", 0)
                rid_map[rid]["mime"] = resp.get("mimeType", "")
                rid_map[rid]["headers"] = resp.get("headers", {})
                ts_ms = params.get("timestamp", 0) * 1000.0
                if rid_map[rid]["start_ms"] < 0:
                    rid_map[rid]["start_ms"] = ts_ms

            elif method == "Network.loadingFinished":
                enc = params.get("encodedDataLength", -1)
                ts_ms = params.get("timestamp", 0) * 1000.0
                if rid in rid_map:
                    rid_map[rid]["end_ms"] = ts_ms
                    if enc >= 0:
                        rid_map[rid]["transferSize"] = int(enc)

        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    # Find earliest CDP timestamp for relative offsets
    all_starts = [v["start_ms"] for v in rid_map.values() if v["start_ms"] > 0]
    cdp_t0 = min(all_starts) if all_starts else 0.0

    # Convert CDP timestamp (seconds since epoch) to ISO datetime
    # CDP timestamps are in seconds since the Chrome epoch
    def _ms_to_iso(ms: float) -> str:
        if ms <= 0:
            return anchor.isoformat().replace("+00:00", "Z")
        try:
            d = dt.datetime.fromtimestamp(ms / 1000.0, tz=UTC)
            return d.isoformat().replace("+00:00", "Z")
        except Exception:
            return anchor.isoformat().replace("+00:00", "Z")

    entries: List[dict] = []
    for rid, info in rid_map.items():
        url = info["url"]
        if not (url.startswith("http://") or url.startswith("https://")):
            continue

        ts = info["transferSize"]
        mime = info["mime"]
        start_iso = _ms_to_iso(info["start_ms"])

        total_ms = -1.0
        if info["start_ms"] > 0 and info["end_ms"] > 0:
            total_ms = max(0.0, info["end_ms"] - info["start_ms"])

        if total_ms > 0:
            send_ms = min(5.0, total_ms * 0.05)
            wait_ms = total_ms * 0.40
            receive_ms = max(1.0, total_ms - send_ms - wait_ms)
        else:
            send_ms = wait_ms = receive_ms = -1.0

        # Try to get response body for decoded size (text resources only)
        decoded_body_size = ts  # default: assume no compression
        headers_lower = {k.lower(): v for k, v in info.get("headers", {}).items()}
        encoding = headers_lower.get("content-encoding", "").lower().strip()

        if info["resourceType"] in ("Script", "Stylesheet", "Document", "XHR", "Fetch") or mime.startswith("text/") or "javascript" in mime:
            try:
                body_result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
                body_text = body_result.get("body", "")
                is_base64 = body_result.get("base64Encoded", False)
                if is_base64:
                    import base64
                    raw = base64.b64decode(body_text)
                else:
                    raw = body_text.encode("utf-8")
                # If compressed, decode
                if encoding == "br" and brotli:
                    try:
                        raw = brotli.decompress(raw)
                    except Exception:
                        pass
                elif encoding == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                elif encoding == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except Exception:
                        pass
                decoded_body_size = len(raw)
            except Exception:
                pass

        content_obj = {"size": decoded_body_size, "mimeType": mime}
        if decoded_body_size > 0 and ts > 0 and decoded_body_size != ts:
            content_obj["compression"] = decoded_body_size - ts

        body_size = ts if ts > 0 else decoded_body_size

        entries.append({
            "pageref": page_title,
            "startedDateTime": start_iso,
            "time": round(total_ms, 3) if total_ms >= 0 else -1,
            "timings": {
                "send": round(send_ms, 3),
                "wait": round(wait_ms, 3),
                "receive": round(receive_ms, 3),
            },
            "request": {"method": info["method"], "url": url, "httpVersion": "HTTP/1.1"},
            "response": {
                "status": info["status"],
                "httpVersion": "HTTP/1.1",
                "content": content_obj,
                "bodySize": body_size,
                "decodedBodySize": decoded_body_size,
                "transferSize": ts,
                "_transferSize": ts,
                "_decodedBodySize": decoded_body_size,
                "bodyCaptured": True,
            },
        })

    page_start = anchor.isoformat().replace("+00:00", "Z")
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "crawl_ads_cdp", "version": "2.0"},
            "pages": [{
                "startedDateTime": page_start,
                "id": page_title,
                "title": page_title,
                "pageTimings": {},
            }],
            "entries": entries,
        }
    }


# ── ad discovery ───────────────────────────────────────────────────────────────

def _find_html_in_dir(d: Path) -> Optional[Path]:
    if not d.is_dir():
        return None
    for candidate in sorted(d.rglob("index.html")):
        if "__MACOSX" not in candidate.parts:
            return candidate
    for candidate in sorted(d.rglob("*.html")):
        if "__MACOSX" not in candidate.parts:
            return candidate
    return None


def _extract_zips_in_dir(d: Path) -> None:
    for z in sorted(d.glob("*.zip")):
        dest = d / z.stem
        if dest.exists():
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                zf.extractall(dest)
        except Exception as exc:
            print(f"  [warn] could not extract {z.name}: {exc}", file=sys.stderr)


def find_ads(html_dir: Path, ad_type: str) -> List[Tuple[str, Path]]:
    ads: List[Tuple[str, Path]] = []
    if ad_type == "nexd":
        for f in sorted(html_dir.glob("*.html")):
            ads.append((f.stem, f))
        return ads
    for d in sorted(html_dir.iterdir()):
        if not d.is_dir() or d.name == "__MACOSX":
            continue
        _extract_zips_in_dir(d)
        html_path = _find_html_in_dir(d)
        if html_path:
            ads.append((d.name, html_path))
        else:
            print(f"  [warn] no HTML found in {d.name}, skipping", file=sys.stderr)
    return ads


# ── main crawl loop ────────────────────────────────────────────────────────────

def _add_file_entries(har: dict, file_url: str) -> None:
    """Add entries for local file:// resources that CDP doesn't capture."""
    if not file_url.startswith("file://"):
        return
    from urllib.parse import urlparse, unquote
    path = Path(unquote(urlparse(file_url).path))
    base_dir = path.parent
    if not base_dir.is_dir():
        return

    # Find earliest HTTP entry timestamp to use as base
    entries = har["log"]["entries"]
    existing_urls = {e["request"]["url"] for e in entries}
    http_times = []
    for e in entries:
        if not e["request"]["url"].startswith("file://"):
            ts = e.get("startedDateTime", "")
            if ts:
                http_times.append(ts)
    base_ts = min(http_times) if http_times else entries[0]["startedDateTime"]

    mime_db = mimetypes.MimeTypes()
    idx = 0
    for f in sorted(base_dir.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        url = f"file://{f.resolve()}"
        if url in existing_urls:
            continue
        size = f.stat().st_size
        if size == 0:
            continue
        mime_type, _ = mime_db.guess_type(str(f))
        if not mime_type:
            mime_type = "application/octet-stream"
        # Space entries slightly in time so schedule doesn't put everything at t=0
        offset_ms = (idx * 0.5)
        ts_s = offset_ms / 1000.0
        send_ms = min(5.0, ts_s * 10)
        wait_ms = ts_s * 100
        receive_ms = max(1.0, ts_s * 50)
        entries.append({
            "pageref": har["log"]["pages"][0]["id"],
            "startedDateTime": base_ts,
            "time": max(1.0, offset_ms),
            "timings": {"send": send_ms, "wait": wait_ms, "receive": receive_ms},
            "request": {"method": "GET", "url": url, "httpVersion": "HTTP/1.1"},
            "response": {
                "status": 200,
                "httpVersion": "HTTP/1.1",
                "content": {"size": size, "mimeType": mime_type},
                "bodySize": size,
                "decodedBodySize": size,
                "_transferSize": -1,
                "transferSize": -1,
            },
        })
        idx += 1


def crawl_one(driver, url: str, creative_id: str) -> Optional[dict]:
    with contextlib.suppress(Exception):
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
    try:
        driver.get(url)
    except TimeoutException:
        pass
    except Exception as exc:
        print(f"  [skip] navigation error for {creative_id}: {exc}", file=sys.stderr)
        return None
    _wait_network_settled(driver, MIN_DWELL_S, MAX_DWELL_S)
    har = _build_har_from_cdp(driver, page_title=creative_id)
    _add_file_entries(har, url)
    return har


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl HTML ads and capture HARs via CDP.")
    ap.add_argument("--html-dir", required=True)
    ap.add_argument("--ad-type", required=True, choices=["nexd", "html5"])
    ap.add_argument("--output-dir", default="hars")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--server-url", default=None)
    args = ap.parse_args()

    html_dir = Path(args.html_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    ads = find_ads(html_dir, args.ad_type)
    if not ads:
        print(f"No ads found in {html_dir} for type '{args.ad_type}'")
        sys.exit(1)

    if args.limit is not None:
        ads = ads[: args.limit]

    print(f"Found {len(ads)} {args.ad_type} ads in {html_dir}")
    print(f"Output directory: {output_dir}")

    succeeded = failed = 0
    for i, (creative_id, html_path) in enumerate(ads, 1):
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in creative_id)[:80]
        out_filename = f"{args.ad_type}__{safe_id}.json"
        out_path = output_dir / out_filename

        if args.skip_existing and out_path.exists():
            print(f"[{i}/{len(ads)}] skip (exists): {out_filename}")
            succeeded += 1
            continue

        if args.server_url:
            try:
                rel_path = html_path.relative_to(html_dir.parent)
                target_url = f"{args.server_url.rstrip('/')}/{rel_path.as_posix()}"
            except Exception:
                target_url = f"file://{html_path}"
        else:
            target_url = f"file://{html_path}"

        print(f"[{i}/{len(ads)}] crawling {args.ad_type}/{creative_id} ...", end=" ", flush=True)

        driver = None
        profile_dir = None
        try:
            driver, profile_dir = _build_driver(headless=args.headless)
            har = crawl_one(driver, target_url, creative_id=safe_id)
            if har is None:
                print("FAILED (no HAR)")
                failed += 1
                continue
            n_entries = len(har.get("log", {}).get("entries", []))
            total_bytes = sum(
                e.get("response", {}).get("_transferSize", 0) or 0
                for e in har.get("log", {}).get("entries", [])
            )
            decoded_bytes = sum(
                e.get("response", {}).get("decodedBodySize", 0) or 0
                for e in har.get("log", {}).get("entries", [])
            )
            print(f"OK ({n_entries} entries, {total_bytes/1e3:.1f} KB wire / {decoded_bytes/1e3:.1f} KB decoded)")
            out_path.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
            succeeded += 1
        except Exception as exc:
            print(f"FAILED ({exc})")
            failed += 1
        finally:
            if driver:
                with contextlib.suppress(Exception):
                    driver.quit()
            if profile_dir:
                shutil.rmtree(profile_dir, ignore_errors=True)

    print(f"\nDone: {succeeded} succeeded, {failed} failed")


if __name__ == "__main__":
    main()
