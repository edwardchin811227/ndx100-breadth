"""Daily pipeline: sync membership, download prices, compute % of Nasdaq-100 members above their
50-day moving average for the last three years, validate, and write site/data/.

Exit code 1 means validation failed and nothing was written, so the site keeps the previous data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import warnings
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import membership as ms  # noqa: E402

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = ms.ROOT
OUT_DIR = ROOT / "site" / "data"
MA_DAYS = 50
YEARS = 3
INDEX_SYMBOL = "^NDX"
MIN_COVERAGE_LATEST = 0.90
MAX_STALE_DAYS = 6
SPIKE = 0.40


def yahoo_symbol(t: str) -> str:
    return t.replace(".", "-")


def download(tickers: list[str], start: dt.date, retry: set[str] | None = None) -> pd.DataFrame:
    """Adjusted closes. Only symbols in `retry` (default: all) are retried when Yahoo returns nothing,
    so delisted tickers cost a single request."""
    symbols = [yahoo_symbol(t) for t in tickers]
    retry_symbols = {yahoo_symbol(t) for t in (retry if retry is not None else tickers)}
    closes: dict[str, pd.Series] = {}
    pending = symbols
    for attempt in range(4):
        if not pending:
            break
        df = yf.download(pending, start=start.isoformat(), auto_adjust=True, progress=False,
                         threads=True, group_by="column")
        close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(
            columns={"Close": pending[0]})
        for s in pending:
            if s in close and close[s].notna().sum() > 0:
                closes[s] = close[s].dropna()
        pending = [s for s in pending if s not in closes and s in retry_symbols]
        if pending:
            print(f"  retry {attempt + 1}: {len(pending)} symbols without data", flush=True)
            time.sleep(30 * (attempt + 1))
    back = {yahoo_symbol(t): t for t in tickers}
    return pd.DataFrame({back[s]: v for s, v in closes.items()}).sort_index()


TIINGO_MIN_ROWS = 60
TIINGO_MAX_SYMBOLS = 20


def fill_from_tiingo(px: pd.DataFrame, doc: dict, universe: list[str], start: dt.date) -> tuple[pd.DataFrame, list[str]]:
    """Replace series Yahoo lacks (mostly acquired / delisted names) with Tiingo adjusted closes.

    Tiingo keeps stale rows after a delisting and sometimes reuses the symbol, so each series is
    cut at the day the ticker left the index.
    """
    key = os.environ.get("TIINGO_API_KEY")
    if not key:
        return px, []
    wanted = [t for t in universe if t not in px or px[t].notna().sum() < TIINGO_MIN_ROWS][:TIINGO_MAX_SYMBOLS]
    filled = []
    px = px.copy()
    for t in wanted:
        try:
            r = ms.http_get(f"https://api.tiingo.com/tiingo/daily/{t.replace('.', '-')}/prices",
                            {"startDate": start.isoformat(), "token": key}, tries=3)
            rows = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: Tiingo {t} failed: {str(e).replace(key, '***')}")
            continue
        if not isinstance(rows, list) or not rows:
            continue
        s = pd.Series({pd.Timestamp(x["date"][:10]): x["adjClose"] for x in rows}).sort_index()
        until = ms.removed_on(doc, t)
        if until:
            s = s[s.index < pd.Timestamp(until)]
        if len(s) < TIINGO_MIN_ROWS:
            continue
        px = px.drop(columns=t, errors="ignore").join(s.rename(t), how="outer")
        filled.append(t)
    if filled:
        print("filled from Tiingo:", ", ".join(filled))
    return px.sort_index(), filled


def _money(s: str) -> float | None:
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def patch_latest_from_nasdaq(px: pd.DataFrame, index_close: pd.Series):
    """Yahoo sometimes publishes the latest daily bar many hours late. Nasdaq's list endpoint carries
    every member's last sale in one request; once the market is closed that is the official close."""
    h = {**ms.BROWSER_UA, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    try:
        info = ms.http_get("https://api.nasdaq.com/api/quote/NDX/info?assetclass=index", headers=h,
                           tries=3).json()["data"]
        if info.get("marketStatus") != "Closed":
            return px, index_close, None
        day = pd.Timestamp(dt.datetime.strptime(info["primaryData"]["lastTradeTimestamp"], "%b %d, %Y"))
        listing = ms.http_get(ms.NASDAQ_API, headers=h, tries=3).json()["data"]
        if pd.Timestamp(dt.datetime.strptime(listing["date"], "%b %d, %Y")) != day:
            return px, index_close, None
    except Exception as e:  # noqa: BLE001
        print("WARNING: Nasdaq latest-close patch unavailable:", e)
        return px, index_close, None
    listed = [t for t in (ms.clean_ticker(r["symbol"]) for r in listing["data"]["rows"]) if t in px]
    have = px.reindex(columns=listed).notna().mean(axis=1)
    last_good = have[have >= MIN_COVERAGE_LATEST].index.max()
    if day <= last_good:
        return px, index_close, None

    ndx = _money(info["primaryData"]["lastSalePrice"])
    if ndx is None:
        return px, index_close, None
    px = px.copy()
    patched, skipped = 0, []
    for r in listing["data"]["rows"]:
        t = ms.clean_ticker(r["symbol"])
        price = _money(r["lastSalePrice"])
        if not t or price is None or t not in px:
            continue
        prev = px[t].dropna()
        if prev.empty or abs(price / prev.iloc[-1] - 1) > SPIKE:
            skipped.append(t)
            continue
        px.loc[day, t] = price
        patched += 1
    px = px.sort_index()
    if day not in index_close.index:
        index_close = pd.concat([index_close, pd.Series({day: ndx})]).sort_index()
    print(f"patched {day.date()} from Nasdaq quotes: {patched} closes, NDX {ndx}"
          + (f"; skipped (>{SPIKE:.0%} move): {skipped}" if skipped else ""))
    return px, index_close, day.date().isoformat()


def compute(doc: dict, px: pd.DataFrame, index_close: pd.Series, start: dt.date) -> dict:
    above = {}
    for t in px.columns:
        s = px[t].dropna()
        sma = s.rolling(MA_DAYS).mean()
        above[t] = (s > sma).where(sma.notna())
    above = pd.DataFrame(above)

    days = index_close.loc[pd.Timestamp(start):].index
    rows = []
    for d in days:
        key = d.date().isoformat()
        members = sorted(ms.members_on(doc, key))
        vals = above.reindex(columns=members).loc[d] if d in above.index else pd.Series(dtype=float)
        valid = vals.dropna()
        missing = sorted(set(members) - set(valid.index))
        n_above = int(valid.sum())
        rows.append({
            "date": key,
            "pct": round(100 * n_above / len(valid), 2) if len(valid) else None,
            "above": n_above,
            "valid": int(len(valid)),
            "members": len(members),
            "ndx": round(float(index_close.loc[d]), 2),
            "missing": missing,
        })
    return {"rows": rows}


def spikes(px: pd.DataFrame, since: dt.date) -> list[str]:
    r = px.loc[pd.Timestamp(since):].pct_change(fill_method=None)
    out = []
    for t in r.columns:
        hit = r[t][r[t].abs() > SPIKE]
        out += [f"{t} {d.date()} {v:+.0%}" for d, v in hit.items()]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true", help="skip Nasdaq official list check")
    ap.add_argument("--allow-stale", action="store_true")
    args = ap.parse_args()

    doc = ms.load()
    if not args.no_sync:
        try:
            event = ms.sync(doc)
            ms.save(doc)
            if event:
                print("membership change recorded:", event)
        except Exception as e:  # noqa: BLE001
            print("WARNING: official list check failed, using stored membership:", e)

    today = dt.date.fromisoformat(ms.et_today())
    start = today.replace(year=today.year - YEARS)
    price_start = start - dt.timedelta(days=MA_DAYS * 2 + 30)
    universe = sorted(ms.all_tickers_since(doc, start.isoformat()))
    print(f"downloading {len(universe)} tickers + {INDEX_SYMBOL} from {price_start}", flush=True)

    current = ms.members_on(doc, "9999-12-31")
    out = OUT_DIR / "breadth.json"
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    previously_empty = set(prev.get("yahoo_missing", prev.get("no_price_data", [])))
    px = download(universe, price_start, retry={t for t in universe if t in current or t not in previously_empty})
    yahoo_missing = sorted(set(universe) - set(px.columns))
    px, tiingo_filled = fill_from_tiingo(px, doc, universe, price_start)
    idx = download([INDEX_SYMBOL], price_start)
    if INDEX_SYMBOL not in idx:
        print("ERROR: index data unavailable")
        return 1
    index_close = idx[INDEX_SYMBOL].dropna()
    px = px.dropna(how="all")
    px, index_close, patched_day = patch_latest_from_nasdaq(px, index_close)

    result = compute(doc, px, index_close, start)
    rows = [r for r in result["rows"] if r["pct"] is not None]
    # a trailing day with only partial data means the source hasn't finished publishing it yet
    while rows and rows[-1]["valid"] < MIN_COVERAGE_LATEST * rows[-1]["members"]:
        print(f"NOTE {rows[-1]['date']} only {rows[-1]['valid']}/{rows[-1]['members']} closes, not published yet")
        rows.pop()
    no_data = sorted(set(universe) - set(px.columns))

    latest = rows[-1]
    coverage = latest["valid"] / latest["members"]
    age = (today - dt.date.fromisoformat(latest["date"])).days
    problems = []
    if coverage < MIN_COVERAGE_LATEST:
        problems.append(f"latest day coverage {latest['valid']}/{latest['members']} below {MIN_COVERAGE_LATEST:.0%}")
    if age > MAX_STALE_DAYS and not args.allow_stale:
        problems.append(f"latest data {latest['date']} is {age} days old")
    if len(rows) < 250 * YEARS * 0.95:
        problems.append(f"only {len(rows)} trading days computed")
    if problems:
        print("VALIDATION FAILED, not writing output:\n  " + "\n  ".join(problems))
        return 1

    for s in spikes(px, start):
        print("NOTE large daily move (check for bad data):", s)

    events = [e for e in doc["events"] if e["date"] >= rows[0]["date"]]
    gaps: dict[str, list] = {}
    for r in rows:
        for t in r["missing"]:
            g = gaps.setdefault(t, [r["date"], r["date"], 0])
            g[1] = r["date"]
            g[2] += 1

    payload = {
        "latest_date": latest["date"],
        "latest_source": "nasdaq-quote" if patched_day == latest["date"] else "yahoo",
        "ma_days": MA_DAYS,
        "start_date": rows[0]["date"],
        "series": {k: [r[k] for r in rows] for k in ("date", "pct", "above", "valid", "members", "ndx")},
        "missing": {r["date"]: r["missing"] for r in rows if r["missing"]},
        "no_price_data": no_data,
        "yahoo_missing": yahoo_missing,
        "tiingo_filled": tiingo_filled,
        "gaps": gaps,
        "events": [{k: e[k] for k in ("date", "added", "removed", "source", "note")} for e in events],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    old.pop("generated_at", None)
    if old == payload:
        print(f"no change (latest {latest['date']})")
        return 0
    payload["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    pd.DataFrame(rows).drop(columns="missing").to_csv(OUT_DIR / "breadth.csv", index=False)
    print(f"wrote {len(rows)} days {rows[0]['date']}..{latest['date']}; latest {latest['pct']}% "
          f"({latest['above']}/{latest['valid']}, members {latest['members']}); "
          f"no price data: {', '.join(no_data) or 'none'}; Tiingo: {', '.join(tiingo_filled) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
