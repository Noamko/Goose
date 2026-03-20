#!/usr/bin/env bash
# Goose deployment setup script
# Run once on a fresh Digital Ocean droplet (Ubuntu 22.04+)
# Usage: sudo bash setup.sh <github-repo-url> <tailscale-auth-key>
# Example: sudo bash setup.sh git@github.com:Noamko/Goose.git tskey-auth-xxxxx
set -e

REPO_URL="${1:?Usage: sudo bash setup.sh <github-repo-url> <tailscale-auth-key>}"
TS_AUTH_KEY="${2:?Usage: sudo bash setup.sh <github-repo-url> <tailscale-auth-key>}"
APP_DIR="/opt/goose"
APP_USER="goose"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx git curl ufw

echo "==> Installing Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey "$TS_AUTH_KEY" --hostname goose --accept-routes

echo "==> Waiting for Tailscale IP..."
TS_IP=""
for i in $(seq 1 20); do
  TS_IP=$(tailscale ip -4 2>/dev/null || true)
  [ -n "$TS_IP" ] && break
  sleep 2
done
[ -z "$TS_IP" ] && { echo "ERROR: Could not get Tailscale IP"; exit 1; }
echo "    Tailscale IP: $TS_IP"

TS_HOSTNAME=$(tailscale status --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))")
echo "    Tailscale hostname: $TS_HOSTNAME"

echo "==> Creating app user"
id -u "$APP_USER" &>/dev/null || useradd -r -m -d "$APP_DIR" -s /bin/bash "$APP_USER"

echo "==> Cloning / updating repo"
if [ -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" pull
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
else
  # If the dir exists but is not a git repo, clone into it
  if [ -d "$APP_DIR" ] && [ "$(ls -A $APP_DIR)" ]; then
    rm -rf "${APP_DIR:?}"
  fi
  git clone "$REPO_URL" "$APP_DIR"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi

echo "==> Setting up Python venv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Setting up .env"
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'ENVEOF'
OPENAI_API_KEY=your-key-here
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_ALLOWED_CHAT_IDS=
ENVEOF
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  echo ""
  echo "  !! ACTION REQUIRED: edit $APP_DIR/.env and set OPENAI_API_KEY"
  echo "     Then run: systemctl restart goose"
  echo ""
fi

echo "==> Installing systemd service"
cp "$(dirname "$0")/goose.service" /etc/systemd/system/goose.service
sed -i "s|APP_DIR_PLACEHOLDER|$APP_DIR|g" /etc/systemd/system/goose.service
systemctl daemon-reload
systemctl enable goose

echo "==> Getting Tailscale HTTPS certificate"
CERT_DIR="/etc/nginx/tailscale-certs"
mkdir -p "$CERT_DIR"
tailscale cert --cert-file "$CERT_DIR/$TS_HOSTNAME.crt" --key-file "$CERT_DIR/$TS_HOSTNAME.key" "$TS_HOSTNAME"
chmod 640 "$CERT_DIR/$TS_HOSTNAME.key"
chown root:www-data "$CERT_DIR/$TS_HOSTNAME.key"

echo "==> Configuring nginx"
cp "$(dirname "$0")/nginx-goose.conf" /etc/nginx/sites-available/goose
sed -i "s|TS_IP_PLACEHOLDER|$TS_IP|g" /etc/nginx/sites-available/goose
sed -i "s|TS_HOSTNAME_PLACEHOLDER|$TS_HOSTNAME|g" /etc/nginx/sites-available/goose
ln -sf /etc/nginx/sites-available/goose /etc/nginx/sites-enabled/goose
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx

echo "==> Configuring firewall (UFW)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
# Allow SSH (so you don't lock yourself out)
ufw allow 22/tcp comment "SSH"
# Do NOT open 80 or 443 — only accessible via Tailscale interface
ufw --force enable

echo "==> Starting services"
systemctl start goose
systemctl restart nginx

echo ""
echo "Done! Goose is running."
echo ""
echo "  Dashboard: https://$TS_HOSTNAME"
echo ""
echo "  If you haven't set OPENAI_API_KEY yet:"
echo "    sudo nano $APP_DIR/.env"
echo "    sudo systemctl restart goose"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status goose"
echo "    sudo journalctl -u goose -f"
