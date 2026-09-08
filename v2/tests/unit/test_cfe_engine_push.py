"""Tests: CFE overlay push to the ARGIA Engine (v181).

Contract: CFE_ENGINE_INGEST_CONTRACT.md (2026-09-03). Locked here:
- the fold lets cfe_scrape beat master_db_10 (contract ORDER BY),
- SEMIPUNTA and non-engine codes never leave this host,
- the engine's validation gates run LOCALLY before any push — one bad
  value stops the whole overlay,
- the secret lives only in /root/.argia_cfe_push, never in the repo,
- the push is wired event-driven (ingest) + daily timer + alerting.
"""

import pathlib
import re

from scripts import cfe_engine_push as cep

V2 = pathlib.Path(__file__).resolve().parents[2]
SRC = (V2 / "scripts" / "cfe_engine_push.py").read_text(encoding="utf-8")


def R(code="GDMTH", region="BAJIO", charge="ENERGIA PUNTA",
      ym="2026-01", value="1.5", source="master_db_10"):
    return (code, region, charge, ym, value, source)


def full_rows(n_months=12):
    """Enough rows to clear the 60-value floor: 6 charges x 12 months."""
    rows = []
    for ch in cep.CHARGES_ENERGY:
        for m in range(1, n_months + 1):
            rows.append(R(charge=ch, ym=f"2026-{m:02d}", value="1.1"))
    return rows


ADDERS_H1 = [R(charge="TRANSMISION", value="0.1801"), R(charge="CENACE", value="0.0076"),
             R(charge="SERVICIOS CONEXOS NO MEM", value="0.0069")]


class TestEnergyBasis:
    """v239 — the engine memo of 2026-09-08: scraped energy cells are
    CFE's integrated price, the seed is decomposed; the overlay must be
    on one basis and say which."""

    def test_real_jalisco_june_cell(self):
        # GDMTH/JALISCO 2026-06 on pio06: BASE 0.9995 scraped, adders 0.1801/0.0076/0.0069 seeded
        rows = [R(region="JALISCO", charge="TRANSMISION", ym="2026-06", value="0.1801"),
                R(region="JALISCO", charge="CENACE", ym="2026-06", value="0.0076"),
                R(region="JALISCO", charge="SERVICIOS CONEXOS NO MEM", ym="2026-06", value="0.0069"),
                R(region="JALISCO", charge="ENERGIA BASE", ym="2026-06", value="0.9995", source="cfe_scrape")]
        ov = cep.build_overlay(rows, 2026)
        assert ov["data"]["GDMTH"]["JALISCO"]["ENERGIA BASE"]["2026-06"] == 0.8049
        assert ov["data"]["GDMTH"]["JALISCO"]["TRANSMISION"]["2026-06"] == 0.1801     # adders untouched

    def test_pdbt_also_strips_distribucion_and_capacidad(self):
        # PDBT/JALISCO 2026-08 on pio06: BASE 4.255 scraped; 0.1809 + 0.0065 + 0.0062 + 1.3951 + 1.21 = 2.7987
        rows = [R(code="PDBT", region="JALISCO", charge=c, ym="2026-08", value=v) for c, v in
                (("TRANSMISION", "0.1809"), ("CENACE", "0.0065"), ("SERVICIOS CONEXOS NO MEM", "0.0062"),
                 ("DISTRIBUCION", "1.3951"), ("CAPACIDAD", "1.21"))]
        rows.append(R(code="PDBT", region="JALISCO", charge="ENERGIA BASE", ym="2026-08", value="4.255", source="cfe_scrape"))
        ov = cep.build_overlay(rows, 2026)
        assert ov["data"]["PDBT"]["JALISCO"]["ENERGIA BASE"]["2026-08"] == 1.4563
        # GDMTH must NOT strip the demand charges (they are per kW there)
        rows2 = ADDERS_H1 + [R(charge="CAPACIDAD", value="384.86"), R(charge="ENERGIA BASE", value="0.9995", source="cfe_scrape")]
        assert cep.build_overlay(rows2, 2026)["data"]["GDMTH"]["BAJIO"]["ENERGIA BASE"]["2026-01"] == 0.8049

    def test_master_cells_are_left_alone(self):
        rows = ADDERS_H1 + [R(charge="ENERGIA BASE", value="0.81", source="master_db_10")]
        ov = cep.build_overlay(rows, 2026)
        assert ov["data"]["GDMTH"]["BAJIO"]["ENERGIA BASE"]["2026-01"] == 0.81 and ov["basis"]["decomposed"] == 0

    def test_missing_adders_keep_the_master_value_or_drop(self):
        rows = [R(charge="ENERGIA BASE", value="0.81", source="master_db_10"),
                R(charge="ENERGIA BASE", value="0.9995", source="cfe_scrape")]          # no adders at all
        ov = cep.build_overlay(rows, 2026)
        assert ov["data"]["GDMTH"]["BAJIO"]["ENERGIA BASE"]["2026-01"] == 0.81 and ov["basis"]["kept_master"] == 1
        ov = cep.build_overlay([R(charge="ENERGIA BASE", value="0.9995", source="cfe_scrape")], 2026)
        assert "ENERGIA BASE" not in ov["data"]["GDMTH"]["BAJIO"] and ov["basis"]["dropped"] == 1

    def test_a_decomposed_cell_inside_a_scraped_block_is_not_made_negative(self):
        # engine §6: PDBT/BAJA CALIFORNIA/ENERGIA BASE/2026-07 = 0.734 sits among ~2.45 integrated peers
        rows = [R(code="PDBT", region="BAJA CALIFORNIA", charge=c, ym="2026-07", value=v) for c, v in
                (("TRANSMISION", "0.1809"), ("CENACE", "0.0065"), ("SERVICIOS CONEXOS NO MEM", "0.0062"),
                 ("DISTRIBUCION", "1.3951"), ("CAPACIDAD", "1.21"))]
        rows.append(R(code="PDBT", region="BAJA CALIFORNIA", charge="ENERGIA BASE", ym="2026-07", value="0.734", source="cfe_scrape"))
        ov = cep.build_overlay(rows, 2026)
        assert "ENERGIA BASE" not in ov["data"]["PDBT"]["BAJA CALIFORNIA"] and ov["basis"]["dropped"] == 1

    def test_decompose_arithmetic(self):
        assert cep.decompose(0.9995, [0.1801, 0.0076, 0.0069]) == 0.8049
        assert cep.decompose(0.19, [0.1801, 0.0076, 0.0069]) is None          # not a positive price
        assert cep.decompose(1.0, [0.18, None, 0.0069]) is None
        assert cep.adders_for("PDBT") == cep.ADDERS_PDBT and cep.adders_for("GDMTH") == cep.ADDERS

    def test_validation_requires_the_basis_and_the_stats_stay_home(self):
        ov = cep.build_overlay(full_rows(), 2026)
        assert cep.validate_overlay(ov) == []
        ov2 = dict(ov); ov2.pop("energyBasis")
        assert any("energyBasis" in e for e in cep.validate_overlay(ov2))
        assert "if k != \"basis\"" in SRC and '"energyBasis": ENERGY_BASIS' in SRC


