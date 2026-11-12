"""PPA monthly performance report (v374): the Excel workbook - every number of the month, so anyone can check it.

Sheets: Summary, Plants, Bridge, Daily, Trend, Events, Method. Values are numbers (not text) with a display
format, so the reader can add them up; the Daily sheet holds the inputs every monthly figure is a sum of.
"""
from __future__ import annotations

import io
from typing import Dict, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from argia.report import ppa_monthly as PM
from argia.report import ppa_monthly_view as V

HEAD_FILL = PatternFill("solid", fgColor="E6F7F5")
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14, color="053B38")
F_KWH, F_MXN, F_PCT, F_TAR = "#,##0", '"$"#,##0', "0.0%", "0.0000"


def _sheet(wb: Workbook, title: str, head: Sequence[str], rows: Sequence[Sequence], fmts: Sequence[Optional[str]],
           widths: Optional[Sequence[int]] = None):
    ws = wb.create_sheet(title)
    ws.append(list(head))
    for c in ws[1]:
        c.font, c.fill = BOLD, HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append(list(r))
    for j, f in enumerate(fmts, start=1):
        if f:
            for i in range(2, ws.max_row + 1):
                ws.cell(i, j).number_format = f
    for j in range(1, len(head) + 1):
        ws.column_dimensions[get_column_letter(j)].width = (widths[j - 1] if widths else 14)
    ws.freeze_panes = "B2"
    return ws


