# Exact screening specifications

> ⚠️ **权威声明**：本文件与 `references/trading-rules.md` 为 `daily-stock-analysis` 脚本内部的量化初筛与发现层规则。最终交易裁决、资金仲裁、超大单否决、回落分档锁定与仓位控制以根目录《选股框架.md》与《CLAUDE.md》为最高权威。若本文件规则与《选股框架.md》存在冲突，**一律以《选股框架.md》为准**。

Use these specifications only after obtaining current, timestamped market data. Apply all conditions before sorting. Unless the user changes a threshold, boundary words are literal: “大于” and “小于” are strict; “在 A 至 B 之间” and “不超过” include their endpoints.

## Metric definitions

- **Five-session cumulative return**: `current price / close five trading sessions ago - 1`. Use a consistent adjusted series across the comparison.
- **Current-to-high gap**: `today high / current price - 1`.
- **Distance to 60-session high**: `60-session high / current price - 1`.
- **MA20 rising**: current MA20 is greater than the previous trading session’s MA20. State a stricter slope window if the data provider uses one.
- **Five-day average volume**: average full-day volume of the five completed trading sessions before today.
- **Industry/board**: report the source provider's primary industry classification; do not substitute exchange board, which is already constrained by the stock code.
- **Intraday high pullback**: `today high change percentage - current change percentage`, reported in percentage points. If only high price is available, compute `today high / previous close - 1`, then subtract current change.
- **Intraday average/VWAP status**: use minute data or a provider VWAP/average-price field. If unavailable in a single-run data source, write `无法验证`, lower the candidate by at least one class, and do not claim it is above VWAP.
- **First appeared after HH:MM**: only assert this when running a monitor with prior snapshots. In a single scan after the time threshold, treat qualifying high-gain names as tail-session risk if they otherwise meet that risk rule; do not invent first-appearance history.
- **Sector resonance**: yes only when at least two stocks in the same provider industry/sector are simultaneously strong under comparable gain/turnover/amount conditions. Otherwise report no or unverified.

## Tradeable low-absorption ultra-short (低吸超短线)

Use this when the user asks for ultra-short candidates that are still in a tradable low-absorption zone instead of the highest gainers. Filter the full A-share universe with all hard conditions before classification:

