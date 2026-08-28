"""CFE tariff pipeline units: page parser (against the captured real
GDMTH fragment), charge mapping, month ranges, watchdog alerts, and
ingest CSV validation."""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cfe_scrape = _load("cfe_scrape", "pi/cfe/cfe_scrape.py")
cfe_ingest = _load("cfe_ingest", "server/bundle/cfe_ingest.py")

FIXTURE = (ROOT / "tests" / "fixtures" / "cfe" /
           "gdmth_jalisco_2026-08.html").read_text(encoding="utf-8")


class TestParseChargeTable:
    def test_real_gdmth_fragment(self):
        tag, mtag, rows = cfe_scrape.parse_charge_table(FIXTURE)
        assert tag == "GDMTH"
        assert mtag == "AGO-26"
        assert len(rows) == 6
        vals = {(r[0], r[1]): r[3] for r in rows}
        assert vals[("-", "Fijo")] == 330.66
        assert vals[("Base", "Variable (Energía)")] == 0.9834
        assert vals[("Punta", "Variable (Energía)")] == 1.9675
        assert vals[("-", "Capacidad")] == 377.17

    def test_no_table(self):
        assert cfe_scrape.parse_charge_table("<html></html>") == \
            (None, None, [])

    # flat layout (no "Int. Horario" column) — PDBT/GDMTO/RABT/RAMT/
    # APBT/APMT/GDBT; structure mirrors the live page, values from the
    # rendered PDBT/GDMTO JALISCO AGO-26 pages (captured 2026-08-28)
    FLAT = (
        '<table class="table table-bordered table-striped"><tbody>'
        "<tr><th>Tarifa</th><th>Descripción</th><th>Cargo</th>"
        "<th>Unidades</th><th>AGO-26</th></tr>"
        '<tr><th rowspan="2">PDBT</th><th rowspan="2">Pequeña demanda'
        "</th><td>Fijo </td><td>$/mes</td><td>33.07</td></tr>"
        "<tr><td>Variable (Energía)</td><td>$/kWh</td><td>4.255</td>"
        "</tr></tbody></table>")

    def test_flat_layout_pdbt(self):
        tag, mtag, rows = cfe_scrape.parse_charge_table(self.FLAT)
        assert tag == "PDBT"
        assert mtag == "AGO-26"
        assert rows == [("-", "Fijo", "$/mes", 33.07),
                        ("-", "Variable (Energía)", "$/kWh", 4.255)]
        mapped = [cfe_scrape.map_charge(h, c) for h, c, _u, _v in rows]
        assert mapped == [("SUMINISTRO BASICO", "MXN/MONTH"),
                          ("ENERGIA BASE", "MXN/KWH")]


class TestMapCharge:
    def test_gdmth_set(self):
        mc = cfe_scrape.map_charge
        assert mc("-", "Fijo ") == ("SUMINISTRO BASICO", "MXN/MONTH")
        assert mc("Base", "Variable (Energía)") == \
            ("ENERGIA BASE", "MXN/KWH")
        assert mc("Intermedia", "Variable (Energía)") == \
            ("ENERGIA INTERMEDIA", "MXN/KWH")
        assert mc("Punta", "Variable (Energía)") == \
            ("ENERGIA PUNTA", "MXN/KWH")
        assert mc("-", "Distribución") == \
            ("DISTRIBUCION", "MXN/KW-MONTH")
        assert mc("-", "Capacidad") == ("CAPACIDAD", "MXN/KW-MONTH")

    def test_single_rate_maps_to_base(self):
        # PDBT-style: no horario split -> ENERGIA BASE (seed keeps
        # intermedia/punta at 0 for flat tariffs)
        assert cfe_scrape.map_charge("-", "Variable (Energía)") == \
            ("ENERGIA BASE", "MXN/KWH")

    def test_unknown_concept_passes_through(self):
        ct, unit = cfe_scrape.map_charge("-", "Cargo Nuevo")
        assert ct == "CARGO NUEVO" and unit == ""


