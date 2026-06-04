# ☁️ 텔레그램 봇 — Google Cloud Always Free 배포 가이드

Oracle Cloud 가입이 막힐 때 대안으로 진행. Google Cloud Always Free의 e2-micro VM은 영구 무료이며,
우리가 작성한 `install.sh`와 `newsbot.service`를 그대로 재사용합니다.

> Oracle은 `배포가이드.md` 참고. GCP는 더 간단합니다 — 브라우저에서 SSH·파일 업로드 가능해서 Windows 키 관리 불필요.

---

## 1단계 — Google 계정 준비

이미 Gmail 계정이 있다면 그대로 사용. 없으면 https://accounts.google.com/signup 에서 새로 만드세요.
(naver 메일도 Google 계정으로 등록 가능하지만 Gmail 권장)

---

## 2단계 — Google Cloud 가입

1. https://cloud.google.com/free 접속 → **무료로 시작하기** 클릭
2. Google 계정으로 로그인
3. **국가**: 대한민국
4. **결제 정보**: 카드 등록 (Always Free 한도 안에선 결제 X)
5. **약관 동의** → 시작

> ⚠️ 가입하면 자동으로 **$300 90일 무료 크레딧** 받음. 그 크레딧을 안 써도 **Always Free 자원**(e2-micro 1대 등)은 영구 무료라 안전.

---

## 3단계 — 프로젝트 생성 (또는 기본 프로젝트 사용)

가입 직후 자동 생성된 프로젝트(`My First Project` 등) 있으면 그대로 사용 가능. 새로 만들고 싶으면:

1. 콘솔 좌측 상단 프로젝트 드롭다운 → **새 프로젝트**
2. 이름: `newsbot` (아무거나 OK)
3. **만들기**

---

## 4단계 — Compute Engine API 활성화

1. 좌측 햄버거 메뉴(≡) → **Compute Engine** → **VM 인스턴스**
2. 첫 진입 시 "Compute Engine API 사용 설정" 화면 뜸 → **사용** 클릭
3. 1~2분 대기 (백그라운드에서 활성화)

---

## 5단계 — Always Free VM 생성

활성화 끝나면 자동으로 VM 인스턴스 페이지로 이동. **인스턴스 만들기** 클릭.

### 설정값 (Always Free 조건 정확히 맞춰야 함)

| 항목 | 값 | 비고 |
|---|---|---|
| **이름** | `newsbot` | 영문 소문자 |
| **리전** | **`us-west1` (오리건)** ⭐ | ⚠️ Always Free는 **us-west1 / us-central1 / us-east1**에서만 가능 |
| **영역** | `us-west1-a` (자동 선택됨) | |
| **머신 구성** | E2 시리즈 → **`e2-micro`** | ⚠️ 이거 외에 다른 거 고르면 유료 |
| **부팅 디스크** | `변경` 클릭 → OS: **Ubuntu** / 버전: **Ubuntu 22.04 LTS Minimal** 또는 **24.04 LTS** / 크기: **30 GB** / 유형: **표준 영구 디스크** | 30GB까지 무료 |
| **방화벽** | **HTTP 트래픽 허용** 체크 ✓ (선택, 나중에 웹대시보드 필요 시 대비) | HTTPS도 함께 체크해도 OK |
| 네트워크 태그 | 기본값 | |

**만들기** 클릭 → 30초~1분 대기 → 상태가 ▶ 초록색이 되면 준비 완료.

### 외부 IP 확인
VM 인스턴스 목록에서 **외부 IP** 컬럼의 숫자 메모(예: `34.123.45.67`). 매번 시작 시 바뀌므로 **정적 IP**가 필요하면:
- 좌측 메뉴 → 네트워크 → **VPC 네트워크** → **IP 주소** → 임시 → 정적으로 승격

> 💡 우리 봇은 outbound 연결만 쓰므로 외부 IP 자체가 필수는 아니지만, SSH 접속할 때는 필요합니다. 정적 IP 변환은 선택사항.

---

## 6단계 — Browser SSH로 접속 (Windows 키 관리 불요)

1. VM 인스턴스 목록에서 `newsbot` 행 우측의 **SSH** 버튼 클릭
2. 새 창이 열리며 자동으로 키 생성 + 접속 → 터미널 표시
3. 프롬프트가 `username@newsbot:~$` 같이 뜨면 성공

> ✅ Oracle처럼 키 파일 다운로드·icacls 권한 설정 같은 번거로움 없음.
> Google이 자동으로 OS Login용 키를 만들고 인증 처리해줌.

---

## 7단계 — 파일 업로드

### 방법 A (가장 간단): Browser SSH의 업로드 기능

1. Browser SSH 창 우측 상단의 **⚙️ 톱니바퀴 → 파일 업로드** 클릭
2. **로컬에서 zip 만들어서 한 번에 올리기**:

   로컬 PowerShell에서:
   ```powershell
   cd C:\Users\heuij
   Compress-Archive -Path bot-deploy\* -DestinationPath bot-deploy.zip -Force
   # 점 파일(.env, .gitignore)도 포함시키기:
   Compress-Archive -Path bot-deploy\.env, bot-deploy\.gitignore -Update -DestinationPath bot-deploy.zip
   ```

3. 만들어진 `bot-deploy.zip`을 Browser SSH 업로드로 전송 → 홈 디렉토리(`~/`)에 도착

