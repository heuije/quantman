import asyncio
import csv
import feedparser
import anthropic
import yfinance as yf
import requests
import json
import os
import re
import sys
from io import StringIO
from pathlib import Path

# Windows cp949 콘솔에서도 이모지 출력이 깨지지 않도록 stdout/stderr를 UTF-8로 고정
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import datetime, timedelta, timezone, date
from bs4 import BeautifulSoup
from calendar import timegm
from urllib.parse import urlparse

# ─────────────────────────────────────────────
# 설정 (환경변수에서 로드)
# ─────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

missing = [k for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "TELEGRAM_CHAT_ID": CHAT_ID,
}.items() if not v]
if missing:
    print(f"❌ 환경변수 누락: {', '.join(missing)}\n"
          f"   {BASE_DIR / '.env'} 파일을 확인하세요. .env.example 참고.", file=sys.stderr)
    sys.exit(1)

CHAT_ID = int(CHAT_ID)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────
# 타임존 정의
# ─────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
ET = timezone(timedelta(hours=-4))   # EDT (3월~11월)
EST = timezone(timedelta(hours=-5))  # EST (11월~3월)


def get_et_now():
    """
    현재 미국 동부 시간(ET)을 반환.
    DST(3월 둘째 일요일 ~ 11월 첫째 일요일): EDT(UTC-4)
    그 외: EST(UTC-5)
    """
    utc_now = datetime.now(timezone.utc)
    year = utc_now.year

    # 3월 둘째 일요일 02:00 EST → EDT 전환
    mar1 = date(year, 3, 1)
    dst_start_day = 14 - mar1.weekday()
    if dst_start_day <= 7:
        dst_start_day += 7
    dst_start = datetime(year, 3, dst_start_day, 2, 0, 0, tzinfo=EST)

    # 11월 첫째 일요일 02:00 EDT → EST 전환
    nov1 = date(year, 11, 1)
    dst_end_day = 7 - nov1.weekday()
    if dst_end_day == 0:
        dst_end_day = 7
    dst_end = datetime(year, 11, dst_end_day, 2, 0, 0, tzinfo=ET)

    if dst_start.astimezone(timezone.utc) <= utc_now < dst_end.astimezone(timezone.utc):
        return utc_now.astimezone(ET)
    else:
        return utc_now.astimezone(EST)


# ─────────────────────────────────────────────
# 미국 시장 휴장일 (NYSE / NASDAQ 공통)
# ─────────────────────────────────────────────
US_MARKET_HOLIDAYS = {
    # ── 2025 ──
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19),
    date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
    date(2025, 12, 25),
    # ── 2026 ──
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
    # ── 2027 ──
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
    date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25),
    date(2027, 12, 24),
}

US_EARLY_CLOSE = {
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}


def is_us_market_open():
    now_et = get_et_now()
    today = now_et.date()
    if now_et.weekday() >= 5:
        return False
    if today in US_MARKET_HOLIDAYS:
        return False
    current_time = now_et.time()
    market_open = datetime.strptime("09:30", "%H:%M").time()
    if today in US_EARLY_CLOSE:
        market_close = datetime.strptime("13:00", "%H:%M").time()
    else:
        market_close = datetime.strptime("16:00", "%H:%M").time()
    return market_open <= current_time <= market_close


def is_forex_market_open():
    now_et = get_et_now()
    today = now_et.date()
    current_time = now_et.time()
    weekday = now_et.weekday()
    forex_holidays = {d for d in US_MARKET_HOLIDAYS
                      if (d.month == 12 and d.day == 25)
                      or (d.month == 1 and d.day == 1)}
    if today in forex_holidays:
        return False
    if weekday == 5:
        return False
    if weekday == 6:
        return current_time >= datetime.strptime("17:00", "%H:%M").time()
    if weekday == 4:
        return current_time <= datetime.strptime("17:00", "%H:%M").time()
    return True


def get_market_status_message():
    now_et = get_et_now()
    now_kst = datetime.now(KST)
    us_open = is_us_market_open()
    fx_open = is_forex_market_open()
    status = f"🕐 현재 시각: {now_kst.strftime('%H:%M')} KST / {now_et.strftime('%H:%M')} ET\n"
    status += f"🇺🇸 미국 정규장: {'🟢 개장 중' if us_open else '🔴 휴장'}\n"
    status += f"💱 Forex 시장: {'🟢 운영 중' if fx_open else '🔴 휴장'}"
    return status


# ─────────────────────────────────────────────
# RSS 피드 목록 (v2: 깨진 URL 수정/제거)
# ─────────────────────────────────────────────
RSS_FEEDS = [
    # ── EV 전문 미디어 ──────────────────────────────────────────
    "https://www.electrive.com/feed/",
    "https://electrek.co/feed/",
    # insideevs.com/rss/articles/ → 404 발생, Google News 경유로 대체
    "https://news.google.com/rss/search?q=site:insideevs.com&hl=en-US&gl=US&ceid=US:en",
    "https://cleantechnica.com/feed/",
    # ── 배터리·에너지저장 전문 ──────────────────────────────────
    "https://batteriesnews.com/feed/",
    "https://pv-tech.org/feed/",
    "https://www.energy-storage.news/feed/",
    # ── 반도체 전문 ────────────────────────────────────────────
    "https://semiengineering.com/feed/",
    "https://www.eetimes.com/feed/",
    "https://semiconductor-today.com/rss.shtml",
    # ── 원자재·광업 ────────────────────────────────────────────
    "https://www.mining.com/feed/",
    "https://www.mining-technology.com/feed/",
    # ── 중국 EV·배터리 ─────────────────────────────────────────
    "https://news.google.com/rss/search?q=site:cnevpost.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=BYD+OR+CATL+OR+NIO+OR+Xpeng+OR+Li+Auto&hl=en-US&gl=US&ceid=US:en",
    # ── Reuters (Google News 경유) ──────────────────────────────
    "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com+battery+OR+EV+OR+semiconductor&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com+lithium+OR+cobalt+OR+nickel+OR+copper&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com+CATL+OR+BYD+OR+Tesla+OR+tariff&hl=en-US&gl=US&ceid=US:en",
    # ── Bloomberg (Google News 경유) ──────────────────────────
    "https://news.google.com/rss/search?q=when:24h+site:bloomberg.com+battery+OR+EV+OR+lithium+OR+semiconductor&hl=en-US&gl=US&ceid=US:en",
    # ── CNBC ───────────────────────────────────────────────────
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",
]

