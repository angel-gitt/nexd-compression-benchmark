# Comparative Energy Analysis: HTML5 vs NEXD Ad Formats

## 1. Study Overview

- **Sample**: 65 NEXD, 53 HTML5, 57 alt. HTML5 = **175 ads**
- **Crawler**: CDP-native capture (no selenium-wire) con Brotli decode + file:// asset capture
- **Network energy**: NS-3.48 under Wi-Fi (802.11ac) and LTE / 4G
- **Compression**: HTML5 local file:// assets gzip-compressed during NS‑3 simulation (text only, binary kept at original size); NEXD and alt HTML5 use real wire bytes from CDP capture

---

## 2. Core Results

### 2.1 Payload Sizes (median, MB)

| Format | Wire (MB) | Useful (MB) | Wire/Useful |
|:---|---:|---:|---:|
| **NEXD** | 0.4222 | 0.7259 | 58.2% |
| HTML5 (raw, S3) | 2.2565 | 2.4458 | 92.3% |
| alt. HTML5 (CDN) | 0.4448 | 1.1352 | 39.2% |

### 2.2 Network Energy (NS-3.48)

#### Wi-Fi (802.11ac)

| Format | Total Energy (kWh) | Energy/MB useful (kWh/MB) |
|:---|---:|---:|
| **NEXD** | **1.1931e-08** | 1.7062e-08 |
| HTML5 (raw) | 6.5123e-08 | 2.3740e-08 |
| alt. HTML5 (CDN) | 1.2248e-08 | 1.5574e-08 |

#### LTE / 4G

| Format | Total Energy (kWh) | Energy/MB useful (kWh/MB) |
|:---|---:|---:|
| **NEXD** | **3.9934e-06** | 5.7110e-06 |
| HTML5 (raw) | 2.1928e-05 | 7.9802e-06 |
| alt. HTML5 (CDN) | 4.0231e-06 | 5.1360e-06 |

### 2.3 NEXD Savings

| Metric | vs HTML5 (raw) | vs alt. HTML5 (CDN) |
|:---|---:|---:|
| Total Energy (Wi-Fi) | **81.7%** | 2.6% |
| Total Energy (LTE) | **81.8%** | 0.7% |
| Efficiency (Wi-Fi) | **28.1%** | -9.6% |
| Efficiency (LTE) | **28.4%** | -11.2% |

---

## 3. NEXD Payload Forensic

### 3.1 Wire Composition

| Component | Wire (MB) | % of Wire | Compressed |
|:---|---:|---:|:---|
| pack.acz (ZIP) | 18.4 | 42.1% | No (ZIP) |
| adcanvas.min.js | 4.0 | 9.1% | Brotli |
| JS (tracking) | 4.6 | 10.6% | Brotli |
| Images | 3.6 | 8.2% | No |

### 3.2 Decompression

Both formats decompress on device: NEXD via JS ZIP (~309 ms, higher CPU), HTML5 via native Brotli (transparent, faster, lower CPU). Device measurements show NEXD CPU ~21% vs HTML5 ~17%.

---

## 4. Key Conclusions

1. **NEXD saves 82% total energy vs raw HTML5** — the .acz is genuinely lighter.

2. **Vs CDN-served HTML5, savings drop to 2%** — Brotli/CDN compression narrows the gap.

3. **NEXD per-MB efficiency is 9.6% WORSE than CDN HTML5** — the .acz decompression overhead and runtime JS cost offset the wire savings.

4. **'AI optimization' is marketing** — the .acz is a standard ZIP; no AI observable.

5. **NEXD's real advantage is architectural simplicity** — not per-byte efficiency, but lighter source assets.
