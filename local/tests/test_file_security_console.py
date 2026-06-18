"""file_security: Windows icacls 호출이 콘솔창을 띄우지 않도록 CREATE_NO_WINDOW 보장.

콘솔 없는 GUI 앱(MyStock)이 save_json(자동매매 사이클·체결마다 ledger/equity/pending
재보안)으로 icacls를 반복 호출할 때 콘솔창이 깜빡이던 회귀의 가드. creationflags에
CREATE_NO_WINDOW(0x08000000)가 빠지면 이 테스트가 실패한다.
"""
import localapp.file_security as fs

CREATE_NO_WINDOW = 0x08000000


def test_icacls_uses_create_no_window(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw

        class _R:           # subprocess.run 반환 모의(본 코드는 반환값 미사용)
            returncode = 0

        return _R()

    monkeypatch.setattr(fs.sys, "platform", "win32")    # 호스트 무관 — 윈도 경로 강제
    monkeypatch.setattr(fs.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "tester")

    p = tmp_path / "secret.json"
    p.write_text("{}", encoding="utf-8")
    fs.restrict_to_owner(p)

    assert captured["cmd"][0] == "icacls"
    assert captured["kw"].get("creationflags", 0) & CREATE_NO_WINDOW, \
        "icacls는 CREATE_NO_WINDOW로 콘솔창 깜빡임을 막아야 한다"


def test_no_icacls_when_file_absent(monkeypatch, tmp_path):
    # 존재하지 않는 파일은 무동작 — icacls 호출 자체가 없어야(불필요한 자식 spawn 방지).
    called = {"n": 0}
    monkeypatch.setattr(fs.sys, "platform", "win32")
    monkeypatch.setattr(fs.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    fs.restrict_to_owner(tmp_path / "nope.json")
    assert called["n"] == 0
