#!/usr/bin/env bash
# 검색 성적표(seo-dash) 설치 — 데비안/우분투 계열 VM. 사용: sudo bash install.sh
# /opt/seo-dash 에 코드와 가상환경, systemd 서비스(웹 8080) + 부팅 3분 뒤/12시간마다 동기화 타이머.
set -euo pipefail
APP=/opt/seo-dash
REPO=${REPO:-https://github.com/Seonhaesoo/seo-dash.git}
RUN_USER=${SUDO_USER:-$(whoami)}

if ! command -v python3 >/dev/null; then apt-get update && apt-get install -y python3; fi
apt-get update -qq && apt-get install -y -qq python3-venv python3-pip git >/dev/null

if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q "$REPO" "$APP"; fi
mkdir -p "$APP/data"
chown -R "$RUN_USER":"$RUN_USER" "$APP"
sudo -u "$RUN_USER" bash -c "cd $APP && python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt"

cat > /etc/systemd/system/seo-dash.service <<EOF
[Unit]
Description=seo-dash web (검색 성적표)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python app.py
Restart=always
RestartSec=5
Environment=PORT=8080

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/seo-dash-sync.service <<EOF
[Unit]
Description=seo-dash sync (서치콘솔·GA4 수집)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python collector.py
EOF

cat > /etc/systemd/system/seo-dash-sync.timer <<EOF
[Unit]
Description=seo-dash sync timer — 부팅 3분 뒤, 이후 12시간마다

[Timer]
OnBootSec=3min
OnUnitActiveSec=12h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now seo-dash.service seo-dash-sync.timer
systemctl restart seo-dash.service
sleep 2
IP=$(hostname -I | awk '{print $1}')
echo "설치 완료 — 브라우저에서 http://$IP:8080 을 여세요. (설정에서 key.json 업로드 → 지금 동기화)"
