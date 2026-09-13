#!/bin/bash
# Run ON the droplet as root after files are in /opt/a2a.
# Does not touch /opt/edgeos.
set -euo pipefail
A2A_ROOT=/opt/a2a
A2A_DB_DIR=/var/lib/a2a
PUBLIC_URL="${A2A_PUBLIC_URL:-http://170.64.146.185:8787}"

if [[ ! -f "$A2A_ROOT/server.py" ]]; then
  echo "missing $A2A_ROOT/server.py" >&2
  exit 1
fi

apt-get update -y
apt-get install -y python3 python3-venv

id a2a >/dev/null 2>&1 || useradd --system --home "$A2A_ROOT" --shell /usr/sbin/nologin a2a
mkdir -p "$A2A_DB_DIR" "$A2A_ROOT"
chmod 755 "$A2A_ROOT" "$A2A_DB_DIR"

if [[ ! -f "$A2A_ROOT/.env" ]]; then
  KEY=$(python3 "$A2A_ROOT/deploy/new_write_key.py")
  umask 077
  cat > "$A2A_ROOT/.env" <<EOF
A2A_HOST=0.0.0.0
A2A_PORT=8787
A2A_PUBLIC_URL=$PUBLIC_URL
A2A_WRITE_KEY=$KEY
A2A_DB=$A2A_DB_DIR/a2a.sqlite
EOF
  echo "wrote $A2A_ROOT/.env (write key generated on box, not on OneDrive)"
fi

chown -R a2a:a2a "$A2A_ROOT" "$A2A_DB_DIR"
chmod 600 "$A2A_ROOT/.env"

install -m 644 "$A2A_ROOT/deploy/a2a.service" /etc/systemd/system/a2a.service
systemctl daemon-reload
systemctl enable a2a
systemctl restart a2a
sleep 1
systemctl --no-pager --full status a2a || true

if command -v ufw >/dev/null 2>&1; then
  ufw allow 8787/tcp || true
fi

python3 "$A2A_ROOT/deploy/smoke_local.py" "http://127.0.0.1:8787"
echo "PUBLIC $PUBLIC_URL"
echo "card   $PUBLIC_URL/.well-known/agent-card.json"
echo "EdgeOS not touched."
