"""v80: brand filter flags + per-site SolarEdge quota semantics."""

from unittest.mock import MagicMock, patch

from scripts.telemetry_5m import (
    KNOWN_BRANDS, _run_solaredge, _SolarEdgeQuotaExhausted, brand_enabled,
)


class TestBrandEnabled:
    def test_default_runs_everything(self):
        assert all(brand_enabled(b, None, None) for b in KNOWN_BRANDS)

    def test_only(self):
        assert brand_enabled("SOLAREDGE", "SOLAREDGE", None)
        assert not brand_enabled("GROWATT", "SOLAREDGE", None)

    def test_skip(self):
        assert not brand_enabled("SOLAREDGE", None, "SOLAREDGE")
        assert brand_enabled("GROWATT", None, "SOLAREDGE")


def _plant(pk, site, secret):
    p = MagicMock()
    p.plant_key = pk
    p.site_id = site
    p.secret_api_name = secret
    return p


class TestQuotaPerSite:
    """QRO1 hitting its 300/day budget must not stop GTO2 — the
    budgets are per site."""

    def _portfolio(self, plants):
        pf = MagicMock()
        pf.plants_by_brand.return_value = plants
        pf.inverters_for.return_value = [MagicMock()]
        return pf

    @patch("scripts.telemetry_5m._fetch_weather_for_plant")
    @patch("scripts.telemetry_5m._process_solaredge_plant")
    @patch("scripts.telemetry_5m.SolarEdgeClient")
    @patch.dict("os.environ", {"SOLAREDGE_API_KEY": "k1",
                               "SOLAREDGE_API_KEY2": "k2"})
    def test_second_site_still_processed(self, _cli, proc, _weather):
        q = _plant("QRO1", "4146396", "SOLAREDGE_API_KEY")
        g = _plant("GTO2", "4362085", "SOLAREDGE_API_KEY2")
        proc.side_effect = [_SolarEdgeQuotaExhausted(), (["row"], 0)]
        common, processed, skipped, errors = _run_solaredge(
            self._portfolio([q, g]), MagicMock(), "2026-07-10", None,
            None, None, True, MagicMock())
        assert processed == 1        # GTO2 ran
        assert skipped == 1          # QRO1 paused
        assert errors == 0           # quota is not an error
        assert common == ["row"]


class TestWeatherOnLatestRowOnly:
    """v81: current weather belongs only to each inverter's newest
    entry — historical rows re-stamped with fresh weather churned an
    update per row per poll (and misdated the weather itself)."""

    @patch("scripts.telemetry_5m.write_telemetry_rows",
           return_value={"inserted": 0, "updated": 0, "unchanged": 0})
    @patch("scripts.telemetry_5m.ensure_telemetry_tab")
    @patch("scripts.telemetry_5m.solaredge_row")
    @patch("scripts.telemetry_5m.fetch_solaredge_telemetry")
    def test_only_latest_gets_weather(self, fetch, rowmod, _ensure,
                                      _write):
        import datetime as dt

        from scripts.telemetry_5m import _process_solaredge_plant

        def tel(sn, minute):
            t = MagicMock()
            t.inverter_sn = sn
            t.timestamp_utc = dt.datetime(2026, 7, 10, 19, minute,
                                          tzinfo=dt.timezone.utc)
            return t

        fetch.return_value = [tel("A", 0), tel("A", 5), tel("A", 10),
                              tel("B", 5)]
        rowmod.EMPTY_WEATHER = "EMPTY"
        rowmod.build_plant_row.side_effect = lambda t, l, w: [w]
        rowmod.build_common_row.side_effect = lambda t, l, w: [w]

        inv_a, inv_b = MagicMock(), MagicMock()
        inv_a.inverter_sn, inv_a.inverter_label = "A", "Inv A"
        inv_b.inverter_sn, inv_b.inverter_label = "B", "Inv B"
        plant = MagicMock()
        plant.plant_key = "QRO1"

        common, errors = _process_solaredge_plant(
            plant, [inv_a, inv_b], MagicMock(), MagicMock(),
            "LIVE_WEATHER", True, MagicMock())

        assert errors == 0
        # A's 19:10 row and B's only row carry weather; A's older two
        # rows carry the empty snapshot
        weathers = [c[0] for c in common]
        assert weathers.count("LIVE_WEATHER") == 2
        assert weathers.count("EMPTY") == 2
