const fmtPct = (v) => (v == null ? "–" : v.toFixed(1) + "%");
const signed = (v) => (v > 0 ? "+" : "") + v.toFixed(1);

function zone(p) {
  if (p < 20) return ["偏弱", "down"];
  if (p > 80) return ["过热", "up"];
  return ["中性", ""];
}

function fillCards(d) {
  const s = d.series;
  const n = s.date.length - 1;
  const pct = s.pct[n];
  document.getElementById("c-pct").textContent = fmtPct(pct);
  document.getElementById("c-date").textContent = s.date[n];
  const d1 = pct - s.pct[n - 1];
  const el = document.getElementById("c-d1");
  el.textContent = signed(d1) + " 个百分点";
  el.className = "value " + (d1 >= 0 ? "up" : "down");
  document.getElementById("c-d5").textContent = "5 日 " + signed(pct - s.pct[n - 5]) + " · 20 日 " + signed(pct - s.pct[n - 20]);
  document.getElementById("c-count").textContent = `${s.above[n]} / ${s.valid[n]}`;
  document.getElementById("c-cov").textContent =
    s.valid[n] < s.members[n] ? `成分股 ${s.members[n]} 只，${s.members[n] - s.valid[n]} 只无数据` : `成分股 ${s.members[n]} 只，全部计入`;
  const [z, cls] = zone(pct);
  const zEl = document.getElementById("c-zone");
  zEl.textContent = z;
  zEl.className = "value " + cls;
  document.getElementById("m-latest").textContent = d.latest_date +
    (d.latest_source === "nasdaq-quote" ? "（当天收盘价暂取自纳斯达克报价，Yahoo 更新后自动替换）" : "");
  document.getElementById("m-gen").textContent = new Date(d.generated_at).toLocaleString("zh-CN", { hour12: false });
  const dead = new Set(d.no_price_data);
  const nd = Object.entries(d.gaps).map(([t, [a, b, n]]) =>
    `${t}（${a} 至 ${b}，${n} 个交易日${dead.has(t) ? "，查不到价格" : ""}）`);
  document.getElementById("nodata").textContent = nd.length ? nd.join("；") : "无";
}

function fillEvents(d) {
  const tb = document.querySelector("#events tbody");
  const src = { wikipedia: "维基百科修订", "nasdaq-api": "纳斯达克官方名单" };
  tb.innerHTML = [...d.events].reverse().map((e) =>
    `<tr><td>${e.date}</td><td class="add">${e.added.join(", ") || "–"}</td>` +
    `<td class="rm">${e.removed.join(", ") || "–"}</td><td title="${(e.note || "").replace(/"/g, "&quot;")}">${src[e.source] || e.source}</td></tr>`
  ).join("");
}

function drawChart(d) {
  const s = d.series;
  const chart = echarts.init(document.getElementById("chart"), "dark", { renderer: "canvas" });
  const dateSet = new Set(s.date);
  // an event dated on a non-trading day shows on the next trading day
  const eventLines = d.events.map((e) => {
    const x = s.date.find((x) => x >= e.date);
    return x && dateSet.has(x) ? { xAxis: x, event: e } : null;
  }).filter(Boolean);
  chart.setOption({
    backgroundColor: "transparent",
    animation: false,
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter(params) {
        const i = params[0].dataIndex;
        const date = s.date[i];
        let html = `<b>${date}</b><br>市宽 ${fmtPct(s.pct[i])}（${s.above[i]}/${s.valid[i]}）<br>纳指100 ${s.ndx[i].toLocaleString()}`;
        if (s.valid[i] < s.members[i]) html += `<br><span style="color:#aaa">无数据：${(d.missing[date] || []).join(", ")}</span>`;
        const ev = eventLines.find((l) => l.xAxis === date);
        if (ev) {
          const e = ev.event;
          if (e.added.length) html += `<br><span style="color:#26a69a">加入 ${e.added.join(", ")}</span>`;
          if (e.removed.length) html += `<br><span style="color:#ef5350">剔除 ${e.removed.join(", ")}</span>`;
        }
        return html;
      },
    },
    grid: [
      { left: 56, right: 24, top: 20, height: "55%" },
      { left: 56, right: 24, top: "68%", height: "20%" },
    ],
    xAxis: [
      { type: "category", data: s.date, gridIndex: 0, axisLabel: { show: false }, boundaryGap: false },
      { type: "category", data: s.date, gridIndex: 1, boundaryGap: false },
    ],
    yAxis: [
      { type: "value", min: 0, max: 100, gridIndex: 0, axisLabel: { formatter: "{value}%" }, splitLine: { lineStyle: { color: "#273039" } } },
      { type: "value", gridIndex: 1, scale: true, splitLine: { lineStyle: { color: "#273039" } } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1] },
      { type: "slider", xAxisIndex: [0, 1], bottom: 8, height: 22 },
    ],
    series: [
      {
        name: "市宽", type: "line", data: s.pct, xAxisIndex: 0, yAxisIndex: 0,
        showSymbol: false, lineStyle: { width: 1.6, color: "#4aa3ff" },
        areaStyle: { color: "rgba(74,163,255,0.08)" },
        markLine: {
          silent: true, symbol: "none", label: { color: "#8b96a1", formatter: "{c}%" },
          data: [
            { yAxis: 80, lineStyle: { color: "#26a69a", type: "dashed" } },
            { yAxis: 50, lineStyle: { color: "#555", type: "dotted" } },
            { yAxis: 20, lineStyle: { color: "#ef5350", type: "dashed" } },
            ...eventLines.map((l) => ({
              xAxis: l.xAxis, label: { show: false },
              lineStyle: { color: "rgba(255,193,7,0.35)", type: "solid", width: 1 },
            })),
          ],
        },
      },
      {
        name: "纳指100", type: "line", data: s.ndx, xAxisIndex: 1, yAxisIndex: 1,
        showSymbol: false, lineStyle: { width: 1.2, color: "#c9d1d9" },
      },
    ],
  });

  document.getElementById("ranges").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#ranges button").forEach((x) => x.classList.toggle("on", x === b));
    const months = { "3m": 3, "6m": 6, "1y": 12 }[b.dataset.range];
    let startValue = s.date[0];
    if (months) {
      const t = new Date(s.date[s.date.length - 1]);
      t.setMonth(t.getMonth() - months);
      const iso = t.toISOString().slice(0, 10);
      startValue = s.date.find((x) => x >= iso);
    }
    chart.dispatchAction({ type: "dataZoom", startValue, endValue: s.date[s.date.length - 1] });
  });
  window.addEventListener("resize", () => chart.resize());
}

fetch("data/breadth.json", { cache: "no-cache" })
  .then((r) => r.json())
  .then((d) => {
    fillCards(d);
    fillEvents(d);
    drawChart(d);
  })
  .catch((e) => {
    document.getElementById("chart").textContent = "数据载入失败：" + e;
  });
