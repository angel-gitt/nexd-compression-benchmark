#!/usr/bin/env python3
"""
measure_compression_energy.py
=============================

Simula un servidor de anuncios sirviendo millones de impresiones para medir
el coste energético marginal de compresión + serving, comparando:

  1. NEXD:  1 ZIP bundle → 1 archivo por impresión
  2. HTML5: M archivos gzip → M archivos por impresión (CDN paralelo)
  3. Alt HTML5: M archivos brotli → M archivos por impresión (Google Ads CDN)

Cada medición descuenta el consumo baseline del sistema midiendo la energía
en idle durante el mismo tiempo que dura el experimento y restándola.

La simulación ejecuta un bucle de serving que:
  - Escribe los datos comprimidos en /dev/null (modela I/O de red)
  - Hashea cada payload con SHA-256 (modela coste CPU de TLS/checksum)
  - Mide el tiempo y energía total

El coste marginal por impresión se calcula como:
  E_per_imp = (E_total - E_baseline) / N_impressions

Usage:
  python3 scripts/measure_compression_energy.py
  python3 scripts/measure_compression_energy.py --ads-dir /path/to/ads/html5
  python3 scripts/measure_compression_energy.py --n-ads 10 --imp-scale 100000,500000,1000000

Output:
  results/compression_energy.csv
  results/plots_network/10_compression_energy.png
"""

import argparse
import csv
import gzip
import hashlib
import os
import tempfile
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

N_ADS              = 8
DEFAULT_IMP_SCALES = [50_000, 200_000, 1_000_000]

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
    buf = tempfile.SpooledTemporaryFile(max_size=100_000_000)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:
            arcname = str(asset.relative_to(asset.parents[-2]))
            zf.write(asset, arcname)
    buf.seek(0)
    data = buf.read()
    buf.close()
    return data, len(assets)


def compress_per_file_gzip(assets: list[Path]) -> tuple[list[bytes], int]:
    blobs = [gzip.compress(a.read_bytes(), mtime=0) for a in assets]
    return blobs, len(assets)


def compress_per_file_brotli(assets: list[Path]) -> tuple[list[bytes], int]:
    if not HAS_BROTLI:
        raise RuntimeError("brotli not installed")
    blobs = [brotli.compress(a.read_bytes(), quality=4) for a in assets]
    return blobs, len(assets)


# ── Serving workload ───────────────────────────────────────────────────────

def serve_impression(blobs: list[bytes], fd: int):
    """
    One impression: write all compressed payloads through /dev/null
    AND hash each one with SHA-256 to model real work.
    """
    for b in blobs:
        remaining = b
        while remaining:
            written = os.write(fd, remaining)
            remaining = remaining[written:]
        hashlib.sha256(b).digest()


def serve_loop(blobs: list[bytes], n_impressions: int) -> tuple[int, float]:
    """
    Run the serving loop for n_impressions.
    Returns (impressions_actually_served, elapsed_seconds).
    """
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        t0 = time.perf_counter()
        for i in range(n_impressions):
            serve_impression(blobs, fd)
        elapsed = time.perf_counter() - t0
        return n_impressions, elapsed
    finally:
        os.close(fd)


# ── Energy measurement with baseline subtraction ───────────────────────────

def measure_baseline_energy(duration_s: float) -> float:
    """
    Measure idle energy consumption for `duration_s` seconds.
    Returns energy in mJ.
    """
    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    tracker.start()
    time.sleep(duration_s)
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
    return energy_kwh * 3.6e6


def measure_compression_energy(assets: list[Path], scenario: str,
                                ad_name: str) -> Optional[dict]:
    """Measure compression energy (one-time cost per ad)."""
    label = {"NEXD": "NEXD", "HTML5_gzip": "HTML5-gzip",
             "HTML5_brotli": "HTML5-brotli"}.get(scenario, scenario)
    print(f"  [compress] {label:12s}  {ad_name[:45]:45s}", end=" ", flush=True)

    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    tracker.start()
    t0 = time.perf_counter()

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

    duration_s = time.perf_counter() - t0
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
    energy_mj  = energy_kwh * 3.6e6

    uncompressed_size = total_uncompressed_size(assets)
    compressed_size   = len(result_bytes)
    ratio = uncompressed_size / compressed_size if compressed_size > 0 else 0.0

    print(f"✅  {energy_mj:8.3f} mJ  {compressed_size / 1024:.1f} KB  "
          f"({n_files} files)  ratio={ratio:.1f}x")

    return {
        "ad_name": ad_name, "scenario": scenario, "scenario_label": label,
        "phase": "compression", "energy_mj": round(energy_mj, 4),
        "duration_s": round(duration_s, 4),
        "n_files": n_files, "n_impressions": 0,
        "uncompressed_bytes": uncompressed_size,
        "compressed_bytes": compressed_size,
        "compression_ratio": round(ratio, 3),
        "energy_uj_per_imp": 0.0,
    }


