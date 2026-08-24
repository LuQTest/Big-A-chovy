# Trading decision rules

> ⚠️ **权威声明**：本文件与 `references/screeners.md` 为 `daily-stock-analysis` 脚本内部的量化初筛与发现层规则。最终交易裁决、资金仲裁、超大单否决、回落分档锁定与仓位控制以根目录《选股框架.md》与《CLAUDE.md》为最高权威。若本文件规则与《选股框架.md》存在冲突，**一律以《选股框架.md》为准**。
>
> 核心层级映射：
> - **生产/初筛层**（本目录）：负责全市场粗筛、日K/均线指标计算、双池交集 default-v2 状态机。
> - **仲裁/决策层**（选股框架.md）：负责 5/5 门禁、超大单主导验证、回落分档锁定、板块联动必备、一票否决与执行落盘。

Use these rules after screening, before any buy, sell, position-size, holding, or review decision. Screening入榜只代表发现候选，不代表可以买。

## Decision words

| Decision | Meaning |
|---|---|
| cash | No market-level edge today; do not buy. |
| observe | Conditions are improving but incomplete; watch only. |
| low_absorb_only | Only buy near support/VWAP with a clear stop. |
| trial_entry_allowed | Strict evidence passed; only small trial size is allowed. |
| avoid | Hard flaw, broken structure, bad R/R, or unacceptable risk; do not trade. |

Keep `avoid` as the risk brake. A stock may appear in a screener and still receive `avoid` in the trading layer. Never loosen standards because the user wants to trade.

## Candidate layers

| Layer | Meaning |
|---|---|
| strict | Evidence stack passed; may enter a trade plan if buy zone, stop, R/R, time window, and position rules also pass. |
| hopeful | Potential exists but it must wait for trigger, pullback, or re-screen confirmation. |
| avoid | Hard flaw or unfixable R/R; no buy zone or position size. |

`hopeful` defaults to `observe`. `avoid` must output `avoid` and must not provide a buy plan.

## Time-window rules

### 09:30-10:10 direction observation

- Watch market direction and sector strength only.
- Do not output `trial_entry_allowed`.
- Even dual-pool overlap can only be `observe` or, at most, `low_absorb_only` with no direct buy instruction.
- Any stock found here must wait for later re-screen confirmation.

### 10:20-10:45 first buy-point window

This is the primary confirmation window. Allow a trade plan only when all are true:

1. The stock stayed in the most recent two scans or prior snapshots prove continuity.
2. Sector resonance is still present.
3. Current price is above intraday average/VWAP.
4. Pullback from intraday high is <= 1.5 percentage points.
5. Live change is still in the buyable zone.
6. Volume ratio and turnover are not overheated.
7. After pullback, price moved sideways for at least 5 minutes and is not continuously falling.
8. R/R is acceptable.

If no prior scan snapshots exist, say repeated-screen confirmation is `无法验证`; do not pretend it exists.

Default rule: do not buy before 10:20; after 10:20, standard buying requires the stock to remain A/strict. Exception: use the **continuous-A early trial rule** only when all conditions in that module pass. This exception is for CNY 3,000-5,000 trial size only; it is not a standard buy point, not a heavy-position rule, and not a way to repair a prior loss.

### 11:00-11:30 morning confirmation

- Not a preferred new buy window.
- Confirm whether early candidates remain strong.
- If an early candidate drops out of the pool, cancel its trading qualification.
- For existing holdings, judge holding strength only.

### 13:20-13:45 afternoon reflux window

Only consider:

1. Morning main-line reflux.
2. Low-position supplement inside the main line.

Position size must be lower than the morning primary buy point. If the morning structure already had a clear spike-and-fade, do not catch it only because price is lower.

### 14:20-14:40 tail-session risk / next-day watchlist

- Default: no new positions.
- Focus on risk control, holding management, and next-day watchlist.
- Tail-session trial buys require all of these:
  1. Sector resonance = yes.
  2. Price is above intraday average/VWAP.
  3. Live change <= 4.5%.
  4. Turnover <= 7%.
  5. Pullback from high <= 1.2 percentage points.
  6. It is not a first-time tail-session spike; prior snapshots are required to prove this.
  7. The most recent two scans stayed in-pool.
  8. Core sector names did not fail, open limit-up, or dive.

### After 14:40

- Do not recommend new positions.
- Manage existing holdings only.
- Build next-day watchlists only.

## Re-screen and drop-out rules

A first-time in-pool stock must pass at least one later re-screen before it can become A/strict.

