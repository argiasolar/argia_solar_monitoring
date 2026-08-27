#!/bin/bash
# Deploy ARGIA reporting v3: restyled site + landing + per-report ACL + setup app.
# Idempotent. No credentials are ever echoed.
set -e
cd /opt/argia/bundle
tar xzf srv_bundle_v3.tgz
echo "== extracted"

export DEBIAN_FRONTEND=noninteractive
dpkg -s python3-flask >/dev/null 2>&1 || apt-get install -y -q python3-flask >/dev/null
echo "== flask $(dpkg -s python3-flask | grep ^Version)"

mkdir -p /opt/argia/auth /etc/nginx/snippets
cp nginx_argia_auth.conf /etc/nginx/snippets/argia_auth.conf
cp report.argia.com.mx.conf /etc/nginx/sites-available/report.argia.com.mx.conf
cp enable_report_domains.sh /opt/argia/enable_report_domains.sh
chmod +x /opt/argia/enable_report_domains.sh

# patch the domains_conf hook: old single-gate auth -> per-area snippet include
python3 - <<'PYEOF'
p = '/etc/nginx/sites-available/domains_conf/monitoring.argia.com.mx.conf'
s = open(p).read()
inc = 'include /etc/nginx/snippets/argia_auth.conf;\n'
if 'snippets/argia_auth.conf' not in s:
    i = s.find('# --- ARGIA dashboard')
    if i >= 0:
        s = s[:i] + inc
    else:
        s += '\n' + inc
    open(p, 'w').write(s)
    print('hook patched')
else:
    print('hook already patched')
PYEOF

cp argia-setup.service /etc/systemd/system/argia-setup.service
systemctl daemon-reload
systemctl enable --now argia-setup.service >/dev/null 2>&1
sleep 2
systemctl restart argia-setup.service
sleep 2
curl -s -o /dev/null -w 'setup app healthz=%{http_code}\n' http://127.0.0.1:8511/healthz
ls -l /opt/argia/auth/ | head -20

# regenerate site (new tree; drop the old layout first)
W=/www/hosting/monitoring.argia.com.mx/www
rm -rf "$W/ppa" "$W/capex" "$W/financial"
python3 report_gen.py "$W"

nginx -t
systemctl reload nginx
echo "== nginx reloaded"

# ---- auth matrix verification (passwords never printed) ----
PW=$(grep '^dashboard_web=' /root/.credentials | cut -d= -f2)
B=https://monitoring.argia.com.mx
chk () { echo "$1 $(curl -s -o /dev/null -w '%{http_code}' "${@:2}")"; }
chk "noauth /           " "$B/"
chk "noauth /financial/ " "$B/financial/"
chk "noauth /setup/     " "$B/setup/"
chk "argia  /           " -u "argia:$PW" "$B/"
chk "argia  /financial/ " -u "argia:$PW" "$B/financial/"
chk "argia  /gto1/      " -u "argia:$PW" "$B/gto1/"
chk "argia  /capex/     " -u "argia:$PW" "$B/capex/"
chk "argia  /mex3/      " -u "argia:$PW" "$B/mex3/"
chk "argia  /setup/     " -u "argia:$PW" "$B/setup/"

# probe user: financial only -> financial 200, plant 401
TOK=$(curl -s http://127.0.0.1:8511/ | grep -oE 'name="csrf" value="[0-9a-f]+"' | head -1 | grep -oE '[0-9a-f]{32}')
curl -s -o /dev/null -X POST http://127.0.0.1:8511/add \
  --data "csrf=$TOK&username=probe&password=Probe123x&level=custom&reports=financial"
chk "probe  /financial/ " -u "probe:Probe123x" "$B/financial/"
chk "probe  /gto1/      " -u "probe:Probe123x" "$B/gto1/"
chk "probe  /setup/     " -u "probe:Probe123x" "$B/setup/"
chk "probe  /           " -u "probe:Probe123x" "$B/"
curl -s -o /dev/null -X POST http://127.0.0.1:8511/delete --data "csrf=$TOK&username=probe"
chk "probe deleted, financial again" -u "probe:Probe123x" "$B/financial/"

echo "=== DEPLOY3 DONE ==="
