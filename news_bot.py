"""Kioxia / Sandisk 뉴스 감시 봇 (v3)
- 공식 소스(누락 방지 최우선): Kioxia 뉴스룸, Sandisk IR 뉴스, Sandisk 뉴스룸(sitemap), SEC EDGAR
- 보조 소스: Google News RSS (+ 직접 추가하는 RSS)
- 새 항목 -> Claude 두 줄 요약(제품 뉴스는 🆕 표시) -> 텔레그램
- 공식 소스가 오래 수집 실패하면 텔레그램으로 경고
"""
import calendar, json, os, re, sys, time, html
from pathlib import Path
from urllib.parse import quote, urljoin
import feedparser, requests, anthropic

STATE_FILE = Path("seen.json")          # 파일명 유지 (워크플로 수정 불필요)
MODEL = "claude-haiku-5-5"
UA = "Mozilla/5.0 (news-bot)"
MAX_AGE_HOURS = 48                      # Google News 기사에만 적용
OFFICIAL_BASELINE_DAYS = 3              # 새 공식 소스 첫 수집 때: 최근 3일 이내 기사는 알림, 그 이전은 조용히 기록
OFFICIAL_IGNORE_DAYS = 30               # 공식 소스에서 30일보다 오래된 항목은 무시
FAIL_ALERT_RUNS = 6                     # 공식 소스가 연속 6회(약 1시간) 실패하면 경고
SEEN_MAX = 6000

# ───────── Google News RSS (회사, 검색어, 언어설정) ─────────
GNEWS = [
    ("Kioxia", "Kioxia", "en-US&gl=US&ceid=US:en"),
    ("Kioxia", "キオクシア", "ja&gl=JP&ceid=JP:ja"),
    ("Kioxia", "키오시아", "ko&gl=KR&ceid=KR:ko"),
    ("Kioxia", "Kioxia site:businesswire.com", "en-US&gl=US&ceid=US:en"),
    ("Kioxia", "Kioxia SSD", "en-US&gl=US&ceid=US:en"),
    ("Sandisk", "Sandisk", "en-US&gl=US&ceid=US:en"),
    ("Sandisk", "샌디스크", "ko&gl=KR&ceid=KR:ko"),
    ("Sandisk", "Sandisk site:businesswire.com", "en-US&gl=US&ceid=US:en"),
]

# 직접 RSS/Atom (회사, 이름, URL) - 알게 되는 대로 한 줄씩 추가
EXTRA_FEEDS = []

# ───────── 공식 소스 설정 ─────────
SEC_UA = os.environ.get("SEC_USER_AGENT", "news-bot your-email@example.com")
SEC_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           "&CIK=0002023554&type=&dateb=&owner=include&count=40&output=atom")
SEC_EXCLUDE = {"3", "4", "5", "144"}    # 임원 지분변동 등 잡음 제외

KIOXIA_NEWS = "https://www.kioxia.com/en-jp/news.html"
KIOXIA_LINK = re.compile(
    r"^https://www\.kioxia(?:-holdings)?\.com/en-jp/(?:(?:about|business)/)?news/\d{4}/(\d{8})-\d+\.html$")
KIOXIA_PDF = re.compile(r"^https://www\.kioxia\.com/content/dam/kioxia-hd/.+\.pdf$")

SANDISK_IR_LIST = ["https://investor.sandisk.com/news-events/news-releases",
                   "https://sandisk.gcs-web.com/news-events/news-releases"]   # 앞에서부터 시도
SANDISK_IR_SLUG = re.compile(r"/news-releases/news-release-details/([a-z0-9-]+)")
SANDISK_SITEMAPS = ["https://www.sandisk.com/sitemap.xml"]                    # 최선 노력(best-effort)
SANDISK_PR = re.compile(
    r"^https://www\.sandisk\.com/(?:[a-z]{2}-[a-z]{2}/)?company/newsroom/press-releases/"
    r"(\d{4})/(\d{4}-\d{1,2}-\d{1,2})-([a-z0-9-]+)$")

CRITICAL = {"kioxia", "sandisk_ir", "sandisk_news", "sec"}
OFFICIAL_SOURCES = CRITICAL
SOURCE_LABEL = {"kioxia": "Kioxia 뉴스룸", "sandisk_ir": "Sandisk IR 뉴스",
                "sandisk_news": "Sandisk 뉴스룸", "sec": "SEC 공시", "gnews": "Google News"}

