#!/usr/bin/env bash
# Install the remote codex review service and its Cloudflare Tunnel on Ubuntu 22.04 x86_64.
# Idempotent; installs units without enabling them and never writes a credential.
set -euo pipefail

CODEX_VERSION=0.160.1
CODEX_ASSET=codex-x86_64-unknown-linux-musl.tar.gz
CODEX_URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${CODEX_ASSET}"
CODEX_SHA256=9226581be592d18f7e7f740a352fdb63aa61e45e39f7eb9b09d3888c84bba33f
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
if [ ! -e "$ETC/effort-rules.json" ]; then
  install -o root -g "$SERVICE_USER" -m 0640 "$SRC/effort-rules.json" "$ETC/effort-rules.json"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

say "codex-cli $CODEX_VERSION"
if [ "$(/usr/local/bin/codex --version 2>/dev/null || true)" != "codex-cli $CODEX_VERSION" ]; then
  download "$CODEX_URL" "$CODEX_SHA256" "$tmp/$CODEX_ASSET"
  mkdir "$tmp/codex"
  tar -xzf "$tmp/$CODEX_ASSET" -C "$tmp/codex" --no-same-owner
  binaries=$(find "$tmp/codex" -type f -perm -u+x)
  [ "$(printf '%s\n' "$binaries" | grep -c .)" -eq 1 ] || die "expected one executable in $CODEX_ASSET"
  install -o root -g root -m 0755 "$binaries" /usr/local/bin/codex
fi
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
