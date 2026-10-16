#!/usr/bin/env bash
# One-time installation as root. Existing sites, ports and their files are left intact.
set -Eeuo pipefail
umask 077
port="${1:-18443}"
backend="${2:-18081}"
python_bin="${3:-/usr/bin/python3}"
[[ "$python_bin" =~ ^/[A-Za-z0-9_./-]+$ && -x "$python_bin" ]]
[[ "$port" =~ ^[0-9]{4,5}$ && "$backend" =~ ^[0-9]{4,5}$ ]]
(( port >= 1024 && port <= 65535 && backend >= 1024 && backend <= 65535 && port != backend ))
[[ $(id -u) == 0 ]] || { echo 'Run in the server administrator terminal as root.' >&2; exit 1; }
for command in nginx curl openssl systemctl visudo ss flock; do command -v "$command" >/dev/null; done
id ledger-deploy >/dev/null
"$python_bin" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required; pass its absolute path as argument 3"'
nginx -t
systemctl is-active --quiet nginx
cert=/etc/nginx/ssl/8.148.157.132/fullchain.pem
key=/etc/nginx/ssl/8.148.157.132/privkey.pem
test -s "$cert" && test -s "$key"
openssl x509 -in "$cert" -noout -checkend 0
openssl x509 -in "$cert" -noout -checkip 8.148.157.132

# Check both configured listeners and live sockets before any change. Never stop
# whatever currently owns a port. Pick different ports and rerun instead.
config=$(nginx -T 2>/dev/null)
grep -Eq 'include[[:space:]]+/etc/nginx/conf.d/\*\.conf;' <<< "$config" || {
  echo 'Nginx must include /etc/nginx/conf.d/*.conf; inspect the server layout first.' >&2; exit 1;
}
if grep -Eq "listen[[:space:]]+([^;[:space:]]*:)?($port|$backend)([;[:space:]])" <<< "$config"; then
  echo 'Requested port already appears in Nginx configuration. Nothing changed.' >&2; exit 1
fi
"$python_bin" - "$port" "$backend" <<'PY'
import socket, sys
for port in map(int, sys.argv[1:]):
    with socket.socket() as s:
        s.bind(('0.0.0.0', port))
PY
for path in /etc/mydbdb /etc/nginx/conf.d/mydbdb.conf /etc/systemd/system/mydbdb.service \
    /etc/sudoers.d/mydbdb-deploy /opt/mydbdb /var/lib/mydbdb /var/lib/mydbdb-deploy; do
  if [[ -e "$path" ]]; then echo "Already exists: $path. Refusing to overwrite." >&2; exit 1; fi
done
nginx_user=$(awk '$1 == "user" {gsub(";", "", $2); print $2; exit}' <<< "$config")
test -n "$nginx_user"
nginx_group=$(id -gn "$nginx_user")

echo 'Browsing is public. Set an admin password ONLY for adding entries and syncing NeoDB (user: owner).'
read -rs -p 'Admin password (at least 12 characters): ' password </dev/tty; echo
read -rs -p 'Repeat password: ' confirm </dev/tty; echo
[[ ${#password} -ge 12 && "$password" == "$confirm" ]] || { echo 'Passwords differ or are too short.' >&2; exit 1; }
password_hash=$(printf '%s\n' "$password" | openssl passwd -6 -stdin)
unset password confirm

install -d -m 755 /etc/mydbdb
install -d -m 700 -o ledger-deploy -g ledger-deploy \
  /opt/mydbdb /opt/mydbdb/releases /var/lib/mydbdb /var/lib/mydbdb-deploy /var/lib/mydbdb-deploy/incoming
printf '[]\n' > /var/lib/mydbdb/media_data.json
chown ledger-deploy:ledger-deploy /var/lib/mydbdb/media_data.json
printf 'owner:%s\n' "$password_hash" > /etc/mydbdb/htpasswd
unset password_hash
chown "root:$nginx_group" /etc/mydbdb/htpasswd
chmod 640 /etc/mydbdb/htpasswd
printf 'PUBLIC_PORT=%s\nBACKEND_PORT=%s\nPYTHON_BIN=%s\n' "$port" "$backend" "$python_bin" > /etc/mydbdb/deploy.conf
chmod 644 /etc/mydbdb/deploy.conf

cat > /etc/systemd/system/mydbdb.service <<UNIT
[Unit]
Description=mydbdb NeoDB collection
Wants=network-online.target
After=network-online.target
ConditionPathExists=/opt/mydbdb/current/server.py

[Service]
Type=simple
User=ledger-deploy
Group=ledger-deploy
WorkingDirectory=/opt/mydbdb/current
ExecStart=$python_bin /opt/mydbdb/current/server.py --port $backend --state-dir /var/lib/mydbdb --public-origin https://8.148.157.132:$port
Restart=on-failure
RestartSec=3
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/mydbdb

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 /etc/systemd/system/mydbdb.service
printf 'ledger-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart mydbdb.service\n' > /etc/sudoers.d/mydbdb-deploy
chmod 440 /etc/sudoers.d/mydbdb-deploy
visudo -cf /etc/sudoers.d/mydbdb-deploy
systemd-analyze verify /etc/systemd/system/mydbdb.service

cat > /etc/nginx/conf.d/mydbdb.conf <<NGINX
# mydbdb only; no changes to existing 80/443/8088 virtual hosts.
server {
    listen $port ssl;
    server_name 8.148.157.132;
    ssl_certificate $cert;
    ssl_certificate_key $key;
    ssl_protocols TLSv1.2 TLSv1.3;
    auth_basic "mydbdb";
    auth_basic_user_file /etc/mydbdb/htpasswd;
    client_max_body_size 64k;
    access_log off;
    error_log /var/log/nginx/mydbdb-error.log warn;
    proxy_set_header Host 127.0.0.1:$backend;
    proxy_set_header Authorization "";
    proxy_set_header Connection "";
    proxy_http_version 1.1;
    proxy_read_timeout 120s;
    proxy_hide_header WWW-Authenticate;

    location = /_health { return 404; }
    # Only the read-only page, script and collection are public. All APIs retain
    # the admin check, including session/CSRF and NeoDB account connection.
    location ~ ^/(index\\.html|add\\.js|media_data\\.json)?\$ {
        auth_basic off;
        proxy_pass http://127.0.0.1:$backend;
    }
    location / {
        proxy_pass http://127.0.0.1:$backend;
    }
}
NGINX
chmod 644 /etc/nginx/conf.d/mydbdb.conf
# A failed config/reload never replaces the running Nginx configuration. Remove
# only the newly-created include so later renewals cannot load a broken one.
if ! nginx -t || ! systemctl reload nginx; then
  mv /etc/nginx/conf.d/mydbdb.conf /etc/mydbdb/nginx.failed.conf
  echo 'Nginx activation failed; existing sites kept running. See /etc/mydbdb/nginx.failed.conf.' >&2
  exit 1
fi
systemctl daemon-reload
systemctl enable mydbdb.service
echo "Setup ready: https://8.148.157.132:$port (public browsing; admin: owner). Open TCP $port only."
echo 'Next: authorize the deployment SSH public key, upload initial media_data.json, then run Deploy mydbdb.'
