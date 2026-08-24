---
name: daily-stock-analysis
description: Analyze current A-share market conditions, screen Shanghai and Shenzhen main-board stocks for ultra-short-term or short-term trend setups, build tradable low-absorption candidate pools with anti-chase filters, build pre-market/intraday watchlists with trigger prices, and produce T+1/T+3 entry zones, stops, targets, position management, and review plans. Use when the user asks for 今日/实时 A股选股、超短线、短线趋势、盘中动能、候选股前十、是否空仓、低风险介入计划、仓位、止损分析、明日观察池、盘前池、触发价、提前埋伏、盘中提醒、低吸候选、可买区间、防追高、尾盘可买池、持仓处理、止盈、复盘, or why they keep missing entries.
---

# Daily Stock Analysis

Optimize for risk-adjusted decisions, not guaranteed returns. Treat “no trade” as a valid result. Never invent missing market data or loosen a hard filter merely to fill ten rows.

## Route the request

Choose exactly one mode:

1. Use **Tradeable low-absorption** when the user asks for 低吸候选、可买区间、防追高、尾盘可买池, or explicitly says the goal is not highest gainers but stocks still in a tradable buy zone.
2. Use **Ultra-short** when the user says 超短线, 日内动能, or asks for the specified 5–30 CNY momentum screen without low-absorption/anti-chase constraints.
3. Use **Short-term trend** when the user says 短线趋势, 趋势主升, or asks for the specified MA5/MA10/MA20 trend screen without low-absorption/anti-chase constraints.
4. Use **Daily decision** for broader T+1/T+3 analysis, entry planning, position sizing, or whether to stay in cash.
5. Use **Position management** when the user already bought or holds a stock and asks what to do, when to sell, whether to add, stop, or take profit.
6. Use **Review mode** for 盘后复盘, reviewing whether a trade followed the plan, or diagnosing chasing/missed-entry mistakes.
7. If the user explicitly requests both screeners, run them independently and return separate tables. Do not merge thresholds.
8. Use **Watchlist + trigger** when the user asks why they miss entries, asks for 明日观察池、盘前池、触发价、提前埋伏、盘中提醒, or wants candidates before they satisfy the strict momentum screen.

Read [references/screeners.md](references/screeners.md) before running Tradeable low-absorption, Ultra-short, or Short-term trend. Treat every listed condition as an AND condition. User-supplied thresholds override defaults only for that request.

Read [references/trading-rules.md](references/trading-rules.md) before giving Daily decision, Watchlist + trigger, Position management, Review mode, or any buy/sell/position-size plan. Screening results are only the discovery layer; trading permission must still pass risk and trading layers.

## Bundled query tool

Use `scripts/a_share_daily_screen.py` when the user wants current screening results and can run or paste tool output. The script fetches market snapshots, indices, daily K lines, strict dual-screen results, low-absorption A/B/C pools, a next-day watchlist, and latest-announcement risk labels. It outputs Markdown by default and JSON when requested.

For non-technical Mac users, use `scripts/运行A股筛选.command`. Double-clicking it opens a simple local GUI when Tkinter is available; otherwise it falls back to command-line mode and saves Markdown output to the Desktop.

Common commands:

```bash
python3 scripts/a_share_daily_screen.py --mode all --format md
python3 scripts/a_share_daily_screen.py --mode low watchlist --format md
python3 scripts/a_share_daily_screen.py --mode strict low --format json --save /tmp/a_share_screen.json
python3 scripts/a_share_daily_screen.py --mode all --skip-announcements
```

`--mode` now accepts multiple values: `strict` (原始双筛), `low` (低吸A/B/C), `watchlist` (明日观察池). Use `all` to include everything.

When using pasted script output, treat it as the query/discovery layer and still apply [references/trading-rules.md](references/trading-rules.md) before any buy/sell/position-size decision.

## Acquire and validate data

1. Fetch current data with live browsing, a market-data API, or another available real-time source whenever the request says 当前、今日、实时、盘中, or equivalent. Do not rely on model memory.
2. Record the market-data timestamp, trading status, and source. Prefer one internally consistent source; if combining sources, align timestamps and adjustment conventions.
3. Use unadjusted live quotes for current price, high, change, turnover, amount, and volume ratio. Use a consistent adjusted historical series for moving averages, cumulative returns, and 60-day highs.
4. Interpret `亿元` as CNY 100 million and confirm source units before filtering.
5. If the market is closed, clearly label the latest completed session rather than calling it real-time. If essential fields are unavailable or stale, state that screening cannot be verified; do not estimate values.
6. Return all qualifying stocks when fewer than ten pass. State “仅 N 只满足全部条件”; never add near-matches.

## Apply the probability gate

For any actual entry, exit, position, or review decision, apply [references/trading-rules.md](references/trading-rules.md). Before recommending an actual entry in Daily decision mode, require all four layers:

1. **Market**: breadth is neutral or positive, index risk is not clearly bearish, and enough strict candidates exist.
2. **Sector**: the candidate belongs to an active industry/theme with corroborating peer strength, not an isolated spike.
3. **Stock**: trend, extension, upper-shadow, and volume quality are acceptable.
4. **Entry**: the buy zone is near support or MA5 and has a nearby invalidation level.

