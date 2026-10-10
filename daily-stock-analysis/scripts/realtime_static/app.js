"use strict";

// ── State ──────────────────────────────────────────
let currentTab = "intersection";
let lastData = null;
let pollTimer = null;
let statusTimer = null;

const POLL_INTERVAL = 10000; // 10s
const STATUS_INTERVAL = 5000; // 5s

// ── Helpers ────────────────────────────────────────
function esc(value) {
  return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
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
function flowBadge(status, row) {
  // 2026-09-26：引擎已设置 flow_veto（超大单为负一票否决），但此前前端只渲染 flow_status，
  // 看板用户看不到否决状态。这里在徽标后拼上标记，与 Markdown 报告的写法保持一致。
  const veto = row && row.flow_veto
    ? `<span class="badge badge-bad">❌${row.flow_veto}</span>`
    : "";
  if (!status) return veto || '<span class="badge badge-dim">-</span>';
  const m = {
    "有效流入": "badge-good",
    "疑似流入": "badge-maybe",
    "价量背离": "badge-warn",
    "疑似派发": "badge-bad",
    "数据不足": "badge-dim",
  };
  return `<span class="badge ${m[status] || "badge-dim"}">${status}</span>${veto}`;
}
function shadowBadge(badge) {
  // 影子徽标由报告落盘后的只读判定器回填；未回填/历史不足一律“未完成判定”，
  // 绝不显示成“不符合”。
  const status = badge && badge.status;
  if (status === "triggered") {
    return `<span class="badge badge-good" title="由报告落盘后的只读判定器回填">今日已触发影子条件 · ${badge.trigger_time || "-"}</span>`;
  }
  if (status === "not_triggered") {
    return '<span class="badge badge-dim">未触发（已判定）</span>';
  }
  return '<span class="badge badge-dim" title="历史不足、报告尚未落盘或该股不在判定器的低吸表中">未完成判定</span>';
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
  const rr = row.rr_ratio;
  const basis = row.exit_reference_basis || "按现价估算；实际计划按买点价重算";
  return `<span class="entry-exit"><small>${basis}（参考价${row.entry_reference_price ?? "-"}）</small>参考止损<span class="sl">${sl}</span>(${slPct}%) → +2%参考目标<span class="tp">${tp1 ?? "-"}</span> <span class="rr">RR参考${rr ?? "-"}</span></span>`;
}

function renderCapitalClass(v, row) {
  if (!row.capital_class_confirmed) {
    const candidate = row.capital_class_candidate || "评级待核验";
    return `<span class="badge badge-maybe">未确认</span><small>快照候选：${candidate}</small>`;
  }
  return `<span class="badge badge-purple">${v || "-"}</span>`;
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

// ── 术语词典（唯一来源）────────────────────────────
// 键 = 栏目 + 术语。表格入口（ⓘ）与侧栏从同一份数据读取；同名术语靠栏目区分，
// 不跨栏目串义。遇到未收录的新标签一律原样显示，不套用近似解释。
// 每条统一三段：是什么(what) → 现在应看什么(look) → 权限边界(limit)。
// ref 供维护者定位：对应《选股框架.md》章节或判定函数，规则改变时据此同步文案。
// 突破状态只由 CLI 路径计算；看板链路不产出该字段，词条须说明适用范围。
const BREAKOUT_SCOPE_NOTE = "实时看板当前未计算这些状态；仅用于理解已运行状态机的报告。";

const GLOSSARY_ENTRIES = [
  // 观察池结构（明日观察池「结构」列 / 触发价 / 低吸区）
  {
    group: "观察池结构", scope: "明日观察", term: "趋势低吸",
    what: "当前价高于5日线，5日线高于10日线，10日线不低于20日线。这是观察池的均线结构标签。",
    look: "价格是否到达低吸区（见「低吸区」列）。",
    limit: "标签只描述均线结构；是否到达低吸区、能否买入仍须分别核验。",
    ref: "选股框架.md 一、明日观察池监控；a_share_daily_screen.build_watchlist()",
  },
  {
    group: "观察池结构", scope: "明日观察", term: "突破前观察",
    what: "已通过观察池基础筛选，但均线排列未达到「趋势低吸」的定义。",
    look: "关注触发价、低吸区与失效条件。",
    limit: "此标签不表示突破已触发。",
    ref: "选股框架.md 一、明日观察池监控；a_share_daily_screen.build_watchlist()",
  },
  {
    group: "观察池结构", scope: "明日观察", term: "触发价",
    what: "用于判断价格是否进入突破观察阶段的参考价。",
    look: "触及价格后还要检查资金及后续快照。",
    limit: "不能按触价直接买入。",
    ref: "选股框架.md 一、明日观察池监控；a_share_daily_screen.build_watchlist()",
  },
  {
    group: "观察池结构", scope: "明日观察", term: "低吸区",
    what: "报告给出的价格观察区间。",
    look: "进入区间仍须核验当时的资金、盘口、公告与有效买点。",
    limit: "进入区间不等于可以买入。",
    ref: "选股框架.md 一、一票否决与最低门槛；a_share_daily_screen.build_watchlist()",
  },
  // 交集状态（交集「相位」/「交集状态」列）
  {
    group: "交集状态", scope: "交集状态", term: "准交集",
    what: "已进入超短池，距趋势确认条件还差一项，且当前前置门槛通过。",
    look: "属于提前观察：关注后续快照是否推进为正式交集。",
    limit: "提前观察，不是买点。",
    ref: "选股框架.md 一、信号层级；a_share_daily_screen.compute_pre_intersection()",
  },
  {
    group: "交集状态", scope: "交集状态", term: "首次交集",
    what: "超短池与趋势确认池首次同时出现，记录的是启动事件。",
    look: "后续还要等待回踩。",
    limit: "启动事件不是买点。",
    ref: "选股框架.md 一、信号层级；a_share_daily_screen.evaluate_intersection_states()",
  },
  {
    group: "交集状态", scope: "交集状态", term: "等待回踩",
    what: "交集已记录，正在等待价格、量能与资金形成合格回踩。",
    look: "观察是否形成合格回踩。",
    limit: "不因出现交集而追价。",
    ref: "选股框架.md 一、信号层级；a_share_daily_screen.evaluate_intersection_states()",
  },
  {
    group: "交集状态", scope: "交集状态", term: "回踩确认",
    what: "状态机的回踩条件已满足。",
    look: "真实仓仍须完成公告、主导、分笔五档、基本面及买点等最终核验。",
    limit: "状态机确认不等于真实仓门禁全过。",
    ref: "选股框架.md 一、一票否决与最低门槛；a_share_daily_screen.evaluate_intersection_states()",
  },
  {
    group: "交集状态", scope: "交集状态", term: "可新开仓",
    what: "状态机资格达到入场阶段。",
    look: "核对全部真实仓门禁与《选股框架.md》核验清单。",
    limit: "不代表全部真实仓门禁已通过，也不表示已挂单或成交。",
    ref: "选股框架.md 四、决策速查表；a_share_daily_screen.evaluate_intersection_states()",
  },
  {
    group: "交集状态", scope: "交集状态", term: "迟到交集",
    what: "首次交集时已出现过热等问题。",
    look: "无需再找买点。",
    limit: "状态机不提供追价买点。",
    ref: "选股框架.md 一、信号层级；a_share_daily_screen.evaluate_intersection_states()",
  },
  // 突破状态（对应已评估报告里明日观察池的「突破状态」列；看板链路不产出该列，仅作词条）
  {
    group: "突破状态", scope: "突破状态", term: "WATCHING",
    what: "仍在观察阶段，价格或资金尚未形成有效突破触发。",
    look: "关注触发价与失效条件。",
    limit: "观察阶段不可作为买入依据。",
    note: BREAKOUT_SCOPE_NOTE,
    ref: "选股框架.md 一、明日观察池监控；观察池突破状态机",
  },
  {
    group: "突破状态", scope: "突破状态", term: "TRIGGERED",
    what: "首次触发突破条件，仍需后续快照确认。",
    look: "等待下一快照站稳、VWAP 上方、共振与主导有效。",
    limit: "单次触发不等于突破成立。",
    note: BREAKOUT_SCOPE_NOTE,
    ref: "选股框架.md 一、明日观察池监控；观察池突破状态机",
  },
  {
    group: "突破状态", scope: "突破状态", term: "CONFIRMED",
    what: "已经过跨快照确认及相应资金、均价线和共振检查。",
    look: "观察池突破仍属实验，需按框架核验真实仓条件。",
    limit: "不能仅凭状态取得真实仓权限。",
    note: BREAKOUT_SCOPE_NOTE,
    ref: "选股框架.md 一、明日观察池监控（突破实验仍在待验证）；观察池突破状态机",
  },
  // 资金状态（各表「资金状态」徽标）
  {
    group: "资金与风险", scope: "资金状态", term: "有效流入",
    what: "本轮资金、价格和均价线组合符合系统的流入分类。",
    look: "关注下一快照是否维持。",
    limit: "描述当前快照，不保证流入持续。",
    ref: "a_share_daily_screen.classify_flow()",
  },
  {
    group: "资金与风险", scope: "资金状态", term: "疑似流入",
    what: "主力资金为正，但支持「有效流入」的条件尚未全部满足。",
    look: "看还缺哪一项（资金占比、价格、均价线）。",
    limit: "不构成买入依据，须继续核验。",
    ref: "a_share_daily_screen.classify_flow()",
  },
  {
    group: "资金与风险", scope: "资金状态", term: "价量背离",
    what: "资金或成交活跃，价格却没有同步配合。",
    look: "关注后续是否失效。",
    limit: "背离属风险提示，不构成买入依据。",
    ref: "a_share_daily_screen.classify_flow()",
  },
  {
    group: "资金与风险", scope: "资金状态", term: "疑似派发",
    what: "当前大、小单及价格位置出现系统定义的派发风险。",
    look: "查看具体资金和盘口证据。",
    limit: "属风险提示，不构成买入依据。",
    ref: "选股框架.md 一、一票否决（高位破位派发）；a_share_daily_screen.classify_flow()",
  },
  {
    group: "资金与风险", scope: "资金状态", term: "数据不足",
    what: "缺少判定所需数据或历史基准。",
    look: "补齐分笔/快照基准后再判断。",
    limit: "不能理解为「资金没有变化」。",
    ref: "a_share_daily_screen.classify_flow()",
  },
  // 公告（各表「公告」徽标）
  {
    group: "资金与风险", scope: "公告", term: "watch_risk",
    what: "存在需关注的公告风险。",
    look: "按框架减分处理，并核对具体公告条目。",
    limit: "它本身不是 avoid/unknown 一票否决。",
    ref: "选股框架.md 一、一票否决与最低门槛；a_share_daily_screen.classify_announcement_risk()",
  },
  {
    group: "资金与风险", scope: "公告", term: "avoid / unknown", match: ["avoid", "unknown"],
    what: "公告风险需回避，或数据不足以排除风险。",
    look: "先排除该标的，等公告风险明确后再评估。",
    limit: "两者均阻止正式买入建议。",
    ref: "选股框架.md 一、一票否决与最低门槛；a_share_daily_screen.classify_announcement_risk()",
  },
];

const GLOSSARY_GROUPS = [];
const GLOSSARY_INDEX = {};    // scope -> 原始值 -> entry
const GLOSSARY_ANCHOR = {};   // scope -> 原始值 -> 侧栏元素 id
(function buildGlossary() {
  const seen = [];
  GLOSSARY_ENTRIES.forEach((entry, i) => {
    entry.id = "g-term-" + i;
    const keys = entry.match || [entry.term];
    GLOSSARY_INDEX[entry.scope] = GLOSSARY_INDEX[entry.scope] || {};
    GLOSSARY_ANCHOR[entry.scope] = GLOSSARY_ANCHOR[entry.scope] || {};
    for (const k of keys) {
      GLOSSARY_INDEX[entry.scope][k] = entry;
      GLOSSARY_ANCHOR[entry.scope][k] = entry.id;
    }
    if (!seen.includes(entry.group)) seen.push(entry.group);
  });
  for (const g of seen) {
    GLOSSARY_GROUPS.push({ title: g, entries: GLOSSARY_ENTRIES.filter((e) => e.group === g) });
  }
})();

function glossaryEntry(scope, value) {
  const byScope = GLOSSARY_INDEX[scope];
  if (!byScope || value === null || value === undefined || value === "") return null;
  return byScope[String(value)] || null;
}

/** 表格入口的 ⓘ 按钮；未收录的标签不生成按钮（原样显示，不给近似解释）。 */
function infoButton(scope, value) {
  const entry = glossaryEntry(scope, value);
  if (!entry) return "";
  return `<button type="button" class="info-btn" data-glossary-id="${entry.id}"` +
    ` title="${esc(entry.what)}" aria-label="术语解释：${esc(entry.term)}">ⓘ</button>`;
}

// ── 个股代码 → 东方财富个股页（纯函数：只校验与生成地址，不发起请求）──
// 只接受恰好六位数字，按市场前缀映射官方路径；未知前缀不猜测，返回空串由调用方退化为纯文本。
function eastmoneyStockUrl(code) {
  const c = String(code == null ? "" : code).trim();
  if (!/^\d{6}$/.test(c)) return "";
  if (c.startsWith("60")) return `https://quote.eastmoney.com/sh${c}.html`;
  if (c.startsWith("68")) return `https://quote.eastmoney.com/kcb/${c}.html`;
  if (c.startsWith("00") || c.startsWith("30")) return `https://quote.eastmoney.com/sz${c}.html`;
  return "";
}

/** 代码列统一渲染：合法个股代码渲染为东财新标签页链接，否则原样显示纯文本。 */
function renderStockCode(code) {
  if (code === null || code === undefined || code === "") return "-";
  const text = esc(code);
  const url = eastmoneyStockUrl(code);
  if (!url) return text;
  return `<a class="stock-link" href="${url}" target="_blank" rel="noopener noreferrer"` +
    ` title="在东方财富查看个股页面" aria-label="在东方财富查看个股页面（代码 ${text}）">${text} ↗</a>`;
}

// 列 → 栏目：单元格上的原始值据此查词典；解释层不参与筛选与判定。
// 注：突破状态（WATCHING/TRIGGERED/CONFIRMED）不在此列——看板链路（realtime_engine）
// 目前不产出 breakout_phase（仅 CLI 跑观察池突破状态机）。硬加一列会把缺失值画成
// WATCHING，等于伪造状态，所以它只作为侧栏词条存在，不挂表格入口。
const GLOSSARY_COLUMN_SCOPE = {
  structure: "明日观察",
  intersection_phase: "交集状态",
  flow_status: "资金状态",
  announcement_risk: "公告",
  risk_status: "公告",
};

// ── Column definitions per tab ─────────────────────
const COLS = {
  sticky: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "board_label", l: "交易板", c: "board" },
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

  // ── 低开观察：必须高于昨收，20日条件单独标出数据是否足够 ──
  "low-open": [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "open", l: "开盘%", c: "num", r: fmtOpenPct },
    { k: "low_open_pct", l: "低开%", c: "num", r: (v) => (isNumber(v) ? `<span class="warn">${Number(v).toFixed(2)}</span>` : "-") },
    { k: "main_net", l: "主力净流入", c: "num", r: (v) => fmtPct(v, 1) },
    { k: "persistent_net", l: "20日累计净流入", c: "num", r: fmtFlow },
    { k: "persistent_flow_status", l: "20日验证" },
    { k: "flow_status", l: "资金状态", r: flowBadge },
  ],

  // ── 准交集预警：超短 + 差1项趋势条件 + 前置门槛（主力/5分资金/均价线；共振仅参考） ──
  pre: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "trigger_price", l: "触发价", c: "num", r: (v) => (isNumber(v) ? Number(v).toFixed(2) : "-") },
    { k: "late_reason", l: "迟到原因", r: (v) => v && v !== "无" ? `<span class="flat">${v}</span>` : "-" },
  ],
  intersection: [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "capital_class", l: "资金类", r: renderCapitalClass },
    { k: "pool_source", l: "来源" },
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "board_label", l: "交易板", c: "board" },
    { k: "price", l: "现价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "capital_score", l: "正式评分", c: "num", r: (v) => (v != null ? v.toFixed(1) : "-") },
    { k: "sector_experiment_score", l: "板块实验分（不计正式评级）", c: "num", r: (v) => (v != null ? v.toFixed(1) : "-") },
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
    { k: "code", l: "代码", c: "code", r: (v, row) => renderStockCode(v) + (row._holding ? ' <span class="holding-tag">持仓</span>' : "") },
    { k: "name", l: "名称", c: "name" },
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
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
    { k: "board_label", l: "交易板", c: "board" },
    { k: "price", l: "当前价", c: "num", r: fmtPrice },
    { k: "change", l: "涨幅", c: "num", r: (v) => `<span class="${colorChange(v)}">${fmtPct(v)}</span>` },
    { k: "industry", l: "板块" },
    { k: "structure", l: "结构" },
    { k: "trigger", l: "触发价", gh: ["明日观察", "触发价"] },
    { k: "buy_zone", l: "低吸区", gh: ["明日观察", "低吸区"] },
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
    { k: "persistent_flow_status", l: "20日验证" },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "risk", l: "风险" },
    { k: "announcement_risk", l: "公告", r: riskBadge },
  ],
  "neg-super": [
    { k: "code", l: "代码", c: "code" },
    { k: "name", l: "名称", c: "name" },
    { k: "data_time", l: "数据时间" },
    { k: "super_net", l: "超大单", c: "num", r: (v) => `<span class="down">${fmtFlow(v)}</span>` },
    { k: "main_net", l: "主力净额", c: "num", r: fmtFlow },
    { k: "big_net", l: "大单净额", c: "num", r: fmtFlow },
    { k: "flow_5m_inc", l: "5分钟增量", c: "num", r: fmtFlow },
    { k: "price", l: "现价/均价线", c: "num", r: (v, row) => `${fmtPrice(v)} / ${row.vwap_state || "-"}` },
    { k: "resonance", l: "板块共振" },
    { k: "dominance_label", l: "生产主导标签" },
    { k: "flow_status", l: "资金状态", r: flowBadge },
    { k: "announcement_risk", l: "公告", r: riskBadge },
    { k: "blockers", l: "未通过门槛", r: (v) => (Array.isArray(v) ? v.join("；") : (v || "-")) },
    { k: "shadow_badge", l: "影子徽标", r: shadowBadge },
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
  "low-open": "低开观察（高于昨收、站上均价线；20个完整交易日资金历史不足时不通过）",
  "neg-super": "负超单观察（独立列表 · 仅观察 · 真实仓一票否决不变）",
};

