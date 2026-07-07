#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from statistics import mean, median

# Add repo root to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from scripts.har_utils import calculate_payloads, extract_sourcemap_embedded_bytes


def compute_corrected_payloads(har: dict, treat_acz_as_media: bool) -> tuple:
    """
    payload_media/payload_total correctos: bytes de red (calculate_payloads,
    con .acz contado como media si treat_acz_as_media) + bytes embebidos como
    data URIs en _domSnapshot.sourceMap (no aparecen como entries de red).
    """
    p_media, p_total = calculate_payloads(har, treat_acz_as_media=treat_acz_as_media)
    emb_media, emb_total = extract_sourcemap_embedded_bytes(har)
    return p_media + emb_media, p_total + emb_total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="display", help="Directorio con JSON tipo HAR (relativo al repo)")
    parser.add_argument("--treat-acz", action="store_true", help="Contar .acz (bundles NEXD) como payload_media")
    args = parser.parse_args()

    display_dir = repo_root / args.dir
    if not display_dir.exists():
        print(f"Error: {display_dir} does not exist.")
        return 1

    json_files = sorted(display_dir.glob("*.json"))
    if not json_files:
        print(f"No JSON files found in {display_dir}.")
        return 1

    ratios = []
    payloads_media = []
    payloads_total = []

    print(f"Analyzing {len(json_files)} JSON files in {display_dir} (treat_acz_as_media={args.treat_acz})...\n")

    for p in json_files:
        try:
            with p.open(encoding="utf-8") as f:
                har = json.load(f)

            p_media, p_total = compute_corrected_payloads(har, args.treat_acz)

            payloads_media.append(p_media)
            payloads_total.append(p_total)

            if p_total > 0:
                ratio = p_media / p_total
                ratios.append(ratio)
            else:
                ratios.append(0.0)
        except Exception as exc:
            print(f"Warning: failed to process {p.name}: {exc}")

    if not ratios:
        print("No valid ratios computed.")
        return 1

    print("--- RESULTS ---")
    print(f"Total analyzed ads: {len(ratios)}")
    print(f"Mean payload_total: {mean(payloads_total):,.1f} bytes")
    print(f"Median payload_total: {median(payloads_total):,.1f} bytes")
    print(f"Mean payload_media: {mean(payloads_media):,.1f} bytes")
    print(f"Median payload_media: {median(payloads_media):,.1f} bytes")
    print("")
    print(f"Mean Ratio (payload_media / payload_total): {mean(ratios) * 100:.2f}%")
    print(f"Median Ratio (payload_media / payload_total): {median(ratios) * 100:.2f}%")

if __name__ == "__main__":
    sys.exit(main())