# ─────────────────────────────────────────────
# 키워드
# ─────────────────────────────────────────────
KEYWORDS = [
    "battery", "lithium", "cathode", "anode", "LFP", "NMC", "NCM",
    "solid state", "solid-state", "electrolyte", "cobalt", "nickel",
    "graphite", "silicon anode", "ESS", "energy storage",
    "semiconductor", "chip", "TSMC", "memory", "NAND", "DRAM", "HBM",
    "wafer", "foundry", "fab", "packaging", "advanced packaging",
    "electric vehicle", "EV", "BEV", "NEV", "BYD", "Tesla", "CATL",
    "charging", "gigafactory", "range", "battery pack",
    "NIO", "Xpeng", "Li Auto", "SAIC", "Changan", "Geely", "Great Wall",
    "Chery", "BAIC",
    "copper", "Copper", "mining", "lithium mine", "cobalt mine",
    "sodium", "iron phosphate",
    "Fed", "interest rate", "inflation", "tariff", "trade war",
    "Robot", "humanoid", "Autonomous Vehicle", "autonomous driving",
    "GM", "Ford", "Volkswagen", "Hyundai", "Kia",
    "삼성SDI", "LG에너지솔루션", "SK온", "배터리", "2차전지",
    "양극재", "전고체", "음극재", "동박", "로봇", "휴머노이드",
    "현대차", "에코프로", "리튬", "NAND", "국제유가", "금리",
    "에코프로비엠", "포스코퓨처엠", "엘앤에프",
]

# ─────────────────────────────────────────────
# 시세 설정
# ─────────────────────────────────────────────
COMMODITIES = {
    "WTI 유가 (USO)": "USO",
    "천연가스 (UNG)": "UNG",
    "구리 (CPER)": "CPER",
    "금 (GLD)": "GLD",
    "은 (SLV)": "SLV",
}

INDICES = {
    "S&P 500 (SPY)": "SPY",
    "나스닥100 (QQQ)": "QQQ",
    "다우존스 (DIA)": "DIA",
    "중국 (FXI)": "FXI",
    "일본 (EWJ)": "EWJ",
    "한국 (EWY)": "EWY",
}

FX = {
    "달러/원 (USD/KRW)": "KRW=X",
    "달러/엔 (USD/JPY)": "JPY=X",
    "달러/위안 (USD/CNY)": "CNY=X",
    "유로/달러 (EUR/USD)": "EURUSD=X",
    "달러 인덱스 (DXY)": "DX-Y.NYB",
}

TICKER_UNITS = {
    "KRW=X": "₩",
    "JPY=X": "¥",
    "CNY=X": "¥",
    "EURUSD=X": "$",
    "DX-Y.NYB": "",
}

# 미 국채 만기 — (한국어 라벨, Treasury CSV 컬럼명)
TREASURY_MATURITIES = [
    ("3개월",  "3 Mo"),
    ("1년",    "1 Yr"),
    ("3년",    "3 Yr"),
    ("5년",    "5 Yr"),
    ("10년",   "10 Yr"),
    ("20년",   "20 Yr"),
    ("30년",   "30 Yr"),
]

# ─────────────────────────────────────────────
# 본문 직접 수집 허용 도메인
# ─────────────────────────────────────────────
FETCHABLE_DOMAINS = [
    "electrek.co", "insideevs.com", "cleantechnica.com",
    "mining.com", "pv-tech.org", "electrive.com", "cnevpost.com",
    "batteriesnews.com", "semiengineering.com", "energy-storage.news",
    "mining-technology.com", "eetimes.com",
]

# ─────────────────────────────────────────────
# 상태 저장 (스크립트와 같은 폴더에 절대경로로)
# ─────────────────────────────────────────────
SENT_LINKS_FILE = str(BASE_DIR / "sent_links.json")


def load_sent_links():
    """sent_links.json 로드. 7일 이상 된 항목은 자동 정리."""
    if os.path.exists(SENT_LINKS_FILE):
        try:
            with open(SENT_LINKS_FILE, "r") as f:
                data = json.load(f)
            # v2: 타임스탬프 포함 형식 지원
            # 기존 리스트 형식이면 새 형식으로 마이그레이션
            if isinstance(data, list):
                now_ts = datetime.now(timezone.utc).timestamp()
                return {link: now_ts for link in data}
            elif isinstance(data, dict):
                # 7일 이상 된 항목 정리
                cutoff = datetime.now(timezone.utc).timestamp() - (7 * 86400)
                return {link: ts for link, ts in data.items() if ts > cutoff}
        except Exception:
            pass
    return {}


def save_sent_links():
    with open(SENT_LINKS_FILE, "w") as f:
        json.dump(sent_links, f)


sent_links = load_sent_links()  # dict: {link: timestamp}

# ─────────────────────────────────────────────
# 진단 카운터 (매 check_feeds 호출마다 리셋)
# ─────────────────────────────────────────────
diag = {
    "total_entries": 0,
    "already_sent": 0,
    "filtered_date": 0,
    "filtered_keyword": 0,
    "sent": 0,
    "feed_errors": 0,
}


