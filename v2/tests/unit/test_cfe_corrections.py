"""v239 — the cfe_tariff corrections register: the file, the comparison
with the table, the idempotent UPDATEs, the drift hook."""
from __future__ import annotations

import sys
from pathlib import Path

from argia.core import cfe_corrections as CC

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))


def rows_from(cells):
    return [(k[0], k[1], k[2], k[3], str(v)) for k, v in cells.items()]


class TestRegister:
    def test_file_is_valid_and_carries_arturos_cells(self):
        reg = CC.load()
        assert CC.validate(reg) == []
        w = CC.wanted(reg)
        assert w[("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-03")] == 0.0069
        assert w[("DIST", "BAJA CALIFORNIA", "CAPACIDAD", "2026-12")] == 401.23
        assert w[("DIST", "BAJA CALIFORNIA", "CAPACIDAD", "2026-11")] == 374.56
        assert w[("GDBT", "VALLE DE MEXICO NORTE", "TRANSMISION", "2026-10")] == 0.1809
        assert w[("GDMTH", "BAJA CALIFORNIA", "CENACE", "2026-09")] == 0.0065
        assert len(w) == 6 + 1 + 1 + 6 + 3 + 6 + 4
        assert len(reg["open_questions"]) == 2

    def test_validate_catches_bad_entries(self):
        reg = {"corrections": [{"id": "x", "tariff_code": "GDMTH", "region": "R", "charge_type": "CENACE",
                                "months": ["2026-13"], "value": -1, "status": "maybe", "confirmed_by": "", "basis": ""},
                               {"id": "x", "tariff_code": "GDMTH", "region": "R", "charge_type": "CENACE",
                                "months": ["2026-01"], "value": 1, "status": "confirmed", "confirmed_by": "a", "basis": "b"}]}
        out = CC.validate(reg)
        assert any("duplicate id" in x for x in out) and any("bad month" in x for x in out)
        assert any("status" in x for x in out) and any("non-negative" in x for x in out)


class TestCompareAndApply:
    REG = {"corrections": [
        {"id": "a", "tariff_code": "GDMTO", "region": "NORTE", "charge_type": "SERVICIOS CONEXOS NO MEM",
         "months": ["2026-01", "2026-02"], "value": 0.0069, "status": "confirmed", "confirmed_by": "A", "basis": "b"},
        {"id": "p", "tariff_code": "GDMTH", "region": "X", "charge_type": "CENACE",
         "months": ["2026-01"], "value": 0.0065, "status": "proposed", "confirmed_by": "A", "basis": "b"}]}

    def test_compare_names_differences_and_missing_cells(self):
        rows = rows_from({("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-01"): 0.069})
        out = CC.compare(self.REG, rows)
        assert out == ["GDMTO/NORTE/SERVICIOS CONEXOS NO MEM/2026-01: 0.069 in cfe_tariff, register says 0.0069",
                       "GDMTO/NORTE/SERVICIOS CONEXOS NO MEM/2026-02: not in cfe_tariff (want 0.0069)"]
        # proposed cells are never asserted
        assert not any("GDMTH/X" in x for x in out)

    def test_apply_is_idempotent_and_never_inserts(self):
        rows = rows_from({("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-01"): 0.069,
                          ("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-02"): 0.0069})
        sql = CC.apply_sql(self.REG, rows)
        assert sql == ["UPDATE cfe_tariff SET value_mxn = 0.0069 WHERE tariff_code = 'GDMTO' AND region = 'NORTE'"
                       " AND charge_type = 'SERVICIOS CONEXOS NO MEM' AND month = DATE '2026-01-01' AND value_mxn IS DISTINCT FROM 0.0069;"]
        fixed = rows_from({("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-01"): 0.0069,
                           ("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-02"): 0.0069})
        assert CC.apply_sql(self.REG, fixed) == [] and CC.compare(self.REG, fixed) == []

    def test_select_sql_covers_only_the_registers_codes_and_years(self):
        q = CC.select_sql(self.REG)
        assert "tariff_code IN ('GDMTH', 'GDMTO')" in q and "IN ('2026')" in q and "value_mxn::text" in q


class TestWiring:
    def test_script_and_drift_hook(self):
        import cfe_corrections as CS
        reg = CC.load()
        seen = []
        def rows(sql):
            seen.append(sql)
            return [["GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-01", "0.0069"]]
        assert CS.table_rows(rows, reg) == [("GDMTO", "NORTE", "SERVICIOS CONEXOS NO MEM", "2026-01", "0.0069")]
        assert seen[0].startswith("SET statement_timeout")
        dc = (V2 / "scripts" / "drift_check.py").read_text(encoding="utf-8")
        assert "cfe_corrections.json" in dc and 'f"cfe_tariff: {x}"' in dc
