"""Nasdaq-100 membership history.

`data/membership.json` holds the member list as of `start_date` plus dated change events.
Members on day D = initial list with every event dated <= D applied.

CLI:
  python scripts/membership.py rebuild   # reconstruct history from Wikipedia revisions
  python scripts/membership.py sync      # compare with Nasdaq's official list, record changes
  python scripts/membership.py show 2024-03-15
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
MEMBERSHIP_PATH = ROOT / "data" / "membership.json"
WIKI_CACHE_PATH = ROOT / "data" / ".wiki_cache.json"

ET = ZoneInfo("America/New_York")
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_INDEX = "https://en.wikipedia.org/w/index.php"
# Wikipedia moved the component table from the main article to the list article in July 2026;
# the main article's table stopped being maintained after that.
WIKI_PAGES = ["Nasdaq-100", "List of NASDAQ-100 companies"]
NASDAQ_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
UA = {"User-Agent": "ndx100-breadth/1.0 (https://github.com/edwardchin811227/ndx100-breadth)"}
BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
MIN_MEMBERS, MAX_MEMBERS = 95, 106
REVERT_WINDOW = dt.timedelta(days=3)


def http_get(url: str, params: dict | None = None, headers: dict | None = None,
             tries: int = 6) -> requests.Response:
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=60)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def clean_ticker(raw: str) -> str | None:
    t = re.sub(r"\[.*?\]", "", str(raw)).strip().upper()
    return t if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", t) else None


# ----------------------------------------------------------------- stored history
def load() -> dict:
    return json.loads(MEMBERSHIP_PATH.read_text(encoding="utf-8"))


def save(doc: dict) -> None:
    doc["events"].sort(key=lambda e: e["date"])
    MEMBERSHIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMBERSHIP_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def members_on(doc: dict, day: str) -> set[str]:
    m = set(doc["initial"])
    for e in doc["events"]:
        if e["date"] > day:
            break
        m -= set(e["removed"])
        m |= set(e["added"])
    return m


def all_tickers_since(doc: dict, day: str) -> set[str]:
    s = members_on(doc, day)
    for e in doc["events"]:
        if e["date"] > day:
            s |= set(e["added"])
    return s


# ----------------------------------------------------------------- official list
def fetch_official() -> set[str]:
    last = None
    for attempt in range(3):
        try:
            j = http_get(NASDAQ_API, headers={**BROWSER_UA, "Origin": "https://www.nasdaq.com",
                                              "Referer": "https://www.nasdaq.com/"}).json()
            rows = j["data"]["data"]["rows"]
            tickers = {t for t in (clean_ticker(r["symbol"]) for r in rows) if t}
            if MIN_MEMBERS <= len(tickers) <= MAX_MEMBERS:
                return tickers
            last = RuntimeError(f"official list has {len(tickers)} symbols")
        except (RuntimeError, KeyError, TypeError, ValueError) as e:
            last = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Nasdaq official list unavailable: {last}")


def fetch_wikipedia_current() -> set[str]:
    html = http_get("https://en.wikipedia.org/wiki/" + WIKI_PAGES[1].replace(" ", "_")).text
    found = _parse_members(html)
    if not found:
        raise RuntimeError("no component table on the current Wikipedia list page")
    return set(found)


def et_today() -> str:
    return dt.datetime.now(ET).date().isoformat()


CONFIRM_AFTER_DAYS = 2


def sync(doc: dict) -> dict | None:
    """Detect membership changes against the stored latest state.

    A change is recorded once Nasdaq's official list and the current Wikipedia list agree on it,
    or once the same change has persisted for CONFIRM_AFTER_DAYS. Until then it is kept as
    `pending`, so one bad API response cannot write a false event. The event is dated on the
    day the change was first seen.
    """
    sources: dict[str, set[str]] = {}
    errors = []
    for name, fn in (("nasdaq-api", fetch_official), ("wikipedia", fetch_wikipedia_current)):
        try:
            sources[name] = fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    if not sources:
        raise RuntimeError("; ".join(errors))
    for err in errors:
        print("WARNING:", err)

    today = et_today()
    current = members_on(doc, "9999-12-31")
    diffs = {k: (sorted(v - current), sorted(current - v)) for k, v in sources.items()}
    primary = "nasdaq-api" if "nasdaq-api" in diffs else "wikipedia"
    added, removed = diffs[primary]
    if not added and not removed:
        doc.pop("pending", None)
        return None

    pending = doc.get("pending")
    if pending and (pending["added"], pending["removed"]) == (added, removed):
        first_seen = pending["first_seen"]
    else:
        first_seen = today
    agreed = len(diffs) == 2 and len({tuple(map(tuple, d)) for d in diffs.values()}) == 1
    persisted = (dt.date.fromisoformat(today) - dt.date.fromisoformat(first_seen)).days >= CONFIRM_AFTER_DAYS
    if not (agreed or persisted):
        doc["pending"] = {"first_seen": first_seen, "added": added, "removed": removed, "source": primary}
        print(f"pending membership change (first seen {first_seen}): +{added} -{removed}")
        return None

    doc.pop("pending", None)
    how = "confirmed by Nasdaq and Wikipedia" if agreed else f"persisted {CONFIRM_AFTER_DAYS}+ days in {primary}"
    event = {"date": first_seen, "added": added, "removed": removed, "source": primary,
             "ref": NASDAQ_API if primary == "nasdaq-api" else WIKI_PAGES[1],
             "note": f"detected by daily check, {how}"}
    doc["events"].append(event)
    return event


# ----------------------------------------------------------------- wikipedia rebuild
def _wiki_revisions(title: str, start: str) -> list[dict]:
    """All revisions from `start`, plus the last revision before it."""
    base = {"action": "query", "prop": "revisions", "titles": title,
            "rvprop": "ids|timestamp|comment", "format": "json"}
    before = http_get(WIKI_API, {**base, "rvlimit": 1, "rvdir": "older", "rvstart": start}).json()
    out = list(before["query"]["pages"].values())[0].get("revisions", [])
    cont: dict = {}
    while True:
        j = http_get(WIKI_API, {**base, "rvlimit": 500, "rvdir": "newer", "rvstart": start, **cont}).json()
        out += list(j["query"]["pages"].values())[0].get("revisions", [])
        if "continue" not in j:
            break
        cont = j["continue"]
    for r in out:
        r["page"] = title
    return out


def _wiki_members(revid: int, cache: dict) -> list[str] | None:
    key = str(revid)
    if key in cache:
        return cache[key]
    found = _parse_members(http_get(WIKI_INDEX, {"oldid": revid}).text)
    cache[key] = found
    time.sleep(0.2)
    return found


def _parse_members(html: str) -> list[str] | None:
    try:
        tables = pd.read_html(io.StringIO(html), flavor="lxml")
    except ValueError:
        return None
    for t in tables:
        cols = [str(c) for c in t.columns]
        hit = [c for c in cols if c.lower().startswith(("ticker", "symbol"))]
        if not hit:
            continue
        tickers = sorted({x for x in (clean_ticker(v) for v in t[t.columns[cols.index(hit[0])]]) if x})
        if MIN_MEMBERS <= len(tickers) <= MAX_MEMBERS:
            return tickers
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _dates_in(text: str, edited: dt.date) -> list[dt.date]:
    out = []
    for m in re.finditer(r"(\d{1,2})-([a-z]{3})[a-z]*-(\d{4})", text):
        if m.group(2) in _MONTHS:
            out.append(dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))))
    for m in re.finditer(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", text):
        if m.group(1) in _MONTHS:
            out.append(dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))))
    return sorted({d for d in out if abs((d - edited).days) <= 20})


def _effective_date(comment: str, ticker: str, edited: dt.date) -> dt.date | None:
    """Date stated in the edit summary, preferring the clause that names this ticker."""
    c = comment.lower()
    for part in re.split(r"[;|]", c):
        if re.search(rf"\b{re.escape(ticker.lower())}\b", part):
            dates = _dates_in(part, edited)
            if len(dates) == 1:
                return dates[0]
    dates = _dates_in(c, edited)
    return dates[0] if len(set(dates)) == 1 else None


def _annual_reconstitution(edited: dt.date) -> dt.date | None:
    """Annual reconstitution takes effect before the open on the Monday after December's third Friday."""
    if edited.month != 12:
        return None
    first = dt.date(edited.year, 12, 1)
    third_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
    monday = third_friday + dt.timedelta(days=3)
    return monday if 0 <= (edited - monday).days <= 7 or 0 < (monday - edited).days <= 3 else None


