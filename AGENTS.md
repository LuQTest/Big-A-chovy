# AGENTS.md — 多 Agent 快速上下文（每次会话必读）

> 本文件是所有 AI 助手（Claude Code / Codex / opencode / ZCode 等）进入本工作区的统一入口，每次会话必须最先读取。它只负责定位与红线，**不另立规则**；与任何文档冲突时，以下方权威链为准。

## 30 秒定位

A 股 T+1 超短线（低吸模式）量化筛选 + AI 辅助决策工作台。工具链只做筛选、证据发现和数据查询，**不会自动下单，也不做最终交易裁决**——裁决由 `盘中` skill 依据《选股框架.md》完成。

## 权威链（冲突时以此为准）

1. `选股框架.md` — 交易规则唯一权威（信号层级 / 一票否决 / 决策速查表 / 参数总表 / 待验证项）。**任何买卖裁决前必读。**
2. `CLAUDE.md` — 盘中三模式工作流（决策 / 盘问 / 复盘）、纪律内嵌、工具用法、沟通约定（含快捷指令 `ggp`）。
3. `skills/盘中/SKILL.md`（项目唯一入口；已安装旧版停用，不得加载）— 盘中决策/盘问/复盘的操作脚本；`daily-stock-analysis/` 为筛选引擎（发现层，详见其 `SKILL.md` 与 `codex_prompt.md`）。
4. `README.md` — 环境安装、工具命令全集、隐私与 GitHub 同步规则。

## 目录地图

| 路径 | 内容 | 性质 |
|---|---|---|
| `选股框架.md` | 规则权威，持续演化 | 必读 |
| `CLAUDE.md` | 工作流与纪律详情 | 必读 |
| `筛选结果/YYYYMMDD/A股筛选结果_YYYYMMDD_HHMM.md` | 盘中快照报告（每 1-2 分钟一份） | 私有数据，禁上传 |
| `决策记录/YYYYMMDD.md` | 当日决策 / 执行 / 复盘 + 收盘持仓快照 | **持仓与 T+1 状态唯一来源**，私有 |
| `tools/` | 扫描、影子验证、行情/基本面查询（全部非权威） | 辅助 |
| `daily-stock-analysis/` | 筛选引擎、GUI、实时看板（localhost:8765） | 辅助 |
| `股票/开盘&收盘关注.md` | 开盘 / 收盘关注清单 | 参考 |

## 每次会话开始必做

1. 读 `选股框架.md`（至少：一票否决、决策速查表、参数总表）。
2. 读最新 `决策记录/YYYYMMDD.md` —— 持仓、观察池、T+1 预案以它为准，**不要从历史会话或模板猜测当前状态**；可用 `python3 tools/get_position.py` 快速读取。
3. 盘中取报告：按 `筛选结果/YYYYMMDD/` 下文件名时间戳取最新；「继续看筛选」= 上次提问到当前的**所有**报告，不能只看最新一份。

## 硬红线（详版见选股框架.md，此处为最易犯的几条）

- 公告 `avoid` / `unknown` 一票否决；`watch_risk` 仅减分不否决。
- 超大单为负 → 一票否决（散户堆量），即使主力净占比 >5%。
- 无合格标的不持仓过夜；T+1 买入当日不可卖出，离场按框架区分当日不可卖、次日开盘风险优先、09:45常规退出截止。
- 预测 ≠ 规则：只输出规则结果，不给「我觉得会涨」。
- `coalition`、观察池突破、`sector_boost` 满20个完整结算样本并评估转正前仅模拟；`divergence_leader` 仅影子采样；回落放宽仅模拟。权限详见框架。
- 输出必须双仓分层：①真实仓可开仓 ②模拟仓可买 ③仅观察/真实仓暂不开 ④完全空仓；存在模拟候选时不得笼统写「空仓」。
- 真实仓开仓建议必须附完整支撑原因（主线 / 资金 / 分笔五档 / 买点 / 基本面 / 模拟验证 / 盈亏比）+ 买点区间 + 止损 + T+1 计划。
- 建仓前必验：分笔与五档承接、基本面盈亏（`python3 tools/query_financials.py <代码>`）。

## 工具速查（全部在项目根目录执行）

```bash
python3 tools/scan_reports.py --date YYYYMMDD        # 全天报告 5/5、4/5 扫描（诊断用，非权威）
python3 tools/track_stock.py <代码> --date YYYYMMDD  # 单股全天状态变化
python3 tools/query_quote.py <代码> --minute --kline # 实时行情/五档/分时/日K
python3 tools/query_financials.py <代码>             # 基本面盈亏/PE/YTD（建仓前必验）
python3 tools/get_position.py [--json]               # 读取持仓/观察池/T+1 预案
python3 tools/validate_consistency.py                # 框架-代码-影子库一致性对账（只读）
curl -s "https://qt.gtimg.cn/q=sh601615" | iconv -f GBK -t UTF-8   # 实时行情
```

解析报告表格**禁止硬编码列号**，按表头动态定位；批量扫描必须输出样例行核对后再接受结果，空结果 = 嫌疑。

## 隐私红线

`筛选结果/`、`决策记录/`、持仓、交易金额、影子样本库一律不得提交 GitHub 或发送到外部服务（已由 `.gitignore` 与 `.git/info/exclude` 排除）。同步前用 `git status --short --ignored` 复查。

## 当前数据备注（易踩坑）

- `筛选结果/20260825/` 子目录缺失：当日 156 份报告仍平铺在 `筛选结果/` 根目录。不带日期参数的默认扫描（如 `scan_reports.py --latest`）会命中 8/25 而非最新交易日——盘中取报告务必先确定目标日期。
- 网络路径已改为**代码内实测择优**（2026-09-09，方案 C）：`daily-stock-analysis/scripts/network_path.py` 并发实测「直连 + 本机候选代理端口（来自 `proxy_ports.json` 的 `candidate_ports`，默认 7890/7897）+ 环境代理 + 系统代理」的真实东财接口延迟，最快路径优先，**彻底不依赖系统代理/任何代理软件**；直连若被限速会自动让位给代理，反之亦然。看板默认 `network_mode=auto`，不再因无代理而放弃筛选。诊断：`python3 daily-stock-analysis/scripts/network_path.py`。
  - **配置唯一来源**：`proxy_ports.json` 的 `candidate_ports`，`network_path.py` 与 `keep_proxy_alive.sh` 共用——**换代理软件只改这一处**（旧版硬编码 7897 并 `open -a "Clash Verge"` 会与新软件争夺系统代理、关掉 Verge 就断网，已废弃）。
  - **多端点探测**：主端点（push2delay）必通该路径才可用；82push2(资金流)/push2his(K线) 不通只记降级 + 排序惩罚，不一票否决。
  - **切换粘性 + 熔断**：当前路径比最快慢 ≤50ms 不换（防抖动）；连续失败 3 次冷却 60s，全在冷却仍放行。
  - 测试：`scripts/test_network_path.py`（28 用例）。
- 2026-09-08 实测：行情接口（push2/push2delay/82.push2/push2his/quote/np-anotice + 新浪/腾讯）**直连全部可达**，0.08~0.2s；2026-09-09 复测直连约 105ms，快于 Clash Party 代理（约 286ms）。盘中高峰稳定性仍待验证。