4. 서버에서 압축 풀기:
   ```bash
   sudo apt-get update -y && sudo apt-get install -y unzip
   mkdir -p ~/newsbot
   unzip ~/bot-deploy.zip -d ~/newsbot
   ls ~/newsbot   # bot.py, .env, requirements.txt, deploy/ 등 확인
   ```

### 방법 B: gcloud CLI로 scp (선택)

로컬에 gcloud CLI 설치(https://cloud.google.com/sdk/docs/install) 후:
```powershell
gcloud auth login
gcloud compute scp --recurse C:\Users\heuij\bot-deploy newsbot:~/newsbot --zone=us-west1-a
```

---

## 8단계 — 봇 설치 및 가동

서버 SSH 창에서:

```bash
cd ~/newsbot
chmod +x deploy/install.sh
bash deploy/install.sh
```

### install.sh가 자동 처리하는 것
- Python 3, venv, pip 설치
- 의존성 설치 (anthropic, python-telegram-bot, yfinance 등)
- systemd 서비스 등록 + 부팅 시 자동 시작
- 백그라운드 가동

마지막에 `Active: active (running)` 보이면 성공.

### 로그 보기
```bash
sudo journalctl -u newsbot -f
```
(Ctrl+C로 빠져나옴)

### 텔레그램 확인
봇과의 채팅에서 `/status` 보내고 응답 오면 100% 정상.
`/rate` 보내면 미국채 7개 만기 수익률 표 받음.

---

## 9단계 — 운영 명령어

| 작업 | 명령어 |
|---|---|
| 상태 보기 | `sudo systemctl status newsbot` |
| 재시작 | `sudo systemctl restart newsbot` |
| 정지 | `sudo systemctl stop newsbot` |
| 시작 | `sudo systemctl start newsbot` |
| 실시간 로그 | `sudo journalctl -u newsbot -f` |
| 최근 100줄 로그 | `sudo journalctl -u newsbot -n 100 --no-pager` |
| 코드 수정 후 재기동 | `bash ~/newsbot/deploy/install.sh restart` |
| 의존성까지 재설치 | `bash ~/newsbot/deploy/install.sh` |

### .env 수정 (토큰 교체 등)
```bash
nano ~/newsbot/.env
sudo systemctl restart newsbot
```

### 코드 업데이트 (로컬에서 수정 후)
가장 간단:
1. 로컬에서 zip 다시 만들기 → Browser SSH 업로드
2. `unzip -o ~/bot-deploy.zip -d ~/newsbot` (`-o`: 덮어쓰기)
3. `bash ~/newsbot/deploy/install.sh restart`

---

## ⚠️ Always Free 한도 — 안전하게 사용하기

| 자원 | 무료 한도 | 우리 봇 예상 사용량 |
|---|---|---|
| **VM** | e2-micro **1대**, us-west1/central1/east1만 | 1대 ✓ |
| **디스크** | 30GB 표준 영구 디스크 | 5GB 미만 ✓ |
| **네트워크 송신** | 월 1GB (북미→다른 지역, **중국·호주 제외**) | ~100MB ✓ |
| **외부 IP** | 임시 IP 무료, 정적 IP 1개 무료(VM 연결 중일 때) | 임시 IP 사용 ✓ |

이 한도 안에 100% 들어옵니다. 한 가지 주의:
- **VM은 us-west1/central1/east1 중 한 곳에만** 만들기. 다른 리전에 만들면 유료.
- **e2-micro만**. 같은 시리즈의 e2-small/medium은 유료.

### 결제 알림 설정 (안전망)
1. 콘솔 좌측 → **결제** → **예산 및 알림** → **예산 만들기**
2. 이름: `safety`, 금액: **$1**, 알림 임계값 50%/90%/100%
3. 만들기 → 실수로 유료 자원 만들었을 때 즉시 알림 받음

---

## 트러블슈팅

### Browser SSH 접속이 안 됨
- VM이 `RUNNING` 상태인지 확인
- VPC 방화벽 규칙에 `default-allow-ssh` (port 22) 있는지 확인 — 기본 VPC면 자동 포함

### `sudo journalctl -u newsbot` 에 한글 깨짐
```bash
sudo locale-gen ko_KR.UTF-8 en_US.UTF-8
sudo update-locale LANG=ko_KR.UTF-8
sudo systemctl restart newsbot
```

### 봇이 계속 재시작
```bash
sudo journalctl -u newsbot -n 50 --no-pager
```
- `❌ 환경변수 누락` → `.env`가 zip에 안 포함됨. 다시 업로드
- `Conflict: terminated by other getUpdates` → 로컬에서 다른 봇 인스턴스가 같은 토큰으로 폴링 중. 종료

### Always Free 한도 초과 우려
무료 한도 사용량은 콘솔 → **결제** → **보고서**에서 실시간 확인 가능.
예산 알림 설정해두면 자동 통보.

### 봇 자체를 끄고 싶을 때
```bash
sudo systemctl stop newsbot
sudo systemctl disable newsbot   # 부팅 시 자동 시작도 해제
```

### VM 자체를 삭제하고 싶을 때
콘솔 → VM 인스턴스 → newsbot 우측 ⋮ → **삭제**
디스크도 함께 삭제됨(체크박스).
