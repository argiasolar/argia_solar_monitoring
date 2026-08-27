# CFE tariff fetcher (runs on the ARGIA Pi in Zapopan)

The CFE portal (app.cfe.mx) sits behind an Incapsula WAF that blocks
plain HTTP everywhere and headless browsers from datacenter IPs. The
Pi's residential Mexican IP + Playwright chromium passes (verified
2026-08-27). The Pi is therefore the fetch point; pio06 never talks
to CFE directly.

## Layout on the Pi (~/cfe/)

    venv/          python venv with playwright (+ its arm64 chromium;
                   the distro chromium/firefox builds SIGILL on Pi 4)
    cfe_scrape.py  scraper (copy of v2/pi/cfe/cfe_scrape.py)
    cfe_daily.sh   daily cron job (copy of v2/pi/cfe/cfe_daily.sh)
    divmap.json    division -> (estado, municipio) select values,
                   built once by `cfe_scrape.py --discover`
    state/         probe.csv, sent_YYYY-MM markers, heartbeat.json
    outbox/        monthly full CSVs (+ .manifest.json), 90 days
    logs/          daily_YYYYMMDD.log, 30 days

## Schedule (zemel's crontab)

    10 8 * * *  /home/zemel/cfe/cfe_daily.sh

Daily: probe (GDMTH, current month, 17 divisions) -> between day 3
and 27 fetch the full month once (10 tariffs x 17 divisions) and
push it -> push heartbeat.json. Everything lands in
/opt/argia/cfe_inbox on pio06 through an rrsync-jailed SSH key
(~/.ssh/argia_cfe_push; the key can ONLY write into that inbox).
pio06's argia-cfe-ingest.timer (09:15 MX) validates + loads CSVs
(cfe_load.py, source=cfe_scrape) and records status;
alert_mailer WARNs on stale heartbeat / failed probe / rejected
CSV / month not updated by day 10.

## Scope and honesty

- 10 business/industrial tariffs: PDBT GDBT GDMTO GDMTH DIST DIT
  RABT RAMT APBT APMT. DB1/DB2 (domestic) are not published on
  these pages and stay on the master_db_10 seed.
- The pages publish the INTEGRATED final tariffs. Component charges
  (TRANSMISION, CENACE, SERVICIOS CONEXOS NO MEM, FACTOR DE CARGA)
  are not published there; the scraper never touches them.
- cfe_load upsert: cfe_scrape overwrites seed; seed never overwrites
  cfe_scrape.

## Manual runs

    ./venv/bin/python cfe_scrape.py --discover
    ./venv/bin/python cfe_scrape.py --months 2026-08 --out /tmp/m.csv
    ./venv/bin/python cfe_scrape.py --months 2025-09:2026-08 \
        --out outbox/backfill.csv     # ~2-3 h, run under nohup