class TestBuildOverlay:
    def test_folds_and_counts(self):
        ov = cep.build_overlay(full_rows(), 2026)
        t, combos, values = cep.overlay_stats(ov)
        assert (t, combos, values) == (1, 1, 72)
        assert ov["data"]["GDMTH"]["BAJIO"]["ENERGIA PUNTA"]["2026-01"] \
            == 1.1

    def test_scrape_beats_master_when_folded_last(self):
        rows = ADDERS_H1 + [R(value="1.0", source="master_db_10"),
                            R(value="2.0", source="cfe_scrape")]   # scrape LAST
        ov = cep.build_overlay(rows, 2026)
        # v239: the scraped (integrated) 2.0 goes out decomposed: 2.0 - (0.1801 + 0.0076 + 0.0069)
        assert ov["data"]["GDMTH"]["BAJIO"]["ENERGIA PUNTA"]["2026-01"] \
            == 1.8054
        assert ov["sourceThrough"] == "2026-01"
        assert ov["energyBasis"] == "decomposed" and ov["basis"] == {"decomposed": 1, "kept_master": 0, "dropped": 0}

    def test_no_scrape_means_null_through(self):
        assert cep.build_overlay([R()], 2026)["sourceThrough"] is None

    def test_source_through_is_max_scrape_month(self):
        rows = [R(ym="2026-03", source="cfe_scrape"),
                R(ym="2026-09", source="cfe_scrape"),
                R(ym="2026-11", source="master_db_10")]
        assert cep.build_overlay(rows, 2026)["sourceThrough"] == "2026-09"

    def test_semipunta_and_unknown_codes_dropped(self):
        rows = [R(charge="ENERGIA SEMIPUNTA"), R(code="DIT"),
                R(code="APBT"), R(charge="NO SUCH CHARGE")]
        assert cep.build_overlay(rows, 2026)["data"] == {}

    def test_bad_months_and_values_dropped(self):
        rows = [R(ym="2026-13"), R(ym="garbage"), R(value="nan"),
                R(value="not-a-number"), R(value="inf")]
        assert cep.build_overlay(rows, 2026)["data"] == {}