A stock can enter A/strict only when all are true:

1. Sector resonance = yes.
2. Price is above intraday average/VWAP.
3. Pullback from high <= 1.5 percentage points.
4. Volume ratio <= 3.5.
5. Turnover <= 7%.
6. Live change is in a buyable zone.
7. It held sideways after pullback and is not continuously falling.
8. It passed at least two scans, or the lack of history is explicitly marked `无法验证` and the decision is downgraded.

If a stock entered during 09:45-10:10 and drops out after 10:20, treat it as early fake strength and do not buy. If a dual-pool stock drops out of either pool, downgrade its trading permission.

## Continuous-A early trial rule

This module exists to fix mechanical 10:20 over-filtering. It does not make the strategy more aggressive. The 10:20 confirmation rule remains the main rule; continuous-A early trial is a narrow exception for repeated low-absorption A names that are technically intact.

### Background

Some stocks may enter `低吸超短 A` several times between 10:05 and 10:16, with clean announcements, price above VWAP, sector resonance, moderate volume ratio and turnover, acceptable high pullback, and tradable amount. If such a stock drops out once around 10:19-10:20 because of script ranking, temporary threshold drift, candidate-count limits, or small live-data movement, do not automatically treat it as failed.

Differentiate:

1. real weakening;
2. temporary script drop-out;
3. being squeezed out by ranking or candidate-count limits;
4. short-lived metric boundary drift;
5. intact technical structure.

### Early trial eligibility

Between 10:05 and 10:16, allow an early small trial only when all are true:

1. The stock entered `低吸超短 A` at least three consecutive scans.
2. Announcement risk is `clean`.
3. Intraday average/VWAP state is `均价线上方`.
4. Sector resonance is `是`.
5. Pullback from intraday high is <= 1.5 percentage points.
6. Live change is +2.0% to +4.0%.
7. Volume ratio is 1.2 to 3.5.
8. Turnover is 2% to 6%.
9. Amount is CNY 300 million to 1.5 billion.
10. No `avoid`, `watch_risk`, announcement watch risk, volume-stagnation risk, spike-and-fade risk, high-pullback risk, or sector-resonance-insufficient tag exists.
11. The market is not rapidly diving.
12. The user does not already hold a highly correlated stock in the same theme.

If any item is unverifiable, write `无法验证` and do not upgrade it to standard buy. At most it can be a tiny trial if all hard-risk fields are clean and the user explicitly accepts T+1 risk.

### Early trial size

For a CNY 150,000 account:

- Early trial amount: CNY 3,000-5,000.
- Buy only in whole board lots.
- Do not exceed CNY 5,000.
- Do not buy CNY 8,000 or more just because the stock was continuous A.
- Do not use this rule to buy a second stock in the same theme.
- Do not increase trial amount after a prior loss.

The role of this trade is **trial-and-error**, not confirmed heavy buying.

### After 10:20 handling

If an early-trial stock still meets all of these after 10:20, continue holding:

- still `低吸超短 A`;
- announcement risk `clean`;
- price above VWAP;
- high pullback <= 1.5 percentage points;
- sector resonance remains `是`;
- no volume-driven selloff.

Do not actively add by default. Add only when the user explicitly asks and the market, sector, and intraday structure all strengthen clearly.

If it drops out after 10:20 but still meets all of these, classify as `观察持有`, not an immediate mistake:

- still above VWAP;
- high pullback <= 1.5 percentage points;
- announcement risk remains `clean`;
- sector has not clearly weakened;
- no volume-driven selloff;
- live change remains +2.0% to +4.5%.

If it drops out after 10:20 and any of these appear, classify the trial as failed:

- falls below VWAP;
- high pullback > 1.8 percentage points;
- volume-driven selloff;
- sector resonance disappears;
- `watch_risk` appears;
- `avoid` appears;
- live change quickly falls below +2%;
- market rapidly dives.

Because the account is subject to T+1, a failed same-day trial cannot be sold immediately. Produce a next-day stop-loss or loss-reduction plan first.

### Temporary drop-out review

For a 10:05-10:16 continuous-A stock, a single drop-out at 10:19 or 10:20 cannot by itself be a hard veto. Review:

1. Is it still above VWAP?
2. Is it still within the +2.0% to +4.5% acceptable zone?
3. Is high pullback still <= 1.5 percentage points?
4. Is announcement risk still `clean`?
5. Does sector resonance remain?
6. Was it only squeezed out by ranking, not by a broken intraday structure?
7. Did a volume-driven selloff appear?
8. Did it break the morning key platform?
9. Does it highly overlap with an existing holding's theme?

