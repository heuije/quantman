## 주요 변경 — Hotfix: catch-up 두 회귀 버그 동시 fix

v0.9.1-beta에서 Phase 7 catch-up을 처음 추가할 때 두 가지 회귀가 들어갔고,
v0.9.2/v0.9.3은 다른 fix만이라 그대로 남아 있었음. **사용자 자동매매 catch-up
경로가 NameError·TypeError로 무력화**되던 상태. 미장 후보 6건 있어도 매수 0건.

### 🐞 버그 1 — `catchup` 파라미터 chain 전파 누락 (NameError)

증상: catch-up이 미장 cycle 호출 시
```
NameError: name 'catchup' is not defined
File "localapp\trader.py", line 1231, in _cycle_body
```

원인: v0.9.1-beta 때 `Trader.cycle()`에만 `catchup=False` 파라미터를 추가했고,
내부 호출 chain(`cycle` → `_cycle_locked` → `_cycle_body`)에 전파 누락.
`_cycle_body` 안에서 `catchup` 변수를 사용했지만 local scope에 없어 NameError.

Fix ([trader.py:1035-1054](localapp/trader.py)):
- `_cycle_locked(strategies, ..., catchup: bool = False)` 시그니처 추가
- `_cycle_body(strategies, ..., catchup: bool = False)` 시그니처 추가
- `cycle` → `_cycle_locked` → `_cycle_body` chain에서 `catchup=catchup`로 전파

### 🐞 버그 2 — `IntradayStopManager` 필수 인자 누락 (TypeError)

증상: catch-up이 미장 stop-loss 호출 시
```
TypeError: IntradayStopManager.__init__() missing 1 required positional argument: 'submit_sell_fn'
File "localapp\catchup.py", line 384, in _catchup_stop_loss
```

원인: `IntradayStopManager.__init__(self, broker, get_ledger, get_strat_def,
submit_sell_fn, dataset=None)` — 4번째 인자 `submit_sell_fn` 필수인데
catchup.py에서 3개만 전달.

Fix ([catchup.py:384](localapp/catchup.py)):
- `IntradayStopManager(broker, lambda: trader.ledger, get_strat_def,
  trader._submit_sell)`로 4번째 인자 명시. trader._submit_sell이 over-sell
  방지 + intent journal + sold_today 처리를 모두 담당.

### 사용자 영향

- v0.9.1~v0.9.3 사용자: 자동매매 시작 후 PC가 꺼져 있던 동안 missed된 미장
  cycle을 catch-up이 자동 보완하지 못해 매매 기회 누락.
- **v0.9.4-beta 업데이트 후 첫 자동매매 시작 시**: 강화된 catch-up이 정상
  동작 → 시초가 limit 변환 매수 + 손절선 즉시 체크.

### 자동 업데이트

v0.9.3-beta의 강화된 updater가 본 release 감지 → amber 배너 → [지금 업데이트]
클릭 1회로 v0.9.4-beta 자동 적용. PID kill·잠금 폴링·robocopy retry로
race condition 안전.

### 내부 변경

**[localapp/trader.py](localapp/trader.py)**:
- `_cycle_locked`, `_cycle_body` 시그니처에 `catchup` 추가
- `cycle` → chain 전파 명시

**[localapp/catchup.py](localapp/catchup.py)**:
- `IntradayStopManager` 호출 시 `trader._submit_sell` 4번째 인자 명시

**[localapp/__init__.py](localapp/__init__.py)**:
- 버전 0.9.3-beta → 0.9.4-beta