# ─────────────────────────────────────────────
# 날짜 필터 (v2: 48시간 윈도우로 확장)
# ─────────────────────────────────────────────
def is_recent_article(entry):
    """
    v2: 최근 48시간 이내 기사인지 확인.
    - 기존 is_today_article은 KST 당일만 허용해서 너무 제한적이었음
    - 날짜 정보 없으면 통과 (True)
    - 파싱 실패하면 통과 (True)
    """
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return True  # 날짜 정보 없으면 허용
    try:
        entry_utc = datetime.fromtimestamp(timegm(published), tz=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        age = now_utc - entry_utc
        return age <= timedelta(hours=48)
    except Exception:
        return True  # 파싱 실패하면 허용


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def clean_html(raw):
    return BeautifulSoup(raw, "html.parser").get_text(separator=" ")


def is_relevant(title, summary):
    text = (clean_html(title) + " " + clean_html(summary)).lower()
    return any(kw.lower() in text for kw in KEYWORDS)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


def get_entry_text(entry):
    if hasattr(entry, "content") and entry.content:
        raw = entry.content[0].get("value", "")
        text = clean_html(raw).strip()
        if len(text) > 80:
            return text
    raw = entry.get("summary", "")
    text = clean_html(raw).strip()
    if text:
        return text
    return ""


def try_fetch_article(url):
    try:
        domain = urlparse(url).netloc.replace("www.", "")
        if not any(d in domain for d in FETCHABLE_DOMAINS):
            return None
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["nav", "header", "footer", "aside", "script",
                         "style", "figure", "figcaption", "noscript"]):
            tag.decompose()
        for selector in [
            "article", ".article-body", ".article__body",
            ".post-content", ".entry-content", ".story-body",
            ".content-body", "main",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(separator=" ").strip()
                if len(text) > 200:
                    return text[:3000]
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────
# Claude 요약 / 제목 번역
# ─────────────────────────────────────────────
def summarize(title, text):
    """본문이 충분(>=100자)할 때 한국어 3줄 요약. 실패하면 None → 호출자가 제목 번역으로 fallback."""
    clean_text = clean_html(text).strip()
    if len(clean_text) < 100:
        return None
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{
                "role": "user",
                "content": (
                    "다음 기사를 한국어로 3줄 요약해줘. "
                    "핵심 수치나 기업명을 반드시 포함해서 바로 요약만 써. "
                    "어떤 경우에도 '죄송', '본문', '제공', '어렵습니다', '전문' 같은 말은 절대 쓰지 마. "
                    "문장 어미는 '~세', '~중', '~전망', '~증대', '~확인', '~분석', '~알려져', "
                    "'~예정', '~추정', '~주장', '~조정', '~추세', '~상황', '~것으로 확인', "
                    "'~것으로 분석', '~것으로 알려져', '~것으로 추정' 등 "
                    "명사형 또는 축약형으로 끝내. "
                    "'~입니다', '~합니다', '~했습니다', '~됩니다', '~이다', '~했다' 같은 "
                    "서술형 어미는 절대 사용하지 마. "
                    "첫 번째·두 번째 문장 끝에는 마침표(.)를 붙이고, "
                    "마지막 세 번째 문장 끝에는 마침표(.)를 절대 붙이지 마. "
                    "무조건 요약 내용만 출력해:\n"
                    "제목: " + title + "\n내용: " + clean_text[:2500]
                )
            }]
        )
        result = response.content[0].text.strip()
        bad_phrases = [
            "죄송", "본문이", "제공해주시면", "제공해주시기", "어렵습니다",
            "불가능합니다", "전문을", "바랍니다", "파악되는", "다음과 같습니다",
            "제목만", "내용이 없",
        ]
        if any(phrase in result for phrase in bad_phrases):
            return None
        return result
    except Exception as e:
        print(f"  ⚠️ Claude 요약 오류: {e}")
        return None


def translate_title(title):
    """본문 확보 실패 시 제목만 한국어로 번역. 이미 한국어면 그대로 반환."""
    title = (title or "").strip()
    if not title:
        return None
    # 한글 글자가 5자 이상이면 이미 한국어로 간주
    if sum(1 for c in title if '가' <= c <= '힣') >= 5:
        return title
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "다음 영문 뉴스 제목을 자연스러운 한국어로 번역해. "
                    "번역문만 한 줄로 출력하고 다른 말은 절대 하지 마. "
                    "기업명·모델명·약어(EV, BYD, TSMC, NMC, LFP, ESS, GWh 등)는 영문 그대로 유지. "
                    "끝에 마침표는 붙이지 마:\n" + title
                )
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠️ 제목 번역 오류: {e}")
        return None


