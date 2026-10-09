"""Kioxia / Sandisk 뉴스 감시 봇
소스: Google News RSS + 공식 소스(Kioxia 뉴스룸, SEC EDGAR Sandisk 공시)
새 항목 -> Claude 두 줄 요약 -> 텔레그램 전송
"""
import calendar, json, os, re, sys, time, html
from pathlib import Path
from urllib.parse import quote, urljoin
import feedparser, requests, anthropic

SEEN_FILE = Path("seen.json")
MODEL = "claude-haiku-5-5"
MAX_AGE_HOURS = 24          # Google News 기사에만 적용 (공식 소스는 seen.json으로 중복 방지)
UA = "Mozilla/5.0 (news-bot)"

# ───────── 1) Google News RSS (회사, 검색어, 언어설정) ─────────
GNEWS = [
    ("Kioxia", "Kioxia", "en-US&gl=US&ceid=US:en"),
    ("Kioxia", "キオクシア", "ja&gl=JP&ceid=JP:ja"),
    ("Kioxia", "키오시아", "ko&gl=KR&ceid=KR:ko"),
    ("Sandisk", "Sandisk", "en-US&gl=US&ceid=US:en"),
    ("Sandisk", "샌디스크", "ko&gl=KR&ceid=KR:ko"),
    # 보도자료 배포처(Business Wire)에서 나온 Sandisk 공식 발표
    ("Sandisk", "Sandisk site:businesswire.com", "en-US&gl=US&ceid=US:en"),
]

# ───────── 2) 직접 RSS/Atom URL (원하는 만큼 추가: (회사, 이름, URL)) ─────────
# 공식 RSS 주소를 알게 되면 여기에 한 줄만 추가하면 됩니다.
EXTRA_FEEDS = [
    # ("Sandisk", "예시 RSS", "https://example.com/rss.xml"),
]

# SEC EDGAR (Sandisk CIK 0002023554). 요청 시 연락처가 담긴 User-Agent가 필수입니다.
SEC_UA = os.environ.get("SEC_USER_AGENT", "news-bot your-email@example.com")
SEC_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           "&CIK=0002023554&type=&dateb=&owner=include&count=40&output=atom")
SEC_EXCLUDE = {"3", "4", "5", "144"}   # 임원 지분변동 등 잡음 제외 (보고 받고 싶으면 비우세요)

# Kioxia 공식 뉴스룸 (RSS 없음 -> 목록 페이지 파싱). Holdings(IR) 공지도 이 목록에 포함됨
KIOXIA_NEWS = "https://www.kioxia.com/en-jp/news.html"
KIOXIA_LINK = re.compile(
    r"^https://www\.kioxia(?:-holdings)?\.com/en-jp/(?:(?:about|business)/)?news/\d{4}/(\d{8})-\d+\.html$")
KIOXIA_PDF = re.compile(r"^https://www\.kioxia\.com/content/dam/kioxia-hd/.+\.pdf$")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
claude = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용


def clean(s):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html.unescape(s))).strip()


