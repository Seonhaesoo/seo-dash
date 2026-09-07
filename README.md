# 검색 성적표 (seo-dash)

구글 서치콘솔과 GA4의 숫자를 한 장으로 보여 주는 LAN 전용 대시보드. 항상 켜져 있지 않은 VM에서 돌리는 것을 전제로,
서버가 켜질 때(부팅 3분 뒤)와 12시간마다 자동으로 받아 오고, 화면의 **지금 동기화** 버튼으로 언제든 다시 받는다.

- `collector.py` — 서비스 계정 키로 접근 가능한 서치콘솔 속성·GA4 속성을 모두 찾아 SQLite(`data/seo.db`)에 저장. 처음이면 16개월치, 이후엔 빠진 날만 채움(서치콘솔 확정 지연 감안해 최근 4일은 다시 받음).
- `app.py` — Flask 웹(8080). 사이트별 7일 클릭·노출·순위와 30일 막대, 검색어·페이지 상위, 기회 검색어(노출 많고 순위 10위 밖), 사이트맵 제출 수, GA4 어제·7일 방문, 많이 본 페이지, 유입 경로, 한 줄 요약. `/settings`에서 key.json 업로드와 네이버 CSV 수동 업로드.
- `install.sh` — 데비안/우분투 VM에 `/opt/seo-dash` 설치 + systemd 서비스·타이머.

## 설치
sudo 가 되는 VM:
```bash
curl -fsSL https://raw.githubusercontent.com/Seonhaesoo/seo-dash/main/install.sh | sudo bash
```
sudo 없이(사용자 서비스 + linger):
```bash
curl -fsSL https://raw.githubusercontent.com/Seonhaesoo/seo-dash/main/install-user.sh | bash
```
그다음 `http://<VM IP>:8080/settings` 에서 key.json 업로드 → 서비스 계정 이메일을 서치콘솔·GA4에 사용자로 추가 → 성적표에서 지금 동기화.

## 갱신
```bash
cd ~/seo-dash && git pull && systemctl --user restart seo-dash   # (sudo 설치면 /opt/seo-dash, sudo systemctl restart seo-dash)
```

키 파일(`data/key.json`)과 데이터(`data/seo.db`)는 저장소에 올리지 않는다.