If any layer fails, output **cash**, **observe**, **low_absorb_only**, or **avoid** as appropriate; Chinese-facing output may render these as 空仓、观察、只可低吸、回避. If market data is incomplete, lower the stance by at least one level.

## Avoid late entries

Before recommending an entry, check whether the candidate is already past the proper entry.

Mark as **错过/等待回踩** when any of these are true:

- Current price is more than 3% above MA5 or the planned support zone.
- Current-to-high gap is small but the stock has already made a sharp intraday move.
- Turnover is unusually high and the stock is no longer near a controllable invalidation level.
- The stop distance from current price is wider than the expected first target.

Do not convert a strong stock into an actionable buy if the buy point has passed.

## Build a watchlist before confirmation

Use Watchlist + trigger mode to find stocks before they enter the strict Ultra-short or Short-term trend screens.

1. The goal is not to recommend immediate buying. The goal is to create a watchlist with trigger prices, support zones, invalidation levels, and what to wait for intraday.
2. Prefer stocks that are close to a breakout or pullback confirmation, not stocks already extended.
3. Use relaxed pre-trigger filters:
   - Code starts with `60` or `00`; exclude ST/*ST and suspended stocks.
   - Current price: 5-45 CNY.
   - Float market cap: CNY 3-25 billion preferred.
   - Price is above MA10 or within 3% below MA10.
   - MA20 is flat or rising preferred.
   - Five-session cumulative return no more than 12% preferred.
   - Distance to 60-session high no more than 15% preferred.
   - Prior day or current turnover above 2% preferred.
   - Sector has at least two peers showing strength or improving structure.
4. For each watchlist stock, provide:
   - trigger price: break above prior high / intraday platform / MA5 reclaim
   - buy zone: pullback area only
   - invalidation: nearby support or MA10/MA20 break
   - chase ban: price above trigger by more than 2%-3% is no longer a buy
5. If the stock already satisfies strict Ultra-short conditions but is far above buy zone, mark it as **错过/等待回踩**, not actionable.

## Build a Daily decision plan

1. Set the stance:
   - **空仓**: weak breadth, insufficient candidates, excessive extension, missing data, or poor reward/risk.
   - **轻仓试错**: several candidates pass and breadth is neutral or better, but pullback or confirmation is still required.
   - **允许介入**: market, sector, stock, and entry all align; never imply full-position entry.
2. Prefer main-board `60*` and `00*` stocks unless the user specifies another universe. Exclude ST/*ST, suspended stocks, inaccessible limit-up stocks, and obvious long-upper-shadow setups.
3. Evaluate live price, change, turnover, amount, volume ratio, float market cap, industry; MA5/MA10/MA20, MA20 slope, five-session return, distance to 60-session high; current-to-high distance, volume quality, breadth, and sector confirmation.
4. For T+1, prefer a pullback to MA5/support and avoid chasing gaps or sharp intraday surges. For T+3, require aligned MA5/MA10/MA20, rising MA20, and limited extension from the base.
5. Give every actionable idea a buy zone, invalidation/stop, target area, abandon conditions, holding horizon, and maximum position.
6. Default to 0% exposure without a clear edge, 10%–20% per trial idea, and no full position even in a strong setup unless the user explicitly accepts higher risk.

## Present results

For exact screeners, follow the columns and sort order in [references/screeners.md](references/screeners.md). For low-absorption screeners, sort by current tradability/buy-space first, not by gain descending. Keep the response compact and place one line above the table with data timestamp, trading status, and source.

For Daily decision mode, return:

1. 市场立场：cash / observe / low_absorb_only / trial_entry_allowed / avoid.
2. Two to four data-backed reasons.
3. Candidate table with code, name, price, change, turnover, amount, volume ratio, industry, and score.
4. Entry-plan table with decision, candidate layer, buy zone, no-chase zone, stop/invalidation, target, R/R, T+1/T+3 view, abandon conditions, and max position.
5. A brief risk note. State explicitly that screening is not a profit guarantee or personalized investment advice when the user asks for certainty.

For intraday low-absorption advice that uses repeated scan history, explicitly classify each idea as **标准买入**, **提前试仓**, **观察持有**, or **取消** according to [references/trading-rules.md](references/trading-rules.md). If using 提前试仓, state “小仓试错，不是确认买点” and cap the CNY 150,000-account reference size at CNY 3,000-5,000.

For Watchlist + trigger mode, return:

1. 明日/盘中观察池 stance: observe / wait_trigger / avoid.
2. Watchlist table with code, name, price, industry, structure, trigger price, buy zone, invalidation, chase ban, and reason.
3. Separate **already missed** list for strong stocks that passed strict screens but are no longer near a valid entry.
4. A short execution rule: only act when trigger, time-window, repeated-screen, market, and sector confirmation happen together.

For Position management mode, return current holding, cost, quantity, current state, decision, strength line, warning line, hard stop, take-profit zone, next-day T+1 plan, whether adding is allowed, and whether opening a new stock is allowed.

For Review mode, return trade result, whether it followed the original plan, buy-point quality, position sizing, whether it chased, whether it violated the time window, whether repeated-screen confirmation was present, ignored risks, and the next rule correction.

Use direct language: “等待”, “只可低吸”, “不追高”, and “跌破 X 则失效”. Do not claim that any stock must rise or will guarantee profit.
