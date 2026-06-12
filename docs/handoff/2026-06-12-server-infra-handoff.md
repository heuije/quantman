# 핸드오프 — 서버 인프라(Neon·Railway) 효율·안정성 문제 #1~6

> **수신: 자동매매 무발주 ultra 캠페인 세션 (또는 서버 인프라를 일괄 처리할 세션)**
> **작성: 2026-06-12 · read-only 감사 세션 (조대표 지시로 핸드오프)**
> **목적:** 사전 맥락 없이 읽고 #1~6을 일괄 설계·구현·검증할 수 있도록 self-contained로 정리.

---

## 0. 한눈에 + 출처·신뢰도

이 6건은 **Neon·Railway 효율/안정성 4축 read-only 감사**(클라이언트 폴링 · 서버 읽기경로 · cron/캐시 · 자동매매↔DB 결합)에서 나왔고, **P1 항목은 코드로 직접 재확인**했다. 코드 변경은 **하나도 안 했다**(감사·진단만).

| # | 문제 | 영향 | 충돌 위험 | 머지 제약 |
|---|---|---|---|---|
| 1 | 무한 성장 테이블 + pruning cron 부재 | 💰비용·🐢렉 (시간 갈수록 악화) | 낮음 (자동매매 로직 무관) | 무거래 창 |
| 2 | 인덱스 공백 (received_at·status) | 💰🐢 | 낮음 | 무거래 창 (인덱스 생성 부하) |
| 3 | 스케줄러 폭주 가드 부재 | 🤖자동매매·💰 | 낮음 | 무거래 창 (재시작) |
| 4 | 메모리 1.3GB 상주 + 9.4GB 폴백 OOM | 💰🤖 | 낮음 | 진단 선행 |
| 5 | Monitor 15s 폴링 3/4 ETag 없음 | 💰 | web 안전 / commands.py 신중 | web은 무제약 |
| 6 | `server conn crashed?` (Neon idle-suspend) | 🤖🐢 | **높음 — 무발주 클러스터와 직접 겹침** | db.py 공유 |

### ⛔ 재작업 금지 (이미 해결됨 — 건드리지 말 것)
- **차트 렉(EquityChart) = PR #117 머지·배포 완료** (web 전용, 서버 무관). 백테스트 실행/모의적용 시 컴퓨터 프리즈의 직접 원인이었고 끝났다.
- **PR #75/#76 egress 수정**(tag-first ETag·DB projection·CORS `expose_headers=["ETag"]`)은 **현재 코드에 실재·작동 검증됨**. `/sync/snapshot`·`/trading/timeline`·`/sync/timeline`은 이미 304 최적화돼 있으니 재구현 말 것. (#5는 그 최적화가 **안 걸린** 나머지 엔드포인트 얘기다.)

### 🔒 공통 제약 두 가지
1. **머지 = Railway 재시작 + (인덱스/pruning은) DB 부하.** 서버 변경은 **무거래 창에서만 머지**(US 마감 ~05:00 KST 후 ~KR 개장 전, 또는 주말). main 푸시는 docs만 바꿔도 재배포되니 doc PR도 동일.
2. **#6은 `db.py`로 무발주 클러스터(Neon 끊김)와 직접 겹친다.** 이미 설계 중이면 그쪽에 통합, 아니면 아래 설계안 채택.

