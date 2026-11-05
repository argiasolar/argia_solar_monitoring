"""v357 - "from the building view there should be icon for each file related to project so I can open it and download if
needed, even if someone doesnt have access to our google drive" (Tomasz, 5 Nov 2026).

The latest version of each project document (audit, lighting study, proforma, work order, Prologis PO, handover) is copied
to the server (lighting/docs/), listed in lighting/docs.json, and served by the portal to signed-in users only: PDFs and
pictures open in the browser, ?dl=1 downloads, every access is in the audit log. Paths outside docs/ are refused.

All data invented (public repo).
"""
from __future__ import annotations

import json
import os
import time

import pytest

from tests.unit.test_v345_lighting import env, sign_in   # noqa: F401  (fixture re-used)


def _docs(tmp, files, raw=None):
    base = tmp / "lighting" / "docs" / "1_DELIVERED" / "Mexico City" / "MXC09901 Parque Uno [P001]"
    base.mkdir(parents=True, exist_ok=True)
    (base / "MXC09901_1-AUDIT_20220101_Acme.pdf").write_bytes(b"%PDF-1.4 audit")
    (base / "MXC09901_4-WO_20220301_Acme.xlsx").write_bytes(b"PK\x03\x04 wo")
    (tmp / "lighting" / "secret.txt").write_text("not a document")
    f = tmp / "lighting" / "docs.json"
    f.write_text(raw if raw is not None else json.dumps({"schema": 1, "files": files}), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))


REL = "1_DELIVERED/Mexico City/MXC09901 Parque Uno [P001]/"
FILES = [{"project": 1, "type": "audit", "date": "2022-01-01", "name": "MXC09901_1-AUDIT_20220101_Acme.pdf",
          "path": REL + "MXC09901_1-AUDIT_20220101_Acme.pdf", "size": 2_400_000},
         {"project": 1, "type": "wo", "date": "2022-03-01", "name": "MXC09901_4-WO_20220301_Acme.xlsx",
          "path": REL + "MXC09901_4-WO_20220301_Acme.xlsx", "size": 12_000}]


class TestDocuments:
    def test_project_page_lists_the_documents(self, env):
        mod, pws, tmp = env
        _docs(tmp, FILES)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert "Project documents" in h and "No Google Drive access needed." in h
        assert '<a class="docf" href="/lighting/p/1/doc/0"' in h and '<a class="docf" href="/lighting/p/1/doc/1"' in h
        assert "2022-01-01 · 2.4 MB · PDF" in h and "<b>Work order</b>" in h
        assert "Project documents" not in cl.get("/lighting/p/2").get_data(as_text=True)

    def test_open_inline_download_and_audit(self, env):
        mod, pws, tmp = env
        _docs(tmp, FILES)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        r = cl.get("/lighting/p/1/doc/0")
        assert r.status_code == 200 and r.data == b"%PDF-1.4 audit" and r.mimetype == "application/pdf"
        assert "attachment" not in r.headers.get("Content-Disposition", "") and r.headers["Cache-Control"] == "private, no-store"
        r = cl.get("/lighting/p/1/doc/0?dl=1")
        assert "attachment" in r.headers["Content-Disposition"] and "MXC09901_1-AUDIT_20220101_Acme.pdf" in r.headers["Content-Disposition"]
        r = cl.get("/lighting/p/1/doc/1")
        assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]                 # not a PDF: download
        from argia.prologis import store as S
        c = S.connect(os.environ["ARGIA_PL_DB"])
        assert c.execute("SELECT COUNT(*) FROM audit WHERE action='lighting_doc' AND target='P1/audit'").fetchone()[0] == 2
        c.close()

    def test_unknown_index_and_signed_out_are_refused(self, env):
        mod, pws, tmp = env
        _docs(tmp, FILES)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert cl.get("/lighting/p/1/doc/2").status_code == 404 and cl.get("/lighting/p/2/doc/0").status_code == 404
        assert cl.get("/lighting/p/999/doc/0").status_code == 404
        anon = mod.app.test_client()
        assert anon.get("/lighting/p/1/doc/0").status_code in (302, 401, 403)

    @pytest.mark.parametrize("bad", ["../secret.txt", "/etc/passwd", "a/../../secret.txt", "C:\\\\x.pdf"])
    def test_a_path_outside_docs_refuses_the_list(self, env, bad):
        mod, pws, tmp = env
        _docs(tmp, FILES + [{"project": 1, "type": "po", "path": bad, "name": "x.pdf", "size": 1}])
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert "Project documents" not in cl.get("/lighting/p/1").get_data(as_text=True)                 # whole list refused
        assert cl.get("/lighting/p/1/doc/2").status_code == 404

    def test_a_broken_list_keeps_the_last_good_one(self, env):
        mod, pws, tmp = env
        _docs(tmp, FILES)
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert cl.get("/lighting/p/1/doc/0").status_code == 200
        _docs(tmp, FILES, raw="{broken")
        assert cl.get("/lighting/p/1/doc/0").status_code == 200
