#!/usr/bin/env python3
"""
measure_compression_energy.py
=============================
Measures the energy consumption of compressing and serving ad creative assets
at scale, comparing three scenarios:

  1. NEXD:  all assets → single ZIP bundle → served as 1 file per impression
  2. HTML5: each asset → individual gzip file → served as M files per impression
  3. Alt HTML5: each asset → individual brotli file (like Google Ads CDN)

The simulation runs two phases per ad:

  **Phase 1 — Compression** (one-time cost):
     Compress all assets, record energy.

  **Phase 2 — Serving at scale** (per-impression cost):
     Loop N_IMPRESSIONS times, each iteration simulates serving the compressed
     payload(s) by writing them through a kernel pipe (syscall + memory copy).
     Measures cumulative energy of the entire serving loop.

Results are extrapolated to millions of impressions.

Usage:
  python3 scripts/measure_compression_energy.py
  python3 scripts/measure_compression_energy.py --ads-dir /path/to/ads/html5

Output:
  results/compression_energy.csv       — per-ad, per-phase measurements
  results/serving_scale_energy.csv     — scale simulation results
  results/plots_network/10_compression_energy.png
"""

import argparse
import csv
import gzip
import os
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
OUT_SCALE = OUT_DIR / "serving_scale_energy.csv"
OUT_PLOT  = OUT_DIR / "plots_network" / "10_compression_energy.png"

# Binary extensions — won't compress well with gzip/brotli
BINARY_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
                     ".mp4", ".webm", ".mkv", ".avi", ".mov", ".wmv", ".flv",
                     ".mp3", ".aac", ".wav", ".ogg", ".flac", ".woff", ".woff2",
                     ".ttf", ".eot", ".otf", ".pdf", ".zip", ".gz", ".br"}

N_ADS         = 8       # number of ads to test
SAMPLE_WAIT   = 0.5     # seconds between CPU samples
BASELINE_S    = 3.0     # idle baseline before measurement
COOLDOWN_S    = 2.0     # pause between measurements

IMPRESSION_SCALES = [1_000, 10_000, 100_000, 1_000_000]
PIPE_BUF_SIZE     = 4_194_304  # 4 MB pipe buffer

COLORS = {"NEXD": "#E07B39", "HTML5_gzip": "#4C72B0", "HTML5_brotli": "#2CA02C"}


# ── Helpers ────────────────────────────────────────────────────────────────