def measure_serving_energy(blobs: list[bytes], scenario: str,
                            ad_name: str, n_impressions: int) -> Optional[dict]:
    """
    Measure serving energy with baseline subtraction:
      1. Run serving loop for n_impressions, measure total energy
      2. Measure baseline idle energy for the same duration
      3. Marginal energy = total - baseline
      4. Per-impression = marginal / n_impressions
    """
    label = {"NEXD": "NEXD", "HTML5_gzip": "HTML5-gzip",
             "HTML5_brotli": "HTML5-brotli"}.get(scenario, scenario)
    total_bytes_per_imp = sum(len(b) for b in blobs)

    print(f"  [serving]  {label:12s}  {ad_name[:35]:35s}  "
          f"{n_impressions:>10_} imp", end=" ", flush=True)

    # 1. Dry-run to estimate loop duration (at least 50 reps)
    dry_run_imp = max(50, n_impressions // 20)
    _, est_duration = serve_loop(blobs, dry_run_imp)
    est_per_imp = est_duration / dry_run_imp
    est_total_duration = est_per_imp * n_impressions

    print(f"est ~{est_total_duration:.0f}s", end=" ", flush=True)

    # 2. Run serving loop with energy tracking
    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    tracker.start()
    t0 = time.perf_counter()
    served, elapsed = serve_loop(blobs, n_impressions)
    emissions_kg = tracker.stop()

    energy_kwh = emissions_kg * 1.87 if emissions_kg else 0.0
    total_energy_mj = energy_kwh * 3.6e6

    # 3. Measure baseline for same duration (no cooldown needed)
    print(f"baseline {elapsed:.1f}s", end=" ", flush=True)
    baseline_mj = measure_baseline_energy(elapsed)

    # 4. Marginal energy
    marginal_mj = max(0, total_energy_mj - baseline_mj)
    per_imp_uj  = (marginal_mj * 1000) / served if served > 0 else 0.0

    total_bytes = total_bytes_per_imp * served
    throughput_mbps = (total_bytes / elapsed / 1e6) if elapsed > 0 else 0

    print(f"✅  total={total_energy_mj:.1f}mJ  base={baseline_mj:.1f}mJ  "
          f"marg={marginal_mj:.1f}mJ  {per_imp_uj:.3f}µJ/imp  "
          f"{throughput_mbps:.0f}MB/s")

    return {
        "ad_name": ad_name, "scenario": scenario, "scenario_label": label,
        "phase": "serving",
        "energy_mj": round(marginal_mj, 4),
        "total_energy_mj": round(total_energy_mj, 4),
        "baseline_energy_mj": round(baseline_mj, 4),
        "duration_s": round(elapsed, 4),
        "n_files": len(blobs),
        "n_impressions": served,
        "uncompressed_bytes": 0,
        "compressed_bytes": total_bytes_per_imp,
        "compression_ratio": 0.0,
        "energy_uj_per_imp": round(per_imp_uj, 4),
        "throughput_mbps": round(throughput_mbps, 1),
    }


# ── Plotting ───────────────────────────────────────────────────────────────

def make_plots(df: pd.DataFrame):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    rng = np.random.default_rng(42)
    scenarios = ["NEXD", "HTML5_gzip", "HTML5_brotli"]
    labels    = ["NEXD (ZIP)", "HTML5 (gzip)", "HTML5 (brotli)"]
    available = [s for s in scenarios if s in df.scenario.values]

    comp   = df[df.phase == "compression"]
    serving = df[df.phase == "serving"]

    # 1. Compression energy
    ax = axes[0, 0]
    for i, s in enumerate(available):
        vals = comp[comp.scenario == s]["energy_mj"].dropna().values
        if len(vals) == 0:
            continue
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2}, widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        ax.scatter(i + rng.uniform(-0.1, 0.1, len(vals)), vals,
                   color=COLORS[s], alpha=0.7, s=30, edgecolors="white",
                   linewidths=0.3, zorder=3)
    ax.set_xticks(range(len(available)))
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Compression (one-time per creative)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 2. Per-impression energy (µJ/imp) across serving scales
    ax = axes[0, 1]
    for s in available:
        g = serving[serving.scenario == s]
        if len(g) == 0:
            continue
        avg = g.groupby("n_impressions")["energy_uj_per_imp"].mean()
        ax.plot(avg.index, avg.values, "o-", color=COLORS[s],
                label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Energy per impression (µJ)")
    ax.set_title("Marginal Energy per Impression")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 3. Total marginal energy at scale
    ax = axes[0, 2]
    for s in available:
        g = serving[serving.scenario == s]
        if len(g) == 0:
            continue
        avg = g.groupby("n_impressions")["energy_mj"].mean()
        ax.plot(avg.index, avg.values, "o-", color=COLORS[s],
                label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Marginal Energy (mJ)")
    ax.set_title("Cumulative Marginal Serving Energy")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 4. Extrapolation to millions
    ax = axes[1, 0]
    extrap = [1_000_000, 10_000_000, 50_000_000, 100_000_000]
    for s in available:
        g = serving[serving.scenario == s]
        if len(g) == 0:
            continue
        comp_e = comp[comp.scenario == s]["energy_mj"].mean()
        per_imp = g["energy_mj"].sum() / g["n_impressions"].sum()
        totals = [comp_e + per_imp * n for n in extrap]
        ax.plot(extrap, totals, "o--", color=COLORS[s],
                label=labels[scenarios.index(s)])
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Total Energy (compression + serving, mJ)")
    ax.set_title("Extrapolation to 1M–100M impressions")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 5. Throughput vs energy
    ax = axes[1, 1]
    for s in available:
        g = serving[serving.scenario == s]
        if len(g) == 0:
            continue
        ax.scatter(g["throughput_mbps"], g["energy_uj_per_imp"],
                   color=COLORS[s], label=labels[scenarios.index(s)],
                   alpha=0.7, s=30)
    ax.set_xlabel("Throughput (MB/s)")
    ax.set_ylabel("Energy per imp (µJ)")
    ax.set_title("Energy efficiency vs throughput")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 6. Total+baseline breakdown at max scale
    ax = axes[1, 2]
    max_scale = serving["n_impressions"].max()
    g_max = serving[serving.n_impressions == max_scale]
    x = np.arange(len(available))
    width = 0.3
    for j, field in enumerate(["total_energy_mj", "baseline_energy_mj"]):
        color = ["#e74c3c", "#95a5a6"][j]
        label_field = ["Total", "Baseline"][j]
        vals = [g_max[g_max.scenario == s][field].mean() for s in available]
        ax.bar(x + j * width, vals, width, alpha=0.75, color=color,
               label=label_field)
    ax.set_xticks(x + width / 2)
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy (mJ)")
    ax.set_title(f"Energy breakdown @ {max_scale:,} imp")
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    fig.suptitle("Serving Energy at Scale — NEXD vs HTML5 (baseline subtracted)",
                 fontsize=13)
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure compression + serving marginal energy at scale"
    )
    parser.add_argument("--ads-dir", type=str, default=None,
                        help="Path to ads/html5 directory")
    parser.add_argument("--n-ads", type=int, default=N_ADS,
                        help=f"Number of ads (default: {N_ADS})")
    parser.add_argument("--imp-scale", type=str,
                        default=",".join(str(s) for s in DEFAULT_IMP_SCALES),
                        help="Comma-separated impression counts")
    args = parser.parse_args()

    imp_scales = [int(s.strip()) for s in args.imp_scale.split(",")]
    ads_dir = Path(args.ads_dir) if args.ads_dir else REPO_ROOT / "ads" / "html5"

    print("\n" + "=" * 80)
    print("⚡ Serving Energy at Scale with Baseline Subtraction")
    print("=" * 80)
    print(f"  Ads dir:     {ads_dir}")
    print(f"  Ad samples:  {args.n_ads}")
    print(f"  Impressions: {imp_scales}")

    if not ads_dir.exists():
        print(f"\n  ❌ Ads directory not found: {ads_dir}")
        return

    if not HAS_BROTLI:
        print("  ⚠️  brotli missing — will skip")

    ad_dirs = find_ad_dirs(ads_dir)
    test_ads = ad_dirs[:args.n_ads]
    print(f"\n  Found {len(ad_dirs)} ads, testing {len(test_ads)}\n")

    FIELDNAMES = [
        "ad_name", "scenario", "scenario_label", "phase",
        "energy_mj", "total_energy_mj", "baseline_energy_mj",
        "duration_s", "n_files", "n_impressions",
        "uncompressed_bytes", "compressed_bytes", "compression_ratio",
        "energy_uj_per_imp", "throughput_mbps",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_file = open(OUT_CSV, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
    writer.writeheader()

    results = []

    def record(r):
        if r is None:
            return
        # Fill missing columns
        for f in FIELDNAMES:
            r.setdefault(f, 0.0)
        writer.writerow(r)
        csv_file.flush()
        results.append(r)

    for idx, ad_dir in enumerate(test_ads):
        ad_name = ad_dir.name
        assets = collect_assets(ad_dir)
        if not assets:
            continue

        uncompressed_mb = total_uncompressed_size(assets) / (1024 * 1024)
        print(f"\n  {'─' * 78}")
        print(f"  [{idx + 1}/{len(test_ads)}] {ad_name}  "
              f"({len(assets)} files, {uncompressed_mb:.2f} MB)")
        print(f"  {'─' * 78}")

        for scenario in ("NEXD", "HTML5_gzip", "HTML5_brotli"):
            if scenario == "HTML5_brotli" and not HAS_BROTLI:
                continue

            # Phase 1: compression
            record(measure_compression_energy(assets, scenario, ad_name))
            time.sleep(2)

            # Get compressed blobs
            if scenario == "NEXD":
                blob, _ = compress_nexd(assets)
                blobs = [blob]
            elif scenario == "HTML5_gzip":
                blobs, _ = compress_per_file_gzip(assets)
            elif scenario == "HTML5_brotli":
                blobs, _ = compress_per_file_brotli(assets)

            # Phase 2: serving at each impression scale
            for n_imp in sorted(imp_scales):
                record(measure_serving_energy(blobs, scenario, ad_name, n_imp))
                time.sleep(2)

    csv_file.close()

    if not results:
        print("❌ No results")
        return

    df = pd.DataFrame(results)
    print(f"\n  ✅ CSV: {OUT_CSV}")

    comp = df[df.phase == "compression"]
    serving = df[df.phase == "serving"]

    print(f"\n  {'─' * 78}")
    print("  Phase 1 — Compression (one-time cost)")
    print(comp.groupby("scenario")[["energy_mj", "compression_ratio"]]
          .agg({"energy_mj": ["mean", "std"], "compression_ratio": "mean"})
          .round(4).to_string())

    available = [s for s in ("NEXD", "HTML5_gzip", "HTML5_brotli") if s in df.scenario.values]

    print(f"\n  Phase 2 — Serving (marginal, baseline subtracted)")
    if len(serving) > 0:
        pivot = serving.pivot_table(index="scenario", columns="n_impressions",
                                     values="energy_uj_per_imp", aggfunc="mean")
        print(pivot.round(3).to_string())

        # Overall per-impression cost (weighted by impressions)
        print(f"\n  Weighted per-impression cost (µJ):")
        for s in available:
            g = serving[serving.scenario == s]
            total_imp = g["n_impressions"].sum()
            total_mj  = g["energy_mj"].sum()
            avg_uj = (total_mj * 1000) / total_imp if total_imp > 0 else 0
            print(f"    {s:16s}  {avg_uj:.3f} µJ/imp")

        print(f"\n  Extrapolation:")
        for s in available:
            comp_e = comp[comp.scenario == s]["energy_mj"].mean()
            g = serving[serving.scenario == s]
            total_imp = g["n_impressions"].sum()
            total_mj  = g["energy_mj"].sum()
            per_imp_mj = total_mj / total_imp if total_imp > 0 else 0
            for millions in [1, 10, 100]:
                total_mj_ext = comp_e + per_imp_mj * millions * 1_000_000
                print(f"    {s:16s} @ {millions:4d}M imp:  "
                      f"{total_mj_ext:>12.1f} mJ  ({total_mj_ext / 3600:>8.3f} mWh)")

    make_plots(df)
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
