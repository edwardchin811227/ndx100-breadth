# 纳指100 市宽（高于 50 日均线占比）

每个美股交易日，计算纳指 100 成分股中收盘价高于自身 50 日均线的比例，展示过去 3 年走势，并叠加纳指 100 指数。

- 每一天都按**当天的真实成分股**计算：历史名单从维基百科修订历史还原，之后每日对比纳斯达克官方名单，自动记录变更。
- 价格来自 Yahoo Finance（复权收盘价）。
- GitHub Actions 在美股收盘后运行 `scripts/update.py`，把结果提交到 `site/data/`，Cloudflare Pages 随即自动部署 `site/` 目录。

## 目录

| 路径 | 用途 |
|---|---|
| `scripts/membership.py` | 成分股历史：`rebuild` 从维基百科重建，`sync` 对比官方名单，`show 日期` 查看某天成分股 |
| `scripts/update.py` | 每日流程：同步成分股 → 下载价格 → 计算 → 校验 → 输出 |
| `scripts/futu_check.py` | 本机每周用富途对数（需要富途 MCP 登录，只在本机运行） |
| `data/membership.json` | 成分股初始名单与变更事件 |
| `site/` | 静态网页，`site/data/breadth.json` 与 `breadth.csv` 为输出数据 |

## 校验规则

`update.py` 以**美股交易日**判断资料是否够新，日历来自 `exchange_calendars` 的 NASDAQ 日历（与 XNYS 同一套节假日、半日市和夏令时规则）：

- 最新可发布的日期，必须是「最近一个已收市、且收市后已过 2 小时发布宽限期」的交易日；落后于它即失败，不再用「6 个自然日」这种宽松判断。
- 当天有效股票少于成分股的 90%，或交易日数量不足，同样失败。
- 仍在交易时段内、非交易日、或日历无法判断的日期，一律不进入 50 日均线计算，也不会被发布。
- 失败时不写入 `site/data/`，网站保留前一天结果，GitHub 会发邮件通知失败。
- `--allow-stale` 只把「资料不够新」降级为警告（离线／排查用），**不会**放宽 90% 覆盖率、交易日数量，也不会让未收市的数据被发布。

### 数据源与后备顺序

Yahoo Finance 是主来源，其余来源只补 Yahoo 缺的部分，**从不覆盖** Yahoo 已有的数值：

- **成分股价格**：Yahoo 缺价的股票（含整批被限流的情况）逐只用 Tiingo 复权收盘价补齐，**不设只数上限**；Tiingo 额度用尽时会停下并写明还缺哪些，不会中途写入半截数据。
- **纳指指数**：Yahoo 的 `^NDX` 取不到时，先用纳斯达克指数表补历史（该表比美股收市迟一个交易日），最新一个交易日仍由下面的报价后备补齐；Tiingo 没有指数数据（`NDX`／`^NDX` 都是 404），所以指数不靠 Tiingo。若当天最新一根由纳斯达克报价补齐，`latest_source` 记为 `nasdaq-quote`；只有指数序列整体来自纳斯达克、当天没有用报价补时，才记为 `nasdaq-index`。两者网站都会注明。
- **当天最新一根**：`patch_latest_from_nasdaq` 用纳斯达克报价补，`latest_source` 记为 `nasdaq-quote`。

## 测试

```bash
pip install -r requirements.txt
python -m pytest tests -q
```

`tests/test_update_freshness.py` 全部离线运行：行情下载、Nasdaq 报价接口、Tiingo、成分股名单和输出目录都换成测试替身，不会下载真实行情，也不会改写 `site/data/`。

## 本机运行

```bash
pip install -r requirements.txt
python scripts/update.py
python -m http.server -d site 8000
```

## Cloudflare Pages 设置

Workers & Pages → Create → Pages → Connect to Git → 选择本仓库。Build command 留空，Build output directory 填 `site`。
