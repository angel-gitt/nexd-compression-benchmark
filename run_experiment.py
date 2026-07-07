#!/usr/bin/env python3
"""
run_experiment.py

Pipeline de simulación de consumo energético por KB transmitido en
distintas tecnologías de acceso (Wi‑Fi, LTE/4G, Ethernet y Fibra óptica) a partir
de ficheros HAR de creatividades publicitarias.

Flujo:
  1. Buscar HARs bajo un directorio raíz (por defecto
     DEV_management/s3_creatives_data/carbonfetch) y seleccionar
     una muestra de N HAR (por defecto 10) intentando cubrir
     plataformas distintas (según el nombre del directorio padre).
  2. Copiar los HAR seleccionados a `network_simulation/hars/`.
  3. Convertir cada HAR a un schedule CSV usando `scripts/har_to_schedule.py`,
     lo que ya añade el overhead de TCP/IP/TLS y la segmentación
     a MSS.
  4. Para cada schedule, lanzar el binario ns‑3 `net-schedule-sim`
     cuatro veces con un único perfil representativo por tecnología:
       - Wi‑Fi:  `profiles/wifi_office_ac.json`
       - LTE/4G: `profiles/lte_urban.json`
       - Ethernet: `profiles/ethernet_1g.json`
       - Fibra óptica: `profiles/fiber_10g.json`
  5. Leer el CSV de resultados generado por ns‑3 y construir una
     tabla resumen con, por creatividad:
       * payload_total_kb
       * consumo_total_mWh por tecnología
       * consumo_mWh_por_kb por tecnología
     Además se calculan media y varianza del consumo por KB para
     cada tecnología sobre la muestra.

Uso típico (desde la raíz del repo):

    python3 network_simulation/run_experiment.py \\
        --ns3-bin ns-3.48/build/scratch/ns3.48-net-schedule-sim-default

Requisitos:
  * El binario ns‑3 `net-schedule-sim` debe estar compilado en ns-3.48/.
  * Debe existir un árbol con HARs accesible (por defecto el mismo
    que se usa en el resto del proyecto).
"""

import argparse
import csv
import gzip
import json
import math
import random
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, unquote

from scripts.har_utils import extract_sourcemap_embedded_bytes


def log(msg: str) -> None:
    print(f"[net-sim] {msg}")

def resolve_ns3_bin(ns3_bin: Path) -> Path:
    """
    Resuelve el ejecutable real de ns-3 si el usuario pasa un path "genérico"
    (p.ej. .../build/scratch/net-schedule-sim) pero ns-3 lo ha nombrado como
    .../build/scratch/ns3.38-net-schedule-sim-default.
    """
    if ns3_bin.exists():
        return ns3_bin
    parent = ns3_bin.parent
    if not parent.exists():
        return ns3_bin
    # Preferir ejecutables con "net-schedule-sim" en el nombre
    candidates = sorted([p for p in parent.glob("*net-schedule-sim*") if p.is_file()])
    for c in candidates:
        try:
            if c.stat().st_mode & 0o111:
                return c
        except Exception:
            continue
    return ns3_bin


# ---------------------------------------------------------------------------
# Utilidades para HAR: extracción de tamaños y payload total
# ---------------------------------------------------------------------------

def _extract_size_from_entry(entry: dict) -> int:
    """
    Extrae el tamaño transferido de una entrada HAR, replicando la
    lógica de `scripts/har_to_schedule.py`.
    """
    if "_transferSize" in entry and isinstance(entry["_transferSize"], int):
        if entry["_transferSize"] >= 0:
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
        size += 2
    if isinstance(body_size, int) and body_size > 0:
        size += body_size
    return max(0, size)


def _extract_useful_size_from_entry(entry: dict) -> int:
    """
    Bytes "útiles" de la respuesta: tamaño descomprimido que llega al dispositivo.

    - Si HAR tiene content.size > 0, ese es el tamaño descomprimido (HAR spec).
    - Si content.size = 0 (Playwright con cuerpo omitido, recurso sin body, etc.),
      no tenemos info de compresión → usamos los bytes de wire como cota inferior
      (equivale a asumir sin compresión para ese recurso).
    """
    resp = entry.get("response", {}) or {}
    content = resp.get("content", {}) or {}
    csize = content.get("size")
    if isinstance(csize, int) and csize > 0:
        return csize
    # Sin info de tamaño descomprimido: asumir = bytes transmitidos (sin compresión)
    return _extract_size_from_entry(entry)


def _is_text_mime(mime: str) -> bool:
    """Returns True if the MIME type is text-based and benefits from gzip."""
    m = mime.lower().split(";")[0].strip()
    if m.startswith("text/"):
        return True
    if m in ("application/javascript", "application/x-javascript", "application/json",
             "application/xml", "application/xhtml+xml", "application/font-woff2",
             "font/woff2", "application/font-woff", "font/woff",
             "application/octet-stream"):
        # octet-stream heuristic: only gzip if the original URL hinted text
        return True
    return False


def _gzip_file_size(file_url: str, entry: dict) -> Tuple[Optional[int], Optional[int]]:
    """
    If the entry is a ``file://`` URL, read the local file and return
    ``(gzip_compressed_size, original_size)``.  Only text-based MIME types
    are gzip'd; binary media (images, video, audio) keep their original size.

    Returns ``(None, None)`` for non-local URLs.
    """
    if not file_url.startswith("file://"):
        return None, None
    try:
        path = urlparse(file_url).path
        path = unquote(path)
        data = Path(path).read_bytes()
        mime = (entry.get("response") or {}).get("content", {}).get("mimeType", "")
        if _is_text_mime(mime):
            compressed = gzip.compress(data, mtime=0)
            return len(compressed), len(data)
        # Binary media: no gzip benefit, keep original size
        return len(data), len(data)
    except Exception:
        return None, None


def compute_payload_mb(har_path: Path) -> Tuple[float, float]:
    """
    Devuelve una tupla (transmitted_mb, useful_mb) en MB (decimal, 1 MB = 1e6 bytes).

    - transmitted_mb: bytes comprimidos enviados por el cable/aire.
      Para recursos servidos por HTTP real se usa ``_transferSize`` (CDP).
      Para recursos ``file://`` se comprime el contenido local con gzip real
      (solo recursos de texto; binarios mantienen su tamaño original).
    - useful_mb: bytes descomprimidos que llega al dispositivo.

    Ambas métricas incluyen los bytes de subida (request), que no se comprimen.
    """
    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", []) or []
    transmitted_bytes = 0
    useful_bytes = 0
    for entry in entries:
        req_bytes = _extract_request_size_from_entry(entry)
        ut = _extract_useful_size_from_entry(entry)   # decompressed (≥ tx)
        # For file:// URLs, gzip text-based content to get real wire size
        url = (entry.get("request") or {}).get("url") or ""
        gz_size, raw_size = _gzip_file_size(url, entry)
        if gz_size is not None:
            tx = gz_size
            if raw_size is not None:
                ut = raw_size  # useful = original uncompressed size
        else:
            tx = _extract_size_from_entry(entry)      # wire bytes from CDP
        transmitted_bytes += tx + req_bytes
        useful_bytes += ut + req_bytes
    # Include embedded data URIs (HTML5 base64 resources) with real gzip compression
    emb_media, emb_total = extract_sourcemap_embedded_bytes(har)
    useful_bytes += emb_total
    return transmitted_bytes / 1_000_000.0, useful_bytes / 1_000_000.0


# ---------------------------------------------------------------------------
# Selección y copiado de HARs
# ---------------------------------------------------------------------------