const TAB_NOTES = {
  intersection: "注：交集仅代表启动确认，不是买点；真正买点由交集后的缩量回踩产生（见下方状态机）。",
  pre: "准交集需距趋势确认仅差一项，并通过主力净额、5分钟资金和均价线前置检查；板块共振在此阶段用于参考，不单独否决。准交集仍是观察信号。未过的标为「观察中」并列出未通过项；公告 avoid/unknown 一票否决（不给新开仓资格），watch_risk 仅减分。",
  triggered: "交集信号锁存15分钟：不立即追，等从触发价缩量回踩0.5%–1.5%、5分资金仍正、不破VWAP，方为买点（回踩确认/可试错）。",
  late: "首次交集即过热（涨幅>4.6%/距VWAP>1.2%/换手>7%/脉冲大阳/高位回撤>1.5%/无共振），已标记迟到，不提供买点。",
  "trend-obs": "趋势观察池比严格趋势池宽一些，避免大跌或修复行情中趋势池完全空掉。",
  sticky: "进入过超短池/自选的股票，退出候选池后仍跟踪 15 分钟，便于继续验证买墙后续与量能。人工关注的股票持续跟踪。",
  "low-open": "低开观察：低开≥2%、现价高于昨收并站上均价线、当日主力净流入为正，且此前最近20个已完整结算交易日累计主力净流入为正；盘中快照不替代日频历史，数据不足时不通过。",
  "neg-super": "超大单为负仍是一票否决，本表不构成买入依据。仅当参数配置的「负超单观察」开启时显示。逐项未通过门槛如实列出（含「不满足 absolute 主导」），不得当作“仅差一个条件即可买”。影子徽标由报告落盘后的只读判定器回填：历史不足、报告尚未落盘或该股不在判定器的低吸表中显示「未完成判定」。",
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
  "neg-super": (d) => d.negative_super_observations || [],
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
  if (tabName === "neg-super" && data.negative_super_status && data.negative_super_status !== "ok") {
    const reason = data.negative_super_status === "degraded" ? "行情降级（新浪备用源）" : "行情快照不完整";
    html += `<div class="section-note warn">⚠️ ${reason}，本轮未产出负超单观察列表；列表为空不代表“今天没有负超单标的”。</div>`;
  }
  html += `<div>共 ${rows.length} 条</div>`;

  if (rows.length === 0) {
    html += '<div class="placeholder">无数据</div>';
    return html;
  }

  html += '<table class="screen-table"><thead><tr>';
  for (const col of cols) {
    const hInfo = col.gh ? infoButton(col.gh[0], col.gh[1]) : "";
    html += `<th class="${col.c || ""}">${col.l}${hInfo}</th>`;
  }
  html += "</tr></thead><tbody>";

  for (const row of rows) {
    html += "<tr>";
    for (const col of cols) {
      const val = row[col.k];
      let rendered;
      if (col.r) rendered = col.r(val, row);
      else if (col.c === "code") rendered = renderStockCode(val);
      else rendered = (val != null && val !== "" ? String(val) : "-");
      // 标签/徽标旁挂 ⓘ：原始标签与数值照旧，解释层不参与筛选与判定
      const glossScope = GLOSSARY_COLUMN_SCOPE[col.k];
      if (glossScope) rendered += infoButton(glossScope, val);
      html += `<td class="${col.c || ""}">${rendered}</td>`;
    }
    html += "</tr>";
  }

  html += "</tbody></table>";
  return html;
}

