#!/usr/bin/env bash
# One-time install on a fresh Ubuntu server. Run with:  sudo bash setup.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo ">> Installing Python..."
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv git >/dev/null
echo ">> Setting up the logger..."
python3 -m venv "$DIR/venv"
mkdir -p "$DIR/data"
cat > /etc/systemd/system/polylog.service <<UNIT
[Unit]
Description=Polymarket US 15-minute logger and paper trader
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python $DIR/logger.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now polylog
sleep 15
echo
echo ">> Market check:"
"$DIR/venv/bin/python" "$DIR/logger.py" check || true
echo
echo ">> Done. Last few log lines:"
journalctl -u polylog -n 15 --no-pager
echo
echo "The logger now runs 24/7 and restarts itself if the server reboots."
echo "Check on it any time with:  cd $DIR && venv/bin/python analyze.py"
