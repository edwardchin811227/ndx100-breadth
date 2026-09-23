"""Local weekly cross-check: Yahoo vs Futu closes and above/below-50MA signals for current members.

Runs on the local PC only (needs the Futu MCP login). Writes reports/futu_check_YYYY-MM-DD.md.
Futu rate-limits after roughly 30 quick calls, so requests are paced.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import membership as ms  # noqa: E402
from update import download  # noqa: E402

FUTU_SCRIPT = Path(os.environ.get(
    "FUTU_MCP_SCRIPT", r"C:\Users\av_ch\.dsh\skills\futu-stock-price\scripts\futu_mcp.py"))
BARS = 120
PAUSE = 2.5
PRICE_TOL = 0.005
REPORT_DIR = ms.ROOT / "reports"


def load_futu():
    spec = importlib.util.spec_from_file_location("futu_mcp", FUTU_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def futu_closes(session, ticker: str, end: str) -> pd.Series | None:
    symbol = "US." + ticker.replace("-", ".")
    for attempt in range(4):
        r = session.call("quote_history_kline", {"symbol": symbol, "ktype": 2, "end": end, "num": BARS})
        d = json.loads(r["result"]["content"][0]["text"])
        if d.get("ret_code") == 0:
            kl = d["data"].get("kline_list") or []
            return pd.Series({pd.Timestamp(str(k["date"])): k["close"] for k in kl}).sort_index()
        if "rate limit" in d.get("ret_msg", "").lower():
            time.sleep(60 * (attempt + 1))
            continue
        return None
    return None


def main() -> int:
    today = ms.et_today()
    REPORT_DIR.mkdir(exist_ok=True)
    report = REPORT_DIR / f"futu_check_{today}.md"
    members = sorted(ms.members_on(ms.load(), today))

    try:
        futu = load_futu()
        session = futu.McpSession()
        session.initialize()
    except BaseException as e:  # futu_mcp raises SystemExit on auth failure
        msg = f"# 富途对数 {today}\n\n无法连接富途 MCP（可能需要重新登录：`python {FUTU_SCRIPT} login`）。\n\n错误：{e}\n"
        report.write_text(msg, encoding="utf-8")
        print(msg)
        return 1

    start = (pd.Timestamp(today) - pd.Timedelta(days=BARS * 2)).date()
    yahoo = download(members, start)

    rows, skipped = [], []
    for i, t in enumerate(members):
        fu = futu_closes(session, t, today)
        time.sleep(PAUSE)
        if fu is None or fu.empty or t not in yahoo:
            skipped.append(t)
            continue
        df = pd.concat([fu.rename("futu"), yahoo[t].rename("yahoo")], axis=1, sort=True).dropna()
        diff = (df.yahoo / df.futu - 1).abs()
        sig = pd.concat([(df[c] > df[c].rolling(50).mean()) for c in ("futu", "yahoo")], axis=1).iloc[50:]
        mism = sig.index[sig.iloc[:, 0] != sig.iloc[:, 1]]
        rows.append({"ticker": t, "days": len(df), "max_diff_pct": round(diff.max() * 100, 3),
                     "bad_days": int((diff > PRICE_TOL).sum()),
                     "signal_mismatch": ", ".join(d.date().isoformat() for d in mism)})
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(members)}", flush=True)

    res = pd.DataFrame(rows)
    flagged = res[(res.bad_days > 0) | (res.signal_mismatch != "")] if len(res) else res
    lines = [f"# 富途对数 {today}", "",
             f"对比最近约 {BARS} 个交易日：{len(res)} 只完成，{len(skipped)} 只跳过（富途或 Yahoo 无数据）。",
             f"价格偏差超过 {PRICE_TOL:.1%} 或 50 日线信号不一致的股票：{len(flagged)} 只。", ""]
    if len(flagged):
        lines += ["| 股票 | 天数 | 最大偏差% | 偏差天数 | 信号不一致日期 |", "|---|---|---|---|---|"]
        lines += [f"| {r.ticker} | {r.days} | {r.max_diff_pct} | {r.bad_days} | {r.signal_mismatch or '–'} |"
                  for r in flagged.itertuples()]
    if skipped:
        lines += ["", "跳过：" + ", ".join(skipped)]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:5]))
    print("report:", report)
    return 0 if not len(flagged) else 2


if __name__ == "__main__":
    raise SystemExit(main())
