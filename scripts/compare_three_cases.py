#!/usr/bin/env python3
import json
import glob
import sys
from pathlib import Path
from statistics import mean, median

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from scripts.calculate_media_ratio import compute_corrected_payloads


def get_stats_from_dir(glob_pattern, treat_acz_as_media=False):
    files = glob.glob(glob_pattern)
    if not files:
        # Check subdirectories too
        files = glob.glob(glob_pattern, recursive=True)

    totals = []
    medias = []
    ratios = []

    for f in files:
        try:
            with open(f, encoding="utf-8") as file:
                d = json.load(file)

            if "log" not in d:
                continue

            # Recompute media/total con .acz + sourceMap embebido, en vez de
            # confiar en los campos precomputados (calculados con la lógica
            # antigua, que no contaba ni .acz como media ni bytes embebidos).
            media, total = compute_corrected_payloads(d, treat_acz_as_media)

            totals.append(total)
            medias.append(media)
            if total > 0:
                ratios.append(media / total)
            else:
                ratios.append(0.0)
        except Exception:
            pass
            
    if not totals:
        return None
        
    return {
        "count": len(totals),
        "mean_total": mean(totals),
        "median_total": median(totals),
        "mean_media": mean(medias),
        "median_media": median(medias),
        "mean_ratio": mean(ratios) * 100,
        "median_ratio": median(ratios) * 100
    }

def main():
    # (glob_pattern, treat_acz_as_media) — "display" mezcla bundles .acz de
    # NEXD con iconos embebidos vía sourceMap; los demás no usan .acz.
    cases = {
        "NEXD (nexd_experiment_desktop_nexd)": ("display/*.json", True),
        "HTML5 (nexd_experiment_desktop_html5)": ("display_html5/display/*.json", False),
        "NEW (ads_html5_hiili)": ("hars_ads/html5hiili__*.json", False),
    }

    print("=== PAYLOAD COMPARISON (recomputed: .acz + sourceMap embebido) ===\n")

    for name, (pattern, treat_acz) in cases.items():
        stats = get_stats_from_dir(pattern, treat_acz)
        if stats:
            print(f"Case: {name}")
            print(f"  Count: {stats['count']} ads")
            print(f"  Total Payload: Mean = {stats['mean_total']:,.1f} B, Median = {stats['median_total']:,.1f} B")
            print(f"  Media Payload: Mean = {stats['mean_media']:,.1f} B, Median = {stats['median_media']:,.1f} B")
            print(f"  Media Ratio  : Mean = {stats['mean_ratio']:.2f}%, Median = {stats['median_ratio']:.2f}%")
            print("-" * 50)
        else:
            print(f"Case: {name} - No data found in {pattern}")
            print("-" * 50)

if __name__ == "__main__":
    main()
