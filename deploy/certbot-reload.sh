#!/bin/sh
set -eu
# Renew only this host's main nginx service, never DataLens's separate master.
if [ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/puls-mp.ru ]; then
    /usr/sbin/nginx -t
    /usr/bin/systemctl kill --kill-whom=main --signal=HUP nginx.service
fi
