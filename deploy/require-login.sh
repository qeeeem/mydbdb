#!/usr/bin/env bash
# One-time migration for an existing public-browsing mydbdb installation.
set -Eeuo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as the server administrator.' >&2; exit 1; }
config=/etc/nginx/conf.d/mydbdb.conf
backup=/etc/mydbdb/nginx.before-private.conf
test -s "$config"
test -s /etc/mydbdb/htpasswd
grep -Fq 'auth_basic "mydbdb";' "$config"
nginx -t
if grep -Eq '^[[:space:]]*auth_basic off;[[:space:]]*$' "$config"; then
  [[ $(grep -Ec '^[[:space:]]*auth_basic off;[[:space:]]*$' "$config") == 1 ]] || {
    echo 'Unexpected configuration; no changes made.' >&2; exit 1;
  }
  test ! -e "$backup" || { echo "Backup already exists: $backup; inspect before retrying." >&2; exit 1; }
  cp -p -- "$config" "$backup"
  temporary=$(mktemp /etc/nginx/conf.d/mydbdb-private.XXXXXX)
  trap 'rm -f -- "$temporary"' EXIT
  sed '/^[[:space:]]*auth_basic off;[[:space:]]*$/d' "$config" > "$temporary"
  chown --reference="$config" "$temporary"
  chmod --reference="$config" "$temporary"
  mv -f -- "$temporary" "$config"
  if ! nginx -t || ! systemctl reload nginx; then
    cp -p -- "$backup" "$config"
    nginx -t
    echo 'Change failed; previous configuration restored. Inspect Nginx before retrying.' >&2
    exit 1
  fi
fi
# shellcheck source=/dev/null
source /etc/mydbdb/deploy.conf
for path in / /index.html /add.js /media_data.json /api/data; do
  status=$(curl --silent --show-error --connect-timeout 5 --max-time 10 \
    --connect-to "8.148.157.132:$PUBLIC_PORT:127.0.0.1:$PUBLIC_PORT" \
    -o /dev/null -w '%{http_code}' "https://8.148.157.132:$PUBLIC_PORT$path")
  [[ "$status" == 401 ]] || { echo "Password protection check failed: $path ($status)" >&2; exit 1; }
done
echo "Password required: https://8.148.157.132:$PUBLIC_PORT (user: owner; existing password)."
