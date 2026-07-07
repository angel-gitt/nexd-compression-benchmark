#!/usr/bin/env python3
"""
measure_ads_energy.py
Mide energía CPU durante la carga de anuncios HTML5 y NEXD.
"""
import os, sys, time, csv, threading
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
import psutil

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from codecarbon import EmissionsTracker
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ADS_DIR   = Path("/tmp/folderHTML_in_batches")
HTML5_DIR = ADS_DIR / "HTML5_files_extracted"
NEXD_DIR  = ADS_DIR / "nexd_html"
OUT_DIR   = Path("/Users/angelmerino/Desktop/network_simulation_NEXTD/results")
OUT_CSV   = OUT_DIR / "energy_ads_codecarbon.csv"
OUT_PLOT  = OUT_DIR / "plots_network" / "08_energy_ads_html5_vs_nexd.png"

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8081
HTTP_URL  = f"http://{HTTP_HOST}:{HTTP_PORT}"
SETTLE    = 4   # s settle tras carga
N_ADS     = 8   # anuncios por tipo

COLORS = {"HTML5": "#4C72B0", "NEXD": "#E07B39"}


# ── HTTP server ───────────────────────────────────────────────────────────────

class _Silent(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def start_server():
    os.chdir(ADS_DIR)
    srv = HTTPServer((HTTP_HOST, HTTP_PORT), _Silent)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    print(f"  ✅ HTTP en {HTTP_URL}")


# ── Chrome ────────────────────────────────────────────────────────────────────

def make_driver():
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,800")
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from selenium.webdriver.chrome.service import Service
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    except Exception:
        return webdriver.Chrome(options=opts)


# ── Medición ──────────────────────────────────────────────────────────────────

def measure_one(url: str, ad_name: str, ad_type: str) -> dict | None:
    print(f"  {'HTML5' if ad_type=='HTML5' else 'NEXD ':4s}  {ad_name[:55]:55s}", end=" ", flush=True)

    tracker = EmissionsTracker(log_level="error", save_to_file=False,
                               force_carbon_intensity_g_co2e_kwh=233.0)
    driver = make_driver()
    driver.set_page_load_timeout(25)

    try:
        cpu_samples = []
        stop_flag = threading.Event()

        def sample_cpu():
            while not stop_flag.is_set():
                cpu_samples.append(psutil.cpu_percent(interval=0.5))

        sampler = threading.Thread(target=sample_cpu, daemon=True)

        tracker.start()
        sampler.start()
        t0 = time.time()

        driver.get(url)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(SETTLE)

        duration_s = time.time() - t0
        stop_flag.set()
        emissions_kg = tracker.stop()

        energy_kwh  = emissions_kg * 1.87 if emissions_kg else 0.0
        energy_mj   = energy_kwh * 3.6e6
        cpu_mean    = float(np.mean(cpu_samples)) if cpu_samples else 0.0
        cpu_peak    = float(np.max(cpu_samples))  if cpu_samples else 0.0

        print(f"✅  {energy_mj:8.3f} mJ  CPU avg {cpu_mean:4.1f}%  dur {duration_s:.1f}s")

        return {
            "ad_name":    ad_name,
            "ad_type":    ad_type,
            "energy_mj":  energy_mj,
            "energy_kwh": energy_kwh,
            "cpu_avg_pct":cpu_mean,
            "cpu_peak_pct":cpu_peak,
            "duration_s": duration_s,
            "timestamp":  datetime.now().isoformat(),
        }

    except Exception as e:
        tracker.stop()
        print(f"❌  {e}")
        return None
    finally:
        driver.quit()


# ── Plot ──────────────────────────────────────────────────────────────────────

def make_plots(df: pd.DataFrame):
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    FONT = 10

    groups = df.groupby("ad_type")

    # Plot A: boxplot energía
    ax = axes[0]
    data = [df[df.ad_type == t]["energy_mj"].dropna().values for t in ["HTML5", "NEXD"]]
    bp = ax.boxplot(data, positions=[1,2], patch_artist=True,
                    medianprops={"color":"black","linewidth":2},
                    widths=0.5)
    for patch, col in zip(bp["boxes"], [COLORS["HTML5"], COLORS["NEXD"]]):
        patch.set_facecolor(col); patch.set_alpha(0.7)
    rng = np.random.default_rng(42)
    for pos, vals, col in [(1, data[0], COLORS["HTML5"]), (2, data[1], COLORS["NEXD"])]:
        if len(vals):
            jitter = rng.uniform(-0.15, 0.15, len(vals))
            ax.scatter(pos + jitter, vals, color=col, alpha=0.7, s=25,
                       edgecolors="white", linewidths=0.3, zorder=3)
    ax.set_xticks([1,2]); ax.set_xticklabels(["HTML5","NEXD"])
    ax.set_ylabel("Energía (mJ)"); ax.set_title("Energía por anuncio")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # Plot B: scatter energía vs duración
    ax = axes[1]
    for at in ["HTML5", "NEXD"]:
        g = df[df.ad_type == at]
        ax.scatter(g["duration_s"], g["energy_mj"], color=COLORS[at],
                   label=at, alpha=0.75, s=50, edgecolors="white", linewidths=0.3)
        if len(g) >= 2:
            b, a = np.polyfit(g["duration_s"], g["energy_mj"], 1)
            xs = np.linspace(g["duration_s"].min(), g["duration_s"].max(), 30)
            ax.plot(xs, a + b*xs, color=COLORS[at], linestyle="--", linewidth=1.5)
    ax.set_xlabel("Duración de carga (s)"); ax.set_ylabel("Energía (mJ)")
    ax.set_title("Energía vs duración")
    ax.legend(); ax.grid(linestyle="--", alpha=0.4)

    # Plot C: bar CPU avg %
    ax = axes[2]
    means = [df[df.ad_type == t]["cpu_avg_pct"].mean() for t in ["HTML5","NEXD"]]
    errs  = [df[df.ad_type == t]["cpu_avg_pct"].sem() for t in ["HTML5","NEXD"]]
    bars  = ax.bar([1,2], means, color=[COLORS["HTML5"], COLORS["NEXD"]],
                   alpha=0.75, width=0.5, yerr=errs, capsize=5)
    ax.set_xticks([1,2]); ax.set_xticklabels(["HTML5","NEXD"])
    ax.set_ylabel("CPU promedio (%)"); ax.set_title("Uso de CPU durante carga")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    # Medias en el título
    h5 = df[df.ad_type=="HTML5"]["energy_mj"].mean()
    nx = df[df.ad_type=="NEXD"]["energy_mj"].mean()
    ratio = nx / h5 if h5 > 0 else float("nan")
    fig.suptitle(
        f"Energía de carga: HTML5 {h5:.2f} mJ  vs  NEXD {nx:.2f} mJ  —  ratio NEXD/HTML5 = {ratio:.2f}×",
        fontsize=FONT+1
    )
    fig.tight_layout()
    OUT_PLOT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PLOT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  ✅ Plot: {OUT_PLOT}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*80)
    print("⚡ Medición de energía: HTML5 vs NEXD (CodeCarbon + psutil)")
    print("="*80)

    start_server()

    html5_ads = sorted([d.name for d in HTML5_DIR.iterdir()
                        if d.is_dir() and (d / "index.html").exists()])
    nexd_files = sorted([f.name for f in NEXD_DIR.iterdir() if f.suffix == ".html"])

    print(f"\n  HTML5: {len(html5_ads)} | NEXD: {len(nexd_files)}")

    results = []

    print(f"\n{'─'*80}\n  🔷 HTML5\n{'─'*80}")
    for name in html5_ads[:N_ADS]:
        r = measure_one(f"{HTTP_URL}/HTML5_files_extracted/{name}/index.html", name, "HTML5")
        if r: results.append(r)

    print(f"\n  ⏸  pausa 3s...")
    time.sleep(3)

    print(f"\n{'─'*80}\n  🟠 NEXD\n{'─'*80}")
    for name in nexd_files[:N_ADS]:
        r = measure_one(f"{HTTP_URL}/nexd_html/{name}", name, "NEXD")
        if r: results.append(r)

    if not results:
        print("❌ Sin resultados"); return

    df = pd.DataFrame(results)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"\n  ✅ CSV: {OUT_CSV}")

    print("\n" + "─"*80)
    print(df.groupby("ad_type")[["energy_mj","cpu_avg_pct","duration_s"]]
            .agg({"energy_mj":["mean","std"],"cpu_avg_pct":"mean","duration_s":"mean"})
            .round(3))

    make_plots(df)
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
