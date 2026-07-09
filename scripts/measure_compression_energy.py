#!/usr/bin/env python3
"""
measure_compression_energy.py
=============================

Realistic simulation of serving ad creatives at scale, comparing:

  1. NEXD:  1 server → 1 TCP connection per impression (single .acz bundle)
  2. HTML5: M servers → M TCP connections per impression (parallel CDN model,
            energies summed serially as independent infrastructure)

Each impression opens real TCP connections on loopback with a discard server.
This models actual kernel TCP stack work (handshake, segmentation, ACKs).
Connections are kept alive across impressions (HTTP/1.1 keep-alive model).

The per-impression cost captures:
  - TCP 3-way handshake overhead
  - Kernel memory copy through socket buffers
  - Connection teardown

Baseline (idle) energy is measured separately and subtracted to get the
marginal energy cost of serving.

Usage:
  python3 scripts/measure_compression_energy.py
  python3 scripts/measure_compression_energy.py --ads-dir /path/to/ads/html5
  python3 scripts/measure_compression_energy.py --n-ads 5 --imp-scale 10000,50000,100000
"""

import argparse
import csv
import gzip
import hashlib
import random
import socket
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
OUT_DIR = REPO_ROOT / "results"
OUT_CSV = OUT_DIR / "compression_energy.csv"
OUT_PLOT = OUT_DIR / "plots_network" / "10_compression_energy.png"

N_ADS = 8
DEFAULT_IMP_SCALES = [5_000, 20_000, 100_000]

# CodeCarbon computes: emissions_kg = energy_kwh * CARBON_INTENSITY / 1000
# Therefore:         energy_kwh = emissions_kg * 1000 / CARBON_INTENSITY
CARBON_INTENSITY = 233.0  # gCO2e/kWh
ENERGY_KWH_PER_KG_CO2 = 1000.0 / CARBON_INTENSITY  # ≈ 4.2918

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

def compress_nexd_bundle(assets: list[Path]) -> tuple[bytes, int]:
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


def compute_blobs_for_serving(assets: list[Path], scenario: str) -> list[bytes]:
    """Compute compressed blobs without energy measurement (for checkpoint resume)."""
    if scenario == "NEXD":
        blob, _ = compress_nexd_bundle(assets)
        return [blob]
    elif scenario == "HTML5_gzip":
        blobs, _ = compress_per_file_gzip(assets)
        return blobs
    elif scenario == "HTML5_brotli":
        blobs, _ = compress_per_file_brotli(assets)
        return blobs
    raise ValueError(f"Unknown scenario: {scenario}")


# ── TCP discard server (real kernel TCP stack) ─────────────────────────────

