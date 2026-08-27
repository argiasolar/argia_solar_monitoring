# Server bundle — /opt/argia/bundle on pio06, under version control

These files run report.argia.com.mx (report_gen, dashboard_gen, landing),
the /setup/ admin app (setup_app), client pages (argia_client_logos),
CFE tariffs (cfe_load, cfe_page_gen), the PG loaders, the KPI->PG sync
(sync_kpi, argia-sync.timer), nginx access control, and their systemd
units. They were previously patched in place on the server ONLY — the
2026-08-27 due diligence flagged that as the single uncontrolled
failure point (roadmap P0-3); this directory is the fix.

SOURCE OF TRUTH: this directory (git). Deploy after a change:

    cd /root/argia_v2 && git pull
    cp /root/argia_v2/v2/server/bundle/*.py /opt/argia/bundle/
    cp /root/argia_v2/v2/server/monitoring_gen.py /opt/argia/bundle/
    systemctl restart argia-setup.service      # only when setup_app changed
    systemctl start argia-dashboard.service    # regenerate reports

NOT in git (server-local data, stays in /opt/argia/bundle):
cfe_masterdb.csv and the one-time migration CSVs (plants, inverters,
loans, loan_schedule, contract_monthly, daily_v1/v2) plus historical
srv_*.tgz shipping archives (candidates for deletion).

monitoring_gen.py deliberately lives one level up (v2/server/) — it was
version-controlled from the start and deploys the same way.