# ─────────────────────────────────────────────
# 뉴스 피드 체크 (v2: 진단 로그 강화)
# ─────────────────────────────────────────────
async def check_feeds():
    global diag
    diag = {
        "total_entries": 0, "already_sent": 0,
        "filtered_date": 0, "filtered_keyword": 0,
        "sent": 0, "feed_errors": 0,
    }

    for feed_url in RSS_FEEDS:
        feed_name = urlparse(feed_url).netloc.replace("www.", "")[:25]
        try:
            response = requests.get(feed_url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                print(f"  ❌ [{feed_name}] HTTP {response.status_code}")
                diag["feed_errors"] += 1
                continue

            feed = feedparser.parse(response.text)
            feed_total = len(feed.entries)
            feed_sent = 0
            feed_skip_sent = 0
            feed_skip_date = 0
            feed_skip_kw = 0

            for entry in feed.entries:
                link = entry.get("link", "")
                if not link:
                    continue

                diag["total_entries"] += 1

                # 이미 전송한 링크
                if link in sent_links:
                    feed_skip_sent += 1
                    diag["already_sent"] += 1
                    continue

                # 날짜 필터 (v2: 48시간)
                if not is_recent_article(entry):
                    feed_skip_date += 1
                    diag["filtered_date"] += 1
                    continue

                title = entry.get("title", "")
                body = get_entry_text(entry)

                if len(body) < 200:
                    fetched = try_fetch_article(link)
                    if fetched:
                        body = fetched

                # 키워드 필터
                if not is_relevant(title, body):
                    feed_skip_kw += 1
                    diag["filtered_keyword"] += 1
                    continue

                # Claude 요약 및 전송
                korean_summary = summarize(title, body)
                if korean_summary:
                    message = f"{title}\n\n{korean_summary}\n\n {link}"
                else:
                    # 본문 확보 실패 (구글뉴스 우회 등) → 제목만 한국어로 번역, 본문 생략
                    korean_title = translate_title(title)
                    if korean_title and korean_title != title:
                        message = f"{korean_title}\n(원제: {title})\n\n {link}"
                    else:
                        message = f"{title}\n\n {link}"

                await bot.send_message(chat_id=CHAT_ID, text=message)
                sent_links[link] = datetime.now(timezone.utc).timestamp()
                save_sent_links()
                feed_sent += 1
                diag["sent"] += 1
                await asyncio.sleep(2)

            # 피드별 요약 로그
            if feed_sent > 0 or feed_skip_date > 0 or feed_skip_kw > 0:
                print(f"  📋 [{feed_name}] 항목={feed_total} | 전송={feed_sent} | "
                      f"기전송={feed_skip_sent} | 날짜제외={feed_skip_date} | 키워드제외={feed_skip_kw}")

        except Exception as e:
            print(f"  ❌ [{feed_name}] {str(e)[:80]}")
            diag["feed_errors"] += 1

    # 전체 요약 로그
    print(f"뉴스 체크 완료 — 전송={diag['sent']} | "
          f"전체항목={diag['total_entries']} | 기전송={diag['already_sent']} | "
          f"날짜제외={diag['filtered_date']} | 키워드제외={diag['filtered_keyword']} | "
          f"피드오류={diag['feed_errors']}")


# ─────────────────────────────────────────────
# 시세 체크 (시장 상태 반영)
# ─────────────────────────────────────────────
def get_price_row(name, ticker):
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty or len(hist) < 2:
            return None, None
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None, None
        current = hist["Close"].iloc[-1]
        previous = hist["Close"].iloc[-2]
        change_pct = (current - previous) / previous * 100
        sign = "+" if change_pct >= 0 else ""
        arrow = "🔺" if change_pct >= 0 else "▼"
        unit = TICKER_UNITS.get(ticker, "$")
        row = f"{arrow} {name}: {unit}{round(current, 2)} ({sign}{round(change_pct, 2)}%)\n"
        alert = None
        if abs(change_pct) >= 5:
            direction = "🔺 급등" if change_pct > 0 else "▼ 급락"
            alert = f"{direction} {name} {round(change_pct, 2)}% 변동!"
        return row, alert
    except Exception as e:
        print(f"  ⚠️ {name} 가격 오류: {e}")
        return None, None


async def check_prices():
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    now_et = get_et_now()
    alerts = []
    us_open = is_us_market_open()
    fx_open = is_forex_market_open()

    if not us_open and not fx_open:
        print(f"[{now_kst}] 모든 시장 휴장 → 시세 체크 건너뜀")
        return

    # ── 원자재 (미국 정규장 시간에만) ─────────────────────
    if us_open:
        commodity_report = f"📊 원자재 가격 현황 (실시간)\n{now_kst} KST / {now_et.strftime('%H:%M')} ET\n\n"
        has_data = False
        for name, ticker in COMMODITIES.items():
            row, alert = get_price_row(name, ticker)
            if row:
                commodity_report += row
                has_data = True
            if alert:
                alerts.append(alert)
        if has_data:
            await bot.send_message(chat_id=CHAT_ID, text=commodity_report)
            await asyncio.sleep(1)
    else:
        print(f"[{now_kst}] 미국 정규장 휴장 → 원자재 시세 스킵")

    # ── 글로벌 지수 (미국 정규장 시간에만) ─────────────────
    if us_open:
        index_report = f"🌏 글로벌 주요 지수 동향 (실시간)\n{now_kst} KST / {now_et.strftime('%H:%M')} ET\n\n"
        has_data = False
        for name, ticker in INDICES.items():
            row, alert = get_price_row(name, ticker)
            if row:
                index_report += row
                has_data = True
            if alert:
                alerts.append(alert)
        if has_data:
            await bot.send_message(chat_id=CHAT_ID, text=index_report)
            await asyncio.sleep(1)
    else:
        print(f"[{now_kst}] 미국 정규장 휴장 → 지수 시세 스킵")

    # ── 환율 (Forex 운영 시간에만) ─────────────────────────
    if fx_open:
        fx_report = f"💱 주요 환율 (실시간)\n{now_kst} KST / {now_et.strftime('%H:%M')} ET\n\n"
        has_data = False
        for name, ticker in FX.items():
            row, alert = get_price_row(name, ticker)
            if row:
                fx_report += row
                has_data = True
            if alert:
                alerts.append(alert)
        if has_data:
            await bot.send_message(chat_id=CHAT_ID, text=fx_report)
    else:
        print(f"[{now_kst}] Forex 시장 휴장 → 환율 시세 스킵")

    for alert in alerts:
        await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ 가격 급변 알람\n{alert}")

    print(f"가격 체크 완료 (미국장: {'개장' if us_open else '휴장'}, Forex: {'운영' if fx_open else '휴장'})")


# ─────────────────────────────────────────────
# 미 국채 수익률 (U.S. Treasury 일별 CSV)
# ─────────────────────────────────────────────
def _fetch_treasury_csv_rows(year):
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        f"interest-rates/daily-treasury-rates.csv/{year}/all"
        "?type=daily_treasury_yield_curve&field_tdr_date_value_month=&page&_format=csv"
    )
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return []
    return list(csv.DictReader(StringIO(resp.text)))


