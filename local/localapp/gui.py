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

from . import (__version__, killswitch, kis_health, order_log, pairing,
                secrets_store, sync_client, updater)
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

        # Phase 7 — Catch-up 결과 polling. scheduler가 background thread로 catchup
        # 실행 → 결과 catchup_result.json. 5초 후 첫 read 시도(catch-up이 진행
        # 중이거나 끝났을 시점). 최대 12회 (1분) polling — 그 안에 끝남.
        self._catchup_poll_count = 0
        self.root.after(5_000, self._check_catchup_result_polling)

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

        # Phase 7 — Catch-up 결과 알림 배너 (PC 꺼져 있어 missed된 cycle/settlement
        # 을 기동 시 자동 보완 결과). scheduler.register_jobs의 background thread가
        # catchup_result.json에 기록 → gui가 polling으로 읽어 표시.
        self.catchup_banner = tk.Frame(self.root, bg=AMBER)
        self.catchup_banner_label = tk.Label(
            self.catchup_banner, text="", bg=AMBER, fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
            justify="left", anchor="w", wraplength=900)
        self.catchup_banner_label.pack(side="left", padx=12, pady=8, fill="x",
                                         expand=True)
        self.catchup_banner_btn = ttk.Button(
            self.catchup_banner, text="확인",
            command=self._dismiss_catchup_banner)
        self.catchup_banner_btn.pack(side="right", padx=12, pady=6)
        # 평소엔 숨김. _check_catchup_result_polling이 5초 후 첫 체크 시작.

        # 상태 히어로 — 한눈에 현재 상태를 보여준다
        self.hero = tk.Frame(self.root, bg=SLATE)
        self.hero.pack(fill="x", padx=12, pady=(12, 4))
        self.hero_label = tk.Label(self.hero, text="", bg=SLATE, fg="#ffffff",
                                   font=("Segoe UI", 14, "bold"))
        self.hero_label.pack(pady=(14, 2))
        self.hero_sub = tk.Label(self.hero, text="", bg=SLATE, fg="#e5e7eb",
                                 font=("Segoe UI", 9))
        self.hero_sub.pack(pady=(0, 14))

        # ── 자동매매 timeline 패널 (hero 바로 아래) ─────────────────────────
        # 웹앱 /monitor의 timeline과 동일 데이터(서버 /sync/timeline) 표시.
        # 어제·오늘·내일 그룹 + 6 종류 event(국장/미장 × 후보결정/시작/종료).
        # scheduler 가동 중일 때만 노출(.pack), 중지 시 .pack_forget.
        self.timeline_frame = tk.Frame(self.root, bg=PANEL,
                                        highlightbackground=BORDER, highlightthickness=1)
        # 헤더: "자동매매 상태  ● 정상 · 로컬앱 12초 전"
        self.timeline_header = tk.Frame(self.timeline_frame, bg=PANEL)
        self.timeline_header.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(self.timeline_header, text="자동매매 상태", bg=PANEL, fg=TEXT,
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        self.timeline_status_label = tk.Label(self.timeline_header, text="",
                                               bg=PANEL, fg=MUTED,
                                               font=("Segoe UI", 9))
        self.timeline_status_label.pack(side="right")
        # 본문: rows 동적 렌더 — refresh마다 children 모두 destroy 후 재구성
        self.timeline_body = tk.Frame(self.timeline_frame, bg=PANEL)
        self.timeline_body.pack(fill="x", padx=12, pady=(0, 8))
        # hero 직후 위치 확보, refresh_status가 pack_forget으로 일단 숨김.
        self.timeline_frame.pack(fill="x", padx=12, pady=(0, 6))
        self.timeline_frame.pack_forget()
        # timeline 데이터 캐시 — 네트워크 실패 시 마지막 데이터 유지.
        self._timeline_data = None

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

        # ① KIS 자격증명 — 3-step wizard
        # Step 1: 안내·KIS 포털 deep-link  Step 2: 모의/실전 모드  Step 3: 입력+테스트+저장
        # 자격증명 미등록 시 Step 1부터, 재진입(⚙ 변경) 시 Step 3 직행.
        self.kf = ttk.LabelFrame(self.setup_expanded, text="① KIS 자격증명")
        self.kf.pack(fill="x", **pad)
        self._build_kis_wizard(self.kf)

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

        # 새로고침 버튼 — ref 보관 (wizard 변경 모드 진입 시 숨김)
        self.refresh_btn = ttk.Button(self.root, text="새로고침",
                                       command=self.refresh_status)
        self.refresh_btn.pack(anchor="e", padx=14, pady=(0, 10))

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

    # ── 자동매매 timeline 패널 ───────────────────────────────────────────────
    # 데이터 단일 출처: 서버 GET /sync/timeline.
    # 웹앱 /monitor의 TradingTimeline.tsx와 동일 events·status·detail 공유.

    KIND_LABEL = {
        "krx_preview":    "국장 매매 후보 결정",
        "krx_cycle":      "국장 자동매매 시작",
        "krx_settlement": "국장 자동매매 종료",
        "us_preview":     "미장 매매 후보 결정",
        "us_cycle":       "미장 자동매매 시작",
        "us_settlement":  "미장 자동매매 종료",
    }
    STATUS_ICON = {
        "done": "✓", "scheduled": "⏳", "missed": "✗", "holiday": "—",
    }

    def _schedule_minute_tick(self):
        """매분 timeline 갱신. countdown·heartbeat·신규 done 반영."""
        try:
            self.refresh_status()
        finally:
            self.root.after(60_000, self._schedule_minute_tick)

    def _refresh_timeline_panel_async(self):
        """timeline data fetch in background — UI thread block 안 함."""
        threading.Thread(target=self._fetch_and_render_timeline,
                          daemon=True, name="timeline-fetch").start()

    def _fetch_and_render_timeline(self):
        """background: 서버 /sync/timeline 호출 → root.after로 UI 그리기."""
        from .sync_client import pull_timeline
        data = pull_timeline()
        if data is not None:
            self._timeline_data = data
        # data가 None이면 캐시된 마지막 데이터 그대로 사용 (네트워크 일시 실패 견고함).
        self.root.after(0, self._render_timeline)

    def _render_timeline(self):
        """timeline_body 안의 모든 children을 destroy 후 재구성."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        kst = ZoneInfo("Asia/Seoul")

        # status header
        data = self._timeline_data
        if data is None:
            self.timeline_status_label.configure(text="불러오는 중…", fg=MUTED)
            return
        hb_status = data.get("heartbeat_status", "error")
        hb_at = data.get("heartbeat_at")
        hb_color = {"normal": GREEN, "warning": AMBER, "error": RED}.get(hb_status, MUTED)
        hb_text_map = {"normal": "정상", "warning": "응답 느림", "error": "연결 끊김"}
        hb_label = hb_text_map.get(hb_status, "?")
        if hb_at:
            try:
                hb_dt = datetime.fromisoformat(hb_at.replace("Z", "+00:00")).astimezone(kst)
                hb_rel = self._relative_time(hb_dt, datetime.now(kst))
                hb_subtext = f"  ·  로컬앱 {hb_rel}"
            except Exception:
                hb_subtext = ""
        else:
            hb_subtext = "  ·  로컬앱 응답 없음"
        self.timeline_status_label.configure(
            text=f"● {hb_label}{hb_subtext}", fg=hb_color)

        # body — 기존 children destroy 후 재구성
        for w in self.timeline_body.winfo_children():
            w.destroy()

        events = data.get("events") or []
        if not events:
            tk.Label(self.timeline_body, text="예정된 이벤트가 없습니다.",
                      bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(fill="x", pady=4)
            return

        # 날짜별 그룹 (KST 기준)
        now = datetime.now(kst)
        groups: dict[str, list[dict]] = {}
        for ev in events:
            try:
                ev_dt = datetime.fromisoformat(ev["at"]).astimezone(kst)
            except Exception:
                continue
            key = ev_dt.strftime("%Y-%m-%d")
            groups.setdefault(key, []).append((ev_dt, ev))
        # 날짜 정렬 (오래된 것부터)
        for key in sorted(groups.keys()):
            day_label = self._day_label(key, now)
            sep = tk.Frame(self.timeline_body, bg=PANEL)
            sep.pack(fill="x", pady=(6, 2))
            tk.Label(sep, text=day_label, bg=PANEL, fg=MUTED,
                      font=("Segoe UI", 8, "bold")).pack(side="left")
            tk.Frame(sep, bg=BORDER, height=1).pack(side="left", fill="x",
                                                     expand=True, padx=(8, 0), pady=(0, 0))
            for ev_dt, ev in sorted(groups[key], key=lambda x: x[0]):
                self._render_event_row(ev_dt, ev, now)

    def _render_event_row(self, ev_dt, ev, now):
        """단일 event row — 시각 · 종류 · 상태·요약."""
        row = tk.Frame(self.timeline_body, bg=PANEL)
        row.pack(fill="x", pady=1)
        kind = ev.get("kind", "")
        status = ev.get("status", "")
        # 좌측 시각
        tk.Label(row, text=ev_dt.strftime("%H:%M"), bg=PANEL, fg=TEXT,
                  font=("Segoe UI", 9, "bold"), width=6, anchor="w"
                  ).pack(side="left")
        # 종류
        kind_label = self.KIND_LABEL.get(kind, kind)
        tk.Label(row, text=kind_label, bg=PANEL, fg=TEXT,
                  font=("Segoe UI", 9), anchor="w"
                  ).pack(side="left")
        # 우측 status·요약
        icon = self.STATUS_ICON.get(status, "")
        if status == "scheduled":
            summary = self._relative_time(ev_dt, now)
            color = MUTED
        elif status == "done":
            summary = ev.get("summary") or ""
            color = GREEN
        elif status == "missed":
            summary = ev.get("summary") or "누락"
            color = RED
        elif status == "holiday":
            summary = ev.get("summary") or "휴장"
            color = MUTED
        else:
            summary, color = "", MUTED
        right = tk.Label(row, text=f"{icon}  {summary}".strip(), bg=PANEL,
                          fg=color, font=("Segoe UI", 9, "bold" if status == "missed" else "normal"))
        right.pack(side="right")
        # detail tooltip — Tkinter는 hover tooltip native 지원 X, 간단한 enter 이벤트로 status_label에 표시.
        detail = (ev.get("detail") or "").strip()
        if detail:
            row.bind("<Enter>", lambda e, d=detail: self.timeline_status_label.configure(
                text=d[:200], fg=MUTED))
            row.bind("<Leave>", lambda e: self._render_timeline())  # 복원

    @staticmethod
    def _day_label(date_str: str, now) -> str:
        """YYYY-MM-DD → '오늘 · 2026.05.27. (수)' 같은 친화적 라벨."""
        from datetime import datetime, timedelta
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return date_str
        today = now.date()
        weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
        diff = (d - today).days
        if diff == 0:
            return f"오늘 · {d.strftime('%Y.%m.%d.')} ({weekday_kr})"
        if diff == 1:
            return f"내일 · {d.strftime('%Y.%m.%d.')} ({weekday_kr})"
        if diff == -1:
            return f"어제 · {d.strftime('%Y.%m.%d.')} ({weekday_kr})"
        return f"{d.strftime('%Y.%m.%d.')} ({weekday_kr})"

    @staticmethod
    def _relative_time(target, now) -> str:
        """상대 시각 — '7h 30m 후', '3분 전'."""
        from datetime import timedelta
        delta = target - now
        secs = int(delta.total_seconds())
        future = secs > 0
        secs = abs(secs)
        if secs < 60:
            return "곧" if future else "방금"
        if secs < 3600:
            return f"{secs // 60}분 {'후' if future else '전'}"
        if secs < 86400:
            h, m = secs // 3600, (secs % 3600) // 60
            base = f"{h}h {m}m" if m else f"{h}h"
            return f"{base} {'후' if future else '전'}"
        d, h = secs // 86400, (secs % 86400) // 3600
        base = f"{d}일 {h}h" if h else f"{d}일"
        return f"{base} {'후' if future else '전'}"

    def _render_setup_area(self, kis_ok: bool, dev_ok: bool):
        """3가지 모드 layout — 한 번에 하나씩 진행:

          - normal:      hero · setup_bar · af · nb · refresh_btn
          - wizard_kis:  hero · ① wizard (kf)만           ※ pf/af/nb/refresh 숨김
          - wizard_pair: hero · ② 페어링 (pf)만           ※ kf/af/nb/refresh 숨김

        모드 결정:
          - 둘 다 완료 + setup_collapsed=True              → normal
          - kis 미등록                                      → wizard_kis (신규 1단계)
          - kis 등록, dev 미등록                            → wizard_pair (신규 2단계)
          - 둘 다 등록인데 ⚙로 펼침(setup_collapsed=False) → wizard_kis (자격증명 변경)

        ① wizard 완료 (Step 3 저장) → setup_collapsed=True 자동 설정 +
        kis_ok=True, dev_ok=False → wizard_pair로 자연 전환.
        """
        both_ok = kis_ok and dev_ok
        if both_ok and self.setup_collapsed:
            new_mode = "normal"
        elif not kis_ok:
            new_mode = "wizard_kis"
        elif not dev_ok:
            new_mode = "wizard_pair"
        else:
            # both_ok이지만 사용자가 ⚙ 클릭으로 펼침 → 자격증명 변경 모드
            new_mode = "wizard_kis"

        if getattr(self, "_setup_mode", None) != new_mode:
            # 모드 전환 — 관련 위젯 모두 pack_forget 후 새 모드 순서로 재pack
            for w in (self.setup_bar, self.setup_expanded, self.kf, self.pf,
                       self.af, self.nb, self.refresh_btn):
                w.pack_forget()
            if new_mode == "normal":
                self.setup_bar.pack(fill="x", padx=12, pady=(4, 6))
                self.af.pack(fill="x", padx=12, pady=(4, 6))
                self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 4))
                self.refresh_btn.pack(anchor="e", padx=14, pady=(0, 10))
            elif new_mode == "wizard_kis":
                self.setup_expanded.pack(fill="x")
                self.kf.pack(fill="x", padx=12, pady=(4, 6))
            else:  # wizard_pair
                self.setup_expanded.pack(fill="x")
                self.pf.pack(fill="x", padx=12, pady=(4, 6))
            self._setup_mode = new_mode

        # normal 모드 — bar 라벨 매번 갱신 (모드·키 변경 즉시 반영)
        if new_mode == "normal":
            kis = secrets_store.load_kis()
            mode = ""
            if kis:
                mode = "모의" if kis.get("virtual", True) else "실전"
            parts = []
            parts.append(f"✓ KIS 자격증명 ({mode})" if mode else "✓ KIS 자격증명 등록됨")
            parts.append("✓ 플랫폼 계정 연결됨")
            self.setup_bar_label.configure(text="  ·  ".join(parts))

    def _toggle_setup_expanded(self):
        """⚙ 변경 버튼 — 한 줄 bar ↔ 펼친 LabelFrame 토글.

        펼칠 때 wizard는 Step 3(입력)로 직행 — 이미 자격증명 있는 사용자가
        Step 1·2 안내를 다시 거치지 않도록.
        """
        was_collapsed = self.setup_collapsed
        self.setup_collapsed = not self.setup_collapsed
        if was_collapsed and secrets_store.load_kis():
            self._wizard_jump_to_input()
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

        # 자동매매 timeline 패널 — scheduler 가동 중일 때만 표시.
        # 서버 /sync/timeline 호출해 어제·오늘·내일 6 종류 event 렌더.
        # after=self.hero로 hero 직후 위치 강제(repack 시 root 끝 밀림 방지).
        if running and not ks_active:
            self.timeline_frame.pack(fill="x", padx=12, pady=(0, 6), after=self.hero)
            self._refresh_timeline_panel_async()
        else:
            self.timeline_frame.pack_forget()

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

        # 단계 헤더에 진행 상태 표시 — 저장된 모드(virtual) 기반 라벨
        if kis:
            mode = "모의투자" if kis.get("virtual", True) else "실전투자"
            kis_header = f"① KIS 자격증명 ({mode})        ✓ 등록됨"
        else:
            kis_header = "① KIS 자격증명        입력 필요"
        self.kf.configure(text=kis_header)
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

        # wizard sub-card variant 갱신 — init 시점 keyring race 방어 +
        # 사용자가 자격증명 저장·삭제 직후 즉시 active↔done 반영.
        if hasattr(self, "_sub1_active"):
            self._wizard_show_substep(self.wizard_substep)

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

    # ── KIS 자격증명 wizard ────────────────────────────────────────────────────

    def _build_kis_wizard(self, parent):
        """3-step wizard 구성.

        Step 1: KIS 계좌·Open API 신청 안내 + deep-link
        Step 2: 모의/실전 모드 선택 (기본 모의, 실전은 빨강 경고)
        Step 3: 자격증명 입력 + 연결 테스트 + 저장

        Wizard state는 GUI 인스턴스에 직접 보관:
          self.wizard_step (1-3)
          self.wizard_virtual (bool, Step 2에서 결정 — 기본 True)
          self.wizard_test_ok (bool, Step 3 연결 테스트 통과 여부)

        e_key/e_secret/e_acct는 기존 코드(refresh_status 등)와의 호환 위해
        instance attr로 유지.
        """
        self.wizard_step = 1
        self.wizard_virtual = True
        self.wizard_test_ok = False

        # 단일 컨테이너 — 각 step은 안에서 grid_remove로 토글
        self.wizard_box = tk.Frame(parent, bg=PANEL)
        self.wizard_box.pack(fill="x", padx=12, pady=8)

        self._wizard_step_frames = {
            1: self._build_wizard_step1(self.wizard_box),
            2: self._build_wizard_step2(self.wizard_box),
            3: self._build_wizard_step3(self.wizard_box),
        }
        self._wizard_show_step(1)

    def _build_wizard_step1(self, parent) -> tk.Frame:
        """Step 1 — KIS Open API 준비 (3개 sub-card 순차).

        한꺼번에 모든 안내를 던지지 않고 ‘있나요? → 있으면 다음 / 없으면 신청 페이지’
        분기형으로 1-1(증권계좌) → 1-2(API신청·키발급) → 1-3(모의계좌) 진행.
        """
        f = tk.Frame(parent, bg=PANEL)
        # 동적 헤더 — sub-step 변경 시 갱신
        self._wizard_step1_header = ttk.Label(
            f, text="1 / 3   ·   KIS Open API 준비   (1-1)",
            font=("Segoe UI", 11, "bold"),
            background=PANEL, foreground=TEXT)
        self._wizard_step1_header.pack(anchor="w")

        # sub-card 컨테이너 — 안에 3개 card 중 하나만 pack
        sub_container = tk.Frame(f, bg=PANEL)
        sub_container.pack(fill="x", pady=(10, 0))

        self.wizard_substep = 1
        self._wizard_substep_frames = {
            1: self._build_substep_account(sub_container),
            2: self._build_substep_api(sub_container),
            3: self._build_substep_mock(sub_container),
        }
        self._wizard_show_substep(1)
        return f

    def _build_substep_account(self, parent) -> tk.Frame:
        """1-1: 한국투자증권 증권계좌. active + done 두 variant.

        한 sub-card 안에 두 frame을 만들고 _wizard_show_substep이 등록 상태 보고 토글.
        등록된 사용자에게는 "✓ 이미 완료" 압축 UI 노출.
        """
        f = tk.Frame(parent, bg=PANEL)
        self._sub1_active = self._build_sub_active(
            f, idx="①",
            question="한국투자증권 계좌가 있으신가요?",
            description="KIS Open API는 한투 증권 계좌가 있어야 사용할 수 있습니다. "
                        "없으면 비대면으로 무료·즉시 개설할 수 있습니다.",
            link_text="🌐 비대면 계좌개설",
            link_url="https://www.truefriend.com/main/customer/guide/_static/TF04ac010000.shtm",
            prev_sub=None,
            next_text="✓ 계좌 있어요, 다음 →",
            next_cmd=lambda: self._wizard_show_substep(2))
        self._sub1_done = self._build_sub_done(
            f, idx="①", title="한국투자증권 계좌",
            link_text="🌐 비대면 계좌개설 (변경 시)",
            link_url="https://www.truefriend.com/main/customer/guide/_static/TF04ac010000.shtm",
            prev_sub=None,
            next_cmd=lambda: self._wizard_show_substep(2))
        return f

    def _build_substep_api(self, parent) -> tk.Frame:
        """1-2: KIS Open API 서비스 신청·키 발급. active + done 두 variant."""
        f = tk.Frame(parent, bg=PANEL)
        self._sub2_active = self._build_sub_active(
            f, idx="②",
            question="KIS Open API 신청·키 발급하셨나요?",
            description="KIS Open API 포털에서 서비스 신청 후, 마이페이지에서 "
                        "App Key · App Secret을 발급받습니다. (신청 즉시 발급)",
            link_text="🌐 KIS Open API 신청",
            link_url="https://apiportal.koreainvestment.com/intro",
            prev_sub=1,
            next_text="✓ 키 발급받았어요, 다음 →",
            next_cmd=lambda: self._wizard_show_substep(3))
        self._sub2_done = self._build_sub_done(
            f, idx="②", title="KIS Open API · App Key/Secret 발급",
            link_text="🌐 KIS Open API 포털 (키 재발급 시)",
            link_url="https://apiportal.koreainvestment.com/intro",
            prev_sub=1,
            next_cmd=lambda: self._wizard_show_substep(3))
        return f

    def _build_substep_mock(self, parent) -> tk.Frame:
        """1-3: 모의투자 가상계좌. active + done 두 variant."""
        f = tk.Frame(parent, bg=PANEL)
        self._sub3_active = self._build_sub_active(
            f, idx="③",
            question="모의투자 가상계좌 발급받으셨나요?",
            description="모의투자는 실제 자금 없이 전략을 검증할 수 있는 가상계좌입니다. "
                        "별도 신청이 필요하지만 무료·즉시 발급됩니다. "
                        "실전만 사용할 거면 이 단계는 건너뛰셔도 됩니다.",
            link_text="🌐 모의투자 신청",
            link_url="https://securities.koreainvestment.com/main/customer/systemdown/MockInvest.jsp",
            prev_sub=2,
            next_text="✓ 발급받았어요, 다음 →",
            next_cmd=lambda: self._wizard_show_step(2))
        self._sub3_done = self._build_sub_done(
            f, idx="③", title="모의투자 가상계좌",
            link_text="🌐 모의투자 신청 (변경 시)",
            link_url="https://securities.koreainvestment.com/main/customer/systemdown/MockInvest.jsp",
            prev_sub=2,
            next_cmd=lambda: self._wizard_show_step(2))
        return f

    def _build_sub_active(self, parent, *, idx: str, question: str,
                           description: str, link_text: str, link_url: str,
                           prev_sub: int | None, next_text: str, next_cmd) -> tk.Frame:
        """신규 사용자용 sub-card — 질문 + 설명 + 신청 링크 + nav (한 row).

        link 버튼과 [다음 →]을 같은 row에 두어 시선 흐름 단축.
        """
        f = tk.Frame(parent, bg=PANEL)
        ttk.Label(f, text=f"{idx}  {question}",
                  font=("Segoe UI", 10, "bold"),
                  background=PANEL, foreground=TEXT).pack(anchor="w", pady=(0, 4))
        ttk.Label(f, style="Muted.TLabel", wraplength=560, justify="left",
                  text=description).pack(anchor="w", pady=(0, 12))

        # link 버튼과 nav 버튼을 같은 row에 — link는 좌측, nav는 우측
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(0, 2))
        ttk.Button(row, text=link_text,
                   command=lambda: webbrowser.open(link_url)
                   ).pack(side="left")
        # 우측에 [다음 →] (Accent), 그 옆에 [← 이전] (있으면)
        ttk.Button(row, text=next_text, style="Accent.TButton",
                   command=next_cmd).pack(side="right")
        if prev_sub is not None:
            ttk.Button(row, text="← 이전",
                       command=lambda: self._wizard_show_substep(prev_sub)
                       ).pack(side="right", padx=(0, 6))
        return f

    def _build_sub_done(self, parent, *, idx: str, title: str,
                         link_text: str, link_url: str,
                         prev_sub: int | None, next_cmd) -> tk.Frame:
        """등록된 사용자용 sub-card — ✓ 완료 chip + 변경 시 신청 링크 + nav (한 row).

        ✓ 표시에 ACCENT_SOFT 배경 chip을 둘러 ‘완료’ 시각적 강조. 신청 페이지
        링크는 ‘변경 시’ 보조 액션으로 작게.
        """
        f = tk.Frame(parent, bg=PANEL)
        # ✓ chip — ACCENT_SOFT 배경에 GREEN ✓ + 제목 + 보조 설명
        chip = tk.Frame(f, bg=ACCENT_SOFT, highlightbackground=BORDER,
                         highlightthickness=1)
        chip.pack(fill="x", pady=(0, 10))
        tk.Label(chip, text=f"{idx}  ", bg=ACCENT_SOFT, fg=MUTED,
                  font=("Segoe UI", 10, "bold")).pack(side="left",
                                                       padx=(10, 0), pady=8)
        tk.Label(chip, text="✓", bg=ACCENT_SOFT, fg=GREEN,
                  font=("Segoe UI", 12, "bold")).pack(side="left", padx=(0, 6),
                                                      pady=8)
        tk.Label(chip, text=title, bg=ACCENT_SOFT, fg=TEXT,
                  font=("Segoe UI", 10, "bold")).pack(side="left", pady=8)
        tk.Label(chip, text="  ·  이미 완료되어 있습니다",
                  bg=ACCENT_SOFT, fg=MUTED,
                  font=("Segoe UI", 9)).pack(side="left", pady=8, padx=(0, 10))

        # 변경 시 신청 페이지 링크 + nav 한 row
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text=link_text,
                   command=lambda: webbrowser.open(link_url)
                   ).pack(side="left")
        ttk.Button(row, text="다음 →", style="Accent.TButton",
                   command=next_cmd).pack(side="right")
        if prev_sub is not None:
            ttk.Button(row, text="← 이전",
                       command=lambda: self._wizard_show_substep(prev_sub)
                       ).pack(side="right", padx=(0, 6))
        return f

    _SUBSTEP_TITLES = {
        1: "한국투자증권 계좌",
        2: "KIS Open API 신청",
        3: "모의투자 가상계좌",
    }

    def _wizard_show_substep(self, sub: int) -> None:
        """Step 1 내부 sub-card 토글 + sub-step별 단계명 헤더 갱신.

        등록된 사용자(load_kis() not None)에게는 sub-card의 done variant 노출,
        신규 사용자에게는 active variant.
        """
        self.wizard_substep = sub
        is_registered = bool(secrets_store.load_kis())
        for s, frame in self._wizard_substep_frames.items():
            if s == sub:
                frame.pack(fill="x")
            else:
                frame.pack_forget()
        # 현재 sub-card 안의 active/done variant 토글
        active_map = {1: self._sub1_active, 2: self._sub2_active, 3: self._sub3_active}
        done_map = {1: self._sub1_done, 2: self._sub2_done, 3: self._sub3_done}
        if is_registered:
            active_map[sub].pack_forget()
            done_map[sub].pack(fill="x")
            for other in (1, 2, 3):
                if other != sub:
                    active_map[other].pack_forget()
                    done_map[other].pack_forget()
        else:
            done_map[sub].pack_forget()
            active_map[sub].pack(fill="x")
            for other in (1, 2, 3):
                if other != sub:
                    active_map[other].pack_forget()
                    done_map[other].pack_forget()
        title = self._SUBSTEP_TITLES.get(sub, "")
        self._wizard_step1_header.configure(
            text=f"1 / 3 (1-{sub})   ·   {title}")

    def _build_wizard_step2(self, parent) -> tk.Frame:
        """Step 2 — 모의/실전 모드 선택 (카드 hover/select 반응)."""
        f = tk.Frame(parent, bg=PANEL)
        ttk.Label(f, text="2 / 3   ·   거래 모드 선택",
                  font=("Segoe UI", 11, "bold"),
                  background=PANEL, foreground=TEXT).pack(anchor="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=580, justify="left",
                  text="자동매매를 어느 모드에서 실행할지 선택하세요. "
                       "나중에 변경할 수 있지만, 모드별로 KIS App Key·Secret이 다릅니다."
                  ).pack(anchor="w", pady=(8, 12))

        self._wizard_virtual_var = tk.StringVar(value="virtual")
        self._wizard_hover_v = False
        self._wizard_hover_r = False

        # 모의 카드 — 권장
        self._card_v = tk.Frame(f, bg=PANEL, highlightbackground=BORDER,
                                 highlightthickness=1, cursor="hand2")
        self._card_v.pack(fill="x", pady=(0, 8))
        self._head_v = tk.Frame(self._card_v, bg=PANEL, cursor="hand2")
        self._head_v.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Radiobutton(
            self._head_v, text="🧪  모의투자",
            variable=self._wizard_virtual_var, value="virtual",
            command=self._wizard_on_mode_change).pack(side="left")
        tk.Label(self._head_v, text="권장", bg=ACCENT_SOFT, fg=ACCENT,
                  font=("Segoe UI", 9, "bold"),
                  padx=8, pady=2).pack(side="left", padx=(8, 0))
        self._desc_v = tk.Label(
            self._card_v, bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9), wraplength=540, justify="left",
            cursor="hand2",
            text="가상 자금으로 거래. KIS 모의투자 계좌의 App Key·Secret 필요. "
                 "실거래 없이 전략 검증에 사용합니다.")
        self._desc_v.pack(anchor="w", padx=36, pady=(2, 10))

        # 실전 카드
        self._card_r = tk.Frame(f, bg=PANEL, highlightbackground=BORDER,
                                 highlightthickness=1, cursor="hand2")
        self._card_r.pack(fill="x", pady=(0, 4))
        self._head_r = tk.Frame(self._card_r, bg=PANEL, cursor="hand2")
        self._head_r.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Radiobutton(
            self._head_r, text="🔥  실전투자",
            variable=self._wizard_virtual_var, value="real",
            command=self._wizard_on_mode_change).pack(side="left")
        tk.Label(self._head_r, text="주의", bg="#fef2f2", fg=RED,
                  font=("Segoe UI", 9, "bold"),
                  padx=8, pady=2).pack(side="left", padx=(8, 0))
        # 실전 모드 경고 — 카드 배경 따라 색 갱신되므로 ref 보관
        self._wizard_real_warn = tk.Label(
            self._card_r, bg=PANEL, fg=RED, wraplength=540, justify="left",
            font=("Segoe UI", 9), cursor="hand2",
            text="⚠ 실전 모드는 사용자 실제 자금으로 거래합니다. "
                 "전략·조건을 충분히 모의에서 검증한 뒤에만 사용하세요.")
        self._wizard_real_warn.pack(anchor="w", padx=36, pady=(2, 10))

        # 카드 전체 클릭 → 라디오 선택. hover → 시각 강조.
        def select_v(_e=None):
            self._wizard_virtual_var.set("virtual")
            self._wizard_on_mode_change()

        def select_r(_e=None):
            self._wizard_virtual_var.set("real")
            self._wizard_on_mode_change()

        for w in (self._card_v, self._head_v, self._desc_v):
            w.bind("<Button-1>", select_v)
            w.bind("<Enter>", lambda _e: self._set_card_hover("v", True))
            w.bind("<Leave>", lambda _e: self._set_card_hover("v", False))
        for w in (self._card_r, self._head_r, self._wizard_real_warn):
            w.bind("<Button-1>", select_r)
            w.bind("<Enter>", lambda _e: self._set_card_hover("r", True))
            w.bind("<Leave>", lambda _e: self._set_card_hover("r", False))

        self._refresh_step2_cards()  # 초기 색상 적용

        nav = ttk.Frame(f)
        nav.pack(fill="x", pady=(14, 0))
        ttk.Button(nav, text="← 이전",
                   command=lambda: self._wizard_show_step(1)).pack(side="left")
        ttk.Button(nav, text="다음 →", style="Accent.TButton",
                   command=self._wizard_step2_next).pack(side="right")
        return f

    def _set_card_hover(self, which: str, hover: bool) -> None:
        """Step 2 카드 hover state 토글 + 색상 갱신."""
        if which == "v":
            self._wizard_hover_v = hover
        else:
            self._wizard_hover_r = hover
        self._refresh_step2_cards()

    def _refresh_step2_cards(self) -> None:
        """선택·hover 상태에 따라 두 카드의 색상 갱신.

        - 선택됨: ACCENT_SOFT 배경 + ACCENT 테두리 2px (강조)
        - hover  : 옅은 hover 톤 + BORDER 2px
        - default: PANEL + BORDER 1px
        """
        selected = self._wizard_virtual_var.get()
        # 모의 카드
        if selected == "virtual":
            self._paint_card(self._card_v, self._head_v, self._desc_v,
                              bg=ACCENT_SOFT, border=ACCENT, thickness=2)
        elif self._wizard_hover_v:
            self._paint_card(self._card_v, self._head_v, self._desc_v,
                              bg="#f5f0e9", border=ACCENT, thickness=2)
        else:
            self._paint_card(self._card_v, self._head_v, self._desc_v,
                              bg=PANEL, border=BORDER, thickness=1)
        # 실전 카드
        if selected == "real":
            self._paint_card(self._card_r, self._head_r, self._wizard_real_warn,
                              bg="#fef2f2", border=RED, thickness=2)
        elif self._wizard_hover_r:
            self._paint_card(self._card_r, self._head_r, self._wizard_real_warn,
                              bg="#fef7f7", border=RED, thickness=2)
        else:
            self._paint_card(self._card_r, self._head_r, self._wizard_real_warn,
                              bg=PANEL, border=BORDER, thickness=1)

    def _paint_card(self, card, head, desc, *, bg: str, border: str,
                     thickness: int) -> None:
        """카드 frame + 안의 widget bg/border 일괄 갱신."""
        card.configure(bg=bg, highlightbackground=border,
                        highlightthickness=thickness)
        head.configure(bg=bg)
        desc.configure(bg=bg)

    def _build_wizard_step3(self, parent) -> tk.Frame:
        """Step 3 — 자격증명 입력 + 단일 액션 버튼 (연결 테스트 → 저장).

        UI 단순화: nav 우측에 버튼 1개만. 처음엔 [🔌 연결 테스트], 성공하면 같은
        자리가 [💾 저장]으로 변신. 입력값 바뀌면 다시 [🔌 연결 테스트]로 reset.
        """
        f = tk.Frame(parent, bg=PANEL)
        self._wizard_step3_title = ttk.Label(
            f, text="3 / 3   ·   자격증명 입력 (모의투자)",
            font=("Segoe UI", 11, "bold"),
            background=PANEL, foreground=TEXT)
        self._wizard_step3_title.pack(anchor="w")
        ttk.Label(
            f, style="Muted.TLabel", wraplength=580, justify="left",
            text="KIS 마이페이지에서 발급받은 App Key · App Secret · 계좌번호를 입력하세요. "
                 "키는 이 PC의 Windows 자격증명 저장소에만 저장되며, 플랫폼 서버로 "
                 "전송되지 않습니다."
        ).pack(anchor="w", pady=(8, 8))

        # 입력란
        self.e_key = self._make_wizard_entry(f, "App Key")
        self.e_secret = self._make_wizard_entry(f, "App Secret", show="*")
        self.e_acct = self._make_wizard_entry(f, "계좌번호 (예: 50001234-01)")

        # 입력 변경 시 결과·버튼 reset (다시 테스트해야 저장 가능)
        for ent in (self.e_key, self.e_secret, self.e_acct):
            ent.bind("<KeyRelease>", lambda _e: self._wizard_on_input_change())

        # 결과·진행 상태 (한 줄 안내)
        self._wizard_test_status = tk.Label(
            f, bg=PANEL, fg=MUTED, font=("Segoe UI", 9),
            text="App Key·Secret·계좌번호 입력 후 ‘연결 테스트’를 누르세요.",
            wraplength=580, justify="left", anchor="w")
        self._wizard_test_status.pack(anchor="w", pady=(12, 0))

        # nav — [← 이전]과 [🔌 연결 테스트 / 💾 저장] 한 버튼
        nav = ttk.Frame(f)
        nav.pack(fill="x", pady=(14, 0))
        ttk.Button(nav, text="← 이전",
                   command=lambda: self._wizard_show_step(2)).pack(side="left")
        self._wizard_action_btn = ttk.Button(
            nav, text="🔌 연결 테스트", style="Accent.TButton",
            command=self._wizard_test_connection)
        self._wizard_action_btn.pack(side="right")
        return f

    def _make_wizard_entry(self, parent, label: str, show: str | None = None) -> ttk.Entry:
        """Wizard 전용 labeled entry — _labeled_entry는 LabelFrame 가정이라 자체 헬퍼.

        붙여넣기 자동 정화: focus out 시 \\n·\\r·공백 trim.
        """
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=22, anchor="w").pack(side="left")
        ent = ttk.Entry(row)
        if show:
            ent.configure(show=show)
        ent.pack(side="left", fill="x", expand=True)

        def _sanitize(_e=None):
            v = ent.get()
            # 줄바꿈·탭·앞뒤 공백 제거 (KIS 키 복사 시 흔한 오염)
            cleaned = v.replace("\r", "").replace("\n", "").replace("\t", "").strip()
            if cleaned != v:
                ent.delete(0, "end")
                ent.insert(0, cleaned)
        ent.bind("<FocusOut>", _sanitize)
        # 붙여넣기 직후에도 정화
        ent.bind("<<Paste>>", lambda _e: ent.after(1, _sanitize))
        return ent

    def _wizard_show_step(self, step: int) -> None:
        """Step 1-3 중 하나만 보이게 토글."""
        self.wizard_step = step
        for s, frame in self._wizard_step_frames.items():
            if s == step:
                frame.pack(fill="x")
            else:
                frame.pack_forget()

    def _wizard_on_mode_change(self) -> None:
        """Step 2 라디오 변경 시 호출 — 카드 색상 갱신 + 모드 state 동기화."""
        val = self._wizard_virtual_var.get()
        self.wizard_virtual = (val == "virtual")
        self._refresh_step2_cards()

    def _wizard_step2_next(self) -> None:
        """Step 2 → 3 전환. 실전 선택 시 confirm dialog."""
        val = self._wizard_virtual_var.get()
        self.wizard_virtual = (val == "virtual")
        if not self.wizard_virtual:
            ok = messagebox.askyesno(
                "실전 모드 확인",
                "실전 모드는 사용자 실제 자금으로 거래합니다.\n\n"
                "전략·조건이 모의에서 충분히 검증되었습니까?\n"
                "계속하시려면 ‘예’를 누르세요.")
            if not ok:
                self._wizard_virtual_var.set("virtual")
                self.wizard_virtual = True
                self._wizard_on_mode_change()
                return
        # Step 3 제목·테스트 결과 모드별로 갱신
        mode = "모의투자" if self.wizard_virtual else "실전투자"
        self._wizard_step3_title.configure(text=f"3 / 3   ·   자격증명 입력 ({mode})")
        self._wizard_reset_test_state()
        self._wizard_show_step(3)

    def _wizard_on_input_change(self) -> None:
        """Step 3 입력 변경 시 — 이전 테스트 결과 무효화."""
        if self.wizard_test_ok:
            self._wizard_reset_test_state()

    def _wizard_reset_test_state(self) -> None:
        """버튼·결과 라벨을 입력 직후 초기 상태로 — 다시 연결 테스트부터."""
        self.wizard_test_ok = False
        self._wizard_action_btn.configure(
            text="🔌 연결 테스트", command=self._wizard_test_connection)
        self._wizard_test_status.configure(
            fg=MUTED,
            text="App Key·Secret·계좌번호 입력 후 ‘연결 테스트’를 누르세요.")

    def _wizard_test_connection(self) -> None:
        """Step 3 액션 버튼이 [🔌 연결 테스트]인 상태에서 클릭됨.

        성공 시 같은 버튼이 [💾 저장]으로 변신하고 command가 _wizard_save로 교체.
        실패·입력 변경 시 다시 [🔌 연결 테스트]로 reset.
        """
        key = self.e_key.get().strip()
        secret = self.e_secret.get().strip()
        acct = self.e_acct.get().strip()
        if not (key and secret and acct):
            self._wizard_test_status.configure(
                fg=AMBER,
                text="App Key·Secret·계좌번호를 모두 입력하세요.")
            return

        self._wizard_action_btn.configure(state="disabled")
        self._wizard_test_status.configure(
            fg=MUTED, text="KIS 서버 호출 중...")

        def work():
            return kis_health.test_credentials(key, secret, acct,
                                                virtual=self.wizard_virtual)

        def done(result, err):
            self._wizard_action_btn.configure(state="normal")
            if err is not None:
                self.wizard_test_ok = False
                self._wizard_test_status.configure(
                    fg=RED, text=f"❌ 테스트 실패: {err}")
                return
            if result and result.get("ok"):
                # 성공 — 버튼을 저장으로 변신
                self.wizard_test_ok = True
                self._wizard_test_status.configure(
                    fg=GREEN, text=f"✓ {result['msg']}")
                self._wizard_action_btn.configure(
                    text="💾 저장", command=self._wizard_save)
            else:
                self.wizard_test_ok = False
                self._wizard_test_status.configure(
                    fg=RED,
                    text=f"❌ {result.get('msg', '알 수 없는 오류') if result else '알 수 없는 오류'}")

        self._run_bg(work, done)

    def _wizard_save(self) -> None:
        """Step 3 [저장] — 연결 테스트 통과 상태에서만 활성.

        저장 후 setup_collapsed=True → 페어링도 끝났으면 자동으로 정상 모드 복귀
        (③ 자동매매/Notebook 다시 표시). 페어링이 아직 안 끝났으면 ② 페어링만 펼친
        wizard 모드 유지.
        """
        if not self.wizard_test_ok:
            return
        key = self.e_key.get().strip()
        secret = self.e_secret.get().strip()
        acct = self.e_acct.get().strip()
        secrets_store.save_kis(key, secret, acct, virtual=self.wizard_virtual)
        self.e_secret.delete(0, "end")
        self.setup_collapsed = True
        mode = "모의투자" if self.wizard_virtual else "실전투자"
        messagebox.showinfo(
            "저장 완료",
            f"KIS 자격증명을 저장했습니다 ({mode}). "
            "키는 이 PC를 떠나지 않습니다.\n\n"
            "다음 단계: ② 플랫폼 계정 연결.")
        self.refresh_status()

    def _wizard_jump_to_input(self) -> None:
        """⚙ 변경 클릭 시 — Step 1 안내·모드 선택 건너뛰고 Step 3 직행.

        기존 자격증명 있으면 모드(virtual)도 그대로 유지. 사용자는 키 재발급
        받았을 때 Step 3만 다시 입력하면 됨.
        """
        kis = secrets_store.load_kis()
        if kis:
            self.wizard_virtual = bool(kis.get("virtual", True))
            self._wizard_virtual_var.set("virtual" if self.wizard_virtual else "real")
            mode = "모의투자" if self.wizard_virtual else "실전투자"
            self._wizard_step3_title.configure(text=f"3 / 3   ·   자격증명 입력 ({mode})")
        self._wizard_reset_test_state()
        self._wizard_show_step(3)

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
                    self.setup_collapsed = True  # 페어링 후 자동 정상 모드 복귀
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

    # ── Phase 7: Catch-up 결과 알림 ───────────────────────────────────────

    def _check_catchup_result_polling(self):
        """catchup_result.json polling — scheduler thread가 끝나면 파일 생성.

        5초 간격으로 최대 12회 (1분) 체크. 파일 있으면 amber 배너 표시.
        없으면 (catch-up plan 자체가 없었거나 결과 없음) 조용히 종료.
        """
        from . import catchup
        if catchup.CATCHUP_RESULT_PATH.exists():
            try:
                import json as _json
                data = _json.loads(
                    catchup.CATCHUP_RESULT_PATH.read_text(encoding="utf-8"))
                self._show_catchup_banner(data)
            except Exception as e:
                import logging
                logging.getLogger("localapp.gui").warning(
                    "catch-up 결과 파일 읽기 실패: %s", e)
            return
        self._catchup_poll_count += 1
        if self._catchup_poll_count < 12:
            self.root.after(5_000, self._check_catchup_result_polling)

    def _show_catchup_banner(self, data: dict):
        """catch-up 결과 amber 배너 표시 — hero 위, update_banner 아래."""
        msg = self._format_catchup_summary(data)
        if not msg:
            return
        self.catchup_banner_label.config(text=msg)
        # update_banner 있으면 그 아래, 없으면 hero 바로 위.
        if self.update_banner.winfo_ismapped():
            self.catchup_banner.pack(side="top", fill="x",
                                       before=self.hero)
        else:
            self.catchup_banner.pack(side="top", fill="x",
                                       before=self.hero)

    def _format_catchup_summary(self, data: dict) -> str:
        """결과 dict → 한 줄 사용자 메시지."""
        results = data.get("results") or {}
        if not results:
            return ""
        parts = ["⏰ 자동 catch-up 실행됨:"]
        for k, v in results.items():
            if v.get("error"):
                parts.append(f"  · {k}: ❌ {v['error']}")
            elif k.endswith("_stop_loss"):
                checked = v.get("checked", 0)
                fired = v.get("fired", 0)
                if checked == 0:
                    continue  # 보유 종목 0건 — 표시 생략
                fire_mark = f"🔴 {fired}건 손절 발주" if fired > 0 else "✓ 손절선 안전"
                parts.append(f"  · {k}: 보유 {checked}건 → {fire_mark}")
            elif k.endswith("_cycle"):
                nb = v.get("n_bought", 0)
                ns = v.get("n_sold", 0)
                parts.append(f"  · {k}: 매수 {nb}건 매도 {ns}건")
            elif k.endswith("_settle"):
                applied = v.get("reconcile_applied", 0)
                drift = v.get("reconcile_drift", False)
                drift_mark = f"drift {applied}건 정정" if drift else "차이 없음"
                parts.append(f"  · {k}: reconcile {drift_mark}")
        return "\n".join(parts) if len(parts) > 1 else ""

    def _dismiss_catchup_banner(self):
        """사용자 [확인] 클릭 — 배너 숨김 + 결과 파일 삭제."""
        from . import catchup
        self.catchup_banner.pack_forget()
        try:
            catchup.CATCHUP_RESULT_PATH.unlink(missing_ok=True)
        except Exception:
            pass

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