function renderMarketPanel(data) {
  const breadth = data.breadth || {};

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

function negSuperVisible() {
  return Boolean(lastData && lastData.meta && lastData.meta.negative_super_view === "observe");
}

function updateNegSuperTab(data) {
  const btn = document.getElementById("tab-btn-neg-super");
  if (btn) btn.style.display = negSuperVisible() ? "" : "none";
  // 由“观察开启”切回“严格展示”时，避免停在已隐藏的标签页上
  if (!negSuperVisible() && currentTab === "neg-super") switchTab("watchlist");
}

function renderConfigChip(status) {
  const chip = document.getElementById("config-chip");
  if (!chip) return;
  const viewText = (v) => (v === "observe" ? "观察开启" : (v ? "严格展示" : null));
  const curView = viewText(status.negative_super_view) || "严格展示";
  // 旧快照筛选方式提示：当时的创业板/科创板只观察，与当前「全部参与正式筛选」口径不同
  const methodWarn = status.snapshot_screen_method === "legacy_extended_observation"
    ? '<div class="banner banner-snapshot">⚠️ 当前展示的快照由旧口径生成：当时创业板/科创板只进观察列表。当前设置已是三板统一参与正式筛选，两者不可直接比较。</div>'
    : "";
  const snapView = viewText(status.negative_super_view_snapshot);
  let text = `负超单：${curView} · 保存时间：${status.config_updated_at || "尚未保存"}`;
  let tone = "config-chip";
  if (status.config_pending) {
    // 待生效期间：当前设置与当前快照是两个口径，必须分别写清楚，不能混在一句里。
    text += ` · 待下一轮生效（当前快照：${snapView || "未生成"}）`;
    tone += " pending";
  } else if (!status.has_result) {
    text += " · 等待首轮筛选";
  }
  if (status.config_error) {
    text += "（配置回退默认）";
    tone += " error";
  }
  chip.textContent = text;
  chip.className = tone;
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
    "neg-super": (data.negative_super_observations || []).length,
  };
  for (const [tab, count] of Object.entries(counts)) {
    const el = document.getElementById("cnt-" + tab);
    if (el) el.textContent = count || "";
  }
  // Group totals
  const shortTotal = counts.intersection + counts.ultra + counts["trend-obs"] + counts["trend-conf"];
  const capitalTotal = counts.capital + counts.flow;
  const lowTotal = counts["low-ultra"] + counts["low-trend"] + counts.watchlist
    + (negSuperVisible() ? counts["neg-super"] : 0)
  const shortEl = document.getElementById("cnt-short");
  const capEl = document.getElementById("cnt-capital-group");
  const lowEl = document.getElementById("cnt-low-group");
  if (shortEl) shortEl.textContent = shortTotal || "";
  if (capEl) capEl.textContent = capitalTotal || "";
  if (lowEl) lowEl.textContent = lowTotal || "";
}

