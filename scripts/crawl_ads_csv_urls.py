#!/usr/bin/env python3
"""
crawl_ads_csv_urls.py

Crawls a CSV of (ad_id;url) pairs pointing at live ad-preview URLs (e.g. DV360
"adspreview.googleusercontent.com" previews) and records one HAR per ad with
Playwright, the same way crawl_ads_playwright.py does for local HTML files.

Usage:
    python3 scripts/crawl_ads_csv_urls.py \
        --csv /path/to/ads_html5_hiili.csv \
        --ad-type html5hiili \
        --output-dir hars_ads
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

MIN_DWELL_S = 8.0
MAX_DWELL_S = 25.0
IDLE_WINDOW_S = 2.0
POLL_S = 0.25
VIEWPORT = {"width": 1920, "height": 1080}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _wait_network_settled(page, start: float) -> float:
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
    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)
    modified = False
    for entry in har.get("log", {}).get("entries", []):
        resp = entry.get("response", {}) or {}
        content = resp.get("content", {}) or {}
        # Playwright HAR: content.size = decoded size, _transferSize = wire size
        content_size = content.get("size", -1)
        transfer_size = resp.get("_transferSize", -1)
        # If transferSize missing, try encodedBodySize
        if transfer_size < 0:
            transfer_size = resp.get("encodedBodySize", -1)
        # Set decodedBodySize from content.size
        if content_size > 0 and not resp.get("decodedBodySize"):
            resp["decodedBodySize"] = content_size
            resp["_decodedBodySize"] = content_size
            modified = True
        # Set _transferSize if missing
        if transfer_size >= 0 and not resp.get("_transferSize"):
            resp["_transferSize"] = transfer_size
            resp["transferSize"] = transfer_size
            resp["bodySize"] = transfer_size
            modified = True
        # If we have both, set compression
        if content_size > 0 and transfer_size >= 0 and content_size != transfer_size:
            if "compression" not in content:
                content["compression"] = content_size - transfer_size
                resp["content"] = content
                modified = True
        if not resp.get("bodySize") and transfer_size >= 0:
            resp["bodySize"] = transfer_size
            modified = True
        entry["response"] = resp
    if modified:
        with har_path.open("w", encoding="utf-8") as f:
            json.dump(har, f)


def crawl_one_url(url: str, ad_type: str, output_dir: Path, ad_name: str,
                   timeout_ms: int = 60_000) -> Optional[Path]:
    from playwright.sync_api import sync_playwright, Error as PWError

    out_name = f"{ad_type}__{ad_name}.json"
    dest = output_dir / out_name
    tmp_har = Path(tempfile.mktemp(suffix=".har"))

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
                "--disable-background-networking",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            record_har_path=str(tmp_har),
            record_har_content="embed",
            record_har_mode="full",
            ignore_https_errors=True,
        )
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PWError:
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Crawl ad-preview URLs from a CSV and record HAR with Playwright")
    ap.add_argument("--csv", required=True, help="CSV file with columns ad;urlHTML")
    ap.add_argument("--ad-type", required=True, help="Label used as filename prefix, e.g. html5hiili")
    ap.add_argument("--output-dir", required=True, help="Output directory for HAR files")
    ap.add_argument("--timeout", type=int, default=60, help="Per-ad page load timeout in seconds")
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = [(r["ad"], r["urlHTML"]) for r in reader if r.get("urlHTML")]

    if not rows:
        sys.exit(f"No rows found in {csv_path}")

    print(f"Found {len(rows)} ads in {csv_path}")
    print(f"Output directory: {output_dir}")

    ok = fail = 0
    for i, (ad_name, url) in enumerate(rows, 1):
        out_path = output_dir / f"{args.ad_type}__{ad_name}.json"
        if out_path.exists():
            print(f"[{i}/{len(rows)}] skip {ad_name} (exists)")
            ok += 1
            continue

        print(f"[{i}/{len(rows)}] crawling {ad_name} ...", end=" ", flush=True)
        try:
            result = crawl_one_url(url, args.ad_type, output_dir, ad_name, timeout_ms=args.timeout * 1000)
            if result:
                har = json.load(result.open())
                ents = har["log"]["entries"]
                total_tx = sum(e.get("response", {}).get("_transferSize") or 0 for e in ents)
                total_cs = sum((e.get("response", {}).get("content") or {}).get("size") or 0 for e in ents)
                comp_ents = sum(1 for e in ents if (e.get("response", {}).get("content") or {}).get("compression"))
                print(f"ok — {len(ents)} entries, {total_tx/1e3:.1f} kB wire / {total_cs/1e3:.1f} kB content ({comp_ents} compressed)")
                ok += 1
            else:
                print("EMPTY")
                fail += 1
        except Exception as exc:
            print(f"ERROR: {exc}")
            fail += 1

    print(f"\nDone: {ok} ok, {fail} failed (out of {len(rows)})")


if __name__ == "__main__":
    main()
