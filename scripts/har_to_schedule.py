#!/usr/bin/env python3
"""
har_to_schedule.py

Convierte un fichero HAR (o JSON con estructura HAR bajo `log.entries`) a un
schedule CSV consumible por `net-schedule-sim.cc`.

Formato de salida:
  time_offset_s,conn_id,protocol,direction,packet_size_bytes

`direction` es:
  - uplink: bytes enviados por el dispositivo movil (request)
  - downlink: bytes recibidos por el dispositivo movil (response)

Nota: esta implementación es deliberadamente simple y autosuficiente (sin deps).
"""

import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


def _parse_dt(s: str) -> datetime:
    # HAR suele venir con "Z" o con offset; fromisoformat soporta "+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

def _timing_ms(val) -> float:
    # HAR timings can be -1 for "not available"
    try:
        v = float(val)
    except Exception:
        return 0.0
    return v if v > 0 else 0.0


def _entry_windows(entry: dict, t0: Optional[datetime], fallback_cursor_s: float) -> Tuple[float, float, float]:
    """
    Returns (start_offset_s, send_start_s, send_duration_s).
    We model downlink bytes as being transmitted by the server roughly during the
    receive phase (after wait/TTFB).
    """
    started = entry.get("startedDateTime")
    start_offset = None
    if isinstance(started, str) and t0 is not None:
        try:
            ts = _parse_dt(started)
            start_offset = max(0.0, (ts - t0).total_seconds())
        except Exception:
            start_offset = None

    # Total request time in seconds if available
    total_time_ms = entry.get("time")
    total_s = None
    if isinstance(total_time_ms, (int, float)) and total_time_ms > 0:
        total_s = float(total_time_ms) / 1000.0

    timings = entry.get("timings") or {}
    if isinstance(timings, dict) and timings:
        blocked = _timing_ms(timings.get("blocked"))
        dns = _timing_ms(timings.get("dns"))
        connect = _timing_ms(timings.get("connect"))
        ssl = _timing_ms(timings.get("ssl"))
        send = _timing_ms(timings.get("send"))
        wait = _timing_ms(timings.get("wait"))  # TTFB
        receive = _timing_ms(timings.get("receive"))
        # Start of payload transmission ~= end of wait (TTFB)
        pre = (blocked + dns + connect + ssl + send + wait) / 1000.0
        send_duration = receive / 1000.0
        # If receive is missing but total exists, approximate remaining time as receive
        if send_duration <= 0.0 and total_s is not None:
            send_duration = max(0.0, total_s - pre)
        if send_duration <= 0.0:
            send_duration = 0.01

        if start_offset is None:
            start_offset = fallback_cursor_s
        send_start = start_offset + pre
        return start_offset, send_start, send_duration

    # Fallback if no timings dict
    if start_offset is None:
        start_offset = fallback_cursor_s
    send_start = start_offset
    send_duration = total_s if total_s is not None else 0.01
    return start_offset, send_start, max(0.01, send_duration)


def _extract_size_from_entry(entry: dict) -> int:
    if "_transferSize" in entry and isinstance(entry["_transferSize"], int) and entry["_transferSize"] >= 0:
        return entry["_transferSize"]
    resp = entry.get("response", {}) or {}
    for key in ("_transferSize", "transferSize", "bodySize"):
        val = resp.get(key)
        if isinstance(val, int) and val >= 0:
            return val
    content = resp.get("content", {}) or {}
    csize = content.get("size")
    if isinstance(csize, int) and csize >= 0:
        return csize
    return 0


def _extract_request_size_from_entry(entry: dict) -> int:
    req = entry.get("request", {}) or {}
    size = 0
    headers_size = req.get("headersSize")
    body_size = req.get("bodySize")
    if isinstance(headers_size, int) and headers_size > 0:
        size += headers_size
    else:
        method = req.get("method") or "GET"
        url = req.get("url") or ""
        http_version = req.get("httpVersion") or "HTTP/1.1"
        if isinstance(method, str) and isinstance(url, str) and isinstance(http_version, str):
            size += len(f"{method} {url} {http_version}\r\n".encode("utf-8"))
        headers = req.get("headers") or []
        if isinstance(headers, list):
            for h in headers:
                if not isinstance(h, dict):
                    continue
                name = h.get("name") or ""
                value = h.get("value") or ""
                if isinstance(name, str) and isinstance(value, str):
                    size += len(f"{name}: {value}\r\n".encode("utf-8"))
        size += 2  # final CRLF
    if isinstance(body_size, int) and body_size > 0:
        size += body_size
    post_data = req.get("postData") or {}
    if isinstance(post_data, dict):
        text = post_data.get("text")
        if isinstance(text, str):
            size = max(size, len(text.encode("utf-8")) + max(0, size - (body_size if isinstance(body_size, int) else 0)))
    return max(0, size)


