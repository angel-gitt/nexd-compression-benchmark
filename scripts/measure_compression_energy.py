#!/usr/bin/env python3
"""
measure_compression_energy.py
=============================

Simulates the energy cost of serving ad creatives at scale, comparing:

  1. NEXD:  1 ZIP bundle → 1 file/impression
  2. HTML5: M gzip files → M files/impression (parallel CDN model)
  3. Alt HTML5: M brotli files → M files/impression (Google Ads CDN)

**Phase 1 — Compression** (one-time, per-creative):
    Compress all assets, measure energy with CodeCarbon.

**Phase 2 — Per-impression cost** (scales linearly):
    For each scenario, measures the CPU + I/O cost of serving one impression:
      - I/O cost: write payload(s) through /dev/null (syscall + kernel copy)
      - CPU cost: SHA-256 of each payload (models TLS / data-touching overhead)
    Then sums these into a per-impression figure and extrapolates.

This avoids long running loops for millions of impressions — we measure the
atomic per-impression cost precisely and multiply.

Usage:
  python3 scripts/measure_compression_energy.py
  python3 scripts/measure_compression_energy.py --ads-dir /path/to/ads/html5

Output:
  results/compression_energy.csv    — per-ad, per-scenario phases
  results/plots_network/10_compression_energy.png
"""

import argparse
import csv
import gzip
import hashlib
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

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR   = REPO_ROOT / "results"
OUT_CSV   = OUT_DIR / "compression_energy.csv"
OUT_PLOT  = OUT_DIR / "plots_network" / "10_compression_energy.png"

BINARY_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
                     ".mp4", ".webm", ".mkv", ".avi", ".mov", ".wmv", ".flv",
                     ".mp3", ".aac", ".wav", ".ogg", ".flac", ".woff", ".woff2",
                     ".ttf", ".eot", ".otf", ".pdf", ".zip", ".gz", ".br"}

N_ADS        = 8
SAMPLE_WAIT  = 0.5
BASELINE_S   = 3.0
COOLDOWN_S   = 2.0
LOOP_REPS    = 1000  # repeat per-impression measurement N times for SNR

COLORS = {"NEXD": "#E07B39", "HTML5_gzip": "#4C72B0", "HTML5_brotli": "#2CA02C"}


# ── Helpers ────────────────────────────────────────────────────────────────

def find_ad_dirs(ads_dir: Path) -> list[Path]:
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
    blobs = []
    for asset in assets:
        blobs.append(gzip.compress(asset.read_bytes(), mtime=0))
    return blobs, len(assets)


def compress_per_file_brotli(assets: list[Path]) -> tuple[list[bytes], int]:
    if not HAS_BROTLI:
        raise RuntimeError("brotli not installed")
    blobs = []
    for asset in assets:
        blobs.append(brotli.compress(asset.read_bytes(), quality=4))
    return blobs, len(assets)


# ── Per-impression atomic cost measurement ─────────────────────────────────

def measure_io_cost(blobs: list[bytes], label: str, reps: int = LOOP_REPS) -> float:
    """
    Measure the energy of writing all payloads through /dev/null `reps` times.
    Returns energy per impression (mJ).
    """
    if not blobs:
        return 0.0
    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    psutil.cpu_percent(interval=None)
    time.sleep(BASELINE_S)
    tracker.start()
    t0 = time.time()
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        for _ in range(reps):
            for b in blobs:
                remaining = b
                while remaining:
                    written = os.write(fd, remaining)
                    remaining = remaining[written:]
    finally:
        os.close(fd)
    duration_s = time.time() - t0
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
    energy_mj = energy_kwh * 3.6e6
    per_imp = energy_mj / reps if reps > 0 else 0.0
    return per_imp