Classify conclusions:

- Continuous A + one drop-out + intact technical structure: keep observing.
- Continuous A + early trial + one drop-out + intact technical structure: hold and observe; do not add.
- Continuous A + drop-out + below VWAP: cancel.
- Continuous A + drop-out + `watch_risk` or `avoid`: cancel.
- Non-continuous A + drop-out: cancel.
- Only one or two A appearances: early trial rule is not allowed.

### Abuse bans

Do not use the continuous-A early trial rule when any are true:

1. Only one or two A appearances.
2. Announcement risk is not `clean`.
3. `watch_risk` appears.
4. `avoid` appears.
5. Live change > +4.5%.
6. High pullback > 1.5 percentage points.
7. Volume ratio > 3.5.
8. Turnover > 6.5%.
9. Amount > CNY 1.5 billion and turnover is high.
10. Price falls below VWAP.
11. Sector resonance is `否`.
12. Market indices are rapidly diving.
13. The user already bought a same-theme stock.
14. The user just exited a losing trade and is emotionally unstable.
15. The reason is fear of missing out.

### Output classes for intraday advice

When giving intraday advice, explicitly classify one of:

1. **标准买入**: after 10:20, still A/strict, clean, above VWAP, sector resonance, high pullback <= 1.5 percentage points.
2. **提前试仓**: 10:05-10:16 at least three consecutive A scans and all early-trial conditions pass. State: `小仓试错，不是确认买点`. Size: CNY 3,000-5,000.
3. **观察持有**: early trial already entered; one 10:20 drop-out, but VWAP, announcement, sector, and high-pullback conditions remain intact.
4. **取消**: below VWAP, `watch_risk` or `avoid`, high pullback exceeded, volume-driven selloff, sector resonance disappeared, or non-continuous A dropped out.

### Case note: 002245 on 2026-07-02

On 2026-07-02, 002245 蔚蓝锂芯 repeatedly entered `低吸超短 A` between 10:05 and 10:16. The setup had clean announcement risk, battery-sector resonance, price above VWAP, moderate volume ratio and turnover, tradable amount, and high pullback near but not above the 1.5 percentage-point cap. A temporary drop-out around 10:19 caused a mechanical cancellation under the old rule. Review conclusion: it was a missed entry, not a bad read. Single drop-out should trigger the temporary drop-out review above, not automatic cancellation.

## Market breadth permission

| Breadth | Permission |
|---|---|
| >=55% | Normal opportunity search. |
| 48%-55% | Light trial only. |
| 42%-48% | Downgrade one level; low-absorption or observe only. |
| <42% | Usually cash/observe; only very strong main line can be low_absorb_only. |
| <35% | Default cash unless an exceptional main line and clear buy point exist. |

Weak breadth does not stop screening, but it must reduce trading permission and position size.

## Buy-zone layers

- Main buy zone: live change +2.5%-+4.5%. This is the preferred area for `low_absorb_only` or `trial_entry_allowed` if all other evidence passes.
- Cautious buy zone: +4.5%-+5.0%. Allow only small size when sector resonance, VWAP, high-pullback, sideways hold, repeated scans, and R/R all pass.
- Trend confirmation / holding zone: +5.0%-+6.5%. Default observe or holding management; do not chase new buys.
- Wind vane / take-profit zone: above +6.5%. No new positions; use as sector leader, holding take-profit reference, or next-day anchor.

## A/B/C trading classes

### A / strict tradable candidate

All must pass:

1. Time is after 10:20.
2. Live change +2.5%-+4.6%.
3. High pullback <= 1.5 percentage points.
4. Price above intraday average/VWAP.
5. Turnover 3%-7%.
6. Volume ratio 1.2-3.5.
7. Amount preferably CNY 300 million-1.5 billion.
8. Sector resonance = yes.
9. Most recent two scans stayed in-pool.
10. After pullback, price held sideways at least 5 minutes and is not continuously falling.
11. R/R >= 1.8; in a strong market minimum 1.5.

If any condition fails, do not classify as A/strict.

### B / hopeful observation

Use B when any apply:

- Time is before 10:20.
- High pullback is 1.5-3 percentage points.
- Live change is +4.6%-+5.2%.
- Sector resonance is insufficient.
- Volume ratio or turnover is high.
- First-time in-pool with no re-screen confirmation.
- Price is above VWAP but has not held sideways.
- R/R is not confirmed.

