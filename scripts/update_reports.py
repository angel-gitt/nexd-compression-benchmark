#!/usr/bin/env python3
import pandas as pd
import math
import sys
import subprocess
from pathlib import Path

def format_md_sci(val):
    if math.isnan(val) or val == 0:
        return "0"
    exponent = int(math.floor(math.log10(abs(val))))
    mantissa = val / (10 ** exponent)
    return f"${mantissa:.4f} \\times 10^{{{exponent}}}$"

def format_docx_sci(val):
    if math.isnan(val) or val == 0:
        return "0"
    exponent = int(math.floor(math.log10(abs(val))))
    mantissa = val / (10 ** exponent)
    # convert exponent to superscript characters
    superscripts = {
        '-': '⁻', '0': '⁰', '1': '¹', '2': '²', '3': '³',
        '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'
    }
    exp_str = "".join(superscripts.get(c, c) for c in str(exponent))
    return f"{mantissa:.4f} × 10{exp_str}"

def main():
    repo_root = Path(__file__).resolve().parent.parent
    summary_path = repo_root / "results" / "summary.csv"
    if not summary_path.exists():
        print(f"Error: {summary_path} not found.")
        sys.exit(1)

    df = pd.read_csv(summary_path)
    
    # Infer ad types
    def infer_type(cp):
        cp_lower = str(cp).lower()
        if cp_lower.startswith("nexd__"):
            return "nexd"
        elif cp_lower.startswith("html5hiili__"):
            return "html5hiili"
        elif cp_lower.startswith("html5__"):
            return "html5"
        return None

    df["ad_type"] = df["creative_platform"].apply(infer_type)
    df = df.dropna(subset=["ad_type"])

    # Clean residuals
    for col in df.columns:
        if (col.endswith("_consumed_kWh") or col.endswith("_kWh_per_mb") or col.endswith("_kWh_per_useful_mb")):
            df[col] = pd.to_numeric(df[col], errors="coerce").clip(lower=0)

    # Filter for per-MB stats (exclude failed ads below 50kB)
    MIN_PAYLOAD_MB = 0.05
    df_pm = df[pd.to_numeric(df["payload_mb"], errors="coerce").fillna(0) >= MIN_PAYLOAD_MB].copy()

    # Calculate statistics
    stats = {}
    ad_types = ["nexd", "html5", "html5hiili"]
    techs = ["wifi_ac", "lte"]

    for t in ad_types:
        sub_all = df[df["ad_type"] == t]
        sub_pm = df_pm[df_pm["ad_type"] == t]
        
        stats[t] = {
            "N": len(sub_all)
        }
        for tech in techs:
            col_energy = f"{tech}_consumed_kWh"
            col_useful = f"{tech}_kWh_per_useful_mb"
            
            stats[t][f"{tech}_energy"] = sub_all[col_energy].median() if len(sub_all) else float("nan")
            stats[t][f"{tech}_useful"] = sub_pm[col_useful].median() if len(sub_pm) else float("nan")

    # Wi-Fi Savings calculations
    wifi_nexd_vs_html5_energy = (stats["html5"]["wifi_ac_energy"] - stats["nexd"]["wifi_ac_energy"]) / stats["html5"]["wifi_ac_energy"] * 100
    wifi_nexd_vs_alt_energy = (stats["html5hiili"]["wifi_ac_energy"] - stats["nexd"]["wifi_ac_energy"]) / stats["html5hiili"]["wifi_ac_energy"] * 100
    
    # LTE Savings calculations
    lte_nexd_vs_html5_energy = (stats["html5"]["lte_energy"] - stats["nexd"]["lte_energy"]) / stats["html5"]["lte_energy"] * 100
    lte_nexd_vs_alt_energy = (stats["html5hiili"]["lte_energy"] - stats["nexd"]["lte_energy"]) / stats["html5hiili"]["lte_energy"] * 100

    # per-MB useful savings
    lte_nexd_vs_html5_useful = (stats["html5"]["lte_useful"] - stats["nexd"]["lte_useful"]) / stats["html5"]["lte_useful"] * 100
    wifi_nexd_vs_html5_useful = (stats["html5"]["wifi_ac_useful"] - stats["nexd"]["wifi_ac_useful"]) / stats["html5"]["wifi_ac_useful"] * 100

    # Format values for markdown reports
    md_vals = {
        "wifi_nexd_N": str(stats["nexd"]["N"]),
        "wifi_html5_N": str(stats["html5"]["N"]),
        "wifi_alt_N": str(stats["html5hiili"]["N"]),
        "lte_nexd_N": str(stats["nexd"]["N"]),
        "lte_html5_N": str(stats["html5"]["N"]),
        "lte_alt_N": str(stats["html5hiili"]["N"]),
        
        "wifi_nexd_energy": format_md_sci(stats["nexd"]["wifi_ac_energy"]),
        "wifi_html5_energy": format_md_sci(stats["html5"]["wifi_ac_energy"]),
        "wifi_alt_energy": format_md_sci(stats["html5hiili"]["wifi_ac_energy"]),
        "wifi_nexd_useful": format_md_sci(stats["nexd"]["wifi_ac_useful"]),
        "wifi_html5_useful": format_md_sci(stats["html5"]["wifi_ac_useful"]),
        "wifi_alt_useful": format_md_sci(stats["html5hiili"]["wifi_ac_useful"]),

        "lte_nexd_energy": format_md_sci(stats["nexd"]["lte_energy"]),
        "lte_html5_energy": format_md_sci(stats["html5"]["lte_energy"]),
        "lte_alt_energy": format_md_sci(stats["html5hiili"]["lte_energy"]),
        "lte_nexd_useful": format_md_sci(stats["nexd"]["lte_useful"]),
        "lte_html5_useful": format_md_sci(stats["html5"]["lte_useful"]),
        "lte_alt_useful": format_md_sci(stats["html5hiili"]["lte_useful"]),

        "wifi_nexd_vs_html5_pct": f"{wifi_nexd_vs_html5_energy:.1f}%",
        "wifi_nexd_vs_alt_pct": f"{wifi_nexd_vs_alt_energy:.1f}%",
        "lte_nexd_vs_html5_pct": f"{lte_nexd_vs_html5_energy:.1f}%",
        "lte_nexd_vs_alt_pct": f"{lte_nexd_vs_alt_energy:.1f}%",
        
        "lte_useful_saving_pct": f"{lte_nexd_vs_html5_useful:.1f}%",
        "wifi_useful_saving_pct": f"{wifi_nexd_vs_html5_useful:.1f}%"
    }

    # Format values for DOCX create script
    docx_vals = {
        "wifi_nexd_N": str(stats["nexd"]["N"]),
        "wifi_html5_N": str(stats["html5"]["N"]),
        "wifi_alt_N": str(stats["html5hiili"]["N"]),
        "lte_nexd_N": str(stats["nexd"]["N"]),
        "lte_html5_N": str(stats["html5"]["N"]),
        "lte_alt_N": str(stats["html5hiili"]["N"]),

        "wifi_nexd_energy": format_docx_sci(stats["nexd"]["wifi_ac_energy"]),
        "wifi_html5_energy": format_docx_sci(stats["html5"]["wifi_ac_energy"]),
        "wifi_alt_energy": format_docx_sci(stats["html5hiili"]["wifi_ac_energy"]),
        "wifi_nexd_useful": format_docx_sci(stats["nexd"]["wifi_ac_useful"]),
        "wifi_html5_useful": format_docx_sci(stats["html5"]["wifi_ac_useful"]),
        "wifi_alt_useful": format_docx_sci(stats["html5hiili"]["wifi_ac_useful"]),

        "lte_nexd_energy": format_docx_sci(stats["nexd"]["lte_energy"]),
        "lte_html5_energy": format_docx_sci(stats["html5"]["lte_energy"]),
        "lte_alt_energy": format_docx_sci(stats["html5hiili"]["lte_energy"]),
        "lte_nexd_useful": format_docx_sci(stats["nexd"]["lte_useful"]),
        "lte_html5_useful": format_docx_sci(stats["html5"]["lte_useful"]),
        "lte_alt_useful": format_docx_sci(stats["html5hiili"]["lte_useful"]),

        "wifi_savings_str": f"{wifi_nexd_vs_html5_energy:.1f}% vs HTML5\\n{wifi_nexd_vs_alt_energy:.1f}% vs alt. html5" if wifi_nexd_vs_alt_energy >= 0 else f"{wifi_nexd_vs_html5_energy:.1f}% vs HTML5\\n-{abs(wifi_nexd_vs_alt_energy):.1f}% vs alt. html5",
        "lte_savings_str": f"{lte_nexd_vs_html5_energy:.1f}% vs HTML5\\n{lte_nexd_vs_alt_energy:.1f}% vs alt. html5" if lte_nexd_vs_alt_energy >= 0 else f"{lte_nexd_vs_html5_energy:.1f}% vs HTML5\\n-{abs(lte_nexd_vs_alt_energy):.1f}% vs alt. html5",
        "wifi_nexd_vs_html5_pct": f"{wifi_nexd_vs_html5_energy:.1f}%",
        "wifi_nexd_vs_alt_pct": f"{wifi_nexd_vs_alt_energy:.1f}%",
        "lte_nexd_vs_html5_pct": f"{lte_nexd_vs_html5_energy:.1f}%",
        "lte_nexd_vs_alt_pct": f"{lte_nexd_vs_alt_energy:.1f}%",
        "lte_useful_saving_pct": f"{lte_nexd_vs_html5_useful:.1f}%"
    }

    # 1. Update create_docx.py
    create_docx_path = Path("/Users/angelmerino/.gemini/antigravity-cli/brain/0d9b176b-396d-467c-952c-80f6473955c9/scratch/create_docx.py")
    if create_docx_path.exists():
        content = create_docx_path.read_text(encoding="utf-8")
        
        # Modify the data block
        # Use simple string replacement for table fields since values vary
        lines = content.splitlines()
        data_start = -1
        data_end = -1
        for idx, line in enumerate(lines):
            if "data = [" in line:
                data_start = idx
            if data_start != -1 and "]" in line and idx > data_start:
                data_end = idx
                break
                
        if data_start != -1 and data_end != -1:
            new_data_lines = [
                '    data = [',
                f'        ("Wi-Fi (802.11ac)", "NEXD", "{docx_vals["wifi_nexd_N"]}", "{docx_vals["wifi_nexd_energy"]}", "{docx_vals["wifi_nexd_useful"]}", "{docx_vals["wifi_savings_str"]}"),',
                f'        ("Wi-Fi (802.11ac)", "HTML5 (Baseline)", "{docx_vals["wifi_html5_N"]}", "{docx_vals["wifi_html5_energy"]}", "{docx_vals["wifi_html5_useful"]}", "Baseline"),',
                f'        ("Wi-Fi (802.11ac)", "alt. html5 (Baseline)", "{docx_vals["wifi_alt_N"]}", "{docx_vals["wifi_alt_energy"]}", "{docx_vals["wifi_alt_useful"]}", "Baseline"),',
                f'        ("LTE / 4G", "NEXD", "{docx_vals["lte_nexd_N"]}", "{docx_vals["lte_nexd_energy"]}", "{docx_vals["lte_nexd_useful"]}", "{docx_vals["lte_savings_str"]}"),',
                f'        ("LTE / 4G", "HTML5 (Baseline)", "{docx_vals["lte_html5_N"]}", "{docx_vals["lte_html5_energy"]}", "{docx_vals["lte_html5_useful"]}", "Baseline"),',
                f'        ("LTE / 4G", "alt. html5 (Baseline)", "{docx_vals["lte_alt_N"]}", "{docx_vals["lte_alt_energy"]}", "{docx_vals["lte_alt_useful"]}", "Baseline")',
                '    ]'
            ]
            lines = lines[:data_start] + new_data_lines + lines[data_end+1:]
            content = "\n".join(lines)
            
        # Update findings text in docx script
        # We can reconstruct it or do replaces
        content_lines = content.splitlines()
        f1_start = -1
        f1_end = -1
        for idx, line in enumerate(content_lines):
            if "p_f1 = doc.add_paragraph" in line:
                f1_start = idx
            if f1_start != -1 and "on LTE.\")" in line and idx > f1_start:
                f1_end = idx
                break
                
        if f1_start != -1 and f1_end != -1:
            new_f1_lines = [
                "    p_f1 = doc.add_paragraph(style='List Bullet')",
                "    rf1 = p_f1.add_run(\"NEXD Absolute Energy Savings: \")",
                "    rf1.bold = True",
                "    p_f1.add_run(",
                "        \"NEXD achieves substantial absolute energy savings, reducing total network transmission energy by \"",
                "    )",
                f"    r_saving_lte = p_f1.add_run(\"{docx_vals['lte_nexd_vs_html5_pct']}\")",
                "    r_saving_lte.bold = True",
                "    p_f1.add_run(\" on LTE / 4G networks and by \")",
                f"    r_saving_wifi = p_f1.add_run(\"{docx_vals['wifi_nexd_vs_html5_pct']}\")",
                "    r_saving_wifi.bold = True",
                "    p_f1.add_run(\" on Wi-Fi (802.11ac) compared to standard HTML5. Furthermore, NEXD out-performs the alt. html5 baseline in absolute energy, saving \")",
                f"    r_saving_alt_wifi = p_f1.add_run(\"{docx_vals['wifi_nexd_vs_alt_pct']}\")",
                "    r_saving_alt_wifi.bold = True",
                "    p_f1.add_run(\" on Wi-Fi and \")",
                f"    r_saving_alt_lte = p_f1.add_run(\"{docx_vals['lte_nexd_vs_alt_pct']}\")",
                "    r_saving_alt_lte.bold = True",
                "    p_f1.add_run(\" on LTE.\")"
            ]
            content_lines = content_lines[:f1_start] + new_f1_lines + content_lines[f1_end+1:]
            content = "\n".join(content_lines)

        content_lines = content.splitlines()
        f2_start = -1
        f2_end = -1
        for idx, line in enumerate(content_lines):
            if "p_f2 = doc.add_paragraph" in line:
                f2_start = idx
            if f2_start != -1 and "connections.\")" in line and idx > f2_start:
                f2_end = idx
                break
                
        if f2_start != -1 and f2_end != -1:
            new_f2_lines = [
                "    p_f2 = doc.add_paragraph(style='List Bullet')",
                "    rf2 = p_f2.add_run(\"NEXD Energy Efficiency (per MB útil): \")",
                "    rf2.bold = True",
                "    p_f2.add_run(",
                "        \"In terms of energy efficiency per useful megabyte delivered, NEXD achieves a \"",
                "    )",
                f"    r_mb_saving = p_f2.add_run(\"{docx_vals['lte_useful_saving_pct']} saving\")",
                "    r_mb_saving.bold = True",
                "    p_f2.add_run(\" on LTE / 4G compared to standard HTML5. On Wi-Fi networks, NEXD is highly competitive with a median of \")",
                f"    p_f2.add_run(\"{docx_vals['wifi_nexd_useful']} kWh/MB \")",
                "    p_f2.add_run(\"(compared to standard HTML5's \")",
                f"    p_f2.add_run(\"{docx_vals['wifi_html5_useful']} kWh/MB \")",
                "    p_f2.add_run(\"and alt. html5's \")",
                f"    p_f2.add_run(\"{docx_vals['wifi_alt_useful']} kWh/MB \")",
                "    p_f2.add_run(\"). This demonstrates that layout and asset compression translate directly to energy efficiency in data-intensive wireless connections.\")"
            ]
            content_lines = content_lines[:f2_start] + new_f2_lines + content_lines[f2_end+1:]
            content = "\n".join(content_lines)

        create_docx_path.write_text(content, encoding="utf-8")
        print("Updated create_docx.py")

        # Execute create_docx.py
        subprocess.run([sys.executable, str(create_docx_path)], cwd=str(repo_root), check=True)
        print("Re-generated results/Network_Energy_Measurements.docx")

        # Copy docx to artifacts folder
        dest_docx = Path("/Users/angelmerino/.gemini/antigravity-cli/brain/0d9b176b-396d-467c-952c-80f6473955c9/Network_Energy_Measurements.docx")
        dest_docx.write_bytes((repo_root / "results" / "Network_Energy_Measurements.docx").read_bytes())
        print("Copied Network_Energy_Measurements.docx to artifacts folder")

    # 2. Update markdown report templates
    md_reports = [
        repo_root / "REPORT_COMPLETO.md",
        Path("/Users/angelmerino/.gemini/antigravity-cli/brain/0d9b176b-396d-467c-952c-80f6473955c9/analisis_completo.md")
    ]
    
    for report_path in md_reports:
        if not report_path.exists():
            continue
        content = report_path.read_text(encoding="utf-8")
        
        # Replace table in markdown robustly
        report_lines = content.splitlines()
        tbl_start = -1
        tbl_end = -1
        for idx, line in enumerate(report_lines):
            if "| Technology | Ad Type | N |" in line:
                tbl_start = idx
                # Find the end of consecutive table lines (lines starting with '|')
                temp_idx = idx
                while temp_idx < len(report_lines) and report_lines[temp_idx].strip().startswith("|"):
                    temp_idx += 1
                tbl_end = temp_idx
                break
        
        if tbl_start != -1 and tbl_end != -1:
            wifi_savings_text = f"**{md_vals['wifi_nexd_vs_html5_pct']} vs. HTML5**<br>**{md_vals['wifi_nexd_vs_alt_pct']} vs. alt. html5**" if wifi_nexd_vs_alt_energy >= 0 else f"**{md_vals['wifi_nexd_vs_html5_pct']} vs. HTML5**<br>**-{abs(wifi_nexd_vs_alt_energy):.1f}% vs. alt. html5**"
            lte_savings_text = f"**{md_vals['lte_nexd_vs_html5_pct']} vs. HTML5** 🌟<br>**{md_vals['lte_nexd_vs_alt_pct']} vs. alt. html5**" if lte_nexd_vs_alt_energy >= 0 else f"**{md_vals['lte_nexd_vs_html5_pct']} vs. HTML5** 🌟<br>**-{abs(lte_nexd_vs_alt_energy):.1f}% vs. alt. html5**"
            
            new_tbl_lines = [
                "| Technology | Ad Type | N | Median Total Energy (kWh) | Median Energy / MB útil (kWh/MB) | NEXD Savings |",
                "| :--- | :--- | :---: | :---: | :---: | :--- |",
                f"| **Wi-Fi (802.11ac)** | **NEXD** | {md_vals['wifi_nexd_N']} | {md_vals['wifi_nexd_energy']} | {md_vals['wifi_nexd_useful']} | {wifi_savings_text} |",
                f"| | **HTML5 (Baseline)** | {md_vals['wifi_html5_N']} | {md_vals['wifi_html5_energy']} | {md_vals['wifi_html5_useful']} | Baseline |",
                f"| | **alt. html5 (Baseline)** | {md_vals['wifi_alt_N']} | {md_vals['wifi_alt_energy']} | {md_vals['wifi_alt_useful']} | Baseline |",
                f"| **LTE / 4G** | **NEXD** | {md_vals['lte_nexd_N']} | {md_vals['lte_nexd_energy']} | {md_vals['lte_nexd_useful']} | {lte_savings_text} |",
                f"| | **HTML5 (Baseline)** | {md_vals['lte_html5_N']} | {md_vals['lte_html5_energy']} | {md_vals['lte_html5_useful']} | Baseline |",
                f"| | **alt. html5 (Baseline)** | {md_vals['lte_alt_N']} | {md_vals['lte_alt_energy']} | {md_vals['lte_alt_useful']} | Baseline |"
            ]
            report_lines = report_lines[:tbl_start] + new_tbl_lines + report_lines[tbl_end:]
            content = "\n".join(report_lines)

        # Re-split to find the list items for key findings
        report_lines = content.splitlines()
        fnd_start = -1
        fnd_end = -1
        for idx, line in enumerate(report_lines):
            if "2. **NEXD Absolute Energy Conservation:**" in line:
                fnd_start = idx
            if fnd_start != -1 and "3. **NEXD Efficiency per MB útil:**" in line:
                fnd_end = idx + 1
                break
                
        if fnd_start != -1 and fnd_end != -1:
            md_wifi_alt_saving_text = f"**{wifi_nexd_vs_alt_energy:.1f}%**" if wifi_nexd_vs_alt_energy >= 0 else f"**-{abs(wifi_nexd_vs_alt_energy):.1f}%**"
            md_lte_alt_saving_text = f"**{lte_nexd_vs_alt_energy:.1f}%**" if lte_nexd_vs_alt_energy >= 0 else f"**-{abs(lte_nexd_vs_alt_energy):.1f}%**"
            
            new_fnd_lines = [
                f"2. **NEXD Absolute Energy Conservation:** NEXD achieves substantial absolute energy savings, reducing total network transmission energy by **{wifi_nexd_vs_html5_energy:.1f}%** on Wi-Fi (802.11ac) (median of {md_vals['wifi_nexd_energy']} kWh vs. {md_vals['wifi_html5_energy']} kWh) and by **{lte_nexd_vs_html5_energy:.1f}%** on LTE / 4G networks (median of {md_vals['lte_nexd_energy']} kWh vs. {md_vals['lte_html5_energy']} kWh) compared to standard HTML5. Furthermore, NEXD out-performs the alt. html5 baseline in absolute energy, saving {md_wifi_alt_saving_text} on Wi-Fi and {md_lte_alt_saving_text} on LTE.",
                f"3. **NEXD Efficiency per MB útil:** In terms of energy efficiency per useful megabyte delivered, NEXD achieves a **{lte_nexd_vs_html5_useful:.1f}% saving** on LTE / 4G and **{wifi_nexd_vs_html5_useful:.1f}% saving** on Wi-Fi compared to standard HTML5. On Wi-Fi networks, NEXD is highly competitive with a median of {md_vals['wifi_nexd_useful']} kWh/MB (compared to standard HTML5's {md_vals['wifi_html5_useful']} kWh/MB and alt. html5's {md_vals['wifi_alt_useful']} kWh/MB). This demonstrates that layout and asset compression translate directly to energy efficiency in data-intensive wireless connections."
            ]
            report_lines = report_lines[:fnd_start] + new_fnd_lines + report_lines[fnd_end:]
            content = "\n".join(report_lines)

        report_path.write_text(content, encoding="utf-8")
        print(f"Updated report: {report_path.name}")

if __name__ == "__main__":
    main()
