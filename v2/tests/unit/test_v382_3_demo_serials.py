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


def _reference_scrub(text, codes):
    """The v382.3 scrub (three searches per serial) - the reference v382.4 must equal."""
    import re
    body, keep = DG._mask(text)
    n = 0
    for sn in sorted(codes, key=lambda s: (-len(s), s)):
        e = re.escape(sn)
        for rx, new in ((r"\s*\(" + e + r"\)", ""), (r'\s*<span class="sn">' + e + r"</span>", ""),
                        (r"(?<![A-Za-z0-9])" + e + r"(?![A-Za-z0-9])", codes[sn])):
            body, c = re.subn(rx, new, body, flags=re.IGNORECASE)
            n += c
    return DG._unmask(body, keep), n


def test_one_pass_scrub_equals_the_reference_on_random_pages():
    """v382.4: one combined pattern (the demo run took 209 s with three searches per serial)."""
    import random
    rnd = random.Random(7)
    sers = [f"{rnd.choice(['ZZQ', 'ZZR', 'YYP'])}{rnd.randint(10**6, 10**9)}{rnd.choice('ABCDTU')}" for _ in range(60)]
    sers += ["ABCDEF7788", "ABCDEF77889"]                                 # one serial inside another
    codes = DG.serial_codes(sers)
    forms = ['Inverter {i} ({s})', 'Inverter {i} <span class="sn">{s}</span>', '<td>{s}</td>', '{{"sn":"{s}"}}',
             'inverter {l}', 'x{s}y', 'data:image/png;base64,AA{s}AA', 'id="inv-{s}"']
    for _ in range(300):
        page = "".join(rnd.choice(forms).format(i=rnd.randint(1, 9), s=s, l=s.lower()) + " lorem "
                       for s in (rnd.choice(sers) for _ in range(rnd.randint(1, 30))))
        ref = _reference_scrub(page, codes)
        assert DG.scrub_serials(page, codes) == ref
        left = DG._mask(ref[0])[0].upper()
        assert bool(DG.serial_leaks(ref[0], codes)) == any(s.upper() in left for s in codes)
    assert DG.scrub_serials("no serial here", {}) == ("no serial here", 0) and DG.serial_leaks("x", {}) == []
