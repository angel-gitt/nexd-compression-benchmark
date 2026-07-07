#!/usr/bin/env python3
"""
measure_ads_fair.py
-------------------
Comparación JUSTA HTML5 vs NEXD:
  - HTML5: assets locales (carpeta index.html)
  - NEXD:  assets pre-descargados y servidos localmente VÍA CACHING PROXY
           El JS de descompresión sigue corriendo → medimos el coste real de render

Flujo:
  1. Caching proxy en :8082 (intercepta media.adcanvas.com → cache local)
  2. Pre-warming: carga cada NEXD una vez para rellenar la caché
  3. Medición: HTML5 desde servidor estático, NEXD desde caché local
  4. Plot comparativo
"""

import os, sys, re, time, csv, json, threading, subprocess, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
import psutil

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from codecarbon import EmissionsTracker
import pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ── Paths ──────────────────────────────────────────────────────────────────
ADS_DIR   = Path("/tmp/folderHTML_in_batches")
HTML5_DIR = ADS_DIR / "HTML5_files_extracted"
NEXD_DIR  = ADS_DIR / "nexd_html"
CACHE_DIR = ADS_DIR / "nexd_cache"
OUT_DIR   = Path("/Users/angelmerino/Desktop/network_simulation_NEXTD/results")
OUT_CSV   = OUT_DIR / "energy_ads_fair.csv"
OUT_PLOT  = OUT_DIR / "plots_network" / "08_energy_ads_fair.png"

# ── Ports ──────────────────────────────────────────────────────────────────
STATIC_PORT = 8083    # sirve HTML5 y NEXD wrapper
PROXY_PORT  = 8082    # caching proxy para media.adcanvas.com
STATIC_URL  = f"http://127.0.0.1:{STATIC_PORT}"
PROXY_URL   = f"http://127.0.0.1:{PROXY_PORT}"

CDN_HOST    = "media.adcanvas.com"
CDN_BASE    = f"https://{CDN_HOST}"

SETTLE      = 4
N_ADS       = 8
COLORS      = {"HTML5": "#4C72B0", "NEXD": "#E07B39"}


# ── Caching proxy ──────────────────────────────────────────────────────────

class CachingProxyHandler(BaseHTTPRequestHandler):
    """
    Sirve assets de media.adcanvas.com desde caché local.
    Si no existe en caché, los descarga del CDN y los guarda.
    """

    def log_message(self, *a): pass

    def do_GET(self):
        path = self.path.lstrip("/")
        cache_file = CACHE_DIR / path.replace("?", "_").replace("&", "_")

        # Sirve desde caché si existe
        if cache_file.exists():
            data = cache_file.read_bytes()
            content_type = self._guess_ct(path)
            self._respond(200, content_type, data)
            return

        # Descarga del CDN
        real_url = f"{CDN_BASE}/{path}"
        try:
            req = urllib.request.Request(
                real_url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()

            # Guarda en caché
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(data)

            content_type = resp.headers.get("Content-Type", self._guess_ct(path))
            self._respond(200, content_type, data)

        except Exception as e:
            self._respond(503, "text/plain", f"Proxy error: {e}".encode())

    def _guess_ct(self, path):
        if path.endswith(".js"):    return "application/javascript"
        if path.endswith(".json"):  return "application/json"
        if path.endswith(".css"):   return "text/css"
        if path.endswith(".html"):  return "text/html"
        if path.endswith(".png"):   return "image/png"
        if path.endswith(".jpg") or path.endswith(".jpeg"): return "image/jpeg"
        if path.endswith(".mp4"):   return "video/mp4"
        if path.endswith(".woff2"): return "font/woff2"
        return "application/octet-stream"

    def _respond(self, code, ct, data):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(data))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


# ── Static server ──────────────────────────────────────────────────────────

class StaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass


def start_servers():
    """Inicia servidor estático y caching proxy en threads."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Servidor estático (HTML5 + wrappers NEXD)
    os.chdir(ADS_DIR)
    static_srv = HTTPServer(("127.0.0.1", STATIC_PORT), StaticHandler)
    threading.Thread(target=static_srv.serve_forever, daemon=True).start()

    # Caching proxy
    proxy_srv = HTTPServer(("127.0.0.1", PROXY_PORT), CachingProxyHandler)
    threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()

    time.sleep(0.5)
    print(f"  ✅ Static server: {STATIC_URL}")
    print(f"  ✅ Caching proxy: {PROXY_URL}  (→ {CDN_HOST})")


# ── NEXD wrapper local ─────────────────────────────────────────────────────

def build_nexd_wrapper(nexd_html: Path) -> Path:
    """
    Lee el HTML de NEXD, reemplaza media.adcanvas.com → proxy local.
    Guarda el wrapper en un directorio temporal.
    Returns path al wrapper.
    """
    content = nexd_html.read_text(encoding="utf-8")

    # Reemplaza CDN por proxy local
    local_content = content.replace(
        f"https://{CDN_HOST}",
        f"http://127.0.0.1:{PROXY_PORT}"
    ).replace(
        f"http://{CDN_HOST}",
        f"http://127.0.0.1:{PROXY_PORT}"
    )

    # Añade script de timing para medir decompresión
    timing_script = """
