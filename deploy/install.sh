#!/usr/bin/env bash
# Install the remote codex review service and its Cloudflare Tunnel on Ubuntu 22.04 x86_64.
# Idempotent; installs units without enabling them and never writes a credential.
set -euo pipefail

CODEX_VERSION=0.160.1
CODEX_ASSET=codex-package-x86_64-unknown-linux-musl.tar.gz
CODEX_URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${CODEX_ASSET}"
CODEX_SHA256=340801565906a7028f6baaa9ab6853addaef221f0016a1417a7c1ffdd96c21f0
CODEX_DIR=/opt/codex
# codex finds its helpers through the package layout of the resolved executable (docs/runbook.zh.md §9).
CODEX_REQUIRED="bin/codex bin/codex-code-mode-host codex-resources/bwrap codex-resources/zsh/bin/zsh codex-path/rg"
CLOUDFLARED_VERSION=2026.10.0
CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
CLOUDFLARED_SHA256=d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db

SERVICE_USER=codex-review
TUNNEL_USER=codex-review-tunnel
STATE=/var/lib/codex-review
ETC=/etc/codex-review
TUNNEL_ETC=/etc/codex-review-tunnel
APP=/opt/codex-review
RUNNER_ROOT=/opt/actions-runner
SRC=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$SRC/.." && pwd)

die() { echo "install.sh: $*" >&2; exit 1; }
say() { echo "== $*"; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ "$(uname -m)" = x86_64 ] || die "x86_64 only"

download() {  # url sha256 destination
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet - || die "sha256 mismatch for $1"
}

say "packages"
missing=""
for pkg in git bubblewrap ca-certificates curl openssl python3 sqlite3; do
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed' || missing="$missing $pkg"
done
if [ -n "$missing" ]; then
  apt-get update -qq
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $missing
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 >= 3.10 required"

say "users"
id -u "$SERVICE_USER" >/dev/null 2>&1 ||
  useradd --system --user-group --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
id -u "$TUNNEL_USER" >/dev/null 2>&1 ||
  useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$TUNNEL_USER"

say "isolation from runner accounts"
if [ -d "$RUNNER_ROOT" ]; then
  own_groups=" $(id -nG "$SERVICE_USER") "
  runner_users=$(find "$RUNNER_ROOT" -mindepth 0 -maxdepth 1 -printf '%u\n' | sort -u)
  for runner in $runner_users; do
    for group in $(id -nG "$runner"); do
      case "$own_groups" in *" $group "*) die "$SERVICE_USER shares group $group with runner user $runner" ;; esac
    done
  done
  while IFS= read -r path; do
    if runuser -u "$SERVICE_USER" -- test -w "$path"; then die "$SERVICE_USER can write $path"; fi
  done < <(find "$RUNNER_ROOT" -mindepth 0 -maxdepth 2)
  [ -z "$(find "$RUNNER_ROOT" -user "$SERVICE_USER" -print -quit)" ] || die "$SERVICE_USER owns files under $RUNNER_ROOT"
fi

say "directories"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$STATE" "$STATE/mirrors" "$STATE/work" "$STATE/.codex"
install -d -o root -g "$SERVICE_USER" -m 0750 "$ETC"
install -d -o root -g "$TUNNEL_USER" -m 0750 "$TUNNEL_ETC"
install -d -o root -g root -m 0755 "$APP"

say "program"
install -o root -g root -m 0755 "$ROOT/codex_review.py" "$APP/codex_review.py"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 "$SRC/codex-config.toml" "$STATE/.codex/config.toml"
if [ ! -e "$ETC/codex-review.env" ]; then
  install -o root -g "$SERVICE_USER" -m 0640 "$SRC/codex-review.env.example" "$ETC/codex-review.env"
