#!/bin/sh
# Update only Great Equalizer. Run as root after extracting a verified release.
set -eu
GE_VERSION=${1:-}
case "$GE_VERSION" in
    ''|*[!0-9.]*|.*) echo 'Usage: update-release.sh 0.4.0' >&2; exit 2 ;;
esac
GE_RELEASE="/opt/great_equalizer/releases/$GE_VERSION"
GE_PREVIOUS=$(readlink -f /opt/great_equalizer/current)
case "$GE_PREVIOUS" in
    /opt/great_equalizer/releases/*) ;;
    *) echo 'Unexpected current release; nothing changed.' >&2; exit 2 ;;
esac
test -f "$GE_RELEASE/web_app.py"
test -x /opt/great_equalizer/venv/bin/python
if [ "$GE_RELEASE" = "$GE_PREVIOUS" ]; then
    echo 'This release is already selected; nothing changed.'
    exit 0
fi
# Check imports before stopping the existing application. Never load credentials.
env GE_SERVER_MODE=0 PYTHONDONTWRITEBYTECODE=1 /opt/great_equalizer/venv/bin/python - "$GE_RELEASE" "$GE_VERSION" <<'PY'
import sys
sys.path.insert(0,sys.argv[1])
import web_app
assert web_app.APP_VERSION == sys.argv[2], 'Unexpected application version'
PY
GE_ROLLBACK=1
rollback() {
    if [ "$GE_ROLLBACK" = 1 ]; then
        echo 'Update failed. Restoring the previous Great Equalizer release.' >&2
        systemctl stop great-equalizer.service || true
        ln -sTf "$GE_PREVIOUS" /opt/great_equalizer/current
        systemctl start great-equalizer.service || true
    fi
}
trap rollback EXIT
trap 'exit 1' HUP INT TERM
systemctl stop great-equalizer.service
ln -sTf "$GE_RELEASE" /opt/great_equalizer/current
systemctl start great-equalizer.service
GE_ATTEMPT=0
while [ "$GE_ATTEMPT" -lt 20 ]; do
    GE_HEALTH=$(curl -fsS --max-time 2 http://127.0.0.1:8765/great_equalizer/health 2>/dev/null || true)
    if printf '%s' "$GE_HEALTH" | /opt/great_equalizer/venv/bin/python -c 'import json,sys;assert json.load(sys.stdin)["version"]==sys.argv[1]' "$GE_VERSION" 2>/dev/null; then
        GE_ROLLBACK=0
        printf '%s\n' "$GE_HEALTH"
        echo 'Great Equalizer updated. Log in and reconnect Ozon; no key was retained.'
        exit 0
    fi
    GE_ATTEMPT=$((GE_ATTEMPT+1))
    sleep 1
done
exit 1