B is observe only; do not give a buy recommendation.

### C / avoid

Use C/avoid when any apply:

- High pullback >3 percentage points.
- Price is below intraday average/VWAP.
- Turnover >10%.
- Volume ratio >6.
- Live change >5.2% and the user does not already hold it.
- Early in-pool then dropped out.
- Single-stock strength without sector resonance.
- Continuously falling; do not buy merely because it is cheaper.
- Volume stagnation, high-turnover weak gain, or giant-volume stagnation.
- Sector leader already limit-up failed/opened or dived while laggards start moving.
- Main theme has climaxed and tail-session laggards are catching up.

C must output `avoid` or `observe`; no buy zone and no position size.

## Risk layer

| Risk tag | Meaning |
|---|---|
| clean | No obvious hard flaw; may enter trading plan. |
| watch_risk | Soft risk exists; downgrade decision or position. |
| avoid | Hard flaw; do not trade. |

Do not delete risky names from the screener table unless the user asks for filtered output. Screening discovers; risk layer decides tradability.

## Announcement risk layer

When announcement data is available, apply it before any trade plan:

- `avoid`: hard negative announcements such as reduction, passive reduction, inquiry/regulatory letters, investigation, administrative penalty, delisting risk, loss warning, earnings downgrade, impairment, unlock, share freeze, litigation/arbitration, overdue debt/guarantee, capital occupation, modified audit opinion, or trading suspension check. Do not provide a buy plan.
- `watch_risk`: soft risks such as pledge, guarantee, related-party transaction, earnings forecast/flash report, correction/supplemental announcement, senior executive/director resignation, accounting-policy change, auditor change, or shareholder-meeting delay. Downgrade at least one level and reduce or cancel position size.
- `clean`: no listed keyword in the latest checked announcements; this is not proof of no risk.
- `无法验证`: announcement API failed or returned unusable data. Downgrade at least one level if the trade depends on clean announcement status.

Ignore routine non-risk items such as dividend/equity distribution, legal opinion, independent opinion, qualification approval, and shareholder-meeting resolution unless their title also contains a hard-risk keyword.

## Intraday hard vetoes

Any of these forces `avoid` or `observe`, never `trial_entry_allowed`:

1. **超大单未主导（8/7新增·一票否决）**：超大单 ≤0 或超大单占主力净额 <50%，降级为散户堆量，严禁买入（中国巨石教训）。
2. **板块联动缺失（8/6新增·A类必备）**：同板块在筛选中 <2 只，早盘 A 类严禁出手（大恒科技教训）。
3. **绝不补仓（一票否决）**：持仓发生浮亏或走弱，严禁摊薄成本，破止损线无条件离场（哈药股份教训）。
4. **回落分档基准锁定（8/20新增）**：首次 5/5 触发时锁定回落基准，后续回踩 VWAP 不重算分档（解决超声电子买点互斥悖论）。
5. Turnover >10% and gain <4%: high-turnover weak gain.
6. Amount expands but gain no longer expands: volume stagnation, only if snapshot/minute data supports it; otherwise write `无法验证`.
7. Price below VWAP and cannot recover within 10 minutes.
8. Pullback from intraday high >3 percentage points without clear support.
9. After 14:20, first-time screen entry with gain >4.5%; prior snapshots are required to prove not first-time.
10. Single-stock sector strength only.
11. Leader failed/opened limit-up or dived while laggards start moving.
12. Main theme climaxed and tail-session laggards catch up.
13. Early strong stock drops out after 10:20.
14. Continuous falling; do not low-absorb only because price is lower.

## Evidence stack

A trade plan must check:

1. Market trend is not clearly bearish.
2. Shanghai, Shenzhen, ChiNext, and CSI 300 are not severely diverging.
3. Breadth permits the intended trading level.
4. Market turnover supports continuation.
5. No clear macro/policy/regulatory negative shock.
6. Sector has multiple-stock resonance.
7. Theme has continuity, not just one-day news.
8. Volume is active but not explosive.
9. Stock is above MA5/MA10/MA20, with rising MA20 preferred.
10. Exclude ST, delisting, earnings landmine, reduction, unlock, inquiry-letter, or similar obvious risk when data is available.
11. Buy zone is near support and stop is clear.
12. Default R/R >= 1.8; strong-market minimum 1.5.

If evidence is insufficient, do not give an aggressive buy.

## Position sizing