class TestValidate:
    def test_clean_overlay_passes(self):
        ov = cep.build_overlay(full_rows(), 2026)
        assert cep.validate_overlay(ov) == []

    def test_one_bad_value_rejects_everything(self):
        ov = cep.build_overlay(full_rows(), 2026)
        ov["data"]["GDMTH"]["BAJIO"]["ENERGIA PUNTA"]["2026-01"] = 51.0
        errs = cep.validate_overlay(ov)
        assert any("out of range" in e for e in errs)

    def test_unit_ranges(self):
        base = cep.build_overlay(full_rows(), 2026)
        g = base["data"]["GDMTH"]["BAJIO"]
        g["FACTOR DE CARGA"] = {"2026-01": 0.5}
        g["SUMINISTRO BASICO"] = {"2026-01": 150000.0}
        g["CAPACIDAD"] = {"2026-01": 4999.0}
        assert cep.validate_overlay(base) == []
        g["FACTOR DE CARGA"]["2026-01"] = 1.2         # >1 → reject
        assert cep.validate_overlay(base)
        g["FACTOR DE CARGA"]["2026-01"] = 0.5
        g["CAPACIDAD"]["2026-01"] = 5001.0            # >5000 → reject
        assert cep.validate_overlay(base)

    def test_coverage_floor(self):
        ov = cep.build_overlay([R()], 2026)
        errs = cep.validate_overlay(ov)
        assert any("coverage floor" in e for e in errs)

    def test_year_bounds(self):
        ov = cep.build_overlay(full_rows(), 2026)
        ov["year"] = 2019
        assert any("year" in e for e in cep.validate_overlay(ov))


class TestConfig:
    def test_parses_key_value(self, tmp_path):
        p = tmp_path / "cfg"
        p.write_text("# engine push\nCFE_PUSH_URL=https://x/y\n"
                     "CFE_PUSH_KEY=abc\n", encoding="utf-8")
        cfg = cep.load_config(str(p))
        assert cfg == {"CFE_PUSH_URL": "https://x/y",
                       "CFE_PUSH_KEY": "abc"}

    def test_missing_file_or_keys_is_none(self, tmp_path):
        assert cep.load_config(str(tmp_path / "absent")) is None
        p = tmp_path / "cfg"
        p.write_text("CFE_PUSH_URL=https://x/y\n", encoding="utf-8")
        assert cep.load_config(str(p)) is None


class TestWiring:
    def test_no_secret_literal_in_repo(self):
        # the shared secret is a 48-char hex string; the repo must not
        # contain any such literal — config file only, never code
        assert not re.search(r"[0-9a-f]{48}", SRC)

    def test_contract_sql_semantics(self):
        assert "charge_type <> 'ENERGIA SEMIPUNTA'" in SRC
        assert "(source='cfe_scrape')" in SRC           # scrape folds last
        assert "'GDMTH', 'GDMTO', 'PDBT', 'RAMT', 'GDBT', 'DIST'" \
            in repr(cep.ENGINE_CODES)

    def test_ingest_triggers_push_event_driven(self):
        ing = (V2 / "server" / "bundle" / "cfe_ingest.py").read_text(
            encoding="utf-8")
        assert '"argia-cfe-push.service"' in ing
        assert "if loaded:" in ing

    def test_units_exist_and_alerting_covers_the_push(self):
        svc = (V2 / "server" / "bundle" / "argia-cfe-push.service"
               ).read_text(encoding="utf-8")
        assert "run_job.sh cfepush cfe_engine_push.py" in svc
        tim = (V2 / "server" / "bundle" / "argia-cfe-push.timer"
               ).read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 15:45:00 UTC" in tim
        assert "Persistent=true" in tim
        mailer = (V2 / "scripts" / "alert_mailer.py").read_text(
            encoding="utf-8")
        assert '"argia-cfe-push"' in mailer

    def test_secret_never_logged(self):
        # the only use of the key is the request header
        assert SRC.count("CFE_PUSH_KEY") <= 3
        assert 'LOG.info("%s", cfg' not in SRC
