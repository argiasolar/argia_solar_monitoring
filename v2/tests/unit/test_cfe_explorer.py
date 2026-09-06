"""v212 — Setup › CFE › Tariff explorer (pure module, no PostgreSQL).

Tomasz 2026-09-05: "make it like it used to be where we can search for
it, make also an easy indicator that they are up to date, and also make
a tile with average prices for BASE, PUNTA and INTERMEDIA in different
tariff scheme, display GDMTH as default"."""
import datetime as dt
import json
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))

import cfe_explorer as CX   # noqa: E402


def rows(month="2026-09"):
    """A tiny cfe_tariff extract: GDMTH in two regions, GDMTO flat, the
    domestic DB1 that must be dropped, and one older month."""
    out = []
    for reg, base, inter, punta in [("BAJIO", 1.0, 1.5, 2.0), ("GOLFO NORTE", 1.2, 1.7, 2.4)]:
        out += [("GDMTH", reg, month, "ENERGIA BASE", "MXN/KWH", base),
                ("GDMTH", reg, month, "ENERGIA INTERMEDIA", "MXN/KWH", inter),
                ("GDMTH", reg, month, "ENERGIA PUNTA", "MXN/KWH", punta),
                ("GDMTH", reg, month, "CAPACIDAD", "MXN/KW", 300.0)]
    out += [("GDMTH", "BAJIO", "2026-08", "ENERGIA BASE", "MXN/KWH", 0.9),
            ("GDMTH", "GOLFO NORTE", "2026-08", "ENERGIA BASE", "MXN/KWH", 1.1),
            ("GDMTO", "BAJIO", month, "ENERGIA", "MXN/KWH", 2.2),
            ("DB1", "BAJIO", month, "ENERGIA BASE", "MXN/KWH", 0.5)]
    return out


class TestDataset:
    def test_nested_shape_units_and_months(self):
        ds = CX.build_dataset(rows())
        assert ds["months"] == ["2026-08", "2026-09"]
        assert ds["data"]["GDMTH"]["BAJIO"]["ENERGIA BASE"] == {"2026-08": 0.9, "2026-09": 1.0}
        assert ds["units"]["CAPACIDAD"] == "MXN/KW"
        assert "DB1" not in ds["data"]                    # domestic dropped
        assert "GDMTO" in ds["data"]

    def test_window_keeps_the_last_n_months_only(self):
        r = rows() + [("GDMTH", "BAJIO", "2024-01", "ENERGIA BASE", "MXN/KWH", 0.1)]
        ds = CX.build_dataset(r, months_shown=2)
        assert ds["months"] == ["2026-08", "2026-09"]
        assert "2024-01" not in ds["data"]["GDMTH"]["BAJIO"]["ENERGIA BASE"]

    def test_garbage_rows_are_skipped(self):
        ds = CX.build_dataset([("GDMTH", "BAJIO", "2026-09", "ENERGIA BASE", "MXN/KWH", "n/a"),
                               ("GDMTH",), ("", "BAJIO", "2026-09", "X", "", "1")])
        assert ds["data"] == {} and ds["months"] == []


class TestAverages:
    def test_period_average_across_regions(self):
        ds = CX.build_dataset(rows())
        a = CX.period_average(ds["data"], "GDMTH", "ENERGIA PUNTA", "2026-09")
        assert a == {"avg": 2.2, "min": 2.0, "max": 2.4, "n": 2}
        assert CX.period_average(ds["data"], "GDMTH", "ENERGIA PUNTA", "2026-08") is None
        assert CX.period_average(ds["data"], "NOPE", "ENERGIA PUNTA", "2026-09") is None

    def test_scheme_averages_latest_month_and_previous(self):
        ds = CX.build_dataset(rows())
        s = CX.scheme_averages(ds["data"], ds["months"])
        g = s["GDMTH"]
        assert g["month"] == "2026-09" and g["prev"] == "2026-08"
        assert g["ENERGIA BASE"]["avg"] == pytest.approx(1.1)
        assert g["ENERGIA BASE"]["prev_avg"] == pytest.approx(1.0)
        assert g["ENERGIA INTERMEDIA"]["prev_avg"] is None      # not quoted in August
        # a flat scheme lists with no period prices — never an error
        assert s["GDMTO"] == {"month": None, "prev": None}

    def test_seeded_future_months_stay_out_of_the_averages(self):
        # pio06 2026-09-06: cfe_scrape through 2026-09, master_db seeds to 2026-12
        r = rows() + [("GDMTH", "BAJIO", "2026-12", "ENERGIA BASE", "MXN/KWH", 9.9),
                      ("GDMTH", "BAJIO", "2026-10", "ENERGIA BASE", "MXN/KWH", 8.8)]
        ds = CX.build_dataset(r)
        assert ds["months"][-1] == "2026-12"
        # the window counts verified months; seeded ones ride along at the end
        ds2 = CX.build_dataset(r, months_shown=1, upto="2026-09")
        assert ds2["months"] == ["2026-09", "2026-10", "2026-12"]
        s = CX.scheme_averages(ds["data"], ds["months"], upto="2026-09")
        assert s["GDMTH"]["month"] == "2026-09" and s["GDMTH"]["ENERGIA BASE"]["avg"] == pytest.approx(1.1)
        # without a verified month the latest available wins (old behaviour)
        assert CX.scheme_averages(ds["data"], ds["months"])["GDMTH"]["month"] == "2026-12"
        h = CX.explorer_html(ds, "good", "x", "x", through="2026-09")
        assert 'const THROUGH="2026-09"' in h and 'class="seed"' in h


