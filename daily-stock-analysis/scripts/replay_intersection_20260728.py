#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-07-28 验收回放：default-v2 严格顺序状态机。

用「按验收时间线构造的合成快照」驱动真实状态机函数
（compute_pre_intersection / evaluate_intersection_states），逐快照输出
`时间 → 状态 → 触发条件 → 失效/推进原因` 日志，并断言验收预期：

  1. 云图控股 002539：13:41 进准交集，13:41–13:44 维持，
     13:46 因 5 分钟资金转负退出，不产生买点。
  2. 星帅尔 002860：始终板块不共振 + 换手过高，不得进准交集。
  3. 中原内配 002448：主力资金为负时不得进准交集。
  4. 比音勒芬 002832：首次交集过热 → LATE_INTERSECTION。
  5. 分钟K超 180 秒：任何股票不得 RETEST_READY / ENTRY_ELIGIBLE。
  6. 市场宽度 <42%（CASH）：所有新开仓资格必须为否。
  7. 程序重启后未过期锁存状态必须恢复。
  8. 正向路径（共进股份 603118）：LATCHED→WAIT_RETEST→RETEST_READY→
     ENTRY_ELIGIBLE，两次确认使用不同 snapshot_id 且均在 14:20 前。

无未来数据：每个快照只携带该时刻可见字段；状态机仅读取
「当前快照 + 已持久化历史状态」，从不读取任何后续快照。
"""
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import a_share_daily_screen as screen

TZ = screen.TZ
FAILURES: list = []


def _t(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 7, 28, int(h), int(m), 0, tzinfo=TZ)


def check(cond: bool, msg: str) -> None:
    tag = "通过" if cond else "失败"
    print(f"  [{tag}] {msg}")
    if not cond:
        FAILURES.append(msg)


def mk(code, name, price, change, **kw):
    """构造一只 Enriched（默认满足超短+趋势质量条件，显式覆盖除外）。"""
    prev_close = kw.get("prev_close", round(price / (1 + change / 100), 2))
    ma5 = kw.get("ma5", round(price * 0.97, 2))
    ma10 = kw.get("ma10", round(price * 0.95, 2))
    ma20 = kw.get("ma20", round(price * 0.93, 2))
    vwap = kw.get("vwap", round(price * 0.995, 2))
    above = kw.get("price_above_vwap", vwap <= price)
    return screen.Enriched(
        code=code, name=name, price=price, change=change,
        turnover=kw.get("turnover", 4.0), amount=kw.get("amount", 6e8),
        volume_ratio=kw.get("volume_ratio", 2.0), high=kw.get("high", round(price * 1.01, 2)),
        low=kw.get("low", round(price * 0.99, 2)), open=kw.get("open", price),
        prev_close=prev_close, total_mv=kw.get("total_mv", 1.2e10),
        float_mv=kw.get("float_mv", 8e9), industry=kw.get("industry", "电子设备"),
        timestamp=0, volume=kw.get("volume", 3e6), kdate="2026-07-28", k_source="replay",
        adj_close=price, ma5=ma5, ma10=ma10, ma20=ma20,
        prev_ma5=ma5 * 0.999, prev_ma10=ma10 * 0.999, prev_ma20=ma20 * 0.997,
        five_ret=kw.get("five_ret", 0.10), dist60=kw.get("dist60", 0.05),
        ma20_dist=0.04, high_pull=kw.get("high_pull", 0.3),
        cur_to_high=kw.get("cur_to_high", 0.01), vol_vs_avg5=kw.get("vol_vs_avg5", 1.2),
        vwap=vwap, vwap_state=("均价线上方" if above else "均价线下方"),
        prior_high=round(price * 1.02, 2), prior_low=round(price * 0.98, 2),
        main_net=kw.get("main_net", 2e7), main_pct=kw.get("main_pct", 1.5),
        flow_5m_inc=kw.get("flow_5m_inc", 8e6), flow_15m_inc=kw.get("flow_15m_inc", 1.5e7),
        price_above_vwap=above, flow_status=kw.get("flow_status", "资金流入"),
    )


def m5(vol, close, vwap, age=30.0, status="fresh", bar="13:40"):
    """构造 minute_map 条目（状态机期望的字段结构）。"""
    return {
        "status": status, "age_seconds": age, "last_bar_at": bar,
        "close_5m": close, "vwap_5m": vwap, "vol_5m": vol,
        "fetch_status": "ok", "last_success_at": None, "error": None,
    }


# ── 时间线（每快照：时间, [(股票, resonance)], minute_map, market_context）──
NORMAL = {"market_mode": "NORMAL", "breadth_pct": 58.0, "reason": "市场宽度58.0%"}
CASH = {"market_mode": "CASH", "breadth_pct": 38.0, "reason": "市场宽度38.0%"}

RISK_MAP = {
    "002539": "watch_risk",   # 云图控股：可预警可观察，不新开仓
    "002860": "clean",
    "002448": "clean",
    "002832": "clean",
    "603118": "clean",
}


def yuntu(flow5=9e6):
    # 云图控股：差1项(当日涨幅2.8<3)，四道门槛由 flow5 控制
    return mk("002539", "云图控股", 8.60, 2.8, main_net=3.4e7, main_pct=2.2,
              flow_5m_inc=flow5, flow_15m_inc=2.0e7)


def xingshuaier():
    # 星帅尔：换手过高(9.5，差1项) + 板块不共振（resonance 在快照层置否）
    return mk("002860", "星帅尔", 11.20, 2.9, turnover=9.5, main_net=2e7)


def zhongyuan():
    # 中原内配：主力资金为负（差1项=当日涨幅2.6<3）
    return mk("002448", "中原内配", 9.40, 2.6, main_net=-1.2e7, main_pct=-0.8)


def biyin(price=33.0):
    # 比音勒芬：正式交集但首次即过热（涨幅5.2%≥4.6%）→ 迟到交集
    # 价格>30 会掉出超短池，改用 28.5 保持双池成员资格
    return mk("002832", "比音勒芬", 28.50, 5.2, turnover=6.0, main_net=5e7)


def gongjin(price, high=16.80, flow5=1.2e7, flow15=2.2e7):
    # 共进股份：正向路径（首次交集 change 3.4% 不过热）
    return mk("603118", "共进股份", price, 3.4, high=high, vwap=round(price * 0.994, 2),
              main_net=4.5e7, main_pct=2.1, flow_5m_inc=flow5, flow_15m_inc=flow15)


def build_timeline():
    T = []
    # 13:39 云图第1次通过（连续确认1/2）；共进首次交集(LATCHED)；星帅尔/中原内配观察
    T.append(("13:39",
              [(yuntu(), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.76, high=16.78)), ],
              {"603118": m5(8000, 16.76, 16.70, age=25, bar="13:35"),
               "002539": m5(5000, 8.60, 8.55, age=25, bar="13:35")},
              NORMAL))
    # 13:41 云图第2次通过 → 准交集；共进回踩 → WAIT_RETEST（只推进一级）
    T.append(("13:41",
              [(yuntu(), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.62, high=16.80)), ],
              {"603118": m5(5200, 16.62, 16.58, age=30, bar="13:40"),
               "002539": m5(5100, 8.61, 8.56, age=30, bar="13:40")},
              NORMAL))
    # 13:42 云图维持准交集；共进条件全好但分钟K过期(400s) → 不得推进
    T.append(("13:42",
              [(yuntu(), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.63, high=16.80)), ],
              {"603118": m5(4000, 16.63, 16.60, age=400, status="stale", bar="13:35"),
               "002539": m5(5100, 8.61, 8.56, age=35, bar="13:40")},
              NORMAL))
    # 13:43 比音勒芬首次进入正式交集（过热）→ 迟到交集
    T.append(("13:43",
              [(yuntu(), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.63, high=16.80)), (biyin(), "是")],
              {"603118": m5(4000, 16.63, 16.60, age=420, status="stale", bar="13:35"),
               "002539": m5(5000, 8.61, 8.56, age=30, bar="13:42")},
              NORMAL))
    # 13:44 云图维持准交集；共进分钟K恢复新鲜+缩量回踩确认 → RETEST_READY（第1次确认）
    T.append(("13:44",
              [(yuntu(), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.63, high=16.80)), (biyin(), "是")],
              {"603118": m5(4000, 16.63, 16.60, age=40, bar="13:43"),
               "002539": m5(5000, 8.61, 8.56, age=30, bar="13:43")},
              NORMAL))
    # —— 此处模拟程序重启：保存→重新加载，锁存状态必须恢复 ——
    # 13:46 云图5分钟资金转负 → 退出准交集；共进第2次确认(不同snapshot) → ENTRY_ELIGIBLE
    T.append(("13:46",
              [(yuntu(flow5=-8e6), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.65, high=16.80)), (biyin(), "是")],
              {"603118": m5(4100, 16.65, 16.61, age=35, bar="13:45"),
               "002539": m5(4800, 8.58, 8.57, age=30, bar="13:45")},
              NORMAL))
    # 13:48 市场宽度跌破42% → CASH：所有新开仓资格必须为否
    T.append(("13:48",
              [(yuntu(flow5=-8e6), "是"), (xingshuaier(), "否"), (zhongyuan(), "是"),
               (gongjin(16.66, high=16.80)), (biyin(), "是")],
              {"603118": m5(4100, 16.66, 16.62, age=30, bar="13:47"),
               "002539": m5(4800, 8.57, 8.57, age=30, bar="13:47")},
              CASH))
    return T


def run():
    # 持久化重定向到临时文件，避免污染真实 intersection_state.json
    tmp_state = Path(tempfile.mkdtemp()) / "replay_state.json"
    screen.INTERSECTION_STATE_FILE = tmp_state

    cfg = dict(screen.DEFAULT_INTERSECTION_CONFIG)
    prev_state: dict = {}
    history: dict = {}   # code -> [(time, phase_code, note)]

    print("=" * 100)
    print("2026-07-28 default-v2 状态机验收回放（合成快照驱动真实状态机函数，无未来数据）")
    print("=" * 100)
    print(f"{'时间':<7} {'代码':<7} {'名称':<6} {'相位':<22} {'新开仓':<4} 触发/失效/阻断原因")
    print("-" * 100)

    for hhmm, stocks, minute_map, mctx in build_timeline():
        now = _t(hhmm)
        enriched = []
        resonance_by_code = {}
        for entry in stocks:
            if isinstance(entry, tuple):
                e, res = entry
            else:
                e, res = entry, "是"
            enriched.append(e)
            resonance_by_code[e.code] = res

        strict_ultra_rows = []
        for e in enriched:
            if not screen.strict_ultra(e):
                continue
            r = asdict(e)
            r["resonance"] = resonance_by_code[e.code]
            strict_ultra_rows.append(r)
        confirmation_codes = {e.code for e in enriched if screen.trend_confirm_relaxed(e)}
        pre = screen.compute_pre_intersection(strict_ultra_rows, confirmation_codes, {}, cfg)
        dual = [dict(r) for r in strict_ultra_rows if r["code"] in confirmation_codes]

        state_rows, prev_state = screen.evaluate_intersection_states(
            dual, pre, prev_state, now, cfg,
            snapshot_id=f"2026-07-28 {hhmm}:00",
            risk_map=RISK_MAP,
            minute_map=minute_map,
            market_context=mctx,
        )

        for r in state_rows:
            code = r["code"]
            phase = r.get("phase_code")
            label = r.get("intersection_phase")
            note = (r.get("invalid_reason") or r.get("data_block_reason")
                    or r.get("entry_block_reason") or r.get("pre_exit_reason")
                    or r.get("pre_note") or r.get("gate_failure_text") or "")
            allow = "是" if r.get("new_entry_allowed") else "否"
            print(f"{hhmm:<7} {code:<7} {str(r.get('name') or ''):<6} "
                  f"{label + '(' + phase + ')':<28} {allow:<4} {note}")
            history.setdefault(code, []).append((hhmm, phase, r))

        # 13:44 后模拟重启：保存 → 清内存 → 重新加载
        if hhmm == "13:44":
            screen.save_intersection_state(prev_state, "2026-07-28")
            reloaded = screen.load_intersection_state()
            restored = reloaded.get("items") or {}
            print("-" * 100)
            print("【模拟重启】保存状态 → 重新加载：")
            check(reloaded.get("date") == "2026-07-28", "重启后交易日恢复正确")
            gj = restored.get("603118") or {}
            check(screen._canonical_phase(gj.get("phase")) == screen.PHASE_RETEST_READY,
                  f"共进股份重启后恢复 RETEST_READY（实际 {gj.get('phase')}）")
            check(bool(gj.get("first_intersection_at")) and bool(gj.get("expires_at")),
                  "重启后锁存时间戳/过期时间完整保留")
            prev_state = restored
            print("-" * 100)

    # ── 验收断言 ──────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("验收断言")
    print("=" * 100)

    def phases(code):
        return [(t, p) for t, p, _ in history.get(code, [])]

    def rows(code):
        return {t: r for t, _, r in history.get(code, [])}

    # 1. 云图控股
    yt = dict(phases("002539"))
    yt_rows = rows("002539")
    check(yt.get("13:39") == screen.PHASE_OBSERVING, "云图 13:39 观察中（连续确认1/2，防单快照噪声）")
    check(yt.get("13:41") == screen.PHASE_PRE, "云图 13:41 进准交集")
    check(yt.get("13:42") == screen.PHASE_PRE and yt.get("13:44") == screen.PHASE_PRE,
          "云图 13:42–13:44 维持准交集")
    check(yt.get("13:46") == screen.PHASE_OBSERVING, "云图 13:46 退出准交集（5分钟资金转负）")
    check("5分钟资金未为正" in (yt_rows.get("13:46", {}).get("pre_exit_reason") or ""),
          f"云图退出原因含实际数值：{yt_rows.get('13:46', {}).get('pre_exit_reason')}")
    check(all(p not in (screen.PHASE_ENTRY, screen.PHASE_RETEST_READY) for _, p in phases("002539")),
          "云图全程不产生买点")
    check(all(not r.get("new_entry_allowed") for r in yt_rows.values()),
          "云图 watch_risk：全程不允许新开仓（可预警可观察）")

    # 2. 星帅尔
    check(all(p == screen.PHASE_OBSERVING for _, p in phases("002860")),
          "星帅尔始终观察中，不得进准交集（板块不共振+换手过高）")
    xs_notes = [r.get("gate_failure_text") or "" for r in rows("002860").values()]
    check(all("板块未共振" in n for n in xs_notes), "星帅尔失败原因均为门槛描述（板块未共振）")

    # 3. 中原内配
    check(all(p == screen.PHASE_OBSERVING for _, p in phases("002448")),
          "中原内配始终观察中（主力资金为负不得进准交集）")
    zy_note = list(rows("002448").values())[0].get("gate_failure_text") or ""
    check("主力净占比未为正" in zy_note, f"中原内配失败原因含实际数值：{zy_note}")

    # 4. 比音勒芬
    by = dict(phases("002832"))
    check(by.get("13:43") == screen.PHASE_LATE, "比音勒芬首次交集过热 → LATE_INTERSECTION")
    check(all(p == screen.PHASE_LATE for _, p in phases("002832")), "比音勒芬保持迟到交集，不产生买点")

    # 5. 分钟K过期硬否决
    gj = dict(phases("603118"))
    gj_rows = rows("603118")
    check(gj.get("13:39") == screen.PHASE_LATCHED, "共进 13:39 首次交集（只记录启动事件）")
    check(gj.get("13:41") == screen.PHASE_WAIT_RETEST, "共进 13:41 等待回踩（禁止同轮跳级）")
    check(gj.get("13:42") == screen.PHASE_WAIT_RETEST and gj_rows["13:42"].get("minute_data_stale"),
          "共进 13:42 分钟K过期(400s)：不得推进，报告标记 minute_data_stale")
    check("分钟K过期" in (gj_rows["13:42"].get("data_block_reason") or ""),
          f"共进 13:42 阻断原因：{gj_rows['13:42'].get('data_block_reason')}")
    check(gj.get("13:44") == screen.PHASE_RETEST_READY, "共进 13:44 分钟K恢复新鲜 → 回踩确认(第1次)")
    check(gj.get("13:46") == screen.PHASE_ENTRY, "共进 13:46 第2次确认(不同snapshot) → ENTRY_ELIGIBLE(14:20前)")

    # 6. 市场宽度<42% → CASH：所有新开仓资格必须为否
    cash_rows = [r for code in history for t, p, r in history[code] if t == "13:48"]
    check(len(cash_rows) > 0 and all(not r.get("new_entry_allowed") for r in cash_rows),
          f"13:48 CASH 环境：全部 {len(cash_rows)} 只标的新开仓资格=否")
    check(all(r.get("market_mode") == "CASH" for r in cash_rows), "13:48 所有行标记 market_mode=CASH")

    # 8. 连续确认使用不同 snapshot_id
    check(gj_rows["13:46"].get("consecutive_confirmations") == 2,
          "共进两次确认计数=2（不同 snapshot_id，同快照不重复计数）")

    print("\n" + "=" * 100)
    if FAILURES:
        print(f"验收失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        raise SystemExit(1)
    print("全部验收断言通过 ✓（状态机仅读取当前快照+持久化历史，无未来数据）")


if __name__ == "__main__":
    run()
