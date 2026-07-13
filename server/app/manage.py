"""수동 데이터 갱신 CLI — **로컬 개발 전용**. 스케줄(cron)을 안 기다리고 갱신.

새 데이터 소스를 붙이거나, 로컬에서 스크리너/preview를 실데이터로 검증할 때 사용.

사용 (로컬에서):
    python -m app.manage status          # 디스크 데이터 인벤토리 (진행확인)
    python -m app.manage us              # 미국 일괄: 시드 → OHLCV → 시총 → metrics
    python -m app.manage us --limit 10   # 앞 10종목만 (빠른 검증)
    python -m app.manage master|krx|naver|technical|global|kr|us_caps|us_metrics|all

⚠ 주의 (꼭 읽기)
1. **라이브 서버엔 부적합**: 모든 캐시(krx/us_metrics/data_cache/...)는 프로세스별
   인메모리 상태다. 별도 프로세스로 실행하면 디스크는 갱신해도 **돌아가는 서버의
   인메모리 metrics는 안 바뀐다**(재시작 전까지). 배포 서버는 cron이 갱신한다.
2. **krx 순서**: krx_cache.refresh()는 metrics를 통째 교체해 NAVER/technical 머지
   (PER/PBR/RSI)를 파괴한다. 단독 `krx`를 naver/technical 뒤에 돌리지 말 것.
   `all`은 안전 순서(krx→naver→technical)로 실행한다.
3. **API rate-limit**: 기존 함수의 페이싱만 적용된다. NAVER 등 대량 단시간 호출은
   여전히 차단될 수 있으니 `all`·반복 실행을 남발하지 말 것.
"""

from __future__ import annotations

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("manage")


def _ensure_master() -> None:
    from . import kis_master_cache
    if not kis_master_cache.get_master_set():
        log.info("KIS 마스터 로드 중...")
        kis_master_cache.refresh()


def cmd_us(limit: int | None = None) -> None:
    """미국 데이터 일괄: 마스터 → S&P500 시드 → OHLCV → 시총 → metrics."""
    from quant_core import data_fetcher
    from . import data_cache, main, us_metrics_cache

    _ensure_master()
    n_seed = main._seed_sp500_overseas()
    n_etf = main._seed_us_master_overseas()
    log.info("S&P500 시드: %d종목 · 미국 마스터 전체(주식+ETF) 시드: %d종목", n_seed, n_etf)

    n = data_fetcher.fetch_managed_overseas(limit=limit, verbose=True)
    log.info("해외 OHLCV fetch: %d종목 시도", n)
    data_cache.invalidate()

    log.info("미국 시가총액(fast_info) 수집...")
    us_metrics_cache.refresh_market_caps(timeout_each=0.2, limit=limit)
    r = us_metrics_cache.refresh()
    log.info("us_metrics 빌드 완료: %s", r)


def cmd_us_caps(limit: int | None = None) -> None:
    from . import us_metrics_cache
    us_metrics_cache.refresh_market_caps(timeout_each=0.2, limit=limit)
    log.info("us_metrics 재빌드: %s", us_metrics_cache.refresh())


def cmd_us_metrics(limit: int | None = None) -> None:
    from . import us_metrics_cache
    log.info("us_metrics 재빌드: %s", us_metrics_cache.refresh())


def cmd_status() -> None:
    """디스크 데이터 인벤토리 — 무엇이 얼마나 적재됐는지 진행확인.

    인메모리 캐시는 프로세스별이라 새 프로세스에선 비어 있으므로, 디스크에
    영속된 사실(parquet·관리목록·시총캐시)을 보고한다.
    """
    from quant_core import data_fetcher
    from . import us_metrics_cache

    dd = data_fetcher.DATA_DIR
    n_parquet = len(list(dd.glob("*.parquet"))) if dd.exists() else 0
    sp_codes = data_fetcher.sp500_yf_codes()
    have = 0
    for c in sp_codes:
        try:
            if data_fetcher._parquet_path(c).exists():
                have += 1
        except Exception:
            pass
    caps = us_metrics_cache._load_caps()
    print("=== 디스크 데이터 인벤토리 (로컬) ===")
    print(f"  parquet 파일 총수:     {n_parquet}")
    print(f"  S&P500 유니버스:       {len(sp_codes)}")
    print(f"  S&P500 OHLCV 적재:     {have}/{len(sp_codes)}")
    print(f"  managed_overseas:      {len(data_fetcher.load_managed_overseas())}")
    print(f"  managed_kr:            {len(data_fetcher.load_managed_kr_codes())}")
    print(f"  us_market_caps(시총):  {len(caps)}")


