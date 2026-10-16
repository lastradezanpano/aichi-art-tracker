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

def http_get(url: str, retries: int = 3, timeout: int = 40) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
        time.sleep(2 * (attempt + 1))
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


NOISE_RE = re.compile(r"[\s!！?？「」『』（）()\[\]【】～〜\-－–—ー・:：、。,.&＆×]")


def title_key(title: str) -> str:
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
                 resolve_new, stats: dict) -> None:
    """取得した公演を既存データへ反映する。

    既存の公演は category/note/url などClaudeが整えた情報を保持し、同じ情報源由来の
    ものに限り会期だけ最新へ更新する。見つからない既存公演は(期限切れ以外)削除しない。
    """
    budget = MAX_DETAIL_FETCHES
    for item in scraped:
        if event_end(item) < today:
            continue
        match = find_match(item, events)
        if match:
            if match.get("src") == src and (
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
        except Exception as exc:  # noqa: BLE001
            stats["detail_errors"] += 1
            print(f"[warn] 詳細取得に失敗したため新規追加を見送り: {item['title']} ({exc!r})")
            continue
        if new_event:
            events.append(new_event)
            stats["added"] += 1


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


SOURCES = [
    ("artscape", "artscape", fetch_artscape_list, resolve_artscape),
    ("原画展.info", "gengaten", fetch_gengaten_list, resolve_gengaten),
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
    for label, src, fetch_fn, resolve_fn in SOURCES:
        stats = {"added": 0, "updated": 0, "skipped_budget": 0, "detail_errors": 0}
        try:
            scraped = fetch_fn()
            if not scraped:
                raise RuntimeError("0件(サイト構造の変更またはブロックの可能性)")
            merge_source(events, scraped, src, today, resolve_fn, stats)
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
        "GitHub Actionsでartscape・原画展.infoを自動取得（それ以外の情報源は確認済みの既存データを維持）"
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
