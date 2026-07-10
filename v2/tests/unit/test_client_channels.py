"""v77 client channels.

The isolation contract: a client's report view contains exactly that
client's active plants — nothing internal, nothing from other clients —
and the internal show_daily_report flag never hides a plant from its
OWN client's report.
"""

from argia.core.config import PlantConfig, Portfolio, _client_channel


def _plant(pk, **kw):
    base = dict(
        plant_key=pk, customer="c", brand="growatt", site_id="1",
        kwp_dc=100.0, kwp_ac=90.0, lat=None, lon=None,
        expected_factor=0.8, pr_target=0.8, installation_date="",
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True)
    base.update(kw)
    return PlantConfig(**base)


def _pf(*plants):
    p = Portfolio()
    for pl in plants:
        p.plants[pl.plant_key] = pl
        p.inverters_by_plant[pl.plant_key] = []
    return p


class TestChannelToken:
    def test_normalization(self):
        assert _client_channel("  Acme Corp ", "P") == "acme_corp"
        assert _client_channel("PROLOGIS", "P") == "prologis"
        assert _client_channel("", "P") == ""
        assert _client_channel(None, "P") == ""


class TestChannelDiscovery:
    def test_distinct_sorted_nonblank_active_only(self):
        pf = _pf(_plant("GTO1"),                                # internal
                 _plant("MEX3", client_channel="acme"),
                 _plant("NL2", client_channel="acme"),
                 _plant("QRO1", client_channel="prologis"),
                 _plant("GTO2", client_channel="beta",
                        active=False))                          # inactive
        assert pf.client_channels() == ["acme", "prologis"]


class TestClientView:
    def test_view_contains_only_that_clients_active_plants(self):
        pf = _pf(_plant("GTO1"),
                 _plant("MEX3", client_channel="acme",
                        show_daily_report=False),
                 _plant("NL2", client_channel="acme", active=False),
                 _plant("QRO1", client_channel="prologis"))
        view = pf.for_client_channel("acme")
        assert sorted(view.plants) == ["MEX3"]
        assert "GTO1" not in view.plants and "QRO1" not in view.plants

    def test_internal_flag_never_hides_from_own_client(self):
        # the typical CAPEX config: hidden internally, visible to client
        pf = _pf(_plant("MEX3", client_channel="acme",
                        show_daily_report=False))
        view = pf.for_client_channel("acme")
        assert view.daily_report_plants()[0].plant_key == "MEX3"

    def test_original_portfolio_untouched(self):
        p = _plant("MEX3", client_channel="acme",
                   show_daily_report=False)
        pf = _pf(p)
        pf.for_client_channel("acme")
        assert pf.plants["MEX3"].show_daily_report is False

    def test_alert_scope_follows_the_view(self):
        # build_report_data scopes alerts by daily_report_plants() of
        # the portfolio it is GIVEN — for a client view that is exactly
        # the client's plants (v76 machinery reused, not duplicated)
        from argia.report.daily import scoped_alerts
        pf = _pf(_plant("MEX3", client_channel="acme",
                        show_daily_report=False),
                 _plant("GTO1"))
        view = pf.for_client_channel("acme")
        visible = {p.plant_key for p in view.daily_report_plants()}
        assert visible == {"MEX3"}
        assert scoped_alerts([], visible) == []
