"""Parquet 원자적 쓰기·안전 읽기 — 손상(잘린 write) 파일로부터 데이터 로드를 보호.

`df.to_parquet(최종경로)` 직접 쓰기는 쓰는 도중 프로세스가 죽으면(재배포·OOM·
컨테이너 재시작) footer 없는 잘린 파일을 남긴다. 이후 `read_parquet`가
ArrowInvalid("Parquet magic bytes not found in footer")로 터지고, 그 파일을 읽는
전체 데이터 로드(load_all → 백테스트·preview·전략저장)가 통째로 무너졌다.

이 모듈이 그 부류를 두 겹으로 닫는다:
  - write_parquet_atomic : 임시파일에 쓴 뒤 os.replace로 교체 → 손상 자체를 차단(근본).
  - read_parquet_safe    : 손상 파일을 만나면 격리·자가복구 → 한 파일이 전체를 못 죽이게(blast-radius).

os·tempfile·pandas에만 의존(quant_core 다른 모듈 import 없음) → 어디서든 순환 없이 import.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


# Windows 예약 장치명 — 파일명으로 못 쓴다(확장자 무관: "CON.parquet"도 콘솔 장치로 취급).
# 데이터엔진은 Linux라 이런 이름도 정상 저장하지만, 그 parquet을 tar 번들로 받아 푸는
# Windows 로컬앱에선 추출이 [WinError]로 깨진다 → 그 지점 이후 멤버가 통째로 유실.
# (2026-07-13 실유저 무발주 인시던트: 실재 티커 "CON"(Concentra)·"PRN"이 공유 번들에 실려
#  한 유저의 dataset 추출 전체를 중단시켜 발주 0. 재발 방지 = 이 이름을 클라이언트에 안 보낸다.)
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# Windows 파일명 금지문자(기존 '/'만 치환하던 것을 전 금지문자로 확장).
_WINDOWS_ILLEGAL_CHARS = '<>:"/\\|?*'


def sanitize_fs_name(stem: str) -> str:
    """심볼(파일 stem)을 모든 OS에서 안전한 파일명 stem으로 정규화한다.

    **정상 이름은 그대로(byte-identical)** — 오직 Windows 예약장치명·금지문자·말미 점/공백만
    결정적으로 remap한다. write·read·bundle 세 경로가 이 한 함수를 SSOT로 공유하므로
    원본↔안전키가 1:1로 유지된다(별도 매핑 테이블 불필요).
    예: "AAPL"→"AAPL", "코스피200선물"→"코스피200선물", "BRK/B"→"BRK_B",
        "CON"→"CON_", "PRN"→"PRN_", "005930"→"005930".
    """
    safe = stem
    for ch in _WINDOWS_ILLEGAL_CHARS:
        if ch in safe:
            safe = safe.replace(ch, "_")
    safe = safe.rstrip(" .")                 # Windows는 말미 점·공백을 잘라 충돌·유실 유발
    if safe.upper() in _WINDOWS_RESERVED_STEMS:
        safe = safe + "_"                    # 확장자 무관 예약어라 stem 자체를 무력화
    return safe or "_"                       # 전부 제거된 극단 방어(정상 심볼엔 도달 안 함)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """parquet을 원자적으로 저장: 같은 디렉터리 임시파일에 쓴 뒤 os.replace로 교체.

    쓰는 도중 프로세스가 죽어도 최종 경로엔 항상 완결된 파일만 남는다(중단 시 임시파일만
    버려짐). 동일 파일시스템 rename이라 os.replace는 원자적이다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    os.close(fd)
    try:
        df.to_parquet(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def quarantine_corrupt(path: Path, err: Exception) -> None:
    """손상된 parquet를 {name}.corrupt로 격리하고 명시 로그를 남긴다(조용히 무시 금지).

    격리된 파일은 load_all이 정확 경로({sym}.parquet)만 읽으므로 더는 로드를 깨지 않고,
    다음 갱신이 write_parquet_atomic으로 같은 경로를 재수급한다. .corrupt는 포렌식용으로
    남되 심볼당 최대 1개(os.replace가 기존 것을 덮어씀)."""
    bad = path.parent / (path.name + ".corrupt")
    try:
        os.replace(path, bad)
        moved = bad.name
    except OSError:
        moved = "(이동 실패)"
    print(f"[parquet] 손상 격리: {path.name} -> {moved} "
          f"({type(err).__name__}: {err})", flush=True)


def read_parquet_safe(path: Path) -> pd.DataFrame | None:
    """parquet을 읽되 손상 파일은 격리하고 None을 반환(예외를 전파하지 않음).

    근본 원인(비원자적 write)은 write_parquet_atomic으로 차단했고, 이 함수는 이미
    손상된 파일이나 외부 요인 손상을 만났을 때 전체 데이터 로드가 무너지지 않도록
    격리·자가복구한다. None은 호출자가 '해당 심볼 없음'으로 처리한다."""
    try:
        return pd.read_parquet(path)
    except Exception as e:               # noqa: BLE001 — 손상 파일 격리 후 계속
        quarantine_corrupt(path, e)
        return None