def find_ad_dirs(ads_dir: Path) -> list[Path]:
    """Return directories with actual asset files (skip embedded-data-URI)."""
    dirs = []
    for d in sorted(ads_dir.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if name.endswith("html") or name.startswith("_"):
            continue
        files = [f for f in d.rglob("*") if f.is_file() and f.name != ".DS_Store"]
        if len(files) > 1:
            dirs.append(d)
    return dirs


def collect_assets(ad_dir: Path) -> list[Path]:
    assets = []
    for f in sorted(ad_dir.rglob("*")):
        if not f.is_file() or f.name == ".DS_Store":
            continue
        assets.append(f)
    return assets


def total_uncompressed_size(assets: list[Path]) -> int:
    return sum(f.stat().st_size for f in assets)


# ── Compression ────────────────────────────────────────────────────────────

def compress_nexd(assets: list[Path]) -> tuple[bytes, int]:
    """Single ZIP bundle of all assets."""
    buf = tempfile.SpooledTemporaryFile(max_size=50_000_000)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:
            arcname = str(asset.relative_to(asset.parents[-2]))
            zf.write(asset, arcname)
    buf.seek(0)
    data = buf.read()
    buf.close()
    return data, len(assets)


def compress_per_file_gzip(assets: list[Path]) -> tuple[list[bytes], int]:
    """Each asset compressed individually with gzip. Returns list of compressed blobs."""
    blobs = []
    for asset in assets:
        raw = asset.read_bytes()
        blobs.append(gzip.compress(raw, mtime=0))
    return blobs, len(assets)


def compress_per_file_brotli(assets: list[Path]) -> tuple[list[bytes], int]:
    """Each asset compressed individually with brotli (quality 4)."""
    if not HAS_BROTLI:
        raise RuntimeError("brotli not installed")
    blobs = []
    for asset in assets:
        raw = asset.read_bytes()
        blobs.append(brotli.compress(raw, quality=4))
    return blobs, len(assets)


# ── Serving simulation ─────────────────────────────────────────────────────

def simulate_serving_write(compressed_blobs: list[bytes], n_impressions: int):
    """
    Simulate serving compressed payloads by writing through /dev/null.
    Models the syscall + kernel memory copy cost of sending data over a
    network socket without disk I/O overhead.
    """
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        for _ in range(n_impressions):
            for blob in compressed_blobs:
                remaining = blob
                while remaining:
                    written = os.write(fd, remaining)
                    remaining = remaining[written:]
    finally:
        os.close(fd)


def measure_serving_loop(
    scenario: str,
    scenario_label: str,
    ad_name: str,
    compressed_blobs: list[bytes],    # NEXD: list of 1 blob; HTML5: list of M blobs
    n_impressions: int,
) -> Optional[dict]:
    """
    Measure energy of serving `n_impressions` impressions.

    For NEXD: a single write of the bundle per impression.
    For HTML5: M writes (one per asset) per impression.
    """
    proto = "single-file" if len(compressed_blobs) == 1 else f"{len(compressed_blobs)}-files"
    print(f"  {scenario_label:12s}  {ad_name[:40]:40s}  "
          f"{n_impressions:>8_} imp  {proto:12s}", end=" ", flush=True)

    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    cpu_samples = []
    stop_flag = threading.Event()

    def sample_loop():
        while not stop_flag.is_set():
            cpu_samples.append(psutil.cpu_percent(interval=SAMPLE_WAIT))

    sampler = threading.Thread(target=sample_loop, daemon=True)
    psutil.cpu_percent(interval=None)

    try:
        time.sleep(BASELINE_S)
        tracker.start()
        sampler.start()
        t0 = time.time()

        total_bytes_per_impression = sum(len(b) for b in compressed_blobs)

        # Serve by writing to /dev/null (syscall + kernel memcpy, no disk I/O)
        simulate_serving_write(compressed_blobs, n_impressions)

        duration_s = time.time() - t0
        stop_flag.set()
        emissions_kg = tracker.stop()

        energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
        energy_mj  = energy_kwh * 3.6e6
        cpu_avg    = float(np.mean(cpu_samples)) if cpu_samples else 0.0

        total_bytes = total_bytes_per_impression * n_impressions
        print(f"✅  {energy_mj:10.3f} mJ  "
              f"{energy_mj / n_impressions * 1000:.6f} µJ/imp  "
              f"{total_bytes / 1e6:.1f} MB served")

        return {
            "ad_name":                ad_name,
            "scenario":               scenario,
            "scenario_label":         scenario_label,
            "n_impressions":          n_impressions,
            "energy_mj":              round(energy_mj, 4),
            "energy_kwh":             round(energy_kwh, 10),
            "cpu_avg_pct":            round(cpu_avg, 2),
            "duration_s":             round(duration_s, 4),
            "n_assets":               len(compressed_blobs),
            "bytes_per_impression":   total_bytes_per_impression,
            "total_bytes_served":     total_bytes,
            "energy_uj_per_imp":      round(energy_mj * 1000 / n_impressions, 6),
            "timestamp":              datetime.now().isoformat(),
        }

    except Exception as e:
        stop_flag.set()
        tracker.stop()
        print(f"❌  {e}")
        return None


# ── Phase 1: Compression measurement ───────────────────────────────────────

def measure_compression_energy(assets: list[Path], scenario: str, ad_name: str) -> Optional[dict]:
    scenario_label = {
        "NEXD": "NEXD", "HTML5_gzip": "HTML5-gzip", "HTML5_brotli": "HTML5-brotli",
    }.get(scenario, scenario)

    print(f"  [compress] {scenario_label:12s}  {ad_name[:50]:50s}", end=" ", flush=True)

    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    cpu_samples = []
    stop_flag = threading.Event()

    def sample_loop():
        while not stop_flag.is_set():
            cpu_samples.append(psutil.cpu_percent(interval=SAMPLE_WAIT))

    sampler = threading.Thread(target=sample_loop, daemon=True)
    psutil.cpu_percent(interval=None)

    try:
        time.sleep(BASELINE_S)
        tracker.start()
        sampler.start()
        t0 = time.time()

        if scenario == "NEXD":
            result_bytes, n_files = compress_nexd(assets)
        elif scenario == "HTML5_gzip":
            blobs, n_files = compress_per_file_gzip(assets)
            result_bytes = b"".join(blobs)
        elif scenario == "HTML5_brotli":
            blobs, n_files = compress_per_file_brotli(assets)
            result_bytes = b"".join(blobs)
        else:
            raise ValueError(f"Unknown scenario: {scenario}")

        duration_s = time.time() - t0
        stop_flag.set()
        emissions_kg = tracker.stop()

        energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
        energy_mj  = energy_kwh * 3.6e6
        cpu_avg    = float(np.mean(cpu_samples)) if cpu_samples else 0.0
        cpu_peak   = float(np.max(cpu_samples)) if cpu_samples else 0.0

        uncompressed_size = total_uncompressed_size(assets)
        compressed_size   = len(result_bytes)
        ratio             = uncompressed_size / compressed_size if compressed_size > 0 else 0.0

        print(f"✅  {energy_mj:8.3f} mJ  {compressed_size / 1024:.1f} KB  "
              f"({n_files} files)  ratio={ratio:.1f}x")

        return {
            "ad_name":             ad_name,
            "scenario":            scenario,
            "scenario_label":      scenario_label,
            "phase":               "compression",
            "energy_mj":           round(energy_mj, 4),
            "energy_kwh":          round(energy_kwh, 10),
            "cpu_avg_pct":         round(cpu_avg, 2),
            "cpu_peak_pct":        round(cpu_peak, 2),
            "duration_s":          round(duration_s, 4),
            "n_files":             n_files,
            "uncompressed_bytes":  uncompressed_size,
            "compressed_bytes":    compressed_size,
            "compression_ratio":   round(ratio, 3),
            "n_impressions":       0,
            "energy_uj_per_imp":   0.0,
            "timestamp":           datetime.now().isoformat(),
        }

    except Exception as e:
        stop_flag.set()
        tracker.stop()
        print(f"❌  {e}")
        return None


# ── Plotting ───────────────────────────────────────────────────────────────

def make_plots(df: pd.DataFrame, df_scale: pd.DataFrame):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    rng = np.random.default_rng(42)

    scenarios = ["NEXD", "HTML5_gzip", "HTML5_brotli"]
    labels    = ["NEXD (ZIP)", "HTML5 (gzip)", "HTML5 (brotli)"]

    # --- Compression phase ------------------------------------------------
    comp = df[df.phase == "compression"]

    # 1. Compression energy boxplot
    ax = axes[0, 0]
    for i, s in enumerate(scenarios):
        vals = comp[comp.scenario == s]["energy_mj"].dropna().values
        if len(vals) == 0:
            continue
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        ax.scatter(i + rng.uniform(-0.1, 0.1, len(vals)), vals,
                   color=COLORS[s], alpha=0.7, s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Phase 1: Compression Energy (one-time)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 2. Compression ratio
    ax = axes[0, 1]
    for i, s in enumerate(scenarios):
        vals = comp[comp.scenario == s]["compression_ratio"].dropna().values
        if len(vals) == 0:
            continue
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        ax.scatter(i + rng.uniform(-0.1, 0.1, len(vals)), vals,
                   color=COLORS[s], alpha=0.7, s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Compression Ratio")
    ax.set_title("Phase 1: Compression Efficiency")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # --- Serving phase ----------------------------------------------------
    sv = df_scale

    # 3. Serving energy per impression (µJ/imp) across scales
    ax = axes[0, 2]
    for s in scenarios:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        scales = g["n_impressions"].values
        means  = g.groupby("n_impressions")["energy_uj_per_imp"].mean().values
        ax.plot(scales, means, "o-", color=COLORS[s], label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Energy per impression (µJ)")
    ax.set_title("Phase 2: Serving Energy per Impression")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 4. Total serving energy vs impressions (cumulative)
    ax = axes[1, 0]
    for s in scenarios:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        scales = g["n_impressions"].values
        means  = g.groupby("n_impressions")["energy_mj"].mean().values
        ax.plot(scales, means, "o-", color=COLORS[s], label=labels[scenarios.index(s)])
        # Fit linear model: energy = slope * impressions
        if len(scales) >= 2:
            slope = np.polyfit(scales, means, 1)[0]
            extrap = np.array([1e6, 10e6, 100e6])
            ax.plot(extrap, slope * extrap, "--", color=COLORS[s], alpha=0.4, linewidth=1)
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Total Serving Energy (mJ)")
    ax.set_title("Phase 2: Cumulative Serving Energy")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 5. Total bytes served vs impressions
    ax = axes[1, 1]
    for s in scenarios:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        scales = g["n_impressions"].values
        means  = g.groupby("n_impressions")["total_bytes_served"].mean().values
        ax.plot(scales, means / 1e6, "o-", color=COLORS[s], label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Total Data Served (MB)")
    ax.set_title("Phase 2: Data Served at Scale")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 6. Mean + SEM bar (largest scale)
    ax = axes[1, 2]
    max_scale = sv["n_impressions"].max()
    g_max = sv[sv.n_impressions == max_scale]
    for i, s in enumerate(scenarios):
        vals = g_max[g_max.scenario == s]["energy_mj"].dropna().values
        if len(vals) == 0:
            continue
        mean_v = vals.mean()
        sem_v  = vals.std() / np.sqrt(len(vals)) if len(vals) > 1 else 0
        ax.bar(i, mean_v, color=COLORS[s], alpha=0.75, width=0.5, yerr=sem_v, capsize=5)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels(labels)
    ax.set_ylabel(f"Energy at {max_scale:,} impressions (mJ)")
    ax.set_title(f"Phase 2: Serving Energy at {max_scale:,}")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    fig.suptitle("Compression + Serving Energy: NEXD vs HTML5 at Scale", fontsize=13)
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure compression + serving energy of ad creatives at scale"
    )
    parser.add_argument("--ads-dir", type=str, default=None,
                        help="Path to ads/html5 directory")
    parser.add_argument("--impressions", type=str, default=",".join(str(s) for s in IMPRESSION_SCALES),
                        help="Comma-separated impression counts to simulate")
    parser.add_argument("--n-ads", type=int, default=N_ADS,
                        help=f"Number of ads to test (default: {N_ADS})")
    args = parser.parse_args()

    impression_scales = [int(s.strip()) for s in args.impressions.split(",")]

    ads_dir = Path(args.ads_dir) if args.ads_dir else REPO_ROOT / "ads" / "html5"

    print("\n" + "=" * 80)
    print("⚡ Compression + Serving Energy: NEXD vs HTML5 at Scale")
    print("=" * 80)
    print(f"  Ads dir:      {ads_dir}")
    print(f"  Ad samples:   {args.n_ads}")
    print(f"  Impressions:  {impression_scales}")

    if not ads_dir.exists():
        print(f"\n  ❌ Ads directory not found: {ads_dir}")
        print("     Transfer via rsync:\n"
              "       rsync -avz /local/ads/html5/ user@server:/path/to/ads/html5/")
        return

    if not HAS_BROTLI:
        print("  ⚠️  brotli not installed — HTML5_brotli will be skipped")

    ad_dirs = find_ad_dirs(ads_dir)
    test_ads = ad_dirs[:args.n_ads]
    print(f"\n  Found {len(ad_dirs)} ads, testing {len(test_ads)}\n")

    # ── CSV setup ─────────────────────────────────────────────────────────
    COMP_FIELDS = [
        "ad_name", "scenario", "scenario_label", "phase",
        "energy_mj", "energy_kwh", "cpu_avg_pct", "cpu_peak_pct",
        "duration_s", "n_files",
        "uncompressed_bytes", "compressed_bytes", "compression_ratio",
        "n_impressions", "energy_uj_per_imp", "timestamp",
    ]
    SCALE_FIELDS = [
        "ad_name", "scenario", "scenario_label",
        "n_impressions", "energy_mj", "energy_kwh", "cpu_avg_pct",
        "duration_s", "n_assets",
        "bytes_per_impression", "total_bytes_served", "energy_uj_per_imp",
        "timestamp",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    comp_csv = open(OUT_CSV, "w", newline="", encoding="utf-8")
    comp_writer = csv.DictWriter(comp_csv, fieldnames=COMP_FIELDS)
    comp_writer.writeheader()
    comp_csv.flush()

    scale_csv = open(OUT_SCALE, "w", newline="", encoding="utf-8")
    scale_writer = csv.DictWriter(scale_csv, fieldnames=SCALE_FIELDS)
    scale_writer.writeheader()
    scale_csv.flush()

    comp_results = []
    scale_results = []

    def save_comp(r):
        if r is None:
            return r
        comp_writer.writerow(r)
        comp_csv.flush()
        comp_results.append(r)
        return r

    def save_scale(r):
        if r is None:
            return r
        scale_writer.writerow(r)
        scale_csv.flush()
        scale_results.append(r)
        return r

    # ── Run experiment ────────────────────────────────────────────────────
    for idx, ad_dir in enumerate(test_ads):
        ad_name = ad_dir.name
        assets = collect_assets(ad_dir)
        if not assets:
            continue

        uncompressed_mb = total_uncompressed_size(assets) / (1024 * 1024)
        print(f"\n  {'─' * 75}")
        print(f"  [{idx + 1}/{len(test_ads)}] {ad_name}  "
              f"({len(assets)} files, {uncompressed_mb:.2f} MB)")
        print(f"  {'─' * 75}")

        for scenario in ("NEXD", "HTML5_gzip", "HTML5_brotli"):
            if scenario == "HTML5_brotli" and not HAS_BROTLI:
                continue

            # Phase 1: compress, get blob(s)
            r = save_comp(measure_compression_energy(assets, scenario, ad_name))
            time.sleep(COOLDOWN_S)

            # Get compressed blobs for serving simulation
            if scenario == "NEXD":
                blob, _ = compress_nexd(assets)
                blobs = [blob]
            elif scenario == "HTML5_gzip":
                blobs, _ = compress_per_file_gzip(assets)
            elif scenario == "HTML5_brotli":
                blobs, _ = compress_per_file_brotli(assets)

            # Phase 2: serve at scale
            for n_imp in impression_scales:
                label = {"NEXD": "NEXD", "HTML5_gzip": "HTML5-gzip",
                         "HTML5_brotli": "HTML5-brotli"}[scenario]
                sr = save_scale(measure_serving_loop(
                    scenario, label, ad_name, blobs, n_imp,
                ))
                time.sleep(COOLDOWN_S)

    comp_csv.close()
    scale_csv.close()

    if not comp_results:
        print("❌ No results")
        return

    df_comp = pd.DataFrame(comp_results)
    df_scale = pd.DataFrame(scale_results)

    print(f"\n  ✅ Compression CSV:   {OUT_CSV}")
    print(f"  ✅ Serving scale CSV: {OUT_SCALE}")

    # Print summary
    print(f"\n  {'─' * 80}")
    print("  Phase 1 — Compression Energy")
    print(df_comp.groupby("scenario")[["energy_mj", "duration_s", "compression_ratio"]]
          .agg({"energy_mj": ["mean", "std"], "duration_s": "mean", "compression_ratio": "mean"})
          .round(4).to_string())

    print(f"\n  Phase 2 — Serving at {impression_scales[-1]:,} impressions")
    last = df_scale[df_scale.n_impressions == impression_scales[-1]]
    print(last.groupby("scenario")[["energy_mj", "energy_uj_per_imp"]]
          .agg({"energy_mj": ["mean", "std"], "energy_uj_per_imp": "mean"})
          .round(4).to_string())

    # Extrapolation to millions
    print(f"\n  📊 Extrapolation to 10M / 100M impressions:")
    for scenario in ("NEXD", "HTML5_gzip", "HTML5_brotli"):
        g = df_scale[df_scale.scenario == scenario]
        if len(g) < 2:
            continue
        # Linear fit: energy ~ impressions
        x = g["n_impressions"].values.astype(float)
        y = g["energy_mj"].values
        slope, intercept = np.polyfit(x, y, 1)

        # Add compression energy (one-time)
        comp_e = df_comp[df_comp.scenario == scenario]["energy_mj"].mean()
        for millions in [1, 10, 100]:
            total_mj = comp_e + slope * millions * 1_000_000
            print(f"    {scenario:15s}  @ {millions}M impressions:  "
                  f"{total_mj:,.1f} mJ  ({total_mj / 3600:,.3f} mWh)")

    make_plots(df_comp, df_scale)
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