def is_har_like(path: Path) -> bool:
    """
    Devuelve True si el fichero JSON/HTTP Archive tiene estructura
    tipo HAR (clave raíz 'log' con 'entries').

    Esto permite trabajar tanto con HARs puros (*.har) como con
    los JSON enriquecidos que usa este proyecto, que envuelven
    el HAR bajo la misma clave `log`.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        log_obj = data.get("log")
        if not isinstance(log_obj, dict):
            return False
        entries = log_obj.get("entries")
        return isinstance(entries, list)
    except Exception:
        return False


def find_har_files(root: Path) -> List[Path]:
    """
    Busca recursivamente ficheros que contengan un HAR bajo `root`.

    Se consideran candidatos tanto:
      - Ficheros con extensión `.har`
      - Ficheros `.json` que tengan una clave `log.entries` (como
        `DEV_management/s3_creatives_data/.../*.json` en este repo)
    """
    # Candidatos por extensión
    har_paths = set(root.rglob("*.har"))
    json_paths = set(root.rglob("*.json"))
    candidates = har_paths | json_paths
    # Filtrar por estructura tipo HAR
    return [p for p in sorted(candidates) if is_har_like(p)]


def group_by_platform(hars: Iterable[Path]) -> Dict[str, List[Path]]:
    """
    Agrupa HARs por "plataforma", definida aquí como el nombre del
    directorio padre inmediato (p.ej. dv360, cm360, meta, tiktok...).
    """
    grouped: Dict[str, List[Path]] = defaultdict(list)
    for p in hars:
        platform = p.parent.name
        grouped[platform].append(p)
    return grouped


def select_sample(grouped: Dict[str, List[Path]], total: int) -> List[Path]:
    """
    Selecciona una muestra de hasta `total` HARs intentando cubrir el
    máximo número de plataformas distinto (uno por plataforma en una
    primera pasada y luego rellenando si faltan).
    """
    platforms = sorted(grouped.keys())
    random.shuffle(platforms)
    selected: List[Path] = []
    for plat in platforms:
        paths = sorted(grouped[plat])
        random.shuffle(paths)
        if paths:
            selected.append(paths[0])
        if len(selected) >= total:
            break
    if len(selected) < total:
        remaining: List[Path] = []
        for plat, paths in sorted(grouped.items()):
            for p in sorted(paths):
                if p not in selected:
                    remaining.append(p)
        random.shuffle(remaining)
        needed = total - len(selected)
        selected.extend(remaining[:needed])
    return selected


def copy_hars(hars: List[Path], dest_dir: Path) -> List[Tuple[Path, str, str]]:
    """
    Copia los HAR seleccionados a `dest_dir`, renombrándolos de forma
    estable para identificar creatividad y plataforma.

    Devuelve una lista de tuplas (ruta_destino, creative_id, platform).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: List[Tuple[Path, str, str]] = []
    for idx, src in enumerate(hars):
        # Si el fichero ya tiene formato "creative_platform.ext", respetarlo para evitar colisiones
        stem = src.stem
        if "_" in stem:
            creative_id, platform = stem.split("_", 1)
        else:
            platform = src.parent.name
            creative_id = src.parent.parent.name if src.parent.parent is not None else stem
        new_name = f"{creative_id}_{platform}{src.suffix}"
        dst = dest_dir / new_name
        if dst.exists():
            # Evitar pisar si por cualquier motivo chocan nombres
            dst = dest_dir / f"{creative_id}_{platform}__{idx}{src.suffix}"
        shutil.copy2(src, dst)
        copied.append((dst, creative_id, platform))
    return copied


# ---------------------------------------------------------------------------
# Conversión HAR → schedule usando el script existente
# ---------------------------------------------------------------------------

def har_to_schedule(
    har_path: Path,
    sched_path: Path,
    scripts_dir: Path,
    dry_run: bool = False,
    transactional: bool = False,
    tech: str = "wifi_ac",
    http2: bool = False
) -> None:
    """
    Invoca `scripts/har_to_schedule.py` para generar el schedule CSV.

    Si transactional=True, usa el formato transaccional (request/response con
    dependencias) convertido a pre-timed schedule con RTT por tecnología.
    """
    if not transactional:
        # Fase 1: formato pre-timed original
        cmd = [sys.executable, str(scripts_dir / "har_to_schedule.py"), str(har_path), str(sched_path)]
        if dry_run:
            log("HAR→schedule (dry-run): " + " ".join(cmd))
            return
        log("HAR→schedule: " + har_path.name + " → " + sched_path.name)
        subprocess.run(cmd, check=True)
    else:
        # Fase 2: transaccional → pre-timed con latencias
        trans_temp = sched_path.parent / (sched_path.stem + "_transactional.csv")
        try:
            # Step 1: generate transactional schedule
            cmd1 = [sys.executable, str(scripts_dir / "har_to_schedule.py"), str(har_path), str(trans_temp), "--transactional"]
            if http2:
                cmd1.append("--http2")
            # Step 2: convert transactional to pre-timed with RTT
            cmd2 = [
                sys.executable,
                str(scripts_dir / "transactions_to_timed_schedule.py"),
                str(trans_temp),
                str(sched_path),
                "--tech", tech,
            ]
            if dry_run:
                log("HAR→transactions (dry-run): " + " ".join(cmd1))
                log("transactions→timed (dry-run): " + " ".join(cmd2))
                return
            log("HAR→schedule (Fase 2, transactional): " + har_path.name)
            subprocess.run(cmd1, check=True)
            subprocess.run(cmd2, check=True)
        finally:
            # Clean up temporary file
            if trans_temp.exists():
                trans_temp.unlink()


# ---------------------------------------------------------------------------
# Ejecución de ns‑3 para cada tecnología
# ---------------------------------------------------------------------------

