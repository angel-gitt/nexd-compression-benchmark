#!/usr/bin/env python3
"""
measure_compression_energy.py
=============================
Measures the energy consumption of compressing ad creative assets in three
different scenarios:

  1. NEXD-style:  all assets → single ZIP bundle (single compression op)
  2. HTML5-style: each asset → individual gzip file (parallel CDN simulation,
                  energies summed)
  3. Alt HTML5:   each asset → individual brotli file (parallel CDN simulation,
                  energies summed)

Uses CodeCarbon (EmissionsTracker) on bare-metal servers to measure per-op
energy.  Decompression is left for a future experiment.

Usage:
  python3 scripts/measure_compression_energy.py
  python3 scripts/measure_compression_energy.py --ads-dir /path/to/ads/html5

Output:
  results/compression_energy.csv
  results/plots_network/10_compression_energy.png
"""

import argparse
import csv
import gzip
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import psutil

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

from codecarbon import EmissionsTracker

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR   = REPO_ROOT / "results"
OUT_CSV   = OUT_DIR / "compression_energy.csv"
OUT_PLOT  = OUT_DIR / "plots_network" / "10_compression_energy.png"

# Binary assets (images, video, audio) — these won't compress well with gzip/brotli
BINARY_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
                     ".mp4", ".webm", ".mkv", ".avi", ".mov", ".wmv", ".flv",
                     ".mp3", ".aac", ".wav", ".ogg", ".flac", ".woff", ".woff2",
                     ".ttf", ".eot", ".otf", ".pdf", ".zip", ".gz", ".br"}

N_ADS        = 8   # number of ads to test per scenario
SAMPLE_WAIT  = 0.5  # seconds between CPU samples
BASELINE_S   = 3.0  # seconds of idle baseline before each measurement
COOLDOWN_S   = 2.0  # seconds between measurements

COLORS = {"NEXD": "#E07B39", "HTML5_gzip": "#4C72B0", "HTML5_brotli": "#2CA02C"}

# ── Helpers ────────────────────────────────────────────────────────────────