def entry_ts(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else time.time()


def item(company, title, link, source, ts, summary="", official=False, skip_llm=False):
    return dict(company=company, title=title, link=link, source=source,
                ts=ts, summary=summary, official=official, skip_llm=skip_llm)


# ───────── 수집기 ─────────
def from_gnews():
    out = []
    for company, q, loc in GNEWS:
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:1d&hl={loc}"
        for e in feedparser.parse(url).entries:
            out.append(item(company, e.title, e.link, getattr(e, "source", {}).get("title", ""),
                            entry_ts(e), clean(e.get("summary", ""))))
    return out


def from_extra_feeds():
    out = []
    for company, name, url in EXTRA_FEEDS:
        for e in feedparser.parse(url).entries:
            out.append(item(company, e.title, e.link, name, entry_ts(e),
                            clean(e.get("summary", "")), official=True))
    return out


def from_sec():
    out = []
    for e in feedparser.parse(SEC_URL, agent=SEC_UA).entries:
        form = e.title.split()[0] if e.title else ""
        if form in SEC_EXCLUDE:
            continue
        out.append(item("Sandisk", f"SEC 공시 {clean(e.title)}", e.link, "SEC EDGAR",
                        entry_ts(e), clean(e.get("summary", "")), official=True, skip_llm=True))
    return out


def parse_kioxia(page_html, limit=30):
    out, seen_links = [], set()
    for href, inner in re.findall(r'<a\s[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.S | re.I):
        link = urljoin(KIOXIA_NEWS, html.unescape(href))
        m = KIOXIA_LINK.match(link)
        is_pdf = bool(KIOXIA_PDF.match(link))
        if not (m or is_pdf) or link in seen_links:
            continue
        title = clean(inner)
        if not title:
            continue
        seen_links.add(link)
        ts = time.mktime(time.strptime(m.group(1), "%Y%m%d")) if m else time.time()
        out.append(item("Kioxia", title, link, "Kioxia 공식", ts, official=True,
                        skip_llm=is_pdf))
        if len(out) >= limit:
            break
    return out


def from_kioxia():
    r = requests.get(KIOXIA_NEWS, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return parse_kioxia(r.text)


def fetch_text(url, n=1500):
    """공식 소스 기사 본문 일부 (실패해도 무시)"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if "html" not in r.headers.get("content-type", ""):
            return ""
        body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", r.text)
        return clean(body)[:n]
    except Exception:
        return ""


def collect():
    items = {}
    for name, fn in [("gnews", from_gnews), ("extra", from_extra_feeds),
                     ("sec", from_sec), ("kioxia", from_kioxia)]:
        try:
            for it in fn():
                items.setdefault(it["link"], it)
        except Exception as ex:  # 한 소스가 죽어도 나머지는 계속
            print(f"[{name}] 수집 실패: {ex}", file=sys.stderr)
    return list(items.values())


# ───────── 요약 / 전송 ─────────
def summarize(it):
    body = it["summary"] or (fetch_text(it["link"]) if it["official"] else "")
    msg = claude.messages.create(
        model=MODEL, max_tokens=300,
        messages=[{"role": "user", "content":
            "반도체 애널리스트에게 보고할 뉴스입니다. 한국어로 정확히 두 줄로 요약하세요.\n"
            "1줄: 무슨 일인지(사실). 2줄: 투자/사업 관점의 시사점.\n"
            "아래 내용에 없는 사실은 추측하지 마세요. 정보가 제목뿐이면 제목 기준으로만 쓰세요. "
            "두 줄 외 다른 말은 쓰지 마세요.\n"
            "단, 기사의 핵심이 '주가가 올랐다/내렸다', 시황, 주가 등락 원인 추측 같은 단순 주가 변동 보도라면 "
            "요약하지 말고 정확히 SKIP 한 단어만 출력하세요. "
            "실적, 계약, 투자, 증설, 신제품, 기술, 인사, 소송, 공급/수요 같은 사업 내용이 핵심이면 "
            "주가 언급이 있어도 SKIP하지 말고 요약하세요.\n\n"
            f"회사: {it['company']}\n제목: {it['title']}\n출처: {it['source']}\n내용: {body[:1500]}"}],
    )
    out = msg.content[0].text.strip()
    return None if out.upper().startswith("SKIP") else out


def esc(x):
    return html.escape(x, quote=True)


def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()


def main():
    first_run = not SEEN_FILE.exists()
    seen = set() if first_run else set(json.loads(SEEN_FILE.read_text()))
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    new = [i for i in collect()
           if i["link"] not in seen and (i["official"] or i["ts"] > cutoff)]
    new.sort(key=lambda i: i["ts"])

    if first_run:  # 첫 실행: 기존 항목은 기록만 하고 전송 안 함
        seen.update(i["link"] for i in new)
        send("✅ 뉴스 봇 시작. 지금부터 새 항목만 보고합니다.\n"
             "소스: Google News · Kioxia 공식 뉴스룸 · SEC(Sandisk)")
    else:
        for it in new:
            try:
                tag = "🏛공식" if it["official"] else "📰"
                summ = "" if it["skip_llm"] else summarize(it)
                if summ is None:  # 단순 주가 등락 기사: 보내지 않고 처리 완료로 기록
                    print("SKIP(주가기사):", it["title"])
                    seen.add(it["link"])
                    continue
                body = f"{esc(summ)}\n\n" if summ else ""
                send(f"{tag} <b>[{it['company']}]</b> {esc(it['title'])}\n\n{body}"
                     f"{esc(it['source'])} · <a href=\"{esc(it['link'])}\">원문 보기</a>")
                seen.add(it["link"])
            except Exception as ex:
                print("실패:", ex, file=sys.stderr)  # seen에 안 넣어 다음 턴에 재시도

    SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:]))


if __name__ == "__main__":
    main()
