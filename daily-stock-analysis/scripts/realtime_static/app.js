"use strict";

// ── State ──────────────────────────────────────────
let currentTab = "intersection";
let lastData = null;
let pollTimer = null;
let statusTimer = null;

const POLL_INTERVAL = 10000; // 10s
const STATUS_INTERVAL = 5000; // 5s

// ── Helpers ────────────────────────────────────────
function fmtPct(val, digits = 2) {
  if (val == null || val === "" || isNaN(val)) return "-";
  return Number(val).toFixed(digits) + "%";
}
function fmtPrice(val) {
  if (val == null || val === "" || isNaN(val)) return "-";
  return Number(val).toFixed(2);
}
function fmtAmount(val) {
  if (val == null || val === "" || isNaN(val)) return "-";
  const yi = val / 1e8;
  if (Math.abs(yi) >= 1) return yi.toFixed(2) + "亿";
  return (val / 1e4).toFixed(0) + "万";
}
function fmtFlow(val) {
  if (val == null || val === "" || isNaN(val)) return "-";
  const yi = val / 1e8;
  if (Math.abs(yi) >= 1) return yi.toFixed(2) + "亿";
  const wan = val / 1e4;
  if (Math.abs(wan) >= 1) return wan.toFixed(0) + "万";
  return val.toFixed(0);
}
// 量能倍数：相对过去N根均量倍数，>=2 标记"突然爆量"
function fmtVolSurge(val) {
  if (val == null || val === "" || isNaN(val)) return "-";
  const v = Number(val);
  const color = v >= 3.0 ? "#ff4d4f" : v >= 2.0 ? "#ff7a45" : "inherit";
  const weight = v >= 2.0 ? "700" : "400";
  return `<span style="color:${color};font-weight:${weight}">${v.toFixed(2)}x</span>${v >= 2.0 ? " 🔥" : ""}`;
}
// 5分钟成交额增量
function fmtAmtInc(val) {
  if (val == null || val === "" || isNaN(val)) return "-";
  return fmtAmount(val);
}
function fmtRatio(val, digits = 1) {
  if (val == null || val === "" || isNaN(val)) return "-";
  return (val * 100).toFixed(digits) + "%";
}
function isNumber(val) {
  return val != null && val !== "" && !isNaN(Number(val));
}
function colorChange(val) {
  if (val == null || isNaN(val)) return "";
  if (val > 0) return "up";
  if (val < 0) return "down";
  return "flat";
}
function flowBadge(status) {
  if (!status) return '<span class="badge badge-dim">-</span>';
  const m = {
    "有效流入": "badge-good",
    "疑似流入": "badge-maybe",
    "价量背离": "badge-warn",
    "疑似派发": "badge-bad",
    "数据不足": "badge-dim",
  };
  return `<span class="badge ${m[status] || "badge-dim"}">${status}</span>`;
}
function riskBadge(risk) {
  if (!risk) return '<span class="badge badge-dim">-</span>';
  const m = {
    clean: "badge-good",
    watch_risk: "badge-maybe",
    avoid: "badge-bad",
    unknown: "badge-dim",
  };
  const label = { clean: "clean", watch_risk: "watch", avoid: "avoid", unknown: "unknown" }[risk] || risk;
  return `<span class="badge ${m[risk] || "badge-dim"}">${label}</span>`;
}
function stateBadge(state) {
  if (!state) return "-";
  const m = {
    "准交集": "badge-info",
    "等待转强": "badge-warn",
    "首次交集": "badge-teal",
    "等待回踩": "badge-teal",
    "回踩确认": "badge-good",
    "可新开仓": "badge-good",
    "可试错": "badge-good",
    "迟到交集": "badge-bad",
    "失效": "badge-bad",
    "已过期": "badge-dim",
    "观察中": "badge-dim",
    "新出现交集": "badge-info",
    "连续确认中": "badge-teal",
    "上午资金买点": "badge-good",
    "午后新启动买点": "badge-good",
    "午后滞后信号": "badge-warn",
    "信号已过热": "badge-bad",
    "交集但不合格": "badge-bad",
  };
  return `<span class="badge ${m[state] || "badge-dim"}">${state}</span>`;
}
function eligibleBadge(val) {
  return val
    ? '<span class="badge badge-good">可开仓</span>'
    : '<span class="badge badge-dim">否</span>';
}
function classBadge(cls) {
  if (!cls) return "-";
  const m = { A: "badge-good", B: "badge-maybe", C: "badge-bad" };
  return `<span class="badge ${m[cls] || "badge-dim"}">${cls}</span>`;
}

// ── 进出场建议渲染 ──
function renderEntryExit(v, row) {
  if (!row.stop_loss) return "-";
  const sl = row.stop_loss;
  const slPct = row.stop_loss_pct;
  const tp1 = row.take_profit_1;
  const tp2 = row.take_profit_2;
  const rr = row.rr_ratio;
  return `<span class="entry-exit">止损<span class="sl">${sl}</span>(${slPct}%) → 止盈<span class="tp">${tp1}</span>/<span class="tp">${tp2}</span> <span class="rr">RR${rr || "-"}</span></span>`;
}

// ── 警告标签渲染 ──
function renderWarn(v, row) {
  if (!v) return "";
  const isDanger = v.includes("诱多") || v.includes("出货");
  return `<span class="warn-tag${isDanger ? " danger" : ""}">${v}</span>`;
}

