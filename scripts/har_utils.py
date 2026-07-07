#!/usr/bin/env python3
"""
Utilidades genéricas para trabajar con HARs en el crawler de creatividades.

Este módulo contiene:
  - Detección de tipos multimedia a partir de mimeType.
  - Filtrado de recursos de navegador que no forman parte de la página HTML.
  - Cálculo de payload_media y payload_total a partir de un HAR estándar.
  - Enriquecimiento del HAR con metadatos (width, height, payloads).

Para lógica específica de plataformas (por ejemplo, YouTube), se delega en
`scripts.youtube_utils` cuando se pasa `platform=\"youtube\"` y un
`player_response` válido.
"""

from __future__ import annotations

import base64
import gzip

from typing import List, Optional, Tuple

# Fallback data for specific platforms (size and payloads) due to login walls/consent screens
PLATFORM_FALLBACKS = {
    "meta": {"mobile": {"w": 325, "h": 406, "tot": 1400000, "med": 45000}, "desktop": {"w": 500, "h": 500, "tot": 315000, "med": 11000}},
    "facebook": {"mobile": {"w": 325, "h": 406, "tot": 1400000, "med": 45000}, "desktop": {"w": 500, "h": 500, "tot": 315000, "med": 11000}},
    "instagram": {"mobile": {"w": 325, "h": 406, "tot": 1400000, "med": 45000}, "desktop": {"w": 500, "h": 500, "tot": 315000, "med": 11000}},
    "youtube": {"mobile": {"w": 330, "h": 185, "tot": 1900000, "med": 430000}, "desktop": {"w": 1381, "h": 777, "tot": 6800000, "med": 400000}},
}


def is_media_mimetype(mime_type: str) -> bool:
    """
    Determina si un mimeType es de contenido multimedia.

    Args:
        mime_type: El mimeType a verificar (ej: "image/png", "video/mp4")

    Returns:
        True si es multimedia, False en caso contrario
    """
    if not mime_type:
        return False

    mime_lower = mime_type.lower().strip()

    # Tipos multimedia principales
    media_prefixes = [
        "image/",
        "video/",
        "audio/",
    ]

    # Tipos específicos adicionales
    media_types = [
        "gif",
        "svg",
        "png",
        "jpeg",
        "jpg",
        "webp",
        "bmp",
        "ico",
        "tiff",
        "mp3",
        "mp4",
        "webm",
        "ogg",
        "wav",
        "flac",
        "aac",
        "m4a",
        "avi",
        "mov",
        "wmv",
        "flv",
    ]

    for prefix in media_prefixes:
        if mime_lower.startswith(prefix):
            return True

    for media_type in media_types:
        if media_type in mime_lower:
            return True

    return False


def is_page_related_resource(url: str) -> bool:
    """
    Determina si un recurso se cargaría al abrir la página HTML directamente en el navegador.
    Excluye recursos del navegador que se cargan en segundo plano durante la sesión del crawler
    pero que NO aparecerían en DevTools al abrir la página directamente.

    Args:
        url: La URL del recurso

    Returns:
        True si el recurso se cargaría al abrir la página, False si es un recurso del navegador en segundo plano
    """
    if not url:
        return False

    # Patrones de URLs que son recursos del navegador en segundo plano
    # Estos NO se cargarían al abrir example.html directamente
    browser_background_patterns = [
        "clients2.google.com/time",  # Sincronización de tiempo del navegador
        "accounts.google.com/ListAccounts",  # Cuentas de Google del navegador
        "www.google.com/async/newtab",  # Recursos de la página de inicio/newtab
        "www.google.com/async/folae",  # Recursos de newtab
        "www.google.com/async/newtab_promos",  # Promociones de newtab
        "www.gstatic.com/og/_/",  # Recursos de newtab (OneGoogle)
        "apis.google.com/_/scs/abc-static",  # APIs de newtab
        "ogads-pa.clients6.google.com",  # Servicios internos de Google
        "play.google.com/log",  # Servicios de Google Play
        "android.clients.google.com",  # Servicios de Android/Chrome
        "optimizationguide-pa.googleapis.com",  # Optimización del navegador
    ]

    for pattern in browser_background_patterns:
        if pattern in url:
            return False

    # Todos los demás recursos se incluyen (recursos del anuncio, iframes, scripts, etc.)
    return True