def fetch_treasury_yields():
    """
    Treasury 공식 CSV에서 최신 + 직전 거래일 수익률 추출.
    반환: (기준일 문자열, [(라벨, 수익률%, 변동bp), ...]) 또는 None
    """
    try:
        now_year = datetime.now(timezone.utc).year
        rows = _fetch_treasury_csv_rows(now_year)
        # 연초로 데이터가 부족하면 전년도까지 끌어와 직전 거래일 채움
        if len(rows) < 2:
            rows = rows + _fetch_treasury_csv_rows(now_year - 1)
        if len(rows) < 2:
            return None

        latest, prev = rows[0], rows[1]  # CSV는 날짜 내림차순
        out = []
        for label, col in TREASURY_MATURITIES:
            try:
                a = float(latest[col])
                b = float(prev[col])
                bp = round((a - b) * 100)  # %p × 100 = bp
                out.append((label, a, bp))
            except (KeyError, ValueError, TypeError):
                out.append((label, None, None))
        return latest.get("Date", ""), out
    except Exception as e:
        print(f"  ⚠️ Treasury 수익률 조회 오류: {e}")
        return None


# ─────────────────────────────────────────────
# Telegram 커맨드 핸들러
# ─────────────────────────────────────────────
async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 데이터 수집 중...")
    await check_prices()
    await check_feeds()


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 시장 상태 + 최근 진단 정보"""
    msg = get_market_status_message()
    msg += f"\n\n📈 최근 뉴스 체크 통계:\n"
    msg += f"  전체 항목: {diag['total_entries']}\n"
    msg += f"  전송: {diag['sent']}\n"
    msg += f"  기전송 스킵: {diag['already_sent']}\n"
    msg += f"  날짜 제외: {diag['filtered_date']}\n"
    msg += f"  키워드 제외: {diag['filtered_keyword']}\n"
    msg += f"  피드 오류: {diag['feed_errors']}"
    await update.message.reply_text(msg)


async def force_prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """시장 상태 무시, 강제 시세 조회"""
    await update.message.reply_text("⏳ 강제 시세 조회 중...")
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    alerts = []

    commodity_report = f"📊 원자재 가격 현황 (강제 조회)\n{now}\n\n"
    for name, ticker in COMMODITIES.items():
        row, alert = get_price_row(name, ticker)
        if row:
            commodity_report += row
        if alert:
            alerts.append(alert)
    await bot.send_message(chat_id=CHAT_ID, text=commodity_report)
    await asyncio.sleep(1)

    index_report = f"🌏 글로벌 주요 지수 동향 (강제 조회)\n{now}\n\n"
    for name, ticker in INDICES.items():
        row, alert = get_price_row(name, ticker)
        if row:
            index_report += row
        if alert:
            alerts.append(alert)
    await bot.send_message(chat_id=CHAT_ID, text=index_report)
    await asyncio.sleep(1)

    fx_report = f"💱 주요 환율 (강제 조회)\n{now}\n\n"
    for name, ticker in FX.items():
        row, alert = get_price_row(name, ticker)
        if row:
            fx_report += row
        if alert:
            alerts.append(alert)
    await bot.send_message(chat_id=CHAT_ID, text=fx_report)

    for alert in alerts:
        await bot.send_message(chat_id=CHAT_ID, text=f"⚠️ 가격 급변 알람\n{alert}")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global sent_links
    sent_links = {}
    save_sent_links()
    await update.message.reply_text("✅ 전송 기록 초기화 완료")


# ─────────────────────────────────────────────
# 광물·원자재 가격 (/mineral 명령어)
# ─────────────────────────────────────────────
def fetch_lithium_carbonate_eastmoney():
    """탄산리튬 주력 계약 가격 - eastmoney (광저우선물거래소 m:225).
    특정 월물을 하드코딩하지 않고, 거래량(f6) 1위 'lc' 계약을 자동 선택해
    계약 만기 롤오버에 자동 대응. 실시간 가격 + 전일 대비 변동률."""
    em_headers = {"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/122.0.0.0"}
    # clist 엔드포인트만 사용 (GCP에서 안정적). 가격 f2, 변동률 f3을 한 번에 취득.
    # 단일 stock/get 엔드포인트는 GCP에서 간헐적 빈 응답이라 미사용.
    list_url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?fs=m:225&fields=f12,f14,f2,f3,f6&pn=1&pz=60"
    )
    for attempt in range(2):  # 간헐 timeout 대비 1회 재시도
        try:
            r = requests.get(list_url, headers=em_headers, timeout=15)
            data = (r.json() or {}).get("data") or {}
            diff = data.get("diff") or {}
            rows = diff.values() if isinstance(diff, dict) else diff
            best = None
            for v in rows:
                code = str(v.get("f12", ""))
                vol = v.get("f6") or 0
                price = v.get("f2")
                if code.startswith("lc") and vol and price not in (None, "-", ""):
                    if best is None or vol > (best.get("f6") or 0):
                        best = v
            if not best:
                return None
            code = best["f12"]                 # 예: lc2607
            price = best["f2"]                 # 현재가
            change_pct = best.get("f3")        # 변동률 (% × 100)
            if change_pct is not None:
                change_pct = change_pct / 100
            return {
                "label": f"탄산리튬 {code} (GFEX 주력)",
                "value": price,
                "unit": "¥/t",
                "change_pct": change_pct,
                "source": "eastmoney.com",
                "is_realtime": True,
            }
        except Exception as e:
            if attempt == 1:
                print(f"  ⚠️ 탄산리튬 조회 오류: {e}")
                return None
            continue
    return None


def fetch_oilprice_crude(slug_candidates, label):
    """oilprice.com에서 WTI/Brent 가격 스크래핑.
    slug_candidates: 시도할 URL 슬러그 리스트 (예: ['wti', 'wti-crude']).
    페이지 HTML 구조가 바뀌면 None 반환 → 호출자가 yfinance fallback."""
    for slug in slug_candidates:
        url = f"https://oilprice.com/futures/{slug}/"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            # 페이지 텍스트에서 첫 합리적 원유 가격($30~$200) 추출
            text = soup.get_text(" ", strip=True)
            for m in re.finditer(r"\$\s*(\d{2,3}\.\d{1,2})", text):
                v = float(m.group(1))
                if 30 < v < 200:
                    return {
                        "label": label,
                        "value": v,
                        "unit": "$/bbl",
                        "change_pct": None,
                        "source": "oilprice.com",
                        "is_realtime": True,
                    }
        except Exception as e:
            print(f"  ⚠️ {label} (oilprice {slug}) 오류: {e}")
            continue
    return None


def fetch_yf_commodity(label, ticker, unit):
    """yfinance 상품 가격 + 전일 대비 변동률."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty or len(hist) < 2:
            return None
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 2:
            return None
        cur = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        change_pct = (cur - prev) / prev * 100 if prev else None
        return {
            "label": label,
            "value": cur,
            "unit": unit,
            "change_pct": change_pct,
            "source": f"yfinance ({ticker})",
            "is_realtime": True,
        }
    except Exception as e:
        print(f"  ⚠️ {label} (yf) 오류: {e}")
        return None


