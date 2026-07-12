"""SSE keepalive 래퍼 — 챗 침묵 UX·유휴 끊김 부류 회귀 가드.

LLM 라운드(로컬 $0 shim은 라운드당 수 분) 동안 SSE 이벤트가 0이라 유휴 연결이 끊기고
network error로 표면화되며, 끊긴 뒤 서버 턴은 다음 yield에서 중단돼 완성 직전 분석이
유실되던 부류(실측 conv#4/6/7). 전송층 한 곳에서 마감:
① 유휴 interval마다 SSE 주석(': ka') → 연결 생존  ② producer 스레드는 소비자와
무관하게 턴 완주·영속(끊긴 사용자는 대화 재열람으로 완성 답변 수신).
"""
import asyncio
import time

from app.routers.chat import _stream_with_keepalive


def _collect(agen):
    async def run():
        out = []
        async for chunk in agen:
            out.append(chunk)
        return out
    return asyncio.run(run())


def test_keepalive_emitted_while_idle():
    def slow_gen():
        yield ("delta", {"text": "a"})
        time.sleep(0.25)              # LLM 라운드 침묵 모사
        yield ("done", {"parts": []})

    out = _collect(_stream_with_keepalive(slow_gen(), interval=0.05))
    assert [c for c in out if c.startswith(":")], "유휴 구간에 keepalive 주석이 흘러야 한다"
    events = [c.split("\n")[0] for c in out if c.startswith("event:")]
    assert events[0] == "event: delta"
    assert events[-1] == "event: done"


def test_fast_stream_passes_through_in_order_without_keepalive():
    def fast_gen():
        yield ("delta", {"text": "x"})
        yield ("tool_use", {"id": "t1", "name": "simulate", "input": {}})
        yield ("progress", {"label": "모델 응답 생성 중 (라운드 2)"})
        yield ("done", {"parts": []})

    out = _collect(_stream_with_keepalive(fast_gen(), interval=5.0))
    events = [c.split("\n")[0] for c in out if c.startswith("event:")]
    assert events == ["event: delta", "event: tool_use", "event: progress", "event: done"]
    assert not [c for c in out if c.startswith(":")]
    assert "event: done\ndata: {}" in [c.strip() for c in out if c.startswith("event: done")][0] + ""


def test_producer_completes_after_consumer_stops():
    """클라이언트 끊김(소비 중단)에도 producer는 턴을 끝까지 실행 — 결과 유실 부류 마감."""
    finished = {"v": False}

    def gen():
        yield ("delta", {"text": "a"})
        yield ("delta", {"text": "b"})
        time.sleep(0.05)              # 남은 작업 모사
        finished["v"] = True

    async def run():
        agen = _stream_with_keepalive(gen(), interval=0.05)
        async for _ in agen:
            break                     # 첫 청크만 읽고 이탈(클라 끊김 모사)
        await agen.aclose()

    asyncio.run(run())
    for _ in range(200):              # producer 스레드 완주 대기(최대 ~2s)
        if finished["v"]:
            break
        time.sleep(0.01)
    assert finished["v"], "소비자가 끊겨도 producer는 완주해야 한다(영속 보존)"
