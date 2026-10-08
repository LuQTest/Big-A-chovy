/* 实时看板 / 筛选工作台 共用层：状态读取与渲染
 *
 * 设计约束（与 docs/web-workbench.md 的「并发与安全设计」一致）：
 *   - 两个入口共用一把筛选锁，同一时刻只有一个引擎在跑；状态条要能回答
 *     「现在是谁在跑」，所以占用判定只在这里实现一次。
 *   - 状态条的措辞是唯一的：不要在各页 app.js 里另写一套说法。
 *
 * 数据来源：/api/status。本线没有 screening_owner 字段，占用方由
 * is_running / is_prewarming 推导（看板自动刷新 / K线预热）。
 */
(function (global) {
  "use strict";

  var OWNER_TEXT = {
    dashboard: "看板自动刷新",
    prewarm: "K线预热",
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback === undefined ? "-" : fallback;
    return String(value);
  }

  function clock(value) {
    var parts = text(value, "").split(" ");
    return parts.length > 1 ? parts[1] : text(value, "");
  }

  function duration(seconds) {
    var total = Number(seconds);
    if (!isFinite(total) || total < 0) return "";
    if (total < 60) return total + "s";
    return Math.floor(total / 60) + "m" + String(Math.floor(total % 60)).padStart(2, "0") + "s";
  }

  /** 占用状态：谁在跑，以及能否发起新任务。 */
  function ownerState(status) {
    var st = status || {};
    var owner = st.is_prewarming ? "prewarm" : (st.is_running ? "dashboard" : null);
    return {
      owner: owner,
      label: owner ? OWNER_TEXT[owner] || owner : null,
      running: Boolean(owner),
      busyReason: owner ? (OWNER_TEXT[owner] || owner) + "正在运行，请等它结束后再试" : null,
      canStartScreening: !owner,
    };
  }

  /** 自动刷新列。 */
  function autoRefreshState(status) {
    var st = status || {};
    var settings = st.settings || {};
    var state = ownerState(st);
    if (!settings.auto_refresh) return { text: "已关闭（仅手动触发）", tone: "" };
    if (state.owner === "prewarm") return { text: "等待K线预热完成", tone: "is-warn" };
    if (state.owner) return { text: "本轮进行中", tone: "" };
    if (st.is_trading_hours) {
      return { text: "开启 · 下轮 " + (clock(st.next_refresh_time) || "即将"), tone: "" };
    }
    if (st.next_is_trading_open) {
      return { text: "盘后待机 · 下次开盘 " + (clock(st.next_refresh_time) || "-"), tone: "" };
    }
    return { text: "盘后待机", tone: "" };
  }

  /** 引擎占用列。 */
  function occupancyState(status) {
    var st = status || {};
    var state = ownerState(st);
    if (!state.owner) return { text: "空闲", tone: "is-ok" };
    if (state.owner === "prewarm") {
      var progress = st.prewarm_progress || {};
      var failed = progress.failed ? "（" + progress.failed + " 失败）" : "";
      return { text: "K线预热 " + (progress.done || 0) + "/" + (progress.total || 0) + failed, tone: "is-warn" };
    }
    return { text: state.label + "（本轮进行中）", tone: "is-warn" };
  }

  /** 状态条右侧旗标：只列会影响判读的异常与门禁变化。 */
  function flagList(status) {
    var st = status || {};
    var settings = st.settings || {};
    var flags = [];
    if (st.data_mode === "degraded") flags.push({ text: "降级数据", tone: "bad", title: "行情源降级：部分字段缺失，不作为完整判定依据" });
    else if (st.data_mode === "snapshot") flags.push({ text: "最近快照", tone: "warn", title: "非交易时段或数据源异常，展示最近一次完整筛选结果" });
    if (st.market_fetch_complete === false) {
      flags.push({ text: "行情不完整（缺 " + ((st.failed_pages || []).length) + " 页）", tone: "warn", title: "东财部分分页失败，本轮为局部快照" });
    }
    if (st.em_in_cooldown) flags.push({ text: "东财冷却中", tone: "warn", title: "东财入口被限流，冷却结束前不参与请求轮换" });
    if (st.proxy_unavailable) flags.push({ text: "代理断开", tone: "bad", title: "本机代理不可用，行情可能取不到" });
    // 防呆：公告检查是一票否决门禁。判据是"这份快照实际有没有跳过公告检查"
    // （announcement_check_skipped，由结果 meta 记录、/api/status 暴露），
    // 不是"当前设置"——设置与快照执行口径是两回事，不能混为一谈。
    if (st.announcement_check_skipped) {
      flags.push({ text: "本快照公告检查已跳过", tone: "bad", title: "本快照公告检查已跳过，本轮结论不可作为真实仓依据" });
    }
    if (settings.skip_capital_ranking) flags.push({ text: "资金排名已关闭", tone: "warn", title: "未做资金排序，候选按其他条件排序" });
    // 交易板范围：同样跟随**快照**而不是当前设置。范围含扩展板时必须可见，
    // 便于把本轮结果与范围对应起来；旧快照没有该字段时如实标「范围未记录」。
    var boards = st.enabled_boards;
    var scopeNote = st.board_scope_note;
    if (boards === null || boards === undefined) {
      flags.push({ text: "范围未记录", tone: "warn", title: "该快照生成时还没有交易板范围口径（旧版本产物），无法追溯本轮实际筛选范围" });
    } else if (boards.length && !(boards.length === 1 && boards[0] === "main")) {
      flags.push({
        text: "范围：" + (st.enabled_boards_label || boards.join(" + ")),
        tone: "info",
        title: scopeNote || "本轮筛选的交易板范围；所选交易板统一参与正式筛选（同一门槛、同一排名、同一条状态机）",
      });
    }
    // 范围结论：必须能区分「范围内没有符合条件的候选」与「本轮数据不可用」。
    // 一律按**快照**的结论显示，不按当前设置推断。范围默认（仅主板）时也显示，
    // 否则用户会把“数据不可用”误读成“今天没候选”。
    if (st.board_scope_status && st.board_scope_status !== "ok") {
      flags.push({
        text: "本轮范围内结果不可用",
        tone: "bad",
        title: scopeNote || "行情降级或快照不完整：本轮没有候选**不等于**范围内没有符合条件的标的",
      });
    } else if (st.board_scope_status === "ok" && st.board_scope_candidates === 0) {
      flags.push({
        text: "范围内无符合条件的候选",
        tone: "info",
        title: scopeNote || "数据完整，本轮交易板范围内没有符合现有门槛的候选（属正常筛选结果，不是数据问题）",
      });
    }
    return flags;
  }

  function item(label, value, tone, title) {
    return '<div class="ss-item"><span class="ss-label">' + esc(label) + '</span>' +
      '<span class="ss-value' + (tone ? " " + tone : "") + '"' +
      (title ? ' title="' + esc(title) + '"' : "") + ">" + esc(value) + "</span></div>";
  }

  /**
   * 渲染状态条。两页调用方式一致，唯一差别是 variant:
   *   - "dashboard"：看板顶部（其后仍有涨跌/指数各行）
   *   - "workbench"：工作台顶部（同一槽位，措辞相同）
   */
  function render(element, status, options) {
    if (!element) return;
    var st = status || {};
    var opts = options || {};
    var refresh = autoRefreshState(st);
    var occupancy = occupancyState(st);
    var html = "";
    html += item("数据时点", text(st.data_timestamp), "", "最近一次筛选使用的行情时点；手动任务运行时会更新为本次时点");
    html += item("数据源", text(st.data_source), "", "最近一次筛选实际使用的行情源");
    html += item("自动刷新", refresh.text, refresh.tone);
    html += item("引擎占用", occupancy.text, occupancy.tone);
    var flags = flagList(st).map(function (flag) {
      return '<span class="ss-flag ' + flag.tone + '" title="' + esc(flag.title) + '">' + esc(flag.text) + "</span>";
    }).join("");
    html += '<div class="ss-flags">' + flags + "</div>";
    element.innerHTML = html;
    if (typeof opts.onRendered === "function") opts.onRendered(st, ownerState(st));
  }

  /** 统一的拒绝提示：动作被占用挡住时必须让用户看到原因。 */
  function notice(element, message, tone) {
    if (!element) return;
    if (!message) {
      element.classList.add("hidden");
      element.innerHTML = "";
      return;
    }
    element.className = "ss-notice" + (tone ? " " + tone : "");
    element.innerHTML = esc(message) + '<button type="button" aria-label="关闭提示">✕</button>';
    element.querySelector("button").addEventListener("click", function () {
      element.classList.add("hidden");
    });
  }

  /* ---- 版本提示：非模态、默认折叠 ----
     只依赖 /api/update 的字段。Release 正文一律 esc() 后按**纯文本**渲染，不做
     Markdown→HTML：上游正文里的 <script> 或事件属性在纯文本下无法执行，从根上
     避免把第三方文本当标记执行。 */
  var UPDATE_POLL_MS = 30 * 60 * 1000;
  var UPDATE_RETRY_MS = 2000;
  var UPDATE_RETRY_MAX = 5;
  var UPDATE_NOTES_MAX = 4000;

  /** Release 链接只放行 GitHub 的 https 地址（服务端已过滤，这里再挡一道）。 */
  function safeReleaseUrl(value) {
    var url = text(value, "");
    return /^https:\/\/github\.com\//.test(url) ? url : "";
  }

  /** 正文按纯文本渲染：只把行首的 -/* 换成项目符号，不生成任何 HTML。 */
  function releaseNotes(value) {
    return text(value, "").slice(0, UPDATE_NOTES_MAX).split("\n").map(function (line) {
      return line.replace(/^\s*[-*]\s+/, "• ");
    }).join("\n");
  }

  function updateSummary(st) {
    if (st.update_available) return "";
    if (st.status === "ok") return "已是最新";
    if (st.status === "checking") return "检查中…";
    if (st.status === "disabled") return "更新检查已关闭";
    return "未获取到版本信息";
  }

  /** 版本条：常显本机版本，有新版本时加蓝色胶囊，详情默认折叠（非模态）。 */
  function renderUpdate(element, data) {
    if (!element) return;
    var st = data || {};
    if (!st.local_version) {
      // 本机版本都读不到就不占位：这里不该出现只有维护者才看得懂的空白条。
      element.classList.add("hidden");
      element.innerHTML = "";
      return;
    }
    var url = safeReleaseUrl(st.release_url);
    var notes = releaseNotes(st.release_notes);
    var expandable = Boolean(st.update_available || notes || st.detail);
    var html = '<div class="un-bar">';
    html += '<span class="un-meta">本机 v' + esc(st.local_version) + " · " + esc(st.channel_label) + "</span>";
    if (st.update_available) {
      html += '<span class="un-pill">有新版本 v' + esc(st.latest_version) + "</span>";
    }
    var summary = updateSummary(st);
    if (summary) html += '<span class="un-meta">' + esc(summary) + "</span>";
    if (expandable) {
      html += '<button type="button" class="un-toggle" aria-expanded="false">'
        + (st.update_available ? "查看更新内容" : "详情") + "</button>";
    }
    if (url) html += '<a class="un-link" data-role="release">Release 页面</a>';
    if (st.status !== "disabled") {
      html += '<button type="button" class="un-check">检查更新</button>';
    }
    html += "</div>";

    if (expandable) {
      html += '<div class="un-panel hidden">';
      if (st.update_available && st.install_hint) {
        html += '<div class="un-meta">升级：<code>' + esc(st.install_hint) + "</code></div>";
      }
      if (st.update_available && st.image_ref) {
        html += '<div class="un-meta">镜像：<code>' + esc(st.image_ref) + "</code></div>";
      }
      if (notes) html += '<pre class="un-notes">' + esc(notes) + "</pre>";
      if (st.detail) html += '<div class="un-detail">' + esc(st.detail) + "</div>";
      html += "</div>";
    }
    element.innerHTML = html;
    element.classList.remove("hidden");

    var toggle = element.querySelector(".un-toggle");
    var panel = element.querySelector(".un-panel");
    if (toggle && panel) {
      var openLabel = st.update_available ? "查看更新内容" : "详情";
      toggle.addEventListener("click", function () {
        var nowHidden = panel.classList.toggle("hidden");
        toggle.setAttribute("aria-expanded", String(!nowHidden));
        toggle.textContent = nowHidden ? openLabel : "收起";
      });
    }
    var link = element.querySelector('[data-role="release"]');
    if (link && url) {
      // href 用属性赋值，不拼进 innerHTML；上面已限定 https://github.com/ 前缀。
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    var check = element.querySelector(".un-check");
    if (check) {
      check.addEventListener("click", function () {
        loadUpdate(element, true, 0);
      });
    }
  }

  /** 读一次 /api/update。检查失败静默处理，软件照常使用。 */
  function loadUpdate(element, force, retryLeft) {
    var url = force ? "/api/update?force=1" : "/api/update";
    return fetch(url).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) {
      renderUpdate(element, data);
      var left = retryLeft === undefined ? UPDATE_RETRY_MAX : retryLeft;
      if (data && data.status === "checking" && left > 0) {
        // 首次检查在后台线程里跑：短间隔补几次，别让用户等下一个 30 分钟轮询。
        setTimeout(function () { loadUpdate(element, false, left - 1); }, UPDATE_RETRY_MS);
      }
    }).catch(function () {
      // 检查失败不打扰用户：不弹窗、不写控制台错误、软件照常使用。
      return null;
    });
  }

  /** 挂载版本条。宿主页面没有这个席位时什么都不做。 */
  function mountUpdateNotice() {
    var element = document.getElementById("update-notice");
    if (!element) return;
    startPolling(function () { loadUpdate(element, false); }, UPDATE_POLL_MS);
  }

  function fetchStatus() {
    return fetch("/api/status").then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function startPolling(handler, intervalMs) {
    var timer = setInterval(handler, intervalMs);
    handler();
    return timer;
  }

  global.SharedUI = {
    render: render,
    notice: notice,
    fetchStatus: fetchStatus,
    startPolling: startPolling,
    ownerState: ownerState,
    autoRefreshState: autoRefreshState,
    occupancyState: occupancyState,
    flagList: flagList,
    esc: esc,
    duration: duration,
    clock: clock,
    OWNER_TEXT: OWNER_TEXT,
    renderUpdate: renderUpdate,
    loadUpdate: loadUpdate,
    mountUpdateNotice: mountUpdateNotice,
    releaseNotes: releaseNotes,
    safeReleaseUrl: safeReleaseUrl,
  };

  // 自挂载：版本条是共用行为，两个入口都只管在页面里留席位，不必各写一遍启动代码。
  mountUpdateNotice();
})(window);
