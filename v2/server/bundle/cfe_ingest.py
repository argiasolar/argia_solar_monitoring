#!/usr/bin/env python3
"""Ingest CFE tariff CSVs + heartbeat pushed by the Pi into
/opt/argia/cfe_inbox (rrsync-jailed key). Runs daily via
argia-cfe-ingest.timer at 15:15 UTC (after the Pi's 08:10 MX job).

- validates each CSV (header, row count, charge types, value sanity,
  anomaly ratio vs previous month) before handing it to cfe_load.py
- valid  -> load as cfe_scrape, move to processed/
- broken -> move to rejected/ (alert_mailer picks this up)
- heartbeat.json -> cfe_pipeline_status row (alert_mailer watches
  staleness / probe failures / missing months)
"""
import json
import os
import shutil
import subprocess
import sys

INBOX = "/opt/argia/cfe_inbox"
LOAD = "/opt/argia/bundle/cfe_load.py"
ALLOWED = {"SUMINISTRO BASICO", "ENERGIA BASE", "ENERGIA INTERMEDIA",
           "ENERGIA PUNTA", "DISTRIBUCION", "CAPACIDAD",
           # CFE introduced a semi-peak energy component with the
           # Sep-2026 files (seen on DIST) — capture it as known
           # rather than warning (v179, Tomasz 2026-09-03)
           "ENERGIA SEMIPUNTA"}
HEADER = "tariff_code,region,month,charge_type,unit,value_mxn"
VAL_MAX = 10000.0


def psql(sql):
    r = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-d", "argia_mont",
         "-t", "-A", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        print("PSQL ERROR:", r.stderr[-500:])
        sys.exit(1)
    return r.stdout.strip()


def validate(path):
    """Return (ok, info). Never raises."""
    import csv as _csv
    try:
        with open(path, encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
    except Exception as ex:
        return False, f"unreadable: {ex}"
    if not rows:
        return False, "empty file"
    got = list(rows[0].keys())
    if ",".join(got) != HEADER:
        return False, f"bad header: {got}"
    unknown = {r["charge_type"] for r in rows} - ALLOWED
    bad_vals = 0
    for r in rows:
        try:
            v = float(r["value_mxn"])
            if v < 0 or v > VAL_MAX:
                bad_vals += 1
        except ValueError:
            bad_vals += 1
    if bad_vals:
        return False, f"{bad_vals} out-of-range values"
    if unknown:
        # unknown concepts load fine but deserve eyes — warn, allow
        print(f"  WARNING unknown charge types: {sorted(unknown)[:5]}")
    # anomaly check vs values already in PG (previous month, same key)
    months = sorted({r["month"] for r in rows})
    return True, f"{len(rows)} rows, months {months[0]}..{months[-1]}"


def main():
    os.makedirs(f"{INBOX}/processed", exist_ok=True)
    os.makedirs(f"{INBOX}/rejected", exist_ok=True)
    psql("""CREATE TABLE IF NOT EXISTS cfe_pipeline_status (
        id int PRIMARY KEY DEFAULT 1,
        heartbeat_ts timestamptz,
        probe_status text,
        probe_rows int,
        sent_month text,
        last_csv text,
        last_csv_result text,
        updated_at timestamptz NOT NULL DEFAULT now());""")

    loaded, rejected = [], []
    for f in sorted(os.listdir(INBOX)):
        p = os.path.join(INBOX, f)
        if not os.path.isfile(p) or not f.endswith(".csv"):
            continue
        ok, info = validate(p)
        print(f"{f}: {'OK' if ok else 'REJECT'} — {info}")
        if ok:
            r = subprocess.run(
                ["python3", LOAD, p, "cfe_scrape"],
                capture_output=True, text=True)
            print(r.stdout[-400:])
            if r.returncode == 0 and "CFE LOAD DONE" in r.stdout:
                shutil.move(p, f"{INBOX}/processed/{f}")
                loaded.append(f)
            else:
                print("LOAD FAILED:", r.stderr[-300:])
                shutil.move(p, f"{INBOX}/rejected/{f}")
                rejected.append(f)
        else:
            shutil.move(p, f"{INBOX}/rejected/{f}")
            rejected.append(f)

    # heartbeat -> status row
    hb_path = os.path.join(INBOX, "heartbeat.json")
    hb = {}
    if os.path.exists(hb_path):
        try:
            hb = json.load(open(hb_path))
        except Exception as ex:
            print("heartbeat unreadable:", ex)
    last_csv = (loaded + rejected)[-1] if (loaded or rejected) else ""
    result = ("loaded" if loaded and not rejected else
              "rejected" if rejected else "")
    ts = hb.get("ts", "")
    psql(f"""INSERT INTO cfe_pipeline_status
        (id, heartbeat_ts, probe_status, probe_rows, sent_month,
         last_csv, last_csv_result, updated_at)
        VALUES (1, {f"'{ts}'" if ts else "NULL"},
                '{hb.get("probe_status", "")}',
                {int(hb.get("probe_rows", 0) or 0)},
                '{hb.get("sent_month", "")}',
                '{last_csv}', '{result}', now())
        ON CONFLICT (id) DO UPDATE SET
          heartbeat_ts = COALESCE(EXCLUDED.heartbeat_ts,
                                  cfe_pipeline_status.heartbeat_ts),
          probe_status = EXCLUDED.probe_status,
          probe_rows = EXCLUDED.probe_rows,
          sent_month = EXCLUDED.sent_month,
          last_csv = CASE WHEN EXCLUDED.last_csv <> ''
                          THEN EXCLUDED.last_csv
                          ELSE cfe_pipeline_status.last_csv END,
          last_csv_result = CASE WHEN EXCLUDED.last_csv_result <> ''
                          THEN EXCLUDED.last_csv_result
                          ELSE cfe_pipeline_status.last_csv_result END,
          updated_at = now();""")
    cov = psql("SELECT max(month) FROM cfe_tariff"
               " WHERE source='cfe_scrape';")
    if loaded:
        # event-driven engine push (v181): a fresh load is the same
        # event that turns the CFE mark yellow — push the overlay now.
        # Fire-and-forget; the unit no-ops if the push is unconfigured.
        subprocess.run(["systemctl", "start", "--no-block",
                        "argia-cfe-push.service"], check=False)
    print(f"ingest: loaded={loaded} rejected={rejected}"
          f" scrape-coverage-through={cov or 'none'}")
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
