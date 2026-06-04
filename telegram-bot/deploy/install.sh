#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Oracle Cloud Ubuntu VM에서 봇 설치/업데이트용 스크립트.
# 사용법:
#   bash install.sh           # 처음 설치 또는 의존성 업데이트
#   bash install.sh restart   # 코드만 바꾸고 서비스 재시작
# ─────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="/home/ubuntu/newsbot"
SERVICE_NAME="newsbot"
SERVICE_SRC="$APP_DIR/deploy/newsbot.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"

cd "$APP_DIR"

if [ "${1:-}" = "restart" ]; then
  sudo systemctl restart "$SERVICE_NAME"
  sudo systemctl status "$SERVICE_NAME" --no-pager
  exit 0
fi

echo "==> 시스템 패키지 업데이트"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Python venv 생성/활성화"
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

echo "==> 의존성 설치"
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "⚠️  .env 파일이 없습니다. .env.example을 복사해서 토큰을 채워주세요:"
  echo "    cp .env.example .env && nano .env"
  exit 1
fi

echo "==> systemd 서비스 등록"
sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "==> 상태 확인"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager || true

echo ""
echo "✅ 설치 완료. 로그 보기:  sudo journalctl -u ${SERVICE_NAME} -f"