function renderData(data) {
  renderPermissionHint(data);
  if (!data || data.error) {
    const container = document.getElementById("table-container");
    container.innerHTML = `<div class="error-msg">${data ? data.error : "无数据"}</div>`;
    return;
  }
  lastData = data;
  renderMarketPanel(data);
  updateNegSuperTab(data);
  renderCounts(data);
  renderFooter(data);

  const wideScreen = window.innerWidth >= 1800;
  if (wideScreen) {
    // Multi-panel layout: show all tabs simultaneously
    const container = document.getElementById("table-container");
    container.className = "multi-panel";
    const tabs = ["intersection", "pre", "triggered", "late", "low-open", "ultra", "trend-obs", "trend-conf", "capital", "flow", "low-ultra", "low-trend", "watchlist", "sectors", "sticky"]
      .concat(negSuperVisible() ? ["neg-super"] : [])
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

  // 共用运行状态条：数据时点 / 数据源 / 自动刷新 / 引擎占用 / 告警旗标。
  // 与筛选工作台同一实现、同一措辞；三个动作按钮的忙时禁用也由它决定。
  // （原先分散在这里的「下次刷新」「行情不完整」「K线缓存」三处展示已并入状态条，
  //   避免同一事实在页面上出现三次。）
  SharedUI.render(document.getElementById("shared-status"), status);
  const ownerState = SharedUI.ownerState(status);
  for (const id of ["refresh-btn", "force-refresh-btn", "prewarm-btn"]) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    if (!ownerState.canStartScreening && !btn.dataset.busyLocked) {
      btn.dataset.busyLocked = "1";
      btn.disabled = true;
      btn.title = ownerState.busyReason || "";
    } else if (ownerState.canStartScreening && btn.dataset.busyLocked) {
      delete btn.dataset.busyLocked;
      btn.disabled = false;
      btn.title = "";
    }
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
  renderConfigChip(status);
}

// ── 术语侧栏（位于表格重绘区之外，刷新不关闭、不跳词）──
let glossaryRendered = false;
let glossaryLastFocus = null;

function renderGlossaryOnce() {
  if (glossaryRendered) return;
  const body = document.getElementById("glossary-body");
  if (!body) return;
  body.innerHTML = GLOSSARY_GROUPS.map((g) =>
    `<section class="g-group"><h2>${esc(g.title)}</h2>` +
    g.entries.map((e) =>
      `<section class="g-term" id="${e.id}">` +
        `<h3>${esc(e.term)}<span class="g-scope">${esc(e.scope)}</span></h3>` +
        `<dl>` +
          `<dt>是什么</dt><dd>${esc(e.what)}</dd>` +
          `<dt>现在应看什么</dt><dd>${esc(e.look)}</dd>` +
          `<dt>权限边界</dt><dd>${esc(e.limit)}</dd>` +
        `</dl>` +
        (e.note ? `<p class="g-note">ℹ️ ${esc(e.note)}</p>` : "") +
        `<p class="g-ref">依据：${esc(e.ref)}</p>` +
      `</section>`).join("") +
    `</section>`).join("");
  glossaryRendered = true;
}

function openGlossary(entryId) {
  const panel = document.getElementById("glossary");
  if (!panel) return;
  renderGlossaryOnce();
  if (panel.classList.contains("hidden")) {
    glossaryLastFocus = document.activeElement;
    panel.classList.remove("hidden");
  }
  document.querySelectorAll("#glossary .g-term.target").forEach((n) => n.classList.remove("target"));
  const body = document.getElementById("glossary-body");
  const target = entryId ? document.getElementById(entryId) : null;
  if (target) {
    target.classList.add("target");
    target.scrollIntoView({ block: "center" });
  } else if (body) {
    body.scrollTop = 0;
  }
  const closeBtn = document.getElementById("glossary-close");
  if (closeBtn) closeBtn.focus();
}

function closeGlossary() {
  const panel = document.getElementById("glossary");
  if (!panel || panel.classList.contains("hidden")) return;
  panel.classList.add("hidden");
  if (glossaryLastFocus && document.contains(glossaryLastFocus)) glossaryLastFocus.focus();
}

// ── 权限提示：这些词不能只靠用户主动点 ⓘ 才看到边界 ──
const EXPERIMENTAL_BREAKOUT_PHASES = ["TRIGGERED", "CONFIRMED", "B_BREAKOUT", "A_STRICT"];

function renderPermissionHint(data) {
  const el = document.getElementById("permission-hint");
  if (!el) return;
  const d = data && !data.error ? data : null;
  const parts = [];
  if (d) {
    const states = d.intersection_states || [];
    if (states.some((r) => r.intersection_phase === "可新开仓" || r.intersection_phase === "可试错")) {
      parts.push("「可新开仓」是状态机资格，不等于全部真实仓门禁已通过，也不表示已挂单或成交。");
    }
    if ((d.watchlist || []).some((r) => EXPERIMENTAL_BREAKOUT_PHASES.includes(r.breakout_phase))) {
      parts.push("观察池突破仍属实验状态，不能仅凭状态名取得真实仓权限。");
    }
  }
  if (!parts.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.innerHTML = parts.map((p) => `<span class="ph-item">⚠️ ${esc(p)}</span>`).join("");
  el.classList.remove("hidden");
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
    const response = await fetch("/api/refresh" + (force ? "?force=1" : ""), { method: "POST" });
    const started = await response.json();
    if (started.status !== "started") {
      // 被另一个入口（工作台手动任务 / 预热）占用：说明原因，不能默默返回。
      btn.disabled = false;
      other.disabled = false;
      btn.textContent = force ? "强制刷新" : "立即刷新";
      other.textContent = force ? "强制刷新" : "立即刷新";
      SharedUI.notice(document.getElementById("action-notice"),
        started.reason || "已有筛选任务在运行（看板自动刷新或工作台手动任务），请等它结束后再试");
      fetchStatus();
      return;
    }
    SharedUI.notice(document.getElementById("action-notice"), "");
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
    const response = await fetch("/api/prewarm", { method: "POST" });
    const started = await response.json();
    if (started.status !== "started") {
      btn.disabled = false;
      btn.textContent = "预热K线";
      SharedUI.notice(document.getElementById("action-notice"),
        started.reason || "已有任务在运行（看板自动刷新或工作台手动任务），请等它结束后再试");
      fetchStatus();
      return;
    }
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

// 口径调整入口已迁到「筛选工作台 → 参数配置」：看板顶部只显示状态，不在看板上改规则。

// ── Tab group/sub-tab switching ────────────────────
const GROUP_MAP = {
  intersection: "short", ultra: "short", "trend-obs": "short", "trend-conf": "short",
  pre: "statemachine", triggered: "statemachine", late: "statemachine",
  capital: "capital", flow: "capital",
  "low-ultra": "low", "low-trend": "low", "low-open": "low", watchlist: "low", "neg-super": "low",
  // Keep both names valid: the group button uses `sector`, while the table
  // data key is `sectors`.
  sector: "sector", sectors: "sector",
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

  // 策略开关（公告检查/资金排名）已迁到工作台参数配置，看板顶部不再提供编辑入口。

  // 术语说明：顶部入口 + 表格里的 ⓘ（表格每 10 秒重绘，用事件委托绑定）
  const glossaryBtn = document.getElementById("glossary-btn");
  if (glossaryBtn) glossaryBtn.addEventListener("click", () => openGlossary(null));
  const glossaryClose = document.getElementById("glossary-close");
  if (glossaryClose) glossaryClose.addEventListener("click", closeGlossary);
  const glossaryBackdrop = document.getElementById("glossary-backdrop");
  if (glossaryBackdrop) glossaryBackdrop.addEventListener("click", closeGlossary);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" || e.key === "Esc") closeGlossary();
  });
  document.addEventListener("click", (e) => {
    const target = e.target;
    const trigger = target && target.closest ? target.closest("[data-glossary-id]") : null;
    if (!trigger) return;
    e.preventDefault();
    openGlossary(trigger.dataset.glossaryId);
  });

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
