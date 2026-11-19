#!/bin/sh
# nginx entrypoint: serve HTTP-only (ACME challenge) until the Let's Encrypt cert exists,
# then switch to HTTPS automatically and reload whenever the cert is renewed.
set -eu
: "${DOMAIN:?DOMAIN must be set}"
LIVE="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"

render() {
  if [ -f "$LIVE" ]; then t=https.conf; else t=http-only.conf; fi
  sed "s#__DOMAIN__#$DOMAIN#g" "/etc/nginx/aegis/$t" > /etc/nginx/conf.d/default.conf
}

render
(
  last=""
  while :; do
    sleep 60
    cur=$(stat -L -c %Y "$LIVE" 2>/dev/null || echo none)
    if [ "$cur" != "$last" ]; then
      last=$cur
      render && nginx -t -q && nginx -s reload
    fi
  done
) &
exec nginx -g 'daemon off;'