def _add_segmented_rows(rows, start_s: float, duration_s: float, conn_id: str, protocol: str, direction: str, size_bytes: int) -> None:
    if size_bytes <= 0:
        return

    # Segmentacion/overhead aproximados.
    MSS = 1460
    OVERHEAD_BYTES = 60

    n_packets = int(math.ceil(size_bytes / float(MSS)))
    for p in range(n_packets):
        chunk = min(MSS, size_bytes - p * MSS)
        pkt_size = int(chunk + OVERHEAD_BYTES)
        frac = (p / max(1, n_packets))
        t = start_s + (duration_s * frac)
        rows.append((t, conn_id, protocol, direction, pkt_size))


def har_to_schedule(har_path: Path, out_csv: Path) -> None:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    entries = har.get("log", {}).get("entries", []) or []

    # Determinar t0 (mínimo startedDateTime); si no existe, usamos un cursor incremental
    t0: Optional[datetime] = None
    for e in entries:
        sdt = e.get("startedDateTime")
        if isinstance(sdt, str):
            try:
                t = _parse_dt(sdt)
                if t0 is None or t < t0:
                    t0 = t
            except Exception:
                continue

    rows = []
    # Fallback cursor to avoid collapsing everything at t=0 if startedDateTime is missing
    cursor_s = 0.0
    for idx, e in enumerate(entries):
        response_size_bytes = _extract_size_from_entry(e)
        request_size_bytes = _extract_request_size_from_entry(e)
        if response_size_bytes <= 0 and request_size_bytes <= 0:
            continue
        req = e.get("request", {}) or {}
        url = req.get("url") or ""
        protocol = "tcp"
        if isinstance(url, str) and url.lower().startswith("udp:"):
            protocol = "udp"

        # Conexión lógica: agrupamos por host si podemos, si no por índice
        conn_id = "c0"
        if isinstance(url, str) and "://" in url:
            try:
                host = url.split("://", 1)[1].split("/", 1)[0]
                conn_id = host.replace(":", "_")[:64] or "c0"
            except Exception:
                conn_id = f"c{idx}"
        else:
            conn_id = f"c{idx}"

        start_offset, send_start, send_duration = _entry_windows(e, t0, cursor_s)
        # advance cursor for entries without usable startedDateTime
        if t0 is None:
            cursor_s = max(cursor_s, start_offset) + send_duration

        timings = e.get("timings") or {}
        request_duration = 0.01
        if isinstance(timings, dict):
            request_duration = max(0.001, _timing_ms(timings.get("send")) / 1000.0)

        # When TTFB is unmeasured (wait < 0, common for file:// and some HTTPS resources),
        # send_start == start_offset which schedules UL request and DL response at the same
        # instant.  On CSMA-based simulations (WiFi 802.11 CSMA/CA, ns-3 Ethernet CSMA)
        # this creates resonant bidirectional collisions that can drop >99 % of packets.
        # Fix: offset DL by 0.8 ms — chosen as a non-integer multiple of the typical WiFi
        # connMinGapS (0.5 ms) so the two timing grids never re-synchronise.
        raw_wait = timings.get("wait") if isinstance(timings, dict) else None
        if raw_wait is None or (isinstance(raw_wait, (int, float)) and raw_wait < 0):
            _DL_TTFB_GUARD_S = 0.0008  # 0.8 ms, non-multiple of 0.5 ms connMinGapS
            send_start = send_start + _DL_TTFB_GUARD_S

        _add_segmented_rows(
            rows=rows,
            start_s=start_offset,
            duration_s=request_duration,
            conn_id=conn_id,
            protocol=protocol,
            direction="uplink",
            size_bytes=request_size_bytes,
        )
        _add_segmented_rows(
            rows=rows,
            start_s=send_start,
            duration_s=send_duration,
            conn_id=conn_id,
            protocol=protocol,
            direction="downlink",
            size_bytes=response_size_bytes,
        )

    rows.sort(key=lambda r: r[0])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_offset_s", "conn_id", "protocol", "direction", "packet_size_bytes"])
        for t, conn_id, proto, direction, size in rows:
            w.writerow([f"{t:.6f}", conn_id, proto, direction, str(int(size))])


