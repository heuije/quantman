# 퀀트 플랫폼 API 서버 — 컨테이너 이미지 (Railway 등 배포용)
# 빌드 컨텍스트: platform/  (core/ 와 server/ 를 함께 포함)
FROM python:3.12-slim

WORKDIR /srv

# core 공유 패키지 먼저 설치 (변경 빈도 낮음 → 레이어 캐시)
COPY core/ ./core/
RUN pip install --no-cache-dir -e ./core

# 서버 의존성
COPY server/ ./server/
RUN pip install --no-cache-dir -r server/requirements.txt

WORKDIR /srv/server
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# PORT는 호스팅(예: Railway)이 주입
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
