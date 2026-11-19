"""v382.3 - demo.argia.com.mx shows no inverter serial (invented serials only).

The portal labels every inverter "Inverter 3 (JGMAE65009)" (v230). On 19 Nov 185 demo pages
carried real serials: a serial is not a customer name, so the name scan let them through.
The demo keeps the label, drops the serial, codes any other occurrence, and refuses to
publish while one is left."""
from __future__ import annotations

import sys
from pathlib import Path

from argia.alerts.naming import inverter_html, inverter_text

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
import demo_gen as DG                                      # noqa: E402

SER = ["ZZQ1234567", "ZZQ1234599", "XY9", "ABCDEF7788"]
CODES = DG.serial_codes(SER)


def test_codes_are_stable_and_skip_short_serials():
    assert CODES == {"ABCDEF7788": "SN-0001", "ZZQ1234567": "SN-0002", "ZZQ1234599": "SN-0003"}
    assert DG.serial_codes(list(reversed(SER))) == CODES


def test_labels_keep_the_name_and_lose_the_serial():
    for s in (inverter_text("Inverter 3", "ZZQ1234567"), inverter_html("Inverter 3", "ZZQ1234567")):
        out, n = DG.scrub_serials(f"<td>{s}</td>", CODES)
        assert out == "<td>Inverter 3</td>" and n == 1


def test_every_other_occurrence_gets_its_code():
    page = ('<td>ZZQ1234599</td><div id="inv-zzq1234567" data-sn="ABCDEF7788">inverter ZZQ1234599</div>'
            '{"sn":"ZZQ1234567"}')
    out, n = DG.scrub_serials(page, CODES)
    assert out == ('<td>SN-0003</td><div id="inv-SN-0002" data-sn="SN-0001">inverter SN-0003</div>'
                   '{"sn":"SN-0002"}')
    assert n == 5 and DG.serial_leaks(out, CODES) == []


def test_a_serial_glued_to_other_text_is_caught_by_the_gate():
    out, _ = DG.scrub_serials("xZZQ1234567y", CODES)            # not replaced (could be part of a word)
    assert [sn for sn, _ in DG.serial_leaks(out, CODES)] == ["ZZQ1234567"]   # but the gate refuses it


def test_data_uris_are_never_touched_nor_scanned():
    uri = "data:image/png;base64,AAAAZZQ1234567AAAA"
    out, n = DG.scrub_serials(f'<img src="{uri}">', CODES)
    assert uri in out and n == 0 and DG.serial_leaks(out, CODES) == []


def test_wired_into_the_publish_gate():
    src = (V2 / "server" / "bundle" / "demo_gen.py").read_text(encoding="utf-8")
    assert "codes = serial_codes(real_serials())" in src
    assert "text, n = scrub_serials(text, codes)" in src
    assert "+ serial_leaks(text, codes)" in src
    assert "SELECT inverter_sn FROM public.inverter;" in src           # every serial, active or not