def calculate_payloads(
    har: dict,
    treat_acz_as_media: bool = False,
) -> Tuple[int, int]:
    """
    Calcula payload_media y payload_total a partir de las entries del HAR.
    Solo incluye recursos que se cargarían al abrir la página HTML directamente,
    excluyendo recursos del navegador que se cargan en segundo plano durante la sesión del crawler
    pero que NO aparecerían en DevTools al abrir la página directamente.

    Args:
        har: El diccionario HAR completo.
        treat_acz_as_media: Si True, los archivos .acz (paquetes multimedia
            comprimidos de NEXD) se contabilizan como payload_media aunque
            su mimeType sea application/zip.

    Returns:
        Tuple (payload_media, payload_total)
    """
    payload_media = 0
    payload_total = 0

    log = har.get("log", {})
    entries = log.get("entries", [])

    for entry in entries:
        request = entry.get("request", {})
        url = request.get("url", "")

        if not is_page_related_resource(url):
            continue

        response = entry.get("response", {})
        transfer_size = response.get("transferSize")
        if transfer_size is None:
            transfer_size = response.get("_transferSize")
        if transfer_size is not None:
            transfer_size = int(transfer_size)

        decoded_body_size = response.get("decodedBodySize")
        if decoded_body_size is None:
            decoded_body_size = response.get("_decodedBodySize")
        if decoded_body_size is not None:
            decoded_body_size = int(decoded_body_size)

        body_size = response.get("bodySize", 0)

        # Si transfer_size es None o 0 (cache), usar decoded_body_size o body_size
        if not transfer_size:
            if decoded_body_size:
                transfer_size = decoded_body_size
            else:
                transfer_size = body_size

        payload_total += transfer_size

        content = response.get("content", {})
        mime_type = content.get("mimeType", "")

        is_media = is_media_mimetype(mime_type)

        # .acz = NEXD multimedia bundle (application/zip); treat as media when requested
        if treat_acz_as_media and not is_media:
            url_lower = url.lower().split("?")[0]
            if url_lower.endswith(".acz"):
                is_media = True

        if is_media:
            payload_media += transfer_size

    return payload_media, payload_total


def extract_sourcemap_embedded_bytes(har: dict) -> Tuple[int, int]:
    """
    Extrae los bytes de recursos multimedia embebidos como data URIs en
    ``log._domSnapshot.sourceMap`` y los comprime con gzip para obtener el
    transfer_size real post-compresión.

    Los creativos HTML5 (p.ej. Nexd experiment desktop html5) empaquetan sus
    imágenes directamente como ``data:image/...;base64,...`` en el HTML, por
    lo que el HAR de red casi no registra tráfico multimedia. Esta función
    recupera esos bytes decodificando el base64 y luego los comprime con gzip
    para obtener el transfer_size real en vez de usar ratios estimados.

    Args:
        har: El diccionario HAR completo (con ``log._domSnapshot.sourceMap``).

    Returns:
        Tuple (embedded_media_bytes, embedded_total_bytes) como transfer sizes
        post-compresión gzip.
    """
    log = har.get("log", {})
    snap = log.get("_domSnapshot", {})
    if not isinstance(snap, dict):
        return 0, 0

    source_map = snap.get("sourceMap", {})
    if not source_map:
        return 0, 0

    embedded_media = 0
    embedded_total = 0

    for key in source_map:
        if not isinstance(key, str) or not key.startswith("data:"):
            continue
        try:
            header, sep, b64data = key.partition(";base64,")
            if not sep or not b64data:
                continue
            mime = header[5:]
            decoded_bytes = base64.b64decode(b64data)
            if not decoded_bytes:
                continue
            compressed = gzip.compress(decoded_bytes, mtime=0)
            transfer_size = len(compressed)
            embedded_total += transfer_size
            if is_media_mimetype(mime):
                embedded_media += transfer_size
        except Exception:
            continue

    return embedded_media, embedded_total


