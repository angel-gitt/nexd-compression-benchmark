#!/usr/bin/env python3
"""Regenera summary.csv post-hoc (sin re-simular ns-3)."""
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))

from run_experiment import (
    compute_payload_mb, load_results, load_profile,
    build_summary, find_har_files, har_to_schedule, log,
)

out_root      = repo_root / "results"
hars_dir      = repo_root / "hars_ads"
schedules_dir = out_root / "schedules"
results_csv   = out_root / "raw_results.csv"
summary_csv   = out_root / "summary.csv"
profiles_dir  = repo_root / "profiles"

har_files = find_har_files(hars_dir)
copied = []
for p in sorted(har_files):
    stem = p.stem
    creative_id, platform = stem.split("_", 1) if "_" in stem else (stem, "unknown")
    copied.append((p, creative_id, platform))
log(f"{len(copied)} HARs en {hars_dir}")

# Regenerar schedules desde HARs canónicos (los del run original tenían sufijos numéricos)
scripts_dir = repo_root / "scripts"
schedules_dir.mkdir(parents=True, exist_ok=True)
log("Regenerando schedules desde HARs canónicos...")
for har_path, creative_id, platform in copied:
    safe_stem = har_path.stem.replace(" ", "_")
    sched_path = schedules_dir / (safe_stem + ".csv")
    if not sched_path.exists():
        har_to_schedule(har_path, sched_path, scripts_dir, dry_run=False)
log("Schedules listos.")

payloads_mb = {}
for har_path, creative_id, platform in copied:
    key = f"{creative_id}_{platform}"
    try:
        payloads_mb[key] = compute_payload_mb(har_path)
    except Exception as exc:
        log(f"ERROR payload {har_path.name}: {exc}")

results_rows = load_results(results_csv)
log(f"{len(results_rows)} filas en raw_results.csv")

sourced_profiles = {
    "wifi_ac":  load_profile(profiles_dir / "wifi_office_ac.json"),
    "lte":      load_profile(profiles_dir / "lte_urban.json"),
}

build_summary(
    copied, payloads_mb, results_rows, summary_csv,
    subtract_idle_baseline=False,
    profiles=sourced_profiles,
    schedules_dir=schedules_dir,
)
log(f"summary.csv regenerado en {summary_csv}")