def load_profile(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - protegido en runtime
            raise RuntimeError(
                "Se requiere PyYAML para leer perfiles YAML. "
                "Instala con 'pip install pyyaml'."
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def build_args(base_args: List[str], tech: str, profile: dict) -> List[str]:
    """
    Replica la lógica de `scripts/run_batch.py::build_args` para
    construir los argumentos de línea de comandos a partir de un
    perfil de tecnología.
    """
    args = list(base_args)
    duration = profile.get("duration")
    if duration is not None:
        args.append(f"--duration={duration}")
    distance = profile.get("distance")
    if distance is not None:
        args.append(f"--distance={distance}")
    energy = profile.get("energy_initial_j")
    if energy is not None:
        args.append(f"--energyInitJ={energy}")

    # Runtime / stopping behavior (nuevo setup)
    # Por defecto, autoStop por inactividad (último Rx) dentro del simulador.
    auto_stop = profile.get("auto_stop", True)
    args.append(f"--autoStop={str(bool(auto_stop)).lower()}")
    stop_guard_s = profile.get("stop_guard_s", 0.5)
    args.append(f"--stopGuardS={stop_guard_s}")

    # Arranque de apps / sockets
    app_start_delay_s = profile.get("app_start_delay_s", 2.0)
    args.append(f"--appStartDelayS={app_start_delay_s}")
    tcp_send_guard_s = profile.get("tcp_send_guard_s", 0.5)
    args.append(f"--tcpSendGuardS={tcp_send_guard_s}")
    conn_min_gap_s = profile.get("conn_min_gap_s", 0.0)
    args.append(f"--connMinGapS={conn_min_gap_s}")

    # En este proyecto los HAR suelen venir sin timings; por defecto usamos TCP real.
    force_udp = profile.get("force_udp", False)
    args.append(f"--forceUdp={str(bool(force_udp)).lower()}")

    if tech.startswith("wifi"):
        w = profile.get("wifi", {})
        standard = w.get("standard")
        if standard:
            args.append(f"--wifiStandard={standard}")
        mcs = w.get("mcs")
        if mcs:
            args.append(f"--wifiMcs={mcs}")
        width = w.get("channel_width_mhz")
        if width:
            args.append(f"--wifiChannelWidth={width}")
        power = w.get("tx_power_dbm")
        if power is not None:
            args.append(f"--wifiTxPower={power}")
        pl = w.get("pathloss", {})
        model = pl.get("model")
        if model:
            args.append(f"--pathlossModel={model}")
        exponent = pl.get("exponent")
        if exponent is not None:
            args.append(f"--pathlossExponent={exponent}")
        fading = w.get("fading")
        if fading:
            args.append(f"--fadingModel={fading}")
        power_cfg = w.get("power", {}) if isinstance(w.get("power", {}), dict) else {}
        idle_w = power_cfg.get("idle_w")
        if idle_w is not None:
            args.append(f"--wifiIdleW={idle_w}")
        tx_w = power_cfg.get("tx_w")
        if tx_w is not None:
            args.append(f"--wifiTxW={tx_w}")
        rx_w = power_cfg.get("rx_w")
        if rx_w is not None:
            args.append(f"--wifiRxW={rx_w}")
        v = power_cfg.get("voltage_v")
        if v is not None:
            args.append(f"--wifiVoltageV={v}")
        for prefix, cfg_name in (("wifiInfra", "infra_power"), ("wifiDevice", "device_power")):
            endpoint_cfg = w.get(cfg_name, {}) if isinstance(w.get(cfg_name, {}), dict) else {}
            idle_w = endpoint_cfg.get("idle_w")
            if idle_w is not None:
                args.append(f"--{prefix}IdleW={idle_w}")
            tx_w = endpoint_cfg.get("tx_w")
            if tx_w is not None:
                args.append(f"--{prefix}TxW={tx_w}")
            rx_w = endpoint_cfg.get("rx_w")
            if rx_w is not None:
                args.append(f"--{prefix}RxW={rx_w}")
    elif tech == "ethernet":
        e = profile.get("ethernet", {})
        rate = e.get("data_rate")
        if rate:
            args.append(f"--ethRate={rate}")
        delay = e.get("delay")
        if delay:
            args.append(f"--ethDelay={delay}")
        power_cfg = e.get("power", {}) if isinstance(e.get("power", {}), dict) else {}
        idle_w = power_cfg.get("idle_w")
        if idle_w is not None:
            args.append(f"--ethIdleW={idle_w}")
        tx_w = power_cfg.get("tx_w")
        if tx_w is not None:
            args.append(f"--ethTxW={tx_w}")
        rx_w = power_cfg.get("rx_w")
        if rx_w is not None:
            args.append(f"--ethRxW={rx_w}")
        v = power_cfg.get("voltage_v")
        if v is not None:
            args.append(f"--ethVoltageV={v}")
        for prefix, cfg_name in (("ethInfra", "infra_power"), ("ethDevice", "device_power")):
            endpoint_cfg = e.get(cfg_name, {}) if isinstance(e.get(cfg_name, {}), dict) else {}
            idle_w = endpoint_cfg.get("idle_w")
            if idle_w is not None:
                args.append(f"--{prefix}IdleW={idle_w}")
            tx_w = endpoint_cfg.get("tx_w")
            if tx_w is not None:
                args.append(f"--{prefix}TxW={tx_w}")
            rx_w = endpoint_cfg.get("rx_w")
            if rx_w is not None:
                args.append(f"--{prefix}RxW={rx_w}")
    elif tech == "lte":
        l = profile.get("lte", {}) if isinstance(profile.get("lte", {}), dict) else {}
        power_cfg = l.get("power", {}) if isinstance(l.get("power", {}), dict) else {}
        idle_w = power_cfg.get("idle_w")
        if idle_w is not None:
            args.append(f"--lteIdleW={idle_w}")
        tx_w = power_cfg.get("tx_w")
        if tx_w is not None:
            args.append(f"--lteTxW={tx_w}")
        rx_w = power_cfg.get("rx_w")
        if rx_w is not None:
            args.append(f"--lteRxW={rx_w}")
        v = power_cfg.get("voltage_v")
        if v is not None:
            args.append(f"--lteVoltageV={v}")
        for prefix, cfg_name in (("lteInfra", "infra_power"), ("lteDevice", "device_power")):
            endpoint_cfg = l.get(cfg_name, {}) if isinstance(l.get(cfg_name, {}), dict) else {}
            idle_w = endpoint_cfg.get("idle_w")
            if idle_w is not None:
                args.append(f"--{prefix}IdleW={idle_w}")
            tx_w = endpoint_cfg.get("tx_w")
            if tx_w is not None:
                args.append(f"--{prefix}TxW={tx_w}")
            rx_w = endpoint_cfg.get("rx_w")
            if rx_w is not None:
                args.append(f"--{prefix}RxW={rx_w}")
        tti_ms = power_cfg.get("tti_ms")
        if tti_ms is not None:
            args.append(f"--lteTtiMs={tti_ms}")
        start_delay_s = power_cfg.get("start_delay_s")
        if start_delay_s is not None:
            args.append(f"--lteStartDelayS={start_delay_s}")
        max_tb_bytes = power_cfg.get("max_tb_bytes")
        if max_tb_bytes is not None:
            args.append(f"--lteMaxTbBytes={max_tb_bytes}")
    elif tech == "fiber":
        f = profile.get("fiber", {})
        rate = f.get("data_rate")
        if rate:
            args.append(f"--fiberRate={rate}")
        delay = f.get("delay")
        if delay:
            args.append(f"--fiberDelay={delay}")
        power_cfg = f.get("power", {}) if isinstance(f.get("power", {}), dict) else {}
        idle_w = power_cfg.get("idle_w")
        if idle_w is not None:
            args.append(f"--fiberIdleW={idle_w}")
        tx_w = power_cfg.get("tx_w")
        if tx_w is not None:
            args.append(f"--fiberTxW={tx_w}")
        rx_w = power_cfg.get("rx_w")
        if rx_w is not None:
            args.append(f"--fiberRxW={rx_w}")
        v = power_cfg.get("voltage_v")
        if v is not None:
            args.append(f"--fiberVoltageV={v}")
        for prefix, cfg_name in (("fiberInfra", "infra_power"), ("fiberDevice", "device_power")):
            endpoint_cfg = f.get(cfg_name, {}) if isinstance(f.get(cfg_name, {}), dict) else {}
            idle_w = endpoint_cfg.get("idle_w")
            if idle_w is not None:
                args.append(f"--{prefix}IdleW={idle_w}")
            tx_w = endpoint_cfg.get("tx_w")
            if tx_w is not None:
                args.append(f"--{prefix}TxW={tx_w}")
            rx_w = endpoint_cfg.get("rx_w")
            if rx_w is not None:
                args.append(f"--{prefix}RxW={rx_w}")
    return args


def _lte_explicit_duration(schedule_path: Path, app_start_delay_s: float,
                            lte_start_delay_s: float, stop_guard_s: float,
                            conn_min_gap_s: float = 0.0) -> float:
    """
    Read the schedule CSV and compute an explicit simulation duration.

    The ns-3 inactivity-stop logic has a bug: OnPacketRx resets the stop
    timer to 'now + stopGuardS', which fires BEFORE the next packet is sent when
    inter-packet gaps exceed stopGuardS.  Passing --autoStop=false with an
    explicit duration derived from the last scheduled packet avoids this.

    When conn_min_gap_s > 0, ns-3 may push the last scheduled packet much later
    than the CSV's last time (by enforcing minimum per-socket inter-send gaps).
    This function simulates that enforcement so the duration is long enough.
    """
    last_t = 0.0
    last_conn_send: Dict[str, float] = {}
    try:
        with schedule_path.open(encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if not row or len(row) < 2:
                    continue
                try:
                    t = float(row[0]) + app_start_delay_s + lte_start_delay_s
                    conn_id = row[1] if len(row) > 1 else "c0"
                    direction = row[3] if len(row) >= 5 else "downlink"
                    proto = row[2] if len(row) >= 3 else "tcp"
                    socket_key = f"{conn_id}|{direction}|{proto}"
                    if conn_min_gap_s > 0.0:
                        prev = last_conn_send.get(socket_key, -1e9)
                        t = max(t, prev + conn_min_gap_s)
                    last_conn_send[socket_key] = t
                    if t > last_t:
                        last_t = t
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass
    # Add stop guard + a 30-second buffer so the sim captures the full load
    return last_t + stop_guard_s + 30.0


def run_ns3_for_schedule(
    ns3_bin: Path,
    schedule_path: Path,
    tech: str,
    profile_path: Path,
    results_csv: Path,
    sim_label: str,
    extra_args: Optional[List[str]] = None,
    dry_run: bool = False,
) -> None:
    profile = load_profile(profile_path)
    ns3_bin = resolve_ns3_bin(ns3_bin)
    base_args = [
        f"--schedule={schedule_path}",
        f"--id={sim_label}",
        f"--results={results_csv}",
        f"--tech={tech}",
    ]
    full_args = build_args(base_args, tech, profile)
    if extra_args:
        full_args.extend(extra_args)
    cmd = [str(ns3_bin)] + full_args
    if dry_run:
        log("ns-3 (dry-run): " + " ".join(cmd))
        return
    log(f"ns-3: {schedule_path.name} → {tech} ({profile_path.name})")
    import os
    env = os.environ.copy()
    # macOS: asegurar que dyld encuentra las .dylib de ns-3 si hace falta
    try:
        # .../build/scratch/<exe> -> ns-3 root
        ns3_root = ns3_bin.parents[2]
        lib_dir = ns3_root / "build" / "lib"
        if lib_dir.exists():
            prev = env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_LIBRARY_PATH"] = (str(lib_dir) + (":" + prev if prev else ""))
    except Exception:
        pass
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        log(f"ERROR ejecutando ns-3 para {schedule_path.name} / {tech} (código: {proc.returncode})")
        log(f"  Comando: {' '.join(cmd)}")
        if proc.stdout:
            log(f"  stdout: {proc.stdout.strip()}")
        if proc.stderr:
            log(f"  stderr: {proc.stderr.strip()}")
        # No lanzamos excepción: simplemente no habrá fila de resultados
        # para este (schedule, tech) concreto, y el resumen lo ignorará.
        return


def write_empty_schedule(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["time_offset_s", "conn_id", "protocol", "direction", "packet_size_bytes"])


def latest_result_for_id(results_csv: Path, sim_id: str) -> Optional[dict]:
    rows = load_results(results_csv)
    for row in reversed(rows):
        if row.get("id") == sim_id:
            return row
    return None


# ---------------------------------------------------------------------------
# Agregación de resultados y cálculo de estadísticas
# ---------------------------------------------------------------------------

def load_results(results_csv: Path) -> List[dict]:
    rows: List[dict] = []
    if not results_csv.exists():
        return rows
    with results_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def compute_stats(values: List[float]) -> Tuple[float, float]:
    """Devuelve (media, varianza) para una lista de valores."""
    if not values:
        return float("nan"), float("nan")
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, var


def _float_or_nan(value: object) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return float("nan")
    return parsed if math.isfinite(parsed) else float("nan")


def _float_or_zero(value: object) -> float:
    parsed = _float_or_nan(value)
    return parsed if not math.isnan(parsed) else 0.0


def _analytical_wired_energy_j(
    rx_total_bytes: float,
    power_cfg: dict,
    role: str,  # "infra" (server/TX side) or "device" (client/RX side)
) -> float:
    """
    Compute net active-transmission energy (J) analytically for wired links
    (Ethernet / Fiber) using a per-bit energy model:

        E_net = (active_w - idle_w) * bytes * 8 / link_rate_bps

    This isolates only the energy increment above idle, equivalent to the
    idle-baseline-subtraction the rest of the pipeline applies, but grounded
    in the literature-sourced power values stored in the profile's
    infra_power / device_power blocks.

    For infra (switch/OLT), the active state is TX (server sends data).
    For device (NIC/ONT), the active state is RX (client receives data).
    """
    if rx_total_bytes <= 0 or math.isnan(rx_total_bytes):
        return 0.0
    link_rate = power_cfg.get("link_rate_bps", 1e9)
    idle_w = power_cfg.get("idle_w", 0.0)
    if role == "infra":
        active_w = power_cfg.get("tx_w", idle_w)
    else:
        active_w = power_cfg.get("rx_w", idle_w)
    delta_w = max(active_w - idle_w, 0.0)
    if delta_w == 0.0 or link_rate <= 0.0:
        return 0.0
    # transmission time = bytes * 8 bits / link_rate
    tx_time_s = (rx_total_bytes * 8.0) / link_rate
    return delta_w * tx_time_s


def _lte_infra_energy_j(rx_total_bytes: float, energy_per_byte_j: float) -> float:
    """LTE infrastructure energy via EARTH per-bit attribution (Auer et al. 2011)."""
    if rx_total_bytes <= 0 or math.isnan(rx_total_bytes):
        return 0.0
    return max(rx_total_bytes * energy_per_byte_j, 0.0)


def _wifi_device_psm_energy_j(
    schedule_path: Path,
    device_cfg: dict,
    psm_cfg: dict,
    app_start_delay_s: float,
    sim_end_s: float,
) -> float:
    """
    WiFi device energy with PSM / U-APSD (802.11e, 2005).

    Three power states:
      TX/RX airtime  : tx_w / rx_w  — packet transmission at PHY rate
      Tail idle      : idle_w       — inactivity timeout after each burst
      Doze (sleep)   : doze_w       — radio asleep between bursts

    A 'burst' = contiguous group of packets with inter-packet gaps < burst_gap_s.

    Source: Halperin et al. HotPower 2010; Pering et al. 'Coolspots' 2006.
    """
    idle_w      = device_cfg.get("idle_w", 0.85)
    tx_w        = device_cfg.get("tx_w",   1.40)
    rx_w        = device_cfg.get("rx_w",   1.00)
    link_rate   = device_cfg.get("link_rate_bps", 2.925e8)  # VhtMcs7/80MHz/1SS PHY
    doze_w      = psm_cfg.get("doze_w",      0.025)
    tail_s      = psm_cfg.get("tail_s",      0.1)
    burst_gap_s = psm_cfg.get("burst_gap_s", 0.05)

    # Load schedule (app-relative times); shift by app_start_delay to sim-absolute
    packets: List[Tuple[float, str, int]] = []
    try:
        with schedule_path.open(encoding="utf-8") as fh:
            rdr = csv.DictReader(fh)
            for row in rdr:
                t   = float(row["time_offset_s"]) + app_start_delay_s
                pkt = int(row["packet_size_bytes"])
                packets.append((t, row["direction"], pkt))
    except Exception:
        return float("nan")

    if not packets:
        # No traffic: device dozes the whole sim
        return doze_w * sim_end_s

    packets.sort(key=lambda x: x[0])

    # ── Build bursts ────────────────────────────────────────────────────────
    # Each burst: (abs_start_s, abs_end_s, tx_airtime_s, rx_airtime_s)
    bursts: List[Tuple[float, float, float, float]] = []
    b_start = packets[0][0]
    b_end   = packets[0][0]
    b_tx    = ((packets[0][2] * 8) / link_rate) if packets[0][1] == "uplink"   else 0.0
    b_rx    = ((packets[0][2] * 8) / link_rate) if packets[0][1] == "downlink" else 0.0

    for t, direction, size in packets[1:]:
        at = (size * 8) / link_rate
        if t - b_end > burst_gap_s:
            bursts.append((b_start, b_end, b_tx, b_rx))
            b_start, b_end, b_tx, b_rx = t, t, (at if direction == "uplink" else 0.0), (at if direction == "downlink" else 0.0)
        else:
            b_end = max(b_end, t)
            if direction == "uplink":
                b_tx += at
            else:
                b_rx += at
    bursts.append((b_start, b_end, b_tx, b_rx))

    # ── Integrate energy over sim timeline (0 … sim_end_s) ─────────────────
    energy_j = 0.0
    cursor   = 0.0  # sim time of last 'awake' end

    for b_start, b_end, b_tx, b_rx in bursts:
        # Gap before this burst → doze
        energy_j += doze_w * max(0.0, b_start - cursor)

        # Burst window [b_start, b_end]
        burst_win  = max(0.0, b_end - b_start)
        active_at  = b_tx + b_rx
        idle_in_burst = max(0.0, burst_win - min(active_at, burst_win))
        energy_j  += tx_w * b_tx + rx_w * b_rx + idle_w * idle_in_burst

        # Tail after burst
        energy_j += idle_w * tail_s
        cursor     = b_end + tail_s

    # After last burst+tail → doze until sim_end
    energy_j += doze_w * max(0.0, sim_end_s - cursor)
    return energy_j


def _lte_device_rrc_drx_energy_j(
    schedule_path: Path,
    device_cfg: dict,
    rrc_cfg: dict,
    app_start_delay_s: float,
    sim_end_s: float,
) -> float:
    """
    LTE device energy with RRC/DRX state machine (Huang et al. MobiSys 2012).

    States:
      IDLE_DEEP: RRC_Idle, deep sleep (~0.59 W idle per Huang)
      CONNECTED: RRC_Connected, active or tail
      CONNECTED_ACTIVE: during TX/RX (elevated power)
      CONNECTED_TAIL: T_inactivity after last packet (5-10 s at ~1.29 W per Huang)
      PROMOTION: energy cost to transition IDLE→CONNECTED (~0.26 J per Huang)

    Model:
      - Packets at time t trigger entry to CONNECTED_ACTIVE
      - Last packet + T_inactivity → CONNECTED_TAIL
      - CONNECTED_TAIL + promotion_delay → IDLE_DEEP if no more packets
      - Each IDLE→CONNECTED transition costs promotion_j
    """
    idle_deep_w = device_cfg.get("idle_w", 0.59)  # RRC_Idle per Huang
    connected_tail_w = rrc_cfg.get("connected_tail_w", 1.29)  # RRC_Connected-idle per Huang
    tx_w = device_cfg.get("tx_w", 3.00)  # peak TX
    rx_w = device_cfg.get("rx_w", 1.20)  # peak RX
    link_rate = device_cfg.get("link_rate_bps", 1.5e8)

    t_inactivity_s = rrc_cfg.get("t_inactivity_s", 5.0)  # RRC inactivity tail (5-10 s range)
    promotion_j = rrc_cfg.get("promotion_j", 0.26)  # Energy spike IDLE→CONNECTED
    promotion_delay_s = rrc_cfg.get("promotion_delay_s", 0.1)  # Time to complete promotion

    # Load schedule
    packets: List[Tuple[float, str, int]] = []
    try:
        with schedule_path.open(encoding="utf-8") as fh:
            rdr = csv.DictReader(fh)
            for row in rdr:
                t = float(row["time_offset_s"]) + app_start_delay_s
                direction = row.get("direction", "downlink")
                pkt = int(row["packet_size_bytes"])
                packets.append((t, direction, pkt))
    except Exception:
        return float("nan")

    if not packets:
        # No traffic: device in IDLE_DEEP entire sim
        return idle_deep_w * sim_end_s

    packets.sort(key=lambda x: x[0])

    # Timeline of state transitions
    energy_j = 0.0
    cursor_s = 0.0  # Current simulation time
    rrc_state = "IDLE_DEEP"  # Start in idle
    last_packet_t = packets[-1][0]  # Last packet time
    promotions = 0  # Count IDLE→CONNECTED promotions

    # Process packets
    for t, direction, size in packets:
        # If we're in IDLE_DEEP and get a packet, transition to CONNECTED
        if rrc_state == "IDLE_DEEP" and t > cursor_s:
            # Energy from previous state (IDLE_DEEP)
            energy_j += idle_deep_w * (t - cursor_s)
            # Promotion cost
            energy_j += promotion_j
            promotions += 1
            # Transition to CONNECTED_ACTIVE
            rrc_state = "CONNECTED_ACTIVE"
            cursor_s = t

        # Active TX/RX for this packet
        if rrc_state == "CONNECTED_ACTIVE":
            airtime_s = (size * 8.0 / link_rate) if link_rate > 0 else 0.01
            active_w = tx_w if direction == "uplink" else rx_w
            energy_j += active_w * airtime_s
            cursor_s = t + airtime_s

    # After last packet: T_inactivity tail in CONNECTED state
    tail_end_s = last_packet_t + t_inactivity_s
    if tail_end_s > cursor_s:
        energy_j += connected_tail_w * (tail_end_s - cursor_s)
        cursor_s = tail_end_s
        rrc_state = "CONNECTED_TAIL"

    # After tail: back to IDLE_DEEP until sim_end
    if cursor_s < sim_end_s:
        energy_j += idle_deep_w * (sim_end_s - cursor_s)

    return energy_j


def calculate_tx_energy_kwh(tech_label: str, rx_bytes: float, tx_bytes: float, prof: dict) -> float:
    if rx_bytes <= 0 or math.isnan(rx_bytes):
        rx_bytes = 0.0
    if tx_bytes <= 0 or math.isnan(tx_bytes):
        tx_bytes = 0.0
        
    # Introduce bytes corresponding to TCP/IP and Link layer protocol headers (Ethernet, Wi-Fi, LTE)
    # to represent physical airtime and transmission energy accurately.
    MSS = 1460.0
    if tech_label == "wifi_ac":
        # TCP (20B) + IP (20B) + LLC/SNAP (8B) + 802.11 MAC (28B) + PLCP/FCS ~= 80B overhead
        rx_pkts = math.ceil(rx_bytes / MSS)
        tx_pkts = math.ceil(tx_bytes / MSS)
        rx_bytes = rx_bytes + rx_pkts * 80.0
        tx_bytes = tx_bytes + tx_pkts * 80.0
    elif tech_label == "lte":
        # TCP (20B) + IP (20B) + PDCP/RLC/MAC (20B) ~= 60B overhead
        rx_pkts = math.ceil(rx_bytes / MSS)
        tx_pkts = math.ceil(tx_bytes / MSS)
        rx_bytes = rx_bytes + rx_pkts * 60.0
        tx_bytes = tx_bytes + tx_pkts * 60.0
    else:
        # Ethernet: TCP (20B) + IP (20B) + Eth MAC/FCS (26B) + Preamble (8B) + IPG (12B) = 86B overhead
        rx_pkts = math.ceil(rx_bytes / MSS)
        tx_pkts = math.ceil(tx_bytes / MSS)
        rx_bytes = rx_bytes + rx_pkts * 86.0
        tx_bytes = tx_bytes + tx_pkts * 86.0

    total_bytes = rx_bytes + tx_bytes
    if total_bytes == 0:
        return 0.0
    
    device_cfg = prof.get("device_power", {})
    infra_cfg = prof.get("infra_power", {})
    
    link_rate = device_cfg.get("link_rate_bps") or device_cfg.get("link_rate") or 1e8
    if isinstance(link_rate, str):
        if "Gbps" in link_rate:
            link_rate = float(link_rate.replace("Gbps", "")) * 1e9
        elif "Mbps" in link_rate:
            link_rate = float(link_rate.replace("Mbps", "")) * 1e6
        else:
            link_rate = float(link_rate)
            
    if tech_label == "wifi_ac":
        doze_w = prof.get("psm", {}).get("doze_w", 0.025)
        tx_w_abs = device_cfg.get("idle_w", 0.85) + device_cfg.get("tx_w", 0.55)
        rx_w_abs = device_cfg.get("idle_w", 0.85) + device_cfg.get("rx_w", 0.15)
        
        dev_tx_j = (tx_bytes * 8.0 / link_rate) * (tx_w_abs - doze_w)
        dev_rx_j = (rx_bytes * 8.0 / link_rate) * (rx_w_abs - doze_w)
        
        infra_rate = infra_cfg.get("link_rate_bps", 2.0e8)
        infra_tx_j = (rx_bytes * 8.0 / infra_rate) * infra_cfg.get("tx_w", 1.6)
        
        total_j = dev_tx_j + dev_rx_j + infra_tx_j
        
    elif tech_label == "lte":
        idle_deep_w = device_cfg.get("idle_w", 0.59)
        tx_w_abs = device_cfg.get("tx_w", 3.0)
        rx_w_abs = device_cfg.get("rx_w", 1.2)
        
        dev_tx_j = (tx_bytes * 8.0 / link_rate) * (tx_w_abs - idle_deep_w)
        dev_rx_j = (rx_bytes * 8.0 / link_rate) * (rx_w_abs - idle_deep_w)
        
        infra_rate = infra_cfg.get("link_rate_bps", 1.5e8)
        infra_tx_j = (rx_bytes * 8.0 / infra_rate) * (infra_cfg.get("tx_w", 1292.2) - infra_cfg.get("idle_w", 712.2))
        
        total_j = dev_tx_j + dev_rx_j + infra_tx_j
        
    elif tech_label == "ethernet":
        dev_tx_j = (tx_bytes * 8.0 / link_rate) * device_cfg.get("tx_w", 0.05)
        dev_rx_j = (rx_bytes * 8.0 / link_rate) * device_cfg.get("rx_w", 0.25)
        
        infra_rate = infra_cfg.get("link_rate_bps", 1.0e9)
        infra_tx_j = (rx_bytes * 8.0 / infra_rate) * infra_cfg.get("tx_w", 0.50)
        
        total_j = dev_tx_j + dev_rx_j + infra_tx_j
        
    elif tech_label == "fiber":
        dev_tx_j = (tx_bytes * 8.0 / link_rate) * device_cfg.get("tx_w", 0.05)
        dev_rx_j = (rx_bytes * 8.0 / link_rate) * device_cfg.get("rx_w", 0.25)
        
        infra_rate = infra_cfg.get("link_rate_bps", 1.0e10)
        infra_tx_j = (rx_bytes * 8.0 / infra_rate) * infra_cfg.get("tx_w", 0.30)
        
        total_j = dev_tx_j + dev_rx_j + infra_tx_j
    else:
        total_j = 0.0
        
    return total_j / 3_600_000.0


def build_summary(
    copied_hars: List[Tuple[Path, str, str]],
    payloads_mb: Dict[str, float],
    results_rows: List[dict],
    out_summary_csv: Path,
    subtract_idle_baseline: bool = False,
    profiles: Optional[Dict[str, dict]] = None,
    schedules_dir: Optional[Path] = None,
) -> None:
    """
    Construye la tabla resumen por creatividad y calcula estadísticas
    de consumo por KB para cada tecnología.
    """
    # Índice de resultados por etiqueta de simulación
    results_by_id: Dict[str, dict] = {r["id"]: r for r in results_rows if "id" in r}

    # Estructura por creatividad
    per_creative: Dict[str, Dict[str, float]] = {}

    for har_path, creative_id, platform in copied_hars:
        key = f"{creative_id}_{platform}"
        payload_entry = payloads_mb.get(key)
        if payload_entry is None:
            continue
        if isinstance(payload_entry, tuple):
            payload_mb, useful_mb = payload_entry
        else:
            payload_mb, useful_mb = payload_entry, payload_entry
        entry: Dict[str, float] = {
            "payload_mb": payload_mb,
            "useful_mb": useful_mb,
        }
        # IDs de simulación para esta creatividad
        # Sanitise spaces so the lookup matches the sanitised --id= used in ns-3.
        safe_key_bs = key.replace(" ", "_")
        for tech_label in ("wifi_ac", "lte"):
            sim_id = f"{safe_key_bs}__{tech_label}"
            row = results_by_id.get(sim_id)
            if not row:
                continue
            try:
                kwh = float(row.get("consumed_kWh", "0.0"))
            except ValueError:
                kwh = float("nan")
            infra_kwh = _float_or_nan(row.get("infra_consumed_kWh"))
            device_kwh = _float_or_nan(row.get("device_consumed_kWh"))
            if subtract_idle_baseline:
                idle_row = results_by_id.get(f"{sim_id}__idle_baseline")
                if idle_row:
                    if not math.isnan(infra_kwh):
                        infra_kwh = infra_kwh - _float_or_zero(idle_row.get("infra_consumed_kWh"))
                    if not math.isnan(device_kwh):
                        device_kwh = device_kwh - _float_or_zero(idle_row.get("device_consumed_kWh"))
                    if not math.isnan(infra_kwh) and not math.isnan(device_kwh):
                        kwh = infra_kwh + device_kwh
                    else:
                        kwh = kwh - _float_or_zero(idle_row.get("consumed_kWh"))
            mobile_rx_bytes = _float_or_nan(row.get("mobile_rx_bytes"))
            mobile_tx_bytes = _float_or_nan(row.get("mobile_tx_bytes"))
            if not math.isnan(mobile_rx_bytes):
                entry[f"{tech_label}_mobile_rx_bytes"] = mobile_rx_bytes
            if not math.isnan(mobile_tx_bytes):
                entry[f"{tech_label}_mobile_tx_bytes"] = mobile_tx_bytes

            # ── Q1 (marginal) energy from ns-3 with idle-baseline subtraction ──────
            # Read energy directly from ns-3 simulation outputs (infra/device_consumed_kWh
            # already populated from raw_results above at lines 827–828).
            # Apply Q1 (incremental above idle) by subtracting the idle-power baseline:
            #
            #   Q1_kWh = sim_kWh − (idle_w × sim_end_s) / 3.6e6
            #
            # The idle baseline is the structural power (always on infrastructure or
            # always-powered device) that is NOT caused by this flow.  We charge only
            # the marginal energy the ad transfer caused above that baseline.
            #
            # This approach is consistent across all four technologies and reads
            # the emergent state-times from the ns-3 simulation rather than using
            # closed-form per-bit models.

            sim_end = _float_or_nan(row.get("sim_end_s"))
            _prof_key = {
                "wifi_ac": "wifi", "lte": "lte",
                "ethernet": "ethernet", "fiber": "fiber",
            }.get(tech_label)
            if profiles and _prof_key and math.isfinite(sim_end) and sim_end > 0:
                prof = profiles.get(tech_label, {}).get(_prof_key, {})

                # INFRASTRUCTURE: apply Q1 baseline subtraction
                infra_cfg = prof.get("infra_power")
                if not math.isnan(infra_kwh) and infra_cfg:
                    idle_w_infra = infra_cfg.get("idle_w", 0.0)
                    idle_baseline_kwh = (idle_w_infra * sim_end) / 3_600_000.0
                    infra_kwh_q1 = infra_kwh - idle_baseline_kwh
                    if infra_kwh_q1 > -1e-9:  # Allow for numerical noise
                        infra_kwh = max(infra_kwh_q1, 0.0)
                    if infra_kwh < 1e-10:
                        infra_kwh = 0.0

                # LTE eNB analytical energy (EARTH Q1 marginal, single-UE airtime).
                # ns-3 LENA does not instrument eNB energy; compute from mobile_rx_bytes
                # using delta_w = tx_w - idle_w (EARTH) over the single-UE airtime.
                if tech_label == "lte" and infra_kwh == 0.0 and infra_cfg:
                    _rx = _float_or_nan(row.get("mobile_rx_bytes"))
                    if not math.isnan(_rx) and _rx > 0:
                        _delta_w = infra_cfg.get("tx_w", 1292.2) - infra_cfg.get("idle_w", 712.2)
                        _link_rate = infra_cfg.get("link_rate_bps", 1.5e8)
                        _tx_time_s = (_rx * 8.0) / _link_rate
                        infra_kwh = (_delta_w * _tx_time_s) / 3_600_000.0

                # DEVICE: apply Q1 baseline subtraction (Fase 1)
                # or RRC/DRX state machine for LTE (Fase 3)
                device_cfg = prof.get("device_power")

                # FASE 3: LTE RRC/DRX state machine
                if tech_label == "lte" and device_cfg and schedules_dir is not None:
                    rrc_cfg = prof.get("rrc_drx", {})
                    if rrc_cfg:
                        app_delay = prof.get("app_start_delay_s", 2.0)
                        safe_stem = har_path.stem.replace(" ", "_")
                        sched_path = schedules_dir / (safe_stem + ".csv")
                        if sched_path.exists():
                            dev_j_rrc = _lte_device_rrc_drx_energy_j(sched_path, device_cfg, rrc_cfg, app_delay, sim_end)
                            if not math.isnan(dev_j_rrc):
                                device_kwh = dev_j_rrc / 3_600_000.0

                # FASE 1: Simple Q1 baseline subtraction (fallback for non-RRC techs or if RRC fails)
                if math.isnan(device_kwh) or (tech_label != "lte"):
                    if not math.isnan(device_kwh) and device_cfg:
                        idle_w_device = device_cfg.get("idle_w", 0.0)
                        idle_baseline_kwh = (idle_w_device * sim_end) / 3_600_000.0
                        device_kwh_q1 = device_kwh - idle_baseline_kwh
                        if device_kwh_q1 > -1e-9:
                            device_kwh = max(device_kwh_q1, 0.0)
                        if device_kwh < 1e-10:
                            device_kwh = 0.0

                # Recompute total kWh from ns-3 components
                if not (math.isnan(infra_kwh) or math.isnan(device_kwh)):
                    kwh = infra_kwh + device_kwh
            # ────────────────────────────────────────────────────────────────

            # Active transmission energy calculations (isolating the transmission from radio overhead)
            tx_kwh = float("nan")
            oh_kwh = float("nan")
            real_kwh = float("nan")
            if profiles and _prof_key and not math.isnan(mobile_rx_bytes):
                tx_kwh = calculate_tx_energy_kwh(tech_label, mobile_rx_bytes, mobile_tx_bytes, prof)
                if not math.isnan(kwh):
                    oh_kwh = max(kwh - tx_kwh, 0.0)
                    real_kwh = tx_kwh  # Radio overhead is ignored entirely (E_ad = E_transmission)
            
            entry[f"{tech_label}_consumed_kWh"] = real_kwh
            entry[f"{tech_label}_kWh_per_mb"] = real_kwh / payload_mb if payload_mb > 0 and not math.isnan(real_kwh) else float("nan")
            entry[f"{tech_label}_kWh_per_useful_mb"] = real_kwh / useful_mb if useful_mb > 0 and not math.isnan(real_kwh) else float("nan")
            
            # Keep separate tracking fields for reference
            entry[f"{tech_label}_tx_consumed_kWh"] = tx_kwh
            entry[f"{tech_label}_oh_consumed_kWh"] = oh_kwh
            entry[f"{tech_label}_real_consumed_kWh"] = real_kwh
            entry[f"{tech_label}_real_kWh_per_mb"] = real_kwh / payload_mb if payload_mb > 0 and not math.isnan(real_kwh) else float("nan")
            entry[f"{tech_label}_real_kWh_per_useful_mb"] = real_kwh / useful_mb if useful_mb > 0 and not math.isnan(real_kwh) else float("nan")

            if not math.isnan(infra_kwh):
                entry[f"{tech_label}_infra_consumed_kWh"] = infra_kwh
                entry[f"{tech_label}_infra_kWh_per_mb"] = infra_kwh / payload_mb if payload_mb > 0 else float("nan")
                entry[f"{tech_label}_infra_kWh_per_useful_mb"] = infra_kwh / useful_mb if useful_mb > 0 else float("nan")
            if not math.isnan(device_kwh):
                entry[f"{tech_label}_device_consumed_kWh"] = device_kwh
                entry[f"{tech_label}_device_kWh_per_mb"] = device_kwh / payload_mb if payload_mb > 0 else float("nan")
                entry[f"{tech_label}_device_kWh_per_useful_mb"] = device_kwh / useful_mb if useful_mb > 0 else float("nan")
        per_creative[key] = entry

    out_summary_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["creative_platform", "payload_mb", "useful_mb"]
    for tech_label in ("wifi_ac", "lte"):
        fieldnames += [
            f"{tech_label}_consumed_kWh",
            f"{tech_label}_mobile_rx_bytes",
            f"{tech_label}_mobile_tx_bytes",
            f"{tech_label}_kWh_per_mb",
            f"{tech_label}_kWh_per_useful_mb",
            f"{tech_label}_tx_consumed_kWh",
            f"{tech_label}_oh_consumed_kWh",
            f"{tech_label}_real_consumed_kWh",
            f"{tech_label}_real_kWh_per_mb",
            f"{tech_label}_real_kWh_per_useful_mb",
            f"{tech_label}_infra_consumed_kWh",
            f"{tech_label}_infra_kWh_per_mb",
            f"{tech_label}_infra_kWh_per_useful_mb",
            f"{tech_label}_device_consumed_kWh",
            f"{tech_label}_device_kWh_per_mb",
            f"{tech_label}_device_kWh_per_useful_mb",
        ]
    with out_summary_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for key, data in sorted(per_creative.items()):
            row = {
                "creative_platform": key,
                "payload_mb": f"{data.get('payload_mb', float('nan')):.6f}",
                "useful_mb": f"{data.get('useful_mb', float('nan')):.6f}",
            }
            for fn in fieldnames:
                if fn in ("creative_platform", "payload_mb", "useful_mb"):
                    continue
                row[fn] = data.get(fn)
            writer.writerow(row)

    # Cálculo de medias y varianzas de consumo por KB
    tech_values: Dict[str, List[float]] = {
        "wifi_ac": [],
        "lte": [],
        "ethernet": [],
        "fiber": [],
    }
    for data in per_creative.values():
        for tech_label in tech_values.keys():
            v = data.get(f"{tech_label}_kWh_per_mb")
            if v is not None and not math.isnan(v):
                tech_values[tech_label].append(v)

    log("Estadísticas consumo por MB transmitido (kWh/MB):")
    for tech_label, vals in tech_values.items():
        mean, var = compute_stats(vals)
        log(f"  {tech_label}: media={mean:.6g} kWh/MB, varianza={var:.6g}")

    useful_values: Dict[str, List[float]] = {t: [] for t in tech_values}
    for data in per_creative.values():
        for tech_label in useful_values.keys():
            v = data.get(f"{tech_label}_kWh_per_useful_mb")
            if v is not None and not math.isnan(v):
                useful_values[tech_label].append(v)
    log("Estadísticas consumo por MB útil/descomprimido (kWh/MB):")
    for tech_label, vals in useful_values.items():
        mean, var = compute_stats(vals)
        log(f"  {tech_label}: media={mean:.6g} kWh/MB_útil, varianza={var:.6g}")


# ---------------------------------------------------------------------------
# CLI principal
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Simulación de consumo energético por KB para HARs seleccionados.")
    ap.add_argument(
        "--ns3-bin",
        required=True,
        help="Ruta al binario ns-3 net-schedule-sim, p.ej. ns-3.48/build/scratch/ns3.48-net-schedule-sim-default",
    )
    ap.add_argument(
        "--har-root",
        default="DEV_management/s3_creatives_data/carbonfetch",
        help="Raíz donde buscar HARs (por defecto DEV_management/s3_creatives_data/carbonfetch)",
    )
    ap.add_argument(
        "--out-dir",
        default="network_simulation",
        help="Directorio donde dejar copias de HARs, schedules y resultados",
    )
    ap.add_argument(
        "--num-hars",
        type=int,
        default=10,
        help="Número de HARs a muestrear (por defecto 10)",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Semilla aleatoria para selección reproducible de HARs",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="No ejecutar conversiones ni simulaciones, solo mostrar qué se haría",
    )
    ap.add_argument(
        "--profiles-dir",
        default="profiles",
        help="Directorio de perfiles de tecnología (por defecto profiles)",
    )
    ap.add_argument(
        "--subtract-idle-baseline",
        action="store_true",
        help=(
            "Tras cada run con tráfico, ejecuta un schedule vacío durante el mismo sim_end_s "
            "y resta esa energía idle del resumen."
        ),
    )
    ap.add_argument(
        "--transactional",
        action="store_true",
        help=(
            "(FASE 2) Generar schedules transaccionales (request/response con dependencias) "
            "convertidos a pre-timed con RTT realista. Modela cómo el timing emerge del simulador."
        ),
    )
    ap.add_argument(
        "--http2",
        action="store_true",
        help="Usar HTTP/2 multiplexado en lugar de HTTP/1.1 para simulación transaccional (todos los requests paralelos, una sola conexión por dominio).",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    # run_experiment.py vive en la raíz del repo
    repo_root = Path(__file__).resolve().parent
    ns3_bin_arg = Path(args.ns3_bin)
    ns3_bin = (repo_root / ns3_bin_arg).resolve() if not ns3_bin_arg.is_absolute() else ns3_bin_arg
    ns3_bin = resolve_ns3_bin(ns3_bin)

    har_root_arg = Path(args.har_root)
    har_root = (repo_root / har_root_arg).resolve() if not har_root_arg.is_absolute() else har_root_arg

    out_root_arg = Path(args.out_dir)
    out_root = (repo_root / out_root_arg).resolve() if not out_root_arg.is_absolute() else out_root_arg
    # Evitar el caso "network_simulation/network_simulation" si el default out_dir coincide con el nombre del repo
    if out_root == (repo_root / repo_root.name).resolve():
        out_root = repo_root
    hars_dir = out_root / "hars"
    schedules_dir = out_root / "schedules"
    results_csv = out_root / "raw_results.csv"
    summary_csv = out_root / "summary.csv"
    scripts_dir = repo_root / "scripts"
    profiles_dir_arg = Path(args.profiles_dir)
    profiles_dir = (repo_root / profiles_dir_arg).resolve() if not profiles_dir_arg.is_absolute() else profiles_dir_arg

    # Validaciones básicas de rutas
    if ns3_bin.suffix == ".cc":
        log(
            f"ERROR: has pasado un fichero fuente C++ como --ns3-bin ({ns3_bin}).\n"
            "Debes compilar primero 'net-schedule-sim.cc' dentro de tu árbol de ns-3\n"
            "y luego pasar la ruta al binario resultante, por ejemplo:\n"
            "  --ns3-bin /ruta/a/ns-3/build/scratch/net-schedule-sim"
        )
        return
    if not ns3_bin.exists():
        log(
            f"ADVERTENCIA: el binario ns-3 {ns3_bin} no existe.\n"
            "Compila 'net-schedule-sim' en tu instalación de ns-3 y pasa aquí\n"
            "la ruta completa al ejecutable (no al .cc)."
        )
    if not har_root.exists():
        log(f"ADVERTENCIA: el directorio de HARs {har_root} no existe. "
            "Asegúrate de que la ruta --har-root es correcta.")

    log(f"Buscando HARs/JSON tipo HAR en {har_root} ...")
    har_files = find_har_files(har_root)
    use_existing_hars_dir = False
    if not har_files:
        # Fallback: usar los HAR ya copiados previamente en network_simulation/hars
        if hars_dir.exists():
            log(
                "No se han encontrado ficheros HAR/JSON en --har-root; "
                f"intentando usar los HAR ya presentes en {hars_dir}."
            )
            har_files = find_har_files(hars_dir)
            if not har_files:
                log("No se han encontrado ficheros HAR/JSON con estructura HAR en hars/; nada que simular.")
                return
            use_existing_hars_dir = True
        else:
            log("No se han encontrado ficheros HAR/JSON con estructura HAR; nada que simular.")
            return

    copied: List[Tuple[Path, str, str]]

    if not use_existing_hars_dir:
        grouped = group_by_platform(har_files)
        log(f"Encontrados {len(har_files)} HARs en {len(grouped)} plataformas.")
        sample = select_sample(grouped, args.num_hars)
        log(f"Seleccionados {len(sample)} HARs para la simulación.")

        copied = copy_hars(sample, hars_dir)
        for dst, creative_id, platform in copied:
            log(f"Copiado HAR: {dst.name} (creative={creative_id}, platform={platform})")
    else:
        # Usar directamente los HAR que ya están en hars/, respetando num_hars
        if len(har_files) > args.num_hars:
            random.shuffle(har_files)
            har_files = har_files[: args.num_hars]
        log(f"Usando {len(har_files)} HARs ya presentes en {hars_dir} para la simulación.")
        copied = []
        for p in sorted(har_files):
            stem = p.stem
            if "_" in stem:
                creative_id, platform = stem.split("_", 1)
            else:
                creative_id, platform = stem, "unknown"
            copied.append((p, creative_id, platform))
            log(f"Usando HAR existente: {p.name} (creative={creative_id}, platform={platform})")

    # Borrar resultados previos si existen (para evitar mezclar runs)
    if results_csv.exists() and not args.dry_run:
        results_csv.unlink()

    # Perfiles fijos por tecnología (modelos representativos)
    wifi_profile = profiles_dir / "wifi_office_ac.json"
    lte_profile = profiles_dir / "lte_urban.json"

    # Payloads por creatividad/plataforma
    payloads_mb: Dict[str, float] = {}

    schedules_dir.mkdir(parents=True, exist_ok=True)
    idle_schedule = schedules_dir / "_idle_baseline_empty.csv"
    if args.subtract_idle_baseline and not args.dry_run:
        write_empty_schedule(idle_schedule)

    for har_path, creative_id, platform in copied:
        key = f"{creative_id}_{platform}"
        # Payload total en KB
        try:
            payloads_mb[key] = compute_payload_mb(har_path)
        except Exception as exc:
            log(f"ERROR calculando payload para {har_path.name}: {exc}")
            continue

        # ns-3's CommandLine parser re-tokenises on whitespace even when Python
        # passes arguments as list elements.  Sanitise filenames and IDs.
        safe_stem = har_path.stem.replace(" ", "_")
        safe_key = key.replace(" ", "_")
        sched_path = schedules_dir / (safe_stem + ".csv")

        # For Fase 2 (transactional), regenerate for each tech with its RTT
        # For Fase 1, generate once and reuse
        if args.transactional:
            tech_list = [
                ("wifi_ac", wifi_profile, "wifi_ac"),
                ("lte", lte_profile, "lte"),
            ]
            # Regenerate schedule for each tech with its RTT
            for tech, profile, label in tech_list:
                sched_path_tech = schedules_dir / (safe_stem + f"__{tech}.csv")
                har_to_schedule(har_path, sched_path_tech, scripts_dir, dry_run=args.dry_run, transactional=True, tech=tech, http2=args.http2)
        else:
            # Fase 1: generate once, use for all techs
            har_to_schedule(har_path, sched_path, scripts_dir, dry_run=args.dry_run, transactional=False)

        # Ejecutar ns-3 para cada tecnología
        for tech, profile, label in (
            ("wifi_ac", wifi_profile, "wifi_ac"),
            ("lte", lte_profile, "lte"),
        ):
            sim_label = f"{safe_key}__{label}"
            # All techs: ns-3's inactivity-stop fires too early for schedules
            # that have gaps between resources (e.g. HTML file loads first, then
            # CDN assets).  OnSinkRx resets the stop timer to now+stopGuardS, but
            # if the next resource arrives >stopGuardS later the sim ends early.
            # Fix: always compute explicit duration from schedule and disable
            # autoStop.  This is identical to the LTE fix already in place.
            # For Fase 2, use tech-specific schedule; for Fase 1, use shared schedule
            actual_sched_path = sched_path
            if args.transactional:
                actual_sched_path = schedules_dir / (safe_stem + f"__{tech}.csv")

            extra_args: list = []
            if not args.dry_run:
                _prof_data = load_profile(profile)
                _app_delay = _prof_data.get("app_start_delay_s", 2.0)
                # LTE has an additional radio start delay
                _tech_delay = 0.0
                if tech == "lte":
                    _tech_delay = (_prof_data.get("lte", {})
                                   .get("power", {}).get("start_delay_s", 0.1))
                _guard = _prof_data.get("stop_guard_s", 0.5)
                _conn_gap = _prof_data.get("conn_min_gap_s", 0.0)
                _dur = _lte_explicit_duration(
                    actual_sched_path, _app_delay, _tech_delay, _guard, _conn_gap
                )
                extra_args = ["--autoStop=false", f"--duration={_dur:.6f}"]
            run_ns3_for_schedule(
                ns3_bin=ns3_bin,
                schedule_path=actual_sched_path,
                tech=tech,
                profile_path=profile,
                results_csv=results_csv,
                sim_label=sim_label,
                extra_args=extra_args or None,
                dry_run=args.dry_run,
            )
            if args.subtract_idle_baseline and not args.dry_run:
                row = latest_result_for_id(results_csv, sim_label)
                sim_end_s = _float_or_nan(row.get("sim_end_s") if row else None)
                if math.isfinite(sim_end_s) and sim_end_s > 0:
                    run_ns3_for_schedule(
                        ns3_bin=ns3_bin,
                        schedule_path=idle_schedule,
                        tech=tech,
                        profile_path=profile,
                        results_csv=results_csv,
                        sim_label=f"{sim_label}__idle_baseline",
                        extra_args=[
                            "--autoStop=false",
                            f"--duration={sim_end_s}",
                        ],
                        dry_run=False,
                    )
                else:
                    log(f"ADVERTENCIA: no se pudo calcular baseline idle para {sim_label}")

    # Construir resumen y estadísticas
    if not args.dry_run:
        results_rows = load_results(results_csv)
        sourced_profiles = {
            "wifi_ac": load_profile(wifi_profile),
            "lte": load_profile(lte_profile),
        }
        build_summary(
            copied,
            payloads_mb,
            results_rows,
            summary_csv,
            subtract_idle_baseline=args.subtract_idle_baseline,
            profiles=sourced_profiles,
            schedules_dir=schedules_dir,
        )
        log(f"Resumen escrito en {summary_csv}")
        log(f"Resultados crudos escritos en {results_csv}")
    else:
        log("Dry-run completado; no se han ejecutado simulaciones ni escrito CSVs.")


if __name__ == "__main__":
    main()
