#!/usr/bin/env python3
"""愛知アート帳: artscape と 原画展.info から愛知県の展覧会を取得し data/events.json を更新する。

- artscape: 一覧ページ(タイトル・会場・会期)を全ページ取得し、新規分だけ詳細ページの住所から市区町村を判定する。
- 原画展.info: 愛知タグの一覧タイトルから開催期間を、新規分だけ詳細ページから会場を取得する。
- コラボカフェ.com 等は複数会場の自由記述で機械的に正しく判定できないため自動取得しない。
  既存データ(Claudeが手作業で確認したもの)はそのまま残し、会期終了分だけ自動で消す。
- 取得に失敗した情報源があっても既存データは消さない。結果は data/scrape_status.json に残す。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "events.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; AichiArtTracker/1.0; "
    "+https://github.com/lastradezanpano/aichi-art-tracker)"
)
REQUEST_INTERVAL = 1.0
MAX_DETAIL_FETCHES = 40

ARTSCAPE_LIST = "https://artscape.jp/exhibitions/?area=chubu-aichi"
ARTSCAPE_MAX_PAGES = 15
GENGATEN_LIST = "https://gengaten.info/tag/aichi/"
GENGATEN_MAX_PAGES = 5


# ---------------------------------------------------------------- 共通部品

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ja,en;q=0.5",
}


def http_get(url: str, retries: int = 4, timeout: int = 90) -> str:
    # artscapeはGitHub ActionsのクラウドIPからだと応答が極端に遅くなることがあるため、待ち時間を長めにしている
    last_error: Exception | None = None
    for attempt in range(retries):
        started = time.time()
        try:
            request = urllib.request.Request(url, headers=REQUEST_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            time.sleep(REQUEST_INTERVAL)
            return body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        print(f"[warn] {url} 取得{attempt + 1}回目失敗({time.time() - started:.0f}秒): {last_error!r}")
        time.sleep(5 * (attempt + 1))
    assert last_error is not None
    raise last_error


TAG_RE = re.compile(r"<[^>]+>")


def clean_text(fragment: str) -> str:
    text = html_lib.unescape(TAG_RE.sub(" ", fragment))
    return re.sub(r"\s+", " ", text.replace("　", " ")).strip()


def clean_title(title: str) -> str:
    title = clean_text(title)
    title = re.sub(r"([「『（(])\s+", r"\1", title)
    title = re.sub(r"\s+([」』）)])", r"\1", title)
    return title


NOISE_RE = re.compile(r"[\s!！?？「」『』（）()\[\]【】～〜\-－–—ー・:：、。,.&＆×|｜\"“”'’―─]")
# 「（仮）」や「没後20年」「企画展」など、同じ展覧会でも情報源によって付いたり付かなかったりする語
TITLE_AFFIX_RE = re.compile(
    r"[（(]仮[)）]|(?:没後|生誕|開館|創刊|放送|誕生|連載)\s*\d+\s*(?:周)?年(?:記念)?|"
    r"^(?:企画展|特別展|特別企画展|館蔵品展|所蔵品展|コレクション展)"
)


def title_key(title: str) -> str:
    title = unicodedata.normalize("NFKC", title).replace("〇", "○")
    title = TITLE_AFFIX_RE.sub("", title.strip())
    return NOISE_RE.sub("", title).lower()


def iso(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


CITY_RE = re.compile(r"愛知県(?:[^市区町村郡0-9０-９]+郡)?([^市区町村郡0-9０-９]+[市町村])")


def city_from_address(address: str) -> str:
    match = CITY_RE.search(address)
    return match.group(1) if match else ""


def guess_category(title: str, venue: str) -> str:
    if "絵本" in title:
        return "こども・絵本"
    if "写真" in title:
        return "写真"
    if re.search(r"陶|やきもの|焼|窯", title + venue):
        return "工芸・陶芸"
    if re.search(r"歴史|博物館|資料館|民俗", title + venue):
        return "歴史・博物"
    if "現代" in venue:
        return "現代美術"
    if re.search(r"科学館|自然|植物|化石|コンクリーション|寄生虫", title + venue):
        return "科学・自然"
    if re.search(r"刀|甲冑|武具", title + venue):
        return "歴史・博物"
    return "展覧会"


# ---------------------------------------------------------------- artscape

ARTSCAPE_ITEM_SPLIT = '<article class="item-article item-exhibitions">'
ARTSCAPE_TITLE_RE = re.compile(
    r'<h3 class="article-title"><a href="(https://artscape\.jp/exhibitions/(\d+)/)"><span>(.*?)</span></a></h3>',
    re.DOTALL,
)
ARTSCAPE_VENUE_RE = re.compile(r"<p>([^<]*)<br>([^<]*)</p>")
ARTSCAPE_PERIOD_RE = re.compile(
    r"会期：(\d{4})年(\d{1,2})月(\d{1,2})日(?:～(\d{4})年(\d{1,2})月(\d{1,2})日)?"
)
ARTSCAPE_ADDRESS_RE = re.compile(r"<dt>住所</dt>.*?<span>([^<]*)</span>", re.DOTALL)


def parse_artscape_list(html: str) -> list[dict]:
    results = []
    for block in html.split(ARTSCAPE_ITEM_SPLIT)[1:]:
        title_m = ARTSCAPE_TITLE_RE.search(block)
        venue_m = ARTSCAPE_VENUE_RE.search(block)
        period_m = ARTSCAPE_PERIOD_RE.search(block)
        if not (title_m and venue_m and period_m):
            continue
        y1, m1, d1, y2, m2, d2 = period_m.groups()
        start = iso(int(y1), int(m1), int(d1))
        end = iso(int(y2), int(m2), int(d2)) if y2 else ""
        results.append({
            "url": title_m.group(1),
            "artscape_id": title_m.group(2),
            "title": clean_title(title_m.group(3)),
            "venue": clean_text(venue_m.group(1)),
            "pref": clean_text(venue_m.group(2)),
            "date": start,
            "dateEnd": end,
        })
    return results


def fetch_artscape_list() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(1, ARTSCAPE_MAX_PAGES + 1):
        url = ARTSCAPE_LIST if page == 1 else f"{ARTSCAPE_LIST}&paged={page}"
        try:
            page_items = parse_artscape_list(http_get(url))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and page > 1:
                break
            raise
        fresh = [i for i in page_items if i["artscape_id"] not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item["artscape_id"])
        items.extend(fresh)
    return [i for i in items if i["pref"] == "愛知"]


def fetch_artscape_address(detail_url: str) -> str:
    match = ARTSCAPE_ADDRESS_RE.search(http_get(detail_url))
    return clean_text(match.group(1)) if match else ""


# ---------------------------------------------------------------- 原画展.info

GENGATEN_CARD_RE = re.compile(
    r'<a href="(https://gengaten\.info/\d{4}/\d{2}/\d+/)" class="entry-card-wrap[^"]*" title="([^"]*)"'
)
GENGATEN_TITLE_RE = re.compile(r"^【([^】]+)】(.+?)[：:]\s*(.+)$")
GENGATEN_VENUE_RE = re.compile(r"<td>会場</td>\s*<td>(.*?)</td>", re.DOTALL)
FULL_DATE_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
MONTH_DAY_RE = re.compile(r"(\d{1,2})月\s*(\d{1,2})日")
DAY_ONLY_RE = re.compile(r"(\d{1,2})日")


def parse_ja_period(text: str) -> tuple[str, str] | None:
    parts = re.split(r"[～〜~－–—-]", text, maxsplit=1)
    start_m = FULL_DATE_RE.search(parts[0])
    if not start_m:
        return None
    sy, sm, sd = (int(g) for g in start_m.groups())
    start = iso(sy, sm, sd)
    end = ""
    if len(parts) == 2:
        full = FULL_DATE_RE.search(parts[1])
        md = MONTH_DAY_RE.search(parts[1])
        day = DAY_ONLY_RE.search(parts[1])
        if full:
            end = iso(*(int(g) for g in full.groups()))
        elif md:
            em, ed = int(md.group(1)), int(md.group(2))
            end = iso(sy + 1 if em < sm else sy, em, ed)
        elif day:
            end = iso(sy, sm, int(day.group(1)))
    return start, end


def parse_gengaten_list(html: str) -> list[dict]:
    results = []
    for url, raw_title in GENGATEN_CARD_RE.findall(html):
        match = GENGATEN_TITLE_RE.match(html_lib.unescape(raw_title))
        if not match:
            continue
        region, name, period_text = match.groups()
        # 複数地域の巡回(例: 東京・愛知)は会場の取り違えを避けて対象外にする
        if not re.search(r"名古屋|愛知", region) or re.search(r"[・／/,、]", region):
            continue
        period = parse_ja_period(period_text)
        if not period:
            continue
        results.append({
            "url": url,
            "title": clean_title(name),
            "region": region,
            "date": period[0],
            "dateEnd": period[1],
        })
    return results


def fetch_gengaten_list() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(1, GENGATEN_MAX_PAGES + 1):
        url = GENGATEN_LIST if page == 1 else f"{GENGATEN_LIST}page/{page}/"
        try:
            page_items = parse_gengaten_list(http_get(url))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and page > 1:
                break
            raise
        fresh = [i for i in page_items if i["url"] not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item["url"])
        items.extend(fresh)
    return items


def fetch_gengaten_venue(detail_url: str) -> str:
    match = GENGATEN_VENUE_RE.search(http_get(detail_url))
    return clean_text(match.group(1)) if match else ""


# ---------------------------------------------------------------- インターネットミュージアム

MUSEUM_LIST = "https://www.museum.or.jp/event?area=pref-23"
MUSEUM_MAX_PAGES = 6
MUSEUM_ITEM_SPLIT = '<div class="c-eventItem" '
MUSEUM_URL_RE = re.compile(r'href="(/event/(\d+))"')
MUSEUM_TITLE_RE = re.compile(r'c-eventItem_museum[^>]*>\s*<a[^>]*>(.*?)</a>', re.DOTALL)
MUSEUM_PLACE_RE = re.compile(r'c-eventItem_event[^>]*>(.*?)</div>', re.DOTALL)
MUSEUM_DURATION_RE = re.compile(r'c-eventItem_duration[^>]*>(.*?)</div>', re.DOTALL)
POSTAL_ADDRESS_RE = re.compile(r"〒\s*\d{3}-?\d{4}\s*(愛知県[^\s<]+)")
# 1年以上先の展覧会は情報が変わりやすく一覧が長くなるため、近づいてから載せる
MUSEUM_HORIZON_DAYS = 365


def parse_museum_list(html: str) -> list[dict]:
    results = []
    for block in html.split(MUSEUM_ITEM_SPLIT)[1:]:
        url_m = MUSEUM_URL_RE.search(block)
        title_m = MUSEUM_TITLE_RE.search(block)
        place_m = MUSEUM_PLACE_RE.search(block)
        dur_m = MUSEUM_DURATION_RE.search(block)
        if not (url_m and title_m and place_m and dur_m):
            continue
        place = clean_text(place_m.group(1))
        venue, _, pref = place.rpartition("|")
        if pref.strip() != "愛知県":
            continue
        period = parse_ja_period(re.sub(r"[（(][^）)]*[）)]", "", clean_text(dur_m.group(1))))
        if not period:
            continue
        results.append({
            "url": "https://www.museum.or.jp" + url_m.group(1),
            "museum_id": url_m.group(2),
            "title": clean_title(title_m.group(1)),
            "venue": venue.strip(),
            "date": period[0],
            "dateEnd": period[1],
        })
    return results


def fetch_museum_list() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    horizon = (datetime.now(timezone(timedelta(hours=9))).date()
               + timedelta(days=MUSEUM_HORIZON_DAYS)).isoformat()
    for page in range(1, MUSEUM_MAX_PAGES + 1):
        url = MUSEUM_LIST if page == 1 else f"{MUSEUM_LIST}&page={page}"
        page_items = parse_museum_list(http_get(url))
        fresh = [i for i in page_items if i["museum_id"] not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item["museum_id"])
        items.extend(fresh)
    return [i for i in items if i["date"] <= horizon]


def resolve_museum(item: dict) -> dict | None:
    page = re.sub(r"<script.*?</script>", "", http_get(item["url"]), flags=re.DOTALL)
    match = POSTAL_ADDRESS_RE.search(clean_text(page))
    address = match.group(1) if match else ""
    event = {
        "date": item["date"],
        "title": item["title"],
        "venue": item["venue"],
        "city": city_from_address(address),
        "category": guess_category(item["title"], item["venue"]),
        "note": "",
        "src": "museum_or_jp",
        "url": item["url"],
    }
    if item["dateEnd"]:
        event["dateEnd"] = item["dateEnd"]
    return event


# ---------------------------------------------------------------- コラボカフェ.com

COLLABO_LIST = "https://collabo-cafe.com/events/tag/nagoya/"
COLLABO_MAX_PAGES = 3
COLLABO_ARTICLE_RE = re.compile(
    r'<article class="post-list[^"]*"[^>]*>\s*<a href="(https://collabo-cafe\.com/events/[^"]+)"'
    r'[^>]*title="([^"]*)"(.*?)</article>',
    re.DOTALL,
)
COLLABO_CAT_RE = re.compile(r'<span class="cat-name[^"]*">(.*?)</span>', re.DOTALL)
COLLABO_LIST_PERIOD_RE = re.compile(r'<span class="event-date[^"]*">\s*期間\s*:\s*(.*?)</span>', re.DOTALL)
COLLABO_TABLE_RE = re.compile(r'<table class="cc-table[^"]*"[^>]*>(.*?)</table>', re.DOTALL)
COLLABO_ROW_RE = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.DOTALL)
COLLABO_CAPTION_RE = re.compile(r"<caption>(.*?)</caption>", re.DOTALL)
AICHI_WORD_RE = re.compile(r"愛知|名古屋")
BRACKET_RE = re.compile(r"^【([^】]*)】\s*")
VAGUE_VENUE_RE = re.compile(r"全国|各店|各会場|店舗|ほか|他|後日|未定|予定")


def parse_collabo_list(html: str) -> list[dict]:
    results = []
    for url, raw_title, body in COLLABO_ARTICLE_RE.findall(html):
        period_m = COLLABO_LIST_PERIOD_RE.search(body)
        period = parse_ja_period(clean_text(period_m.group(1))) if period_m else None
        cat_m = COLLABO_CAT_RE.search(body)
        results.append({
            "url": url,
            "title": clean_title(raw_title),
            "list_category": clean_text(cat_m.group(1)) if cat_m else "",
            # 一覧の期間は全会場まとめた概略。期限切れ判定だけに使い、掲載値は詳細ページから取る
            "date": period[0] if period else "9999-12-31",
            "dateEnd": (period[1] or "") if period else "",
        })
    return results


def fetch_collabo_list() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(1, COLLABO_MAX_PAGES + 1):
        url = COLLABO_LIST if page == 1 else f"{COLLABO_LIST}page/{page}/"
        try:
            page_items = parse_collabo_list(http_get(url))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and page > 1:
                break
            raise
        fresh = [i for i in page_items if i["url"] not in seen]
        if not fresh:
            break
        for item in fresh:
            seen.add(item["url"])
        items.extend(fresh)
    return items


def split_lines(cell: str) -> list[str]:
    return [t for t in (clean_text(p) for p in re.split(r"<br\s*/?>|</p>|</li>", cell)) if t]


def collabo_category(list_category: str, title: str) -> str:
    text = list_category + title
    if re.search(r"原画|展示会|展$|展 |展覧会", text):
        return "アニメ・原画展"
    if "カフェ" in text:
        return "コラボカフェ"
    return "ポップアップ・グッズ"


class NeedsReview(Exception):
    """自動では会場・会期を1つに確定できない(Claudeまたは人の確認が必要)"""


ONLINE_RE = re.compile(r"web|WEB|Web|オンライン|通販|EC|ネット")
POSTAL_RE = re.compile(r"〒|\d{3}-\d{4}|丁目|番地")
# 「名古屋」「愛知」と書かれていなくても名古屋市内と分かる地名
NAGOYA_PLACE_RE = re.compile(r"栄|名駅|大須|金山|久屋|伏見|則武|MIRAI TOWER|ミライタワー|"
                             r"中区|中村区|東区|千種区|熱田区|名東区")
CITY_IN_TEXT_RE = re.compile(r"(豊橋|岡崎|一宮|瀬戸|春日井|豊川|刈谷|豊田|安城|西尾|小牧|稲沢|東海|"
                             r"大府|知多|長久手|日進|常滑|半田|犬山|江南|蒲郡)")


def guess_city(text: str) -> str:
    if "名古屋" in text or NAGOYA_PLACE_RE.search(text):
        return "名古屋市"
    match = CITY_IN_TEXT_RE.search(text)
    return match.group(1) + "市" if match else ""


def tidy_collabo_title(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"\s*(の)?(開催概要|概要|詳細|開催情報)$", "", text)
    text = re.sub(r"\s*\d{1,2}月\d{1,2}日(より|から).*$", "", text)  # 一覧タイトル末尾の「12月21日より開催!」
    match = re.match(r"^「([^」]+)」\s*(.*)$", text)               # 「作品 × 店」コラボ → 作品 × 店 コラボ
    if match:
        text = f"{match.group(1)} {match.group(2)}".strip()
    for left, right in ("「」", "『』"):                             # 片方だけ残ったカッコを外す
        if text.count(left) != text.count(right):
            text = text.replace(left, " ").replace(right, " ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_collabo_table(table: str, item: dict) -> dict | None:
    rows = {clean_text(k): v for k, v in COLLABO_ROW_RE.findall(table)}
    period_cell = next((v for k, v in rows.items() if "期間" in k or "日程" in k), "")
    venue_cell = next((v for k, v in rows.items() if "会場" in k or "店舗" in k or "場所" in k), "")
    if not period_cell or not venue_cell:
        return None

    venue_lines = split_lines(venue_cell)
    aichi_lines = [l for l in venue_lines if AICHI_WORD_RE.search(l)]
    has_brackets = any(BRACKET_RE.match(l) for l in venue_lines)
    if len(aichi_lines) == 1:
        venue_line = aichi_lines[0]
    elif not aichi_lines and len(venue_lines) == 1 and not has_brackets:
        # 会場が1か所だけで愛知と書いていない表。タイトルか会場名から名古屋と分かるときだけ採用
        venue_line = venue_lines[0]
        if ONLINE_RE.search(venue_line):
            return None
        if not (AICHI_WORD_RE.search(item["title"]) or NAGOYA_PLACE_RE.search(venue_line)):
            raise NeedsReview(f"会場が愛知か判断できない: {venue_line}")
    elif not aichi_lines:
        return None  # 愛知の会場がない(名古屋タグでも他県のみの記事がある)
    else:
        raise NeedsReview(f"愛知の会場が複数({len(aichi_lines)}か所)")
    bracket_m = BRACKET_RE.match(venue_line)
    region = bracket_m.group(1) if bracket_m else ""
    venue = BRACKET_RE.sub("", venue_line).strip()
    if not venue or VAGUE_VENUE_RE.search(venue) or ONLINE_RE.search(venue):
        raise NeedsReview(f"会場が確定していない: {venue_line}")
    # 「池袋店・名古屋店・梅田店」のように1行に複数店舗が並ぶ書き方は、期間も全店まとめての可能性が高い
    if re.search(r"[/／、]", venue) or venue.count("・") >= 2:
        raise NeedsReview(f"1行に複数の会場: {venue[:40]}")
    if POSTAL_RE.search(venue):
        raise NeedsReview(f"会場欄が住所表記: {venue[:40]}")

    period_lines = split_lines(period_cell)
    ranged = [l for l in period_lines if FULL_DATE_RE.search(l) or MONTH_DAY_RE.search(l)]
    if len(ranged) > 1:
        # 会場ごとに期間が違う書き方。愛知の会場名・地域名を含む行だけを使う
        keys = [k for k in (region, venue[:6], "名古屋", "愛知") if k]
        ranged = [l for l in ranged if any(k in l for k in keys)]
    if len(ranged) != 1:
        raise NeedsReview("愛知会場の期間を1つに確定できない")
    period_text = BRACKET_RE.sub("", ranged[0])
    if not FULL_DATE_RE.search(period_text):
        year_m = FULL_DATE_RE.search(clean_text(period_cell))
        if not year_m:
            raise NeedsReview("期間に年の記載がない")
        period_text = f"{year_m.group(1)}年{period_text}"
    period = parse_ja_period(period_text)
    if not period:
        raise NeedsReview(f"期間を読み取れない: {period_text[:40]}")

    caption_m = COLLABO_CAPTION_RE.search(table)
    title = tidy_collabo_title(caption_m.group(1)) if caption_m else ""
    title = title or tidy_collabo_title(item["title"])
    event = {
        "date": period[0],
        "title": title,
        "venue": venue,
        "city": guess_city(region + " " + venue),
        "category": collabo_category(item["list_category"], title),
        "note": "",
        "src": "collabo_cafe",
        "url": item["url"],
    }
    if period[1]:
        event["dateEnd"] = period[1]
    return event


def resolve_collabo(item: dict) -> dict | None:
    page = http_get(item["url"])
    # 記事には特典期間・通販などの表も並ぶ。期間と会場の欄がある表を順に見て、愛知の会場が確定できた表を使う
    review: NeedsReview | None = None
    found_table = False
    for table in COLLABO_TABLE_RE.findall(page):
        try:
            event = resolve_collabo_table(table, item)
        except NeedsReview as exc:
            review = review or exc
            found_table = True
            continue
        if event:
            return event
        found_table = found_table or bool(COLLABO_ROW_RE.search(table))
    if review:
        raise review
    if not found_table:
        raise NeedsReview("開催概要の表(期間・会場)が見つからない")
    return None


# ---------------------------------------------------------------- 統合処理

def event_end(event: dict) -> str:
    return event.get("dateEnd") or event.get("date", "")


def find_match(candidate: dict, events: list[dict]) -> dict | None:
    cand_key = title_key(candidate["title"])
    cand_end = event_end(candidate)
    for event in events:
        if event.get("url") == candidate["url"]:
            return event
        key = title_key(event.get("title", ""))
        same_title = key == cand_key or (
            min(len(key), len(cand_key)) >= 4 and (key in cand_key or cand_key in key)
        )
        overlaps = event.get("date", "") <= cand_end and candidate["date"] <= event_end(event)
        if same_title and overlaps:
            return event
    return None


def merge_source(events: list[dict], scraped: list[dict], src: str, today: str,
                 resolve_new, stats: dict, budget: int = MAX_DETAIL_FETCHES) -> None:
    """取得した公演を既存データへ反映する。

    既存の公演は category/note/url などClaudeが整えた情報を保持し、同じ情報源由来の
    ものに限り会期だけ最新へ更新する。見つからない既存公演は(期限切れ以外)削除しない。
    """
    for item in scraped:
        if event_end(item) < today:
            continue
        match = find_match(item, events)
        if match:
            if src in LIST_DATES_RELIABLE and match.get("src") == src and (
                match.get("date") != item["date"] or match.get("dateEnd", "") != item["dateEnd"]
            ):
                match["date"] = item["date"]
                if item["dateEnd"]:
                    match["dateEnd"] = item["dateEnd"]
                else:
                    match.pop("dateEnd", None)
                stats["updated"] += 1
            continue
        if budget <= 0:
            stats["skipped_budget"] += 1
            continue
        budget -= 1
        try:
            new_event = resolve_new(item)
        except NeedsReview as exc:
            stats["needs_review"].append({"title": item["title"], "url": item["url"], "reason": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001
            stats["detail_errors"] += 1
            print(f"[warn] 詳細取得に失敗したため新規追加を見送り: {item['title']} ({exc!r})")
            continue
        if not new_event or event_end(new_event) < today:
            continue
        # 一覧のタイトルと正式名が違う場合に備え、詳細から作った情報でもう一度重複を確認する
        if find_match(new_event, events):
            continue
        events.append(new_event)
        stats["added"] += 1
        stats["added_titles"].append(new_event["title"])


def resolve_artscape(item: dict) -> dict | None:
    address = fetch_artscape_address(item["url"])
    # 愛知の一覧に他県の施設(例: 岐阜県恵那市の美術館)が混ざることがあるため住所で確認する
    if address and "愛知県" not in address:
        print(f"[info] 愛知県外の住所のため除外: {item['title']} ({address})")
        return None
    event = {
        "date": item["date"],
        "title": item["title"],
        "venue": item["venue"],
        "city": city_from_address(address),
        "category": guess_category(item["title"], item["venue"]),
        "note": "",
        "src": "artscape",
        "url": item["url"],
    }
    if item["dateEnd"]:
        event["dateEnd"] = item["dateEnd"]
    return event


def resolve_gengaten(item: dict) -> dict | None:
    venue = fetch_gengaten_venue(item["url"])
    if not venue:
        return None
    event = {
        "date": item["date"],
        "title": item["title"],
        "venue": venue,
        "city": "名古屋市" if "名古屋" in item["region"] else "",
        "category": "アニメ・原画展",
        "note": "",
        "src": "gengaten",
        "url": item["url"],
    }
    if item["dateEnd"]:
        event["dateEnd"] = item["dateEnd"]
    return event


# 一覧に載っている会期が「その会場の会期」と言える情報源。コラボカフェ.comの一覧は全会場まとめた期間なので含めない
LIST_DATES_RELIABLE = {"artscape", "museum_or_jp", "gengaten"}

# (表示名, src, 一覧取得, 新規分の詳細確認, 1回あたりの詳細ページ取得上限)
SOURCES = [
    ("artscape", "artscape", fetch_artscape_list, resolve_artscape, 40),
    ("インターネットミュージアム", "museum_or_jp", fetch_museum_list, resolve_museum, 40),
    ("原画展.info", "gengaten", fetch_gengaten_list, resolve_gengaten, 40),
    ("コラボカフェ.com", "collabo_cafe", fetch_collabo_list, resolve_collabo, 70),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="愛知アート帳のデータ更新")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="events.json のパス")
    parser.add_argument("--dry-run", action="store_true", help="ファイルを書き換えず結果だけ表示")
    args = parser.parse_args()

    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date().isoformat()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    events: list[dict] = data.get("events", [])

    before = len(events)
    events = [e for e in events if event_end(e) >= today]
    expired = before - len(events)

    diagnostics: dict[str, dict] = {}
    all_ok = True
    for label, src, fetch_fn, resolve_fn, budget in SOURCES:
        stats = {"added": 0, "updated": 0, "skipped_budget": 0, "detail_errors": 0,
                 "added_titles": [], "needs_review": []}
        try:
            scraped = fetch_fn()
            if not scraped:
                raise RuntimeError("0件(サイト構造の変更またはブロックの可能性)")
            merge_source(events, scraped, src, today, resolve_fn, stats, budget)
            diagnostics[label] = {"ok": True, "scraped": len(scraped), "error": None, **stats}
            print(f"[ok] {label}: 取得{len(scraped)}件 / 追加{stats['added']} 更新{stats['updated']}")
        except Exception as exc:  # noqa: BLE001 - 1ソースの失敗で全体を止めない
            all_ok = False
            message = f"{type(exc).__name__}: {exc}"
            diagnostics[label] = {"ok": False, "scraped": 0, "error": message, **stats}
            print(f"[warn] {label}の取得に失敗したため既存データを維持: {message}")

    events.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))
    data["events"] = events
    data["last_updated"] = today
    data["last_updated_note"] = (
        "GitHub Actionsでartscape・インターネットミュージアム・原画展.info・コラボカフェ.comを自動取得"
        "（それ以外の情報源はClaudeの定期確認分を維持）"
        + ("" if all_ok else "（一部ソースは取得失敗のため既存データを維持）")
    )

    print(f"期限切れ削除 {expired}件 / 現在 {len(events)}件")
    if args.dry_run:
        print("--dry-run のためファイルは書き換えません")
        return

    args.data.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status_path = args.data.with_name("scrape_status.json")
    status_path.write_text(
        json.dumps({"checked_at": today, "sources": diagnostics}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