def workbook(rep: Dict, daily: Sequence[Dict] = ()) -> bytes:
    wb = Workbook()
    s = wb.active
    s.title = "Summary"
    P = rep["portfolio"]
    pub = rep.get("published")
    s["A1"] = f"ARGIA PPA portfolio - monthly performance report {rep['month']}"
    s["A1"].font = TITLE
    s["A2"] = (f"Published {pub['utc'][:10]} by {pub['by']}" if pub else
               ("Draft - month to date" if not rep["complete"] else f"Draft - publish by {rep['deadline']}"))
    s["A3"] = rep["headline"]["en"]
    s["A4"] = rep["headline"]["es"]
    for c in ("A3", "A4"):
        s[c].alignment = Alignment(wrap_text=True, vertical="top")
    s.merge_cells("A3:F3")
    s.merge_cells("A4:F4")
    s.row_dimensions[3].height = s.row_dimensions[4].height = 60
    loss = P.get("loss") or {}
    kpis = [("Plants", P["plants"], "0"), ("kWp DC", P["kwp_dc"], F_KWH),
            ("Actual kWh", P["actual_kwh"], F_KWH), ("Contract kWh (prorated)", P["contract_kwh"], F_KWH),
            ("Actual / contract", P["promise"], F_PCT), ("Sun vs typical (index)", P["sun_index"], F_PCT),
            ("Actual / design for the measured sun (EPI)", P["epi"], F_PCT),
            ("Plants with a design in the EPI", P["epi_plants"], "0"),
            ("Availability", P["availability"], F_PCT),
            ("Revenue earned MXN", P["revenue_earned_mxn"], F_MXN),
            ("Revenue invoiced MXN", P["revenue_invoiced_mxn"], F_MXN),
            ("Budget MXN (contract x tariff)", P["budget_mxn"], F_MXN),
            ("Compensated kWh (customer stops)", P["compensated_kwh"], F_KWH),
            ("Recoverable losses kWh", loss.get("lost_kwh"), F_KWH),
            ("Recoverable losses MXN", loss.get("lost_mxn"), F_MXN),
            ("CO2 avoided t", P["co2_t"], "#,##0.0")]
    s["A6"], s["B6"] = "Portfolio", "Value"
    s["A6"].font = s["B6"].font = BOLD
    for i, (k, v, f) in enumerate(kpis, start=7):
        s.cell(i, 1, k)
        c = s.cell(i, 2, v)
        c.number_format = f
    r0 = 7 + len(kpis) + 1
    s.cell(r0, 1, "Bridge step").font = BOLD
    s.cell(r0, 2, "kWh").font = BOLD
    s.cell(r0, 3, "MXN").font = BOLD
    s.cell(r0 + 1, 1, "Contract curve")
    s.cell(r0 + 1, 2, P["contract_kwh"]).number_format = F_KWH
    i = r0 + 2
    for k in PM.STEPS[1:-1]:
        if P["bridge_kwh"].get(k) is not None:
            s.cell(i, 1, V.STEP_LABEL[k][0])
            s.cell(i, 2, P["bridge_kwh"][k]).number_format = F_KWH
            s.cell(i, 3, P["bridge_mxn"].get(k)).number_format = F_MXN
            i += 1
    s.cell(i, 1, "Actual (plants with a contract curve)")
    s.cell(i, 2, P["actual_with_contract_kwh"]).number_format = F_KWH
    s.column_dimensions["A"].width = 44
    s.column_dimensions["B"].width = 18
    s.column_dimensions["C"].width = 16

    pl = rep["plants"]
    _sheet(wb, "Plants",
           ["Plant", "Code", "kWp DC", "Days", "Data days", "Sun days", "Actual kWh", "Contract kWh (full month)",
            "Contract kWh (prorated)", "Actual / contract", "Design typical kWh", "Design for measured sun kWh",
            "Irradiance kWh/m2", "Sun vs typical", "EPI (sun days)", "Availability", "Tariff MXN/kWh",
            "Budget MXN", "Revenue earned MXN", "Revenue invoiced MXN", "Compensated kWh", "Lost kWh",
            "Unavailability kWh", "Overheating kWh", "Under-performance kWh", "Lost MXN", "CO2 t", "Yield kWh/kWp",
            "Design source"],
           [[p["name"], p["plant_key"], p["kwp_dc"], p["days"], p["data_days"], p["sun_days"], p["actual_kwh"],
             p["contract_full_kwh"], p["contract_kwh"], p["promise"], p["design_typ_kwh"], p["design_sun_kwh"],
             p["irradiance_kwh_m2"], p["sun_index"], p["epi"], p["availability"], p["tariff"], p["budget_mxn"],
             p["revenue_earned_mxn"], p["revenue_invoiced_mxn"], p["compensated_kwh"],
             *[(p["loss"] or {}).get(k) for k in ("lost_kwh", "unavailability_kwh", "overheating_kwh",
                                                  "underperformance_kwh", "lost_mxn")],
             p["co2_t"], p["yield_kwh_kwp"], p["design_source"] or ""] for p in pl],
           [None, None, F_KWH, "0", "0", "0", F_KWH, F_KWH, F_KWH, F_PCT, F_KWH, F_KWH, "0.00", F_PCT, F_PCT, F_PCT,
            F_TAR, F_MXN, F_MXN, F_MXN, F_KWH, F_KWH, F_KWH, F_KWH, F_KWH, F_MXN, "#,##0.0", "0.0", None],
           [22, 7] + [13] * 26 + [40])
    steps = [k for k in PM.STEPS[1:-1]]
    _sheet(wb, "Bridge",
           ["Plant", "Tariff", "Contract kWh"] + [V.STEP_LABEL[k][0] + " kWh" for k in steps] + ["Actual kWh"]
           + [V.STEP_LABEL[k][0] + " MXN" for k in steps],
           [[p["name"], p["tariff"], p["contract_kwh"]] + [p["bridge"].get(k) for k in steps] + [p["actual_kwh"]]
            + [(p["bridge"][k] * p["tariff"]) if p["bridge"].get(k) is not None and p["tariff"] else None for k in steps]
            for p in pl if p["contract_kwh"] is not None],
           [None, F_TAR, F_KWH] + [F_KWH] * len(steps) + [F_KWH] + [F_MXN] * len(steps), [22] + [15] * (3 + 2 * len(steps)))
    _sheet(wb, "Daily",
           ["Plant", "Code", "Date", "Energy kWh", "Billable kWh", "Irradiance kWh/m2", "Contract kWh/day",
            "Design typical kWh/day", "Design for measured sun kWh", "Availability", "Cloud cover %", "Lost kWh",
            "Unavailability kWh", "Overheating kWh", "Under-performance kWh", "Lost MXN"],
           [[d["name"], d["plant_key"], d["date"], d["energy_kwh"], d["billable_kwh"], d["irradiance_kwh_m2"],
             d["contract_day_kwh"], d["design_typ_day_kwh"], d["design_sun_kwh"], d["availability"],
             d["cloud_cover_pct"], d["lost_kwh"], d["unavailability_kwh"], d["overheating_kwh"],
             d["underperformance_kwh"], d["lost_mxn"]] for d in daily],
           [None, None, None, "#,##0.0", "#,##0.0", "0.000", "#,##0.0", "#,##0.0", "#,##0.0", F_PCT, "0.0",
            "#,##0.0", "#,##0.0", "#,##0.0", "#,##0.0", F_MXN], [22, 7, 11] + [13] * 13)
    _sheet(wb, "Trend",
           ["Month", "Actual kWh", "Contract kWh", "Actual / contract", "EPI", "Sun vs typical", "Availability",
            "Revenue earned MXN", "Lost MXN"],
           [[t["month"], t["actual_kwh"], t["contract_kwh"], t["promise"], t["epi"], t["sun_index"],
             t["availability"], t["revenue_earned_mxn"], t["lost_mxn"]] for t in rep["trend"]],
           [None, F_KWH, F_KWH, F_PCT, F_PCT, F_PCT, F_PCT, F_MXN, F_MXN], [10] + [16] * 8)
    ev: List[List] = [[p["name"], "event", e["start"], e["end"] or "", e["category"], e["deemed_kwh"], e["note"]]
                      for p in pl for e in p["events"]]
    ev += [[p["name"], "below availability target", e["start"], e["end"], f'{e["days"]} days, lowest {e["min"]:.1%}',
            e["unavailability_kwh"], ""] for p in pl for e in p["episodes"]]
    ev += [[t["plant_key"], "ticket open at month end", t["created"], "", f'{t["number"]} {t["priority"]} {t["status"]}',
            None, t["title"]] for t in rep["tickets"]["open_list"]]
    _sheet(wb, "Events", ["Plant", "Kind", "From", "To", "Detail", "kWh", "Note"], ev,
           [None, None, None, None, None, "#,##0.0", None], [22, 26, 11, 11, 30, 10, 70])
    m = wb.create_sheet("Method")
    m.column_dimensions["A"].width = 140
    m["A1"] = "How the numbers are made"
    m["A1"].font = TITLE
    for i, ln in enumerate((PM.__doc__ or "").splitlines(), start=3):
        m.cell(i, 1, ln)
    for p in pl:
        for en, _es in p["notes"]:
            m.append([f"{p['name']}: {en}"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