def measure_cpu_cost(blobs: list[bytes], label: str, reps: int = LOOP_REPS) -> float:
    """
    Measure the energy of SHA-256 hashing all payloads `reps` times.
    Models TLS / checksum CPU cost per impression.
    Returns energy per impression (mJ).
    """
    if not blobs:
        return 0.0
    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    psutil.cpu_percent(interval=None)
    time.sleep(BASELINE_S)
    tracker.start()
    t0 = time.time()
    for _ in range(reps):
        for b in blobs:
            hashlib.sha256(b).digest()
    duration_s = time.time() - t0
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
    energy_mj = energy_kwh * 3.6e6
    per_imp = energy_mj / reps if reps > 0 else 0.0
    return per_imp


# ── Phase 1: Compression ──────────────────────────────────────────────────

def measure_compression(assets: list[Path], scenario: str, ad_name: str) -> Optional[dict]:
    label = {"NEXD": "NEXD", "HTML5_gzip": "HTML5-gzip", "HTML5_brotli": "HTML5-brotli"}.get(scenario, scenario)
    print(f"  [compress] {label:12s}  {ad_name[:50]:50s}", end=" ", flush=True)

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
            "scenario_label":      label,
            "phase":               "compression",
            "energy_mj":           round(energy_mj, 4),
            "cpu_avg_pct":         round(cpu_avg, 2),
            "duration_s":          round(duration_s, 4),
            "n_files":             n_files,
            "uncompressed_bytes":  uncompressed_size,
            "compressed_bytes":    compressed_size,
            "compression_ratio":   round(ratio, 3),
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

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    rng = np.random.default_rng(42)
    scenarios = ["NEXD", "HTML5_gzip", "HTML5_brotli"]
    labels = ["NEXD (ZIP)", "HTML5 (gzip)", "HTML5 (brotli)"]
    available = [s for s in scenarios if s in df.scenario.values]

    # 1. Compression energy boxplot
    ax = axes[0, 0]
    comp = df[df.phase == "compression"]
    for i, s in enumerate(available):
        vals = comp[comp.scenario == s]["energy_mj"].dropna().values
        if len(vals) == 0:
            continue
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        ax.scatter(i + rng.uniform(-0.1, 0.1, len(vals)), vals,
                   color=COLORS[s], alpha=0.7, s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(available)))
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Phase 1: Compression (one-time)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 2. Compression ratio
    ax = axes[0, 1]
    for i, s in enumerate(available):
        vals = comp[comp.scenario == s]["compression_ratio"].dropna().values
        if len(vals) == 0:
            continue
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        ax.scatter(i + rng.uniform(-0.1, 0.1, len(vals)), vals,
                   color=COLORS[s], alpha=0.7, s=30, edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(available)))
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Ratio")
    ax.set_title("Phase 1: Compression Ratio")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 3. Per-impression energy (stacked: IO + CPU)
    ax = axes[0, 2]
    serving = df[df.phase.isin(["io_cost", "cpu_cost"])]
    x = np.arange(len(available))
    width = 0.25
    for j, cost_type in enumerate(["io_cost", "cpu_cost"]):
        phase_label = "I/O (/dev/null)" if cost_type == "io_cost" else "CPU (SHA-256)"
        vals = []
        for s in available:
            g = serving[(serving.scenario == s) & (serving.phase == cost_type)]
            vals.append(g["energy_mj"].mean() if len(g) > 0 else 0)
        ax.bar(x + j * width - width / 2, vals, width, alpha=0.75,
               label=phase_label, color=["#66b3ff", "#ff9999"][j])
    ax.set_xticks(x)
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy per impression (mJ)")
    ax.set_title("Phase 2: Per-Impression Cost")
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 4. Total cost at scale (stacked bar)
    ax = axes[1, 0]
    imp_scales = [1_000, 10_000, 100_000, 1_000_000]
    for i, s in enumerate(available):
        comp_e = comp[comp.scenario == s]["energy_mj"].mean()
        io_e = serving[(serving.scenario == s) & (serving.phase == "io_cost")]["energy_mj"].mean()
        cpu_e = serving[(serving.scenario == s) & (serving.phase == "cpu_cost")]["energy_mj"].mean()
        per_imp = io_e + cpu_e
        totals = [comp_e + per_imp * n for n in imp_scales]
        ax.plot(imp_scales, totals, "o-", color=COLORS[s], label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Total Energy (mJ)")
    ax.set_title("Total Cost at Scale (compression + serving)")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 5. Extrapolation 10M-100M
    ax = axes[1, 1]
    extrap = [10_000_000, 50_000_000, 100_000_000]
    for s in available:
        comp_e = comp[comp.scenario == s]["energy_mj"].mean()
        io_e = serving[(serving.scenario == s) & (serving.phase == "io_cost")]["energy_mj"].mean()
        cpu_e = serving[(serving.scenario == s) & (serving.phase == "cpu_cost")]["energy_mj"].mean()
        per_imp = io_e + cpu_e
        totals = [comp_e + per_imp * n for n in extrap]
        ax.plot(extrap, totals, "o--", color=COLORS[s], label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Total Energy (mJ)")
    ax.set_title("Extrapolation to 10M-100M impressions")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 6. Energy per million impressions bar
    ax = axes[1, 2]
    for i, s in enumerate(available):
        comp_e = comp[comp.scenario == s]["energy_mj"].mean()
        io_e = serving[(serving.scenario == s) & (serving.phase == "io_cost")]["energy_mj"].mean()
        cpu_e = serving[(serving.scenario == s) & (serving.phase == "cpu_cost")]["energy_mj"].mean()
        per_imp = io_e + cpu_e
        per_million = comp_e + per_imp * 1_000_000
        ax.bar(i, per_million, color=COLORS[s], alpha=0.75, width=0.5)
    ax.set_xticks(range(len(available)))
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Total Cost per Million Impressions")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    fig.suptitle("Serving Energy at Scale: NEXD vs HTML5", fontsize=13)
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure compression + serving energy at scale"
    )
    parser.add_argument("--ads-dir", type=str, default=None,
                        help="Path to ads/html5 directory")
    parser.add_argument("--n-ads", type=int, default=N_ADS,
                        help=f"Number of ads (default: {N_ADS})")
    parser.add_argument("--reps", type=int, default=LOOP_REPS,
                        help=f"Measurement repetitions for SNR (default: {LOOP_REPS})")
    args = parser.parse_args()

    ads_dir = Path(args.ads_dir) if args.ads_dir else REPO_ROOT / "ads" / "html5"

    print("\n" + "=" * 80)
    print("⚡ Serving Energy at Scale: NEXD vs HTML5")
    print("=" * 80)
    print(f"  Ads dir:      {ads_dir}")
    print(f"  Ad samples:   {args.n_ads}")
    print(f"  Repetitions:  {args.reps}")

    if not ads_dir.exists():
        print(f"\n  ❌ Ads directory not found: {ads_dir}")
        print("     Transfer via: rsync -avz /local/ads/html5/ user@server:path/ads/html5/")
        return

    if not HAS_BROTLI:
        print("  ⚠️  brotli missing — will skip")

    ad_dirs = find_ad_dirs(ads_dir)
    test_ads = ad_dirs[:args.n_ads]
    print(f"\n  Found {len(ad_dirs)} ads, testing {len(test_ads)}\n")

    FIELDNAMES = [
        "ad_name", "scenario", "scenario_label", "phase",
        "energy_mj", "cpu_avg_pct", "duration_s",
        "n_files", "uncompressed_bytes", "compressed_bytes", "compression_ratio",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_file = open(OUT_CSV, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
    writer.writeheader()

    results = []

    def record(r):
        if r is None:
            return
        writer.writerow(r)
        csv_file.flush()
        results.append(r)

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

            # ── Phase 1: Compression ──────────────────────────────────────
            r = record(measure_compression(assets, scenario, ad_name))
            time.sleep(COOLDOWN_S)

            # ── Get compressed blobs ──────────────────────────────────────
            if scenario == "NEXD":
                blob, _ = compress_nexd(assets)
                blobs = [blob]
            elif scenario == "HTML5_gzip":
                blobs, _ = compress_per_file_gzip(assets)
            elif scenario == "HTML5_brotli":
                blobs, _ = compress_per_file_brotli(assets)

            # ── Phase 2: Per-impression cost ──────────────────────────────
            # I/O cost (write to /dev/null)
            io_per_imp = measure_io_cost(blobs, f"{ad_name}/{scenario}/io", reps=args.reps)
            time.sleep(COOLDOWN_S)

            label = {"NEXD": "NEXD", "HTML5_gzip": "HTML5-gzip",
                     "HTML5_brotli": "HTML5-brotli"}[scenario]
            total_bytes = sum(len(b) for b in blobs)
            record({
                "ad_name": ad_name, "scenario": scenario, "scenario_label": label,
                "phase": "io_cost", "energy_mj": round(io_per_imp, 8),
                "cpu_avg_pct": 0, "duration_s": 0,
                "n_files": len(blobs), "uncompressed_bytes": 0,
                "compressed_bytes": total_bytes, "compression_ratio": 0,
            })
            print(f"  [io_cost]   {label:12s}  {ad_name[:50]:50s}"
                  f"  {io_per_imp * 1e6:.4f} nJ/imp  ({len(blobs)} files)")

            # CPU cost (SHA-256)
            cpu_per_imp = measure_cpu_cost(blobs, f"{ad_name}/{scenario}/cpu", reps=args.reps)
            time.sleep(COOLDOWN_S)

            record({
                "ad_name": ad_name, "scenario": scenario, "scenario_label": label,
                "phase": "cpu_cost", "energy_mj": round(cpu_per_imp, 8),
                "cpu_avg_pct": 0, "duration_s": 0,
                "n_files": len(blobs), "uncompressed_bytes": 0,
                "compressed_bytes": total_bytes, "compression_ratio": 0,
            })
            print(f"  [cpu_cost]  {label:12s}  {ad_name[:50]:50s}"
                  f"  {cpu_per_imp * 1e6:.4f} nJ/imp  ({len(blobs)} hashes)")

            per_imp_total = io_per_imp + cpu_per_imp
            print(f"  [total]     {label:12s}  {ad_name[:50]:50s}"
                  f"  {per_imp_total * 1e6:.4f} nJ/imp  |  "
                  f"1M = {per_imp_total * 1_000_000:.1f} mJ")

            time.sleep(COOLDOWN_S)

    csv_file.close()

    if not results:
        print("❌ No results")
        return

    df = pd.DataFrame(results)

    print(f"\n  ✅ CSV: {OUT_CSV}")

    # Summary
    print(f"\n  {'─' * 80}")
    comp = df[df.phase == "compression"]
    print("  Phase 1 — Compression (one-time per creative)")
    print(comp.groupby("scenario")[["energy_mj", "compression_ratio"]]
          .agg({"energy_mj": ["mean", "std"], "compression_ratio": "mean"}).round(4).to_string())

    serving = df[df.phase.isin(["io_cost", "cpu_cost"])]
    print(f"\n  Phase 2 — Per-impression cost (measured @ {args.reps} reps)")
    pivot = serving.pivot_table(index="scenario", columns="phase",
                                 values="energy_mj", aggfunc="mean")
    pivot["total"] = pivot.get("io_cost", 0) + pivot.get("cpu_cost", 0)
    pivot["1M_impressions"] = pivot["total"] * 1_000_000
    print(pivot.round(8).to_string())

    print(f"\n  Extrapolation to 10M / 100M impressions:")
    for scenario in ("NEXD", "HTML5_gzip", "HTML5_brotli"):
        if scenario not in df.scenario.values:
            continue
        comp_e = comp[comp.scenario == scenario]["energy_mj"].mean()
        io_e = serving[(serving.scenario == scenario) & (serving.phase == "io_cost")]["energy_mj"].mean()
        cpu_e = serving[(serving.scenario == scenario) & (serving.phase == "cpu_cost")]["energy_mj"].mean()
        per_imp = io_e + cpu_e
        for millions in [1, 10, 100]:
            total_mj = comp_e + per_imp * millions * 1_000_000
            print(f"    {scenario:16s}  @ {millions:4d}M:  {total_mj:>12.1f} mJ"
                  f"  ({total_mj / 3600:>8.3f} mWh)")

    make_plots(df)
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