### 기존 작업물
- worktree `_wt-infra` / branch `fix/server-infra-pruning-indexes` (origin/main 기준) 생성됨. **Batch A(#1~3) 착수 직전(코드 미작성).** 일괄로 가져가면 이 브랜치는 폐기 가능.

---

## #1 — 무한 성장 테이블 + pruning cron 부재

**문제 인식.** `HeartbeatEvent`·`SyncSnapshot`이 정리 로직 없이 무한 누적된다. row가 쌓일수록 최신조회 정렬비용·full-row egress가 시간에 비례 증가 → **2026-06-10 egress 쿼터 초과 인시던트를 서서히 재유발하는 구조**.

**핵심 원인.**
- `main.py` 전체에 `DELETE`/`cleanup`/pruning cron **0매치**(grep 확인). 유일 DELETE는 `db.py`의 orphan BacktestRun 1회뿐.
- `HeartbeatEvent`: `scheduler.py`의 `*/5` heartbeat cron → **기기당 288행/일·~105,000행/년**, 절대 안 줄어듦.
- `SyncSnapshot`: 이벤트 push마다(사이클·체결통보·장중손절·킬스위치) 생성, payload는 수백KB JSON.
- **`models.py:105` 주석이 "30일 이상 row는 cleanup cron이 정리한다"고 적혀 있으나 그 cron은 존재하지 않는다 = 거짓 기록.**

**확실한 해결방안.**
- **일 1회 pruning cron 추가** (scheduler.add_job, 예: 새벽 04:00 무거래 시각):
  - `DELETE FROM heartbeatevent WHERE at < now() - interval '30 days'` (timeline window는 24h만 보므로 안전).
  - `SyncSnapshot`: ⚠ **user별 최신 1건은 반드시 보존**(preview_engine·/sync/snapshot·/sync/timeline·/trading/timeline·/portfolio가 최신 1건에 의존). 예:
    `DELETE FROM syncsnapshot WHERE received_at < now() - interval 'N days' AND id NOT IN (SELECT max(id) FROM syncsnapshot GROUP BY user_id)`.
- `models.py:105` 주석을 **현실로 수정**(이 cron을 가리키게) — 거짓 기록 해소.

**후속·참고.**
- **첫 실행은 누적분이 크다** → 대량 DELETE의 락/부하 방지 위해 배치(LIMIT) 또는 무거래 창 1회 수동 정리 후 cron 정착.
- #2 인덱스(received_at)가 있어야 이 DELETE도 빠르다 → **#2를 먼저**.
- pruning은 데이터 엔진/모니터링 모듈 경계 — `routers/{trading,sync}.py` 소비처가 최신 1건 의존함을 깨지 말 것.

---

## #2 — 인덱스 공백 (received_at · status)

**문제 인식.** 폴링 핫경로(최신 snapshot 조회·pending command 조회)가 인덱스 없이 정렬/필터 풀스캔. #1의 무한 성장과 곱해져 렉·egress가 누적 악화.

**핵심 원인.**
- `SyncSnapshot`(models.py:91-97): `user_id`만 인덱스. **`received_at` 인덱스 없음** → "최신 1건"이 `ORDER BY received_at DESC LIMIT 1`인데 매번 정렬(sync.py:299·471·497, trading.py:505, portfolio.py:181·206, preview_engine.py 등 7곳).
- `Command`(models.py:217-234): `device_id`·`user_id` 인덱스. **`status` 인덱스 없음** → pending 폴링 `WHERE device_id=? AND status='pending'`(commands.py:127·189)이 device행 좁힌 뒤 status 스캔.

**확실한 해결방안.**
- 복합 인덱스 2개:
  - `(user_id, received_at DESC)` on `syncsnapshot` → 최신 1건 O(log n).
  - `(device_id, status)` on `command`.
- **구현 위치 = `db.py` `_migrate()`** (기존 `_NEW_COLS`/`_ensure_column` 패턴 미러): `_NEW_INDEXES` 리스트 + `_ensure_index` 헬퍼로 `CREATE INDEX IF NOT EXISTS`. SQLite/PG 양쪽 멱등. (SQLModel `Index`를 model에 박아도 `create_all`은 기존 테이블에 인덱스를 안 만들므로 명시 DDL 필요.)

**후속·참고.**
- 프로덕션 테이블 인덱스 생성 = 락/부하 → **무거래 창**. PG `CREATE INDEX CONCURRENTLY` 고려(단 트랜잭션 밖에서만 — `_migrate`의 `engine.begin()` 블록 안에선 불가, 별도 autocommit 연결 필요).
- 검증: `test_db_retry.py`/`_migrate` 패턴 따라 SQLite 단위테스트 + PG는 `railway run python -c "...EXPLAIN..."`로 인덱스 사용 확인.

---

## #3 — 스케줄러 폭주 가드 부재

**문제 인식.** 무거운 cron이 밀리면 조용히 drop(preview 누락 잠재) + 실패 재시도 job이 원 cron과 **중복 동시 실행** 가능.

**핵심 원인.**
- `main.py:542 BackgroundScheduler(timezone="Asia/Seoul")`에 `job_defaults`/`max_instances`/`coalesce`/`misfire_grace_time` **전무**(grep 0). APScheduler 기본 `misfire_grace_time`이 매우 짧아(≈1s), GIL/I/O로 1s+ 밀린 무거운 job(naver 15분·dataset 수십분)이 **misfire로 조용히 drop**될 수 있음.
- `_run_with_retry`(main.py:47-87)가 retry job에 **매번 다른 id**(`retry_{name}_{attempt}`) 부여 → `max_instances=1`(job별)이 **원 정시 cron 본체와 진행 중 retry의 동시 실행을 못 막음** → 같은 `_refresh_*`가 외부 소스를 중복 fetch할 창.

**확실한 해결방안.**
- `BackgroundScheduler(timezone="Asia/Seoul", job_defaults={"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1})` — 한 줄로 16개 cron 일괄 보호.
- retry 중복 진입: retry job에 **고정 id**(`retry_{name}`, `replace_existing=True`) 부여하거나, `_refresh_*`에 `threading.Lock`(`acquire(blocking=False)`)로 동일 작업 동시 진입 차단.

**후속·참고.** 저위험·소변경. 단 `_run_with_retry`의 "정시 cron이 기존 retry 큐 cancel" 동작(main.py:54-60)을 깨지 말 것.

---

## #4 — 메모리 1.3GB 상주 + 9.4GB 폴백 OOM 위험

**문제 인식.** 단일 uvicorn 워커(Dockerfile: `--workers` 없음)에 모든 인메모리 캐시가 한 RSS로 누적. 폴백 경로 1줄이 전 유니버스 9.4GB 빌드를 트리거 → **OOM kill → preview 전체 실패(매수 후보 누락)**.

**핵심 원인.**
- `data_cache.py:26,84` — raw OHLCV dict **~1.3GB 상주**(설계상 정상), full 45컬럼 dataset은 **~9.4GB**.
- `preview_engine.py:394` `_preview_dataset`: 컬럼 결정 불가 시 `return get_dataset()` 폴백 = **전 유니버스 9.4GB 빌드**. "결정 불가 전략 1개가 전 유저 preview를 9.4GB로" fan-out.
- Railway 인스턴스 메모리 등급·실 RSS·OOM 발생 이력 **미확인**(추정). railway status는 `plan: pro`.

**확실한 해결방안.**
- **ⓐ 진단 선행(필수)**: `railway logs | grep -iE "oom|killed|out of memory"` + Railway 메모리 메트릭/등급 확인 → P0인지 P1인지 확정. (OOM 흔적 있으면 즉시 등급 상향 또는 폴백 격리 우선.)
- **ⓑ 폴백 격리**: `_preview_dataset`의 `get_dataset()` 전체 빌드 폴백을 `get_projected(ALL_KNOWN_COLS, symbols=union_syms)` 또는 **해당 전략만 skip+경고**로 → 9.4GB fan-out 차단.

**후속·참고.** raw 1.3GB 상주 자체를 낮추는 건(부분집합·디스크맵) 더 큰 별도 작업. 우선은 폴백 격리 + 등급 확인.

---

## #5 — Monitor 15s 폴링 3/4 엔드포인트 ETag 없음

**문제 인식.** Monitor 페이지가 열려 있는 동안 15s마다 4개 GET 동시 발사 중 **3개가 무조건 full body 240회/hr/탭** → egress 잔존 표면(PR#75/#76이 닫은 snapshot/timeline 효과를 부분 상쇄).

**핵심 원인.**
- `Monitor.tsx:32-36` `load()`가 15s마다 `snapshot + devices + listCommands + marketContext` 4-fan-out(`Promise.all`). snapshot만 tag-first 304.
- `auth.py`(/auth/devices)·`commands.py`(/sync/commands)·`market.py`(/market/context) 핸들러에 **ETag 미발급**(market.py ETag grep 0).
- 가시성/백오프 가드 부재 — 장외·백그라운드 탭에서도 동일 주기.

**확실한 해결방안.**
- **ⓐ web만(충돌 무관·즉시 가능)**: `document.hidden` 시 폴링 일시중단 + devices/commands를 snapshot과 분리해 주기 ↑(예: 60s).
- **ⓑ 서버**: market/devices/commands에 tag-first ETag(PR#75 패턴: scalar 먼저 → If-None-Match 매칭 시 304, payload 미SELECT). ⚠ `commands.py`는 자동매매 SSE/명령 버스 인접 → 신중·테스트 동반.

**후속·참고.** web 부분(ⓐ)은 #6 등과 무관하게 먼저 머지 가능. 서버 ETag는 라우터별 회귀테스트(`test_timeline_egress.py` 패턴).

---

## #6 — `server conn crashed?` (Neon idle-suspend stale 연결) ★무발주 클러스터와 직접 겹침

**문제 인식.** 프로덕션에서 간헐 500(`/sync/*` GET) + 로그 노이즈. **2026-06-11 라이브 로그에서 실측**(`psycopg.ProtocolViolation: server conn crashed?` ASGI 예외). 로컬앱 재폴링으로 self-heal이라 **현 심각도는 낮음**, 단 latent risk + 로그 오염.

**핵심 원인 (정밀 분석됨).**
1. Neon 서버리스가 유휴 시 compute suspend하며 풀의 연결을 죽임 → 다음 checkout/쿼리에서 stale 소켓 → `ProtocolViolation`.
2. **`pool_pre_ping`이 못 막음**: Neon `-pooler`(PgBouncer)에선 pre_ping의 `SELECT 1`과 실제 쿼리가 **다른 백엔드 연결**로 라우팅될 수 있어, ping 통과해도 쿼리 시점 끊김 잔존. (**`db.py:150-155`에 우리 코드가 이미 이 한계를 주석으로 문서화.**)
3. **커버리지 갭이 500의 직접 원인**: `call_with_disconnect_retry`(끊김→pool 폐기→1회 재시도)의 **프로덕션 호출자는 `main.py:120`(preview cron) 단 1곳뿐**(grep 확인). 요청 핸들러(`get_session`)·나머지 cron은 무방비 → stale 쿼리가 unhandled 500.
4. **가장 중요한 preview cron은 이미 보호됨** → 무발주(매수 후보결정)엔 직접 영향 적음. 영향은 `/sync/*` GET의 간헐 500(클라 재시도로 흡수).

**❌ 정정 (감사 초기 오류):** 이 증상을 `connect_timeout`과 묶었던 건 틀렸다. `connect_timeout`은 *새 연결 수립 hang*(Neon 깨어나기 지연·blackhole = 별개 모드)을 위한 것이고, `server conn crashed?`는 *이미 풀에 있던 연결이 죽은* 부류다. **이 버그엔 connect_timeout이 해결책이 아니다.**

**해결방안 (방향 2개 — 미설계·미검증, 결정 필요).**
- **(A) 근본 — Neon 직결 엔드포인트 사용**: pre_ping이 깨지는 *원인*이 PgBouncer이므로, `-pooler` 대신 직결 엔드포인트면 ping·query 같은 백엔드라 **pre_ping 정상 작동 → 끊김 자체 소멸**. 트레이드오프: 직결은 연결 수 상한 낮음(단일 워커+작은 SQLAlchemy 풀이라 괜찮을 가능성 — Neon 콘솔 상한 확인 필요). **DB_URL/Neon 대시보드/Railway env 변경**(인프라 손).
- **(B) 보강 — 요청 seam 재시도 커버리지**: 멱등 GET(`/sync/*`은 읽기)에 한해 미들웨어가 끊김 감지 시 pool 폐기+1회 재시도. **한 seam에서 부류 닫음**(scattered try/except 금지 = over-engineering 회피). POST는 부작용 때문에 제외.
- **권장 조합**: (A)를 root, (B)를 방어선. connect/statement timeout은 **별개**의 blackhole hardening(원하면 추가).

**후속·참고.**
- **이게 무발주 deep-dive의 "Neon 연결 끊김" 근본원인 클러스터와 직접 겹친다.** 이미 (A)/(B)를 설계 중이면 중복 회피 — 통합할 것.
- 검증: `is_disconnect`/`call_with_disconnect_retry` 단위테스트는 `server/tests/test_db_retry.py`에 이미 있음(패턴 재사용). 의도적 Neon suspend 재현이 가능하면 end-to-end.

---

## 부록 — 권장 순서 · 검증 · 참고 자료

**권장 시퀀스** (충돌·의존 고려): **#2 인덱스 → #1 pruning**(인덱스 후 DELETE 빠름) → **#3 가드**(독립) → **#4 메모리**(진단 선행) → **#5 ETag**(web 먼저) → **#6 Neon**(무발주 작업과 통합).

**검증 신호.**
- 서버 단위테스트: `cd server && pytest`(특히 `test_db_retry.py`·`test_timeline_egress.py`·`test_preview_stale_gate.py` 패턴).
- 멱등 마이그레이션: `_migrate`의 `_ensure_column` 방식대로 SQLite/PG 양쪽 멱등.
- 진단 CLI: `railway logs`(conn crashed·cron·preview), `railway status --json`(commitHash·plan), egress는 `railway run python -c "SELECT 1"`.

**참고 자료.**
- `docs/incidents/2026-06-10-neon-data-transfer-quota.md` — egress 쿼터 초과 인시던트 + PR#75/#76 재발방지(ETag/projection/CORS).
- `db.py:16-37`(풀 설정·keepalive), `db.py:150-182`(disconnect 재시도 헬퍼·pre_ping 한계 주석).
- `docs/modules/autotrade-engine.md`(폴링 endpoint 설계 원칙: tag-first ETag·projection).
- `models.py`(테이블·인덱스 정의), `main.py:40-175`(스케줄러·retry·preview trigger)·`:540~648`(cron 등록).

**연락/조율.** 이 6건을 가져가면 브리핑 로그에 인계 사실 남기고, `_wt-infra`/`fix/server-infra-pruning-indexes` 브랜치는 중복이므로 폐기 가능. 차트 렉(#117)·PR#75/#76 egress 최적화는 **이미 끝났으니 재작업 말 것**.
