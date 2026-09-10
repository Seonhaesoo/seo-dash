#!/usr/bin/env bash
# sudo 없이 설치 — ~/seo-dash 에 코드와 가상환경, 사용자 systemd 서비스(웹 8080) + 타이머(시작 3분 뒤·12시간마다).
# 부팅 때 사용자 서비스가 뜨려면 linger 가 켜져 있어야 한다: loginctl enable-linger $USER
# 사용: curl -fsSL https://raw.githubusercontent.com/Seonhaesoo/seo-dash/main/install-user.sh | bash
set -euo pipefail
APP="$HOME/seo-dash"
REPO=${REPO:-https://github.com/Seonhaesoo/seo-dash.git}

if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q "$REPO" "$APP"; fi
mkdir -p "$APP/data"

# venv 모듈이 없는 우분투(python3-venv 미설치)를 위해 pip → virtualenv 를 사용자 영역에 준비
if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c 'import ensurepip' 2>/dev/null; then
  if ! python3 -m pip --version >/dev/null 2>&1; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | python3 - --user --break-system-packages -q
  fi
  python3 -m pip install --user --break-system-packages -q virtualenv
  [ -d "$APP/.venv" ] || python3 -m virtualenv -q "$APP/.venv"
else
  [ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
fi
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/seo-dash.service" <<EOF
[Unit]
Description=seo-dash web (검색 성적표)
After=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python app.py
Restart=always
RestartSec=5
Environment=PORT=8080

[Install]
WantedBy=default.target
EOF

cat > "$HOME/.config/systemd/user/seo-dash-sync.service" <<EOF
[Unit]
Description=seo-dash sync (서치콘솔·GA4 수집)

[Service]
Type=oneshot
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python collector.py
EOF

cat > "$HOME/.config/systemd/user/seo-dash-sync.timer" <<EOF
[Unit]
Description=seo-dash sync timer — 시작 3분 뒤, 이후 12시간마다

[Timer]
OnStartupSec=3min
OnUnitActiveSec=12h
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > "$HOME/.config/systemd/user/seo-dash-index.service" <<EOF
[Unit]
Description=seo-dash indexer (IndexNow 네이버·빙 알림 · 구글 사이트맵 재제출 · 색인 표본)

[Service]
Type=oneshot
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python -u indexer.py
EOF

cat > "$HOME/.config/systemd/user/seo-dash-index.timer" <<EOF
[Unit]
Description=seo-dash indexer timer — 시작 10분 뒤, 이후 6시간마다

[Timer]
OnStartupSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now seo-dash.service seo-dash-sync.timer seo-dash-index.timer
systemctl --user restart seo-dash.service
loginctl enable-linger "$USER" 2>/dev/null || true
sleep 3
IP=$(hostname -I | awk '{print $1}')
if curl -fs "http://127.0.0.1:8080/health" >/dev/null; then
  echo "설치 완료 — 브라우저에서 http://$IP:8080 을 여세요. (설정에서 key.json 업로드 → 지금 동기화)"
else
  echo "서비스가 아직 응답하지 않습니다: systemctl --user status seo-dash"
fi
