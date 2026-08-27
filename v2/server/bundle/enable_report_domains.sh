#!/bin/bash
# Run AFTER the DNS records exist:
#   A  report.argia.com.mx    -> 37.235.105.173
#   A  *.report.argia.com.mx  -> 37.235.105.173
# Gets one cert for all report names, enables the vhost, reloads nginx.
set -e
NAMES=(report.argia.com.mx financial.report.argia.com.mx setup.report.argia.com.mx \
       capex.report.argia.com.mx gto1.report.argia.com.mx mex1.report.argia.com.mx \
       mex2.report.argia.com.mx nl1.report.argia.com.mx slp1.report.argia.com.mx \
       slp2.report.argia.com.mx gto2.report.argia.com.mx qro1.report.argia.com.mx \
       nl2.report.argia.com.mx mex3.report.argia.com.mx)

echo "checking DNS..."
for n in "${NAMES[@]}"; do
    ip=$(dig +short "$n" A | tail -1)
    if [ "$ip" != "37.235.105.173" ]; then
        echo "STOP: $n resolves to '$ip' (need 37.235.105.173). Add DNS first."
        exit 1
    fi
done

D_ARGS=(); for n in "${NAMES[@]}"; do D_ARGS+=(-d "$n"); done
certbot certonly --webroot -w /www/hosting/monitoring.argia.com.mx/www \
    --cert-name report.argia.com.mx "${D_ARGS[@]}" \
    --non-interactive --agree-tos -m tzemelka@gmail.com

ln -sf /etc/nginx/sites-available/report.argia.com.mx.conf \
       /etc/nginx/sites-enabled/report.argia.com.mx.conf
nginx -t && systemctl reload nginx
echo "DONE — https://report.argia.com.mx is live."