| Decision | Position |
|---|---|
| cash | 0 |
| avoid | 0 |
| observe | 0 |
| low_absorb_only | Max 10%-15% per stock. |
| trial_entry_allowed | Max 5%-10% per stock. |
| Strong market + strong main line + dual-pool + valid buy zone | At most 20% per stock. |

Extra rules:

1. Never exceed 30% of total capital in one stock.
2. No opening before 10:20.
3. Afternoon trial size is half of morning size.
4. Tail-session trial size is at most 5%.
5. Do not increase single-stock size because of a profit cushion.
6. Do not make revenge or compensation trades after missed opportunities.
7. If the user already holds the same sector, halve any new same-direction position.
8. If the user has one large holding, avoid opening a second highly correlated stock.

## T-cycle rules

### T+1

Use for intraday reflux, low-position supplement, tail-session trial, strong theme but incomplete trend evidence, rotation markets, or non-ideal buy points. Goal: sell into next-day strength; do not over-hold. Prefer taking profit at +3%-5%.

### T+3

Use for dual-pool overlap, sector resonance, complete trend structure, support-near buy, healthy volume, strong close, and no afternoon drop-out from the core pool. Goal: +5%-8%, only if main line keeps strengthening.

### T+5

Use only when breadth is strong, main-line height is clear, stock is in early/mid trend, MA20 rises, no obvious overheating exists, and sector strengthens for at least two days. Do not use T+5 in weak, rotation, or spike-and-fade markets.

## Required trade-plan output

When giving a trade plan, include:

```text
决策：
候选层级：
交易周期：
亏损路径：
盈利逻辑：
买区：
不追区：
止损：
目标1：
目标2：
R/R：
放弃条件：
最大仓位：
是否允许加仓：
是否允许T+3：
```

## Position management output

If the user already holds a stock, handle the holding before recommending new stocks. Return:

```text
当前持仓：
成本：
数量：
当前状态：
决策：
强弱分界线：
盘中警戒线：
硬止损：
止盈区：
明日T+1处理：
是否允许补仓：
是否允许开新票：
```

Rules:

1. If an existing holding is large, do not recommend opening new positions.
2. If a held stock drops out of the pool, downgrade trading permission.
3. If a held stock is below cost and the sector weakens, do not add.
4. Under T+1 restrictions, if the user cannot sell today, produce a next-day handling plan.
5. If the next day reaches the target zone, prioritize taking profit.
6. If the next day opens low and weak, prioritize defense.

## Review output

For post-trade review, return:

```text
交易结果：
是否符合原计划：
买点是否合格：
仓位是否合理：
是否追高：
是否违反时间窗口：
是否连续两次入池：
是否满足连续A提前试仓：
10:20掉池是否复核：
是否忽略高位回落：
是否忽略均价线：
是否忽略板块切换：
下次规则修正：
```

Review principles:

1. Do not judge only by profit/loss.
2. Focus on rule compliance.
3. Profitable but rule-breaking trades must still be recorded.
4. Losing trades that followed the rules are not severe mistakes.
5. Main errors are chasing, compensation trades, position-size loss of control, and unconfirmed early fake strength.

## Final hard rules

1. 09:30-10:10 observe only; no standard buying.
2. After 10:20, require two in-pool confirmations before allowing a standard trade.
3. The only pre-10:20 exception is the continuous-A early trial rule, with CNY 3,000-5,000 maximum and all early-trial conditions satisfied.
4. High pullback >1.5 percentage points cannot be A/strict.
5. Price below VWAP cannot be A/strict.
6. Above +5% defaults to no buy; observe only.
7. Early in-pool then dropped out cancels trading qualification unless it qualifies for continuous-A temporary drop-out review and the technical structure is still intact.
8. One 10:19-10:20 drop-out is not a hard veto for a 10:05-10:16 continuous-A stock; review VWAP, change zone, high pullback, announcement risk, sector resonance, volume selloff, morning platform, and same-theme holdings.
9. `watch_risk` and `avoid` remain one-vote vetoes for early trial, standard buy, and add-on decisions.
10. A/strict means tradable now, not merely promising.
11. B/hopeful is observe only.
12. C/avoid is no trade.
13. Handle existing holdings before new stocks.
14. Every new position needs buy zone, stop, target, position size, and abandon conditions.
15. Better to miss than chase as compensation.
16. Do not use early trial to repair a loss, chase fear of missing out, buy a second same-theme stock, or exceed the CNY 3,000-5,000 trial size.
