"""Tests for the daily report (report family, part 1)."""

from __future__ import annotations

from unittest.mock import MagicMock

from argia.core.alerts_state import AlertRecord, AlertState
from argia.core.drive import DriveClient
from argia.report.daily import (
    AMBER,
    GRAY,
    GREEN,
    RED,
    InverterDay,
    PlantDay,
    ReportData,
    allocate_theoretical,
    inverter_dot,
    plant_semaphore,
    render_html,
    svg_inverter_bars,
)


def _plant(pk="SLP1", pp=1.05, av=1.0, dc="full", **kw):
    return PlantDay(plant_key=pk, name=pk, energy_kwh=kw.get("e", 1000.0),
                    expected_kwh=kw.get("x", 950.0), production_pct=pp,
                    pr=0.8, availability=av, soiling=kw.get("soil"),
                    cloud_pct=40.0, data_class=dc,
                    status_note=kw.get("note", "On plan."),
                    inverters=kw.get("inv", []))


def _inv(sn="A", kwh=500.0, rated=124.0, t=50.0, faults=(), rel=None):
    return InverterDay(sn=sn, label=sn, kwh=kwh, rated_kw=rated,
                       tmax_c=t, faults=list(faults), rel=rel)


class TestPlantSemaphore:
    def test_green_amber_red_bands(self):
        assert plant_semaphore(_plant(pp=1.00), False, False) == GREEN
        assert plant_semaphore(_plant(pp=0.90), False, False) == AMBER
        assert plant_semaphore(_plant(pp=0.75), False, False) == RED

    def test_alert_presence_colors(self):
        assert plant_semaphore(_plant(pp=1.05), False, True) == AMBER
        assert plant_semaphore(_plant(pp=1.05), True, True) == RED

    def test_low_availability_is_red(self):
        assert plant_semaphore(_plant(pp=1.0, av=0.78), False, False) == RED

    def test_untrustworthy_day_is_gray(self):
        assert plant_semaphore(_plant(pp=None), False, False) == GRAY
        assert plant_semaphore(_plant(dc="partial"), False, False) == GRAY


class TestInverterDot:
    def test_bands(self):
        assert inverter_dot(_inv()) == GREEN
        assert inverter_dot(_inv(t=66.0)) == AMBER
        assert inverter_dot(_inv(t=75.4)) == RED           # real NL1 case
        assert inverter_dot(_inv(faults=["FT=302"])) == AMBER
        assert inverter_dot(_inv(rel=("WARNING", 0.75))) == AMBER
        assert inverter_dot(_inv(rel=("CRITICAL", 0.58))) == RED


class TestAllocateTheoretical:
    def test_nameplate_shares_sum_exactly(self):
        invs = [_inv("A", rated=124.0), _inv("B", rated=124.0),
                _inv("C", rated=60.0)]
        alloc = allocate_theoretical(4978.0, invs)
        assert round(sum(alloc.values()), 1) == 4978.0
        assert alloc["A"] == alloc["B"]                    # equal nameplates
        assert alloc["C"] < alloc["A"]                     # smaller unit

    def test_missing_inputs_empty(self):
        assert allocate_theoretical(None, [_inv()]) == {}
        assert allocate_theoretical(1000.0, []) == {}
        assert allocate_theoretical(
            1000.0, [_inv(rated=None), _inv(rated=0)]) == {}


class TestRenderSmoke:
    def _data(self):
        gto = _plant("GTO1", pp=0.7497, av=0.7833, e=3731.9, x=4977.98,
                     note="Below plan (75%) — inverter availability 78% "
                          "— see Alerts",
                     inv=[_inv("JFM7DXN00T", 899.5),
                          _inv("JFM7DXN013", 432.8, t=49.9,
                               faults=["FT=302"], rel=("CRITICAL", 0.584))])
        alert = AlertRecord(
            alert_id="ALT-20260703-005",
            alert_key="gto1:inv:jfm7dxn013:inverter_fault",
            plant_key="GTO1", inverter_sn="JFM7DXN013",
            metric="inverter_fault", severity="CRITICAL",
            state=AlertState.OPEN, opened_utc="2026-07-03T20:58:45+00:00",
            last_seen_utc="2026-07-03T21:06:50+00:00", resolved_utc="",
            value=4.0, threshold=None,
            message="GTO1 JFM7DXN013: vendor fault FT=302 (x4)",
            channels_sent="",
            explanation="Needs attention now. The inverter itself reported "
                        "a fault code.")
        return ReportData(date_iso="2026-07-02", plants=[gto],
                          alerts=[alert])

    def test_html_contains_every_layer(self):
        html = render_html(self._data())
        # header + rail
        assert "2026-07-02" in html and 'class="rail"' in html
        # status note verbatim (the words column, rendered)
        assert "inverter availability 78%" in html
        # alert with plain-language explanation
        assert "vendor fault FT=302 (x4)" in html
        assert "The inverter itself reported" in html
        # inverter table with flags and allocation
        assert "JFM7DXN013" in html and "FT=302" in html
        assert "peer median" in html                       # chart annotation
        # honesty footer
        assert "nameplate share" in html
        assert "treat % of plan as directional" in html

    def test_no_alerts_renders_placeholder(self):
        d = self._data()
        d.alerts.clear()
        assert "No open alerts." in render_html(d)

    def test_inverter_chart_skips_unrated(self):
        p = _plant(inv=[_inv(rated=None)])
        assert svg_inverter_bars(p) == ""


class TestDriveUpload:
    def _svc(self):
        return MagicMock()

    def test_upload_new_file_request_shape(self, tmp_path):
        f = tmp_path / "r.pdf"
        f.write_bytes(b"%PDF-1.4 test")
        svc = self._svc()
        svc.files().list().execute.return_value = {"files": []}
        svc.files().create().execute.return_value = {"id": "NEW"}
        d = DriveClient(service=svc)
        assert d.upload_file("FOLDER", "r.pdf", str(f),
                             "application/pdf") == "NEW"
        body = svc.files().create.call_args.kwargs["body"]
        assert body == {"name": "r.pdf", "parents": ["FOLDER"]}
        assert svc.files().create.call_args.kwargs["supportsAllDrives"]

    def test_upload_existing_updates_in_place(self, tmp_path):
        f = tmp_path / "r.pdf"
        f.write_bytes(b"%PDF-1.4 test")
        svc = self._svc()
        svc.files().list().execute.return_value = {
            "files": [{"id": "OLD"}]}
        d = DriveClient(service=svc)
        assert d.upload_file("FOLDER", "r.pdf", str(f),
                             "application/pdf") == "OLD"
        assert svc.files().update.call_args.kwargs["fileId"] == "OLD"
        svc.files().create.assert_not_called()

    def test_ensure_folder_reuses_existing(self):
        svc = self._svc()
        svc.files().list().execute.return_value = {
            "files": [{"id": "FID"}]}
        d = DriveClient(service=svc)
        assert d.ensure_folder("PARENT", "Reports") == "FID"
        svc.files().create.assert_not_called()