// ── 日内形态（四价位纯本地计算，零新增请求）──
function renderIntradayPattern(v, row) {
  const o = row.open, pc = row.prev_close, p = row.price, h = row.high;
  if (!o || !pc || !p || !h) return "-";
  const openPct = (o / pc - 1) * 100;   // 今开 vs 昨收
  const fromOpen = (p / o - 1) * 100;   // 现价 vs 今开
  const distHigh = ((h - p) / h) * 100; // 距最高回落
  let label, cls;
  if (openPct >= 1) {
    if (fromOpen <= -1) { label = "高开回落"; cls = "badge-bad"; }
    else if (distHigh > 3) { label = "冲高回落"; cls = "badge-warn"; }
    else if (fromOpen >= 0.5) { label = "高开高走"; cls = "badge-good"; }
    else { label = "高开震荡"; cls = "badge-maybe"; }
  } else if (openPct <= -1) {
    if (p > pc) { label = "低开反包"; cls = "badge-teal"; }
    else if (fromOpen >= 0.5) { label = "低开修复"; cls = "badge-info"; }
    else { label = "低开低走"; cls = "badge-bad"; }
  } else {
    if (distHigh > 3) { label = "冲高回落"; cls = "badge-warn"; }
    else if (fromOpen >= 1) { label = "平开拉升"; cls = "badge-good"; }
    else if (fromOpen <= -1) { label = "平开走弱"; cls = "badge-bad"; }
    else { label = "横盘震荡"; cls = "badge-dim"; }
  }
  return `<span class="badge ${cls}" title="开盘${openPct.toFixed(1)}% 盘中${fromOpen >= 0 ? "+" : ""}${fromOpen.toFixed(1)}% 距高${distHigh.toFixed(1)}%">${label}</span>`;
}
function fmtOpenPct(v, row) {
  const o = row.open, pc = row.prev_close;
  if (!o || !pc) return "-";
  const p = (o / pc - 1) * 100;
  return `<span class="${colorChange(p)}">${(p >= 0 ? "+" : "") + p.toFixed(2)}%</span>`;
}
function fmtDistHigh(v, row) {
  const h = row.high, p = row.price;
  if (!h || !p) return "-";
  const d = ((h - p) / h) * 100;
  const cls = d <= 1 ? "up" : d >= 3 ? "down" : "";
  return `<span class="${cls}">-${d.toFixed(2)}%</span>`;
}

// ── 5分钟量能（closed口径；hover 显示全部字段）──
function _fmtVolHand(v) {
  if (v == null || isNaN(v)) return "-";
  return v >= 10000 ? (v / 10000).toFixed(1) + "万手" : Math.round(v) + "手";
}
function _min5Title(row) {
  const m = row.min5;
  if (!m) return "";
  const c = m.closed_5m || {};
  const cur = c.cur || {};
  const parts = [
    `5分量:${_fmtVolHand(cur.vol)}`,
    `前5分量:${_fmtVolHand(c.prev_vol)}`,
    `30分均量:${_fmtVolHand(c.avg5_vol_30m)}`,
    `量能比:${c.vol_ratio_5m != null ? c.vol_ratio_5m : "-"}`,
    `5分K:${cur.open ?? "-"} / ${cur.high ?? "-"} / ${cur.low ?? "-"} / ${cur.close ?? "-"}`,
    `VWAP:${cur.vwap ?? "-"}`,
    `数据:${m.bar_end ?? "-"}(${m.age_seconds ?? "?"}s前)${m.stale ? " ⚠️已失效" : ""}`,
  ];
  return parts.join("\n");
}
function fmtVolRatio5m(v, row) {
  const m = row.min5;
  if (!m || v == null || isNaN(v)) return "-";
  const stale = m.stale;
  let cls = "";
  if (v >= 1.5) cls = "up";
  else if (v <= 0.7) cls = "down";
  const txt = v.toFixed(2) + (v >= 1.5 ? " 🔥" : "");
  return `<span class="${stale ? "flat" : cls}" title="${_min5Title(row)}">${stale ? "⚠️" : ""}${txt}</span>`;
}
function fmtVwap5m(v, row) {
  const m = row.min5;
  if (!m || v == null || isNaN(v)) return "-";
  // 现价 vs 5分VWAP：上方红、下方绿
  const p = row.price;
  let cls = "";
  if (p != null && !isNaN(p)) cls = p >= v ? "up" : "down";
  return `<span class="${m.stale ? "flat" : cls}" title="${_min5Title(row)}">${v.toFixed(2)}</span>`;
}

// ── Merge intersection data ────────────────────────
function mergeIntersection(states, rawPool) {
  const rawMap = {};
  (rawPool || []).forEach((r) => {
    rawMap[r.code] = r;
  });
  return (states || []).map((s) => ({ ...(rawMap[s.code] || {}), ...s }));
}

