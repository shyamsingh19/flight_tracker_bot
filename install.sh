#!/usr/bin/env bash
# Install flightbot as a systemd service on any Ubuntu machine (Oracle, DigitalOcean, Pi…).
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
RUN_AS=${SUDO_USER:-$USER}
cd "$DIR"
sudo apt-get update -y && sudo apt-get install -y python3-venv sqlite3
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
chmod 600 .env
sudo tee /etc/systemd/system/flightbot.service >/dev/null <<UNIT
[Unit]
Description=Flight price alert bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable flightbot
.venv/bin/python -m unittest -q
if grep -qE '^(TELEGRAM_TOKEN|IGNAV_API_KEY|ALLOWED_USER_ID)=$' .env; then
  echo "Fill in $DIR/.env, then: sudo systemctl start flightbot"
else
  sudo systemctl restart flightbot && echo "Started. Logs: journalctl -u flightbot -f"
fi