def enrich_har_with_metadata(
    har: dict,
    ad_width: Optional[int],
    ad_height: Optional[int],
    platform: Optional[str] = None,
    player_response: Optional[dict] = None,
    device_type: str = "desktop",
    advertiser: Optional[str] = None,
    ad_html: Optional[str] = None,
) -> dict:
    """
    Enriquece el HAR con width, height, payload_media y payload_total.

    Para plataformas genéricas, aplica `calculate_payloads`.
    Para YouTube, cuando se proporciona `player_response`, intenta estimar los
    bytes de vídeo a partir de ytInitialPlayerResponse y combina esa
    información con el HAR.
    Para Nexd (display):
    - ``advertiser`` que contiene ``"html5"``: suma los bytes de los recursos
      multimedia embebidos como data URIs en ``_domSnapshot.sourceMap``,
      aplicando un ratio de compresión estimado (brotli/gzip).
    - ``advertiser`` que contiene ``"nexd"`` pero NO ``"html5"``: contabiliza
      los archivos ``.acz`` (paquetes multimedia comprimidos de NEXD) como
      ``payload_media``.
    """
    # Extraer tamaño del creativo de forma secundaria si viene como None o 0
    if not ad_width or not ad_height:
        import re
        pages = har.get("log", {}).get("pages", [])
        creative_id = pages[0].get("title", "") if pages else ""
        
        # 1. Buscar en el creative_id (ej: AirAsia-RayaSale-728x90-copyhtml)
        if creative_id:
            m_folder = re.search(r"(\d+)x(\d+)", creative_id)
            if m_folder:
                ad_width = int(m_folder.group(1))
                ad_height = int(m_folder.group(2))
                
        # 2. Buscar en el ad_html (meta ad.size, data-attributes, CSS vars)
        if (not ad_width or not ad_height) and ad_html:
            # Meta ad.size
            m_meta = re.search(r"width\s*=\s*(\d+)\s*,\s*height\s*=\s*(\d+)", ad_html, re.I)
            if m_meta:
                ad_width = int(m_meta.group(1))
                ad_height = int(m_meta.group(2))
            else:
                # data-width y data-height
                m_dw = re.search(r"data-width\s*=\s*[\"\'](\d+)[\"\']", ad_html, re.I)
                m_dh = re.search(r"data-height\s*=\s*[\"\'](\d+)[\"\']", ad_html, re.I)
                if m_dw and m_dh:
                    ad_width = int(m_dw.group(1))
                    ad_height = int(m_dh.group(1))
                else:
                    # CSS root variables
                    m_css_w = re.search(r"--width\s*:\s*(\d+)px", ad_html, re.I)
                    m_css_h = re.search(r"--height\s*:\s*(\d+)px", ad_html, re.I)
                    if m_css_w and m_css_h:
                        ad_width = int(m_css_w.group(1))
                        ad_height = int(m_css_h.group(1))

    # Valores por defecto para display si la detección falló por completo
    # Esto previene errores de NoneType aguas abajo.
    if not ad_width:
        ad_width = 300
    if not ad_height:
        ad_height = 250

    adv = str(advertiser or "").lower()
    # .acz = NEXD multimedia bundle; treat as media for non-html5 nexd advertisers
    treat_acz = "nexd" in adv and "html5" not in adv
    # Embedded data URIs in sourceMap for html5 advertisers
    use_sourcemap = "html5" in adv

    # Valores base calculados solo desde el HAR
    base_media, base_total = calculate_payloads(har, treat_acz_as_media=treat_acz)

    payload_media = base_media
    payload_total = base_total

    # Fallbacks for specific platforms
    plat = (platform or "").lower()
    fallback = PLATFORM_FALLBACKS.get(plat, {}).get(device_type.lower())

    if fallback:
        # Size fallback
        if not ad_width or not ad_height:
            ad_width = fallback["w"]
            ad_height = fallback["h"]
            har.setdefault("log", {}).setdefault("_adSize", {}).update({
                "width": ad_width, "height": ad_height, "deviceType": device_type, "source": "fallback"
            })
        
        # Payload fallback
        if payload_media <= 0 or payload_total < 50000:
            payload_total = fallback["tot"]
            payload_media = fallback["med"]

    # Lógica específica para HTML5 embebidos en sourceMap
    if use_sourcemap:
        emb_media, emb_total = extract_sourcemap_embedded_bytes(har)
        if emb_total > 0:
            payload_media += emb_media
            payload_total += emb_total
            har["embedded_media_bytes"] = emb_media
            har["embedded_total_bytes"] = emb_total

    # Lógica específica para YouTube (si tenemos player_response)
    if platform and platform.lower() == "youtube" and player_response:
        try:
            # Importación perezosa para evitar dependencias fuertes en tiempo de import
            from scripts import youtube_utils  # type: ignore

            video_bytes, audio_bytes, total_media_bytes = youtube_utils.estimate_youtube_media_bytes(
                player_response
            )
        except Exception:
            video_bytes = audio_bytes = total_media_bytes = 0

        if total_media_bytes > 0:
            # No duplicar el vídeo ya contado en el HAR: mantenemos el overhead
            # no multimedia estimado a partir del HAR original.
            non_media_bytes = max(base_total - base_media, 0)
            payload_media = total_media_bytes
            payload_total = payload_media + non_media_bytes

            # Guardar métricas adicionales útiles
            har["video_media_bytes"] = int(video_bytes)
            har["video_audio_bytes"] = int(audio_bytes)

            duration = youtube_utils.extract_duration_seconds(player_response)
            if duration is not None:
                har["video_duration_seconds"] = duration

    # Agregar metadatos al nivel raíz
    har["width"] = ad_width
    har["height"] = ad_height


    har["payload_media"] = int(payload_media)
    har["payload_total"] = int(payload_total)

    return har


