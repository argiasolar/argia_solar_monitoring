"""v382.5 - the demo's real-name scrub and leak gate skip the names that are not on the page.

Same result as before, faster: the demo run spent about 100 of its 125 s running all 48 name
patterns over all 441 pages. The v279 functions are kept here as the reference."""
from __future__ import annotations

import random
import re
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
import demo_gen as DG                                      # noqa: E402

REAL = {"GTO1": ["TAIGENE PPA roof (Leon, GTO)", "TAIGENE"], "MEX1": ["SAG PPA roof (CDMX, MEX)", "SAG"],
        "NL1": ["PLASTIC OMNIUM PPA land (Monterrey, NL)", "plastic omnium"],
        "GTO2": ["HIRSCHMANN-MEXICO CAPEX roof (San Miguel, GTO)", "HIRSCHMANN"], "LOAX1": ["GRUPO MODELO"],
        "SLP2": ["HOLIDAY INN EXPRESS"], "SLP1": ["QUIMICA COYOACAN PPA roof"], "QRO1": ["Tenería Panamericana CAPEX"]}
DEMO = {k: f"ARGIA SOLAR {i}" for i, k in enumerate(REAL, 1)}
RULES = DG.build_rules(REAL)


def ref_scrub(text, rules, demo_names):
    body, keep = DG._mask(text)
    n = 0
    for rx, k, tok in rules:
        dn = demo_names.get(k, 'ARGIA SOLAR')

        def rep(m, dn=dn):
            s = m.group(0)
            return DG.slugify(dn) if (s == s.lower() and ' ' not in s) else dn
        body, c = rx.subn(rep, body)
        n += c
    return DG._unmask(body, keep), n


def ref_leaks(text, rules):
    body, _ = DG._mask(text)
    out = []
    for rx, _k, tok in rules:
        for m in rx.finditer(body):
            a, b = max(0, m.start() - 40), min(len(body), m.end() + 40)
            out.append((tok, body[a:b].replace('\n', ' ')))
    return out


WORDS = ["TAIGENE", "taigene", "Taigene", "SAG", "sag", "usage", "SAG_INV1_MPPT2_C1", "PLASTIC OMNIUM", "plastic-omnium",
         "Plastic_Omnium", "OMNIUM", "HIRSCHMANN-MEXICO", "hirschmann", "GRUPO MODELO", "modelo de clima",
         "HOLIDAY INN EXPRESS", "holiday-inn-express", "QUIMICA COYOACAN", "quimica-coyoacan", "Coyoacán", "Tenería",
         "TENERIA", "TAıGENE", "İnn", "Kelvin", "data:image/png;base64,QUFUQUlHRU5FQUE=", "<b>SAG</b>",
         "ARGIA SOLAR 3", "xTAIGENEx", "taigene2", "Leon, GTO", "lorem"]


def test_identical_to_the_reference_on_random_pages():
    rnd = random.Random(11)
    for _ in range(1500):
        page = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 50)))
        assert DG.scrub(page, RULES, DEMO) == ref_scrub(page, RULES, DEMO)
        assert DG.leaks(page, RULES) == ref_leaks(page, RULES)
        after = ref_scrub(page, RULES, DEMO)[0]
        assert DG.leaks(after, RULES) == ref_leaks(after, RULES)


def test_the_precheck_never_hides_a_case_insensitive_match():
    """The pre-check is a necessary condition only if every character the regex engine folds onto a
    letter of a name also folds onto it in _fold. Over all of Unicode the one exception to plain
    casefold was the dotless i (U+0131); these are the special cases, checked here."""
    for x, c in (("i", "ı"), ("I", "ı"), ("i", "İ"), ("k", "K"), ("s", "ſ"), ("e", "É"), ("n", "Ñ")):
        if re.fullmatch(re.escape(x), c, re.I):
            assert DG._fold(x) in DG._fold(c), (x, hex(ord(c)))
    assert DG.scrub("TAıGENE roof", RULES, DEMO)[0] == ref_scrub("TAıGENE roof", RULES, DEMO)[0]


def test_a_name_not_on_the_page_costs_no_regex():
    calls = []

    class Spy:
        def __init__(self, rx):
            self.rx = rx

        def subn(self, *a):
            calls.append(1)
            return self.rx.subn(*a)

        def finditer(self, *a):
            calls.append(1)
            return self.rx.finditer(*a)
    spied = [(Spy(rx), k, tok) for rx, k, tok in RULES]
    DG.scrub("nothing to see here", spied, DEMO)
    DG.leaks("nothing to see here", spied)
    assert calls == []
    DG.scrub("TAIGENE", spied, DEMO)
    assert len(calls) >= 1