PRODUCT_RE = re.compile(
    r"\b(ssd|nand|bics|flash|hbf|pcie|nvme|ufs|dram|memory|series|sampling|"
    r"introduc\w*|unveil\w*|launch\w*|new\s+\w*\s*(card|drive))\b|発表|サンプル|출시|신제품", re.I)

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
claude = anthropic.Anthropic()


# ───────── 공통 도구 ─────────
def clean(s):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html.unescape(s))).strip()


def esc(x):
    return html.escape(x, quote=True)


def http_get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30, **kw)
    r.raise_for_status()
    return r


def entry_ts(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else time.time()


def norm_title(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def item(skey, company, title, link, source, ts, summary="", skip_llm=False, need_title=False):
    return dict(skey=skey, company=company, title=title, link=link, source=source, ts=ts,
                summary=summary, official=skey in OFFICIAL_SOURCES, skip_llm=skip_llm,
                need_title=need_title)


# ───────── 수집기 (각각 항목 리스트와 '정상 판단용 개수'를 돌려줌) ─────────
def src_gnews():
    out = []
    for company, q, loc in GNEWS:
        url = f"https://news.google.com/rss/search?q={quote(q)}+when:2d&hl={loc}"
        for e in feedparser.parse(url).entries:
            out.append(item("gnews", company, e.title, e.link, getattr(e, "source", {}).get("title", ""),
                            entry_ts(e), clean(e.get("summary", ""))))
    out += src_extra()
    return out, len(out)


def src_extra():
    out = []
    for company, name, url in EXTRA_FEEDS:
        for e in feedparser.parse(url).entries:
            out.append(item("gnews", company, e.title, e.link, name, entry_ts(e), clean(e.get("summary", ""))))
    return out


def src_sec():
    out = []
    feed = feedparser.parse(SEC_URL, agent=SEC_UA)
    if feed.get("status", 200) >= 400:
        raise RuntimeError(f"SEC HTTP {feed.get('status')}")
    for e in feed.entries:
        form = e.title.split()[0] if e.title else ""
        if form in SEC_EXCLUDE:
            continue
        out.append(item("sec", "Sandisk", f"SEC 공시 {clean(e.title)}", e.link, "SEC EDGAR",
                        entry_ts(e), clean(e.get("summary", "")), skip_llm=True))
    return out, len(feed.entries)


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
        out.append(item("kioxia", "Kioxia", title, link, "Kioxia 공식", ts, skip_llm=is_pdf))
        if len(out) >= limit:
            break
    return out


def src_kioxia():
    out = parse_kioxia(http_get(KIOXIA_NEWS).text)
    return out, len(out)


def parse_sandisk_ir(page_html, limit=30):
    """Sandisk IR 목록: 제목 링크 + 'BUSINESS WIRE)--Jul. 2, 2026--' 에서 날짜 추출"""
    out, done = [], set()
    for m in re.finditer(r'<a\s[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.S | re.I):
        sm = SANDISK_IR_SLUG.search(html.unescape(m.group(1)))
        if not sm:
            continue
        slug = sm.group(1)
        title = clean(m.group(2))
        if slug in done or not title or title.lower() in ("read more", "pdf version"):
            continue
        done.add(slug)
        after = clean(page_html[m.end(): m.end() + 1500])
        ts = time.time()
        dm = re.search(r"BUSINESS WIRE\)\s*--\s*([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})", after)
        if dm:
            try:
                ts = time.mktime(time.strptime(f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%b %d %Y"))
            except ValueError:
                pass
        link = f"https://investor.sandisk.com/news-releases/news-release-details/{slug}"
        out.append(item("sandisk_ir", "Sandisk", title, link, "Sandisk IR", ts, summary=after[:700]))
        if len(out) >= limit:
            break
    return out


def src_sandisk_ir():
    last = None
    for url in SANDISK_IR_LIST:
        try:
            out = parse_sandisk_ir(http_get(url).text)
            if out:
                return out, len(out)
        except Exception as ex:
            last = ex
    if last:
        raise last
    return [], 0


def src_sandisk_news():
    """sandisk.com 뉴스룸은 JS로 그려져 목록 파싱이 안 되므로 sitemap에서 보도자료 URL을 찾음 (최선 노력)"""
    locs, queue, fetched = [], list(SANDISK_SITEMAPS), 0
    while queue and fetched < 8:
        url = queue.pop(0)
        fetched += 1
        txt = http_get(url).text
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", txt):
            if loc.endswith(".xml") or loc.endswith(".xml.gz"):
                if "sitemap" in loc and loc not in queue:
                    queue.append(loc)
            else:
                locs.append(loc)
    found = {}
    for loc in locs:
        m = SANDISK_PR.match(loc)
        if not m:
            continue
        year, date, slug = m.groups()
        canon = f"https://www.sandisk.com/company/newsroom/press-releases/{year}/{date}-{slug}"
        try:
            ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
        except ValueError:
            continue
        found[canon] = item("sandisk_news", "Sandisk", slug.replace("-", " ").capitalize(), canon,
                            "Sandisk 뉴스룸", ts, need_title=True)
    cutoff = time.time() - OFFICIAL_IGNORE_DAYS * 86400
    return [i for i in found.values() if i["ts"] > cutoff], len(found)


SOURCES = [("gnews", src_gnews), ("sec", src_sec), ("kioxia", src_kioxia),
           ("sandisk_ir", src_sandisk_ir), ("sandisk_news", src_sandisk_news)]


def collect():
    """returns items, status{skey: (ok, n)}"""
    items, status = {}, {}
    for skey, fn in SOURCES:
        try:
            got, n = fn()
            print(f"[{skey}] {len(got)}건 (정상판단 {n})")
            status[skey] = (True, n)
            for it in got:
                items.setdefault(it["link"], it)
        except Exception as ex:
            print(f"[{skey}] 수집 실패: {ex}", file=sys.stderr)
            status[skey] = (False, 0)
    return list(items.values()), status


# ───────── 요약 / 전송 ─────────
def fetch_page(url, n=1500):
    """(제목, 본문 일부) - 실패해도 ('', '')"""
    try:
        r = http_get(url)
        if "html" not in r.headers.get("content-type", ""):
            return "", ""
        t = r.text
        m = re.search(r'(?is)<meta[^>]+property="og:title"[^>]+content="([^"]+)"', t) or \
            re.search(r"(?is)<title>(.*?)</title>", t)
        title = clean(m.group(1)) if m else ""
        body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", t)
        return title, clean(body)[:n]
    except Exception:
        return "", ""


def summarize(it):
    """returns (is_product, text or None[SKIP])"""
    body = it["summary"]
    if not body and it["official"]:
        body = fetch_page(it["link"])[1]
    skip_rule = "" if it["official"] else (
        "단, 기사의 핵심이 '주가가 올랐다/내렸다', 시황, 주가 등락 원인 추측 같은 단순 주가 변동 보도라면 "
        "TAG 줄 없이 정확히 SKIP 한 단어만 출력하세요. 실적, 계약, 투자, 증설, 신제품, 기술, 인사, 소송, "
        "공급/수요 같은 사업 내용이 핵심이면 주가 언급이 있어도 SKIP하지 말고 요약하세요.\n")
    msg = claude.messages.create(
        model=MODEL, max_tokens=350,
        messages=[{"role": "user", "content":
            "반도체 애널리스트에게 보고할 뉴스입니다. 아래 형식으로만 답하세요.\n"
            "첫 줄: 'TAG: 제품' (신제품·샘플링·양산·기술/로드맵 발표 등 제품·기술 관련) 또는 'TAG: 기타'\n"
            "다음 두 줄(한국어): 1줄 무슨 일인지(사실), 2줄 투자/사업 관점의 시사점.\n"
            "아래 내용에 없는 사실은 추측하지 마세요. 정보가 제목뿐이면 제목 기준으로만 쓰세요.\n"
            + skip_rule + "\n"
            f"회사: {it['company']}\n제목: {it['title']}\n출처: {it['source']}\n내용: {body[:1500]}"}],
    )
    out = msg.content[0].text.strip()
    if out.upper().startswith("SKIP"):
        return False, None
    lines = [l for l in out.splitlines() if l.strip()]
    is_product = bool(lines) and lines[0].upper().replace(" ", "").startswith("TAG:제품")
    if lines and lines[0].upper().startswith("TAG"):
        lines = lines[1:]
    return is_product, "\n".join(lines).strip()


def send(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": True}, timeout=20)
    r.raise_for_status()


# ───────── 상태 ─────────
def load_state():
    if not STATE_FILE.exists():
        return dict(seen=[], init=[], health={}, titles=[], fresh=True)
    raw = json.loads(STATE_FILE.read_text())
    if isinstance(raw, list):   # 구버전 형식: 기존에 돌던 소스들은 이미 초기화된 것으로 간주
        return dict(seen=raw, init=["gnews", "sec", "kioxia"], health={}, titles=[], fresh=False)
    raw.setdefault("titles", []); raw.setdefault("health", {}); raw.setdefault("init", [])
    raw["fresh"] = False
    return raw


def save_state(st):
    st = {k: v for k, v in st.items() if k != "fresh"}
    st["seen"] = st["seen"][-SEEN_MAX:]
    st["titles"] = st["titles"][-1000:]
    STATE_FILE.write_text(json.dumps(st))


def update_health(st, status):
    for skey, (ok, n) in status.items():
        h = st["health"].setdefault(skey, {"fails": 0, "alerted": False})
        good = ok and (n > 0)
        if good:
            if h["alerted"]:
                send(f"✅ {SOURCE_LABEL.get(skey, skey)} 수집이 복구되었습니다.")
            h["fails"], h["alerted"] = 0, False
        else:
            h["fails"] += 1
            if skey in CRITICAL | {"gnews"} and h["fails"] >= FAIL_ALERT_RUNS and not h["alerted"]:
                send(f"⚠️ {SOURCE_LABEL.get(skey, skey)} 수집이 연속 {h['fails']}회 실패했습니다.\n"
                     f"이 소스의 새 소식이 누락될 수 있어요. GitHub Actions 로그를 확인해 주세요.")
                h["alerted"] = True


# ───────── 메인 ─────────
def main():
    st = load_state()
    seen = list(st["seen"]); seen_set = set(seen)
    init = set(st["init"]); titles = set(st["titles"])
    now = time.time()

    def mark(link):
        if link not in seen_set:
            seen.append(link); seen_set.add(link)

    items, status = collect()
    update_health(st, status)

    new = []
    for it in items:
        if it["link"] in seen_set:
            continue
        if it["official"]:
            age_days = (now - it["ts"]) / 86400
            if age_days > OFFICIAL_IGNORE_DAYS:
                continue
            if it["skey"] not in init and age_days > OFFICIAL_BASELINE_DAYS:
                mark(it["link"]); continue            # 새 공식 소스의 오래된 기사: 조용히 기록
            if norm_title(it["title"]) in titles:
                mark(it["link"]); continue            # 다른 공식 소스에서 이미 보낸 같은 제목
        else:
            if it["skey"] not in init:
                mark(it["link"]); continue            # 새 뉴스 소스 첫 수집: 조용히 기록
            if it["ts"] < now - MAX_AGE_HOURS * 3600:
                continue
        new.append(it)

    # 같은 실행에서 여러 공식 소스가 같은 기사를 낸 경우 하나만
    uniq, tseen = [], set()
    for it in sorted(new, key=lambda i: (not i["official"], i["ts"])):
        nt = norm_title(it["title"])
        if it["official"] and nt in tseen:
            mark(it["link"]); continue
        tseen.add(nt); uniq.append(it)
    new = sorted(uniq, key=lambda i: i["ts"])

    print(f"신규 {len(new)}건 (기록 {len(seen)}건)")
    for i in new:
        print("  신규:", i["skey"], "|", i["company"], "|", i["title"][:80])

    for it in new:
        try:
            if it["need_title"]:
                t, _ = fetch_page(it["link"])
                if t:
                    it["title"] = re.sub(r"\s*\|\s*Sandisk.*$", "", t)
            is_product, summ = False, ""
            if not it["skip_llm"]:
                try:
                    is_product, summ = summarize(it)
                except Exception as ex:
                    if not it["official"]:
                        raise                         # 뉴스 기사는 다음 턴에 재시도
                    print("요약 실패(공식 기사는 요약 없이 전송):", ex, file=sys.stderr)
                    summ, is_product = "(요약 생성 실패 - 원문을 확인하세요)", bool(PRODUCT_RE.search(it["title"]))
                if summ is None:
                    print("SKIP(주가기사):", it["title"])
                    mark(it["link"]); continue
            prefix = ("🆕" if is_product else "") + ("🏛" if it["official"] else "📰")
            label = f"{it['company']} · 제품" if is_product else it["company"]
            body = f"{esc(summ)}\n\n" if summ else ""
            send(f"{prefix} <b>[{esc(label)}]</b> {esc(it['title'])}\n\n{body}"
                 f"{esc(it['source'])} · <a href=\"{esc(it['link'])}\">원문 보기</a>")
            mark(it["link"])
            if it["official"]:
                titles.add(norm_title(it["title"]))
        except Exception as ex:
            print("전송 실패(다음 실행에서 재시도):", ex, file=sys.stderr)

    for skey, (ok, n) in status.items():
        if ok:
            init.add(skey)
    st.update(seen=seen, init=sorted(init), titles=sorted(titles))
    save_state(st)


if __name__ == "__main__":
    main()