class TCPDiscardServer:
    """Lightweight TCP server that accepts connections and discards all data."""

    def __init__(self, host: str = "127.0.0.1"):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, 0))
        self._socket.listen(256)
        self._socket.settimeout(1.0)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
                t = threading.Thread(target=self._drain, args=(conn,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

    @staticmethod
    def _drain(conn: socket.socket):
        try:
            conn.settimeout(5.0)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        try:
            self._socket.close()
        except Exception:
            pass
        self._thread.join(timeout=2)


def send_impression(blobs: list[bytes], connections: list[socket.socket]):
    """
    Send one impression: each blob through its pre-established keep-alive
    connection. The connection pool is maintained across impressions.
    This models HTTP/1.1 keep-alive or HTTP/2 multiplexing.
    """
    for blob, sock in zip(blobs, connections):
        sock.sendall(blob)


def serving_loop(blobs: list[bytes], n_impressions: int, port: int) -> tuple[int, float]:
    """Run serving loop with persistent TCP connections (keep-alive)."""
    connections = []
    try:
        for _ in blobs:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30.0)
            sock.connect(("127.0.0.1", port))
            connections.append(sock)

        t0 = time.perf_counter()
        for _ in range(n_impressions):
            send_impression(blobs, connections)
        elapsed = time.perf_counter() - t0
        return n_impressions, elapsed
    finally:
        for sock in connections:
            try:
                sock.close()
            except Exception:
                pass


# ── Energy measurement ─────────────────────────────────────────────────────

def measure_baseline_energy(duration_s: float) -> float:
    """Measure idle energy for a given duration. Returns mJ."""
    tracker = EmissionsTracker(
        log_level="error",
        save_to_file=False,
        force_carbon_intensity_g_co2e_kwh=CARBON_INTENSITY,
    )
    tracker.start()
    time.sleep(duration_s)
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * ENERGY_KWH_PER_KG_CO2 if emissions_kg else 0.0
    return energy_kwh * 3.6e6  # mJ


def measure_with_baseline(tracker_start_fn, duration_s: float) -> tuple[float, float]:
    """
    1. Run the workload, measure total energy.
    2. Measure baseline idle energy for same duration.
    Returns (marginal_mj, total_mj).
    """
    tracker = EmissionsTracker(
        log_level="error",
        save_to_file=False,
        force_carbon_intensity_g_co2e_kwh=CARBON_INTENSITY,
    )
    tracker.start()
    tracker_start_fn()
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * ENERGY_KWH_PER_KG_CO2 if emissions_kg else 0.0
    total_mj = energy_kwh * 3.6e6

    time.sleep(0.5)
    baseline_mj = measure_baseline_energy(duration_s)

    marginal_mj = max(0, total_mj - baseline_mj)
    return marginal_mj, total_mj


def warm_up(ad_dir: Path):
    """Warm up CPU and stabilise power management before real measurements."""
    assets = collect_assets(ad_dir)
    if not assets:
        return
    print("  Warming up CPU...", end=" ", flush=True)
    for _ in range(5):
        compress_per_file_gzip(assets)
    print("done\n")


# ── Measurement functions ──────────────────────────────────────────────────

def measure_compression_energy(
    assets: list[Path], scenario: str, ad_name: str
) -> tuple[Optional[dict], list[bytes]]:
    """
    Measure one-time compression energy and return compressed blobs for serving.
    Returns (result_dict, blobs).
    """
    label = {
        "NEXD": "NEXD",
        "HTML5_gzip": "HTML5-gzip",
        "HTML5_brotli": "HTML5-brotli",
    }.get(scenario, scenario)
    print(
        f"  [compress] {label:12s}  {ad_name[:45]:45s}",
        end=" ",
        flush=True,
    )

    tracker = EmissionsTracker(
        log_level="error",
        save_to_file=False,
        force_carbon_intensity_g_co2e_kwh=CARBON_INTENSITY,
    )
    tracker.start()
    t0 = time.perf_counter()

    if scenario == "NEXD":
        result_bytes, n_files = compress_nexd_bundle(assets)
        blobs = [result_bytes]
    elif scenario == "HTML5_gzip":
        blobs, n_files = compress_per_file_gzip(assets)
        result_bytes = b"".join(blobs)
    elif scenario == "HTML5_brotli":
        blobs, n_files = compress_per_file_brotli(assets)
        result_bytes = b"".join(blobs)
    else:
        raise ValueError(f"Unknown: {scenario}")

    duration_s = time.perf_counter() - t0
    emissions_kg = tracker.stop()
    energy_kwh = emissions_kg * ENERGY_KWH_PER_KG_CO2 if emissions_kg else 0.0
    energy_mj = energy_kwh * 3.6e6

    uncompressed_size = total_uncompressed_size(assets)
    compressed_size = len(result_bytes)
    ratio = uncompressed_size / compressed_size if compressed_size > 0 else 0.0

    print(
        f"✅  {energy_mj:8.3f} mJ  {compressed_size / 1024:.1f} KB  "
        f"({n_files} files)  ratio={ratio:.1f}x"
    )

    return (
        {
            "ad_name": ad_name,
            "scenario": scenario,
            "scenario_label": label,
            "phase": "compression",
            "energy_mj": round(energy_mj, 4),
            "duration_s": round(duration_s, 4),
            "n_files": n_files,
            "n_impressions": 0,
            "n_connections_per_imp": 0,
            "uncompressed_bytes": uncompressed_size,
            "compressed_bytes": compressed_size,
            "compression_ratio": round(ratio, 3),
            "total_energy_mj": round(energy_mj, 4),
            "baseline_energy_mj": 0.0,
            "energy_uj_per_imp": 0.0,
        },
        blobs,
    )


def measure_serving_energy(
    blobs: list[bytes], scenario: str, ad_name: str, n_impressions: int
) -> Optional[dict]:
    """
    Measure serving energy in a single run: start tracker, serve all impressions,
    then subtract idle baseline.
    """
    label = {
        "NEXD": "NEXD",
        "HTML5_gzip": "HTML5-gzip",
        "HTML5_brotli": "HTML5-brotli",
    }.get(scenario, scenario)
    total_bytes_per_imp = sum(len(b) for b in blobs)
    n_conn_per_imp = len(blobs)

    print(
        f"  [serving]  {label:12s}  {ad_name[:30]:30s}  "
        f"{n_impressions:>8_}imp  {n_conn_per_imp}conn/imp",
        end=" ",
        flush=True,
    )

    srv = TCPDiscardServer()
    tracker = EmissionsTracker(
        log_level="error",
        save_to_file=False,
        force_carbon_intensity_g_co2e_kwh=CARBON_INTENSITY,
    )
    tracker.start()
    t0 = time.perf_counter()
    try:
        served, _elapsed = serving_loop(blobs, n_impressions, srv.port)
    finally:
        srv.stop()
    serving_wall = time.perf_counter() - t0
    emissions_kg = tracker.stop()

    energy_kwh = emissions_kg * ENERGY_KWH_PER_KG_CO2 if emissions_kg else 0.0
    total_mj = energy_kwh * 3.6e6

    time.sleep(0.5)
    baseline_mj = measure_baseline_energy(serving_wall)

    marginal_mj = max(0, total_mj - baseline_mj)
    per_imp_uj = (marginal_mj * 1000) / served if served > 0 else 0

    total_bytes = total_bytes_per_imp * served
    throughput = total_bytes / serving_wall / 1e6 if serving_wall > 0 else 0

    print(
        f"✅  total={total_mj:.1f} base={baseline_mj:.1f} "
        f"marg={marginal_mj:.3f}mJ  {per_imp_uj:.4f}µJ/imp  "
        f"{throughput:.0f}MB/s"
    )

    return {
        "ad_name": ad_name,
        "scenario": scenario,
        "scenario_label": label,
        "phase": "serving",
        "energy_mj": round(marginal_mj, 6),
        "total_energy_mj": round(total_mj, 4),
        "baseline_energy_mj": round(baseline_mj, 4),
        "duration_s": round(serving_wall, 4),
        "n_files": len(blobs),
        "n_impressions": served,
        "n_connections_per_imp": n_conn_per_imp,
        "uncompressed_bytes": 0,
        "compressed_bytes": total_bytes_per_imp,
        "compression_ratio": 0.0,
        "energy_uj_per_imp": round(per_imp_uj, 4),
    }


# ── Plotting ───────────────────────────────────────────────────────────────

def make_plots(df: pd.DataFrame):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    rng = np.random.default_rng(42)
    scenarios = ["NEXD", "HTML5_gzip", "HTML5_brotli"]
    labels = [
        "NEXD (1 conn/imp)",
        "HTML5 (M conn/imp)",
        "HTML5-brotli (M conn/imp)",
    ]
    available = [s for s in scenarios if s in df.scenario.values]

    comp = df[df.phase == "compression"]
    sv = df[df.phase == "serving"]

    # 1. Compression energy
    ax = axes[0, 0]
    for i, s in enumerate(available):
        vals = comp[comp.scenario == s]["energy_mj"].dropna().values
        if len(vals) == 0:
            continue
        bp = ax.boxplot(
            [vals],
            positions=[i],
            patch_artist=True,
            medianprops={"color": "black", "linewidth": 2},
            widths=0.45,
        )
        bp["boxes"][0].set_facecolor(COLORS[s])
        bp["boxes"][0].set_alpha(0.7)
        ax.scatter(
            i + rng.uniform(-0.1, 0.1, len(vals)),
            vals,
            color=COLORS[s],
            alpha=0.7,
            s=30,
            edgecolors="white",
            linewidths=0.3,
            zorder=3,
        )
    ax.set_xticks(range(len(available)))
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy (mJ)")
    ax.set_title("Compression (one-time)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # 2. Per-impression energy vs impression scale
    ax = axes[0, 1]
    for s in available:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        avg = g.groupby("n_impressions")["energy_uj_per_imp"].mean()
        ax.plot(
            avg.index,
            avg.values,
            "o-",
            color=COLORS[s],
            label=labels[scenarios.index(s)],
        )
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Energy per impression (µJ)")
    ax.set_title("Marginal Serving Energy per Impression")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 3. Cumulative marginal energy
    ax = axes[0, 2]
    for s in available:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        avg = g.groupby("n_impressions")["energy_mj"].mean()
        ax.plot(
            avg.index,
            avg.values,
            "o-",
            color=COLORS[s],
            label=labels[scenarios.index(s)],
        )
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Marginal Energy (mJ)")
    ax.set_title("Cumulative Marginal Serving Energy")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 4. Extrapolation to millions
    ax = axes[1, 0]
    extrap = [1_000_000, 10_000_000, 100_000_000]
    for s in available:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        comp_e = comp[comp.scenario == s]["energy_mj"].mean()
        total_imp = g["n_impressions"].sum()
        total_mj = g["energy_mj"].sum()
        per_imp_mj = total_mj / total_imp if total_imp > 0 else 0
        totals = [comp_e + per_imp_mj * n for n in extrap]
        ax.plot(
            extrap,
            totals,
            "o--",
            color=COLORS[s],
            label=labels[scenarios.index(s)],
        )
    ax.set_xscale("log")
    ax.set_xlabel("Impressions")
    ax.set_ylabel("Total Energy (mJ)")
    ax.set_title("Extrapolation to Millions")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 5. Connections vs energy
    ax = axes[1, 1]
    for s in available:
        g = sv[sv.scenario == s]
        if len(g) == 0:
            continue
        conn_avg = g.groupby("n_impressions")["n_connections_per_imp"].mean()
        ax.scatter(
            conn_avg.values,
            g.groupby("n_impressions")["energy_uj_per_imp"].mean().values,
            color=COLORS[s],
            label=labels[scenarios.index(s)],
            alpha=0.7,
            s=50,
        )
    ax.set_xlabel("Connections per impression")
    ax.set_ylabel("Energy per imp (µJ)")
    ax.set_title("Energy vs Connections per Impression")
    ax.legend()
    ax.grid(True, linestyle="--", alpha=0.4)

    # 6. Total vs Baseline breakdown
    ax = axes[1, 2]
    max_scale = sv["n_impressions"].max()
    g_max = sv[sv.n_impressions == max_scale]
    x = np.arange(len(available))
    width = 0.3
    for j, field in enumerate(["total_energy_mj", "baseline_energy_mj"]):
        color_val = ["#e74c3c", "#95a5a6"][j]
        label_val = ["Total", "Baseline"][j]
        vals = [g_max[g_max.scenario == s][field].mean() for s in available]
        ax.bar(
            x + j * width, vals, width, alpha=0.75, color=color_val, label=label_val
        )
    ax.set_xticks(x + width / 2)
    ax.set_xticklabels([labels[scenarios.index(s)] for s in available])
    ax.set_ylabel("Energy (mJ)")
    ax.set_title(f"Energy Breakdown @ {max_scale:,} imp")
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    fig.suptitle(
        "Serving Energy: Real TCP Connections (loopback), Baseline Subtracted",
        fontsize=13,
    )
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Measure compression + serving energy at scale (TCP sockets)"
    )
    parser.add_argument("--ads-dir", type=str, default=None)
    parser.add_argument("--n-ads", type=int, default=N_ADS)
    parser.add_argument(
        "--imp-scale",
        type=str,
        default=",".join(str(s) for s in DEFAULT_IMP_SCALES),
        help="Comma-separated impression counts",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore previous CSV and start fresh",
    )
    args = parser.parse_args()

    imp_scales = [int(s.strip()) for s in args.imp_scale.split(",")]
    ads_dir = Path(args.ads_dir) if args.ads_dir else REPO_ROOT / "ads" / "html5"

    print("\n" + "=" * 80)
    print("⚡ Serving Energy at Scale — Real TCP Sockets (loopback)")
    print("=" * 80)
    print(f"  Ads dir:      {ads_dir}")
    print(f"  Ad samples:   {args.n_ads}")
    print(f"  Impressions:  {imp_scales}")
    print(f"  Connections per impression:")
    print(f"    NEXD:         1  (single .acz bundle)")
    print(f"    HTML5:        M  (one per asset, parallel CDN model)")

    if not ads_dir.exists():
        print(f"\n  ❌ Missing: {ads_dir}")
        return

    if not HAS_BROTLI:
        print("  ⚠️  brotli not installed — will skip HTML5_brotli\n")

    # ── Checkpoint / resume ──────────────────────────────────────────────
    completed = set()
    if OUT_CSV.exists() and not args.no_resume:
        try:
            existing = pd.read_csv(OUT_CSV)
            for _, row in existing.iterrows():
                completed.add(
                    (row["ad_name"], row["scenario"], row["phase"], int(row["n_impressions"]))
                )
            if completed:
                print(
                    f"  📋 Resuming: {len(completed)} measurements already in CSV"
                )
        except Exception as e:
            print(f"  ⚠️  Could not read existing CSV: {e}")

    ad_dirs = find_ad_dirs(ads_dir)
    test_ads = ad_dirs[: args.n_ads]
    print(f"\n  Found {len(ad_dirs)} ad directories, testing {len(test_ads)}\n")

    # ── Warm-up (only on fresh run) ──────────────────────────────────────
    if test_ads and (not completed or args.no_resume):
        warm_up(test_ads[0])

    FIELDNAMES = [
        "ad_name",
        "scenario",
        "scenario_label",
        "phase",
        "energy_mj",
        "total_energy_mj",
        "baseline_energy_mj",
        "duration_s",
        "n_files",
        "n_impressions",
        "n_connections_per_imp",
        "uncompressed_bytes",
        "compressed_bytes",
        "compression_ratio",
        "energy_uj_per_imp",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = OUT_CSV.exists()
    file_has_data = file_exists and OUT_CSV.stat().st_size > 0
    csv_file = open(OUT_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
    if not file_has_data:
        writer.writeheader()

    results = []

    def record(r):
        if r is None:
            return
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
        print(
            f"  [{idx + 1}/{len(test_ads)}] {ad_name}  "
            f"({len(assets)} files, {uncompressed_mb:.2f} MB uncompressed)"
        )
        print(f"  {'─' * 78}")

        # Randomize scenario order to reduce thermal bias
        scenarios_order = ["NEXD", "HTML5_gzip", "HTML5_brotli"]
        if not HAS_BROTLI:
            scenarios_order.remove("HTML5_brotli")
        random.shuffle(scenarios_order)

        for scenario in scenarios_order:
            need_compression = (ad_name, scenario, "compression", 0) not in completed
            need_any_serving = any(
                (ad_name, scenario, "serving", n) not in completed for n in imp_scales
            )

            if not need_compression and not need_any_serving:
                print(f"  [{scenario}] ✅ all complete — skipping")
                continue

            # Phase 1: compression (one-time)
            if need_compression:
                result, blobs = measure_compression_energy(assets, scenario, ad_name)
                record(result)
                time.sleep(1)
            else:
                print(
                    f"  [compress] {scenario:12s}  ⏩ skipped (already measured)"
                )
                blobs = compute_blobs_for_serving(assets, scenario)

            # Phase 2: serving with real TCP connections
            for n_imp in sorted(imp_scales):
                if (ad_name, scenario, "serving", n_imp) not in completed:
                    record(
                        measure_serving_energy(blobs, scenario, ad_name, n_imp)
                    )
                    time.sleep(1)
                else:
                    label = {
                        "NEXD": "NEXD",
                        "HTML5_gzip": "HTML5-gzip",
                        "HTML5_brotli": "HTML5-brotli",
                    }.get(scenario, scenario)
                    print(
                        f"  [serving]  {label:12s}  {ad_name[:30]:30s}  "
                        f"{n_imp:>8_}imp  ⏩ skipped"
                    )

    csv_file.close()

    if not results:
        print("❌ No new results")
        return

    df = pd.DataFrame(results)
    print(f"\n  ✅ CSV: {OUT_CSV}")

    comp = df[df.phase == "compression"]
    sv = df[df.phase == "serving"]
    available = [
        s
        for s in ("NEXD", "HTML5_gzip", "HTML5_brotli")
        if s in df.scenario.values
    ]

    print(f"\n  {'─' * 78}")
    print("  Phase 1 — Compression (one-time per creative)")
    print(
        comp.groupby("scenario")[["energy_mj", "compression_ratio"]]
        .agg({"energy_mj": ["mean", "std"], "compression_ratio": "mean"})
        .round(4)
        .to_string()
    )

    print(
        f"\n  Phase 2 — Serving (TCP on loopback, marginal energy = total − baseline)"
    )
    if len(sv) > 0:
        pivot = sv.pivot_table(
            index="scenario",
            columns="n_impressions",
            values="energy_uj_per_imp",
            aggfunc="mean",
        )
        print(pivot.round(4).to_string())

        print(f"\n  Per-connection cost (µJ per TCP connection):")
        for s in available:
            g = sv[sv.scenario == s]
            total_conn = (g["n_impressions"] * g["n_connections_per_imp"]).sum()
            total_mj = g["energy_mj"].sum()
            per_conn_uj = (
                (total_mj * 1000) / total_conn if total_conn > 0 else 0
            )
            print(
                f"    {s:16s}  {per_conn_uj:.4f} µJ/connection  "
                f"({g['n_connections_per_imp'].iloc[0]:.0f} conn/imp)"
            )

        print(f"\n  Extrapolation:")
        for s in available:
            comp_e = comp[comp.scenario == s]["energy_mj"].mean()
            total_imp = sv[sv.scenario == s]["n_impressions"].sum()
            total_mj = sv[sv.scenario == s]["energy_mj"].sum()
            per_imp_mj = total_mj / total_imp if total_imp > 0 else 0
            for millions in [1, 10, 100]:
                total_mj_ext = comp_e + per_imp_mj * millions * 1_000_000
                print(
                    f"    {s:16s} @ {millions:4d}M imp:  {total_mj_ext:>12.3f} mJ"
                    f"  ({total_mj_ext / 3.6e6:>8.3f} Wh)"
                )

    make_plots(df)
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
