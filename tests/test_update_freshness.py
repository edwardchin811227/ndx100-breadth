"""Offline tests for the trading-session freshness validation in scripts/update.py.

No network access: yfinance downloads, the Nasdaq quote endpoints and Tiingo are replaced with
fakes, membership is read from a temporary file and all output goes to a temporary directory.
Nothing here touches the real site/data files or the live membership history.

Run: python -m pytest tests -q
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update as u  # noqa: E402

TICKERS = [f"T{i:02d}" for i in range(100)]
INDEX = u.INDEX_SYMBOL
# The first "current" session the fixture prices stop at (a Wednesday); real XNYS sessions only.
CUR = dt.date(2026, 9, 23)
# The early-close session of 2026 (Black Friday, 13:00 ET).
EARLY = dt.date(2026, 11, 27)
# DST boundaries in 2026: spring forward 2026-03-08, fall back 2026-11-01.
SPRING = dt.date(2026, 3, 10)
FALL = dt.date(2026, 11, 3)


def cal():
    return xcals.get_calendar(u.MARKET_CALENDAR)


def sessions_between(start: dt.date, end: dt.date) -> list[dt.date]:
    c = cal()
    return [ts.date() for ts in c.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


def prev_session(day: dt.date) -> dt.date:
    return cal().date_to_session(pd.Timestamp(day) - dt.timedelta(days=1), direction="previous").date()


def next_session(day: dt.date) -> dt.date:
    return cal().date_to_session(pd.Timestamp(day) + dt.timedelta(days=1), direction="next").date()


def utc(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=dt.timezone.utc)


class FakeMarket:
    """Deterministic prices for a fixed ticker universe, on real XNYS sessions."""

    def __init__(self, last_trade: dt.date = CUR, tickers: list[str] | None = None, seed: int = 7):
        self.last_trade = last_trade
        self.tickers = list(tickers or TICKERS)
        self.first = last_trade - dt.timedelta(days=3 * 365 + 400)
        self.days = sessions_between(self.first, last_trade)
        rng = np.random.default_rng(seed)
        idx = np.arange(len(self.days), dtype=float)
        # slow, tiny moves: never near the 40% spike guard
        self.prices = pd.DataFrame(index=pd.DatetimeIndex(self.days, name="Date"))
        for i, t in enumerate(self.tickers):
            self.prices[t] = 100.0 + 8.0 * np.sin(idx / (37.0 + i)) + 0.002 * idx
        self.index = pd.Series(
            18000.0 + 400.0 * np.sin(idx / 211.0) + 3.0 * idx, index=self.prices.index, name=INDEX)

    def download(self, tickers, start, retry=None):
        """Stand-in for update.download: same contract, no network."""
        wanted = [t for t in tickers if t in self.prices.columns or t == INDEX]
        if not wanted:
            return pd.DataFrame(index=self.prices.index)
        df = self.prices[wanted].copy()
        df = df.loc[df.index >= pd.Timestamp(start)]
        for t in list(df.columns):
            if t != INDEX and t not in self.tickers:
                df = df.drop(columns=t)
        return df

    def index_series(self, through: dt.date | None = None) -> pd.Series:
        s = self.index if through is None else self.index.loc[: pd.Timestamp(through)]
        return s.rename(INDEX)


def write_membership(path: Path, tickers: list[str] | None = None) -> None:
    doc = {
        "start_date": "2023-01-01",
        "initial": sorted(tickers or TICKERS),
        "events": [{
            "date": "2024-01-02", "added": [], "removed": [], "source": "test",
            "ref": "test fixture", "note": "no-op event so the events key is exercised",
        }],
        "sources": {"history": "test fixture", "daily": "test fixture"},
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class Nasdaq:
    """Fake api.nasdaq.com: NDX info + the nasdaq100 list endpoint.

    Quote rows are derived from the fake market's last available close, so they never trip the
    40% spike guard by accident.
    """

    def __init__(self, day: dt.date, price: float = 20123.45, status: str = "Closed",
                 rows: list[dict] | None = None):
        self.day = day
        self.price = price
        self.status = status
        self.rows = rows

    @property
    def quote_date(self) -> str:
        return self.day.strftime("%b %d, %Y")

    def rows_near(self, market: FakeMarket) -> list[dict]:
        """One row per fixture ticker, priced from that ticker's last close (+1%)."""
        if self.rows is not None:
            return self.rows
        out = []
        for t in market.tickers:
            last = float(market.prices[t].dropna().iloc[-1])
            out.append({"symbol": t, "lastSalePrice": f"${last * 1.01:,.2f}"})
        return out

    def info(self) -> dict:
        return {"data": {
            "marketStatus": self.status,
            "primaryData": {"lastTradeTimestamp": self.quote_date,
                            "lastSalePrice": f"${self.price:,.2f}"},
        }}

    def listing(self, date: str | None = None, rows: list[dict] | None = None) -> dict:
        return {"data": {"date": date or self.quote_date,
                         "data": {"rows": self.rows if rows is None else rows}}}

    def index_history(self, market: FakeMarket, lag: int = 1) -> dict:
        """Nasdaq's NDX index table: newest first and one session behind, exactly like the real feed.

        `lag=0` models a table that already carries the newest session.
        """
        s = market.index if not lag else market.index.iloc[:-lag]
        rows = [{"date": ts.strftime("%m/%d/%Y"), "close": f"{float(v):,.2f}"}
                for ts, v in s.items()][::-1]
        return {"data": {"tradesTable": {"rows": rows}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A runnable update.py environment: temp membership, temp output, no network."""
    out = tmp_path / "site" / "data"
    out.mkdir(parents=True, exist_ok=True)
    membership_path = tmp_path / "data" / "membership.json"
    membership_path.parent.mkdir(parents=True, exist_ok=True)
    write_membership(membership_path)

    monkeypatch.setattr(u.ms, "MEMBERSHIP_PATH", membership_path)
    monkeypatch.setattr(u, "OUT_DIR", out)
    monkeypatch.setattr(u.ms, "sync", lambda doc: None)
    monkeypatch.setattr(u.ms, "save", lambda doc: None)
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)

    class Env:
        path = membership_path
        out_dir = out
        market = FakeMarket()
        nasdaq: Nasdaq | None = None
        http = None
        yahoo_index = True  # False simulates Yahoo returning no ^NDX series at all

        def run(self, now: dt.datetime, extra: list[str] | None = None) -> int:
            def fake_download(tickers, start, retry=None):
                market = self.market
                if list(tickers) == [INDEX]:
                    if not self.yahoo_index:
                        return pd.DataFrame(index=market.prices.index)
                    return pd.DataFrame({INDEX: market.index_series()})
                return market.download(tickers, start, retry)

            monkeypatch.setattr(u, "download", fake_download)

            def fake_http(url, params=None, headers=None, tries=6):
                fake = self.nasdaq
                market = self.market
                if "quote/NDX/info" in url:
                    return FakeResponse(fake.info())
                if "quote/NDX/historical" in url:
                    return FakeResponse(fake.index_history(market))
                if "list-type/nasdaq100" in url:
                    return FakeResponse(fake.listing(rows=fake.rows_near(market)))
                raise AssertionError(f"unexpected HTTP request in test: {url}")

            monkeypatch.setattr(u.ms, "http_get", self.http or fake_http)

            argv = ["--no-sync", "--now", now.isoformat()] + (extra or [])
            monkeypatch.setattr(sys, "argv", ["update.py"] + argv)
            return u.main()

        def no_http(self):
            """Simulate a Nasdaq endpoint that raises (unavailable / malformed responses)."""

            def boom(url, *a, **k):
                raise RuntimeError("HTTP 503")

            self.http = boom
            return self

        def payload(self) -> dict:
            return json.loads((self.out_dir / "breadth.json").read_text(encoding="utf-8"))

        def seed_previous(self, latest: str = "2020-01-02") -> str:
            """An existing published file that a failed run must not overwrite."""
            body = json.dumps({"latest_date": latest, "series": {"date": [latest]},
                               "generated_at": "2020-01-02T00:00:00+00:00"})
            (self.out_dir / "breadth.json").write_text(body, encoding="utf-8")
            (self.out_dir / "breadth.csv").write_text("date,pct\n", encoding="utf-8")
            return body

    return Env()


# ------------------------------------------------------------------ pure session helpers
@pytest.mark.parametrize("now, expected_date", [
    # normal session close 20:00 UTC in EDT
    (utc(CUR, 19, 0), None),                    # before the close
    (utc(CUR, 21, 30), prev_session(CUR)),      # after the close, inside the 2h grace
    (utc(CUR, 22, 0), CUR),                     # exactly close + grace
    (utc(CUR, 23, 34), CUR),                    # the incident inspection time
])
def test_expected_session_normal_day(now, expected_date):
    expected = u.expected_completed_session(now)
    assert expected == (expected_date or prev_session(CUR))


def test_expected_session_weekend_holiday_and_early_close():
    saturday = utc(dt.date(2026, 9, 26), 12, 0)
    sunday = utc(dt.date(2026, 9, 27), 23, 0)
    assert u.expected_completed_session(saturday) == dt.date(2026, 9, 25)
    assert u.expected_completed_session(sunday) == dt.date(2026, 9, 25)

    # 2026-11-26 is Thanksgiving: no session, so the previous session is expected
    assert u.expected_completed_session(utc(dt.date(2026, 11, 26), 22, 0)) == dt.date(2026, 11, 25)
    # early close 2026-11-27 13:00 ET -> 18:00 UTC close, 20:00 UTC with grace
    assert u.expected_completed_session(utc(EARLY, 19, 59)) == dt.date(2026, 11, 25)
    assert u.expected_completed_session(utc(EARLY, 20, 0)) == EARLY


def test_expected_session_respects_dst():
    # the same 16:00 ET close is 20:00 UTC in EDT and 21:00 UTC in EST
    assert u._session_close(SPRING) == pd.Timestamp(utc(SPRING, 20, 0))
    assert u._session_close(FALL) == pd.Timestamp(utc(FALL, 21, 0))
    # spring forward: close + grace lands at 22:00 UTC
    assert u.expected_completed_session(utc(SPRING, 21, 59)) == prev_session(SPRING)
    assert u.expected_completed_session(utc(SPRING, 22, 0)) == SPRING
    # fall back: close + grace lands at 23:00 UTC
    assert u.expected_completed_session(utc(FALL, 22, 59)) == prev_session(FALL)
    assert u.expected_completed_session(utc(FALL, 23, 0)) == FALL


def test_early_close_is_shorter_than_a_normal_day():
    # both 2026-11-25 and Black Friday 2026-11-27 are EST, so the time-of-day difference is exact
    def minutes_after_open(day: dt.date) -> float:
        close = u._session_close(day)
        open_ = cal().session_open(pd.Timestamp(day))
        return (close - open_) / pd.Timedelta(minutes=1)

    assert minutes_after_open(dt.date(2026, 11, 25)) == 390.0  # 09:30-16:00 ET
    assert minutes_after_open(EARLY) == 210.0                  # 09:30-13:00 ET


def test_freshness_problems_messages():
    assert u.freshness_problems(CUR, CUR) == []
    assert u.freshness_problems(next_session(CUR), CUR) == []
    problems = u.freshness_problems(dt.date(2026, 9, 21), CUR)
    assert len(problems) == 1 and "2026-09-21" in problems[0] and "2026-09-23" in problems[0]
    assert "no usable trading session" in u.freshness_problems(None, CUR)[0]
    # an unknown expected session fails closed, it is never treated as "nothing due"
    assert u.freshness_problems(CUR, None) == [u.UNKNOWN_SESSION]
    assert u.freshness_problems(None, None) == [u.UNKNOWN_SESSION]
    idx = pd.Series({pd.Timestamp("2026-09-21"): 1.0})
    assert "index data only reaches 2026-09-21" in u.index_freshness_problems(idx, CUR)[0]
    assert u.index_freshness_problems(pd.Series(dtype=float), CUR)
    assert u.index_freshness_problems(pd.Series(dtype=float), None) == []


def test_calendar_failure_is_fail_closed(monkeypatch):
    """A calendar that cannot answer must not make the data look current."""
    calls = {"n": 0}

    def broken_close(day):
        calls["n"] += 1
        return None

    monkeypatch.setattr(u, "_session_close", broken_close)
    assert u.expected_completed_session(utc(CUR, 23, 34)) is None
    assert calls["n"] >= 1


def test_expected_session_outside_calendar_range_is_unknown():
    cal_ = cal()
    future = cal_.last_session.date() + dt.timedelta(days=30)
    past = cal_.first_session.date() - dt.timedelta(days=400)
    assert u.expected_completed_session(utc(future, 12, 0)) is None
    assert u.expected_completed_session(utc(past, 12, 0)) is None


def test_eligible_sessions_and_drop_ineligible():
    now = utc(CUR, 23, 34)
    px = pd.DataFrame(
        {t: [1.0, 1.0, 1.0] for t in ["A"]},
        index=pd.DatetimeIndex([dt.date(2026, 9, 22), dt.date(2026, 9, 23), dt.date(2026, 9, 24)]))
    index = pd.Series([2.0, 2.0, 2.0],
                      index=pd.DatetimeIndex([dt.date(2026, 9, 22), dt.date(2026, 9, 23),
                                              dt.date(2026, 9, 24)]))
    eligible = u.eligible_sessions(px, index, now)
    assert eligible == {dt.date(2026, 9, 22), dt.date(2026, 9, 23)}
    px2, idx2, dropped = u.drop_ineligible(px, index, eligible)
    assert dropped == [dt.date(2026, 9, 24)]
    assert list(px2.index.date) == [dt.date(2026, 9, 22), dt.date(2026, 9, 23)]
    assert list(idx2.index.date) == [dt.date(2026, 9, 22), dt.date(2026, 9, 23)]


def test_non_session_dates_are_not_eligible():
    now = utc(dt.date(2026, 9, 26), 23, 0)  # Saturday
    weekend = pd.DataFrame({"T00": [1.0]}, index=pd.DatetimeIndex([dt.date(2026, 9, 26)]))
    assert u.eligible_sessions(weekend, pd.Series(dtype=float), now) == set()


def test_publishable_through_drops_incomplete_sessions():
    index = pd.Series({pd.Timestamp(d): 100.0 for d in [dt.date(2026, 9, 21), dt.date(2026, 9, 22),
                                                        dt.date(2026, 9, 23)]})
    rows = [
        {"date": "2026-09-21", "valid": 100, "members": 100},
        {"date": "2026-09-22", "valid": 100, "members": 100},
        {"date": "2026-09-23", "valid": 40, "members": 100},
    ]
    through, notes = u.publishable_through(rows, index)
    assert through == dt.date(2026, 9, 22)
    assert [r["date"] for r in rows] == ["2026-09-21", "2026-09-22"]
    assert any("40/100" in n for n in notes)

    # an empty result when nothing is fully covered
    rows = [{"date": "2026-09-23", "valid": 40, "members": 100}]
    through, notes = u.publishable_through(rows, index)
    assert through is None and rows == [] and any("40/100" in n for n in notes)

    # member prices newer than the index are not published
    index = pd.Series({pd.Timestamp("2026-09-21"): 100.0})
    rows = [{"date": "2026-09-21", "valid": 100, "members": 100},
            {"date": "2026-09-22", "valid": 100, "members": 100}]
    through, notes = u.publishable_through(rows, index)
    assert through == dt.date(2026, 9, 21)
    assert any("no index close" in n for n in notes)

    # an index-only session (members missing) rolls back to the last complete member session
    index = pd.Series({pd.Timestamp(d): 100.0 for d in [dt.date(2026, 9, 22), dt.date(2026, 9, 23)]})
    rows = [{"date": "2026-09-22", "valid": 100, "members": 100},
            {"date": "2026-09-23", "valid": 30, "members": 100}]
    through, notes = u.publishable_through(rows, index)
    assert through == dt.date(2026, 9, 22)
    assert any("30/100" in n for n in notes)

    # no rows at all
    assert u.publishable_through([], index) == (None, [])


# ------------------------------------------------------------------ end-to-end pipeline
def expected_pct(market: FakeMarket, day: dt.date, window: int = 50,
                 skip: set[str] | None = None) -> tuple[float, int, int]:
    """Independent recomputation of the published percentage for `day`.

    This is the "member has a prior close" view used by `compute`; when the fixture has dropped a
    session entirely, the publication side is checked separately.
    """
    above = 0
    valid = 0
    for t in TICKERS:
        if skip and t in skip:
            continue
        s = market.prices[t].dropna()
        s = s.loc[: pd.Timestamp(day)]
        if len(s) < window:
            continue
        sma = s.rolling(window).mean().iloc[-1]
        valid += 1
        if s.iloc[-1] > sma:
            above += 1
    return round(100 * above / valid, 2), above, valid


def members_with_value(market: FakeMarket, day: dt.date) -> int:
    """How many fixture members have a price on `day` (the Yahoo-side coverage)."""
    ts = pd.Timestamp(day)
    if ts not in market.prices.index:
        return 0
    return int(market.prices.loc[ts].notna().sum())


def test_normal_trading_day_publishes_and_keeps_50d_ma(env):
    assert env.run(utc(CUR, 23, 34)) == 0
    payload = env.payload()
    assert payload["latest_date"] == CUR.isoformat()
    assert payload["latest_source"] == "yahoo"
    assert payload["ma_days"] == 50
    assert payload["series"]["date"][-1] == CUR.isoformat()

    latest = payload["series"]
    pct, above, valid = expected_pct(env.market, CUR)
    assert latest["pct"][-1] == pct
    assert latest["above"][-1] == above
    assert latest["valid"][-1] == valid == 100
    assert latest["members"][-1] == 100
    assert payload["no_price_data"] == []
    # the 50-day MA needs 50 prior sessions, so ~3 years of rows survive the history check
    assert len(latest["date"]) > 250 * 3 * 0.95
    assert (env.out_dir / "breadth.csv").exists()


def test_before_grace_does_not_require_the_open_session(env, capsys):
    """Yahoo has not published 2026-09-23 yet and the 2h grace window has not passed: 09-22 is fine."""
    env.market = FakeMarket(last_trade=prev_session(CUR))
    assert env.run(utc(CUR, 21, 30)) == 0
    assert env.payload()["latest_date"] == prev_session(CUR).isoformat()
    out = capsys.readouterr().out
    assert f"expected completed session: {prev_session(CUR)}" in out
    assert "DIAG yahoo index latest date:" in out
    assert "VALIDATION FAILED" not in out


def test_after_grace_requires_the_completed_session(env, capsys):
    """The same download 30 minutes later is stale, because close + 2h has passed."""
    previous = env.seed_previous()
    env.market = FakeMarket(last_trade=prev_session(CUR))
    assert env.run(utc(CUR, 22, 30)) == 1
    out = capsys.readouterr().out
    assert "latest data 2026-09-22 is older than the expected completed session 2026-09-23" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_after_grace_with_complete_session_publishes(env):
    assert env.run(utc(CUR, 22, 30)) == 0
    assert env.payload()["latest_date"] == CUR.isoformat()


def test_weekend_uses_friday_session(env):
    env.market = FakeMarket(last_trade=dt.date(2026, 9, 25))
    assert env.run(utc(dt.date(2026, 9, 26), 12, 0)) == 0
    assert env.payload()["latest_date"] == dt.date(2026, 9, 25).isoformat()


def test_us_holiday_uses_previous_session(env):
    # Thanksgiving 2026-11-26 is not a session, so 2026-11-25 is the expected one
    env.market = FakeMarket(last_trade=dt.date(2026, 11, 25))
    assert env.run(utc(dt.date(2026, 11, 26), 22, 0)) == 0
    assert env.payload()["latest_date"] == "2026-11-25"


def test_early_close_day(env):
    env.market = FakeMarket(last_trade=dt.date(2026, 11, 25))
    # 20:00 UTC == 13:00 ET early close + 2h grace; before that only 11-25 is required
    assert env.run(utc(EARLY, 19, 50)) == 0
    assert env.payload()["latest_date"] == dt.date(2026, 11, 25).isoformat()
    env.market = FakeMarket(last_trade=EARLY)
    assert env.run(utc(EARLY, 20, 0)) == 0
    assert env.payload()["latest_date"] == EARLY.isoformat()


def test_dst_spring_and_fall(env):
    env.market = FakeMarket(last_trade=prev_session(SPRING))
    assert env.run(utc(SPRING, 21, 59)) == 0
    assert env.payload()["latest_date"] == prev_session(SPRING).isoformat()
    env.market = FakeMarket(last_trade=SPRING)
    assert env.run(utc(SPRING, 22, 0)) == 0
    assert env.payload()["latest_date"] == SPRING.isoformat()

    env.market = FakeMarket(last_trade=prev_session(FALL))
    assert env.run(utc(FALL, 22, 59)) == 0
    assert env.payload()["latest_date"] == prev_session(FALL).isoformat()
    env.market = FakeMarket(last_trade=FALL)
    assert env.run(utc(FALL, 23, 0)) == 0
    assert env.payload()["latest_date"] == FALL.isoformat()


def test_stale_index_fails_and_preserves_output(env, capsys):
    previous = env.seed_previous()
    env.market = FakeMarket(last_trade=prev_session(CUR))
    # Yahoo index reaches 2026-09-22 while the expected completed session is 2026-09-23
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "VALIDATION FAILED, not writing output" in out
    assert "index data only reaches 2026-09-22" in out
    assert "latest data 2026-09-22 is older than the expected completed session 2026-09-23" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_stale_member_data_fails_and_preserves_output(env, capsys):
    previous = env.seed_previous()
    market = FakeMarket()
    # every member is missing the expected session, but the index has it
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    # the all-empty session never becomes a row; the run fails with a stale diagnostic instead
    assert "DIAG membership price coverage 2026-09-23: date absent from the downloaded prices" in out
    assert "latest data 2026-09-22 is older than the expected completed session 2026-09-23" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_partial_member_data_fails_and_preserves_output(env, capsys):
    previous = env.seed_previous()
    csv_before = (env.out_dir / "breadth.csv").read_text(encoding="utf-8")
    market = FakeMarket()
    market.prices.loc[pd.Timestamp(CUR), TICKERS[:50]] = np.nan
    env.market = market
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "only 50/100 closes, not published yet" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous
    assert (env.out_dir / "breadth.csv").read_text(encoding="utf-8") == csv_before


def test_intraday_data_is_never_published_or_averaged(env, capsys):
    """At 2026-09-24T15:00Z the market is still open: a Sep 24 bar must not be published or counted."""
    previous = env.seed_previous()
    market = FakeMarket(last_trade=dt.date(2026, 9, 24))
    env.market = market
    assert env.run(utc(dt.date(2026, 9, 24), 15, 0)) == 0
    out = capsys.readouterr().out
    assert "not eligible yet" in out and "2026-09-24" in out
    payload = env.payload()
    assert payload["latest_date"] == CUR.isoformat()  # 09-23, not the in-progress 09-24
    assert payload["series"]["date"][-1] == CUR.isoformat()

    # and with --allow-stale the in-progress session is still excluded
    assert env.run(utc(dt.date(2026, 9, 24), 15, 0), extra=["--allow-stale"]) == 0
    assert env.payload()["series"]["date"][-1] == CUR.isoformat()


def test_intraday_only_data_fails(env, capsys):
    """If nothing at all has completed, the run fails instead of publishing the open session."""
    previous = env.seed_previous()
    market = FakeMarket(last_trade=dt.date(2026, 9, 24))
    market.prices = market.prices.loc[[pd.Timestamp("2026-09-24")]]
    market.index = market.index.loc[[pd.Timestamp("2026-09-24")]]
    env.market = market
    assert env.run(utc(dt.date(2026, 9, 24), 15, 0)) == 1
    out = capsys.readouterr().out
    assert "not eligible yet" in out
    assert "ERROR: no usable trading session in the downloaded data" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_empty_rows_fail_cleanly(env, capsys):
    previous = env.seed_previous()
    market = FakeMarket()
    market.prices = market.prices.iloc[0:0]
    market.index = market.index.iloc[0:0]
    env.market = market
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "no usable trading session" in out or "empty rows" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_allow_stale_downgrades_freshness_only(env, capsys):
    previous = env.seed_previous()
    env.market = FakeMarket(last_trade=prev_session(CUR))
    assert env.run(utc(CUR, 23, 34), extra=["--allow-stale"]) == 0
    out = capsys.readouterr().out
    assert "--allow-stale active, downgrading freshness failure" in out
    assert "index data only reaches 2026-09-22" in out
    assert env.payload()["latest_date"] == prev_session(CUR).isoformat()

    # the override never fabricates a session: a half-filled day is still dropped
    second = env.seed_previous()
    capsys.readouterr()
    market = FakeMarket()
    market.prices.loc[pd.Timestamp(CUR), TICKERS[:50]] = np.nan
    env.market = market
    env.nasdaq = None
    assert env.run(utc(CUR, 23, 34), extra=["--allow-stale"]) == 0
    out = capsys.readouterr().out
    assert "only 50/100 closes, not published yet" in out
    assert env.payload()["latest_date"] == prev_session(CUR).isoformat()
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") != second


def test_history_check_is_still_enforced(env, capsys):
    previous = env.seed_previous()
    env.market = FakeMarket(last_trade=CUR)
    env.market.prices = env.market.prices.iloc[-400:]
    env.market.index = env.market.index.iloc[-400:]
    assert env.run(utc(CUR, 23, 34)) == 1
    assert "trading days computed" in capsys.readouterr().out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_no_change_run_keeps_generated_at(env, capsys):
    assert env.run(utc(CUR, 23, 34)) == 0
    first = env.payload()["generated_at"]
    assert env.run(utc(CUR, 23, 34)) == 0
    assert "no change (latest 2026-09-23)" in capsys.readouterr().out
    assert env.payload()["generated_at"] == first


# ------------------------------------------------------------------ Nasdaq fallback
def bar_without(market: FakeMarket, day: dt.date, *, index: bool = True,
                members: bool = True) -> FakeMarket:
    """Remove a day's bar from the fixture, on the index side and/or the member side."""
    if members:
        market.prices = market.prices.drop(index=pd.Timestamp(day))
    if index:
        market.index = market.index.drop(index=pd.Timestamp(day))
    return market


def test_nasdaq_fallback_fills_missing_index_and_members(env, capsys):
    """Both sides are missing the expected session, so both are filled from the quote feed."""
    market = FakeMarket(last_trade=CUR)
    # drop the 2026-09-23 member bar and the index bar, so both sides need filling
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    market.index = market.index.drop(index=pd.Timestamp(CUR))
    env.market = market
    env.nasdaq = Nasdaq(CUR)
    assert env.run(utc(CUR, 23, 34)) == 0
    payload = env.payload()
    assert payload["latest_date"] == CUR.isoformat()
    # site/app.js renders its disclaimer only for this exact value
    assert payload["latest_source"] == "nasdaq-quote"
    assert payload["series"]["ndx"][-1] == round(env.nasdaq.price, 2)
    out = capsys.readouterr().out
    assert "unverified last-sale fallback" in out
    assert "filled the 2026-09-23 index close" in out


def test_nasdaq_fallback_fills_only_members_when_index_is_current(env, capsys):
    """INDEX is current but the members lag: only the member side may be repaired."""
    market = FakeMarket(last_trade=CUR)
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    env.nasdaq = Nasdaq(CUR)
    assert env.run(utc(CUR, 23, 34)) == 0
    out = capsys.readouterr().out
    assert "100 of 100 current members missing" in out
    assert "plus the index close" not in out
    assert env.payload()["latest_date"] == CUR.isoformat()
    assert env.payload()["latest_source"] == "nasdaq-quote"


def test_nasdaq_fallback_fills_only_index_when_members_are_current(env, capsys):
    """The reverse case: members already reach the session, only the index value is missing."""
    market = FakeMarket(last_trade=CUR)
    market.index = market.index.drop(index=pd.Timestamp(CUR))
    env.market = market
    env.nasdaq = Nasdaq(CUR)
    assert env.run(utc(CUR, 23, 34)) == 0
    out = capsys.readouterr().out
    assert "only the index close needed filling" in out
    assert env.payload()["latest_date"] == CUR.isoformat()
    assert env.payload()["latest_source"] == "nasdaq-quote"
    assert env.payload()["series"]["ndx"][-1] == round(env.nasdaq.price, 2)


def test_nasdaq_fallback_preserves_existing_yahoo_values(env, capsys):
    """A value Yahoo already published must not be replaced by the quote feed."""
    previous = env.seed_previous()
    market = FakeMarket(last_trade=CUR)
    market.prices.loc[pd.Timestamp(CUR), TICKERS[:50]] = np.nan
    env.market = market
    rows = [{"symbol": t, "lastSalePrice": "$1.00"} for t in TICKERS]  # would trip the spike guard
    env.nasdaq = Nasdaq(CUR, rows=rows)
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "skipped (>40% move): [" in out  # every $1.00 quote is a wild move and is rejected
    assert "insufficient patch coverage (0% of 50 missing member closes" in out
    assert "VALIDATION FAILED" in out
    # nothing was published at all: the previously published file is byte-identical, so no Yahoo
    # close was overwritten and no fallback source was recorded
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_nasdaq_fallback_fills_only_missing_cells(env, monkeypatch):
    """The fallback completes a session: cells Yahoo already filled keep their exact value."""
    market = FakeMarket(last_trade=CUR)
    day = pd.Timestamp(CUR)
    market.prices.loc[day, TICKERS[:50]] = np.nan
    published = market.prices.loc[day, TICKERS[50:]].astype(float).copy()
    nasdaq = Nasdaq(CUR)

    def fake_http(url, params=None, headers=None, tries=6):
        if "quote/NDX/info" in url:
            return FakeResponse(nasdaq.info())
        return FakeResponse(nasdaq.listing(rows=nasdaq.rows_near(market)))

    monkeypatch.setattr(u.ms, "http_get", fake_http)
    patched_px, patched_index, source = u.patch_latest_from_nasdaq(
        market.download(TICKERS, market.first), market.index_series(), CUR, set(TICKERS))

    assert source == "nasdaq-quote"
    # members that already had a Yahoo close keep that exact value
    pd.testing.assert_series_equal(patched_px.loc[day, TICKERS[50:]].astype(float), published)
    # only the missing half is completed, and the index is left alone because it is already current
    assert patched_px.loc[day, TICKERS[:50]].notna().all()
    assert day in patched_index.index
    assert patched_px.index.is_monotonic_increasing


def test_nasdaq_fallback_skipped_when_market_not_closed(env, capsys):
    env.market = FakeMarket(last_trade=prev_session(CUR))
    env.nasdaq = Nasdaq(CUR, status="Open")
    assert env.run(utc(CUR, 21, 0)) == 0
    assert env.payload()["latest_date"] == prev_session(CUR).isoformat()
    assert "market not closed, Nasdaq reports 'Open'" in capsys.readouterr().out


def test_nasdaq_fallback_skipped_on_source_date_mismatch(env, capsys):
    env.market = FakeMarket(last_trade=prev_session(CUR))
    env.nasdaq = Nasdaq(CUR)
    mismatch = prev_session(CUR).strftime("%b %d, %Y")
    env.nasdaq.listing = lambda date=None, rows=None: {"data": {"date": mismatch, "data": {"rows": []}}}
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "source dates mismatch" in out
    assert "VALIDATION FAILED" in out


def test_nasdaq_fallback_nothing_to_repair(env, capsys):
    """Nothing is missing on the expected session, so the feed is not consulted for values."""
    env.market = FakeMarket(last_trade=CUR)
    env.nasdaq = Nasdaq(CUR)
    assert env.run(utc(CUR, 23, 34)) == 0
    out = capsys.readouterr().out
    print(out)
    assert "nothing to repair" in out
    assert env.payload()["latest_source"] == "yahoo"


def test_nasdaq_fallback_rejects_insufficient_patch_coverage(env, capsys):
    previous = env.seed_previous()
    market = FakeMarket(last_trade=CUR)
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    rows = [{"symbol": t, "lastSalePrice": "N/A"} for t in TICKERS]
    for t in TICKERS[:50]:
        last = float(market.prices[t].dropna().iloc[-1])
        rows[TICKERS.index(t)] = {"symbol": t, "lastSalePrice": f"${last * 1.01:,.2f}"}
    env.nasdaq = Nasdaq(CUR, rows=rows)
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "insufficient patch coverage (50% of 100 missing member closes" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_nasdaq_fallback_unavailable_is_not_fatal(env, capsys):
    """A dead Nasdaq endpoint must not crash the run, and cannot rescue a stale session."""
    previous = env.seed_previous()
    env.market = FakeMarket(last_trade=prev_session(CUR))
    env.no_http()
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "nasdaq fallback: unavailable: HTTP 503" in out
    assert "VALIDATION FAILED" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def test_nasdaq_fallback_malformed_payload_is_not_fatal(env, capsys):
    env.market = FakeMarket(last_trade=prev_session(CUR))
    env.nasdaq = Nasdaq(CUR, rows=[{"symbol": "T00"}, {"symbol": ""}])
    env.nasdaq.info = lambda: {"data": {"marketStatus": "Closed", "primaryData": {}}}
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "no usable last-trade date" in out
    assert "VALIDATION FAILED" in out


@pytest.mark.parametrize("bad, label", [
    ("N/A", "non-numeric"),
    ("", "empty"),
    ("-$3.00", "negative"),
    ("$0", "zero"),
    ("$.", "unparseable"),
    ("NaN", "nan"),
    ("inf", "infinity"),
    (None, "missing"),
])
def test_nasdaq_fallback_rejects_bad_quotes(env, capsys, bad, label):
    """Malformed rows must be counted and ignored, never written as NaN/0/negative prices."""
    market = FakeMarket(last_trade=CUR)
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    # quotes taken verbatim from the fixture's previous close, so the patched bar equals the Yahoo
    # close the source would have published and the percentage is directly checkable
    rows = [{"symbol": t, "lastSalePrice": f"${float(market.prices[t].dropna().iloc[-1]):.6f}"}
            for t in TICKERS]
    rows[TICKERS.index("T00")]["lastSalePrice"] = bad
    env.nasdaq = Nasdaq(CUR, rows=rows)
    assert env.run(utc(CUR, 23, 34)) == 0
    out = capsys.readouterr().out
    assert "ignored 1 malformed quote row(s)" in out
    assert env.payload()["series"]["valid"][-1] == len(TICKERS) - 1
    assert env.payload()["series"]["pct"][-1] == expected_pct(env.market, CUR, skip={"T00"})[0]


def test_nasdaq_fallback_uses_valid_quotes_verbatim(env, capsys):
    """Every usable quote is written as the bar's close; unusable ones are never written."""
    market = FakeMarket(last_trade=CUR)
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    last = {t: float(market.prices[t].dropna().iloc[-1]) for t in TICKERS}
    rows = [{"symbol": t, "lastSalePrice": f"${last[t] * 1.02:,.4f}"} for t in TICKERS]
    rows[TICKERS.index("T00")]["lastSalePrice"] = "N/A"
    rows[TICKERS.index("T01")]["lastSalePrice"] = "NaN"
    env.nasdaq = Nasdaq(CUR, rows=rows)
    assert env.run(utc(CUR, 23, 34)) == 0
    assert env.payload()["series"]["valid"][-1] == len(TICKERS) - 2
    assert "ignored 2 malformed quote row(s)" in capsys.readouterr().out


def test_nasdaq_fallback_handles_broken_rows_container(env, capsys):
    """rows that are not a list must be rejected with a diagnostic rather than raising."""
    market = FakeMarket(last_trade=CUR)
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    env.nasdaq = Nasdaq(CUR)
    env.nasdaq.listing = lambda date=None, rows=None: {"data": {"date": env.nasdaq.quote_date,
                                                                "data": {"rows": {"oops": 1}}}}
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "ignored 1 malformed quote row(s)" in out
    assert "insufficient patch coverage" in out
    assert "VALIDATION FAILED" in out


def test_nasdaq_fallback_spike_guard(env, capsys):
    market = FakeMarket(last_trade=CUR)
    market.prices = market.prices.drop(index=pd.Timestamp(CUR))
    env.market = market
    rows = Nasdaq(CUR).rows_near(market)
    rows[TICKERS.index("T00")]["lastSalePrice"] = "$100000.00"  # far above the spike guard
    env.nasdaq = Nasdaq(CUR, rows=rows)
    assert env.run(utc(CUR, 23, 34)) == 0
    out = capsys.readouterr().out
    assert "skipped (>40% move): ['T00']" in out
    assert env.payload()["latest_date"] == CUR.isoformat()


# ------------------------------------------------------------------ diagnostics
def test_diagnostics_are_logged(env, capsys):
    env.run(utc(CUR, 23, 34))
    out = capsys.readouterr().out
    assert "DIAG yahoo index latest date: 2026-09-23" in out
    assert "DIAG membership price coverage 2026-09-23: 100/100 (100%)" in out
    assert "expected completed session: 2026-09-23" in out
    assert f"nasdaq trading calendar {u.MARKET_CALENDAR}" in out
    assert "DIAG nasdaq fallback:" in out


# ------------------------------------------------------------------ review regressions
def test_calendar_unknown_fails_closed_end_to_end(env, capsys, monkeypatch):
    """Review item 2: an unusable calendar must fail the whole run and write nothing, even with
    --allow-stale; the permissive "6 calendar days" behaviour must not come back."""
    previous = env.seed_previous()
    csv_before = (env.out_dir / "breadth.csv").read_text(encoding="utf-8")
    monkeypatch.setattr(u, "_session_close", lambda day: None)
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "expected completed session (close + 2:00:00): UNKNOWN" in out
    assert "expected trading session could not be established" in out
    assert "VALIDATION FAILED, not writing output" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous
    assert (env.out_dir / "breadth.csv").read_text(encoding="utf-8") == csv_before

    # the override must not bypass a fail-closed calendar either
    assert env.run(utc(CUR, 23, 34), extra=["--allow-stale"]) == 1
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous
    assert (env.out_dir / "breadth.csv").read_text(encoding="utf-8") == csv_before


def test_grace_window_publishes_the_expected_prior_session(env, capsys):
    """Review item 6: inside 2026-09-23's 2h grace the expected session is 09-22, so a partially
    filled in-progress 09-23 bar must be ignored rather than blocking the valid 09-22 row."""
    market = FakeMarket(last_trade=CUR)
    market.prices.loc[pd.Timestamp(CUR), TICKERS[:50]] = np.nan  # half-published, still in progress
    env.market = market
    assert env.run(utc(CUR, 21, 30)) == 0
    out = capsys.readouterr().out
    assert "expected completed session: 2026-09-22" in out
    assert "not eligible yet" in out and "2026-09-23" in out
    payload = env.payload()
    assert payload["latest_date"] == prev_session(CUR).isoformat()
    assert payload["series"]["date"][-1] == prev_session(CUR).isoformat()
    assert payload["latest_source"] == "yahoo"


def test_allow_stale_never_publishes_a_below_floor_row(env, capsys):
    """Review item 6: --allow-stale downgrades freshness only; an 89% row is still not published."""
    market = FakeMarket(last_trade=CUR)
    market.prices.loc[pd.Timestamp(CUR), TICKERS[:11]] = np.nan  # 89/100 members
    env.market = market
    assert env.run(utc(CUR, 23, 34), extra=["--allow-stale"]) == 0
    out = capsys.readouterr().out
    assert "only 89/100 closes, not published yet" in out
    assert env.payload()["latest_date"] == prev_session(CUR).isoformat()
    assert "--allow-stale active, downgrading freshness failure" in out


def test_nasdaq_fallback_does_not_write_a_partially_quoted_session(env, capsys):
    """Review item 3/6: a feed that covers under 90% of the missing members is not written at all.

    Yahoo published 50 of 100 closes and the feed can only supply 44 of the 50 that are missing.
    Filling those 44 would lift the day to 94/100 and let it publish, so the guard has to refuse the
    partial write instead of publishing a session the feed could not fully vouch for.
    """
    previous = env.seed_previous()
    market = FakeMarket(last_trade=CUR)
    market.prices.loc[pd.Timestamp(CUR), TICKERS[:50]] = np.nan
    env.market = market
    good = Nasdaq(CUR).rows_near(market)
    rows = [{"symbol": t, "lastSalePrice": "N/A"} for t in TICKERS]
    for t in TICKERS[:44]:  # 44 of the 50 missing members: 88%, just under the floor
        rows[TICKERS.index(t)] = good[TICKERS.index(t)]
    env.nasdaq = Nasdaq(CUR, rows=rows)
    assert env.run(utc(CUR, 23, 34)) == 1
    out = capsys.readouterr().out
    assert "insufficient patch coverage (88% of 50 missing member closes" in out
    assert "only 50/100 closes, not published yet" in out
    assert (env.out_dir / "breadth.json").read_text(encoding="utf-8") == previous


def _tiingo_rows(market: FakeMarket, ticker: str) -> list[dict]:
    """One Tiingo history row per fixture session, adjusted close equal to the fixture close."""
    return [{"date": ts.date().isoformat(), "adjClose": float(market.prices[ticker].loc[ts])}
            for ts in market.prices.index]


def _tiingo_get(market: FakeMarket, tickers: list[str], *, quota_after: int | None = None):
    """A Tiingo stand-in: answers per-symbol history URLs, optionally quota-limiting after N."""
    seen: list[str] = []

    def fake_get(url, params=None, headers=None, tries=6):
        assert "api.tiingo.com" in url, url
        ticker = url.rsplit("/", 2)[-2]
        if quota_after is not None and len(seen) >= quota_after:
            raise RuntimeError("GET https://api.tiingo.com/... failed: HTTP 429 Too Many Requests")
        seen.append(ticker)
        assert ticker in tickers, ticker
        return FakeResponse(_tiingo_rows(market, ticker))

    return fake_get, seen


def test_tiingo_fallback_has_no_symbol_cap(env, monkeypatch, capsys):
    """A whole-batch Yahoo outage must be fully recoverable: no 20-symbol ceiling any more."""
    monkeypatch.setattr(u, "TIINGO_PAUSE_SECONDS", 0)
    monkeypatch.setenv("TIINGO_API_KEY", "test-token")
    market = env.market
    doc = json.loads(env.path.read_text(encoding="utf-8"))
    missing = TICKERS[:25]
    fake_get, seen = _tiingo_get(market, missing)
    monkeypatch.setattr(u.ms, "http_get", fake_get)

    px, filled = u.fill_from_tiingo(pd.DataFrame(index=market.prices.index), doc, missing, market.first)

    out = capsys.readouterr().out
    assert len(filled) == 25
    assert len(seen) == 25  # 25 requests: the loop is not capped at 20 any more
    assert all(t in px.columns for t in missing)
    assert "Tiingo fallback: 25 symbol(s) missing from Yahoo; requesting all of them" in out
    assert "still missing" not in out


def test_tiingo_fallback_stops_when_quota_is_exhausted(env, monkeypatch, capsys):
    """Quota exhaustion stops the loop and is reported, keeping whatever was already filled."""
    monkeypatch.setattr(u, "TIINGO_PAUSE_SECONDS", 0)
    monkeypatch.setenv("TIINGO_API_KEY", "test-token")
    market = env.market
    doc = json.loads(env.path.read_text(encoding="utf-8"))
    universe = TICKERS[:30]
    fake_get, seen = _tiingo_get(market, universe, quota_after=5)
    monkeypatch.setattr(u.ms, "http_get", fake_get)

    px, filled = u.fill_from_tiingo(pd.DataFrame(index=market.prices.index), doc, universe,
                                    market.first)

    out = capsys.readouterr().out
    assert len(filled) == 5
    assert len(seen) == 5  # nothing was requested after the quota error
    assert "Tiingo quota exhausted after 5 fill(s); 25 symbol(s) still missing" in out
    assert all(t in px.columns for t in filled)


def test_index_fallback_publishes_when_yahoo_returns_no_index(env, capsys):
    """The 2026-09-25 incident: Yahoo throttled ^NDX and the run aborted with `index data
    unavailable`. Nasdaq's index table now carries the history and the existing quote fallback
    completes the newest session, so the day can publish after all."""
    env.nasdaq = Nasdaq(CUR, price=20123.45)
    env.yahoo_index = False
    assert env.run(utc(CUR, 23, 34)) == 0
    out = capsys.readouterr().out
    assert "trying the Nasdaq index feed fallback" in out
    assert "nasdaq index fallback: index series now reaches 2026-09-22" in out  # the feed lags a day
    assert "filled the 2026-09-23 index close (20123.45)" in out
    payload = env.payload()
    assert payload["latest_date"] == CUR.isoformat()
    assert payload["latest_source"] == "nasdaq-quote"
    assert payload["series"]["ndx"][-1] == 20123.45


def test_index_fallback_is_skipped_when_yahoo_is_current(env, monkeypatch):
    """No extra request and no relabelling when Yahoo already reaches the expected session."""
    market = env.market
    yahoo = market.index_series()
    calls: list[str] = []

    def boom(url, *a, **k):
        calls.append(url)
        raise AssertionError(f"no request expected: {url}")

    monkeypatch.setattr(u.ms, "http_get", boom)
    merged, source = u.fill_index_from_nasdaq(yahoo, CUR, market.first)
    assert calls == []
    assert source is None
    assert merged.equals(yahoo)


def test_index_fallback_keeps_yahoo_values_and_adds_missing_sessions(env, monkeypatch):
    """Yahoo stopping one session early: only the missing session comes from Nasdaq."""
    market = env.market
    partial = market.index_series(through=prev_session(CUR))
    calls: list[str] = []

    def fake_get(url, params=None, headers=None, tries=6):
        assert "quote/NDX/historical" in url, url
        calls.append(url)
        return FakeResponse(Nasdaq(CUR).index_history(market, lag=0))

    monkeypatch.setattr(u.ms, "http_get", fake_get)
    merged, source = u.fill_index_from_nasdaq(partial, CUR, market.first)

    assert source == "nasdaq-index"
    assert len(calls) == 1
    assert pd.Timestamp(CUR) in merged.index
    # every overlapping session keeps the exact Yahoo value
    assert merged.reindex(partial.index).equals(partial)
