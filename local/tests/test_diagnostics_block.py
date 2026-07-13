"""로컬 진단 텔레메트리 — diagnostics_block 구조·경로 스크럽·안전정보 검증.

2026-07-13 CON.parquet 인시던트 후속: 유저에게 「진단 정보 복사」를 매번 요청하지 않고도
서버 스냅샷만으로 로컬앱 실패를 원격 진단하기 위한 구조화 신호. 자격증명·유저명은 안 실려야 한다.
"""
from localapp import analytics, datafetch


def test_scrub_removes_user_path_keeps_filename():
    raw = r"[WinError 6] 핸들이 잘못되었습니다: 'C:\Users\agero\.quant-platform\data\CON.parquet'"
    out = analytics._scrub(raw)
    assert "agero" not in out and "Users" not in out      # 유저명·경로 노출 방지
    assert "CON.parquet" in out                           # 진단에 필요한 파일명은 보존


def test_diagnostics_block_failed_bundle_scrubs_and_structures():
    br = {"result": "failed", "error_type": "OSError",
          "error": r"[WinError 6] 'C:\Users\agero\.quant-platform\data\CON.parquet'"}
    d = analytics.diagnostics_block(
        br, {"needed": 129, "loaded": 1, "missing_sample": ["코스피200선물"]})
    assert d["bundle"]["result"] == "failed"
    assert "agero" not in d["bundle"]["error"]            # 스크럽 적용
    assert d["dataset"] == {"needed": 129, "loaded": 1, "missing_sample": ["코스피200선물"]}
    assert "app_version" in d and isinstance(d["recent_errors"], list)


def test_diagnostics_block_ok_bundle():
    d = analytics.diagnostics_block(
        {"result": "ok", "n_files": 24045, "n_failed": 0, "failed_sample": []},
        {"needed": 129, "loaded": 129, "missing_sample": []})
    assert d["bundle"]["n_files"] == 24045 and d["bundle"]["n_failed"] == 0
    assert d["dataset"]["loaded"] == 129


def test_diagnostics_block_empty_inputs():
    d = analytics.diagnostics_block(None, None)
    assert "app_version" in d and "recent_errors" in d
    assert "bundle" not in d and "dataset" not in d       # 없으면 키 자체를 안 넣음


def test_last_bundle_result_default_is_dict():
    assert isinstance(datafetch.last_bundle_result(), dict)
