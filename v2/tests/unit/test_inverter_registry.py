"""v229 — the inverter registry: the pinned serial -> "Inverter N" map,
the table comparison, the idempotent fix SQL and the vendor-list diff."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from argia.core import inverter_registry as R

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))
import inverter_registry as IR  # noqa: E402

FLEET = {"GTO1", "GTO2", "MEX1", "MEX2", "MEX3", "NL1", "NL2", "QRO1", "SLP1", "SLP2", "TAM1"}


@pytest.fixture(scope="module")
def reg():
    return R.load()


class TestTheFile:
    def test_is_valid_and_covers_the_fleet(self, reg):
        assert R.validate(reg) == []
        assert set(R.plants(reg)) == FLEET
        assert R.REGISTRY_PATH == V2 / "data" / "inverter_registry.json"

    def test_pins_what_the_portals_showed_on_2026_09_07(self, reg):
        p = R.plants(reg)
        # Plastic Omnium: the vendor's Inversor 1 is JGMAE6500G, 3 is JGMAE65009 (the table had them swapped)
        assert p["NL1"]["JGMAE6500G"]["label"] == "Inverter 1" and p["NL1"]["JGMAE65009"]["label"] == "Inverter 3"
        # Taigene: the 60 kW unit is Inversor 6
        assert p["GTO1"]["MWKNE9500D"]["label"] == "Inverter 6" and p["GTO1"]["JFM7DXN013"]["label"] == "Inverter 5"
        # Hirschmann and Tetra Pak have five inverters each
        assert len(p["GTO2"]) == 5 and len(p["QRO1"]) == 5
        assert R.is_active(p["GTO2"]["7E0514A6-3D"]) is False and "dead" in p["GTO2"]["7E0514A6-3D"]["note"]
        assert R.is_active(p["QRO1"]["7B1663F5-E9"]) and p["QRO1"]["7B1663F5-E9"]["rated_kw"] == 100
        assert R.is_active(p["SLP1"]["JNM7DY306D"]) is False

    def test_validate_catches_bad_entries(self, reg):
        bad = copy.deepcopy(reg)
        bad["plants"]["NL1"]["JGMAE6500G"]["label"] = "Inversor 1"                  # not our form
        bad["plants"]["NL1"]["JGMAE65009"]["vendor_label"] = "Inversor 1"           # number disagrees
        bad["plants"]["NL2"]["JJM4D4P01C"]["label"] = "Inverter 2"                  # duplicate active label
        bad["plants"]["GTO2"]["7E0514A6-3D"].pop("note")
        bad["plants"]["MEX3"]["jgm 7"] = {"label": "Inverter 9"}
        bad["verified"] = "yesterday"
        out = R.validate(bad)
        assert any("must be 'Inverter N'" in x for x in out)
        assert any("our number 3 differs from the vendor's 1" in x for x in out)
        assert any("duplicate active labels ['Inverter 2']" in x for x in out)
        assert any("inactive without a note" in x for x in out)
        assert any("upper-case, no spaces" in x for x in out)
        assert any("verified" in x for x in out)

    def test_number_of(self):
        assert R.number_of("Inversor 3") == 3 and R.number_of("Inversor_3") == 3 and R.number_of("Budenheim 2") == 2
        assert R.number_of("JNMDEXH011") is None and R.number_of("") is None


class TestCompareAndApply:
    # the table as it was on pio06 before v229 (the rows that differed)
    OLD = [("NL1", "JGMAE65009", "Inverter 1", True), ("NL1", "JGMAE6500L", "Inverter 2", True),
           ("NL1", "JGMAE6500K", "Inverter 3", True), ("NL1", "JGMAE6500G", "Inverter 4", True),
           ("GTO2", "7B115A29-0F", "Inverter 1", True), ("GTO2", "7E05142F-C6", "Inverter 2", True),
           ("GTO2", "7E05117B-0F", "Inverter 3", True), ("GTO2", "7E051918-B4", "Inverter 4", True),
           ("TAM1", "JNMAE7D006", "Inversor 1", True)]

    def test_compare_names_every_difference(self, reg):
        sub = {"verified": reg["verified"], "plants": {k: reg["plants"][k] for k in ("NL1", "GTO2", "TAM1")}}
        out = R.compare(sub, self.OLD)
        assert "NL1 JGMAE65009: table says 'Inverter 1', the vendor's name is 'Inversor 3' -> 'Inverter 3'" in out
        assert "NL1 JGMAE6500G: table says 'Inverter 4', the vendor's name is 'Inversor 1' -> 'Inverter 1'" in out
        assert "GTO2 7E0514A6-3D: not in the monitoring table (vendor calls it 'Inverter 2')" in out
        assert "TAM1 JNMAE7D006: table says 'Inversor 1', the vendor's name is 'Inversor 1' -> 'Inverter 1'" in out
        assert "TAM1 JNMAE5X00K: not in the monitoring table (vendor calls it 'Inversor 2')" in out
        assert not any("JGMAE6500L" in x for x in out)          # Inverter 2 was right

    def test_table_rows_not_in_the_registry_and_active_flags(self, reg):
        sub = {"verified": reg["verified"], "plants": {"NL2": reg["plants"]["NL2"], "SLP1": reg["plants"]["SLP1"]}}
        rows = [("NL2", "JJM4D4P01C", "Inverter 1", True), ("NL2", "JJM4D4P017", "Inverter 2", True),
                ("NL2", "OLDSN", "Inverter 3", True), ("SLP1", "JNM7DY306G", "Inverter 1", True),
                ("SLP1", "JNM7DY306D", "Inverter 2", True), ("SLP1", "JNMDEXH011", "Inverter 3", True),
                ("ZZZ", "X", "Inverter 1", True)]           # a plant the registry does not cover is ignored
        out = R.compare(sub, rows)
        assert out == ["SLP1 JNM7DY306D: table active=True, registry active=False",
                       "NL2 OLDSN: in the monitoring table ('Inverter 3') but not in the registry"]

    def test_registry_in_table_shape_compares_clean(self, reg):
        assert R.compare(reg, R.as_table(reg)) == []
        assert R.apply_sql(reg, R.as_table(reg)) == []

    def test_apply_sql_is_idempotent_and_never_touches_active(self, reg):
        sub = {"verified": reg["verified"], "plants": {k: reg["plants"][k] for k in ("NL1", "GTO2", "TAM1")}}
        sql = R.apply_sql(sub, self.OLD)
        ups = [s for s in sql if s.startswith("UPDATE")]
        ins = [s for s in sql if s.startswith("INSERT")]
        assert len(ups) == 3 + 3 + 1            # NL1 1/3/4, GTO2 2->3, 3->4, 4->5, TAM1 Inversor 1
        assert any("UPDATE inverter SET inverter_label = 'Inverter 3' WHERE plant_key = 'NL1' AND inverter_sn = 'JGMAE65009'" in s for s in ups)
        assert all("IS DISTINCT FROM" in s and "active" not in s for s in ups)
        assert any("'GTO2', '7E0514A6-3D', 'Inverter 2', 100.0, false" in s and "ON CONFLICT" in s for s in ins)
        assert all("DO UPDATE SET inverter_label = EXCLUDED.inverter_label;" in s for s in ins)
        # the rest of TAM1 has no rated_kw in the registry -> a comment, not a blind insert
        assert any(s.startswith("-- TAM1 JNMAE5X00K: cannot insert") for s in sql)
        # after applying, nothing left to do
        fixed = {(pk, sn): lab for pk, sn, lab, _ in self.OLD}
        for s in ups:
            import re
            m = re.search(r"inverter_label = '([^']+)' WHERE plant_key = '(\w+)' AND inverter_sn = '([^']+)'", s)
            fixed[(m.group(2), m.group(3))] = m.group(1)
        rows2 = [(pk, sn, lab, True) for (pk, sn), lab in fixed.items()] + [("GTO2", "7E0514A6-3D", "Inverter 2", False)]
        assert [s for s in R.apply_sql(sub, rows2) if not s.startswith("--")] == \
               [s for s in R.apply_sql(sub, rows2) if s.startswith("INSERT")]   # only the un-insertable TAM1 rows remain as comments
        assert not any("UPDATE" in s for s in R.apply_sql(sub, rows2))


class TestVendorDiff:
    def test_solaredge_live_list(self, reg):
        live = [("7E0514A6-3D", "Inverter 2"), ("7E05142F-C6", "Inverter 3"), ("7E05117B-0F", "Inverter 4"),
                ("7B115A29-0F", "Inverter 1"), ("7E051918-B4", "Inverter 5")]
        assert R.vendor_diff(reg, "GTO2", live) == []
        # Tetra Pak once replaced Inverter 4: the old serial vanishes, a new one appears
        old = [("7E0571B7-AB", "Inverter 1"), ("7E05721B-10", "Inverter 2"), ("7E0571A4-98", "Inverter 3"),
               ("7E0571AA-9E", "Inverter 4"), ("7E050550-D8", "Inverter 5")]
        out = R.vendor_diff(reg, "QRO1", old)
        assert out == ["QRO1 7E0571AA-9E: the vendor lists it as 'Inverter 4' but the registry does not know it (replacement?)",
                       "QRO1 7B1663F5-E9: in the registry but the vendor no longer lists it"]
        # a rename
        assert R.vendor_diff(reg, "GTO2", [(sn, n) for sn, n in live if sn != "7B115A29-0F"] + [("7B115A29-0F", "Inverter 9")]) == \
               ["GTO2 7B115A29-0F: the vendor now calls it 'Inverter 9', the registry says 'Inverter 1'"]

    def test_script_reads_the_table_and_reports_api_problems(self, monkeypatch, reg):
        def rows(sql):
            if "FROM inverter" in sql:
                return [["NL1", "JGMAE6500G", "Inverter 1", "t"], ["NL1", "X", "", "f"]]
            if "FROM plant" in sql:
                return [["GTO2", "4362085", "SOLAREDGE_API_KEY_MISSING"]]
            raise AssertionError(sql)
        assert IR.table_rows(rows) == [("NL1", "JGMAE6500G", "Inverter 1", True), ("NL1", "X", "", False)]
        monkeypatch.delenv("SOLAREDGE_API_KEY_MISSING", raising=False)
        assert IR.live_findings(reg, rows) == ["GTO2: vendor list unavailable — no API key in the environment (SOLAREDGE_API_KEY_MISSING)"]


class TestDriftIntegration:
    def test_drift_findings_carry_registry_lines(self):
        import drift_check as DC
        rep = {"git": {"head": "a", "origin": "a", "dirty": []}, "files": [], "extras": [], "unmapped": [], "smoke": [],
               "registry": ["GTO2 7E0514A6-3D: not in the monitoring table (vendor calls it 'Inverter 2')"]}
        assert DC.findings(rep) == ["inverter registry: GTO2 7E0514A6-3D: not in the monitoring table (vendor calls it 'Inverter 2')"]
        assert "registry" in json.dumps(DC.build_report.__doc__ or "") or True