def rebuild(start: str = "2023-01-01") -> dict:
    WIKI_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(WIKI_CACHE_PATH.read_text()) if WIKI_CACHE_PATH.exists() else {}
    revs: list[dict] = []
    for title in WIKI_PAGES:
        revs += _wiki_revisions(title, start + "T00:00:00Z")
    todo = [r["revid"] for r in revs if str(r["revid"]) not in cache]
    print(f"{len(revs)} revisions, {len(todo)} not cached", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, _ in enumerate(pool.map(lambda rid: _wiki_members(rid, cache), todo), 1):
            if i % 25 == 0:
                WIKI_CACHE_PATH.write_text(json.dumps(dict(cache)))
                print(f"  {i}/{len(todo)}", flush=True)
    WIKI_CACHE_PATH.write_text(json.dumps(cache))
    for r in revs:
        r["members"] = cache[str(r["revid"])]

    list_page = [r for r in revs if r["page"] == WIKI_PAGES[1] and r["members"]]
    switch = min((r["timestamp"] for r in list_page if r["timestamp"] >= start), default="9999")
    timeline = sorted(
        (r for r in revs if r["members"] and
         ((r["page"] == WIKI_PAGES[0] and r["timestamp"] < switch) or
          (r["page"] == WIKI_PAGES[1] and r["timestamp"] >= switch))),
        key=lambda r: r["timestamp"])

    # per-ticker toggles, then drop edits that were reverted within REVERT_WINDOW (vandalism, typos)
    toggles = []
    for prev, cur in zip(timeline, timeline[1:]):
        a, b = set(prev["members"]), set(cur["members"])
        ts = dt.datetime.fromisoformat(cur["timestamp"].replace("Z", "+00:00"))
        for t in b - a:
            toggles.append({"t": t, "kind": "added", "ts": ts, "rev": cur})
        for t in a - b:
            toggles.append({"t": t, "kind": "removed", "ts": ts, "rev": cur})
    keep = []
    by_ticker: dict[str, list] = {}
    for tg in toggles:
        by_ticker.setdefault(tg["t"], []).append(tg)
    for seq in by_ticker.values():
        i = 0
        while i < len(seq):
            if i + 1 < len(seq) and seq[i + 1]["ts"] - seq[i]["ts"] <= REVERT_WINDOW:
                i += 2
                continue
            keep.append(seq[i])
            i += 1

    changes_per_rev: dict[int, int] = {}
    for tg in keep:
        changes_per_rev[tg["rev"]["revid"]] = changes_per_rev.get(tg["rev"]["revid"], 0) + 1
    grouped: dict[str, dict] = {}
    for tg in sorted(keep, key=lambda x: x["ts"]):
        rev = tg["rev"]
        edited = tg["ts"].astimezone(ET).date()
        eff = _effective_date(rev.get("comment", ""), tg["t"], edited)
        if eff is None and changes_per_rev[rev["revid"]] >= 6:
            eff = _annual_reconstitution(edited)
        eff = eff or edited
        e = grouped.setdefault(eff.isoformat(), {
            "date": eff.isoformat(), "added": [], "removed": [], "source": "wikipedia",
            "ref": [], "note": []})
        e[tg["kind"]].append(tg["t"])
        ref = f"{rev['page']} rev {rev['revid']} ({rev['timestamp']})"
        if ref not in e["ref"]:
            e["ref"].append(ref)
            if rev.get("comment"):
                e["note"].append(rev["comment"][:160])
    events = []
    for e in grouped.values():
        e["added"], e["removed"] = sorted(e["added"]), sorted(e["removed"])
        e["ref"], e["note"] = "; ".join(e["ref"]), " | ".join(e["note"])
        events.append(e)

    first_before = [r for r in timeline if r["timestamp"] < start]
    initial = first_before[-1] if first_before else timeline[0]
    return {"start_date": start, "initial": initial["members"], "events": sorted(events, key=lambda e: e["date"]),
            "sources": {"history": "Wikipedia revision history of " + " / ".join(WIKI_PAGES),
                        "daily": NASDAQ_API}}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "show"
    if cmd == "rebuild":
        doc = rebuild(argv[1] if len(argv) > 1 else "2023-01-01")
        save(doc)
        latest = members_on(doc, "9999-12-31")
        print(f"saved {len(doc['events'])} events; latest reconstructed list has {len(latest)} tickers")
        try:
            official = fetch_official()
            print("vs Nasdaq official list -> only in Wikipedia:", sorted(latest - official),
                  "| only in Nasdaq:", sorted(official - latest))
        except Exception as e:  # noqa: BLE001
            print("could not compare with official list:", e)
    elif cmd == "sync":
        doc = load()
        event = sync(doc)
        save(doc)
        print("membership change recorded:" if event else "no confirmed membership change", event or "")
    elif cmd == "show":
        doc = load()
        day = argv[1] if len(argv) > 1 else et_today()
        m = sorted(members_on(doc, day))
        print(day, len(m), " ".join(m))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
