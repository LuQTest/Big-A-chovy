# 股票 · A股量化筛选工作流

先读根目录 `AGENTS.md`。交易规则只维护在[选股框架.md](选股框架.md)，执行入口只使用[项目盘中 skill](skills/盘中/SKILL.md)。已安装旧版已停用，不再同步维护。

## 决策、盘问与复盘

- **决策**：先读最新决策记录，核对持仓、在盯清单和明日观察池；摘要市场与双池；按框架逐项核验；分层裁决；追加日志并更新观察池。
- **盘问单股**：核对池内状态、连续资金、实时分笔五档、基本面与YTD，再对照框架展示事实、卡点和计划。用户独立请求互动盘问时可一次一问；`ggp`内的盘问直接查证到底，不停下来等回答。
- **复盘**：收到实际/模拟操作立即记录方向、类型、代码、数量、价格、时间，再核对当时建议；缺失值标未知，不猜成交。收盘核算盈亏、更新持仓快照与T+1计划。每日/周度经验维护按框架执行，新规则不复制进本文件。

## ggp 快捷流程

用户发 `ggp` 等价于一条龙执行：

1. **继续看筛选**：上次提问到当前的全部快照，覆盖超短池、趋势观察/确认池、交集、低吸候选、主力资金优选与每份报告底部明日观察池。不能只看最新一份。缺少上次检查边界时从当天首份开始，并写明覆盖范围。
2. **盘问候选与观察池**：按框架排序，穿透分笔、五档与基本面；候选进入在盯后持续跟踪，出表必须说明是资金衰减、回落变化、数据缺失还是解析异常。
3. **盘问持仓与开仓判定**：分别核对模拟仓、真实仓、解套仓的实时盈亏和可卖状态，给出分层建议与T+1计划；新增真实仓建议必须展示框架“四、七项支撑”。
4. **落盘**：写入本次核验范围、数据时点、分层结果、在盯变化和下一步计划。没有用户成交反馈不能把建议变成真实持仓。

14:20后的报告继续用于持仓与观察池监控，新增开仓权限按框架时间规则处理。

## 输出与记录

人类可读结果使用 `docs/ggp_display_prompt.md`；机器通道使用 `docs/ggp_prompt.md` 和 `docs/ggp_output_schema.json`，只输出一个JSON对象。渲染：`python3 tools/render_ggp_output.py <response.json>`，校验失败不得猜测修复。

日常决策日志统一格式：

```markdown
## HH:MM 决策（早盘/午后/持仓管理）
- 报告范围：起止时间、份数、最新文件
- 数据时点与缺项：……
- 真实仓可开仓：……（逐只附七项支撑、买点、止损、T+1计划）
- 模拟仓可买：……（实验类别、买点、止损、计划）
- 仅观察/真实仓暂不开：……（卡点、在盯状态变化）
- 完全空仓（无新增可买候选）：是/否
- 持仓核验与T+1计划：……
- 次日验证：待填
```

收盘持仓快照（仅格式示例，不代表当前账户）：

```yaml
## 收盘持仓快照
positions:
  simulated: []
  real: []
  real_mother: []
cash:
  real_available: null
watchlist: []
t1_plan: []
```

## 工具与数据纪律

- 全部命令在项目根目录执行；详见 `README.md`。
- 持仓：`python3 tools/get_position.py [--json]`，唯一事实来源为最新 `决策记录/YYYYMMDD.md`。
- 扫描：`python3 tools/scan_reports.py --date YYYYMMDD`；单股历史：`python3 tools/track_stock.py <代码> --date YYYYMMDD`。
- 行情/五档/分时：`python3 tools/query_quote.py <代码> --minute --kline`；建仓前仍须补齐分笔核验。
- 基本面：`python3 tools/query_financials.py <代码>`。
- 板块退潮：`python3 tools/watch_sector.py <代码> auto --date YYYYMMDD --from HHMM --buy-count N`。
- 对账：`python3 tools/validate_consistency.py`；共享机器参数只读 `tools/rule_config.py`。
- 解析表格按表头动态定位，禁止硬编码列号；批量扫描必须输出样例行并核对候选，空结果先排查解析问题。
- 行情默认使用 `--network-mode auto`：`daily-stock-analysis/scripts/network_path.py` 并发实测直连、`daily-stock-analysis/scripts/proxy_ports.json` 的 `candidate_ports`、环境代理和 macOS 系统代理，按真实可用延迟择优并在请求失败时重测；不预设代理或直连优先。换代理软件只改 `proxy_ports.json`，不要改代码或依赖具体软件。`--network-mode direct` 仅直连，`--network-mode proxy` 仅使用代理路径。诊断：`python3 daily-stock-analysis/scripts/network_path.py`。
- 报告、持仓、决策记录、影子库与账户数据均为本地私有；同步前按README检查排除规则。

## 沟通约定

用户的“继续看筛选”“复盘”“写吧”“ggp”直接执行；重复提问表示继续盯盘。输出规则结果与证据，判断错了对照案例复盘，不以预测替代规则。
