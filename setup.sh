#!/usr/bin/env bash
# Install (or re-configure) Polybot on a fresh Ubuntu server, e.g. a DigitalOcean droplet.
#   sudo bash setup.sh
# Safe to run again any time: it asks for the settings again (press Enter to keep the current value)
# and restarts the bot. Your trading history in data/ is kept.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/polybot.env

if [ "$(id -u)" != "0" ]; then echo "Please run with sudo:  sudo bash setup.sh"; exit 1; fi

echo ">> Installing Python (takes a minute)..."
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl caddy >/dev/null
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q "cryptography==43.0.3"
mkdir -p "$DIR/data"

# ---------- settings ----------
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
current() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- ; }
ask() {  # ask NAME "question" default [secret]
  local cur; cur="$(current "$1")"; [ -z "$cur" ] && cur="$3"
  local shown="$cur"; [ -n "$4" ] && [ -n "$cur" ] && shown="(already set)"
  local ans
  if [ -n "$4" ]; then read -r -s -p "$2 [$shown]: " ans; echo; else read -r -p "$2 [$shown]: " ans; fi
  [ -z "$ans" ] && ans="$cur"
  eval "$1=\"\$ans\""
}
echo
echo ">> Settings (press Enter to keep what's in brackets)"
ask REPORT_KEY "Dashboard password" "" secret
while [ -z "$REPORT_KEY" ]; do echo "   A dashboard password is required."; ask REPORT_KEY "Dashboard password" "" secret; done
ask PM_KEY_ID  "Polymarket US API Key ID (Enter to skip)" ""
ask PM_SECRET  "Polymarket US API Secret Key - typing is hidden (Enter to skip)" "" secret
# daily loss limit, bet cap and cooldowns are set on the dashboard (Live trading -> Safety limits)
LIVE_DAILY_LOSS_LIMIT="$(current LIVE_DAILY_LOSS_LIMIT)"; LIVE_DAILY_LOSS_LIMIT="${LIVE_DAILY_LOSS_LIMIT:-10}"
LIVE_MAX_BETS_PER_DAY="$(current LIVE_MAX_BETS_PER_DAY)"; LIVE_MAX_BETS_PER_DAY="${LIVE_MAX_BETS_PER_DAY:-40}"
cat > "$ENV_FILE" <<ENV
PYTHONUNBUFFERED=1
PORT=8080
BIND=127.0.0.1
DB_PATH=$DIR/data/polylog.db
REPORT_KEY=$REPORT_KEY
PM_KEY_ID=$PM_KEY_ID
PM_SECRET=$PM_SECRET
LIVE_DAILY_LOSS_LIMIT=$LIVE_DAILY_LOSS_LIMIT
LIVE_MAX_BETS_PER_DAY=$LIVE_MAX_BETS_PER_DAY
ENV
chmod 600 "$ENV_FILE"

# ---------- run 24/7 ----------
cat > /etc/systemd/system/polylog.service <<UNIT
[Unit]
Description=Polybot - Polymarket US 15-minute paper/live trader
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/venv/bin/python $DIR/logger.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable polylog >/dev/null 2>&1
systemctl restart polylog
sleep 3
# ---------- secure web address (https) ----------
IP="$(curl -s -4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
HOST="$(echo "$IP" | tr . -).sslip.io"
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    reverse_proxy 127.0.0.1:8080
}
http://$IP {
    redir https://$HOST{uri}
}
CADDY
command -v ufw >/dev/null && ufw status | grep -q active && { ufw allow 80/tcp; ufw allow 443/tcp; } >/dev/null || true
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy
echo ">> Starting up..."
sleep 20

echo
echo ">> Recent log:"
journalctl -u polylog -n 12 --no-pager -o cat
echo
echo "============================================================"
echo " Polybot is running 24/7 and restarts itself after reboots."
echo " Dashboard:  https://$HOST/?key=<your dashboard password>"
echo " Live trading: $( [ -z "$PM_KEY_ID" ] && echo 'not connected (no API keys)' || echo 'keys connected - turn bots on/off from the dashboard' )"
echo " Change settings:  cd $DIR && sudo bash setup.sh"
echo " Get updates:      cd $DIR && git pull && sudo systemctl restart polylog"
echo "============================================================"
