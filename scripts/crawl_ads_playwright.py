#!/usr/bin/env python3
"""
crawl_ads_playwright.py

Crawls a directory of HTML ad files (nexd or standard HTML5) and captures
network traffic as HAR files using Playwright's native HAR recording.

Playwright records HARs directly via Chromium CDP — no MITM proxy required.
This means HTTPS resources are captured correctly, and the recorded HAR contains:
  - response._transferSize / _transferSize : actual on-wire encoded (compressed) bytes
    (from Network.loadingFinished.encodedDataLength)
  - response.content.size                  : decompressed body size
  - response.content.compression           : savings from compression (size - transferSize)

Usage:
    python3 scripts/crawl_ads_playwright.py \
        --html-dir ads/nexd_html \
        --ad-type nexd \
        --output-dir hars_ads_v2

    python3 scripts/crawl_ads_playwright.py \
        --html-dir ads/html5 \
        --ad-type html5 \
        --output-dir hars_ads_v2

Requirements:
    pip install playwright
    python3 -m playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

MIN_DWELL_S  = 8.0    # minimum page dwell
MAX_DWELL_S  = 25.0   # ceiling
IDLE_WINDOW_S = 2.0   # declare idle after 2 s without new resource
POLL_S       = 0.25
VIEWPORT     = {"width": 1920, "height": 1080}
USER_AGENT   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _wait_network_settled(page, start: float) -> float:
    """Poll performance.getEntriesByType('resource') until idle."""
    last_count = -1
    last_change = start
    while True:
        now = time.monotonic()
        try:
            count = page.evaluate("performance.getEntriesByType('resource').length")
        except Exception:
            count = last_count
        if count != last_count:
            last_count, last_change = count, now
        elapsed = now - start
        if elapsed >= MAX_DWELL_S:
            break
        if elapsed >= MIN_DWELL_S and (now - last_change) >= IDLE_WINDOW_S:
            break
        time.sleep(POLL_S)
    return time.monotonic() - start


def _patch_har_compression(har_path: Path) -> None:
    """
    Playwright's recorded HAR may already include correct _transferSize /
    content.size pairs. This function verifies and adds content.compression
    where missing (Playwright sometimes omits it even when sizes differ).
    """
    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)
    modified = False
    for entry in har.get("log", {}).get("entries", []):
        resp = entry.get("response", {}) or {}
        content = resp.get("content", {}) or {}
        # _transferSize is the compressed wire size set by Playwright from CDP
        transfer_size = resp.get("_transferSize", -1)
        content_size  = content.get("size", -1)
        if (transfer_size >= 0 and content_size > 0
                and content_size != transfer_size
                and "compression" not in content):
            content["compression"] = content_size - transfer_size
            resp["content"] = content
            entry["response"] = resp
            modified = True
    if modified:
        with har_path.open("w", encoding="utf-8") as f:
            json.dump(har, f)


def crawl_one(html_path: Path, ad_type: str, output_dir: Path,
              ad_name: str, timeout_ms: int = 60_000,
              server_url: Optional[str] = None, html_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Open html_path in a Playwright Chromium browser (or via HTTP server if server_url is provided),
    record a HAR, and save it to output_dir / f"{ad_type}__{ad_name}.json".
    Returns the saved HAR path, or None on failure.
    """
    from playwright.sync_api import sync_playwright, Error as PWError

    out_name = f"{ad_type}__{ad_name}.json"
    dest = output_dir / out_name
    tmp_har = Path(tempfile.mktemp(suffix=".har"))

    if server_url and html_dir:
        try:
            rel_path = html_path.relative_to(html_dir.parent)
            url = f"{server_url.rstrip('/')}/{rel_path.as_posix()}"
        except Exception as exc:
            print(f" [warn] could not construct server URL, using local file: {exc}")
            url = html_path.as_uri()
    else:
        url = html_path.as_uri()  # file:///...

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--enable-gpu-rasterization",
                "--enable-oop-rasterization",
                "--ignore-gpu-blocklist",
                "--enable-webgl",
                "--enable-webgl2",
                "--enable-accelerated-2d-canvas",
                "--use-gl=angle",
                "--disable-application-cache",
                "--aggressive-cache-discard",
                "--disable-cache",
                "--disk-cache-size=0",
                # keep background networking OFF so we only see ad traffic
                "--disable-background-networking",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            record_har_path=str(tmp_har),
            record_har_content="omit",
            record_har_mode="full",
            ignore_https_errors=True,
        )
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PWError as e:
            # timeout on heavy ads is fine; we still get partial capture
            pass

        start = time.monotonic()
        _wait_network_settled(page, start)

        context.close()
        browser.close()

    if not tmp_har.exists() or tmp_har.stat().st_size < 10:
        return None

    _patch_har_compression(tmp_har)
    shutil.move(str(tmp_har), str(dest))
    return dest


