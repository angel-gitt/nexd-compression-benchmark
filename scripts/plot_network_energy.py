#!/usr/bin/env python3
"""
plot_network_energy.py

Generates network-transmission energy comparison plots (nexd vs HTML5)
from the summary.csv produced by run_experiment.py.

The ad type (nexd / html5) is inferred from the creative_platform column:
  - rows whose name starts with "nexd__" are nexd creatives
  - rows whose name starts with "html5__" are HTML5 creatives

Style matches the device-side rendering energy analysis:
  - seaborn whitegrid, font DejaVu Sans
  - nexd  → #E07B39 (warm orange)
  - html5 → #4C72B0 (steel blue)

Usage:
    python3 scripts/plot_network_energy.py \
        --summary summary.csv \
        --output-dir plots_network
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

try:
    import pandas as pd
except ImportError:
    raise SystemExit("pandas is required: pip install pandas")

try:
    import seaborn as sns
    _HAS_SNS = True
except ImportError:
    _HAS_SNS = False

# ── Style constants ──────────────────────────────────────────────────────────

AD_TYPES = ["nexd", "html5", "html5hiili"]
AD_LABELS = {
    "nexd": "NEXD",
    "html5": "HTML5",
    "html5hiili": "alt. html5",
}
COLORS = {
    "nexd": "#E07B39",
    "html5": "#4C72B0",
    "html5hiili": "#5B9B5B",
}
TECH_LABELS = {
    "wifi_ac": "Wi-Fi (802.11ac)",
    "lte": "LTE / 4G",
    "ethernet": "Ethernet 1G",
    "fiber": "Fiber 10G",
}
TECH_ORDER = ["wifi_ac", "lte"]

FIG_DPI = 150
FONT_SIZE = 10

# Ads with payload below this threshold are excluded from per-MB plots.
# The 8 failed NEXD ads (CDN script blocked via file://) have ~0.001 MB
# (just the bare HTML), which produces astronomically high kWh/MB values
# and makes per-MB statistics meaningless.
MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS = 0.05  # 50 kB

if _HAS_SNS:
    sns.set_theme(style="whitegrid", font_scale=1.0)
    sns.set_palette([COLORS[a] for a in AD_TYPES])
else:
    plt.rcParams.update({
        "axes.facecolor": "white",
        "axes.grid": True,
        "grid.color": "#cccccc",
        "grid.linewidth": 0.6,
    })

plt.rcParams.update({
    "font.size": FONT_SIZE,
    "axes.titlesize": FONT_SIZE + 1,
    "axes.labelsize": FONT_SIZE,
    "xtick.labelsize": FONT_SIZE - 1,
    "ytick.labelsize": FONT_SIZE - 1,
    "legend.fontsize": FONT_SIZE - 1,
    "figure.dpi": FIG_DPI,
})

# ── Data loading ─────────────────────────────────────────────────────────────

def _infer_ad_type(creative_platform: str) -> Optional[str]:
    cp = creative_platform.lower()
    if cp.startswith("nexd__"):
        return "nexd"
    if cp.startswith("html5hiili__"):
        return "html5hiili"
    if cp.startswith("html5__"):
        return "html5"
    return None


def load_summary(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ad_type"] = df["creative_platform"].apply(_infer_ad_type)
    df = df.dropna(subset=["ad_type"])
    # Idle-baseline subtraction can leave tiny negative residuals (numerical
    # noise where the empty-schedule run slightly exceeded the measured run).
    # Negative transmission energy is unphysical, so clamp those columns to 0.
    for col in df.columns:
        if (col.endswith("_consumed_kWh") or col.endswith("_kWh_per_mb")
                or col.endswith("_kWh_per_useful_mb")):
            df[col] = pd.to_numeric(df[col], errors="coerce").clip(lower=0)
    if df.empty:
        raise SystemExit(
            f"No rows with ad_type found in {path}.\n"
            "Expected creative_platform values starting with 'nexd__', 'html5__' or 'html5hiili__'.\n"
            "Run crawl_ads.py first so that HARs are named nexd__*.json / html5__*.json / html5hiili__*.json, "
            "then run run_experiment.py to generate the summary."
        )
    return df


def _to_kwh(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _to_mwh(series: pd.Series) -> pd.Series:
    """Return values in kWh (kept for naming compatibility)."""
    return _to_kwh(series)  # kWh — no unit conversion


def _available_techs(df: pd.DataFrame) -> List[str]:
    """Return technologies for which at least some data exists."""
    available = []
    for t in TECH_ORDER:
        col = f"{t}_consumed_kWh"
        if col in df.columns and df[col].notna().any():
            available.append(t)
    return available


# ── Individual plot functions ─────────────────────────────────────────────────

def plot_mean_energy_bar(df: pd.DataFrame, output_dir: Path) -> None:
    """Bar chart: median total consumed energy (kWh) per ad type × technology."""
    techs = _available_techs(df)
    if not techs:
        return

    n_techs = len(techs)
    present_types = [a for a in AD_TYPES if (df["ad_type"] == a).any()]
    n_types = len(present_types)
    x = np.arange(n_techs)
    width = 0.8 / max(n_types, 1)
    offsets = np.linspace(-width * (n_types - 1) / 2, width * (n_types - 1) / 2, n_types)

    fig, ax = plt.subplots(figsize=(max(6, n_techs * 1.8), 4.5))

    for offset, ad_type in zip(offsets, present_types):
        grp = df[df["ad_type"] == ad_type]
        medians = []
        for t in techs:
            col = f"{t}_consumed_kWh"
            vals = _to_mwh(grp[col]).dropna() if col in grp.columns else pd.Series(dtype=float)
            medians.append(vals.median() if len(vals) else float("nan"))
        bars = ax.bar(
            x + offset, medians, width,
            label=AD_LABELS.get(ad_type, ad_type),
            color=COLORS[ad_type],
        )

    ax.set_xticks(x)
    ax.set_xticklabels([TECH_LABELS.get(t, t) for t in techs], rotation=15, ha="right")
    ax.set_ylabel("Median consumed energy (kWh) — log scale")
    ax.set_yscale("log")
    ax.set_title("Network transmission energy per ad type and access technology")
    ax.legend(title="Ad type")
    fig.tight_layout()
    path = output_dir / "01_mean_energy_bar.png"
    fig.savefig(path, dpi=FIG_DPI)
    plt.close(fig)
    print(f"  Saved: {path.name}")


def plot_boxplot_total_energy(df: pd.DataFrame, output_dir: Path) -> None:
    """Box-plot + jittered strip of total consumed energy (kWh) per ad type × technology."""
    techs = _available_techs(df)
    if not techs:
        return

    # Exclude empty/failed ads for consistency with per-MB plots
    df_filt = df[pd.to_numeric(df["payload_mb"], errors="coerce").fillna(0) >= MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS]

    fig, axes = plt.subplots(1, len(techs), figsize=(len(techs) * 3.2, 5.0), sharey=False)
    if len(techs) == 1:
        axes = [axes]

    rng = np.random.default_rng(42)
    present_types = [a for a in AD_TYPES if (df_filt["ad_type"] == a).any()]
    positions = list(range(1, len(present_types) + 1))

    for ax, tech in zip(axes, techs):
        col = f"{tech}_consumed_kWh"
        groups = [
            np.array([v for v in (_to_kwh(df_filt.loc[df_filt["ad_type"] == a, col]).dropna().tolist() if col in df_filt.columns else []) if v > 0])
            for a in present_types
        ]

        bp = ax.boxplot(
            [g.tolist() for g in groups],
            positions=positions,
            patch_artist=True,
            notch=False,
            medianprops={"color": "black", "linewidth": 2},
            whiskerprops={"linewidth": 1},
            capprops={"linewidth": 1},
            flierprops={"marker": "", "markersize": 0},
            widths=0.45,
        )
        for patch, a in zip(bp["boxes"], present_types):
            patch.set_facecolor(COLORS[a])
            patch.set_alpha(0.55)

        # jittered strip
        for pos, vals, a in zip(positions, groups, present_types):
            if len(vals) == 0:
                continue
            jitter = rng.uniform(-0.18, 0.18, size=len(vals))
            ax.scatter(pos + jitter, vals,
                       color=COLORS[a], alpha=0.55, s=18,
                       edgecolors="white", linewidths=0.3, zorder=3)

        ax.set_xlim(0.4, len(present_types) + 0.6)
        ax.set_xticks(positions)
        ax.set_xticklabels([AD_LABELS.get(a, a) for a in present_types], fontsize=FONT_SIZE - 1)
        ax.set_title(TECH_LABELS.get(tech, tech), fontsize=FONT_SIZE)
        ax.set_ylabel("Total consumed energy (kWh)" if ax is axes[0] else "")
        ax.set_yscale("log")
        ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5)

        # ylim basado en percentiles 2–98 para que la caja ocupe el panel
        all_pos = np.concatenate(groups) if groups else np.array([])
        all_pos = all_pos[all_pos > 0]
        if len(all_pos) >= 4:
            lo = np.percentile(all_pos, 2)
            hi = np.percentile(all_pos, 98)
            ax.set_ylim(lo / 5, hi * 5)

    fig.suptitle(
        "Total network transmission energy (kWh) — NEXD vs HTML5",
        y=1.01, fontsize=FONT_SIZE,
    )
    fig.tight_layout()
    path = output_dir / "01_boxplot_total_energy.png"
    fig.savefig(path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path.name}")



def plot_energy_efficiency_bar(df: pd.DataFrame, output_dir: Path) -> None:
    """Bar chart: median kWh/MB per ad type × technology (energy efficiency)."""
    techs = _available_techs(df)
    if not techs:
        return

    n_techs = len(techs)
    present_types = [a for a in AD_TYPES if (df["ad_type"] == a).any()]
    n_types = len(present_types)
    x = np.arange(n_techs)
    width = 0.8 / max(n_types, 1)
    offsets = np.linspace(-width * (n_types - 1) / 2, width * (n_types - 1) / 2, n_types)

    fig, ax = plt.subplots(figsize=(max(6, n_techs * 1.8), 4.5))

    for offset, ad_type in zip(offsets, present_types):
        grp = df[df["ad_type"] == ad_type]
        medians = []
        for t in techs:
            col = f"{t}_kWh_per_mb"
            vals = _to_kwh(grp[col]).dropna() if col in grp.columns else pd.Series(dtype=float)
            medians.append(vals.median() if len(vals) else float("nan"))
        ax.bar(
            x + offset, medians, width,
            label=AD_LABELS.get(ad_type, ad_type),
            color=COLORS[ad_type],
        )

    ax.set_xticks(x)
    ax.set_xticklabels([TECH_LABELS.get(t, t) for t in techs], rotation=15, ha="right")
    ax.set_ylabel("Median energy efficiency (kWh / MB) — log scale")
    ax.set_yscale("log")
    ax.set_title("Network energy per MB — nexd vs HTML5")
    ax.legend(title="Ad type")
    fig.tight_layout()
    path = output_dir / "02_energy_per_mb_bar.png"
    fig.savefig(path, dpi=FIG_DPI)
    plt.close(fig)
    print(f"  Saved: {path.name}")


def plot_payload_vs_energy_scatter(df: pd.DataFrame, output_dir: Path) -> None:
    """Scatter: payload (MB) vs consumed energy (kWh), one panel per tech."""
    techs = _available_techs(df)
    if not techs:
        return

    n_cols = min(len(techs), 2)
    n_rows = math.ceil(len(techs) / n_cols)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(n_cols * 5, n_rows * 4.2))
    axes_flat = np.array(axes).flatten() if hasattr(axes, "__len__") else [axes]

    payload_col = "payload_mb"

    for ax, tech in zip(axes_flat, techs):
        col = f"{tech}_consumed_kWh"
        for ad_type in AD_TYPES:
            grp = df[df["ad_type"] == ad_type]
            if col not in grp.columns or grp.empty:
                continue
            x = pd.to_numeric(grp[payload_col], errors="coerce")
            y = _to_mwh(grp[col])
            valid = x.notna() & y.notna()
            ax.scatter(x[valid], y[valid], color=COLORS[ad_type], label=AD_LABELS.get(ad_type, ad_type),
                       alpha=0.75, s=45, edgecolors="white", linewidths=0.4)

        ax.set_xlabel("Payload (MB)")
        ax.set_ylabel("Energy (kWh)")
        ax.set_yscale("log")
        ax.set_xscale("log")
        ax.set_title(TECH_LABELS.get(tech, tech))
        ax.legend(fontsize=FONT_SIZE - 2)

    # Hide unused subplots
    for ax in axes_flat[len(techs):]:
        ax.set_visible(False)

    fig.suptitle("Payload vs network transmission energy — nexd vs HTML5", y=1.01)
    fig.tight_layout()
    path = output_dir / "04_payload_vs_energy_scatter.png"
    fig.savefig(path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path.name}")


def plot_boxplot_per_mb(df: pd.DataFrame, output_dir: Path) -> None:
    """Box-plot + jittered strip of kWh per MB útil (decompressed at device).

    Uses only kWh/MB-útil — the decompressed byte count normalisation — as the
    single efficiency metric. Empty/failed ads (payload < threshold) excluded.
    """
    techs = _available_techs(df)
    if not techs:
        return

    # Exclude empty/failed ads (tiny payload → kWh/MB-útil is meaningless)
    df_filt = df[pd.to_numeric(df["payload_mb"], errors="coerce").fillna(0) >= MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS]

    fig, axes = plt.subplots(1, len(techs), figsize=(len(techs) * 3.2, 5.0), sharey=False)
    if len(techs) == 1:
        axes = [axes]

    rng = np.random.default_rng(42)
    present_types = [a for a in AD_TYPES if (df_filt["ad_type"] == a).any()]
    positions = list(range(1, len(present_types) + 1))

    for ax, tech in zip(axes, techs):
        col = f"{tech}_kWh_per_useful_mb"          # ← útil only
        groups = [
            np.array([v for v in (_to_kwh(df_filt.loc[df_filt["ad_type"] == a, col]).dropna().tolist() if col in df_filt.columns else []) if v > 0])
            for a in present_types
        ]

        bp = ax.boxplot(
            [g.tolist() for g in groups],
            positions=positions,
            patch_artist=True,
            notch=False,
            medianprops={"color": "black", "linewidth": 2},
            whiskerprops={"linewidth": 1},
            capprops={"linewidth": 1},
            flierprops={"marker": "", "markersize": 0},
            widths=0.45,
        )
        for patch, a in zip(bp["boxes"], present_types):
            patch.set_facecolor(COLORS[a])
            patch.set_alpha(0.55)

        # jittered strip
        for pos, vals, a in zip(positions, groups, present_types):
            if len(vals) == 0:
                continue
            jitter = rng.uniform(-0.18, 0.18, size=len(vals))
            ax.scatter(pos + jitter, vals,
                       color=COLORS[a], alpha=0.55, s=18,
                       edgecolors="white", linewidths=0.3, zorder=3)

        ax.set_xlim(0.4, len(present_types) + 0.6)
        ax.set_xticks(positions)
        ax.set_xticklabels([AD_LABELS.get(a, a) for a in present_types], fontsize=FONT_SIZE - 1)
        ax.set_title(TECH_LABELS.get(tech, tech), fontsize=FONT_SIZE)
        ax.set_ylabel("kWh / MB\n(decompressed at device)" if ax is axes[0] else "")
        ax.set_yscale("log")
        ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5)

        # ylim basado en percentiles 2–98 para que la caja ocupe el panel
        all_pos = np.concatenate(groups) if groups else np.array([])
        all_pos = all_pos[all_pos > 0]
        if len(all_pos) >= 4:
            lo = np.percentile(all_pos, 2)
            hi = np.percentile(all_pos, 98)
            ax.set_ylim(lo / 5, hi * 5)

    fig.suptitle(
        "Energy per MB (decompressed at device) — NEXD vs HTML5",
        y=1.01, fontsize=FONT_SIZE,
    )
    fig.tight_layout()
    path = output_dir / "06_boxplot_per_mb_util.png"
    fig.savefig(path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path.name}")


def plot_energy_per_ad_vs_richness(df: pd.DataFrame, output_dir: Path) -> None:
    """Fair per-ad comparison: energy per ad (J) vs ad richness (útil MB).

    Reports energy PER AD (no division by MB), with decompressed useful MB on
    the X axis as a richness covariate.  An OLS line per format separates:
      - intercept (a)  ≈ fixed per-ad overhead floor (tail, RTT, setup, #requests)
      - slope     (b)  ≈ marginal energy per useful MB (where compression lives)

    This avoids the unfairness of kWh/MB-útil, where dividing the ~fixed overhead
    by fewer útil MB penalises the smaller (NEXD) ads as an artefact of division.
    """
    techs = _available_techs(df)
    if not techs:
        return

    # Exclude empty/failed ads (tiny payload distorts the fit)
    df_filt = df[pd.to_numeric(df["payload_mb"], errors="coerce").fillna(0) >= MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS]

    n_cols = min(len(techs), 2)
    n_rows = math.ceil(len(techs) / n_cols)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(n_cols * 5.2, n_rows * 4.4))
    axes_flat = np.array(axes).flatten() if hasattr(axes, "__len__") else [axes]

    for ax, tech in zip(axes_flat, techs):
        col = f"{tech}_consumed_kWh"
        fit_txt = []
        for ad_type in AD_TYPES:
            grp = df_filt[df_filt["ad_type"] == ad_type]
            if col not in grp.columns or grp.empty:
                continue
            x = pd.to_numeric(grp["useful_mb"], errors="coerce")
            y = _to_kwh(grp[col]) * 3.6e6        # kWh → Joules per ad
            valid = x.notna() & y.notna() & (x > 0) & (y > 0)
            xs, ys = x[valid].values, y[valid].values
            label = AD_LABELS.get(ad_type, ad_type)
            ax.scatter(xs, ys, color=COLORS[ad_type], label=label,
                       alpha=0.7, s=42, edgecolors="white", linewidths=0.4, zorder=3)
            # OLS linear fit  J = a + b·útil   (≥3 points)
            if len(xs) >= 3:
                b, a = np.polyfit(xs, ys, 1)
                xline = np.linspace(xs.min(), xs.max(), 50)
                ax.plot(xline, a + b * xline, color=COLORS[ad_type],
                        linewidth=1.8, linestyle="--", alpha=0.9, zorder=2)
                fit_txt.append(f"{label}: a={a:.2f} J  b={b:.2f} J/MB")

        ax.set_xlabel("Ad richness — MB (decompressed)")
        ax.set_ylabel("Energy per ad (J)")
        title = TECH_LABELS.get(tech, tech)
        if tech == "lte":
            title += "  [eNB analytical EARTH Q1]"
        ax.set_title(title, fontsize=FONT_SIZE)
        ax.grid(linestyle="--", linewidth=0.5, alpha=0.4)
        ax.legend(fontsize=FONT_SIZE - 2, loc="upper left")
        if fit_txt:
            ax.text(0.98, 0.02, "\n".join(fit_txt), transform=ax.transAxes,
                    ha="right", va="bottom", fontsize=FONT_SIZE - 3,
                    bbox=dict(boxstyle="round", fc="white", ec="gray", alpha=0.8))

    for ax in axes_flat[len(techs):]:
        ax.set_visible(False)

    fig.suptitle(
        "Energy per ad vs richness (MB decompressed) — fair comparison NEXD vs HTML5\n"
        "intercept a = fixed overhead per ad  |  slope b = marginal energy per MB (where compression lives)",
        y=1.01, fontsize=FONT_SIZE,
    )
    fig.tight_layout()
    path = output_dir / "07_energy_per_ad_vs_richness.png"
    fig.savefig(path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path.name}")


def plot_dual_normalization(df: pd.DataFrame, output_dir: Path) -> None:
    """
    Plot 07 — Dual normalization boxplots: kWh/MB-transmitted (row 1) and
    kWh/MB-útil (row 2) per ad type × technology.  Layout mirrors plots 03/06:
    each cell is a boxplot + jittered strip on a log-scale Y axis.

    Empty/failed ads (payload_mb < MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS) are excluded
    so that ads whose CDN script failed don't distort the per-MB statistics.
    The real útil/wire compression ratio (median per-ad) is annotated on row 1.
    """
    techs = _available_techs(df)
    if not techs:
        return

    # Exclude empty/failed ads
    df_filt = df[pd.to_numeric(df["payload_mb"], errors="coerce").fillna(0) >= MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS]

    # Compute median per-ad útil/wire ratio for annotation
    payload_col = pd.to_numeric(df_filt["payload_mb"], errors="coerce")
    useful_col  = pd.to_numeric(df_filt["useful_mb"],  errors="coerce")
    df_filt = df_filt.copy()
    df_filt["_útil_wire"] = useful_col / payload_col.replace(0, float("nan"))

    n = len(techs)
    rng = np.random.default_rng(42)
    present_types = [a for a in AD_TYPES if (df_filt["ad_type"] == a).any()]
    positions = list(range(1, len(present_types) + 1))

    rows = [
        ("_kWh_per_mb",        "kWh / MB transmitted\n(compressed on-wire)"),
        ("_kWh_per_useful_mb", "kWh / MB\n(decompressed at device)"),
    ]

    fig, axes = plt.subplots(2, n, figsize=(n * 3.2, 7.5), sharey=False)
    if n == 1:
        axes = np.array([[axes[0]], [axes[1]]])

    for row_idx, (col_suffix, row_label) in enumerate(rows):
        for col_idx, tech in enumerate(techs):
            ax = axes[row_idx][col_idx]
            col = f"{tech}{col_suffix}"

            groups = [
                np.array([v for v in (_to_kwh(df_filt.loc[df_filt["ad_type"] == a, col]).dropna().tolist() if col in df_filt.columns else []) if v > 0])
                for a in present_types
            ]

            bp = ax.boxplot(
                [g.tolist() for g in groups],
                positions=positions,
                patch_artist=True,
                notch=False,
                medianprops={"color": "black", "linewidth": 2},
                whiskerprops={"linewidth": 1},
                capprops={"linewidth": 1},
                flierprops={"marker": "", "markersize": 0},
                widths=0.45,
            )
            for patch, a in zip(bp["boxes"], present_types):
                patch.set_facecolor(COLORS[a])
                patch.set_alpha(0.55)

            # Jittered strip overlay
            for pos, vals, a in zip(positions, groups, present_types):
                if len(vals):
                    jitter = rng.uniform(-0.18, 0.18, size=len(vals))
                    ax.scatter(pos + jitter, vals,
                               color=COLORS[a], alpha=0.55, s=18,
                               edgecolors="white", linewidths=0.3, zorder=3)

            ax.set_xlim(0.4, len(present_types) + 0.6)
            ax.set_xticks(positions)
            ax.set_xticklabels([AD_LABELS.get(a, a) for a in present_types], fontsize=FONT_SIZE - 1)
            ax.set_yscale("log")
            ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5)

            if col_idx == 0:
                ax.set_ylabel(row_label, fontsize=FONT_SIZE - 1)
            else:
                ax.set_ylabel("")
            if row_idx == 0:
                ax.set_title(TECH_LABELS.get(tech, tech), fontsize=FONT_SIZE)

            # ylim: 2nd–98th percentile of joint distribution, padded
            all_pos = np.concatenate(groups) if groups else np.array([])
            all_pos = all_pos[all_pos > 0]
            if len(all_pos) >= 4:
                lo = np.percentile(all_pos, 2)
                hi = np.percentile(all_pos, 98)
                ax.set_ylim(lo / 5, hi * 5)

            # Row 1 only: annotate median útil/wire compression ratio per ad type
            if row_idx == 0:
                ymin, ymax = ax.get_ylim()
                for pos, ad_type in zip(positions, present_types):
                    ratio_vals = df_filt.loc[df_filt["ad_type"] == ad_type, "_útil_wire"].dropna()
                    if len(ratio_vals):
                        med = ratio_vals.median()
                        ax.text(pos, ymin * 1.8,
                                f"decomp/wire\n{med:.2f}×",
                                ha="center", va="bottom",
                                fontsize=FONT_SIZE - 3,
                                color=COLORS[ad_type],
                                fontweight="bold")

    patches = [mpatches.Patch(color=COLORS[a], label=AD_LABELS.get(a, a)) for a in present_types]
    fig.legend(handles=patches, loc="upper right", fontsize=FONT_SIZE - 1,
               title="Ad type", framealpha=0.85)
    fig.suptitle(
        "Dual normalization: energy per MB transmitted vs per MB decompressed\n"
        "(MB transmitted = compressed on-wire;  MB decompressed = at device)\n"
        f"[ads with payload < {MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS*1000:.0f} kB excluded]",
        y=1.01, fontsize=FONT_SIZE,
    )
    fig.tight_layout()
    path = output_dir / "07_dual_normalization.png"
    fig.savefig(path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path.name}")


def print_stats_table(df: pd.DataFrame) -> None:
    """Print a summary statistics table to stdout."""
    techs = _available_techs(df)

    # Per-MB stats use only ads with meaningful payload (excludes failed ads)
    df_pm = df[pd.to_numeric(df["payload_mb"], errors="coerce").fillna(0) >= MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS].copy()
    payload_num = pd.to_numeric(df_pm["payload_mb"], errors="coerce")
    useful_num  = pd.to_numeric(df_pm["useful_mb"],  errors="coerce")
    df_pm["_útil_wire"] = useful_num / payload_num.replace(0, float("nan"))

    print("\n── Summary statistics (network transmission energy) ──────────────────")
    print(f"   Per-MB stats exclude ads with payload < {MIN_PAYLOAD_MB_FOR_PER_MB_PLOTS*1000:.0f} kB  (decomp/wire = per-ad median compression ratio)")
    header = (
        f"{'Tech':<18} {'Ad type':<8} {'N':>4}"
        f"  {'Mean kWh':>14}  {'Median kWh':>14}"
        f"  {'Med kWh/MB-tx':>14}  {'Med kWh/MB-decomp':>17}"
        f"  {'decomp/wire':>11}"
    )
    print(header)
    print("─" * len(header))
    for tech in techs:
        col_kwh  = f"{tech}_consumed_kWh"
        col_tx   = f"{tech}_kWh_per_mb"
        col_útil = f"{tech}_kWh_per_useful_mb"
        for ad_type in AD_TYPES:
            grp_all = df[df["ad_type"] == ad_type]
            grp_pm  = df_pm[df_pm["ad_type"] == ad_type]
            vals_kwh  = _to_mwh(grp_all[col_kwh]).dropna() if col_kwh  in grp_all.columns else pd.Series(dtype=float)
            vals_tx   = _to_kwh(grp_pm[col_tx]).dropna()   if col_tx   in grp_pm.columns  else pd.Series(dtype=float)
            vals_útil = _to_kwh(grp_pm[col_útil]).dropna() if col_útil in grp_pm.columns  else pd.Series(dtype=float)
            ratio_vals = grp_pm["_útil_wire"].dropna()
            label = AD_LABELS.get(ad_type, ad_type)
            if len(vals_kwh):
                med_tx   = vals_tx.median()   if len(vals_tx)   else float("nan")
                med_útil = vals_útil.median() if len(vals_útil) else float("nan")
                med_ratio = ratio_vals.median() if len(ratio_vals) else float("nan")
                print(
                    f"{TECH_LABELS.get(tech, tech):<18} {label:<8} {len(vals_kwh):>4}"
                    f"  {vals_kwh.mean():>14.4e}  {vals_kwh.median():>14.4e}"
                    f"  {med_tx:>14.4e}  {med_útil:>17.4e}"
                    f"  {med_ratio:>11.3f}"
                )
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Plot network energy comparison (nexd vs HTML5) from summary.csv"
    )
    ap.add_argument("--summary", default="summary.csv",
                    help="Path to summary.csv produced by run_experiment.py")
    ap.add_argument("--output-dir", default="plots_network",
                    help="Directory where PNG plots will be saved")
    args = ap.parse_args()

    summary_path = Path(args.summary).expanduser().resolve()
    if not summary_path.exists():
        raise SystemExit(f"summary.csv not found: {summary_path}")

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading: {summary_path}")
    df = load_summary(summary_path)
    counts = ", ".join(f"{a}: {(df.ad_type==a).sum()}" for a in AD_TYPES)
    print(f"Loaded {len(df)} rows  ({counts})")

    print_stats_table(df)

    print(f"Writing plots to: {output_dir}")
    plot_mean_energy_bar(df, output_dir)
    plot_boxplot_total_energy(df, output_dir)
    plot_energy_efficiency_bar(df, output_dir)
    plot_payload_vs_energy_scatter(df, output_dir)
    plot_boxplot_per_mb(df, output_dir)   # kWh/MB-útil only (plot 03)
    plot_energy_per_ad_vs_richness(df, output_dir)   # J/ad vs útil MB (plot 04)

    print("\nAll plots generated.")


if __name__ == "__main__":
    main()