fi
drop_tests_tier() {  # rules file; removes the retired `tests` tables, keeps medium/high and owner/mode
  python3 -I - "$1" <<'PY'
import json, os, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as handle:
    data = json.load(handle)
tables = [data.get('default')] + list((data.get('repos') or {}).values())
changed = [table.pop('tests') for table in tables if isinstance(table, dict) and 'tests' in table]
if changed:
    info = os.stat(path)
    with open(path + '.new', 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
    os.chown(path + '.new', info.st_uid, info.st_gid)
    os.chmod(path + '.new', info.st_mode & 0o7777)
    os.replace(path + '.new', path)
    print(f'removed {len(changed)} retired tests table(s) from {path}')
PY
}

if [ ! -e "$ETC/effort-rules.json" ]; then
  install -o root -g "$SERVICE_USER" -m 0640 "$SRC/effort-rules.json" "$ETC/effort-rules.json"
fi
drop_tests_tier "$ETC/effort-rules.json"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

package_ok() {  # directory
  grep -q "\"version\": \"$CODEX_VERSION\"" "$1/codex-package.json" 2>/dev/null || return 1
  for file in $CODEX_REQUIRED; do
    [ -f "$1/$file" ] && [ -x "$1/$file" ] || { echo "missing or not executable: $1/$file" >&2; return 1; }
  done
}

say "codex-cli $CODEX_VERSION package"
if ! package_ok "$CODEX_DIR" 2>/dev/null; then
  download "$CODEX_URL" "$CODEX_SHA256" "$tmp/$CODEX_ASSET"
  rm -rf "$CODEX_DIR.new"
  install -d -o root -g root -m 0755 "$CODEX_DIR.new"
  tar -xzf "$tmp/$CODEX_ASSET" -C "$CODEX_DIR.new" --no-same-owner --no-same-permissions
  chown -R root:root "$CODEX_DIR.new"
  chmod -R go-w "$CODEX_DIR.new"
  package_ok "$CODEX_DIR.new" || die "$CODEX_ASSET lacks a required file"
  rm -rf "$CODEX_DIR.old"
  if [ -e "$CODEX_DIR" ]; then mv "$CODEX_DIR" "$CODEX_DIR.old"; fi
  mv "$CODEX_DIR.new" "$CODEX_DIR"
  rm -rf "$CODEX_DIR.old"
fi
ln -sfn "$CODEX_DIR/bin/codex" /usr/local/bin/codex.new
mv -Tf /usr/local/bin/codex.new /usr/local/bin/codex
package_ok "$CODEX_DIR" || die "codex package check failed"
[ "$(readlink -f /usr/local/bin/codex)" = "$CODEX_DIR/bin/codex" ] || die "/usr/local/bin/codex does not resolve into $CODEX_DIR"
[ "$(/usr/local/bin/codex --version)" = "codex-cli $CODEX_VERSION" ] || die "codex version check failed"

say "cloudflared $CLOUDFLARED_VERSION"
case "$(/usr/local/bin/cloudflared --version 2>/dev/null || true)" in
  *"version $CLOUDFLARED_VERSION "*) ;;
  *)
    download "$CLOUDFLARED_URL" "$CLOUDFLARED_SHA256" "$tmp/cloudflared"
    install -o root -g root -m 0755 "$tmp/cloudflared" /usr/local/bin/cloudflared
    ;;
esac
/usr/local/bin/cloudflared --version | grep -q "version $CLOUDFLARED_VERSION " || die "cloudflared version check failed"

say "systemd units (installed, not enabled)"
install -o root -g root -m 0644 "$SRC/codex-review.service" /etc/systemd/system/codex-review.service
install -o root -g root -m 0644 "$SRC/codex-review-tunnel.service" /etc/systemd/system/codex-review-tunnel.service
systemctl daemon-reload
systemctl show -p InaccessiblePaths codex-review.service | grep -q "$RUNNER_ROOT" ||
  die "codex-review.service does not hide $RUNNER_ROOT"

say "credentials still required (not written by this script)"
for file in "$ETC/app-private-key.pem" "$ETC/webhook-secret" "$TUNNEL_ETC/token"; do
  if [ -e "$file" ]; then echo "present: $file"; else echo "missing: $file"; fi
done
if [ -e "$STATE/.codex/auth.json" ]; then echo "present: codex login"; else echo "missing: codex login"; fi
echo "Next: docs/runbook.zh.md"
