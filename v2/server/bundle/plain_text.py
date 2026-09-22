"""v262 - the last line of defence against the em dash.

Tomasz (2026-09-22): no em dash anywhere - portal, report, mail. v261
swept every template and constant; this module catches what the
templates cannot: text that comes out of the database (alert messages
opened before v261, notes typed into the PMO sheets, ticket comments)
and text produced by json.dumps as the \\u escape. Every writer of a
page, a mail or a PDF passes its output through plain() once.

Pure. argia/core/text.py carries the same function for the
package side (the bundle must stay importable on its own); a unit test
keeps the two identical.
"""
import re

EM = chr(0x2014)
_ESC = "\\" + "u2014"                 # the JSON / Python escape, spelled so no file trips the guard
_ENT = ("&" + "mdash;", "&#" + "8212;")
_TIGHT = re.compile("(?<=\\S)" + EM + "(?=\\S)")   # word+dash+word -> word - word


def plain(s):
    """Return ``s`` with every em dash replaced: ' - ' where it separated
    clauses (also when it was tight between two words), '-' where it stood
    alone (the 'no value' mark). Anything
    that is not a str comes back untouched."""
    if not isinstance(s, str):
        return s
    if _ESC in s:
        s = s.replace(_ESC, EM)
    for ent in _ENT:
        if ent in s:
            s = s.replace(ent, EM)
    if EM not in s:
        return s
    s = s.replace(" " + EM + " ", " - ")
    s = _TIGHT.sub(" - ", s)
    return s.replace(EM, "-")
