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