def har_to_transactions(har_path: Path, out_csv: Path, http2: bool = False) -> None:
    """
    Convert HAR to transactional format: request/response pairs with dependencies.

    Output columns:
      entry_id, depends_on, request_bytes, response_bytes, conn_id, protocol

    Dependencies model HTTP/1.1 or HTTP/2:
      - Under HTTP/1.1 (http2=False):
        - First 6 requests on a domain have NO dependency (parallel)
        - Requests 7+ depend on earlier requests to maintain ordering
        - This reflects HTTP/1.1's ~6 simultaneous connections per domain
      - Under HTTP/2 (http2=True):
        - Single connection per domain (conn_id = {domain}#0)
        - No connection-level serialization (depends_on = ""), all parallel (multiplexed)

    This format drives TCP request/response instead of pre-timed UDP blasts,
    allowing timing to emerge from the network simulation.
    """
    har = json.loads(har_path.read_text(encoding="utf-8"))
    entries = har.get("log", {}).get("entries", []) or []

    rows = []
    domain_entries: dict = {}  # maps domain → list of entry_ids on that domain
    MAX_PARALLEL_PER_DOMAIN = 6  # HTTP/1.1 limit

    for idx, entry in enumerate(entries):
        response_size = _extract_size_from_entry(entry)
        request_size = _extract_request_size_from_entry(entry)

        if response_size <= 0 and request_size <= 0:
            continue

        req = entry.get("request", {}) or {}
        url = req.get("url") or ""
        protocol = "tcp"
        if isinstance(url, str) and url.lower().startswith("udp:"):
            protocol = "udp"

        # Extract domain for grouping
        domain = "unknown"
        if isinstance(url, str) and "://" in url:
            try:
                domain = url.split("://", 1)[1].split("/", 1)[0]
            except Exception:
                pass

        if domain not in domain_entries:
            domain_entries[domain] = []

        entry_id = f"e{idx}"

        if http2:
            # HTTP/2: single multiplexed TCP connection per domain, all requests parallel
            conn_id = f"{domain}#0"
            depends_on = ""
        else:
            # HTTP/1.1: up to 6 parallel TCP connections, queuing/dependencies for 7+
            conn_num = len(domain_entries[domain]) % MAX_PARALLEL_PER_DOMAIN
            conn_id = f"{domain}#{conn_num}"

            domain_list = domain_entries[domain]
            if len(domain_list) < MAX_PARALLEL_PER_DOMAIN:
                # First 6: no dependency
                depends_on = ""
            else:
                # Beyond 6: depend on the request 6 slots back to serialize ordering
                depends_on = domain_list[len(domain_list) - MAX_PARALLEL_PER_DOMAIN]

        domain_entries[domain].append(entry_id)
        rows.append((entry_id, depends_on, request_size, response_size, conn_id, protocol))

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entry_id", "depends_on", "request_bytes", "response_bytes", "conn_id", "protocol"])
        for entry_id, depends_on, req_bytes, resp_bytes, conn_id, protocol in rows:
            w.writerow([entry_id, depends_on, str(int(req_bytes)), str(int(resp_bytes)), conn_id, protocol])


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Convert HAR to schedule CSV (pre-timed packets) or transactions (TCP request/response).")
    ap.add_argument("input", help="Input HAR file (.har or .json)")
    ap.add_argument("output", help="Output schedule CSV")
    ap.add_argument("--transactional", action="store_true", help="Output transactional format (request/response pairs with dependencies) instead of pre-timed packets")
    ap.add_argument("--http2", action="store_true", help="Use HTTP/2 multiplexed scheduling (single connection per domain, parallel requests)")
    args = ap.parse_args()

    har_path = Path(args.input)
    out_csv = Path(args.output)
    if not har_path.exists():
        print(f"ERROR: no existe {har_path}", file=sys.stderr)
        return 2

    if args.transactional:
        har_to_transactions(har_path, out_csv, http2=args.http2)
    else:
        har_to_schedule(har_path, out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