// ── Column definitions per tab ─────────────────────
const COLS = {
  sticky: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: (v, row) => row.price != null ? `<span class="${colorChange(row.change)}">${row.price.toFixed(2)}</span>` : "-" },
    { k: "change", l: "涨幅", c: "num", r: (v, row) => row.change != null ? `<span class="${colorChange(row.change)}">${fmtPct(row.change)}</span>` : "-" },
    { k: "vol_ratio_5m", l: "5分量比", c: "num", r: (v, row) => fmtVolRatio5m(row.min5 && row.min5.closed_5m ? row.min5.closed_5m.vol_ratio_5m : null, row) },
    { k: "vwap_5m", l: "5分VWAP", c: "num", r: (v, row) => fmtVwap5m(row.min5 && row.min5.closed_5m && row.min5.closed_5m.cur ? row.min5.closed_5m.cur.vwap : null, row) },
    { k: "remaining", l: "剩余", c: "num", r: (v, row) => row.remaining != null ? `${Math.floor(row.remaining / 60)}分${row.remaining % 60}秒` : "持续" },
    { k: "source", l: "来源", r: (v, row) => {
        const s = row.source || "-";
        const color = s === "关注" ? "#4da3ff" : s === "超短池" ? "#e8a33d" : "#9aa0a6";
        return `<span style="color:${color};font-weight:600">${s}</span>`;
      } },
  ],

  // ── 低开洗盘：低开≥2% + 翻红 + 均价线上 + 当日主力净流入 + 20日持续净流入 ──
  "low-open": [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "low_open_pct", l: "低开%", c: "num", r: (v) => (isNumber(v) ? `<span class="warn">${Number(v).toFixed(2)}</span>` : "-") },
    { k: "main_net", l: "主力净流入", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "persistent_net", l: "20日累计净流入", c: "num", r: fmtFlow },
    { k: "flow_status", l: "资金状态", r: flowBadge },
  ],

  // ── 准交集预警：超短 + 差1项趋势条件 + 四道门槛 ──
  pre: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "intersection_phase", l: "相位", r: stateBadge },
    { k: "preintersection_missing", l: "缺失条件", r: (v) => v ? `<span class="flat">差${v}</span>` : "-" },
    { k: "gate_failure_text", l: "前置门槛", r: (v) => (!v || v === "全部通过") ? '<span class="badge badge-good">全部通过</span>' : `<span class="badge badge-bad">未过</span> <span class="flat">${v}</span>` },
    { k: "trigger_price", l: "预计触发价", c: "num", r: (v) => (isNumber(v) ? `<span class="warn">${Number(v).toFixed(2)}</span>` : "—") },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "resonance", l: "板块共振", r: (v) => v === "是" ? '<span class="badge badge-good">是</span>' : '<span class="badge badge-dim">否</span>' },
    { k: "risk_note", l: "公告风险", r: (v, row) => v ? `<span class="badge badge-warn">${v}</span>` : (row && row.risk_status === "clean" ? '<span class="badge badge-good">clean</span>' : `<span class="badge badge-dim">${(row && row.risk_status) || "-"}</span>`) },
  ],
  // ── 已触发·等待回踩：交集后不追，等缩量回踩 ──
  triggered: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "first_intersection_at", l: "交集时间", r: (v) => v || "-" },
    { k: "trigger_price", l: "触发价", c: "num", r: (v) => (isNumber(v) ? Number(v).toFixed(2) : "-") },
    { k: "trigger_vwap", l: "当时VWAP", c: "num", r: (v) => (isNumber(v) ? Number(v).toFixed(2) : "-") },
    { k: "trigger_flow_5m", l: "5分资金", c: "num", r: (v) => (isNumber(v) ? fmtFlow(v) : "-") },
    { k: "pullback_zone", l: "回踩观察区", r: (v) => v && v !== "-" ? `<span class="warn">${v}</span>` : "-" },
    { k: "intersection_phase", l: "相位", r: stateBadge },
    { k: "failure_reason", l: "有效性", r: (v) => (v && v !== "-" ? `<span class="badge badge-bad">失效</span>` : '<span class="badge badge-good">有效</span>') },
    { k: "actionable", l: "可新开仓", r: (v) => v ? '<span class="badge badge-good">可</span>' : '<span class="badge badge-dim">否</span>' },
  ],
  // ── 迟到交集：不追 ──
  late: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "trigger_price", l: "触发价", c: "num", r: (v) => (isNumber(v) ? Number(v).toFixed(2) : "-") },
    { k: "late_reason", l: "迟到原因", r: (v) => v && v !== "无" ? `<span class="flat">${v}</span>` : "-" },
  ],
  intersection: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "intersection_state", l: "交集状态", r: stateBadge },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "形态", c: "center", r: renderIntradayPattern },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "high", l: "距高%", c: "num", r: fmtDistHigh },
    { k: "vol_ratio_5m", l: "5分量比", c: "num", r: fmtVolRatio5m },
    { k: "vwap_5m", l: "5分VWAP", c: "num", r: fmtVwap5m },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "signal_age_minutes", l: "信号年龄", c: "num", r: (v) => (v != null ? v + "分" : "-") },
    { k: "confirm_count", l: "确认", c: "num", r: (v) => (v != null ? v + "次" : "-") },
    { k: "buy_deadline", l: "买点截止", r: (v) => v || "-" },
    { k: "new_open_eligible", l: "新开仓", r: eligibleBadge },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "rejection_reason", l: "拒绝原因", r: (v) => (v && v !== "无" ? `<span class="flat">${v}</span>` : "-") },
  ],
  ultra: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "形态", c: "center", r: renderIntradayPattern },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "high", l: "距高%", c: "num", r: fmtDistHigh },
    { k: "vol_ratio_5m", l: "5分量比", c: "num", r: fmtVolRatio5m },
    { k: "vwap_5m", l: "5分VWAP", c: "num", r: fmtVwap5m },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "volume_ratio", l: "量比", c: "num", r: (v) => (v != null ? v.toFixed(2) : "-") },
    { k: "industry", l: "板块" },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_5m_inc", l: "5分增量", c: "num", r: fmtFlow },
    { k: "vol_ratio_vs_hist", l: "量能倍数", c: "num", r: fmtVolSurge },
    { k: "amount_5m_inc", l: "5分额增", c: "num", r: fmtAmtInc },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  "trend-obs": [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "形态", c: "center", r: renderIntradayPattern },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "high", l: "距高%", c: "num", r: fmtDistHigh },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "industry", l: "板块" },
    { k: "ma_state", l: "均线状态" },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_5m_inc", l: "5分增量", c: "num", r: fmtFlow },
    { k: "vol_ratio_vs_hist", l: "量能倍数", c: "num", r: fmtVolSurge },
    { k: "amount_5m_inc", l: "5分额增", c: "num", r: fmtAmtInc },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  "trend-conf": [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "形态", c: "center", r: renderIntradayPattern },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "high", l: "距高%", c: "num", r: fmtDistHigh },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "industry", l: "板块" },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_5m_inc", l: "5分增量", c: "num", r: fmtFlow },
    { k: "vol_ratio_vs_hist", l: "量能倍数", c: "num", r: fmtVolSurge },
    { k: "amount_5m_inc", l: "5分额增", c: "num", r: fmtAmtInc },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  capital: [
    { k: "capital_class", l: "资金类", r: (v) => `<span class="badge badge-purple">${v || "-"}</span>` },
    { k: "pool_source", l: "来源" },
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "capital_score", l: "评分", c: "num", r: (v) => (v != null ? v.toFixed(1) : "-") },
    { k: "main_net", l: "主力净额", c: "num", r: fmtFlow },
    { k: "main_pct", l: "净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "super_net", l: "超大单", c: "num", r: fmtFlow },
    { k: "flow_5m_inc", l: "5分增量", c: "num", r: fmtFlow },
    { k: "vol_ratio_vs_hist", l: "量能倍数", c: "num", r: fmtVolSurge },
    { k: "amount_5m_inc", l: "5分额增", c: "num", r: fmtAmtInc },
    { k: "vwap_state", l: "均价线" },
    { k: "resonance", l: "共振" },
    { k: "capital_data", l: "数据完整度" },
    { k: "capital_reason", l: "评分依据" },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  flow: [
    { k: "code", l: "代码", c: "code", r: (v, row) => row._holding ? `${v} <span class="holding-tag">持仓</span>` : v },
    { k: "name", l: "名称", c: "name" },
    { k: "main_net", l: "主力净额", c: "num", r: (v) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${fmtFlow(v)}</span>` },
    { k: "main_pct", l: "净占比", c: "num", r: (v) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${fmtPct(v, 1)}</span>` },
    { k: "super_net", l: "超大单", c: "num", r: fmtFlow },
    { k: "big_net", l: "大单", c: "num", r: fmtFlow },
    { k: "mid_net", l: "中单", c: "num", r: fmtFlow },
    { k: "small_net", l: "小单", c: "num", r: fmtFlow },
    { k: "flow_5m_inc", l: "5分增量", c: "num", r: fmtFlow },
    { k: "vol_ratio_vs_hist", l: "量能倍数", c: "num", r: fmtVolSurge },
    { k: "amount_5m_inc", l: "5分额增", c: "num", r: fmtAmtInc },
    { k: "flow_15m_inc", l: "15分增量", c: "num", r: fmtFlow },
    { k: "vwap_state", l: "均价线" },
    { k: "industry", l: "板块" },
    { k: "flow_status", l: "结论", r: flowBadge },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  "low-ultra": [
    { k: "class", l: "类", r: classBadge },
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "形态", c: "center", r: renderIntradayPattern },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "high", l: "距高%", c: "num", r: fmtDistHigh },
    { k: "vol_ratio_5m", l: "5分量比", c: "num", r: fmtVolRatio5m },
    { k: "vwap_5m", l: "5分VWAP", c: "num", r: fmtVwap5m },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "volume_ratio", l: "量比", c: "num", r: (v) => (v != null ? v.toFixed(2) : "-") },
    { k: "industry", l: "板块" },
    { k: "resonance", l: "共振" },
    { k: "high_pull", l: "高位回落", c: "num", r: (v) => (v != null ? v.toFixed(2) + "pct" : "-") },
    { k: "vwap_state", l: "均价线" },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "risk", l: "风险" },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  "low-trend": [
    { k: "class", l: "类", r: classBadge },
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "形态", c: "center", r: renderIntradayPattern },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "high", l: "距高%", c: "num", r: fmtDistHigh },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "industry", l: "板块" },
    { k: "ma_state", l: "均线状态" },
    { k: "five_ret", l: "近5日", c: "num", r: fmtRatio },
    { k: "ma20_dist", l: "距20日线", c: "num", r: fmtRatio },
    { k: "high_pull", l: "高位回落", c: "num", r: (v) => (v != null ? v.toFixed(2) + "pct" : "-") },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "risk", l: "风险" },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "stop_loss", l: "进出场", r: renderEntryExit },
    { k: "warn", l: "警告", r: renderWarn },
  ],
  watchlist: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "当前价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "industry", l: "板块" },
    { k: "structure", l: "结构" },
    { k: "trigger", l: "触发价" },
    { k: "buy_zone", l: "低吸区" },
    { k: "invalid", l: "失效" },
    { k: "no_chase", l: "追高禁区" },
    { k: "reason", l: "理由" },
    { k: "announcement_risk", l: "公告", r: riskBadge },
  ],
  "low-open": [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "low_open_pct", l: "低开%", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v, 1)}</span>` },
    { k: "open", l: "今开", c: "num", r: fmtPrice },
    { k: "prev_close", l: "昨收", c: "num", r: fmtPrice },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "turnover", l: "换手率", c: "num", r: fmtPct },
    { k: "amount", l: "成交额", c: "num", r: fmtAmount },
    { k: "industry", l: "板块" },
    { k: "vwap_state", l: "均价线" },
    { k: "main_pct", l: "主力净占比", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "persistent_net", l: "20日累计净流入", c: "num", r: fmtAmount },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "risk", l: "风险" },
    { k: "announcement_risk", l: "公告", r: riskBadge },
  ],
  sectors: [
    { k: "name", l: "板块" },
    { k: "change", l: "涨跌%", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "up_down", l: "涨/跌", c: "center", r: (v, row) => `${row.up_count || 0}↑${row.down_count || 0}↓` },
    { k: "source", l: "来源" },
  ],
};

const TAB_TITLES = {
  intersection: "双池交集（超短池 ∩ 趋势确认，启动事件）",
  pre: "准交集预警（提前观察）",
  triggered: "已触发·等待回踩（买点候选）",
  late: "迟到交集（不追）",
  ultra: "超短池",
  "trend-obs": "趋势观察池",
  "trend-conf": "趋势确认池",
  capital: "主力资金优选",
  flow: "重点候选资金追踪",
  "low-ultra": "低吸超短线 A/B/C",
  "low-trend": "低吸短线趋势 A/B/C",
  watchlist: "明日观察池",
  sectors: "相关板块指数",
  sticky: "跟踪中（候选黏性）",
  "low-open": "低开洗盘（低开≥2%+翻红+均价线上+当日主力净流入+20日持续净流入）",
};

const TAB_NOTES = {
  intersection: "注：交集仅代表启动确认，不是买点；真正买点由交集后的缩量回踩产生（见下方状态机）。",
  pre: "超短池 + 距趋势确认仅差1项 + 四道门槛（主力净流入/5分资金正/均价线上/板块共振）全部通过才是准交集。门槛未过的标为「观察中」并列出未通过项；公告风险非clean者仅观察，不给新开仓资格。",
  triggered: "交集信号锁存15分钟：不立即追，等从触发价缩量回踩0.5%–1.5%、5分资金仍正、不破VWAP，方为买点（回踩确认/可试错）。",
  late: "首次交集即过热（涨幅>4.6%/距VWAP>1.2%/换手>7%/脉冲大阳/高位回撤>1.5%/无共振），已标记迟到，不提供买点。",
  "trend-obs": "趋势观察池比严格趋势池宽一些，避免大跌或修复行情中趋势池完全空掉。",
  sticky: "进入过超短池/自选的股票，退出候选池后仍跟踪 15 分钟，便于继续验证买墙后续与量能。人工关注的股票持续跟踪。",
  "low-open": "低开洗盘：低开≥2% 且开盘翻红站上均价线 + 当日主力净流入为正 + 20日主力持续净流入（按会话累计资金流验证）；匹配「恐慌日逆势吸筹」型主力票。",
};

const TAB_DATA_KEY = {
  intersection: (d) => mergeIntersection(d.intersection_states, d.dual_pool_raw),
  pre: (d) => {
    // 准交集/等待转强在前；门槛未过的"观察中"也展示（透明输出未通过门槛），排在后面
    const rows = d.pre_intersection || [];
    const rank = { "准交集": 0, "等待转强": 1, "观察中": 2 };
    return rows.slice().sort((a, b) => (rank[a.intersection_phase] ?? 9) - (rank[b.intersection_phase] ?? 9));
  },
  triggered: (d) => (d.intersection_states || []).filter((r) => !r.late_flag && ["首次交集", "等待回踩", "回踩确认", "可新开仓", "可试错"].includes(r.intersection_phase)),
  late: (d) => (d.intersection_states || []).filter((r) => r.late_flag),
  ultra: (d) => d.strict_ultra || [],
  "trend-obs": (d) => d.trend_observation || [],
  "trend-conf": (d) => d.strict_trend || [],
  capital: (d) => d.capital_rank || [],
  flow: (d) => d.flow_detail || [],
  "low-ultra": (d) => d.low_ultra || [],
  "low-trend": (d) => d.low_trend || [],
  watchlist: (d) => d.watchlist || [],
  sectors: (d) => d.sector_indices || [],
  sticky: (d) => d.sticky_tracking || [],
  "low-open": (d) => d.low_open_wash || [],
};

// ── Render ──────────────────────────────────────────
function renderTable(data, tabName) {
  const cols = COLS[tabName];
  if (!cols) return '<div class="placeholder">未知标签页</div>';

  const rows = TAB_DATA_KEY[tabName] ? TAB_DATA_KEY[tabName](data) : [];

  let html = `<div class="section-title">${TAB_TITLES[tabName] || tabName}</div>`;
  if (TAB_NOTES[tabName]) {
    html += `<div class="section-note">${TAB_NOTES[tabName]}</div>`;
  }
  html += `<div>共 ${rows.length} 条</div>`;

  if (rows.length === 0) {
    html += '<div class="placeholder">无数据</div>';
    return html;
  }

  html += '<table class="screen-table"><thead><tr>';
  for (const col of cols) {
    html += `<th class="${col.c || ""}">${col.l}</th>`;
  }
  html += "</tr></thead><tbody>";

  for (const row of rows) {
    html += "<tr>";
    for (const col of cols) {
      const val = row[col.k];
      const rendered = col.r ? col.r(val, row) : (val != null && val !== "" ? String(val) : "-");
      html += `<td class="${col.c || ""}">${rendered}</td>`;
    }
    html += "</tr>";
  }

  html += "</tbody></table>";
  return html;
}

function renderMarketPanel(data) {
  const meta = data.meta || {};
  const breadth = data.breadth || {};

  document.getElementById("mp-timestamp").textContent = meta.timestamp || "-";
  document.getElementById("mp-source").textContent = meta.source || "-";

  const adv = breadth.adv || 0;
  const dec = breadth.dec || 0;
  document.getElementById("mp-breadth").innerHTML =
    `<span class="up">${adv}涨</span> / <span class="down">${dec}跌</span>`;

  const indices = data.indices || [];
  const idxHtml = indices
    .map((idx) => {
      const chg = idx.change;
      const cls = chg > 0 ? "up" : chg < 0 ? "down" : "flat";
      return `<span class="idx-item"><span class="idx-name">${idx.name || "-"}</span> <span class="idx-change ${cls}">${chg != null ? chg.toFixed(2) + "%" : "-"}</span></span>`;
    })
    .join("");
  document.getElementById("mp-indices").innerHTML = idxHtml;

  const warnings = data.warnings || [];
  const fetchStatus = data.market_fetch_status || {};
  const warnItems = [];
  if (fetchStatus.source === "sina_fallback") {
    warnItems.push("已切换新浪备用源，部分功能降级");
  }
  if (fetchStatus.complete === false) {
    warnItems.push("行情快照不完整");
  }
  for (const w of warnings) {
    warnItems.push(w);
  }
  if (data.announcement_check_available === false && !data.meta?.source?.includes("公告已跳过")) {
    const unknownCount = (data.announcement_unknown_codes || []).length;
    if (unknownCount > 0) {
      warnItems.push(`公告检查不可用，${unknownCount} 只标记为 unknown`);
    }
  }
  document.getElementById("mp-warnings").innerHTML = warnItems
    .map((w) => `<span class="mp-warning-item">${w}</span>`)
    .join("");

  renderThermometer(data);
}

function renderThermometer(data) {
  const t = data.market_thermometer;
  const el = document.getElementById("thermometer");
  if (!t) { el.style.display = "none"; return; }

  el.style.display = "flex";
  const badge = document.getElementById("therm-badge");
  const levelMap = { strong: "强势", normal: "中性", caution: "谨慎", danger: "危险" };
  badge.textContent = levelMap[t.risk_level] || t.risk_level;
  badge.className = "therm-badge " + (t.risk_level || "normal");

  document.getElementById("therm-limit-up").textContent = t.limit_up ?? "-";
  document.getElementById("therm-limit-down").textContent = t.limit_down ?? "-";
  document.getElementById("therm-adv-dec").textContent =
    t.adv_dec_ratio === Infinity ? "∞" : (t.adv_dec_ratio ?? "-");
  document.getElementById("therm-idx").textContent =
    `${t.index_up ?? 0}涨 / ${t.index_down ?? 0}跌`;
  document.getElementById("therm-msg").textContent = t.risk_msg || "";
}

function renderFooter(data) {
  const cfg = data.intersection_config || {};
  const meta = data.intersection_config_meta || {};
  const parts = [];
  if (cfg.version) parts.push(`参数版本: ${cfg.version}`);
  if (cfg.source) parts.push(`来源: ${cfg.source}`);
  if (cfg.confirmation_snapshots) parts.push(`确认快照: ${cfg.confirmation_snapshots}次`);
  if (cfg.morning_cutoff) parts.push(`上午截止: ${cfg.morning_cutoff}`);
  if (cfg.afternoon_buy_deadline) parts.push(`午后买点截止: ${cfg.afternoon_buy_deadline}`);
  const cacheStats = meta.kline_cache_stats || data.meta?.kline_cache_stats;
  if (cacheStats) {
    parts.push(`K线缓存: ${cacheStats.cache_hit_count || 0}命中/${cacheStats.fetch_count || 0}请求 (${cacheStats.cache_size || 0}条)`);
  }
  if (data.meta?.elapsed_seconds) {
    parts.push(`耗时: ${data.meta.elapsed_seconds}s`);
  }
  document.getElementById("footer").textContent = parts.join(" | ");
}

function renderCounts(data) {
  const counts = {
    intersection: (data.intersection_states || []).length,
    pre: (data.pre_intersection || []).filter((r) => r.intersection_phase === "准交集" || r.intersection_phase === "等待转强").length,
    // 注：计数只统计真正进入预警的（准交集/等待转强）；观察中的行仍在表内展示但不计数
    triggered: (data.intersection_states || []).filter((r) => !r.late_flag && ["首次交集", "等待回踩", "回踩确认", "可新开仓", "可试错"].includes(r.intersection_phase)).length,
    late: (data.intersection_states || []).filter((r) => r.late_flag).length,
    ultra: (data.strict_ultra || []).length,
    "trend-obs": (data.trend_observation || []).length,
    "trend-conf": (data.strict_trend || []).length,
    capital: (data.capital_rank || []).length,
    flow: (data.flow_detail || []).length,
    "low-ultra": (data.low_ultra || []).length,
    "low-trend": (data.low_trend || []).length,
    watchlist: (data.watchlist || []).length,
    sectors: (data.sector_indices || []).length,
    sticky: (data.sticky_tracking || []).length,
    "low-open": (data.low_open_wash || []).length,
  };
  for (const [tab, count] of Object.entries(counts)) {
    const el = document.getElementById("cnt-" + tab);
    if (el) el.textContent = count || "";
  }
  // Group totals
  const shortTotal = counts.intersection + counts.ultra + counts["trend-obs"] + counts["trend-conf"];
  const capitalTotal = counts.capital + counts.flow;
  const lowTotal = counts["low-ultra"] + counts["low-trend"] + counts.watchlist;
  const shortEl = document.getElementById("cnt-short");
  const capEl = document.getElementById("cnt-capital-group");
  const lowEl = document.getElementById("cnt-low-group");
  if (shortEl) shortEl.textContent = shortTotal || "";
  if (capEl) capEl.textContent = capitalTotal || "";
  if (lowEl) lowEl.textContent = lowTotal || "";
}

function renderData(data) {
  if (!data || data.error) {
    const container = document.getElementById("table-container");
    container.innerHTML = `<div class="error-msg">${data ? data.error : "无数据"}</div>`;
    return;
  }
  lastData = data;
  renderMarketPanel(data);
  renderCounts(data);
  renderFooter(data);

  const wideScreen = window.innerWidth >= 1800;
  if (wideScreen) {
    // Multi-panel layout: show all tabs simultaneously
    const container = document.getElementById("table-container");
    container.className = "multi-panel";
    const tabs = ["intersection", "pre", "triggered", "late", "low-open", "ultra", "trend-obs", "trend-conf", "capital", "flow", "low-ultra", "low-trend", "watchlist", "sectors", "sticky"];
    let html = "";
    for (const tab of tabs) {
      const rows = TAB_DATA_KEY[tab] ? TAB_DATA_KEY[tab](data) : [];
      html += `<div class="panel" data-tab="${tab}">${renderTable(data, tab)}</div>`;
    }
    container.innerHTML = html || '<div class="placeholder">无数据</div>';
  } else {
    // Single-tab layout
    const container = document.getElementById("table-container");
    container.className = "";
    container.innerHTML = renderTable(data, currentTab);
  }
}

function updateStatus(status) {
  const el = document.getElementById("market-status");
  el.classList.remove("trading", "closed", "running", "error");

  if (status.proxy_unavailable && !status.is_running && !status.is_prewarming) {
    el.classList.add("error");
    el.textContent = "代理断开";
  } else if (status.is_prewarming) {
    el.classList.add("running");
    const p = status.prewarm_progress || {};
    el.textContent = `预热中 ${p.done || 0}/${p.total || 0} (${p.failed || 0}失败)`;
  } else if (status.is_running) {
    el.classList.add("running");
    el.textContent = "筛选中...";
  } else if (status.has_result) {
    if (status.data_mode === "degraded") {
      el.classList.add("closed", "degraded");
      el.textContent = "降级数据";
    } else if (status.data_mode === "snapshot") {
      el.classList.add("closed", "snapshot");
      el.textContent = "最近快照";
    } else if (status.is_trading_hours) {
      el.classList.add("trading");
      el.textContent = "交易中";
    } else {
      el.classList.add("closed");
      el.textContent = "已收盘";
    }
  } else {
    el.classList.add("closed");
    el.textContent = "等待数据";
  }

  document.getElementById("last-refresh").textContent =
    status.last_run_time ? `上次: ${status.last_run_time.split(" ")[1] || status.last_run_time}` : "";
  document.getElementById("elapsed-time").textContent =
    status.last_run_duration ? `耗时${status.last_run_duration}s` : "";

  // 下一次刷新时间
  const nextEl = document.getElementById("next-refresh");
  if (status.next_refresh_time) {
    if (status.next_refresh_time === "运行中") {
      nextEl.textContent = "刷新中…";
      nextEl.classList.add("next-running");
    } else if (status.next_is_trading_open) {
      nextEl.textContent = `下次开盘: ${status.next_refresh_time.split(" ")[1] || status.next_refresh_time}`;
      nextEl.classList.remove("next-running");
    } else {
      nextEl.textContent = `下次刷新: ${status.next_refresh_time.split(" ")[1] || status.next_refresh_time}`;
      nextEl.classList.remove("next-running");
    }
    nextEl.style.display = "";
  } else {
    nextEl.textContent = "";
    nextEl.style.display = "none";
  }

  // 行情快照完整度提示（东财部分页失败但非降级时）
  const fwEl = document.getElementById("fetch-warn");
  if (status.market_fetch_complete === false && !status.market_data_degraded) {
    const fp = status.failed_pages || [];
    fwEl.textContent = `⚠ 行情不完整(缺${fp.length}页·局部快照)`;
    fwEl.style.display = "";
  } else {
    fwEl.textContent = "";
    fwEl.style.display = "none";
  }

  const cacheEl = document.getElementById("cache-info");
  if (status.is_prewarming) {
    const p = status.prewarm_progress || {};
    cacheEl.textContent = `预热 ${p.done || 0}/${p.total || 0}`;
  } else if (status.is_running) {
    cacheEl.textContent = "运行中";
  } else {
    cacheEl.textContent = status.is_trading_hours ? "交易时段" : "非交易时段";
  }

  // Show md path in footer
  if (status.md_path) {
    const footer = document.getElementById("footer");
    const mdShort = status.md_path.split("/").slice(-2).join("/");
    const existing = footer.textContent;
    if (!existing.includes("MD:")) {
      footer.textContent = existing + (existing ? " | " : "") + `MD: ${mdShort}`;
    }
  }

  // 冷却状态
  const cdBtn = document.getElementById("clear-cooldown-btn");
  if (cdBtn) {
    if (status.em_in_cooldown) {
      cdBtn.classList.add("btn-warn");
      cdBtn.textContent = "清除冷却(冷却中)";
    } else {
      cdBtn.classList.remove("btn-warn");
      cdBtn.textContent = "清除冷却";
    }
  }

  updateSnapshotBanner(status);
}

// ── API calls ──────────────────────────────────────
async function fetchData() {
  try {
    const resp = await fetch("/api/data");
    const data = await resp.json();
    renderData(data);
  } catch (e) {
    console.error("fetch data error:", e);
  }
}

async function fetchStatus() {
  try {
    const resp = await fetch("/api/status");
    const status = await resp.json();
    updateStatus(status);
  } catch (e) {
    console.error("fetch status error:", e);
  }
}

async function triggerRefresh(force = false) {
  const btn = document.getElementById(force ? "force-refresh-btn" : "refresh-btn");
  const other = document.getElementById(force ? "refresh-btn" : "force-refresh-btn");
  btn.disabled = true;
  other.disabled = true;
  btn.textContent = "刷新中...";
  other.textContent = "刷新中...";
  try {
    await fetch("/api/refresh" + (force ? "?force=1" : ""), { method: "POST" });
    // Poll status until done
    const checkInterval = setInterval(async () => {
      const resp = await fetch("/api/status");
      const status = await resp.json();
      updateStatus(status);
      if (!status.is_running && !status.is_prewarming) {
        clearInterval(checkInterval);
        btn.disabled = false;
        other.disabled = false;
        btn.textContent = force ? "强制刷新" : "立即刷新";
        other.textContent = force ? "强制刷新" : "立即刷新";
        fetchData();
      }
    }, 3000);
  } catch (e) {
    btn.disabled = false;
    other.disabled = false;
    btn.textContent = force ? "强制刷新" : "立即刷新";
    other.textContent = force ? "强制刷新" : "立即刷新";
  }
}

async function clearCooldown() {
  const btn = document.getElementById("clear-cooldown-btn");
  btn.disabled = true;
  try {
    await fetch("/api/clear_cooldown", { method: "POST" });
    await fetchStatus();
  } catch (e) {
    console.error("clear cooldown error:", e);
  } finally {
    btn.disabled = false;
  }
}

function updateSnapshotBanner(status) {
  const banner = document.getElementById("snapshot-banner");
  if (!banner) return;
  if (status.proxy_unavailable) {
    banner.style.display = "block";
    banner.className = "banner banner-error";
    if (status.preserved_from) {
      banner.textContent = `⚠️ 代理不可用：未检测到可用代理端口（东方财富接口直连被封），已为你保留最近一次完整筛选快照（来源 ${status.preserved_from}）。请先连通代理（Clash / SS / privoxy 等），看板会自动恢复实时筛选。`;
    } else {
      banner.textContent = "⚠️ 代理不可用：未检测到可用代理端口（东方财富接口直连被封），且当前无有效快照可保留。请先连通代理（Clash / SS / privoxy 等），看板会自动恢复实时筛选。";
    }
    return;
  }
  if (status.data_mode === "snapshot" && status.preserved_from) {
    banner.style.display = "block";
    banner.className = "banner banner-snapshot";
    banner.textContent = `行情数据源降级，已为你保留最近一次完整筛选（来源 ${status.preserved_from}）；如需查看降级实时数据请点「强制刷新」。`;
  } else if (status.data_mode === "degraded") {
    banner.style.display = "block";
    banner.className = "banner banner-degraded";
    banner.textContent = `行情数据不完整/降级（仅供参考）；超短池 / 双池 / 低吸 / 资金流在降级模式下不可用。`;
  } else {
    banner.style.display = "none";
  }
}

async function triggerPrewarm() {
  const btn = document.getElementById("prewarm-btn");
  btn.disabled = true;
  btn.textContent = "预热中...";
  try {
    await fetch("/api/prewarm", { method: "POST" });
    // Poll status until done
    const checkInterval = setInterval(async () => {
      const resp = await fetch("/api/status");
      const status = await resp.json();
      updateStatus(status);
      if (!status.is_prewarming && !status.is_running) {
        clearInterval(checkInterval);
        btn.disabled = false;
        btn.textContent = "预热K线";
        fetchData();
      }
    }, 3000);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "预热K线";
  }
}

async function updateSettings() {
  const settings = {
    skip_announcements: !document.getElementById("skip-ann").checked,
    skip_capital_ranking: !document.getElementById("skip-capital").checked,
  };
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
  } catch (e) {
    console.error("update settings error:", e);
  }
}

// ── Tab group/sub-tab switching ────────────────────
const GROUP_MAP = {
  intersection: "short", ultra: "short", "trend-obs": "short", "trend-conf": "short",
  pre: "statemachine", triggered: "statemachine", late: "statemachine",
  capital: "capital", flow: "capital",
  "low-ultra": "low", "low-trend": "low", watchlist: "low",
  sectors: "sector",
  sticky: "sticky",
};

function switchTab(tabName) {
  currentTab = tabName;
  const group = GROUP_MAP[tabName];
  // Activate group
  document.querySelectorAll(".group-tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.group === group);
  });
  document.querySelectorAll(".tab-group").forEach((t) => {
    t.classList.toggle("active", t.dataset.group === group);
  });
  // Activate sub-tab
  document.querySelectorAll(".sub-tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === tabName);
  });

  if (window.innerWidth >= 1800) {
    // Wide screen: scroll to the corresponding panel
    const panel = document.querySelector(`.panel[data-tab="${tabName}"]`);
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  } else if (lastData) {
    document.getElementById("table-container").innerHTML = renderTable(lastData, currentTab);
  }
}

// ── Init ────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  // Group tab clicks → activate group, default to first sub-tab
  document.querySelectorAll(".group-tab").forEach((gt) => {
    gt.addEventListener("click", () => {
      const group = gt.dataset.group;
      const firstSub = gt.closest(".tab-group").querySelector(".sub-tab");
      if (firstSub) switchTab(firstSub.dataset.tab);
      else switchTab(gt.closest(".tab-group").querySelector("[data-tab]")?.dataset.tab || group);
    });
  });
  // Sub-tab clicks → switch within group
  document.querySelectorAll(".sub-tab").forEach((st) => {
    st.addEventListener("click", () => switchTab(st.dataset.tab));
  });

  // Refresh buttons
  document.getElementById("refresh-btn").addEventListener("click", () => triggerRefresh(false));
  document.getElementById("force-refresh-btn").addEventListener("click", () => triggerRefresh(true));
  document.getElementById("clear-cooldown-btn").addEventListener("click", clearCooldown);
  // Prewarm button
  document.getElementById("prewarm-btn").addEventListener("click", triggerPrewarm);
  // MD download button
  document.getElementById("md-btn").addEventListener("click", () => {
    window.open("/api/md", "_blank");
  });

  // Settings
  document.getElementById("skip-ann").addEventListener("change", updateSettings);
  document.getElementById("skip-capital").addEventListener("change", updateSettings);

  // Initial fetch
  fetchData();
  fetchStatus();

  // Start polling
  pollTimer = setInterval(fetchData, POLL_INTERVAL);
  statusTimer = setInterval(fetchStatus, STATUS_INTERVAL);

  // Re-render on screen resize (switch between multi-panel and single-tab)
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (lastData) renderData(lastData);
    }, 300);
  });
});