<script>
window.__nexd_timing = {start: performance.now()};
document.addEventListener('DOMContentLoaded', function() {
    window.__nexd_timing.dom_ready = performance.now();
});
window.addEventListener('load', function() {
    window.__nexd_timing.load_complete = performance.now();
});
</script>
"""
    local_content = f"<!DOCTYPE html><html><head>{timing_script}</head><body>{local_content}</body></html>"

    wrapper_dir = ADS_DIR / "nexd_local"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper_path = wrapper_dir / nexd_html.name
    wrapper_path.write_text(local_content, encoding="utf-8")
    return wrapper_path


# ── Pre-warming ────────────────────────────────────────────────────────────

def prewarm_nexd(nexd_files: list):
    """
    Pre-warming: descarga adtag.js de cada NEXD directamente con urllib.
    El resto de assets se cachea en el primer run de medición.
    """
    print(f"\n  🔥 Pre-warming: descargando adtag.js de {len(nexd_files)} ads...")
    for i, name in enumerate(nexd_files, 1):
        html = (NEXD_DIR / name).read_text(encoding="utf-8")
        # Extrae URL del script src
        match = re.search(r'src="(https://media\.adcanvas\.com/[^"]+)"', html)
        if not match:
            print(f"    [{i:2d}] {name[:50]:50s} ⚠️  sin src")
            continue
        script_url = match.group(1)
        # Descarga via proxy (que cachea en CACHE_DIR)
        local_url = script_url.replace(f"https://{CDN_HOST}", f"http://127.0.0.1:{PROXY_PORT}")
        try:
            with urllib.request.urlopen(local_url, timeout=15) as r:
                r.read()
            print(f"    [{i:2d}] {name[:50]:50s} ✅")
        except Exception as e:
            print(f"    [{i:2d}] {name[:50]:50s} ❌ {e}")
    print(f"  ✅ adtag.js cacheados")


# ── Medición ───────────────────────────────────────────────────────────────

def get_driver(use_proxy=False):
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--enable-gpu")
    opts.add_argument("--use-gl=swiftshader")       # SW rasterizer con pipeline completo
    opts.add_argument("--enable-webgl")
    opts.add_argument("--ignore-gpu-blocklist")      # fuerza GPU process aunque esté en blocklist
    if use_proxy:
        # Solo redirige CDN al proxy; localhost va directo
        opts.add_argument(f"--proxy-server=http://127.0.0.1:{PROXY_PORT}")
        opts.add_argument(f"--proxy-bypass-list=127.0.0.1,localhost")
        opts.add_argument("--ignore-certificate-errors")
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from selenium.webdriver.chrome.service import Service
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    except Exception:
        return webdriver.Chrome(options=opts)


def sample_gpu_utilization() -> float | None:
    """
    Lee GPU utilization (%) del acelerador Apple Silicon via ioreg.
    Devuelve None si no está disponible (Intel, error, etc.).
    """
    try:
        out = subprocess.check_output(
            ["ioreg", "-r", "-d", "1", "-n", "AGXAccelerator"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        m = re.search(r'"Device Utilization %"\s*=\s*(\d+)', out)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def chrome_gpu_proc_cpu(driver_pid: int) -> float:
    """
    Suma el CPU% de todos los procesos Chrome hijos con --type=gpu-process.
    """
    total = 0.0
    try:
        parent = psutil.Process(driver_pid)
        for proc in parent.children(recursive=True):
            try:
                cmd = " ".join(proc.cmdline())
                if "--type=gpu-process" in cmd:
                    total += proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return total


def measure_one(url: str, ad_name: str, ad_type: str) -> dict | None:
    label = f"{'HTML5' if ad_type=='HTML5' else 'NEXD':5s}  {ad_name[:52]:52s}"
    print(f"  {label}", end=" ", flush=True)

    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    use_proxy = (ad_type == "NEXD")
    driver = get_driver(use_proxy=use_proxy)
    driver.set_page_load_timeout(25)

    cpu_samples     = []
    gpu_sys_samples = []   # ioreg GPU utilization %
    gpu_proc_samples= []   # Chrome gpu-process CPU%
    stop_flag = threading.Event()

    # Precalienta cpu_percent para que la primera lectura sea válida
    psutil.cpu_percent(interval=None)

    # Tabla de procesos GPU ya vistos → inicializados para cpu_percent
    _gpu_procs_seen: set[int] = set()

    def sample_loop():
        while not stop_flag.is_set():
            cpu_samples.append(psutil.cpu_percent(interval=0.5))

            # Sistema GPU (Apple Silicon)
            g = sample_gpu_utilization()
            if g is not None:
                gpu_sys_samples.append(g)

            # Proceso GPU de Chrome: busca en cada tick para capturarlo
            # en cuanto Chrome lo lanza (suele ocurrir tras driver.get)
            gpu_cpu_total = 0.0
            try:
                parent = psutil.Process(driver.service.process.pid)
                for proc in parent.children(recursive=True):
                    try:
                        if "--type=gpu-process" not in " ".join(proc.cmdline()):
                            continue
                        pid = proc.pid
                        if pid not in _gpu_procs_seen:
                            proc.cpu_percent(interval=None)  # primer tick, descartado
                            _gpu_procs_seen.add(pid)
                        else:
                            gpu_cpu_total += proc.cpu_percent(interval=None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception:
                pass
            gpu_proc_samples.append(gpu_cpu_total)

    sampler = threading.Thread(target=sample_loop, daemon=True)

    try:
        tracker.start()
        sampler.start()
        t0 = time.time()

        driver.get(url)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(SETTLE)

        # Intenta leer timing de decompresión NEXD
        decomp_ms = None
        if ad_type == "NEXD":
            try:
                timing = driver.execute_script("return window.__nexd_timing || null")
                if timing and "load_complete" in timing:
                    decomp_ms = timing["load_complete"] - timing["start"]
            except Exception:
                pass

        duration_s = time.time() - t0
        stop_flag.set()
        emissions_kg = tracker.stop()

        energy_kwh    = emissions_kg * 1.87 if emissions_kg else 0.0
        energy_mj     = energy_kwh * 3.6e6
        cpu_avg       = float(np.mean(cpu_samples))      if cpu_samples      else 0.0
        cpu_peak      = float(np.max(cpu_samples))       if cpu_samples      else 0.0
        gpu_sys_avg   = float(np.mean(gpu_sys_samples))  if gpu_sys_samples  else None
        gpu_sys_peak  = float(np.max(gpu_sys_samples))   if gpu_sys_samples  else None
        gpu_proc_avg  = float(np.mean(gpu_proc_samples)) if gpu_proc_samples else 0.0
        gpu_proc_peak = float(np.max(gpu_proc_samples))  if gpu_proc_samples else 0.0

        gpu_tag = ""
        if gpu_sys_avg is not None:
            gpu_tag = f"  GPU_sys {gpu_sys_avg:.0f}%"
        if gpu_proc_avg:
            gpu_tag += f"  GPU_proc {gpu_proc_avg:.1f}%"

        print(f"✅  {energy_mj:7.3f} mJ  CPU {cpu_avg:4.1f}%{gpu_tag}"
              f"  {f'decomp {decomp_ms:.0f}ms' if decomp_ms else ''}")

        return {
            "ad_name":      ad_name,
            "ad_type":      ad_type,
            "energy_mj":    round(energy_mj, 4),
            "energy_kwh":   round(energy_kwh, 10),
            "cpu_avg_pct":  round(cpu_avg, 2),
            "cpu_peak_pct": round(cpu_peak, 2),
            "gpu_sys_avg_pct":  round(gpu_sys_avg, 1)  if gpu_sys_avg  is not None else None,
            "gpu_sys_peak_pct": round(gpu_sys_peak, 1) if gpu_sys_peak is not None else None,
            "gpu_proc_avg_pct": round(gpu_proc_avg, 2),
            "gpu_proc_peak_pct":round(gpu_proc_peak, 2),
            "duration_s":   round(duration_s, 2),
            "decomp_ms":    round(decomp_ms, 1) if decomp_ms else None,
            "timestamp":    datetime.now().isoformat(),
        }

    except Exception as e:
        stop_flag.set()
        tracker.stop()
        print(f"❌  {e}")
        return None
    finally:
        driver.quit()


# ── Plot ───────────────────────────────────────────────────────────────────

def make_plots(df: pd.DataFrame):
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.5))
    rng = np.random.default_rng(42)

    # Plot A: boxplot energía
    ax = axes[0]
    for i, at in enumerate(["HTML5", "NEXD"], 1):
        vals = df[df.ad_type == at]["energy_mj"].dropna().values
        bp = ax.boxplot([vals], positions=[i], patch_artist=True,
                        medianprops={"color": "black", "linewidth": 2},
                        widths=0.45)
        bp["boxes"][0].set_facecolor(COLORS[at])
        bp["boxes"][0].set_alpha(0.7)
        jitter = rng.uniform(-0.15, 0.15, len(vals))
        ax.scatter(i + jitter, vals, color=COLORS[at], alpha=0.75, s=30,
                   edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks([1, 2]); ax.set_xticklabels(["HTML5", "NEXD"])
    ax.set_ylabel("Energía (mJ)"); ax.set_title("Energía por anuncio\n(condiciones locales iguales)")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # Plot B: scatter energía vs CPU
    ax = axes[1]
    for at in ["HTML5", "NEXD"]:
        g = df[df.ad_type == at]
        ax.scatter(g["cpu_avg_pct"], g["energy_mj"], color=COLORS[at],
                   label=at, alpha=0.75, s=50, edgecolors="white", linewidths=0.3)
        if len(g) >= 2:
            b, a = np.polyfit(g["cpu_avg_pct"], g["energy_mj"], 1)
            xs = np.linspace(g["cpu_avg_pct"].min(), g["cpu_avg_pct"].max(), 30)
            ax.plot(xs, a + b * xs, color=COLORS[at], linestyle="--", linewidth=1.5)
    ax.set_xlabel("CPU promedio (%)"); ax.set_ylabel("Energía (mJ)")
    ax.set_title("Energía vs carga CPU")
    ax.legend(); ax.grid(linestyle="--", alpha=0.4)

    # Plot C: barra duración + nota decompresión
    ax = axes[2]
    for i, at in enumerate(["HTML5", "NEXD"], 1):
        g = df[df.ad_type == at]
        mean_dur = g["duration_s"].mean()
        sem_dur  = g["duration_s"].sem()
        ax.bar(i, mean_dur, color=COLORS[at], alpha=0.75, width=0.5,
               yerr=sem_dur, capsize=5)
        # Anotar decompresión media si hay datos
        if at == "NEXD":
            decomp_vals = g["decomp_ms"].dropna()
            if len(decomp_vals):
                ax.text(i, mean_dur + sem_dur + 0.1,
                        f"decomp\n{decomp_vals.mean():.0f}ms",
                        ha="center", fontsize=8, color=COLORS[at])
    ax.set_xticks([1, 2]); ax.set_xticklabels(["HTML5", "NEXD"])
    ax.set_ylabel("Duración (s)"); ax.set_title("Duración de carga")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # Título general
    h5  = df[df.ad_type == "HTML5"]["energy_mj"].mean()
    nx  = df[df.ad_type == "NEXD"]["energy_mj"].mean()
    rat = nx / h5 if h5 > 0 else float("nan")
    fig.suptitle(
        f"Comparación JUSTA (assets locales) — HTML5 {h5:.2f} mJ  vs  NEXD {nx:.2f} mJ  |  ratio {rat:.2f}×\n"
        f"NEXD: assets pre-cacheados, descompresión JS activa",
        fontsize=10
    )
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*80)
    print("⚡ Comparación JUSTA: HTML5 vs NEXD (todo local + descompresión NEXD)")
    print("="*80)

    # Inicia servidores
    print("\n📡 Iniciando servidores...")
    start_servers()

    html5_ads  = sorted([d.name for d in HTML5_DIR.iterdir()
                         if d.is_dir() and (d / "index.html").exists()])
    nexd_files = sorted([f.name for f in NEXD_DIR.iterdir() if f.suffix == ".html"])

    print(f"\n  HTML5: {len(html5_ads)} | NEXD: {len(nexd_files)}")

    # Build NEXD wrappers (redirigen CDN → proxy local)
    print("\n🔧 Construyendo wrappers NEXD (CDN → proxy local)...")
    for name in nexd_files:
        build_nexd_wrapper(NEXD_DIR / name)
    print(f"  ✅ {len(nexd_files)} wrappers en nexd_local/")

    # Pre-warming: descarga adtag.js con urllib (rápido, sin Chrome)
    print("\n🔥 Pre-warming NEXD cache...")
    prewarm_nexd(nexd_files[:N_ADS])

    FIELDNAMES = ["ad_name", "ad_type", "energy_mj", "energy_kwh",
                  "cpu_avg_pct", "cpu_peak_pct",
                  "gpu_sys_avg_pct", "gpu_sys_peak_pct",
                  "gpu_proc_avg_pct", "gpu_proc_peak_pct",
                  "duration_s", "decomp_ms", "timestamp"]

    # Abre CSV incremental: si ya existe lo continúa (append), si no lo crea con header
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_exists = OUT_CSV.exists()
    csv_file = open(OUT_CSV, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
    if not csv_exists:
        writer.writeheader()
        csv_file.flush()

    results = []

    def record(r):
        """Guarda fila en CSV inmediatamente y acumula en lista."""
        if r is None:
            return
        writer.writerow(r)
        csv_file.flush()
        results.append(r)

    # Mide intercalando: HTML5 → NEXD → HTML5 → NEXD …
    # Así las condiciones del sistema son comparables par a par
    print(f"\n{'─'*80}\n  🔀 Medición intercalada HTML5 / NEXD\n{'─'*80}")
    h5_slice   = html5_ads[:N_ADS]
    nexd_slice = nexd_files[:N_ADS]
    n_pairs = max(len(h5_slice), len(nexd_slice))

    for i in range(n_pairs):
        if i < len(h5_slice):
            name = h5_slice[i]
            print(f"\n  Par {i+1}/{n_pairs}  🔷 HTML5")
            record(measure_one(
                f"http://127.0.0.1:{STATIC_PORT}/HTML5_files_extracted/{name}/index.html",
                name, "HTML5"
            ))
        if i < len(nexd_slice):
            name = nexd_slice[i]
            print(f"  Par {i+1}/{n_pairs}  🟠 NEXD")
            record(measure_one(
                f"http://127.0.0.1:{STATIC_PORT}/nexd_local/{name}",
                name, "NEXD"
            ))
        if i < n_pairs - 1:
            time.sleep(2)  # pequeña pausa entre pares

    csv_file.close()

    if not results:
        print("❌ Sin resultados"); return

    print(f"\n  ✅ CSV: {OUT_CSV}")

    print("\n" + "─"*80)
    print(df.groupby("ad_type")[["energy_mj", "cpu_avg_pct", "duration_s"]]
            .agg({"energy_mj": ["mean", "std"],
                  "cpu_avg_pct": "mean",
                  "duration_s": "mean"})
            .round(3))

    make_plots(df)
    print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    main()
