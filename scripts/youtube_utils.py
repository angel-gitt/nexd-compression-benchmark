#!/usr/bin/env python3
"""
Utilidades específicas de YouTube para el crawler de creatividades.

Incluye:
  - Carga y normalización de cookies desde S3.
  - Inyección de cookies en el driver de Selenium.
  - Extracción de ytInitialPlayerResponse desde el HTML de YouTube.
  - Estimación de bytes de vídeo/audio a partir de streamingData.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Cookies de YouTube
# ---------------------------------------------------------------------------


def _normalize_cookies_payload(data: Any) -> List[dict]:
    """
    Normaliza un payload de cookies proveniente de JSON.

    Formatos soportados:
      - Lista directa de cookies
      - Diccionario con clave 'cookies' / 'Cookies' / 'CookieList'
    """
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict):
        for key in ("cookies", "Cookies", "CookieList"):
            value = data.get(key)
            if isinstance(value, list):
                return [c for c in value if isinstance(c, dict)]
    raise ValueError("Formato de cookies JSON no soportado; se esperaba lista de objetos cookie.")


def load_youtube_cookies(config: Any, s3_client, log) -> Optional[List[dict]]:
    """
    Carga cookies de YouTube desde la raíz del bucket de input.

    - Bucket: config.input_bucket
    - Key: config.cookies_key (por defecto: 'youtube_cookies.json')
    """
    cookies_key = getattr(config, "cookies_key", None)
    input_bucket = getattr(config, "input_bucket", None)
    if not cookies_key or not input_bucket:
        return None
    try:
        obj = s3_client.get_object(Bucket=input_bucket, Key=cookies_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404", "NotFound"}:
            if log:
                log.info(
                    f"No YouTube cookies JSON found at s3://{input_bucket}/{cookies_key}; "
                    "continuing without cookies."
                )
            return None
        raise

    try:
        body = obj["Body"].read().decode("utf-8", errors="replace")
        raw = json.loads(body)
        cookies = _normalize_cookies_payload(raw)
        # If normalization produced no valid cookie dicts, behave as if there
        # were no usable cookies at all.
        if not cookies:
            if log:
                log.info(
                    f"No valid YouTube cookies found in s3://{input_bucket}/{cookies_key}; "
                    "continuing without cookies."
                )
            return None
        if log:
            log.info(
                f"Loaded {len(cookies)} YouTube cookies from s3://{input_bucket}/{cookies_key}"
            )
        return cookies
    except Exception as exc:
        if log:
            log.info(
                f"Failed to parse YouTube cookies from s3://{input_bucket}/{cookies_key}: {exc}"
            )
        return None


def inject_cookies(
    driver,
    cookies: Optional[List[dict]],
    base_url: str = "https://www.youtube.com",
) -> None:
    """
    Inyecta cookies en el driver para el dominio indicado.

    Se asume que las cookies provienen de un JSON exportado del navegador
    (formato similar al usado por extensiones tipo "EditThisCookie" o
    "Get cookies.txt").
    """
    if not cookies:
        return
    # Es necesario visitar primero el dominio base para poder añadir cookies
    driver.get(base_url)
    for c in cookies:
        try:
            name = c.get("name") or c.get("Name")
            if not name:
                continue
            cookie: Dict[str, Any] = {
                "name": name,
                "value": c.get("value") or c.get("Value") or "",
                "domain": c.get("domain") or c.get("Domain") or ".youtube.com",
                "path": c.get("path") or c.get("Path") or "/",
            }
            exp = c.get("expiry") or c.get("expirationDate")
            if exp is not None:
                try:
                    cookie["expiry"] = int(exp)
                except Exception:
                    pass
            if c.get("secure") is not None:
                cookie["secure"] = bool(c.get("secure"))
            if c.get("httpOnly") is not None:
                cookie["httpOnly"] = bool(c.get("httpOnly"))
            driver.add_cookie(cookie)
        except Exception:
            # No queremos abortar todo el flujo por una cookie inválida
            continue


# ---------------------------------------------------------------------------
# ytInitialPlayerResponse
# ---------------------------------------------------------------------------


_PLAYER_RESPONSE_RE = re.compile(
    r"var ytInitialPlayerResponse\s*=\s*(\{.*?\});", re.DOTALL
)


def extract_initial_player_response(html: str) -> Optional[dict]:
    """
    Extrae ytInitialPlayerResponse del HTML de YouTube.

    Returns:
        Diccionario con el playerResponse, o None si no se pudo extraer.
    """
    if not html:
        return None
    match = _PLAYER_RESPONSE_RE.search(html)
    if not match:
        return None
    blob = match.group(1)
    try:
        data = json.loads(blob)
    except Exception:
        return None
    if isinstance(data, dict):
        return data
    return None


def build_player_response_summary(player_response: dict) -> dict:
    """
    Construye un resumen compacto de ytInitialPlayerResponse para almacenarlo en el HAR.
    """
    streaming = player_response.get("streamingData", {}) or {}
    formats_summary: List[dict] = []
    for f in streaming.get("formats", []) or []:
        if not isinstance(f, dict):
            continue
        formats_summary.append(
            {
                "itag": f.get("itag"),
                "mimeType": f.get("mimeType"),
                "bitrate": f.get("bitrate") or f.get("averageBitrate"),
                "width": f.get("width"),
                "height": f.get("height"),
                "contentLength": f.get("contentLength"),
                "qualityLabel": f.get("qualityLabel"),
            }
        )

    adaptive_summary: List[dict] = []
    for f in streaming.get("adaptiveFormats", []) or []:
        if not isinstance(f, dict):
            continue
        adaptive_summary.append(
            {
                "itag": f.get("itag"),
                "mimeType": f.get("mimeType"),
                "bitrate": f.get("bitrate") or f.get("averageBitrate"),
                "width": f.get("width"),
                "height": f.get("height"),
                "contentLength": f.get("contentLength"),
                "qualityLabel": f.get("qualityLabel"),
            }
        )

    video_details = player_response.get("videoDetails", {}) or {}
    microformat = (
        player_response.get("microformat", {})
        .get("playerMicroformatRenderer", {})
        or {}
    )

    return {
        "streamingData": {
            "formats": formats_summary,
            "adaptiveFormats": adaptive_summary,
        },
        "videoDetails": {
            "videoId": video_details.get("videoId"),
            "title": video_details.get("title"),
            "lengthSeconds": video_details.get("lengthSeconds"),
            "channelId": video_details.get("channelId"),
        },
        "microformat": {
            "lengthSeconds": microformat.get("lengthSeconds"),
            "category": microformat.get("category"),
        },
    }


def extract_duration_seconds(player_response: dict) -> Optional[float]:
    """Extrae la duración del vídeo en segundos, si está disponible."""
    if not isinstance(player_response, dict):
        return None
    video_details = player_response.get("videoDetails") or {}
    length = video_details.get("lengthSeconds")
    if not length:
        micro = (
            player_response.get("microformat", {})
            .get("playerMicroformatRenderer", {})
            or {}
        )
        length = micro.get("lengthSeconds")
    if not length:
        return None
    try:
        return float(length)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Estimación de bytes de vídeo/audio
# ---------------------------------------------------------------------------


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _pick_progressive_format(formats: List[dict]) -> Optional[dict]:
    """Elige un formato progresivo representativo (idealmente itag 18)."""
    if not formats:
        return None
    chosen = None
    for f in formats:
        try:
            itag = int(f.get("itag"))
        except Exception:
            itag = None
        if itag == 18:  # 360p típico mezclando vídeo+audio
            chosen = f
            break
    if chosen is None:
        # Preferir el primero con contentLength; si no, el primero
        for f in formats:
            if f.get("contentLength"):
                chosen = f
                break
        if chosen is None:
            chosen = formats[0]
    return chosen


def _estimate_stream_bytes(stream: dict, duration: Optional[float]) -> int:
    """Devuelve una estimación de bytes para un stream (contentLength o bitrate*dur/8)."""
    if not isinstance(stream, dict):
        return 0
    cl = _safe_int(stream.get("contentLength"))
    if cl is not None and cl > 0:
        return cl
    bitrate = _safe_int(stream.get("bitrate") or stream.get("averageBitrate"))
    if bitrate and duration and duration > 0:
        # bitrate en bits/s, duración en s -> bytes
        return int(bitrate * duration / 8.0)
    return 0


def estimate_youtube_media_bytes(player_response: dict) -> Tuple[int, int, int]:
    """
    Estima los bytes de vídeo y audio a partir de streamingData y duración.

    Returns:
        (video_bytes, audio_bytes, total_media_bytes)
    """
    if not isinstance(player_response, dict):
        return 0, 0, 0

    duration = extract_duration_seconds(player_response)
    streaming = player_response.get("streamingData", {}) or {}

    # 1) PRIORIDAD: adaptiveFormats (vídeo y audio por separado, máxima calidad disponible)
    adaptive = streaming.get("adaptiveFormats") or []
    video_bytes = audio_bytes = total_media_bytes = 0

    if isinstance(adaptive, list) and adaptive:
        video_streams: List[dict] = []
        audio_streams: List[dict] = []
        for f in adaptive:
            if not isinstance(f, dict):
                continue
            mime = f.get("mimeType") or ""
            if isinstance(mime, str):
                if mime.startswith("video/"):
                    video_streams.append(f)
                elif mime.startswith("audio/"):
                    audio_streams.append(f)

        # Elegir vídeo con mayor altura (mejor calidad disponible)
        def pick_by_height(streams: List[dict]) -> Optional[dict]:
            best = None
            best_score = -1
            for s in streams:
                h = _safe_int(s.get("height"))
                score = h or 0
                if score > best_score:
                    best = s
                    best_score = score
            return best

        video = pick_by_height(video_streams)

        # Elegir audio preferente: itag 140 (audio/mp4) si existe, si no el primero
        audio = None
        for s in audio_streams:
            try:
                itag = int(s.get("itag"))
            except Exception:
                itag = None
            if itag == 140:
                audio = s
                break
        if audio is None and audio_streams:
            audio = audio_streams[0]

        video_bytes = _estimate_stream_bytes(video, duration) if video else 0
        audio_bytes = _estimate_stream_bytes(audio, duration) if audio else 0
        total_media_bytes = video_bytes + audio_bytes

        if total_media_bytes > 0:
            return int(video_bytes), int(audio_bytes), int(total_media_bytes)

    # 2) Fallback: formatos progresivos (vídeo+audio en un solo stream)
    formats = streaming.get("formats") or []
    if isinstance(formats, list) and formats:
        chosen = _pick_progressive_format([f for f in formats if isinstance(f, dict)])
        if chosen:
            total_bytes = _estimate_stream_bytes(chosen, duration)
            if total_bytes > 0:
                # En progresivo no distinguimos entre vídeo y audio
                return total_bytes, 0, total_bytes

    return 0, 0, 0


