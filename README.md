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

当天有效股票少于成分股的 90%、最新数据超过 6 天未更新、或交易日数量不足时，`update.py` 返回失败且不写入数据。网站保留前一天的结果，GitHub 会发邮件通知失败。

## 本机运行

```bash
pip install -r requirements.txt
python scripts/update.py
python -m http.server -d site 8000
```

## Cloudflare Pages 设置

Workers & Pages → Create → Pages → Connect to Git → 选择本仓库。Build command 留空，Build output directory 填 `site`。