def fetch_tradingeconomics_metal(slug, label):
    """tradingeconomics.com/commodity/<slug>에서 가격 추출.
    핵심 정보는 <meta name="description"> 태그에 들어있음.
    예: "Nickel traded flat at 18,455.50 USD/T on May 19, 2026..."
    주의: 봇 감지가 까다로워 단순 UA로 호출해야 함 (복잡 헤더 시 축약 응답)."""
    url = f"https://tradingeconomics.com/commodity/{slug}"
    # TE는 복잡 헤더에 적대적 — 최소한의 UA만 사용
    te_headers = {"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/122.0.0.0"}
    try:
        resp = requests.get(url, headers=te_headers, timeout=15)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # 1) Meta description 파싱 (가장 안정적)
        desc_meta = soup.find("meta", attrs={"name": "description"})
        desc = desc_meta.get("content", "") if desc_meta else ""

        # 패턴: "at 18,455.50 USD/T" or "to 33500 USD/Tonne"
        m = re.search(
            r"(?:at|to)\s+([\d,]+(?:\.\d+)?)\s+USD/(\w+)",
            desc, re.IGNORECASE,
        )
        if not m and desc:
            # 폴백 패턴: 그냥 "숫자 USD/단위"
            m = re.search(
                r"([\d,]+(?:\.\d+)?)\s+USD/(\w+)",
                desc, re.IGNORECASE,
            )

        # 2) 본문 텍스트 fallback
        price = None
        unit_raw = None
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
                unit_raw = m.group(2).upper()
            except ValueError:
                pass
        else:
            text = soup.get_text(" ", strip=True)
            m2 = re.search(
                r"([\d,]+(?:\.\d+)?)\s*USD\s*/\s*(MT|Tonne|T|kg|lb|Ton)",
                text, re.IGNORECASE,
            )
            if m2:
                try:
                    price = float(m2.group(1).replace(",", ""))
                    unit_raw = m2.group(2).upper()
                except ValueError:
                    pass

        if price is None or price <= 0:
            return None

        # 단위 정규화
        if unit_raw in ("T", "MT", "TONNE", "TON"):
            unit = "$/t"
        elif unit_raw == "LB":
            unit = "$/lb"
        elif unit_raw == "KG":
            unit = "$/kg"
        else:
            unit = "$/t"

        # 일일 변동률 추출
        #  - "traded flat" → 0%
        #  - "down 1.40% from the previous day" → -1.40
        #  - "rose/up 0.23% from the previous day" → +0.23
        change_pct = None
        if desc:
            dl = desc.lower()
            if "traded flat" in dl:
                change_pct = 0.0
            else:
                cm = re.search(
                    r"(up|down|rose|fell|gained|dropped|increased|decreased)\s+([\d.]+)\s*%\s+from the previous day",
                    dl,
                )
                if cm:
                    try:
                        v = float(cm.group(2))
                        if cm.group(1) in ("down", "fell", "dropped", "decreased"):
                            v = -v
                        change_pct = v
                    except ValueError:
                        pass

        return {
            "label": label,
            "value": price,
            "unit": unit,
            "change_pct": change_pct,
            "source": "tradingeconomics.com",
            "is_realtime": True,
        }
    except Exception as e:
        print(f"  ⚠️ TradingEconomics {slug} 오류: {e}")
        return None


