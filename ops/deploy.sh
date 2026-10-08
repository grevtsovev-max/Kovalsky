#!/bin/sh
set -eu
sha="${1:-}"
case "$sha" in *[!0-9a-f]*|"") echo "Invalid commit id" >&2; exit 64;; esac
[ "${#sha}" -eq 40 ] || { echo "Invalid commit id length" >&2; exit 64; }
incoming=/opt/kovalsky/incoming
release="/opt/kovalsky/releases/$sha"
app=/opt/kovalsky/app
[ -f "$incoming/pyproject.toml" ] && [ -f "$incoming/newsroom/cli.py" ] || { echo "Incoming project is incomplete" >&2; exit 65; }
[ ! -e "$release" ] || { echo "Release already exists" >&2; exit 73; }
# Reject a release that would replace the approved cabinet before changing app.
PYTHONPATH="$incoming" /opt/kovalsky/venv/bin/python -c '
from newsroom.dashboard import PAGE, serve
assert "materials-path-v2" in PAGE and "path-strip" in PAGE, "Approved cabinet missing"
assert "pipeline-funnel" in PAGE and "Отобрано по теме" in PAGE, "Material journey missing"
assert serve.__module__ == "newsroom.cabinet_server", "Cabinet API adapter missing"
'
old_target=
if [ -L "$app" ]; then
  old_target=$(readlink -f "$app")
elif [ -d "$app" ]; then
  old_target="/opt/kovalsky/releases/legacy-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$app" "$old_target"
fi
chown -R root:kovalsky "$incoming"
chmod -R u=rwX,g=rX,o= "$incoming"
mv "$incoming" "$release"
install -d -o kovalsky-deploy -g kovalsky-deploy -m 750 "$incoming"
ln -s "$release" /opt/kovalsky/app.next
mv -Tf /opt/kovalsky/app.next "$app"
# An update must never turn on an agent stopped by its owner.
newsroom_active=false
review_active=false
systemctl is-active --quiet kovalsky-newsroom.service && newsroom_active=true
systemctl is-active --quiet kovalsky-review.service && review_active=true
activate() {
  systemctl try-restart kovalsky-newsroom.service
  systemctl try-restart kovalsky-review.service
  systemctl restart kovalsky-dashboard.service
  if "$newsroom_active" && [ "$(systemctl show -p ConditionResult --value kovalsky-newsroom.service)" != no ]; then
    systemctl is-active --quiet kovalsky-newsroom.service || return 1
  fi
  if "$review_active" && [ "$(systemctl show -p ConditionResult --value kovalsky-review.service)" != no ]; then
    systemctl is-active --quiet kovalsky-review.service || return 1
  fi
  systemctl is-active --quiet kovalsky-dashboard.service
}
if ! activate; then
  if [ -n "$old_target" ] && [ -d "$old_target" ]; then
    ln -s "$old_target" /opt/kovalsky/app.rollback
    mv -Tf /opt/kovalsky/app.rollback "$app"
    activate || true
  fi
  echo "Service restart failed; previous release restored when available" >&2
  exit 1
fi
echo "Deployed $sha; services updated; owner stop state preserved"
