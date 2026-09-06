# Server bundle — what runs on pio06, under version control

SOURCE OF TRUTH: this directory (git). What is deployed where:

| in git | deployed as |
|---|---|
| `*.py`, `*.sh`, `*.sql` here + `v2/server/monitoring_gen.py` | `/opt/argia/bundle/<name>` (the auth/setup/ask services and the generators run from there) |
| `argia-*.service`, `argia-*.timer` | `/etc/systemd/system/<name>` |
| `portal.argia.com.mx.conf`, `report.argia.com.mx.conf`, `portfolio.argia.com.mx.conf` | `/etc/nginx/sites-enabled/<name>` |
| `nginx-monitoring.argia.com.mx.conf` | `/etc/nginx/sites-enabled/monitoring.argia.com.mx.conf` |
| `nginx-argia_session.conf` | `/etc/nginx/snippets/argia_auth.conf` (the session-login snippet every vhost includes) |
| `nginx-argia_auth.conf` | not deployed — the pre-session (HTTP basic) snippet, kept as the documented rollback |
| `portal.argia.com.mx.http.conf` | not deployed — bootstrap vhost used once before the certificate existed |

The mapping is code: `scripts/drift_check.py` (`EXPLICIT`, `NOT_DEPLOYED`)
and a unit test fails when a bundle file has no rule. The `argia-drift`
timer runs it daily at 06:30 MX; any difference between git and the
deployed copies, any extra file in `/opt/argia/bundle`, a checkout that is
not `origin/main`, a portal answer that changed or a stale backup becomes
an admin-only WARNING on the 07:07 MX digest. Nothing is patched on the
server by hand any more — the 2026-08-27 due diligence flagged that as
the single uncontrolled failure point, and the harness keeps it closed.

Deploy after a change (as root on pio06):

    cd /root/argia_v2 && git fetch -q && git reset --hard -q origin/main
    cp v2/server/bundle/*.py v2/server/bundle/*.sh v2/server/bundle/*.sql /opt/argia/bundle/
    cp v2/server/monitoring_gen.py /opt/argia/bundle/
    cp v2/server/bundle/argia-*.service v2/server/bundle/argia-*.timer /etc/systemd/system/ && systemctl daemon-reload
    # nginx only when a vhost/snippet changed:
    cp v2/server/bundle/nginx-argia_session.conf /etc/nginx/snippets/argia_auth.conf && nginx -t && systemctl reload nginx
    systemctl restart argia-auth argia-setup argia-ask     # only when those apps changed
    /root/argia_v2/v2/pi/run_job.sh drift drift_check.py    # prove it: "status quo intact"

Server-local data never in git: the secret files in `/root` (`.argia_env`,
`.argia_mail`, `.argia_cfe_push`, `.argia_ask`, the vendor JSONs — all
0600), `/opt/argia/auth/` (users.db, sessions.db, session.key), the CFE
inbox. One-time migration CSVs and old shipping archives were moved to
`/root/argia_attic/` on 2026-09-07 (v218 house-cleaning).