def _find_html_files(html_dir: Path, ad_type: str):
    """
    nexd: each .html file is one ad.
    html5: each subdirectory contains index.html (one ad per folder).
    """
    if ad_type == "nexd":
        return sorted(html_dir.glob("*.html"))
    # html5
    files = []
    for idx_path in sorted(html_dir.rglob("index.html")):
        files.append(idx_path)
    if not files:
        # fallback: flat html files
        files = sorted(html_dir.glob("*.html"))
    return files


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Crawl ad HTML files and record HAR with Playwright"
    )
    ap.add_argument("--html-dir", required=True,
                    help="Directory containing ad HTML files")
    ap.add_argument("--ad-type", required=True, choices=["nexd", "html5"],
                    help="Ad format type")
    ap.add_argument("--output-dir", required=True,
                    help="Output directory for HAR files")
    ap.add_argument("--timeout", type=int, default=60,
                    help="Per-ad page load timeout in seconds (default: 60)")
    ap.add_argument("--server-url", default=None,
                    help="HTTP server URL where the ads directory is hosted (e.g. http://192.168.1.135:8000)")
    args = ap.parse_args()

    html_dir  = Path(args.html_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = _find_html_files(html_dir, args.ad_type)
    if not files:
        sys.exit(f"No HTML files found in {html_dir}")

    print(f"Found {len(files)} {args.ad_type} ads in {html_dir}")
    print(f"Output directory: {output_dir}")
    if args.server_url:
        print(f"Using HTTP server: {args.server_url}")

    ok = fail = 0
    for i, html_path in enumerate(files, 1):
        if args.ad_type == "nexd":
            ad_name = html_path.stem
        else:
            # html5: use the parent directory name as the ad name
            ad_name = html_path.parent.name if html_path.name == "index.html" else html_path.stem

        out_path = output_dir / f"{args.ad_type}__{ad_name}.json"
        if out_path.exists():
            print(f"[{i}/{len(files)}] skip {args.ad_type}/{ad_name} (exists)")
            ok += 1
            continue

        print(f"[{i}/{len(files)}] crawling {args.ad_type}/{ad_name} ...", end=" ", flush=True)
        try:
            result = crawl_one(
                html_path, args.ad_type, output_dir, ad_name,
                timeout_ms=args.timeout * 1000,
                server_url=args.server_url,
                html_dir=html_dir
            )
            if result:
                # Quick stats
                har = json.load(result.open())
                ents = har["log"]["entries"]
                total_tx = sum(e.get("response", {}).get("_transferSize") or 0 for e in ents)
                total_cs = sum((e.get("response", {}).get("content") or {}).get("size") or 0 for e in ents)
                comp_ents = sum(1 for e in ents if (e.get("response",{}).get("content") or {}).get("compression"))
                print(f"ok — {len(ents)} entries, {total_tx/1e3:.1f} kB wire / {total_cs/1e3:.1f} kB content ({comp_ents} compressed)")
                ok += 1
            else:
                print("EMPTY")
                fail += 1
        except Exception as exc:
            print(f"ERROR: {exc}")
            fail += 1

    print(f"\nDone: {ok} ok, {fail} failed → {output_dir}")


if __name__ == "__main__":
    main()
