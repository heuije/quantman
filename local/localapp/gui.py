"""로컬앱 설정 GUI (tkinter).

KIS 자격증명 입력 · 기기 페어링 · 자동매매 시작/중지 · 상태/로그 확인을
하나의 창에서 처리한다. 트레이 상주는 tray.py가 이 창을 감싼다.

UI는 웹앱과 같은 톤(브랜드 강조=따뜻한 테라코타, 주요 버튼=잉크)으로 맞추고, 상단 상태
히어로 + 1-2-3 단계 구성으로 초중급 사용자가 설정 순서를 헷갈리지 않도록 한다.
"""

from __future__ import annotations

import json
import socket
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import (__version__, killswitch, order_log, pairing, secrets_store,
                sync_client, updater)
from .commands_client import CommandClient
from .config import (EQUITY_PATH, LEDGER_PATH, PENDING_ORDERS_PATH,
                       PLATFORM_URL)
from .logging_setup import setup_logging


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def json_loads(s: str):
    return json.loads(s)

_LOG_PATH_NAME = "logs/localapp.log"

# 색상 팔레트 — DESIGN.md / web index.css :root 와 동기화 (2026-05-22 정제)
BG = "#faf9f6"            # 따뜻한 크림 배경
PANEL = "#ffffff"
BORDER = "#e8e3db"        # 따뜻한 그레이
TEXT = "#20201d"          # 따뜻한 근검정
MUTED = "#6f6a62"         # 따뜻한 그레이

# 브랜드 강조·활성 = Claude 계열 테라코타(주황) / 주요 버튼 = 잉크(따뜻한 차콜).
# 주황은 UI 크롬에만, 수익 빨강(UP)은 숫자에만 써서 같은 난색이라도 맥락으로 분리.
ACCENT = "#d97757"        # 테라코타 — 활성 탭·링크·포커스·페어링 코드 강조
ACCENT_SOFT = "#f7ece5"   # 주황 배경 틴트(따뜻한 피치)
INK = "#292524"           # 주요 버튼 채움(따뜻한 차콜)
INK_HOVER = "#423c37"     # 주요 버튼 hover

# 상태색 (시장 방향과 무관) — 정상/주의/위험. 그대로 유지.
GREEN = "#15803d"         # 정상·완료·연결됨
AMBER = "#b45309"         # 주의·설정 미완료
SLATE = "#475569"         # 중립 — 준비·중지
RED = "#b91c1c"           # 오류·위험·실전모드 경고