def fetch_nonferrous_lme():
    """nonferrous.or.kr LME 일별 종가 테이블 파싱.
    페이지 구조: 6개 컬럼(Cu, Al, Zn, Pb, Ni, Sn) × 여러 일자 행.
    가장 최근 두 영업일에서 종가 + 전일 대비 변동률 계산. 코발트는 페이지에 없음."""
    url = "https://www.nonferrous.or.kr/stats/?act=sub3"
    out = {}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return out
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text("\n", strip=True)
        # 일자 패턴: "2026. 05. 18" 형식, 이어서 6개 숫자(Cu Al Zn Pb Ni Sn)
        # 각 숫자는 "13,428.0" 같은 콤마+소수점
        pattern = re.compile(
            r"(\d{4}\.\s*\d{2}\.\s*\d{2})\s*\n"  # 일자
            r"([\d,]+\.\d+)\s*\n"  # Cu
            r"([\d,]+\.\d+)\s*\n"  # Al
            r"([\d,]+\.\d+)\s*\n"  # Zn
            r"([\d,]+\.\d+)\s*\n"  # Pb
            r"([\d,]+\.\d+)\s*\n"  # Ni
            r"([\d,]+\.\d+)"        # Sn
        )
        rows = pattern.findall(text)
        if len(rows) < 2:
            return out
        # 가장 최근 2 영업일
        latest_date, cu_l, al_l, _zn_l, _pb_l, ni_l, _sn_l = rows[0]
        _, cu_p, al_p, _zn_p, _pb_p, ni_p, _sn_p = rows[1]

        def to_f(s):
            return float(s.replace(",", ""))

        def make(name, cur_str, prev_str):
            try:
                cur = to_f(cur_str)
                prev = to_f(prev_str)
                chg = (cur - prev) / prev * 100 if prev else None
                return {
                    "label": f"{name} (LME)",
                    "value": cur,
                    "unit": "$/t",
                    "change_pct": chg,
                    "source": f"nonferrous.or.kr ({latest_date.replace(' ', '')})",
                    "is_realtime": False,
                }
            except Exception:
                return None

        out["구리"] = make("구리", cu_l, cu_p)
        out["알루미늄"] = make("알루미늄", al_l, al_p)
        out["니켈"] = make("니켈", ni_l, ni_p)
        return out
    except Exception as e:
        print(f"  ⚠️ nonferrous 조회 오류: {e}")
        return out


def _is_gfex_trading_hours():
    """중국 광저우선물거래소(GFEX) 탄산리튬 거래시간 여부.
    오전 9:00-11:30, 오후 13:30-15:00, 야간 21:00-23:00 (중국시간 CST=UTC+8).
    평일만 거래."""
    cn_now = datetime.now(timezone(timedelta(hours=8)))
    if cn_now.weekday() >= 5:  # 토/일
        return False
    minutes = cn_now.hour * 60 + cn_now.minute
    sessions = [
        (9 * 60,        11 * 60 + 30),
        (13 * 60 + 30,  15 * 60),
        (21 * 60,       23 * 60),
    ]
    return any(start <= minutes < end for start, end in sessions)


def _format_mineral_row(item):
    """광물 한 항목을 메시지 한 줄로 포맷팅.
    item에 status_emoji / status_text 키가 있으면 None 표시 시 사용."""
    if not item or item.get("value") is None:
        label = item["label"] if item else "—"
        emoji = (item.get("status_emoji") if item else None) or "❓"
        text = (item.get("status_text") if item else None) or "데이터 없음"
        source = item.get("source", "") if item else ""
        line = f"{emoji} {label}: {text}\n"
        if source:
            line += f"   ↳ {source}\n"
        return line
    label = item["label"]
    value = item["value"]
    unit = item["unit"]
    change_pct = item.get("change_pct")
    source = item.get("source", "")
    is_realtime = item.get("is_realtime", True)

    # 원유류는 변동 유무와 무관하게 원유 이모지로 시작
    is_crude = ("Crude" in label) or ("원유" in label) or ("WTI" in label) or ("Brent" in label)

    if change_pct is not None:
        sign = "+" if change_pct >= 0 else ""
        if change_pct > 0:
            arrow = "🔺"
            change_str = f"  ({sign}{change_pct:.2f}%)"
        elif change_pct < 0:
            arrow = "▼"
            change_str = f"  ({sign}{change_pct:.2f}%)"
        else:
            # 변동 0 = 보합 (저유동성 종목은 며칠씩 보합 유지될 수 있음)
            arrow = "▬"
            change_str = "  (보합)"
    else:
        arrow = "🛢️" if is_crude else "▪"
        change_str = ""

    if is_crude and change_pct is not None:
        # 원유는 화살표 + 드럼 이모지 함께
        arrow = f"🛢️ {arrow}"

    time_tag = "" if is_realtime else "  [전일 기준]"

    if value >= 1000:
        value_str = f"{value:,.0f}"
    elif value >= 10:
        value_str = f"{value:,.2f}"
    else:
        value_str = f"{value:.3f}"

    return f"{arrow} {label}: {value_str} {unit}{change_str}{time_tag}\n   ↳ {source}\n"


