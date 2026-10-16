#!/usr/bin/env bash
# Run as ledger-deploy. Only mydbdb's service and release symlink are changed.
set -Eeuo pipefail
umask 077
release="${1:?Release id required}"
port="${2:?HTTPS port required}"
[[ "$release" =~ ^[0-9a-f]{12}-[0-9]+-[0-9]+$ ]]
[[ "$port" =~ ^[0-9]{4,5}$ ]]
# Root-owned configuration, not part of the writable release.
# shellcheck source=/dev/null
source /etc/mydbdb/deploy.conf
[[ "$port" == "$PUBLIC_PORT" ]] || { echo 'Port differs from server setup; no changes made.' >&2; exit 1; }
base=/opt/mydbdb
state=/var/lib/mydbdb-deploy
archive="$state/incoming/$release.tar.gz"
target="$base/releases/$release"
test -s "$archive"
test ! -e "$target"
test -s /var/lib/mydbdb/media_data.json
test -s /var/lib/mydbdb/import-owner.json
exec 9>"$state/deploy.lock"
flock -w 120 9
previous=
if [[ -L "$base/current" ]]; then previous=$(readlink -f "$base/current"); fi
[[ ! -e "$base/current" || -L "$base/current" ]]
[[ -z "$previous" || "$previous" == "$base/releases/"* ]]
changed=0

health() {
  "$PYTHON_BIN" - "$BACKEND_PORT" "$1" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(f'http://127.0.0.1:{sys.argv[1]}/_health', timeout=5) as response:
    result = json.load(response)
assert result['ok'] and result['release'] == sys.argv[2], result
PY
}

finish() {
  result=$?
  trap - EXIT
  if (( result != 0 && changed == 1 )); then
    if [[ -n "$previous" ]]; then
      ln -sfn "$previous" "$base/current.next"
      mv -Tf "$base/current.next" "$base/current"
      if sudo -n /usr/bin/systemctl restart mydbdb.service; then
        echo "Restored previous code: $previous" >&2
      else
        echo "Recovery needs administrator: systemctl restart mydbdb.service (previous code restored)." >&2
      fi
    else
      echo 'First release failed; inspect journalctl -u mydbdb.service. No previous release exists.' >&2
    fi
  fi
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

mkdir "$target"
mkdir "$target/vendor"
# Extract only explicit code files, never paths supplied by an archive.
for file in index.html add.js server.py fetch_posters.py data_tools.py vendor/fflate.js vendor/fflate.LICENSE RELEASE; do
  tar -xOzf "$archive" "$file" > "$target/$file"
  test -s "$target/$file"
done
[[ "$(cat "$target/RELEASE")" == "$release" ]]
"$PYTHON_BIN" -m py_compile "$target/server.py" "$target/fetch_posters.py" "$target/data_tools.py"
"$PYTHON_BIN" -c 'import json; assert isinstance(json.load(open("/var/lib/mydbdb/media_data.json")), list)'

# Refuse a foreign listener before touching the service. A running mydbdb must
# report the previous release; a first deployment requires a free backend port.
if systemctl is-active --quiet mydbdb.service; then
  test -n "$previous"
  health "$(cat "$previous/RELEASE")"
else
  "$PYTHON_BIN" - "$BACKEND_PORT" <<'PY'
import socket, sys
with socket.socket() as s:
    s.bind(('127.0.0.1', int(sys.argv[1])))
PY
fi
sudo -n -l /usr/bin/systemctl restart mydbdb.service >/dev/null
changed=1
ln -sfn "$target" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
sudo -n /usr/bin/systemctl restart mydbdb.service
healthy=0
for _attempt in {1..15}; do
  if health "$release" 2>/dev/null; then healthy=1; break; fi
  sleep 1
done
(( healthy == 1 )) || { echo 'New release health check failed.' >&2; exit 1; }

# No password in CI: require authentication for the page, data and every API.
# Connecting locally works even before the cloud firewall opens the public port.
for path in / /index.html /add.js /media_data.json /api/data /api/import/library.js /api/import/start /api/import/chunk /api/import/finish /api/data/commit /api/data/cancel; do
  expected=401
  status=$(curl --silent --show-error --connect-timeout 5 --max-time 10 \
    --connect-to "8.148.157.132:$PUBLIC_PORT:127.0.0.1:$PUBLIC_PORT" \
    -o /dev/null -w '%{http_code}' "https://8.148.157.132:$PUBLIC_PORT$path")
  [[ "$status" == "$expected" ]] || { echo "HTTPS access check failed: $path ($status; expected $expected)" >&2; exit 1; }
done
for path in /api/import/start /api/import/chunk /api/import/finish /api/data/commit /api/data/cancel; do
  status=$(curl --silent --show-error --connect-timeout 5 --max-time 10 \
    --connect-to "8.148.157.132:$PUBLIC_PORT:127.0.0.1:$PUBLIC_PORT" \
    -H 'Content-Type: application/json' --data '{}' -o /dev/null -w '%{http_code}' \
    "https://8.148.157.132:$PUBLIC_PORT$path")
  [[ "$status" == 401 ]] || { echo "Unauthenticated write was not rejected: $path ($status)" >&2; exit 1; }
done
printf '%s\n' "$release" > "$state/current-release"
changed=0
rm -f -- "$archive"
echo "Deployed $release; persistent data and private configuration retained."