def cmd_prune_orphans(apply: bool = False) -> None:
    """유니버스 밖 orphan parquet 정리 — **기본 dry-run**(목록만 출력, 삭제 안 함).

    옛 오종목('/'→'_' 파일 AACT_U.parquet 등)이 볼륨에 영구 잔존해 매 번들에 실리는 낭비를
    없앤다. 보존 집합은 write 경로(_parquet_path)와 동일 규칙이라 매크로·실티커·별칭·서브디렉터리
    는 자동 보존. 실제 삭제는 `--apply` 명시(안전). 라이브 서버 볼륨에서 실행 가능(명단 무관·파일만)."""
    from quant_core import data_fetcher

    orphans = data_fetcher.find_orphan_parquets()
    total_bytes = 0
    print(f"=== orphan parquet {'삭제' if apply else 'DRY-RUN(삭제 안 함)'} — {len(orphans)}개 ===")
    for p in orphans:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        total_bytes += sz
        print(f"  {'DEL' if apply else 'would-del'}  {p.name}  ({sz:,}B)")
    print(f"  합계 {total_bytes:,}B ({total_bytes / 1e6:.1f}MB) · 유니버스 키 {len(data_fetcher.iter_universe_keys())}개 보존")
    if not apply:
        print("  → 실제 삭제하려면 --apply 를 붙이세요.")
        return
    deleted = 0
    for p in orphans:
        try:
            p.unlink()
            deleted += 1
        except OSError as e:
            log.warning("삭제 실패(건너뜀): %s — %s", p.name, e)
    log.info("orphan parquet 삭제 완료: %d/%d", deleted, len(orphans))
    if deleted:
        log.info("데이터 세대 마커 bump: %d", data_fetcher.mark_data_dirty())


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="수동 데이터 갱신 CLI")
    ap.add_argument("target", choices=[
        "status", "master", "krx", "naver", "technical", "global", "kr",
        "us", "us_caps", "us_metrics", "all", "mark-dirty", "prune-orphans"])
    ap.add_argument("--limit", type=int, default=None,
                    help="미국 종목 앞 N개만 (개발/검증용)")
    ap.add_argument("--apply", action="store_true",
                    help="prune-orphans: dry-run 대신 실제 삭제")
    args = ap.parse_args()

    if args.target == "status":
        cmd_status()
        return

    if args.target == "prune-orphans":
        cmd_prune_orphans(apply=args.apply)
        return

    if args.target == "mark-dirty":
        # 수동 파일 편집·삭제 후 라이브 서버 캐시가 자가 리로드하도록 세대 마커 bump.
        from quant_core import data_fetcher
        log.info("데이터 세대 마커 bump: %d", data_fetcher.mark_data_dirty())
        return

    from . import main as srv

    if args.target == "master":
        srv._refresh_kis_master()
    elif args.target == "krx":
        log.warning("⚠ 단독 krx는 NAVER/technical 머지(PER/PBR/RSI)를 덮어씁니다. "
                    "당일 naver/technical 이후라면 'all'을 쓰세요(안전 순서).")
        srv._refresh_krx()
    elif args.target == "naver":
        srv._refresh_naver()
    elif args.target == "technical":
        srv._refresh_technical()
    elif args.target == "global":
        srv._refresh_global_dataset()
    elif args.target == "kr":
        srv._refresh_kr_dataset()
    elif args.target == "us":
        cmd_us(args.limit)
    elif args.target == "us_caps":
        cmd_us_caps(args.limit)
    elif args.target == "us_metrics":
        cmd_us_metrics(args.limit)
    elif args.target == "all":
        srv._refresh_kis_master()
        srv._refresh_krx()
        srv._refresh_naver()
        srv._refresh_technical()
        srv._refresh_global_dataset()
        srv._refresh_kr_dataset()
        cmd_us(args.limit)
    log.info("완료: %s", args.target)


if __name__ == "__main__":
    main_cli()
