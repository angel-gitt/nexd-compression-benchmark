#!/usr/bin/env python3
"""
transactions_to_timed_schedule.py

Convierte un schedule transaccional (request/response con dependencias) a un
schedule pre-timed (compatible con net-schedule-sim.cc original) donde el timing
se computa basado en dependencias y latencias realistas por tecnología.

Entrada: formato transaccional
  entry_id,depends_on,request_bytes,response_bytes,conn_id,protocol

Salida: formato pre-timed (compatible)
  time_offset_s,conn_id,protocol,direction,packet_size_bytes

La idea: para cada transacción, si depends_on especifica una dependencia,
la nueva transacción comienza cuando la anterior (incluida su response)
termina, más un RTT mínimo. Esto modela latencia sin requerir TCP en ns-3.
"""

import csv
import sys
import math
from pathlib import Path
from typing import Dict, List, Tuple


def _add_segmented_rows(
    rows: list,
    start_s: float,
    duration_s: float,
    conn_id: str,
    protocol: str,
    direction: str,
    size_bytes: int
) -> None:
    if size_bytes <= 0:
        return
    MSS = 1460
    n_packets = int(math.ceil(size_bytes / float(MSS)))
    for p in range(n_packets):
        chunk = min(MSS, size_bytes - p * MSS)
        frac = (p / max(1, n_packets))
        t = start_s + (duration_s * frac)
        rows.append((t, conn_id, protocol, direction, chunk))


def transactions_to_timed_schedule(
    trans_csv: Path,
    out_csv: Path,
    tech: str = "wifi_ac",
    link_rate_bps: float = 1e8,
    rtt_ms: float = 10.0
) -> None:
    """
    Convert transactional schedule to pre-timed schedule with latency modeling.

    Parameters:
      trans_csv: input transactional schedule
      out_csv: output pre-timed schedule
      tech: technology (affects RTT: wifi_ac ~5ms, lte ~30ms, etc.)
      link_rate_bps: link rate for airtime computation
      rtt_ms: base RTT in milliseconds (can override per-tech defaults)
    """
    # Default RTTs by technology
    DEFAULT_RTT_MS = {
        "wifi_ac": 5.0,
        "wifi_n": 8.0,
        "lte": 40.0,
        "ethernet": 1.0,
        "fiber": 0.5,
    }

    if rtt_ms is None:
        rtt_ms = DEFAULT_RTT_MS.get(tech, 10.0)

    # Read transactional schedule
    transactions = []
    with trans_csv.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            transactions.append({
                "entry_id": row["entry_id"],
                "depends_on": row.get("depends_on", ""),
                "request_bytes": int(row["request_bytes"]),
                "response_bytes": int(row["response_bytes"]),
                "conn_id": row["conn_id"],
                "protocol": row.get("protocol", "tcp"),
            })

    # Compute timing for each transaction based on dependencies
    # entry_id → (request_time, response_time)
    entry_times: Dict[str, Tuple[float, float]] = {}

    for trans in transactions:
        entry_id = trans["entry_id"]
        depends_on = trans["depends_on"]
        request_bytes = trans["request_bytes"]
        response_bytes = trans["response_bytes"]

        # When does this request start?
        if not depends_on:
            # No dependency: starts at t=0
            request_start = 0.0
        else:
            # Depends on a prior transaction: starts after its response + RTT
            if depends_on not in entry_times:
                print(f"WARNING: {entry_id} depends on {depends_on}, but {depends_on} not found", file=sys.stderr)
                request_start = 0.0
            else:
                _, prior_response_end = entry_times[depends_on]
                request_start = prior_response_end + (rtt_ms / 1000.0)

        # How long does the request take?
        request_duration = (request_bytes * 8.0 / link_rate_bps) if link_rate_bps > 0 else 0.01

        # When does the response start? (after request finishes + RTT)
        response_start = request_start + request_duration + (rtt_ms / 1000.0)

        # How long does the response take?
        response_duration = (response_bytes * 8.0 / link_rate_bps) if link_rate_bps > 0 else 0.01

        # When does this transaction end?
        response_end = response_start + response_duration

        entry_times[entry_id] = (response_start, response_end)

    # Generate pre-timed schedule rows
    rows = []
    for trans in transactions:
        entry_id = trans["entry_id"]
        depends_on = trans["depends_on"]
        request_bytes = trans["request_bytes"]
        response_bytes = trans["response_bytes"]
        conn_id = trans["conn_id"]
        protocol = trans.get("protocol", "tcp")

        if depends_on not in entry_times and depends_on:
            request_start = 0.0
        elif not depends_on:
            request_start = 0.0
        else:
            _, prior_response_end = entry_times[depends_on]
            request_start = prior_response_end + (rtt_ms / 1000.0)

        request_duration = (request_bytes * 8.0 / link_rate_bps) if link_rate_bps > 0 else 0.01
        response_start = request_start + request_duration + (rtt_ms / 1000.0)
        response_duration = (response_bytes * 8.0 / link_rate_bps) if link_rate_bps > 0 else 0.01

        # Add segmented request (uplink)
        _add_segmented_rows(
            rows, request_start, request_duration, conn_id, protocol, "uplink", request_bytes
        )

        # Add segmented response (downlink)
        _add_segmented_rows(
            rows, response_start, response_duration, conn_id, protocol, "downlink", response_bytes
        )

    # Sort by time
    rows.sort(key=lambda r: r[0])

    # Write pre-timed schedule
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time_offset_s", "conn_id", "protocol", "direction", "packet_size_bytes"])
        for t, conn_id, proto, direction, size in rows:
            writer.writerow([f"{t:.6f}", conn_id, proto, direction, str(int(size))])


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Convert transactional schedule (request/response with dependencies) to pre-timed schedule."
    )
    ap.add_argument("input", help="Input transactional schedule (.csv)")
    ap.add_argument("output", help="Output pre-timed schedule (.csv)")
    ap.add_argument(
        "--tech",
        default="wifi_ac",
        help="Technology for RTT defaults: wifi_ac|wifi_n|lte|ethernet|fiber"
    )
    ap.add_argument("--rtt-ms", type=float, default=None, help="Override RTT in milliseconds")
    ap.add_argument("--link-rate-bps", type=float, default=1e8, help="Link rate in bits/second for airtime")
    args = ap.parse_args()

    transactions_to_timed_schedule(
        Path(args.input),
        Path(args.output),
        tech=args.tech,
        link_rate_bps=args.link_rate_bps,
        rtt_ms=args.rtt_ms
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