async def mineral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """광물·원자재 7종 실시간 가격 조회"""
    await update.message.reply_text("⏳ 광물·원자재 시세 조회 중... (5~10초)")

    items = []

    # 1) 탄산리튬 (eastmoney 고정)
    li = fetch_lithium_carbonate_eastmoney()
    if li is None:
        if _is_gfex_trading_hours():
            # 거래시간인데 None → 실제 조회 실패
            li = {
                "label": "탄산리튬 lc2605 (GFEX)",
                "value": None,
                "unit": "",
                "source": "eastmoney.com 일시 조회 실패",
            }
        else:
            # 거래시간 외 → 거래소 휴장
            li = {
                "label": "탄산리튬 lc2605 (GFEX)",
                "value": None,
                "unit": "",
                "source": "GFEX 거래시간: KST 10:30-12:30 / 14:30-16:00 / 22:00-24:00 (평일)",
                "status_emoji": "🪨",
                "status_text": "거래 시간 제외",
            }
    items.append(li)

    # 2) 구리 — yfinance COMEX (실시간)
    items.append(fetch_yf_commodity("구리 (COMEX HG=F)", "HG=F", "$/lb"))

    # 3) 니켈 — TradingEconomics
    items.append(fetch_tradingeconomics_metal("nickel", "니켈 (LME)") or {
        "label": "니켈 (LME)", "value": None, "unit": "",
        "source": "tradingeconomics.com 일시 조회 실패",
    })

    # 4) 코발트 — TradingEconomics (LME 코발트는 초저유동성이라 며칠씩 보합 흔함)
    co = fetch_tradingeconomics_metal("cobalt", "코발트 (LME)")
    if co is None:
        co = {"label": "코발트 (LME)", "value": None, "unit": "",
              "source": "거래량 적음 — 실시간 무료 소스 없음"}
    elif co.get("change_pct") == 0.0:
        # 보합일 때 저유동성임을 출처에 명시
        co["source"] = "tradingeconomics.com (저유동성, 보합 잦음)"
    items.append(co)

    # 5) 알루미늄 — TradingEconomics LME (yfinance ALI=F는 비유동이라 변동률 부정확)
    items.append(
        fetch_tradingeconomics_metal("aluminum", "알루미늄 (LME)")
        or fetch_yf_commodity("알루미늄 (COMEX ALI=F)", "ALI=F", "$/t")
    )

    # 6) WTI Crude — oilprice.com 고정, 실패 시 yfinance
    items.append(
        fetch_oilprice_crude(["wti", "wti-crude"], "WTI Crude")
        or fetch_yf_commodity("WTI Crude (CL=F)", "CL=F", "$/bbl")
    )

    # 7) Brent Crude — oilprice.com 고정, 실패 시 yfinance
    items.append(
        fetch_oilprice_crude(["brent", "brent-crude", "brent-oil"], "Brent Crude")
        or fetch_yf_commodity("Brent Crude (BZ=F)", "BZ=F", "$/bbl")
    )

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    msg = f"⛏️ 광물·원자재 가격\n조회시각: {now_kst} KST\n\n"
    for item in items:
        msg += _format_mineral_row(item)

    await update.message.reply_text(msg)


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """미 국채 7개 만기별 수익률 + 직전 거래일 대비 bp 변동"""
    await update.message.reply_text("⏳ 미 국채 수익률 조회 중...")
    result = fetch_treasury_yields()
    if not result:
        await update.message.reply_text(
            "⚠️ 미 국채 수익률 데이터를 가져올 수 없음\n"
            "(Treasury 사이트 일시 장애 또는 네트워크 문제)"
        )
        return

    date_str, rows = result
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    msg = (
        f"🇺🇸 미 국채 수익률 (U.S. Treasury Yields)\n"
        f"기준일: {date_str} (직전 거래일 대비)\n"
        f"조회시각: {now_kst} KST\n\n"
    )
    for label, y, bp in rows:
        if y is None:
            msg += f"— {label}:    데이터 없음\n"
            continue
        sign = "+" if bp >= 0 else ""
        if bp > 0:
            arrow = "🔺"
        elif bp < 0:
            arrow = "▼"
        else:
            arrow = "▬"
        # 라벨 폭 맞춤
        pad = "  " if len(label) < 4 else ""
        msg += f"{arrow} {label}{pad}: {y:>5.2f}%  ({sign}{bp}bp)\n"
    msg += "\n출처: U.S. Department of the Treasury (일별 종가)"

    await update.message.reply_text(msg)


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """진단용: 각 피드 상태를 상세히 체크"""
    await update.message.reply_text("🔍 피드 진단 중... (1~2분 소요)")
    report = "🔍 피드 진단 결과\n\n"

    for feed_url in RSS_FEEDS:
        feed_name = urlparse(feed_url).netloc.replace("www.", "")[:20]
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                report += f"❌ {feed_name}: HTTP {resp.status_code}\n"
                continue
            feed = feedparser.parse(resp.text)
            total = len(feed.entries)
            recent = sum(1 for e in feed.entries if is_recent_article(e))
            relevant = 0
            for e in feed.entries:
                if is_recent_article(e):
                    t = e.get("title", "")
                    s = e.get("summary", "")
                    if is_relevant(t, s):
                        relevant += 1
            status = "✅" if relevant > 0 else ("⚠️" if recent > 0 else "💤")
            report += f"{status} {feed_name}: {total}개 | 최근48h={recent} | 키워드매칭={relevant}\n"
        except Exception as e:
            report += f"❌ {feed_name}: {str(e)[:40]}\n"

    report += f"\n📦 전송 기록: {len(sent_links)}개 저장 중"
    await update.message.reply_text(report)


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
async def main():
    global bot
    bot = Bot(token=TELEGRAM_TOKEN)
    print("봇 시작!", flush=True)

    now_et = get_et_now()
    now_kst = datetime.now(KST)
    print(f"현재 시각: {now_kst.strftime('%Y-%m-%d %H:%M')} KST / {now_et.strftime('%H:%M')} ET", flush=True)
    print(f"미국 정규장: {'개장' if is_us_market_open() else '휴장'}", flush=True)
    print(f"Forex 시장: {'운영' if is_forex_market_open() else '휴장'}", flush=True)
    print(f"전송 기록: {len(sent_links)}개 로드됨", flush=True)

    # 시작 시 즉시 실행
    await check_feeds()
    await check_prices()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("update", update_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("force", force_prices_command))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("mineral", mineral_command))
    app.add_handler(CommandHandler("Mineral", mineral_command))
    app.add_handler(CommandHandler("diag", diag_command))

    news_counter = 0

    async with app:
        await app.start()
        await app.updater.start_polling()

        while True:
            await asyncio.sleep(600)
            news_counter += 1
            await check_feeds()

            if news_counter >= 18:
                await check_prices()
                news_counter = 0

        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
