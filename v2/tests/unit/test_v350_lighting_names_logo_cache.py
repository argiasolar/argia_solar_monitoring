"""v350 - after Tomasz's second look at the logos (5 Nov 2026).

* "remove the ugly green background beneath the logos like Armor IIMAK, Belcorp or Syncreon" - the files
  had been replaced, but the browser kept the old ones for a day (Cache-Control max-age=86400). The logo
  URL now carries a version from the file, so a replaced logo shows at once.
* "always name of company with capital letter" - names are capitalised when the data is read, so a lower-
  case brand ("syncreon") can never reach a page.

All data invented (public repo).
"""
from __future__ import annotations

import copy
import json
import os
import re
import time

from tests.unit.test_v345_lighting import DATA, _png, env, sign_in   # noqa: F401  (fixture re-used)
from argia.prologis import lighting as LT

SRC = re.compile(r'src="/lighting/logo/acme\.png\?v=([0-9a-f]+)"')


class TestNames:
    def test_cap_name(self):
        assert LT.cap_name("syncreon (DP World)") == "Syncreon (DP World)"
        assert LT.cap_name("nVent") == "NVent" and LT.cap_name("DHL") == "DHL" and LT.cap_name("3M") == "3M"
        assert LT.cap_name("") == "" and LT.cap_name(None) is None

    def test_names_are_capitalised_when_read(self):
        d = copy.deepcopy(DATA)
        d["customers"]["acme"]["name"] = "acme logistics"
        d["projects"][0]["customer"] = "acme logistics"
        ds = LT.parse(d)
        assert ds.customers["acme"].name == "Acme logistics"
        assert ds.projects[0].customer == "Acme logistics"
        assert all(p.customer[:1].isupper() for p in ds.projects if p.customer and p.customer[:1].isalpha())

    def test_no_lower_case_company_name_on_the_customer_page(self, env):
        mod, pws, tmp = env
        d = copy.deepcopy(DATA)
        d["customers"]["bravo"]["name"] = "bravo foods"
        (tmp / "lighting" / "lighting.json").write_text(json.dumps(d), encoding="utf-8")
        os.utime(tmp / "lighting" / "lighting.json", (time.time() + 5, time.time() + 5))
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
        shown = re.sub(r' data-[qnp]="[^"]*"', "", h)                     # the search key is lower case on purpose
        assert "<h3>Bravo foods</h3>" in shown and "bravo foods" not in shown


class TestLogoCache:
    def test_every_logo_url_is_versioned(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        for path in ("/lighting/", "/lighting/customers/", "/lighting/buildings/", "/lighting/p/1"):
            h = cl.get(path).get_data(as_text=True)
            assert not re.search(r'/lighting/logo/[a-z0-9-]+\.png"', h), path      # never without ?v=
        assert SRC.search(cl.get("/lighting/customers/").get_data(as_text=True))

    def test_a_replaced_logo_gets_a_new_url(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        v1 = SRC.search(cl.get("/lighting/customers/").get_data(as_text=True)).group(1)
        f = tmp / "lighting" / "logos" / "acme.png"
        f.write_bytes(_png(8, 3))
        os.utime(f, (time.time() + 60, time.time() + 60))
        v2 = SRC.search(cl.get("/lighting/customers/").get_data(as_text=True)).group(1)
        assert v1 != v2
        assert cl.get(f"/lighting/logo/acme.png?v={v2}").status_code == 200     # the versioned URL serves the file