def find_ad_dirs(ads_dir: Path) -> list[Path]:
    """Return directories that contain actual asset files (not embedded-data-URI)."""
    dirs = []
    for d in sorted(ads_dir.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if name.endswith("html"):
            continue
        if name.startswith("_"):
            continue
        files = [f for f in d.rglob("*") if f.is_file() and f.name != ".DS_Store"]
        if len(files) > 1:
            dirs.append(d)
    return dirs


def collect_assets(ad_dir: Path) -> list[Path]:
    """Collect all asset files in an ad directory, skip .DS_Store."""
    assets = []
    for f in sorted(ad_dir.rglob("*")):
        if not f.is_file() or f.name == ".DS_Store":
            continue
        assets.append(f)
    return assets


def is_text_file(path: Path) -> bool:
    """Heuristic: files with binary extensions are treated as binary."""
    return path.suffix.lower() not in BINARY_EXTENSIONS


def total_uncompressed_size(assets: list[Path]) -> int:
    return sum(f.stat().st_size for f in assets)


# ── Compression functions ──────────────────────────────────────────────────

def compress_nexd(assets: list[Path]) -> tuple[bytes, int]:
    """Compress all assets into a single in-memory ZIP bundle.
    Returns (zip_bytes, num_files_compressed)."""
    buf = tempfile.SpooledTemporaryFile(max_size=50_000_000)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:
            arcname = str(asset.relative_to(asset.parents[-2]))  # preserve relative path
            zf.write(asset, arcname)
    buf.seek(0)
    data = buf.read()
    buf.close()
    return data, len(assets)


def compress_html5_gzip(assets: list[Path]) -> tuple[bytes, int]:
    """Compress each asset individually with gzip.
    Returns (concatenated_compressed_bytes, num_assets)."""
    all_compressed = b""
    for asset in assets:
        raw = asset.read_bytes()
        compressed = gzip.compress(raw, mtime=0)
        all_compressed += compressed
    return all_compressed, len(assets)


def compress_html5_brotli(assets: list[Path]) -> tuple[bytes, int]:
    """Compress each asset individually with brotli (quality 4, like typical CDN).
    Returns (concatenated_compressed_bytes, num_assets)."""
    if not HAS_BROTLI:
        raise RuntimeError("brotli module not installed")
    all_compressed = b""
    for asset in assets:
        raw = asset.read_bytes()
        compressed = brotli.compress(raw, quality=4)
        all_compressed += compressed
    return all_compressed, len(assets)


# ── Energy measurement ─────────────────────────────────────────────────────

def measure_compression_energy(
    assets: list[Path],
    scenario: str,
    ad_name: str,
) -> Optional[dict]:
    """
    Measure the energy of compressing `assets` under `scenario`.
    Returns a dict with results or None on failure.
    """
    scenario_label = {
        "NEXD":        "NEXD",
        "HTML5_gzip":  "HTML5-gzip",
        "HTML5_brotli":"HTML5-brotli",
    }.get(scenario, scenario)

    print(f"  {scenario_label:12s}  {ad_name[:50]:50s}", end=" ", flush=True)

    tracker = EmissionsTracker(
        log_level="error",
        save_to_file=False,
        force_carbon_intensity_g_co2e_kwh=233.0,
    )

    cpu_samples = []
    stop_flag = threading.Event()

    def sample_loop():
        while not stop_flag.is_set():
            cpu_samples.append(psutil.cpu_percent(interval=SAMPLE_WAIT))

    sampler = threading.Thread(target=sample_loop, daemon=True)

    # Pre-calibrate CPU sensor
    psutil.cpu_percent(interval=None)

    try:
        # Idle baseline
        time.sleep(BASELINE_S)

        # Measure
        tracker.start()
        sampler.start()
        t0 = time.time()

        # Execute compression
        if scenario == "NEXD":
            result_bytes, n_files = compress_nexd(assets)
        elif scenario == "HTML5_gzip":
            result_bytes, n_files = compress_html5_gzip(assets)
        elif scenario == "HTML5_brotli":
            result_bytes, n_files = compress_html5_brotli(assets)
        else:
            raise ValueError(f"Unknown scenario: {scenario}")

        duration_s = time.time() - t0
        stop_flag.set()
        emissions_kg = tracker.stop()

        energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
        energy_mj  = energy_kwh * 3.6e6
        cpu_avg    = float(np.mean(cpu_samples)) if cpu_samples else 0.0
        cpu_peak   = float(np.max(cpu_samples))  if cpu_samples else 0.0

        uncompressed_size = total_uncompressed_size(assets)
        compressed_size   = len(result_bytes)
        ratio             = uncompressed_size / compressed_size if compressed_size > 0 else 0.0

        print(f"✅  {energy_mj:8.3f} mJ  {compressed_size/1024:.1f} KB  "
              f"({n_files} files)  ratio={ratio:.1f}x")

        return {
            "ad_name":             ad_name,
            "scenario":            scenario,
            "scenario_label":      scenario_label,
            "energy_mj":           round(energy_mj, 4),
            "energy_kwh":          round(energy_kwh, 10),
            "cpu_avg_pct":         round(cpu_avg, 2),
            "cpu_peak_pct":        round(cpu_peak, 2),
            "duration_s":          round(duration_s, 4),
            "n_files":             n_files,
            "uncompressed_bytes":  uncompressed_size,
            "compressed_bytes":    compressed_size,
            "compression_ratio":   round(ratio, 3),
            "timestamp":           datetime.now().isoformat(),
        }

    except Exception as e:
        stop_flag.set()
        tracker.stop()
        print(f"❌  {e}")
        return None


# ── Plotting ───────────────────────────────────────────────────────────────

def make_plots(df: pd.DataFrame):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(16, 10))
    rng = np.random.default_rng(42)

    # 1. Energy boxplot per scenario
    ax = axes[0, 0]
    scenarios = ["NEXD", "HTML5_gzip", "HTML5_brotli"]
    labels    = ["NEXD (ZIP)", "HTML5 (gzip)", "HTML5 (brotli)"]
    data      = [df[df.scenario == s]["energy_mj"].dropna().values for s in scenarios]
    for i, vals in enumerate(data):
        if len(vals) == 0:
            continue
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[scenarios[i]])
        bp["boxes"][0].set_alpha(0.7)
        jitter = rng.uniform(-0.1, 0.1, len(vals))
        ax.scatter(i + jitter, vals, color=COLORS[scenarios[i]], alpha=0.7,
                   s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Compression Energy per Ad")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 2. Compressed size
    ax = axes[0, 1]
    for i, s in enumerate(scenarios):
        g = df[df.scenario == s]
        if len(g) == 0:
            continue
        vals = g["compressed_bytes"].values / 1024
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        jitter = rng.uniform(-0.1, 0.1, len(vals))
        ax.scatter(i + jitter, vals, color=COLORS[s], alpha=0.7,
                   s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Compressed size (KB)")
    ax.set_title("Compressed Payload Size")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 3. Compression ratio
    ax = axes[0, 2]
    for i, s in enumerate(scenarios):
        g = df[df.scenario == s]
        if len(g) == 0:
            continue
        vals = g["compression_ratio"].values
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        jitter = rng.uniform(-0.1, 0.1, len(vals))
        ax.scatter(i + jitter, vals, color=COLORS[s], alpha=0.7,
                   s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Compression Ratio (uncompressed/compressed)")
    ax.set_title("Compression Efficiency")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 4. Energy vs compressed size scatter
    ax = axes[1, 0]
    for s in scenarios:
        g = df[df.scenario == s]
        if len(g) == 0:
            continue
        ax.scatter(g["compressed_bytes"] / 1024, g["energy_mj"],
                   color=COLORS[s], label=labels[scenarios.index(s)],
                   alpha=0.75, s=50, edgecolors="white", linewidths=0.3)
        if len(g) >= 2:
            b, a = np.polyfit(g["compressed_bytes"] / 1024, g["energy_mj"], 1)
            xs = np.linspace((g["compressed_bytes"] / 1024).min(),
                             (g["compressed_bytes"] / 1024).max(), 30)
            ax.plot(xs, a + b * xs, color=COLORS[s], linestyle="--", linewidth=1.5)
    ax.set_xlabel("Compressed Size (KB)")
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Energy vs Compressed Size")
    ax.legend()
    ax.grid(linestyle="--", alpha=0.4)

    # 5. Energy bar with SEM
    ax = axes[1, 1]
    means = [df[df.scenario == s]["energy_mj"].mean() for s in scenarios]
    errs  = [df[df.scenario == s]["energy_mj"].sem() for s in scenarios]
    bars  = ax.bar(range(len(scenarios)), means, color=[COLORS[s] for s in scenarios],
                   alpha=0.75, width=0.5, yerr=errs, capsize=5)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Mean Energy (mJ)")
    ax.set_title("Mean Compression Energy + SEM")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 6. Duration bar
    ax = axes[1, 2]
    for i, s in enumerate(scenarios):
        g = df[df.scenario == s]
        if len(g) == 0:
            continue
        mean_dur = g["duration_s"].mean()
        sem_dur  = g["duration_s"].sem()
        ax.bar(i, mean_dur, color=COLORS[s], alpha=0.75, width=0.5,
               yerr=sem_dur, capsize=5)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Duration (s)")
    ax.set_title("Compression Duration")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # Super title with means
    nex_mj   = df[df.scenario == "NEXD"]["energy_mj"].mean()
    gz_mj    = df[df.scenario == "HTML5_gzip"]["energy_mj"].mean()
    br_mj    = df[df.scenario == "HTML5_brotli"]["energy_mj"].mean() if "HTML5_brotli" in df.scenario.values else 0

    fig.suptitle(
        f"Compression Energy: NEXD {nex_mj:.2f} mJ  |  HTML5-gzip {gz_mj:.2f} mJ  |  "
        f"HTML5-brotli {br_mj:.2f} mJ",
        fontsize=12
    )
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure compression energy of ad creative assets"
    )
    parser.add_argument(
        "--ads-dir", type=str, default=None,
        help="Path to the ads/html5 directory with creative assets. "
             "Defaults to REPO_ROOT/ads/html5"
    )
    args = parser.parse_args()

    ads_dir = Path(args.ads_dir) if args.ads_dir else REPO_ROOT / "ads" / "html5"

    print("\n" + "=" * 80)
    print("⚡ Compression Energy: NEXD (ZIP) vs HTML5 (gzip) vs HTML5 (brotli)")
    print("=" * 80)
    print(f"  Ads dir: {ads_dir}")

    if not ads_dir.exists():
        print(f"\n  ❌ Ads directory not found: {ads_dir}")
        print("     You need the ad creative assets to run this experiment.")
        print("     Transfer the ads/ directory from your local machine:")
        print("       rsync -avz /path/to/local/ads/html5/ user@server:/path/to/ads/html5/")
        print("     Or use --ads-dir to point to an existing directory.\n")
        return

    if not HAS_BROTLI:
        print("  ⚠️  brotli not installed — HTML5_brotli scenario will be skipped.")
        print("     Install with: pip install brotli")
        print()

    ad_dirs = find_ad_dirs(ads_dir)
    print(f"\n  Found {len(ad_dirs)} HTML5 ad directories with assets")

    test_ads = ad_dirs[:N_ADS]
    print(f"  Testing {len(test_ads)} ads across all scenarios\n")

    FIELDNAMES = [
        "ad_name", "scenario", "scenario_label",
        "energy_mj", "energy_kwh",
        "cpu_avg_pct", "cpu_peak_pct",
        "duration_s", "n_files",
        "uncompressed_bytes", "compressed_bytes", "compression_ratio",
        "timestamp",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Start fresh each run
    csv_file = open(OUT_CSV, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
    writer.writeheader()
    csv_file.flush()

    results = []

    def record(r):
        if r is None:
            return
        writer.writerow(r)
        csv_file.flush()
        results.append(r)
        return r

    # Run experiments: for each ad, run all three scenarios sequentially
    for idx, ad_dir in enumerate(test_ads):
        ad_name = ad_dir.name
        assets  = collect_assets(ad_dir)

        if not assets:
            print(f"  ⚠️  {ad_name}: no assets found, skipping")
            continue

        uncompressed_mb = total_uncompressed_size(assets) / (1024 * 1024)
        print(f"\n  {'─' * 70}")
        print(f"  [{idx + 1}/{len(test_ads)}] {ad_name}  ({len(assets)} files, "
              f"{uncompressed_mb:.2f} MB uncompressed)")
        print(f"  {'─' * 70}")

        # NEXD: single ZIP
        record(measure_compression_energy(assets, "NEXD", ad_name))
        time.sleep(COOLDOWN_S)

        # HTML5: per-file gzip
        record(measure_compression_energy(assets, "HTML5_gzip", ad_name))
        time.sleep(COOLDOWN_S)

        # Alt HTML5: per-file brotli
        if HAS_BROTLI:
            record(measure_compression_energy(assets, "HTML5_brotli", ad_name))
            time.sleep(COOLDOWN_S)

    csv_file.close()

    if not results:
        print("❌ No results recorded")
        return

    df = pd.DataFrame(results)
    print(f"\n  ✅ CSV: {OUT_CSV}")
    print(f"\n  {'─' * 80}")
    summary = df.groupby("scenario")[["energy_mj", "duration_s", "compressed_bytes"]].agg({
        "energy_mj": ["mean", "std", "count"],
        "duration_s": "mean",
        "compressed_bytes": "mean",
    }).round(3)
    print(summary.to_string())

    make_plots(df)
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
