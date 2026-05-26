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

from . import __version__, killswitch, order_log, pairing, secrets_store, sync_client
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
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 페어링돼 있으면 user email을 백그라운드로 조회(서버 응답 도착 시 hero 갱신)
        self._fetch_user_info_async()

        # 웹에서 발행한 명령 수신 시작 (페어링돼 있으면 즉시 연결)
        self.cmd_client = CommandClient(self._handle_command)
        self.cmd_client.start()

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

        # 상태 히어로 — 한눈에 현재 상태를 보여준다
        self.hero = tk.Frame(self.root, bg=SLATE)
        self.hero.pack(fill="x", padx=12, pady=(12, 4))
        self.hero_label = tk.Label(self.hero, text="", bg=SLATE, fg="#ffffff",
                                   font=("Segoe UI", 14, "bold"))
        self.hero_label.pack(pady=(14, 2))
        self.hero_sub = tk.Label(self.hero, text="", bg=SLATE, fg="#e5e7eb",
                                 font=("Segoe UI", 9))
        self.hero_sub.pack(pady=(0, 14))

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

    def run(self):
        self.root.mainloop()


def main():
    SettingsApp().run()


if __name__ == "__main__":
    main()