class TestFreshness:
    def test_verified_through_needs_all_scrapeable_tariffs(self):
        cov = [("2026-09", "cfe_scrape", f"T{i}") for i in range(10)]
        assert CX.verified_through(cov) == "2026-09"
        assert CX.verified_through(cov[:9]) == ""                      # partial load
        assert CX.verified_through([("2026-09", "master_db_10", "GDMTH")] * 12) == ""
        cov += [("2026-10", "cfe_scrape", "GDMTH")]                    # partial October
        assert CX.verified_through(cov) == "2026-09"

    def test_pill_states(self):
        today = dt.date(2026, 9, 6)
        assert CX.freshness("2026-09", today)[0] == "good"
        assert CX.freshness("2026-09", today, healthy=True)[0] == "good"
        assert CX.freshness("2026-09", today, healthy=False)[0] == "warn"
        tone, en, _ = CX.freshness("2026-08", today)
        assert tone == "warn" and "2026-09 not loaded yet" in en
        assert CX.freshness("2026-06", today)[0] == "bad"
        assert CX.freshness("", today)[0] == "bad"
        # same rule as the report home's lightning: through >= current month
        assert CX.freshness("2026-10", today)[0] == "good"

    def test_january_previous_month_is_december(self):
        assert CX.freshness("2025-12", dt.date(2026, 1, 3))[0] == "warn"


class TestHtml:
    def test_card_has_search_selects_tiles_and_gdmth_default(self):
        ds = CX.build_dataset(rows())
        h = CX.explorer_html(ds, "good", "Up to date — CFE-verified through 2026-09",
                             "Al día", sources_note="cfe_scrape: through 2026-09 (loaded 2026-09-02)")
        assert 'id="cfe_q"' in h and 'type="search"' in h
        assert 'id="cfe_tar"' in h and 'id="cfe_reg"' in h
        assert 'id="cfe_tiles"' in h and 'id="cfe_schemes"' in h and 'id="cfe_tbl"' in h
        assert 'class="pill good"' in h and "Up to date" in h
        assert 'const DEFAULT="GDMTH"' in h
        assert "cfe_scrape: through 2026-09" in h
        assert "Download PDF" in h and "window.print()" in h
        assert "localStorage.setItem('argia_cfe'" in h        # selection remembered as before
        # embedded JSON is valid and carries the averages
        m = re.search(r"const AVG=(\{.*?\});\n", h)
        avg = json.loads(m.group(1))
        assert avg["GDMTH"]["ENERGIA BASE"]["n"] == 2
        assert "DB1" not in json.loads(re.search(r"const DATA=(\{.*?\});\n", h).group(1))

    def test_default_falls_back_when_gdmth_is_absent(self):
        ds = CX.build_dataset([("GDMTO", "BAJIO", "2026-09", "ENERGIA", "MXN/KWH", 2.2)])
        assert 'const DEFAULT="GDMTO"' in CX.explorer_html(ds, "good", "x", "x")

    def test_empty_dataset_is_a_quiet_card(self):
        h = CX.explorer_html(CX.build_dataset([]), "bad", "x", "x")
        assert "No tariff rows loaded yet" in h and "<script>" not in h

    def test_charge_order_base_semi_peak(self):
        i, j, k = (CX.CHARGE_ORDER.index(c) for c in ("ENERGIA BASE", "ENERGIA SEMIPUNTA", "ENERGIA PUNTA"))
        assert i < j < k


class TestWiring:
    def test_setup_cfe_drawer_leads_with_the_explorer(self):
        sa = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        assert "def cfe_explorer_card():" in sa
        assert "sections = [('explorer', cfe_explorer_card())]" in sa
        # v214: everyone signed in gets the explorer; status/push stay with admins
        assert "if drawer == 'cfe':                       # v214: open to everyone signed in" in sa
        assert "if is_global:\n        sections += [('status', cfe_status_card()), ('push', cfe_push_card())]" in sa
        import auth_core as ac
        assert ac.area_for_path("/setup/cfe/") == ac.ALL
        assert ac.area_for_path("/setup/cfe/x") == ac.ALL
        assert ac.area_for_path("/setup/") == ac.ADMIN and ac.area_for_path("/setup/people/") == ac.ADMIN
        assert "CX.explorer_html(ds, tone, en, es, sources_note=note, through=through)" in sa
        assert "ds = CX.build_dataset(rows, upto=through)" in sa
        # read-only on cfe_tariff: no INSERT/UPDATE/DELETE anywhere near it
        body = sa[sa.index("def cfe_explorer_card():"):sa.index("def cfe_drawer(")]
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER)\b", body)
        assert "CFE_CACHE_SEC = 900" in sa
        # the same freshness rule as report_gen's lightning
        assert "(now() - heartbeat_ts) < interval '48 hours'" in sa
        assert "WHERE source='cfe_scrape'" in sa

    def test_catalog_tab_registered(self):
        import setup_catalog as cat
        assert [t for t, _, _ in cat.drawer("cfe")["tabs"]] == ["explorer", "status", "push"]
