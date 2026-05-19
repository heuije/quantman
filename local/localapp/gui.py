"""로컬앱 설정 GUI (tkinter).

KIS 자격증명 입력 · 기기 페어링 · 자동매매 시작/중지 · 상태/로그 확인을
하나의 창에서 처리한다. 트레이 상주는 tray.py가 이 창을 감싼다.
"""

from __future__ import annotations

import socket
import threading
import tkinter as tk
import webbrowser
from datetime import date
from tkinter import messagebox, ttk

from . import pairing, secrets_store
from .config import EQUITY_PATH, LEDGER_PATH, PLATFORM_URL
from .logging_setup import setup_logging

_LOG_PATH_NAME = "logs/localapp.log"


def _read_json(path, default):
    import json
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


class SettingsApp:
    """로컬앱 메인 설정 창."""

    def __init__(self):
        setup_logging()
        self.scheduler = None
        self.on_close_to_tray = None     # tray.py가 주입

        self.root = tk.Tk()
        self.root.title("퀀트 플랫폼 — 로컬앱")
        self.root.geometry("520x680")
        self.root.resizable(False, True)
        self._build()
        self.refresh_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 구성 ───────────────────────────────────────────────────────────────

    def _build(self):
        pad = {"padx": 12, "pady": 6}

        # 상태
        st = ttk.LabelFrame(self.root, text="연동 상태")
        st.pack(fill="x", **pad)
        self.status_kis = ttk.Label(st, text="")
        self.status_pair = ttk.Label(st, text="")
        self.status_auto = ttk.Label(st, text="")
        for w in (self.status_kis, self.status_pair, self.status_auto):
            w.pack(anchor="w", padx=10, pady=2)

        # KIS 자격증명
        kf = ttk.LabelFrame(self.root, text="KIS 모의투자 자격증명")
        kf.pack(fill="x", **pad)
        self.e_key = self._labeled_entry(kf, "App Key")
        self.e_secret = self._labeled_entry(kf, "App Secret", show="*")
        self.e_acct = self._labeled_entry(kf, "계좌번호 (예: 50001234-01)")
        ttk.Button(kf, text="저장", command=self._save_kis).pack(anchor="e",
                                                                 padx=10, pady=8)

        # 기기 페어링
        pf = ttk.LabelFrame(self.root, text="플랫폼 계정 연결")
        pf.pack(fill="x", **pad)
        ttk.Label(pf, text=f"플랫폼: {PLATFORM_URL}").pack(anchor="w", padx=10, pady=2)
        self.pair_code = ttk.Label(pf, text="", font=("Segoe UI", 14, "bold"))
        self.pair_code.pack(anchor="w", padx=10, pady=2)
        self.pair_msg = ttk.Label(pf, text="", foreground="#555")
        self.pair_msg.pack(anchor="w", padx=10, pady=2)
        self.btn_pair = ttk.Button(pf, text="기기 페어링 시작", command=self._pair)
        self.btn_pair.pack(anchor="e", padx=10, pady=8)

        # 자동매매
        af = ttk.LabelFrame(self.root, text="자동매매")
        af.pack(fill="x", **pad)
        row = ttk.Frame(af)
        row.pack(fill="x", padx=10, pady=8)
        self.btn_toggle = ttk.Button(row, text="자동매매 시작",
                                     command=self._toggle_auto)
        self.btn_toggle.pack(side="left")
        self.btn_cycle = ttk.Button(row, text="지금 한 번 실행",
                                    command=self._run_once)
        self.btn_cycle.pack(side="left", padx=8)
        self.cycle_msg = ttk.Label(af, text="", foreground="#555")
        self.cycle_msg.pack(anchor="w", padx=10, pady=2)

        # 로그
        lf = ttk.LabelFrame(self.root, text="활동 로그")
        lf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(lf, height=8, font=("Consolas", 8),
                                state="disabled", wrap="none")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(lf, text="새로고침", command=self.refresh_status).pack(
            anchor="e", padx=10, pady=4)

    def _labeled_entry(self, parent, label, show=None):
        ttk.Label(parent, text=label).pack(anchor="w", padx=10, pady=(6, 0))
        e = ttk.Entry(parent, show=show or "")
        e.pack(fill="x", padx=10)
        return e

    # ── 상태 갱신 ─────────────────────────────────────────────────────────────

    def refresh_status(self):
        kis = secrets_store.load_kis()
        dev = secrets_store.load_device_token()
        running = bool(self.scheduler and self.scheduler.running)

        self.status_kis.config(
            text=("KIS 자격증명: 등록됨" if kis else "KIS 자격증명: 없음 (입력 필요)"))
        self.status_pair.config(
            text=("기기 페어링: 완료" if dev else "기기 페어링: 안 됨"))
        self.status_auto.config(
            text=f"자동매매: {'실행 중' if running else '중지'}")
        self.btn_toggle.config(text="자동매매 중지" if running else "자동매매 시작")

        if kis:
            self.e_key.delete(0, "end"); self.e_key.insert(0, kis["app_key"])
            self.e_acct.delete(0, "end"); self.e_acct.insert(0, kis["account_no"])

        eq = _read_json(EQUITY_PATH, [])
        led = _read_json(LEDGER_PATH, {})
        if eq:
            self.cycle_msg.config(
                text=f"최근 평가금액 {eq[-1]['value']:,}원 · 보유 {len(led)}종목"
                     f" · {eq[-1]['date']}")
        self._load_log_tail()

    def _load_log_tail(self):
        from .config import APP_DIR
        log_file = APP_DIR / _LOG_PATH_NAME
        text = ""
        if log_file.exists():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            text = "\n".join(lines[-200:])
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ── 백그라운드 작업 ───────────────────────────────────────────────────────

    def _run_bg(self, fn, on_done):
        def worker():
            try:
                res = fn()
                self.root.after(0, lambda: on_done(res, None))
            except Exception as e:
                self.root.after(0, lambda: on_done(None, e))
        threading.Thread(target=worker, daemon=True).start()

    # ── 동작 ──────────────────────────────────────────────────────────────────

    def _save_kis(self):
        key = self.e_key.get().strip()
        secret = self.e_secret.get().strip()
        acct = self.e_acct.get().strip()
        if not (key and secret and acct):
            messagebox.showwarning("입력 확인", "App Key/Secret/계좌번호를 모두 입력하세요.")
            return
        secrets_store.save_kis(key, secret, acct, virtual=True)
        self.e_secret.delete(0, "end")
        messagebox.showinfo("저장 완료",
                            "KIS 자격증명을 저장했습니다. 키는 이 PC를 떠나지 않습니다.")
        self.refresh_status()

    def _pair(self):
        self.btn_pair.config(state="disabled")
        self.pair_msg.config(text="페어링 코드 발급 중...")

        def start():
            return pairing.start_pairing(socket.gethostname() or "내 PC")

        def started(info, err):
            if err:
                self.pair_msg.config(text=f"오류: {err}")
                self.btn_pair.config(state="normal")
                return
            self.pair_code.config(text=info["user_code"])
            self.pair_msg.config(
                text="브라우저에서 로그인 후 승인 버튼을 누르세요. 승인 대기 중...")
            # 코드가 미리 채워진 URL로 연다(구버전 서버 대비 fallback)
            webbrowser.open(info.get("verification_uri_complete")
                            or info["verification_uri"])

            def poll():
                return pairing.poll_for_token(info["device_code"])

            def polled(_tok, e):
                self.btn_pair.config(state="normal")
                if e:
                    self.pair_msg.config(text=f"페어링 실패: {e}")
                else:
                    self.pair_code.config(text="")
                    self.pair_msg.config(text="페어링 완료.")
                    self.refresh_status()

            self._run_bg(poll, polled)

        self._run_bg(start, started)

    def _toggle_auto(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            self.refresh_status()
            return
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        self.scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        self.scheduler.add_job(
            self._cycle_job,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=55,
                        timezone="Asia/Seoul"),
            id="paper_cycle", misfire_grace_time=300)
        self.scheduler.start()
        self.refresh_status()

    def _cycle_job(self):
        from .runner import run_cycle
        run_cycle(use_mock=secrets_store.load_kis() is None)

    def _run_once(self):
        self.btn_cycle.config(state="disabled")
        self.cycle_msg.config(text="실행 중... (시세 수집에 시간이 걸릴 수 있습니다)")

        def job():
            from .runner import run_cycle
            return run_cycle(use_mock=secrets_store.load_kis() is None)

        def done(payload, err):
            self.btn_cycle.config(state="normal")
            if err:
                self.cycle_msg.config(text=f"오류: {err}")
            else:
                b = payload["balance"]
                self.cycle_msg.config(
                    text=f"완료 — 평가금액 {b['total_eval']:,}원 · "
                         f"보유 {len(payload['positions'])}종목 · "
                         f"체결 {len(payload['trades'])}건")
            self.refresh_status()

        self._run_bg(job, done)

    def _on_close(self):
        if self.on_close_to_tray:
            self.on_close_to_tray()        # 트레이로 숨김
        else:
            if self.scheduler and self.scheduler.running:
                self.scheduler.shutdown(wait=False)
            self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    SettingsApp().run()


if __name__ == "__main__":
    main()
