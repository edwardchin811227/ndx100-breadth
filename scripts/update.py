"""Daily pipeline: sync membership, download prices, compute % of Nasdaq-100 members above their
50-day moving average for the last three years, validate, and write site/data/.

Exit code 1 means validation failed and nothing was written, so the site keeps the previous data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
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
SPIKE = 0.40

# Freshness is judged against the US equity trading session, not against an arbitrary number of
# calendar days: the newest usable date must be the latest session whose regular close (early
# closes and DST included) plus this grace window is already in the past.
#
# "NASDAQ" is the name exchange_calendars exposes for the Nasdaq Stock Market; the library maps it
# to the same rules as the XNYS calendar (Nasdaq and NYSE share sessions, holidays and early
# closes), so it is used directly rather than approximating holidays by hand.
MARKET_CALENDAR = "NASDAQ"
PUBLICATION_GRACE = dt.timedelta(hours=2)
SOURCE_TIMEZONE = ZoneInfo("America/New_York")
# Bounded lookback: how many sessions back from the current date we are willing to scan for the
# latest close + grace that has passed. It only needs to cover a weekend plus a holiday run.
SESSION_LOOKBACK = 14
_CALENDAR_CACHE: dict[str, object] = {}


def yahoo_symbol(t: str) -> str:
    return t.replace(".", "-")


# ----------------------------------------------------------- trading-session freshness
def market_calendar():
    """Maintained US equity calendar (Nasdaq sessions, mapped by exchange_calendars to XNYS rules)."""
    cal = _CALENDAR_CACHE.get(MARKET_CALENDAR)
    if cal is None:
        cal = xcals.get_calendar(MARKET_CALENDAR)
        _CALENDAR_CACHE[MARKET_CALENDAR] = cal
    return cal


def _session_close(day: dt.date):
    """UTC timestamp of the regular close for `day`, or None when `day` is not a session."""
    cal = market_calendar()
    ts = pd.Timestamp(day)
    if ts < cal.first_session or ts > cal.last_session:
        return None
    try:
        if not bool(cal.is_session(ts)):
            return None
        return cal.session_close(ts)
    except Exception as e:  # noqa: BLE001 - a calendar failure must never look like freshness
        print(f"WARNING: calendar lookup failed for {day}: {e}")
        return None


def _session_candidates(now_utc: dt.datetime, lookback: int = SESSION_LOOKBACK) -> list[dt.date]:
    """The bounded list of sessions at or before the current date, newest first.

    Empty when the current date is outside the maintained calendar's range, which the caller must
    treat as "expected session unknown" rather than as "nothing is due".
    """
    cal = market_calendar()
    today = pd.Timestamp(now_utc.astimezone(SOURCE_TIMEZONE).date())
    if today < cal.first_session or today > cal.last_session:
        return []
    end = cal.date_to_session(today, direction="previous")
    start = end if lookback <= 1 else cal.sessions_window(end, -(lookback - 1))[0]
    return [ts.date() for ts in reversed(cal.sessions_in_range(start, end))]


def expected_completed_session(now_utc: dt.datetime, grace: dt.timedelta = PUBLICATION_GRACE,
                               lookback: int = SESSION_LOOKBACK):
    """Latest session whose close plus `grace` has already passed at `now_utc`.

    Returns None when the calendar cannot establish it (unknown session, or the current date is
    outside the calendar's range), so callers must treat None as a hard failure.
    """
    now = pd.Timestamp(now_utc)
    for session in _session_candidates(now_utc, lookback):
        close = _session_close(session)
        if close is None:
            return None
        if close + pd.Timedelta(grace) <= now:
            return session
    return None


def _as_timestamp(value) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts


UNKNOWN_SESSION = ("the expected trading session could not be established from the maintained "
                   f"{MARKET_CALENDAR} calendar")


def freshness_problems(data_through: dt.date | None, expected: dt.date | None) -> list[str]:
    """Freshness failure when the newest usable session is older than the expected one.

    An unknown expected session fails closed: without it we cannot claim the data is current.
    """
    if expected is None:
        return [UNKNOWN_SESSION]
    if data_through is None:
        return [f"no usable trading session in the downloaded data; expected {expected.isoformat()}"]
    if dt.date.fromisoformat(str(data_through)) < expected:
        return [f"latest data {data_through} is older than the expected completed session "
                f"{expected.isoformat()} (close + {PUBLICATION_GRACE})"]
    return []


def describe_freshness(data_through: dt.date | None, expected: dt.date | None) -> list[str]:
    """Diagnostic lines for the expected session, whichever way the freshness check went."""
    if expected is None:
        return [f"expected completed session: UNKNOWN ({UNKNOWN_SESSION}); treating this as a failure"]
    if data_through is None:
        return [f"expected completed session: {expected.isoformat()}; data has no usable session yet"]
    if data_through < expected:
        return [f"expected completed session: {expected.isoformat()}; data only reaches {data_through}",
                f"STALE: latest data {data_through} predates the expected completed session {expected.isoformat()}"]
    if data_through > expected:
        return [f"expected completed session: {expected.isoformat()}; data already reaches {data_through}"]
    return [f"expected completed session: {expected.isoformat()}; data is current"]


def eligible_sessions(px: pd.DataFrame, index_close: pd.Series,
                      now_utc: dt.datetime) -> set[dt.date]:
    """Real sessions whose close + grace has already passed.

    Anything else in a downloaded series (an in-progress session, a non-session row the source
    invented, a date the calendar does not know) is not publishable and must not reach the moving
    average either.
    """
    cal = market_calendar()
    days = {ts.date() for ts in px.dropna(how="all").index}
    days |= {ts.date() for ts in index_close.dropna().index}
    now = pd.Timestamp(now_utc)
    out = set()
    for day in days:
        ts = pd.Timestamp(day)
        if ts < cal.first_session or ts > cal.last_session or not bool(cal.is_session(ts)):
            continue
        if cal.session_close(ts) + pd.Timedelta(PUBLICATION_GRACE) <= now:
            out.add(day)
    return out


def drop_ineligible(px: pd.DataFrame, index_close: pd.Series,
                    eligible: set[dt.date]) -> tuple[pd.DataFrame, pd.Series, list[dt.date]]:
    """Remove every price/index row whose session is not publishable yet."""
    px_days = {ts.date() for ts in px.dropna(how="all").index}
    idx_days = {ts.date() for ts in index_close.dropna().index}
    dropped = sorted((px_days | idx_days) - eligible)
    keep_px = [ts for ts in px.index if ts.date() in eligible]
    keep_idx = [ts for ts in index_close.index if ts.date() in eligible]
    return px.loc[keep_px], index_close.loc[keep_idx], dropped


def coverage_report(px: pd.DataFrame, index_close: pd.Series, doc: dict,
                    reported: dt.date | None = None) -> None:
    """Log current-member price coverage for the newest dates, and the index's latest date."""
    if index_close.empty:
        print("DIAG yahoo index: no rows downloaded")
    else:
        ts = index_close.dropna().index.max()
        print(f"DIAG yahoo index latest date: {ts.date().isoformat()}"
              f" ({len(index_close.dropna())} rows)")
    members = sorted(ms.members_on(doc, "9999-12-31"))
    present = [t for t in members if t in px.columns]
    have = px.reindex(columns=present).notna() if present else pd.DataFrame(index=px.index)
    if have.empty or not present:
        print(f"DIAG membership price coverage: no member columns in the download "
              f"(current members: {len(members)})")
        return
    if reported is not None:
        ts = _as_timestamp(reported)
        if ts is not None and ts in have.index:
            n = int(have.loc[ts].sum())
            print(f"DIAG membership price coverage {ts.date().isoformat()}: {n}/{len(members)} "
                  f"({n / len(members):.0%})")
        elif ts is not None:
            print(f"DIAG membership price coverage {ts.date().isoformat()}: date absent from the "
                  f"downloaded prices (current members: {len(members)})")
    counts = have.sum(axis=1)
    for ts in counts.index[-3:]:
        n = int(counts.loc[ts])
        print(f"DIAG membership coverage {ts.date().isoformat()}: {n}/{len(members)} "
              f"({n / len(members):.0%})")


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


def _parse_nasdaq_date(raw) -> pd.Timestamp | None:
    if not raw:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return pd.Timestamp(dt.datetime.strptime(str(raw).strip(), fmt))
        except ValueError:
            continue
    return None


def _valid_quote(raw) -> float | None:
    """A usable last-sale price: parseable, finite and strictly positive."""
    value = _money(raw)
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def patch_latest_from_nasdaq(px: pd.DataFrame, index_close: pd.Series, expected_session: dt.date | None,
                             current_members: set[str] | None = None):
    """Yahoo sometimes publishes the latest daily bar many hours late. Nasdaq's list endpoint carries
    every member's last sale in one request.

    The index value and the member values are repaired independently: either side may already be
    current on its own. Existing Yahoo values are preserved, and only a *missing* value on the
    expected session is filled, so this can only ever complete the session, never rewrite it. The
    quote feed is a last-sale feed that Nasdaq publishes after the close; it is not an
    independently verified official close, so the result is labelled `nasdaq-quote`.
    """
    if expected_session is None:
        print("DIAG nasdaq fallback: skipped (expected trading session unknown)")
        return px, index_close, None
    day = pd.Timestamp(expected_session)
    h = {**ms.BROWSER_UA, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
    try:
        info = ms.http_get("https://api.nasdaq.com/api/quote/NDX/info?assetclass=index", headers=h,
                           tries=3).json()["data"]
        status = info.get("marketStatus")
        source_day = _parse_nasdaq_date(info.get("primaryData", {}).get("lastTradeTimestamp"))
        if status != "Closed":
            print(f"DIAG nasdaq fallback: skipped (market not closed, Nasdaq reports {status!r})")
            return px, index_close, None
        if source_day is None:
            print("DIAG nasdaq fallback: skipped (NDX info has no usable last-trade date)")
            return px, index_close, None
        listing = ms.http_get(ms.NASDAQ_API, headers=h, tries=3).json()["data"]
        if not isinstance(listing, dict):
            print("DIAG nasdaq fallback: skipped (malformed list response)")
            return px, index_close, None
        listing_day = _parse_nasdaq_date(listing.get("date"))
        if listing_day is None:
            print("DIAG nasdaq fallback: skipped (list endpoint date is missing or malformed)")
            return px, index_close, None
        if listing_day != source_day:
            print(f"DIAG nasdaq fallback: source dates mismatch (NDX info {source_day.date()} vs "
                  f"list {listing_day.date()})")
            return px, index_close, None
    except Exception as e:  # noqa: BLE001
        print("DIAG nasdaq fallback: unavailable:", e)
        return px, index_close, None
    if source_day != day:
        print(f"DIAG nasdaq fallback: skipped (source date {source_day.date()} is not the expected "
              f"completed session {expected_session.isoformat()})")
        return px, index_close, None

    bad_rows, quotes = _listing_quotes(listing)
    info_price = _money(info.get("primaryData", {}).get("lastSalePrice"))
    ndx = info_price if info_price is not None and math.isfinite(info_price) and info_price > 0 else None
    if bad_rows:
        print(f"DIAG nasdaq fallback: ignored {bad_rows} malformed quote row(s)")

    have_index = day in index_close.index and pd.notna(index_close.get(day))
    index_patched = False
    if ndx is None:
        print("DIAG nasdaq fallback: NDX last-sale price is missing or not a usable number")
    elif not have_index:
        index_close = pd.concat([index_close, pd.Series({day: ndx})]).sort_index()
        index_patched = True
        print(f"DIAG nasdaq fallback: filled the {day.date()} index close ({ndx}) from the Nasdaq "
              f"last-sale feed")

    members = sorted(current_members) if current_members is not None else sorted(px.columns)
    absent = [t for t in members if t not in px.columns]
    if day in px.index:
        # `members` are column labels: a member is missing when its cell on the expected session is
        # NaN, and an absent column counts as missing too (handled by `absent` above).
        row_missing = [t for t in members if t in px.columns and pd.isna(px.at[day, t])]
    else:
        # the whole session is absent from the member frame: every current member is missing
        row_missing = [t for t in members if t in px.columns]
    if absent:
        print(f"DIAG nasdaq fallback: {len(absent)} current member(s) have no Yahoo series at all")
    missing_set = set(row_missing)
    quoted = [t for t in members if t in px.columns and t in quotes and t in missing_set]
    skipped = [t for t in quoted if _spike(px[t], quotes[t])]
    fillable = [t for t in quoted if t not in skipped]
    coverage = (len(fillable) / len(row_missing)) if row_missing else 1.0
    print(f"nasdaq fallback candidate {day.date()}: {len(row_missing)} of {len(members)} current "
          f"members missing; {len(fillable)} of {len(quoted)} quoted missing members usable"
          + (f" ({coverage:.0%} of {len(row_missing)} missing)" if row_missing else " (nothing missing)")
          + (f"; skipped (>{SPIKE:.0%} move): {skipped}" if skipped else ""))
    if index_patched and not row_missing:
        print("DIAG nasdaq fallback: only the index close needed filling; member side already current")
        return px, index_close, "nasdaq-quote"
    if row_missing and coverage < MIN_COVERAGE_LATEST:
        print(f"DIAG nasdaq fallback: insufficient patch coverage ({coverage:.0%} of "
              f"{len(row_missing)} missing member closes < {MIN_COVERAGE_LATEST:.0%}); the member "
              f"closes are left exactly as Yahoo published them")
        # Never write a partially quoted session: leave the member side untouched and let the day's
        # own coverage check decide. Filling a partial feed here could lift a badly covered day over
        # the 90% floor on data the feed could not fully vouch for.
        return px, index_close, ("nasdaq-quote" if index_patched else None)
    if fillable:
        px = px.copy()
        for t in fillable:
            px.loc[day, t] = quotes[t]
        px = px.sort_index()
    if not fillable and not index_patched:
        print("DIAG nasdaq fallback: nothing to repair (already current)")
        return px, index_close, None
    print(f"patched {day.date()} from Nasdaq last-sale quotes: {len(fillable)} member closes"
          + (", plus the index close" if index_patched else "")
          + "; source recorded as nasdaq-quote (unverified last-sale fallback, not a guaranteed "
            "official close)")
    return px, index_close, "nasdaq-quote"


def _spike(series: pd.Series, price: float) -> bool:
    prev = series.dropna()
    return prev.empty or abs(price / prev.iloc[-1] - 1) > SPIKE


def _listing_quotes(listing: dict) -> tuple[int, dict[str, float]]:
    """Validated (symbol -> last sale) map from a Nasdaq list payload.

    Malformed containers and rows are counted and dropped instead of raising, so a partially broken
    quote page cannot poison prices with NaN, infinity or non-positive values.
    """
    raw = listing.get("data", {})
    rows = raw.get("rows") if isinstance(raw, dict) else None
    if rows is None:
        return 0, {}
    if not isinstance(rows, list):
        return 1, {}
    bad, quotes = 0, {}
    for row in rows:
        if not isinstance(row, dict):
            bad += 1
            continue
        ticker = ms.clean_ticker(row.get("symbol", ""))
        price = _valid_quote(row.get("lastSalePrice"))
        if ticker is None or price is None:
            bad += 1
            continue
        quotes[ticker] = price
    return bad, quotes


def index_freshness_problems(index_close: pd.Series, expected: dt.date | None) -> list[str]:
    """The index series is what the site plots against, so it must reach the expected session too,
    even when a partial member row is dropped later."""
    if expected is None:
        return []
    filled = index_close.dropna()
    if filled.empty:
        return [f"no index ({INDEX_SYMBOL}) closes in the downloaded data; expected {expected.isoformat()}"]
    latest = filled.index.max().date()
    if latest < expected:
        return [f"{INDEX_SYMBOL} index data only reaches {latest} but the expected completed session "
                f"is {expected.isoformat()} (close + {PUBLICATION_GRACE})"]
    return []


def publishable_through(rows: list[dict], index_close: pd.Series) -> tuple[dt.date | None, list[str]]:
    """Newest date that can be published: index close present and member coverage above the floor.

    Trailing sessions that are still filling in are dropped with a NOTE, so a half-published
    current session, or a session whose members lag the index, is never written out.
    """
    notes: list[str] = []
    if not rows:
        return None, notes
    filled = index_close.dropna()
    with_index = {ts.date().isoformat() for ts in filled.index}
    by_day = {dt.date.fromisoformat(r["date"]): r for r in rows}
    known: dt.date | None = None
    for day in sorted((d for d in by_day if d.isoformat() in with_index), reverse=True):
        if by_day[day]["valid"] >= MIN_COVERAGE_LATEST * by_day[day]["members"]:
            known = day
            break
    if known is None:
        newest = by_day[max(by_day)]
        notes.append(f"NOTE {newest['date']} only {newest['valid']}/{newest['members']} closes, "
                     f"not published yet")
        del rows[:]
        return None, notes
    for day in sorted(d for d in by_day if d > known):
        r = by_day[day]
        reason = ("has member prices but no index close" if day.isoformat() not in with_index
                  else f"only {r['valid']}/{r['members']} closes")
        notes.append(f"NOTE {r['date']} {reason}, not published yet")
    if filled.empty:
        notes.append(f"NOTE no {INDEX_SYMBOL} index closes in the downloaded data")
    cut = next((i for i, r in enumerate(rows) if dt.date.fromisoformat(r["date"]) > known), len(rows))
    del rows[cut:]
    return known, notes


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
    ap.add_argument("--allow-stale", action="store_true",
                    help="offline/debug override: downgrade a freshness failure to a warning "
                         "(coverage and history checks still fail the run)")
    ap.add_argument("--now", default=None,
                    help="override the current time with an ISO-8601 instant (testing/diagnostics only)")
    args = ap.parse_args()

    if args.now:
        try:
            now_utc = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"ERROR: --now is not an ISO-8601 instant: {args.now!r}")
            return 2
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
        print(f"NOTE current time overridden with --now {now_utc.isoformat()}")
    else:
        now_utc = dt.datetime.now(dt.timezone.utc)

    doc = ms.load()
    if not args.no_sync:
        try:
            event = ms.sync(doc)
            ms.save(doc)
            if event:
                print("membership change recorded:", event)
        except Exception as e:  # noqa: BLE001
            print("WARNING: official list check failed, using stored membership:", e)

    expected = expected_completed_session(now_utc)
    print(f"nasdaq trading calendar {MARKET_CALENDAR}; now {now_utc.isoformat(timespec='seconds')}; "
          f"expected completed session (close + {PUBLICATION_GRACE}): "
          f"{expected.isoformat() if expected else 'UNKNOWN'}")
    if expected is None:
        print("VALIDATION FAILED, not writing output:\n  " + UNKNOWN_SESSION)
        return 1

    today = now_utc.astimezone(SOURCE_TIMEZONE).date()
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
    coverage_report(px, index_close, doc, expected)
    px, index_close, patch_source = patch_latest_from_nasdaq(px, index_close, expected, current)

    # only sessions whose close + grace has passed may influence the moving average or be published
    eligible = eligible_sessions(px, index_close, now_utc)
    px, index_close, dropped_days = drop_ineligible(px, index_close, eligible)
    if dropped_days:
        print("NOTE not eligible yet (in-progress session, non-session or unknown date), excluded: "
              + ", ".join(d.isoformat() for d in dropped_days[-10:]))

    result = compute(doc, px, index_close, start)
    rows = [r for r in result["rows"] if r["pct"] is not None]
    no_data = sorted(set(universe) - set(px.columns))

    if not rows:
        print("ERROR: no usable trading session in the downloaded data (empty rows after parsing)")
        return 1

    data_through, notes = publishable_through(rows, index_close)
    for note in notes:
        print(note)
    for line in describe_freshness(data_through, expected):
        print("DIAG", line)
    stale = freshness_problems(data_through, expected) + index_freshness_problems(index_close, expected)
    if stale:
        print("DIAG stale detail: " + "; ".join(stale))
    if data_through is None:
        detail = "\n  " + "\n  ".join(stale) if stale else ""
        print("VALIDATION FAILED, not writing output:\n  no complete trading session available" + detail)
        return 1

    latest = rows[-1]
    coverage = latest["valid"] / latest["members"]
    problems = []
    if coverage < MIN_COVERAGE_LATEST:
        problems.append(f"latest day coverage {latest['valid']}/{latest['members']} below {MIN_COVERAGE_LATEST:.0%}")
    if stale and args.allow_stale:
        print("WARNING: --allow-stale active, downgrading freshness failure: " + "; ".join(stale))
    else:
        problems += stale
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
        "latest_source": patch_source or "yahoo",
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