class TestMonthRange:
    def test_single(self):
        assert cfe_scrape.month_range("2026-08") == [(2026, 8)]

    def test_range_across_year(self):
        r = cfe_scrape.month_range("2025-11:2026-02")
        assert r == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]

    def test_backfill_year(self):
        assert len(cfe_scrape.month_range("2025-09:2026-08")) == 12


class TestNorm:
    def test_accents_and_case(self):
        assert cfe_scrape.norm("Valle de México  Centro") == \
            "VALLE DE MEXICO CENTRO"


sys.path.insert(0, str(ROOT))
from argia.alerts import monitor                       # noqa: E402

D10 = dt.date(2026, 8, 10)
D5 = dt.date(2026, 8, 5)


class TestCfeAlerts:
    def test_not_deployed_is_silent(self):
        assert monitor.cfe_alerts(None) == []
        assert monitor.cfe_alerts({}) == []

    def _ok(self):
        return {"heartbeat_age_h": 2.0, "probe_status": "ok",
                "last_csv_result": "loaded",
                "coverage_month": "2026-08-01"}

    def test_healthy_is_quiet(self):
        assert monitor.cfe_alerts(self._ok(), today=D10) == []

    def test_stale_heartbeat(self):
        s = self._ok()
        s["heartbeat_age_h"] = 80.0
        keys = [a.key for a in monitor.cfe_alerts(s, today=D10)]
        assert "cfe-heartbeat" in keys

    def test_probe_fail(self):
        s = self._ok()
        s["probe_status"] = "fail"
        keys = [a.key for a in monitor.cfe_alerts(s, today=D10)]
        assert keys == ["cfe-probe"]

    def test_rejected_csv(self):
        s = self._ok()
        s["last_csv_result"] = "rejected"
        keys = [a.key for a in monitor.cfe_alerts(s, today=D10)]
        assert keys == ["cfe-reject"]

    def test_coverage_gap_after_day10(self):
        s = self._ok()
        s["coverage_month"] = "2026-07-01"
        assert [a.key for a in monitor.cfe_alerts(s, today=D5)] == []
        keys = [a.key for a in monitor.cfe_alerts(s, today=D10)]
        assert keys == ["cfe-coverage"]

    def test_all_warnings(self):
        s = {"heartbeat_age_h": None, "probe_status": "",
             "last_csv_result": "rejected", "coverage_month": ""}
        alerts = monitor.cfe_alerts(s, today=D10)
        assert alerts and all(a.severity == monitor.SEV_WARN
                              for a in alerts)


class TestIngestValidate:
    def _write(self, tmp_path, rows,
               header="tariff_code,region,month,charge_type,unit,"
                      "value_mxn"):
        p = tmp_path / "t.csv"
        p.write_text(header + "\n" + "\n".join(rows) + "\n",
                     encoding="utf-8")
        return str(p)

    def test_good_csv(self, tmp_path):
        p = self._write(tmp_path, [
            "GDMTH,JALISCO,2026-08-01,ENERGIA BASE,MXN/KWH,0.9834",
            "GDMTH,JALISCO,2026-08-01,CAPACIDAD,MXN/KW-MONTH,377.17"])
        ok, info = cfe_ingest.validate(p)
        assert ok and "2 rows" in info

    def test_bad_header(self, tmp_path):
        p = self._write(tmp_path, ["a,b,c,d,e,f"],
                        header="x,y,z,w,v,u")
        ok, info = cfe_ingest.validate(p)
        assert not ok and "header" in info

    def test_out_of_range_value(self, tmp_path):
        p = self._write(tmp_path, [
            "GDMTH,JALISCO,2026-08-01,ENERGIA BASE,MXN/KWH,99999"])
        ok, info = cfe_ingest.validate(p)
        assert not ok and "out-of-range" in info

    def test_negative_value(self, tmp_path):
        p = self._write(tmp_path, [
            "GDMTH,JALISCO,2026-08-01,ENERGIA BASE,MXN/KWH,-1"])
        ok, _info = cfe_ingest.validate(p)
        assert not ok

    def test_empty(self, tmp_path):
        p = tmp_path / "e.csv"
        p.write_text("", encoding="utf-8")
        ok, _info = cfe_ingest.validate(str(p))
        assert not ok