# 시장 방향·손익 (한국 관례: 상승·수익·매수 = 빨강 / 하락·손실·매도 = 파랑)
UP = "#e5383b"            # 상승·수익·매수
UP_SOFT = "#fdeceb"
DOWN = "#1668c4"          # 하락·손실·매도
DOWN_SOFT = "#e7f0fa"


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
        self.auto_paused = False         # 웹의 PAUSE_AUTO 명령으로 일시정지된 상태
        # 자격증명+페어링 둘 다 완료 시 ①② 영역 자동 접기 (사용자 toggle 가능)
        self.setup_collapsed = True
        self.user_email = ""

        self.root = tk.Tk()
        self.root.title("퀀트 플랫폼 — 로컬앱")
        self.root.geometry("880x980")
        self.root.resizable(True, True)
        self._apply_theme()
        self._build()
        self.refresh_status()
        self._schedule_minute_tick()       # 다음 자동매매 countdown 매분 갱신
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 페어링돼 있으면 user email을 백그라운드로 조회(서버 응답 도착 시 hero 갱신)
        self._fetch_user_info_async()

        # 웹에서 발행한 명령 수신 시작 (페어링돼 있으면 즉시 연결)
        self.cmd_client = CommandClient(self._handle_command)
        self.cmd_client.start()

        # Phase 60 — GitHub releases 최신 버전 체크.
        # 시작 시 1회 + 사용자가 창에 focus 줄 때마다 (트레이 복원·alt-tab 등). 폴링 없음.
        # 60s throttle로 빠른 위젯 클릭 시 중복 호출 방지. 이미 새 버전 감지 후엔 skip.
        self._update_info: dict | None = None
        self._last_update_check = 0.0
        if updater.is_frozen():
            threading.Thread(target=self._check_updates_async,
                              daemon=True, name="update-check").start()
            self.root.bind("<FocusIn>", self._on_focus_in_check_updates, add="+")

    # ── 테마 ──────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        self.root.configure(bg=BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT,
                         font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                        font=("Segoe UI", 9))
        style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT,
                        font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", fieldbackground=PANEL, bordercolor=BORDER,
                        borderwidth=1, padding=4)

        style.configure("TButton", background=PANEL, foreground=TEXT,
                        bordercolor=BORDER, borderwidth=1, padding=(12, 7),
                        font=("Segoe UI", 10))
        style.map("TButton",
                  background=[("active", "#f0ece6"),
                              ("disabled", "#f2efe9")],
                  foreground=[("disabled", "#aaa49b")])
        # 주요 액션 버튼 — 잉크 채움(검정 계열), 틸 액센트와 분리
        style.configure("Accent.TButton", background=INK,
                        foreground="#ffffff", bordercolor=INK,
                        padding=(14, 8), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", INK_HOVER),
                              ("disabled", "#cbc6bd")],
                  foreground=[("disabled", "#ffffff")])
        # 위험 액션 (kill switch reset 등) — 빨간 액센트
        style.configure("Danger.TButton", background=RED,
                        foreground="#ffffff", bordercolor=RED,
                        padding=(10, 6), font=("Segoe UI", 9, "bold"))
        style.map("Danger.TButton",
                  background=[("active", "#991b1b")])
        # Notebook 탭 톤
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background="#ece8e1", foreground=TEXT,
                        padding=(14, 6))
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", ACCENT)])
        # Treeview (주문/사이클 표) 톤
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, bordercolor=BORDER, borderwidth=1,
                        rowheight=22, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#f0ece6",
                        foreground=TEXT, font=("Segoe UI", 9, "bold"))

    # ── UI 구성 ───────────────────────────────────────────────────────────────

    def _build(self):
        pad = {"padx": 12, "pady": (4, 6)}

        # Phase 60 — 새 버전 알림 배너 (최상단). 평소엔 pack_forget 상태.
        # _check_updates_async가 최신 release 발견 시 _show_update_banner로 노출.
        self.update_banner = tk.Frame(self.root, bg=AMBER)
        self.update_banner_label = tk.Label(
            self.update_banner, text="", bg=AMBER, fg="#ffffff",
            font=("Segoe UI", 10, "bold"))
        self.update_banner_label.pack(side="left", padx=12, pady=8)
        self.update_banner_btn = ttk.Button(
            self.update_banner, text="지금 업데이트",
            style="Accent.TButton",
            command=self._start_update)
        self.update_banner_btn.pack(side="right", padx=12, pady=6)
        # 평소엔 숨김.

        # 상태 히어로 — 한눈에 현재 상태를 보여준다
        self.hero = tk.Frame(self.root, bg=SLATE)
        self.hero.pack(fill="x", padx=12, pady=(12, 4))
        self.hero_label = tk.Label(self.hero, text="", bg=SLATE, fg="#ffffff",
                                   font=("Segoe UI", 14, "bold"))
        self.hero_label.pack(pady=(14, 2))
        self.hero_sub = tk.Label(self.hero, text="", bg=SLATE, fg="#e5e7eb",
                                 font=("Segoe UI", 9))
        self.hero_sub.pack(pady=(0, 14))

        # mini 자동매매 일정 — hero 바로 아래. "다음 자동매매가 언제인지" 한 줄.
        # scheduler가 가동 중일 때만 표시(중지 상태에선 비활성). 가시성을 위해
        # _build에서 일단 packed 위치를 확보(이후 refresh_status가 pack_forget·
        # repack 토글) — 안 그러면 refresh_status의 pack 호출이 다른 모든 위젯
        # 뒤에 배치되어 사용자에게 안 보임.
        self.next_frame = tk.Frame(self.root, bg=PANEL,
                                    highlightbackground=BORDER, highlightthickness=1)
        self.next_krx_label = tk.Label(self.next_frame, bg=PANEL, fg=TEXT,
                                        font=("Segoe UI", 9), anchor="w",
                                        text="")
        self.next_us_label = tk.Label(self.next_frame, bg=PANEL, fg=TEXT,
                                       font=("Segoe UI", 9), anchor="w",
                                       text="")
        # 누락 알림 — 오늘 예정 cycle이 지났는데 cycles.jsonl에 기록 없을 때만 표시.
        self.miss_alert_label = tk.Label(self.next_frame, bg=PANEL, fg=RED,
                                          font=("Segoe UI", 9, "bold"), anchor="w",
                                          text="")
        self.next_krx_label.pack(fill="x", padx=12, pady=(6, 0))
        self.next_us_label.pack(fill="x", padx=12, pady=(2, 0))
        self.miss_alert_label.pack(fill="x", padx=12, pady=(2, 6))
        # hero 직후 위치 확보 — refresh_status가 pack_forget으로 일단 숨김 처리.
        self.next_frame.pack(fill="x", padx=12, pady=(0, 6))
        self.next_frame.pack_forget()

        # Kill switch 배너 — 활성 시에만 표시
        self.ks_banner = tk.Frame(self.root, bg=RED)
        self.ks_label = tk.Label(self.ks_banner, text="", bg=RED, fg="#ffffff",
                                  font=("Segoe UI", 10, "bold"))
        self.ks_label.pack(side="left", padx=12, pady=8)
        self.btn_ks_reset = ttk.Button(self.ks_banner, text="Kill Switch 해제",
                                        style="Danger.TButton",
                                        command=self._reset_killswitch)
        self.btn_ks_reset.pack(side="right", padx=12, pady=6)

        # 접힌 설정 바 — 자격증명+페어링 둘 다 완료된 정상 운영 상태에서 표시.
        # ①② 펼친 LabelFrame 대신 한 줄로 압축 → Notebook(주문/사이클 등) 공간 확보.
        self.setup_bar = tk.Frame(self.root, bg=PANEL, highlightbackground=BORDER,
                                   highlightthickness=1)
        self.setup_bar_label = tk.Label(self.setup_bar, bg=PANEL, fg=TEXT,
                                         font=("Segoe UI", 9), anchor="w",
                                         text="")
        self.setup_bar_label.pack(side="left", padx=12, pady=8)
        ttk.Button(self.setup_bar, text="⚙ 자격증명·페어링 변경",
                   command=self._toggle_setup_expanded).pack(side="right",
                                                              padx=8, pady=4)

        # ①② 묶음 frame — 토글 시 한 번에 펼침/숨김
        self.setup_expanded = tk.Frame(self.root, bg=BG)

        # ① KIS 자격증명
        self.kf = ttk.LabelFrame(self.setup_expanded, text="① KIS 모의투자 자격증명")
        self.kf.pack(fill="x", **pad)
        ttk.Label(self.kf, style="Muted.TLabel", wraplength=500, justify="left",
                  text="한국투자증권 모의투자 계좌의 App Key·Secret을 입력하세요. "
                       "키는 이 PC에만 저장되며 플랫폼 서버로 전송되지 않습니다."
                  ).pack(anchor="w", padx=12, pady=(8, 4))
        self.e_key = self._labeled_entry(self.kf, "App Key")
        self.e_secret = self._labeled_entry(self.kf, "App Secret", show="*")
        self.e_acct = self._labeled_entry(self.kf, "계좌번호 (예: 50001234-01)")
        ttk.Button(self.kf, text="자격증명 저장", style="Accent.TButton",
                   command=self._save_kis).pack(anchor="e", padx=12, pady=10)

        # ② 기기 페어링
        self.pf = ttk.LabelFrame(self.setup_expanded, text="② 플랫폼 계정 연결")
        self.pf.pack(fill="x", **pad)
        ttk.Label(self.pf, style="Muted.TLabel",
                  text=f"플랫폼: {PLATFORM_URL}"
                  ).pack(anchor="w", padx=12, pady=(8, 2))
        ttk.Label(self.pf, style="Muted.TLabel", wraplength=500, justify="left",
                  text="‘기기 페어링 시작’을 누르면 브라우저가 열립니다. "
                       "플랫폼에 로그인한 뒤 승인하면 연결이 끝납니다."
                  ).pack(anchor="w", padx=12, pady=(0, 4))
        self.pair_code = tk.Label(self.pf, text="", bg=BG, fg=ACCENT,
                                  font=("Segoe UI", 19, "bold"))
        self.pair_code.pack(anchor="w", padx=12, pady=2)
        self.pair_msg = ttk.Label(self.pf, style="Muted.TLabel", text="")
        self.pair_msg.pack(anchor="w", padx=12, pady=2)
        self.btn_pair = ttk.Button(self.pf, text="기기 페어링 시작",
                                   style="Accent.TButton", command=self._pair)
        self.btn_pair.pack(anchor="e", padx=12, pady=10)

        # ③ 자동매매
        self.af = ttk.LabelFrame(self.root, text="③ 자동매매")
        self.af.pack(fill="x", **pad)
        ttk.Label(self.af, style="Muted.TLabel", wraplength=500, justify="left",
                  text="시작하면 평일 오전 8시 55분에 자동으로 매매합니다. "
                       "‘지금 한 번 실행’으로 즉시 테스트할 수 있습니다."
                  ).pack(anchor="w", padx=12, pady=(8, 4))
        row = ttk.Frame(self.af)
        row.pack(fill="x", padx=12, pady=8)
        self.btn_toggle = ttk.Button(row, text="자동매매 시작",
                                     style="Accent.TButton",
                                     command=self._toggle_auto)
        self.btn_toggle.pack(side="left")
        self.btn_cycle = ttk.Button(row, text="지금 한 번 실행",
                                    command=self._run_once)
        self.btn_cycle.pack(side="left", padx=8)
        self.cycle_msg = ttk.Label(self.af, style="Muted.TLabel", text="")
        self.cycle_msg.pack(anchor="w", padx=12, pady=(2, 8))

        # 거래 모니터링 — Notebook: 주문 현황 / 주문 내역 / 사이클 로그 / 슬리피지 / 활동 로그
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 4))

        self._build_tab_pending()
        self._build_tab_orders()
        self._build_tab_cycles()
        self._build_tab_slippage()
        self._build_tab_log()

        ttk.Button(self.root, text="새로고침", command=self.refresh_status).pack(
            anchor="e", padx=14, pady=(0, 10))

    # ── Notebook 탭들 ─────────────────────────────────────────────────────────

    def _make_tree(self, parent, columns: list[tuple[str, str, int]]) -> ttk.Treeview:
        """(id, heading, width) 컬럼 리스트로 Treeview 생성."""
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns],
                             show="headings", height=10)
        for cid, head, w in columns:
            tree.heading(cid, text=head)
            tree.column(cid, width=w, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # 한국 관례: 매수 행 = 빨강 틴트, 매도 행 = 파랑 틴트 (국내 HTS 호가창과 일치)
        tree.tag_configure("buy", background=UP_SOFT)
        tree.tag_configure("sell", background=DOWN_SOFT)
        return tree

    def _build_tab_pending(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="주문 현황")
        ttk.Label(f, style="Muted.TLabel", wraplength=700, justify="left",
                  text="KIS에 제출되어 아직 체결되지 않은 주문입니다. "
                       "동시호가에 들어간 주문은 9시 시초가에 일괄 체결됩니다."
                  ).pack(anchor="w", padx=12, pady=(8, 0))
        self.tv_pending = self._make_tree(f, [
            ("time", "제출시각", 90), ("side", "방향", 60),
            ("symbol", "종목", 80), ("name", "이름", 100),
            ("qty", "주문수량", 70), ("filled", "체결", 60),
            ("remain", "잔량", 60), ("limit", "지정가", 90),
            ("order_no", "주문번호", 110),
        ])

    def _build_tab_orders(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="주문 내역")
        ttk.Label(f, style="Muted.TLabel",
                  text="최근 100건의 주문 이벤트 — 제출 / 체결 / 취소 / 거부 모두."
                  ).pack(anchor="w", padx=12, pady=(8, 0))
        self.tv_orders = self._make_tree(f, [
            ("time", "시각", 130), ("event", "상태", 70),
            ("side", "방향", 50), ("symbol", "종목", 80),
            ("qty", "수량", 60), ("limit", "지정가", 80),
            ("fill", "체결가", 80), ("strategy", "전략", 110),
            ("reason", "사유", 110),
        ])

    def _build_tab_cycles(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="사이클 로그")
        ttk.Label(f, style="Muted.TLabel",
                  text="자동매매 사이클 단위 의사결정 요약 — 무엇을 왜 매수/매도/스킵했는지."
                  ).pack(anchor="w", padx=12, pady=(8, 0))
        self.tv_cycles = self._make_tree(f, [
            ("time", "시각", 140), ("bought", "매수", 50),
            ("sold", "매도", 50), ("gap", "갭스킵", 55),
            ("signal", "신호X", 55), ("rejected", "거부", 55),
            ("ks", "killswitch", 90), ("equity", "평가금액", 120),
        ])
        # 선택 시 상세 표시
        self.tv_cycles.bind("<<TreeviewSelect>>", self._show_cycle_detail)
        self.cycle_detail = tk.Text(f, height=8, font=("Consolas", 9),
                                     state="disabled", wrap="word", bg=PANEL,
                                     fg=TEXT, relief="solid", borderwidth=1,
                                     highlightthickness=0)
        self.cycle_detail.pack(fill="x", padx=8, pady=(0, 8))

    def _build_tab_slippage(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="슬리피지")
        ttk.Label(f, style="Muted.TLabel", wraplength=700, justify="left",
                  text="의도가 vs 체결가의 차이(bps). 양수 = 불리한 체결. "
                       "20bps = 0.20%. 백테스트 가정(10bps default)과 비교 가능."
                  ).pack(anchor="w", padx=12, pady=(8, 4))
        self.slip_summary = ttk.Label(f, style="Muted.TLabel", text="",
                                       font=("Segoe UI", 10, "bold"))
        self.slip_summary.pack(anchor="w", padx=12, pady=(0, 8))
        self.tv_slip = self._make_tree(f, [
            ("time", "시각", 140), ("side", "방향", 60),
            ("symbol", "종목", 80), ("intended", "의도가", 100),
            ("fill", "체결가", 100), ("bps", "슬리피지(bps)", 110),
        ])

    def _build_tab_log(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="활동 로그")
        self.log_text = tk.Text(f, height=8, font=("Consolas", 8),
                                state="disabled", wrap="none",
                                bg=PANEL, fg=TEXT, relief="solid",
                                borderwidth=1, highlightthickness=0)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(10, 4))

    def _labeled_entry(self, parent, label, show=None):
        ttk.Label(parent, text=label).pack(anchor="w", padx=12, pady=(6, 1))
        e = ttk.Entry(parent, show=show or "", font=("Segoe UI", 10))
        e.pack(fill="x", padx=12)
        return e

    # ── 상태 갱신 ─────────────────────────────────────────────────────────────

    def _set_hero(self, text, sub, color):
        self.hero.configure(bg=color)
        self.hero_label.configure(text=text, bg=color)
        self.hero_sub.configure(text=sub, bg=color)

    # ── 다음 자동매매 일정 (mini timeline) ────────────────────────────────────
    def _schedule_minute_tick(self):
        """매분 next-run countdown 갱신. refresh_status 안에서 next_run 라벨 다시 그림."""
        try:
            self.refresh_status()
        finally:
            self.root.after(60_000, self._schedule_minute_tick)

    def _refresh_next_run_labels(self):
        """APScheduler에 등록된 krx_cycle·us_cycle 잡의 next_run_time을 표시.

        사용자가 "다음 자동매매가 언제인지" 추측하지 않게 한다.
        매분 1회 root.after로 자동 갱신 (countdown 의미 있게 유지).
        + 오늘 예정 cycle이 지났는데 cycles.jsonl에 기록 없으면 누락 알림.
        """
        if not self.scheduler:
            return
        try:
            krx_job = self.scheduler.get_job("krx_cycle")
            us_job = self.scheduler.get_job("us_cycle")
        except Exception:
            krx_job = us_job = None
        self.next_krx_label.configure(
            text="다음 KRX 사이클:  " + self._format_next_run(krx_job))
        self.next_us_label.configure(
            text="다음 US 사이클:    " + self._format_next_run(us_job, fallback_us=True))

        # 누락 알림 — 오늘 예정 cycle 시각이 지났는데 cycles.jsonl에 기록 없음 = missed
        missed = self._detect_missed_today()
        if missed:
            self.miss_alert_label.configure(
                text="⚠ 오늘 누락된 cycle: " + " · ".join(missed))
            self.miss_alert_label.pack(fill="x", padx=12, pady=(2, 6))
        else:
            self.miss_alert_label.pack_forget()

    def _detect_missed_today(self) -> list[str]:
        """오늘 자동매매 cycle 누락 여부. 반환: 누락된 시장명 list (예: ['KRX'])."""
        from datetime import datetime, time as dtime, timedelta
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
        now = datetime.now(kst)
        today = now.date()

        # 오늘 평일이면 KRX 08:55, 미국 세션이 오늘 한국시각에 있었으면 US도 체크.
        checks: list[tuple[str, datetime]] = []
        # KRX — 평일만, 08:55 KST 이후
        if today.weekday() < 5:    # 월~금
            krx_sched = datetime.combine(today, dtime(8, 55), tzinfo=kst)
            if now > krx_sched + timedelta(minutes=5):  # grace 5분 지나야 누락 판정
                checks.append(("KRX", krx_sched))
        # US — 캘린더에서 오늘 KST 안에 끝난 세션 있으면. 보통 오늘 새벽까지.
        # 간단화: 어제 22:25~오늘 06:00 사이 미국 cycle이 있었는지만 본다(DST 변동 마진).
        # 더 정확한 판정은 server timeline에 위임.
        try:
            from quant_core import market_calendar as mc
            sess_prev = mc.next_session_kst("US", now - timedelta(days=2))
            if sess_prev:
                open_kst, _close_kst = sess_prev
                us_sched = open_kst - timedelta(minutes=5)
                # 오늘(또는 새벽이라면 어제) 안에 있던 세션만 — 24h 안.
                if 0 < (now - us_sched).total_seconds() < 86400:
                    checks.append(("US", us_sched))
        except Exception:
            pass

        if not checks:
            return []

        # cycles.jsonl 읽어 각 sched 직후 entry 있는지 확인. 없으면 누락.
        from .config import CYCLES_PATH
        recent_ts: list[datetime] = []
        try:
            import json
            with open(CYCLES_PATH, encoding="utf-8") as f:
                # 마지막 100줄만 — 충분히 오늘+어제 커버.
                lines = f.readlines()[-100:]
            for line in lines:
                try:
                    d = json.loads(line)
                    ts_s = d.get("ts")
                    if not ts_s:
                        continue
                    ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                    recent_ts.append(ts.astimezone(kst))
                except (ValueError, json.JSONDecodeError):
                    continue
        except FileNotFoundError:
            pass

        missed = []
        for market, sched in checks:
            # sched-1min ~ sched+30min 사이 entry 있으면 정상.
            window_lo = sched - timedelta(minutes=1)
            window_hi = sched + timedelta(minutes=30)
            if not any(window_lo <= t <= window_hi for t in recent_ts):
                missed.append(market)
        return missed

    def _format_next_run(self, job, fallback_us: bool = False) -> str:
        """job.next_run_time을 사람이 읽는 형태로. None이면 적절한 fallback 메시지."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")
        now = datetime.now(kst)

        if job is None or job.next_run_time is None:
            if fallback_us:
                # us_cycle 잡은 하루에 한 번 동적 등록(_plan_us_session). 정오 plan 전이거나
                # 휴장 안내 시 명시.
                return "오늘 정오에 결정됩니다 (개장 5분 전 자동 등록)"
            return "예정 없음"

        nrt = job.next_run_time.astimezone(kst)
        delta = nrt - now
        secs = int(delta.total_seconds())
        if secs <= 0:
            return f"{nrt.strftime('%H:%M KST')} (곧)"

        # 절대 시각 + 상대 시각.
        # 오늘/내일/모레: 한국어 라벨, 그 외: MM-DD
        today = now.date()
        target = nrt.date()
        days = (target - today).days
        if days == 0:
            day_label = "오늘"
        elif days == 1:
            day_label = "내일"
        elif days == 2:
            day_label = "모레"
        else:
            day_label = nrt.strftime("%m-%d")

        # 상대 시간 — 분/시간/일 단위
        if secs < 60:
            rel = "곧"
        elif secs < 3600:
            rel = f"{secs // 60}분 후"
        elif secs < 86400:
            h = secs // 3600
            m = (secs % 3600) // 60
            rel = f"{h}h {m}m 후" if m else f"{h}h 후"
        else:
            d = secs // 86400
            h = (secs % 86400) // 3600
            rel = f"{d}일 {h}h 후" if h else f"{d}일 후"

        return f"{day_label} {nrt.strftime('%H:%M KST')}  ({rel})"

    def _render_setup_area(self, kis_ok: bool, dev_ok: bool):
        """자격증명+페어링 둘 다 완료면 한 줄 bar, 아니면 LabelFrame 펼침.

        사용자가 ⚙ 변경 버튼으로 강제 펼침한 상태(self.setup_collapsed=False)이면
        bar 대신 펼쳐진 LabelFrame을 우선 표시.
        """
        both_ok = kis_ok and dev_ok
        show_collapsed = both_ok and self.setup_collapsed
        if show_collapsed:
            # bar 노출 / 펼친 frame 숨김
            parts = []
            parts.append("✓ KIS 자격증명 등록됨")
            parts.append("✓ 플랫폼 계정 연결됨")
            self.setup_bar_label.configure(text="  ·  ".join(parts))
            self.setup_expanded.pack_forget()
            self.setup_bar.pack(fill="x", padx=12, pady=(4, 6),
                                 before=self.af)
        else:
            self.setup_bar.pack_forget()
            self.setup_expanded.pack(fill="x", before=self.af)

    def _toggle_setup_expanded(self):
        """⚙ 변경 버튼 — 한 줄 bar ↔ 펼친 LabelFrame 토글."""
        self.setup_collapsed = not self.setup_collapsed
        self.refresh_status()

    def _fetch_user_info_async(self):
        """페어링된 user의 email을 백그라운드로 조회 → hero 갱신.

        서버 응답이 늦어도 GUI를 막지 않도록 별도 thread. 실패하면 email은 빈
        문자열로 남고 hero는 v.X.Y.Z 만 표시.
        """
        def worker():
            info = sync_client.fetch_user_info()
            if info and info.get("email"):
                self.user_email = info["email"]
                self.root.after(0, self.refresh_status)
        threading.Thread(target=worker, daemon=True,
                          name="user-info-fetch").start()

    def refresh_status(self):
        kis = secrets_store.load_kis()
        dev = secrets_store.load_device_token()
        running = bool(self.scheduler and self.scheduler.running)
        ks = killswitch.load()
        ks_active = bool(ks.get("active"))

        # mini 자동매매 일정 — scheduler 가동 중일 때만 표시.
        # scheduler가 멈춰있으면 "다음 자동매매" 자체가 의미 없음.
        # after=self.hero로 hero 직후 위치 강제(repack 시 root 끝으로 밀리는 것 방지).
        if running and not ks_active:
            self._refresh_next_run_labels()
            self.next_frame.pack(fill="x", padx=12, pady=(0, 6), after=self.hero)
        else:
            self.next_frame.pack_forget()

        # hero 부제목: 버전 + 이메일(있을 때) + 상태 메시지
        ident = f"v{__version__}"
        if self.user_email:
            ident += f"  ·  {self.user_email}"

        # 히어로 — 전체 상태 한 줄
        if ks_active:
            self._set_hero("⚠ Kill Switch 활성",
                           f"{ident}  ·  자동매매 중단 — 사용자가 해제해야 재개됩니다", RED)
        elif not kis or not dev:
            missing = []
            if not kis:
                missing.append("KIS 자격증명")
            if not dev:
                missing.append("기기 페어링")
            self._set_hero("설정 미완료",
                           f"{ident}  ·  " + " · ".join(missing) + " 을(를) 완료하세요", AMBER)
        elif running:
            self._set_hero("자동매매 실행 중",
                           f"{ident}  ·  평일 장 시작 전 자동으로 매매합니다", GREEN)
        else:
            self._set_hero("준비 완료 · 중지됨",
                           f"{ident}  ·  ‘자동매매 시작’을 누르면 가동됩니다", SLATE)

        # 설정 영역 toggle — 둘 다 완료 + 사용자가 펼치라고 안 했으면 collapse
        self._render_setup_area(kis_ok=bool(kis), dev_ok=bool(dev))

        # Kill switch 배너
        if ks_active:
            self.ks_label.configure(
                text=f"Kill Switch 발동: {ks.get('reason', '')}  "
                     f"(since {(ks.get('since') or '')[:19]})")
            self.ks_banner.pack(fill="x", padx=12, pady=(0, 4))
        else:
            self.ks_banner.pack_forget()

        # 단계 헤더에 진행 상태 표시
        self.kf.configure(
            text="① KIS 모의투자 자격증명        "
                 + ("✓ 등록됨" if kis else "입력 필요"))
        self.pf.configure(
            text="② 플랫폼 계정 연결        "
                 + ("✓ 완료" if dev else "미완료"))
        self.af.configure(
            text="③ 자동매매        " + ("실행 중" if running else "중지됨"))
        self.btn_toggle.config(text="자동매매 중지" if running else "자동매매 시작")

        if kis:
            self.e_key.delete(0, "end")
            self.e_key.insert(0, kis["app_key"])
            self.e_acct.delete(0, "end")
            self.e_acct.insert(0, kis["account_no"])

        eq = _read_json(EQUITY_PATH, [])
        led = _read_json(LEDGER_PATH, {})
        if eq:
            self.cycle_msg.config(
                text=f"최근 평가금액 {eq[-1]['value']:,}원 · 보유 {len(led)}종목"
                     f" · {eq[-1]['date']}")
        self._load_log_tail()
        self._refresh_pending()
        self._refresh_orders()
        self._refresh_cycles()
        self._refresh_slippage()

    # ── Notebook 탭 데이터 갱신 ──────────────────────────────────────────────

    @staticmethod
    def _fmt_ts(iso: str) -> str:
        return (iso or "").replace("T", " ")[:19]

    def _refresh_pending(self):
        self.tv_pending.delete(*self.tv_pending.get_children())
        local = _read_json(PENDING_ORDERS_PATH, {})
        for p in local.values():
            side = "buy" if p.get("side") == "buy" else "sell"
            self.tv_pending.insert("", "end", values=(
                self._fmt_ts(p.get("submitted_ts_iso", "")) or "",
                "매수" if side == "buy" else "매도",
                p.get("symbol", ""), "",
                p.get("qty", ""),
                p.get("filled_so_far", 0),
                int(p.get("qty", 0)) - int(p.get("filled_so_far", 0)),
                f"{p.get('limit_price', 0):,}" if p.get("limit_price") else "—",
                p.get("order_no", ""),
            ), tags=(side,))

    def _refresh_orders(self):
        self.tv_orders.delete(*self.tv_orders.get_children())
        for o in order_log.read_orders(100):
            side = "buy" if o.get("side") == "buy" else "sell"
            self.tv_orders.insert("", "end", values=(
                self._fmt_ts(o.get("ts", "")),
                o.get("event", ""),
                "매수" if side == "buy" else "매도",
                o.get("symbol", ""),
                o.get("qty", ""),
                f"{o.get('limit_price') or 0:,.0f}" if o.get("limit_price") else "—",
                f"{o.get('fill_price') or 0:,.0f}" if o.get("fill_price") else "—",
                o.get("strategy", ""),
                o.get("reason", ""),
            ), tags=(side,))

    def _refresh_cycles(self):
        self.tv_cycles.delete(*self.tv_cycles.get_children())
        for c in order_log.read_cycles(20):
            s = c.get("summary", {})
            self.tv_cycles.insert("", "end", values=(
                self._fmt_ts(c.get("ts", "")),
                s.get("n_bought", 0), s.get("n_sold", 0),
                s.get("n_skip_gap", 0), s.get("n_skip_signal", 0),
                s.get("n_rejected", 0),
                "ON" if s.get("kill_switch") else "OFF",
                f"{s.get('equity_post', 0):,.0f}",
            ), tags=(json_dumps(c),))
        self.cycle_detail.config(state="normal")
        self.cycle_detail.delete("1.0", "end")
        self.cycle_detail.insert("1.0", "사이클 행을 클릭하면 상세 의사결정이 표시됩니다.")
        self.cycle_detail.config(state="disabled")

    def _show_cycle_detail(self, _event):
        sel = self.tv_cycles.selection()
        if not sel:
            return
        tags = self.tv_cycles.item(sel[0], "tags")
        if not tags:
            return
        c = json_loads(tags[0])
        lines = [f"[{self._fmt_ts(c.get('ts', ''))}] 사이클 상세"]
        for d in c.get("decisions", []):
            sym = d.get("symbol") or "-"
            lines.append(f"  · {d.get('action','?')} | {sym} | "
                         f"{d.get('strategy_name','')} | {d.get('reason','')}")
        text = "\n".join(lines)
        self.cycle_detail.config(state="normal")
        self.cycle_detail.delete("1.0", "end")
        self.cycle_detail.insert("1.0", text)
        self.cycle_detail.config(state="disabled")

    def _refresh_slippage(self):
        s = order_log.slippage_stats()
        if s["n"] == 0:
            self.slip_summary.configure(text="아직 측정된 체결이 없습니다.")
        else:
            self.slip_summary.configure(
                text=f"표본 {s['n']}건 · 평균 {s['avg_bps']} bps · "
                     f"중앙값 {s['p50_bps']} bps · p95 {s['p95_bps']} bps "
                     f"· 최대 {s['max_bps']} bps")
        self.tv_slip.delete(*self.tv_slip.get_children())
        for r in s.get("recent", []):
            side = "buy" if r.get("side") == "buy" else "sell"
            self.tv_slip.insert("", "end", values=(
                self._fmt_ts(r.get("ts", "")),
                "매수" if side == "buy" else "매도",
                r.get("symbol", ""),
                f"{r.get('intended', 0):,.0f}",
                f"{r.get('fill', 0):,.0f}",
                f"{r.get('bps', 0):+.1f}",
            ), tags=(side,))

    def _reset_killswitch(self):
        if not messagebox.askyesno(
                "Kill Switch 해제",
                "Kill Switch를 해제하면 다음 사이클부터 자동매매가 재개됩니다.\n"
                "발동 사유가 충분히 해소됐는지 확인했습니까?"):
            return
        killswitch.reset()
        self.refresh_status()

    # ── 웹에서 발행한 명령 처리 ───────────────────────────────────────────────

    def _handle_command(self, cmd: dict) -> dict:
        """SSE/폴링으로 도착한 명령 처리. 반환값은 ack의 result로 전송."""
        t = cmd.get("type")
        params = cmd.get("params") or {}
        import logging
        log = logging.getLogger("localapp.gui.cmd")
        log.info("명령 수신: %s %s", t, params)

        if t == "RUN_CYCLE_NOW":
            if secrets_store.load_kis() is None:
                return {"error": "KIS 자격증명 없음 — setup 후 다시 시도하세요"}
            from .runner import run_cycle
            payload = run_cycle()
            # GUI는 다음 자동 갱신에서 반영
            self.root.after(100, self.refresh_status)
            return {"balance": payload.get("balance"),
                    "n_decisions": len(payload.get("decisions", []))}

        if t == "PAUSE_AUTO":
            self.auto_paused = True
            if self.scheduler and self.scheduler.running:
                self.scheduler.pause()
            self.root.after(100, self.refresh_status)
            return {"paused": True}

        if t == "RESUME_AUTO":
            self.auto_paused = False
            if self.scheduler and self.scheduler.running:
                self.scheduler.resume()
            self.root.after(100, self.refresh_status)
            return {"paused": False}

        if t == "LIQUIDATE_ALL":
            if secrets_store.load_kis() is None:
                return {"error": "KIS 자격증명 없음 — setup 후 다시 시도하세요"}
            killswitch.activate("웹 명령: LIQUIDATE_ALL")
            from .runner import run_cycle
            payload = run_cycle()
            self.root.after(100, self.refresh_status)
            return {"liquidated_positions": len(payload.get("positions", []))}

        if t == "CANCEL_ORDER":
            order_no = params.get("order_no")
            symbol = params.get("symbol", "")
            qty = int(params.get("qty", 0))
            if not order_no:
                return {"error": "order_no 누락"}
            if secrets_store.load_kis() is None:
                return {"error": "KIS 자격증명 없음"}
            from .kis_broker import KisBroker
            r = KisBroker().cancel(order_no, symbol, qty)
            return r

        if t == "RESET_KILL_SWITCH":
            killswitch.reset()
            self.root.after(100, self.refresh_status)
            return {"reset": True}

        if t == "RECONCILE_NOW":
            # Phase 40 — 수동 reconcile 트리거 (HTS 수동 매매 직후 등)
            if secrets_store.load_kis() is None:
                return {"error": "KIS 자격증명 없음 — setup 후 다시 시도하세요"}
            from .broker import Broker  # type 힌트용
            from .runner import make_broker
            from .trader import Trader
            from .sync_client import push_snapshot
            broker = make_broker()
            trader = Trader(broker)
            result = trader.reconcile_with_kis()
            # 최신 잔고 함께 push해 서버 알림·UI 갱신
            try:
                snap = broker.account_snapshot()
                payload = {
                    "balance": snap.get("balance", {}),
                    "positions": snap.get("positions", []),
                    "reconciliation": result,
                    "cycle_summary": {
                        "kind": "manual_reconcile",
                        "reconcile_drift": result.get("has_drift", False),
                        "reconcile_applied": len(result.get("applied") or []),
                    },
                }
                push_snapshot(payload)
            except Exception as e:
                log.warning("수동 reconcile push 실패: %s", e)
            self.root.after(100, self.refresh_status)
            return {
                "has_drift": result.get("has_drift", False),
                "applied_count": len(result.get("applied") or []),
                "external_extras_count": result.get("external_extras_count", 0),
                "in_sync_count": len(result.get("in_sync") or []),
                "checked_at": result.get("checked_at"),
            }

        return {"error": f"미지원 명령 타입: {t}"}

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
                    self._fetch_user_info_async()
                    self.refresh_status()

            self._run_bg(poll, polled)

        self._run_bg(start, started)

    def _toggle_auto(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            self.refresh_status()
            return
        # Phase 60+ — GUI 모드도 scheduler.register_jobs를 그대로 호출.
        # 이전엔 KRX 08:55만 등록해 미국 cycle·heartbeat·dataset·intraday loop가
        # 모두 누락됐었다 (미장 자동매매 작동 안 한 근본 원인).
        from apscheduler.schedulers.background import BackgroundScheduler
        from . import scheduler as _scheduler_mod
        self.scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        _scheduler_mod.register_jobs(self.scheduler)

        # GUI 모드 보조 — sync retry · 캘린더 기동 시 1회 sync (headless start()와 동등).
        import threading
        try:
            from . import sync_retry
            sync_retry.start()
        except Exception:
            pass
        try:
            from . import calendar_sync
            threading.Thread(target=calendar_sync.pull_all, daemon=True,
                              name="calendar-sync-initial").start()
        except Exception:
            pass

        self.scheduler.start()
        self.refresh_status()

    def _cycle_job(self):
        if secrets_store.load_kis() is None:
            import logging
            logging.getLogger("localapp.gui").warning(
                "KIS 자격증명 없음 — 자동 사이클 skip")
            return
        from .runner import run_cycle
        run_cycle()

    def _run_once(self):
        if secrets_store.load_kis() is None:
            messagebox.showwarning(
                "자격증명 필요",
                "KIS 자격증명을 먼저 등록하세요. (App Key/Secret/계좌번호)\n"
                "KIS 모의투자 가입은 무료이며 즉시 발급됩니다.")
            return
        self.btn_cycle.config(state="disabled")
        self.cycle_msg.config(text="실행 중... (시세 수집에 시간이 걸릴 수 있습니다)")

        def job():
            from .runner import run_cycle
            return run_cycle()

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
            if hasattr(self, "cmd_client") and self.cmd_client:
                self.cmd_client.stop()
            self.root.destroy()

    # ── Phase 60: 자동 업데이트 ────────────────────────────────────────────────

    def _check_updates_async(self):
        """background에서 GitHub releases 최신 버전 조회. 새 버전이면 배너 표시.

        호출 경로: 시작 시 1회 + FocusIn 이벤트(사용자가 창에 돌아올 때).
        예외는 명시적으로 debug log — silent pass는 4원칙 위반이라 디버깅 가능하게.
        """
        import time
        self._last_update_check = time.time()
        try:
            info = updater.check_latest_version()
        except Exception as e:
            import logging
            logging.getLogger("localapp.updater").debug("최신 버전 조회 실패: %s", e)
            return
        if info and updater.is_newer(__version__, info["tag"]):
            self._update_info = info
            self.root.after(0, self._show_update_banner)

    def _on_focus_in_check_updates(self, event):
        """사용자가 창에 focus 줄 때 재체크. throttle·guard로 낭비 차단."""
        import time
        # FocusIn은 child 위젯에도 발생 — root 외엔 무시.
        if event.widget is not self.root:
            return
        # 이미 새 버전 감지·배너 표시 중이면 재체크 불필요.
        if self._update_info is not None:
            return
        # 1분 throttle — 위젯 사이 빠른 클릭 시 중복 호출 방지.
        if time.time() - self._last_update_check < 60.0:
            return
        threading.Thread(target=self._check_updates_async,
                          daemon=True, name="update-recheck").start()

    def _show_update_banner(self):
        """업데이트 배너를 최상단에 표시."""
        info = self._update_info
        if not info:
            return
        self.update_banner_label.config(
            text=f"새 버전 {info['tag']} 사용 가능 — 한 번 클릭으로 업데이트")
        # 최상단 (hero보다 위)
        self.update_banner.pack(side="top", fill="x", before=self.hero)

    def _start_update(self):
        """진행률 다이얼로그 + background 다운로드/설치."""
        info = self._update_info
        if not info or not info.get("url"):
            messagebox.showerror("업데이트 실패", "다운로드 URL을 찾을 수 없습니다.")
            return
        if not updater.is_frozen():
            messagebox.showinfo(
                "개발 환경",
                "자동 업데이트는 빌드된 .exe에서만 동작합니다.\n"
                "개발 환경에선 git pull을 사용하세요.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("업데이트 진행 중")
        dlg.geometry("420x140")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.configure(bg=BG)

        tk.Label(dlg, bg=BG, fg=TEXT, font=("Segoe UI", 10),
                 text=f"퀀트 플랫폼 {info['tag']} 설치 중...").pack(pady=(16, 8))
        status = tk.Label(dlg, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                          text="다운로드 준비 중…")
        status.pack()
        progress = ttk.Progressbar(dlg, length=380, maximum=100, mode="determinate")
        progress.pack(pady=10)

        STAGE_LABEL = {"download": "다운로드 중", "extract": "압축 해제 중",
                       "install": "설치 준비 중"}

        def progress_cb(stage: str, current: int, total: int):
            pct = int(current / max(total, 1) * 100)
            label = STAGE_LABEL.get(stage, stage)
            def update_ui():
                progress.config(value=pct)
                status.config(text=f"{label}… {pct}%")
            self.root.after(0, update_ui)

        def worker():
            try:
                updater.perform_update(info["url"], progress_cb=progress_cb)
                # 성공 — updater.bat이 백그라운드에서 robocopy 대기 중. 앱이 빨리
                # 종료돼야 exe 잠금이 풀려 bat이 즉시 복사 시작. messagebox.showinfo는
                # modal blocking이라 사용자가 [확인] 누를 때까지 self.root.quit()
                # 도달 X → bat 무한 retry 발생 → 사용자 터미널 retry 로그 봄.
                # 진행 다이얼로그 안에 완료 메시지 표시 후 1.5s 자동 quit.
                def finish():
                    status.config(text="✓ 설치 완료 — 곧 자동 재시작됩니다…")
                    progress.config(value=100)
                    def real_quit():
                        if self.scheduler and self.scheduler.running:
                            self.scheduler.shutdown(wait=False)
                        if hasattr(self, "cmd_client") and self.cmd_client:
                            self.cmd_client.stop()
                        self.root.quit()    # mainloop 종료 → 프로세스 종료 → bat이 교체 시작
                    self.root.after(1500, real_quit)
                self.root.after(0, finish)
            except Exception as e:
                err = str(e)
                def show_err():
                    dlg.destroy()
                    messagebox.showerror("업데이트 실패",
                                          f"업데이트 중 오류가 발생했습니다:\n{err}")
                self.root.after(0, show_err)

        threading.Thread(target=worker, daemon=True,
                          name="update-worker").start()

    def run(self):
        self.root.mainloop()


def main():
    SettingsApp().run()


if __name__ == "__main__":
    main()