- Code starts with `60` or `00`; exclude ChiNext, STAR Market, Beijing Stock Exchange, ST/*ST, delisting-risk names, suspended stocks, and stocks currently unavailable to buy at limit-up.
- Live change: 2.2%-4.8% inclusive for buy candidates. 4.8%-5.2% may enter observation only. Above 5.2% is not a buy candidate.
- Live turnover: 2.5%-8% inclusive for preferred buy candidates. Above 10% is excluded from buy candidates.
- Live volume ratio: 1.2-3.8 inclusive. Above 6 is excluded from buy candidates.
- Live amount: above CNY 300 million (3 亿元); prefer CNY 400 million-1.2 billion (4-12 亿元).
- Current price: above MA5.
- Intraday average/VWAP: price is above intraday average, or pulled back to it without breaking. If this cannot be verified, do not place the stock in A unless all other entry conditions are very strong and the limitation is stated.
- Prefer sector resonance with at least two same-sector peers strengthening.

Apply anti-chase and risk tags:

- If current price is close to the intraday high, do not prioritize it; tag `追高风险`.
- If pullback from intraday high exceeds 1.5 percentage points, tag `冲高回落风险`.
- If live change exceeds 5%, place it in observation only, never buy candidates.
- If the scan runs after 14:00 and live change exceeds 4.8%, tag `尾盘追高风险` unless prior snapshots prove it was already active earlier.
- If turnover exceeds 10% and amount is large while live change is below 3%, tag `巨量滞涨`; never recommend as buy.

Classify and output:

- **A类 可买低吸候选**: live change 2.2%-4.6%, healthy structure, controllable stop, not near an unverified/overextended intraday high, no disqualifying risk tag.
- **B类 趋势确认观察**: live change 4.6%-5.2%, or otherwise healthy but needs a pullback/confirmation.
- **C类 禁止追高**: gain too high, turnover too high, abnormal volume ratio, tail-session surge, giant-volume stagnation, or weak VWAP status.

Sort by current tradability, not by gain: controllable stop distance, VWAP/support proximity, sector resonance, moderate turnover/amount, and limited high-pullback come before raw gain.

Return these columns:

| 类别 | 代码 | 名称 | 涨幅 | 换手率 | 成交额 | 量比 | 板块 | 板块共振 | 日内高点回落 | 分时均价线状态 | 风险标签 |
|---|---|---|---:|---:|---:|---:|---|---|---:|---|---|

After 14:20, only stocks satisfying all of these may enter a tail-session buy pool: sector resonance = yes, above VWAP or verified VWAP pullback hold, intraday high pullback <= 1.5 percentage points, turnover <= 7%, and live change <= 4.8%. Otherwise place them in next-day observation.

## Tradeable low-absorption short-term trend (低吸短线趋势)

Use this when the user asks for T+1/T+3 trend candidates that still have buy-space and should not chase already accelerated stocks. Filter the full A-share universe with all hard conditions before classification:

- Code starts with `60` or `00`; exclude ChiNext, STAR Market, Beijing Stock Exchange, ST/*ST, delisting-risk names, and suspended stocks.
- Current price is above MA5, MA10, and MA20.
- MA5 is rising; MA10 is flat or rising.
- Live change: 2.5%-5.2% inclusive for buy candidates. 5.2%-6% may enter observation only. Above 6% is `持仓区/止盈区`, not a new buy.
- Live turnover: 2%-7.5% inclusive. Above 9% is excluded from buy candidates.
- Live amount: above CNY 300 million (3 亿元); prefer CNY 400 million-1.5 billion (4-15 亿元).
- Five-session cumulative return: no more than 18%.
- Current price distance above MA20: no more than 15%.
- Prefer sector resonance with at least two same-sector peers strengthening.

Apply anti-chase and risk tags:

- Live change 5.2%-6%: place in `趋势观察池`, not buy pool.
- Live change above 6%: tag `持仓区/止盈区`; do not recommend as new buy.
- Current price within 0.3% of intraday high: tag `追高风险`.
- If the scan runs after 14:20 and live change exceeds 4.8%, tag `尾盘追高风险` unless prior snapshots prove it was already active earlier.
- If amount/volume expands but gain stops expanding, tag `放量滞涨风险` when intraday or snapshot data supports it; otherwise write `无法验证`.

Classify and output:

- **A类 趋势低吸候选**: suitable for T+1/T+3, live change 2.5%-5.2%, aligned MA5/MA10/MA20, controlled MA20 distance, moderate turnover, no disqualifying risk tag.
- **B类 趋势确认观察**: live change 5.2%-6%, near high, tail-session risk, or otherwise needs pullback.
- **C类 持仓区/止盈区**: live change above 6%, overextended, turnover abnormal, or poor tradability; not recommended for new buys.

Sort by current tradability, not by gain: MA support proximity, stop controllability, sector resonance, limited MA20 extension, and healthy turnover/amount come before raw gain.

Return these columns:

| 类别 | 代码 | 名称 | 涨幅 | 换手率 | 成交额 | 板块 | 均线状态 | 近5日涨幅 | 距20日均线 | 日内高点回落 | 风险标签 |
|---|---|---|---:|---:|---:|---|---|---:|---:|---:|---|

After 14:20, only stocks satisfying all of these may enter a tail-session buy pool: sector resonance = yes, above VWAP or verified VWAP pullback hold, intraday high pullback <= 1.5 percentage points, turnover <= 7%, and live change <= 4.8%. Otherwise place them in next-day observation.

## Ultra-short (超短线)

Filter the full A-share universe with all conditions:

- Code starts with `60` or `00`; exclude ChiNext, STAR Market, and Beijing Stock Exchange.
- Exclude ST/*ST, suspended stocks, and stocks currently unavailable to buy at limit-up.
- Current price: 5–30 CNY inclusive.
- Float market cap: below CNY 20 billion (200 亿元).
- Live turnover: above 3%.
- Live volume ratio: above 1.2.
- Live amount: above CNY 300 million (3 亿元).
- Live change: 2%–5.5% inclusive.
- Current price: above MA5.
- Five-session cumulative return: no more than 18%.
- Current-to-high gap: no more than 3%.

Sort qualifying rows by current change descending and return at most ten:

| 股票代码 | 名称 | 当前涨幅 | 换手率 | 成交额 | 量比 | 所属板块 |
|---|---|---:|---:|---:|---:|---|

Do not add commentary beyond the data timestamp/source line, the result table, and a one-line count or failure reason.

## Short-term trend (短线趋势)

Filter the full A-share universe with all conditions:

- Code starts with `60` or `00`; exclude ChiNext, STAR Market, and Beijing Stock Exchange.
- Exclude ST/*ST, suspended stocks, and stocks currently at limit-up.
- Current price: 8–45 CNY inclusive.
- Float market cap: CNY 3–25 billion (30–250 亿元) inclusive.
- Current price: above MA5, MA10, and MA20.
- MA20: rising.
- Distance to 60-session high: no more than 10%.
- Live turnover: 2%–8% inclusive.
- Current volume: no more than 2 times the average full-day volume of the prior five completed sessions.
- Live change: 3%–6.5% inclusive.
- Five-session cumulative return: no more than 20%.

Sort qualifying rows by current change descending and return at most ten:

| 股票代码 | 名称 | 当前涨幅 | 换手率 | 成交额 | 所属板块 |
|---|---|---:|---:|---:|---|

Do not add commentary beyond the data timestamp/source line, the result table, and a one-line count or failure reason.
