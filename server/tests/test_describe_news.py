"""B2 뉴스 — 360 단일 리포트 '왜 움직였나' facet의 서버 엣지 enrich 회귀 차단.

뉴스는 라이브 네트워크·env키 의존이라 결정적 엔진 밖(서버)에서 붙인다. 종목명은 /symbols와
동일 출처(kis_master_cache)로 해석, 미해석·키 미설정이면 빈 리스트(가짜 채움 0). 단일
리포트(report="single")가 아닌 결과(포트 진단·select·extremize)는 무변경이어야 한다.

    cd platform/server && pytest tests/test_describe_news.py -v
"""
import pytest

ir = pytest.importorskip("app.routers.ir")


def test_single_report_attaches_news(monkeypatch):
    """단일 360 리포트 → 종목명 해석 후 뉴스가 붙는다."""
    monkeypatch.setattr(ir.kis_master_cache, "get_name",
                        lambda c: "삼성전자" if c == "005930" else "")
    seen = {}
    fake = [{"title": "삼성전자 신고가", "link": "http://x", "desc": "", "pub": None}]
    monkeypatch.setattr(ir.news_kr, "fetch_news",
                        lambda name, display=10: (seen.update(name=name), fake)[1])

    out = {"report": "single", "symbol": "005930", "fundamentals": {}}
    ir._attach_symbol_news(out)

    assert seen["name"] == "삼성전자"        # /symbols 출처로 코드→사명 해석
    assert out["news"] == fake


def test_non_single_report_unchanged(monkeypatch):
    """포트 진단 등 단일 리포트가 아니면 뉴스 키를 붙이지 않는다(대상 종목 1개 아님)."""
    monkeypatch.setattr(ir.kis_master_cache, "get_name", lambda c: "삼성전자")
    monkeypatch.setattr(ir.news_kr, "fetch_news", lambda *a, **k: [{"title": "x"}])

    out = {"report": "portfolio", "holdings": []}
    ir._attach_symbol_news(out)

    assert "news" not in out


def test_single_report_attaches_estimates(monkeypatch):
    """단일 360 리포트 → forward 추정실적(FnGuide·estimate_block 위임)이 붙는다(뿌리①)."""
    seen = {}
    monkeypatch.setattr(ir.estimate_kr, "estimate_block",
                        lambda code, last_price=None: (seen.update(code=code, price=last_price),
                                                       {"source": "FnGuide 컨센서스",
                                                        "forward": {"forward_pe": 9.0}})[1])
    out = {"report": "single", "symbol": "005930", "price": {"last": 80000}}
    ir._attach_symbol_estimates(out)
    assert seen == {"code": "005930", "price": 80000}      # 코드·현재가 전달(forward PER용)
    assert out["estimates"]["forward"]["forward_pe"] == 9.0


def test_non_single_report_no_estimates(monkeypatch):
    """단일 리포트가 아니면 추정실적을 붙이지 않는다."""
    monkeypatch.setattr(ir.estimate_kr, "estimate_block", lambda *a, **k: {"forward": {"x": 1}})
    out = {"report": "portfolio"}
    ir._attach_symbol_estimates(out)
    assert "estimates" not in out


def test_single_report_no_estimate_data(monkeypatch):
    """추정실적 데이터 없으면 미부착(가짜 0 금지)."""
    monkeypatch.setattr(ir.estimate_kr, "estimate_block", lambda *a, **k: {})
    out = {"report": "single", "symbol": "999999", "price": {"last": 100}}
    ir._attach_symbol_estimates(out)
    assert "estimates" not in out


def test_single_report_no_name_empty_news(monkeypatch):
    """마스터에 종목명이 없으면 빈 리스트(가짜 채움 0) — 네이버 호출도 안 한다."""
    monkeypatch.setattr(ir.kis_master_cache, "get_name", lambda c: "")
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return [{"title": "should-not-happen"}]

    monkeypatch.setattr(ir.news_kr, "fetch_news", _boom)

    out = {"report": "single", "symbol": "999999"}
    ir._attach_symbol_news(out)

    assert out["news"] == []
    assert called["n"] == 0                    # 이름 없으면 뉴스 조회 자체를 건너뜀
