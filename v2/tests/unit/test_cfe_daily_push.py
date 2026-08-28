"""cfe_daily.sh push path — three bugs found live on 2026-08-28.

The Pi scraped CFE fine every morning and delivered nothing for a day
and a half.  Three separate faults, each silent on its own:

  1. INBOX was "argia-cfe@37.235.105.173".  rsync only reads the
     ~/.ssh/config Host block when the target carries NO explicit
     user@, so the alias (and with it the IdentityFile for the
     rrsync-restricted key) was skipped and sshd saw a literal user
     named "argia-cfe": "Permission denied (publickey)" every run.
  2. push() ended in `... || echo "PUSH FAILED"`, which exits 0.  The
     caller chains `push "$FULL" && ... && touch "$MARK"`, so the
     month was marked as delivered while nothing had arrived — and the
     marker then suppressed every later attempt.
  3. A failed push meant the next run re-scraped from scratch (two
     hours) instead of re-sending the CSV already sitting in outbox.

These are shell-level asserts, not behaviour tests: the job needs a Pi,
a WAF-reachable CFE portal and an ssh jail.  They pin the exact
spellings that broke.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SH = Path(__file__).resolve().parents[2] / "pi" / "cfe" / "cfe_daily.sh"
SRC = SH.read_text(encoding="utf-8")


def body_of(func):
    """The text of a shell function, brace to closing brace."""
    i = SRC.index(func + "() {")
    depth, j = 0, i
    while True:
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
        j += 1


class TestInboxTarget:
    def test_inbox_is_a_bare_ssh_alias(self):
        m = re.search(r'^INBOX="([^"]*)"', SRC, re.M)
        assert m, "INBOX assignment not found"
        assert m.group(1) == "argia-cfe"

    def test_inbox_carries_no_user_or_host(self):
        """@ or a dotted host here means ssh_config is bypassed."""
        m = re.search(r'^INBOX="([^"]*)"', SRC, re.M)
        assert "@" not in m.group(1)
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", m.group(1))

    def test_the_broken_spelling_is_gone_from_every_command(self):
        """Comments may quote it — no command may use it."""
        code = [ln for ln in SRC.splitlines()
                if not ln.lstrip().startswith("#")]
        assert "argia-cfe@" not in "\n".join(code)


class TestPushReportsFailure:
    def test_push_returns_nonzero_when_rsync_fails(self):
        b = body_of("push")
        assert "return 1" in b, "push() must propagate rsync's failure"
        assert "return 0" in b

    def test_push_does_not_end_in_a_bare_or_echo(self):
        """`cmd || echo ...` is the exact shape that always exits 0."""
        b = body_of("push")
        assert not re.search(r'\|\|\s*echo[^\n]*\n\}', b)

    def test_the_month_marker_is_gated_on_the_push(self):
        assert re.search(r'push "\$FULL".*&&.*touch "\$MARK"', SRC)
        # and never touched on its own line, unconditionally
        for line in SRC.splitlines():
            s = line.strip()
            if s.startswith("touch "):
                assert "&&" in line, f"unconditional marker: {s}"


class TestRetryWithoutRescraping:
    def test_an_existing_csv_is_re_pushed_not_re_scraped(self):
        i = SRC.index("monthly full fetch")
        block = SRC[i:i + 1200]
        assert '[ -s "$FULL" ]' in block, "no reuse of the scraped CSV"
        j = block.index('[ -s "$FULL" ]')
        k = block.index("cfe_scrape.py", j)
        # the re-push must come before the re-scrape in that branch
        assert block.index('push "$FULL"', j) < k

    def test_the_retry_still_respects_the_sent_marker(self):
        i = SRC.index("monthly full fetch")
        block = SRC[i:i + 400]
        assert '[ ! -f "$MARK" ]' in block


class TestStillValidShell:
    @pytest.mark.skipif(shutil.which("bash") is None,
                        reason="bash not on PATH")
    def test_it_parses(self):
        subprocess.run(["bash", "-n", str(SH)], check=True)

    def test_it_keeps_failing_loudly_rather_than_silently(self):
        assert "set -u" in SRC
