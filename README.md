# A 股量化筛选工作台

这是一个面向 A 股盘中筛选、低吸候选、明日观察池和复盘分析的本地工具集。

它负责查询行情、计算筛选条件、生成报告和维护观察状态；**不会自动下单**。筛选结果只是发现候选，买卖、仓位和止损仍应按照项目规则人工确认。

职责边界：`daily-stock-analysis` 仅是筛选与报告发现工具，不承担最终买卖、仓位或持仓裁决。最终决策入口是 `盘中` skill，交易规则以根目录 `选股框架.md` 为准，实际持仓以 `决策记录/` 为准。

## 版本标识

- 历史版本：`v0.3.4`，保留原有版本标签。
- 线上预览版：`v0.3.5-preview.1`，指向清理前的线上基线。
- 当前开发预览版：`v0.4.0-preview.1`，包含本次路径、隐私排除和跨用户运行兼容性收尾；盘中网络稳定性与策略效果仍在持续验证，暂不称为稳定版。

## 一、快速开始

### 环境要求

- macOS（双击 `.command` 启动器需要 macOS）。
- Python 3.10 或更高版本，建议使用 Python 3.13。
- 能访问行情接口的网络环境。默认 `auto` 模式会实测直连、本机候选代理端口、环境代理和系统代理后择优；如直连受限，请在 `daily-stock-analysis/scripts/proxy_ports.json` 中配置可用的本机 HTTP 代理端口。代理软件不限定为 Clash。

> 当前版本未做 Windows 适配。项目中的 `.command` 启动器、macOS `scutil` 代理检测、`open`/`osascript` 和部分进程管理命令均按 macOS 编写。Windows 用户可以自行尝试直接运行核心 Python 脚本，但 GUI、实时看板启动、代理检测和路径行为不保证正常，也暂不提供 Windows 专用安装或启动方案。

脚本依赖尽量使用 Python 标准库，并对可选依赖提供了降级处理：

```bash
python3 -m pip install requests pyyaml
```

`requests` 用于更稳定地访问行情接口；`pyyaml` 用于读取决策记录中的持仓快照。没有这些库时，部分功能仍可使用，但网络或 YAML 解析能力可能降级。

### 1. 启动普通筛选 GUI

在 Finder 中双击：

```text
daily-stock-analysis/scripts/运行A股筛选.command
```

GUI 可以选择筛选模块、公告检查、资金排名、输出格式，并维护本地持仓列表。筛选完成后会生成 Markdown 或 JSON 报告。

如果双击没有反应，也可以在终端运行：

```bash
python3 daily-stock-analysis/scripts/a_share_screen_gui.py
```

### 2. 直接运行命令行筛选

在项目根目录执行：

```bash
# 默认：严格双池筛选
python3 daily-stock-analysis/scripts/a_share_daily_screen.py --mode strict --format md

# 一次输出严格双池、低吸池、明日观察池
python3 daily-stock-analysis/scripts/a_share_daily_screen.py --mode all --format md

# 保存为 JSON，便于其他程序读取
python3 daily-stock-analysis/scripts/a_share_daily_screen.py \
  --mode strict low watchlist \
  --format json \
  --top 15 \
  --save /tmp/a_share_screen.json
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--mode strict` | 严格超短/趋势双池 |
| `--mode low` | 低吸 A/B/C 候选池 |
| `--mode watchlist` | 明日观察池和触发价 |
| `--mode all` | 同时运行以上三类 |
| `--format md/json` | 输出 Markdown 或 JSON |
| `--top 15` | 每个模块最多输出多少条 |
| `--save 文件路径` | 同时保存到指定文件 |
| `--network-mode auto` | 并发实测直连、候选代理、环境/系统代理，按实际可用延迟择优 |
| `--network-mode proxy` | 仅使用环境变量或 macOS 系统代理；本机候选端口请使用 `auto` 模式，由 `daily-stock-analysis/scripts/proxy_ports.json` 管理 |
| `--network-mode direct` | 强制直连 |
| `--skip-announcements` | 跳过公告风险检查，不建议日常使用 |
| `--skip-capital-ranking` | 跳过资金排名辅助模块 |

### 3. 启动实时看板

双击：

```text
daily-stock-analysis/运行实时看板.command
```

或在终端运行：

```bash
python3 daily-stock-analysis/scripts/realtime_dashboard.py
```

浏览器打开：<http://localhost:8765>

看板默认在交易时段自动刷新，启动时会预热日 K 缓存；行情不可用时会尽量保留最近一次完整结果。停止看板可以双击：

```text
daily-stock-analysis/停止实时看板.command
```

也可以在终端按 `Ctrl+C` 停止。

看板提供以下本机接口：

```bash
curl -s http://localhost:8765/api/data    # 最新完整 JSON
curl -s http://localhost:8765/api/status  # 运行状态和缓存状态
curl -s http://localhost:8765/api/md      # 最新 Markdown 报告
```

## 二、筛选模块说明

- `strict`：严格超短池、趋势确认池及其交集。
- `low`：低吸 A/B/C 分层，重点关注买入区、追高禁区、资金方向和公告风险。
- `watchlist`：明日观察池，包含触发价、低吸区、失效条件和突破状态。
- 公告风险：`clean`、`watch_risk`、`avoid`、`unknown`；`avoid` 不得绕过。
- 交集状态机：跨快照记录“观察、首次交集、等待回踩、回踩确认、可新开仓、失效”等状态。
- 资金快照：保留最近约 30 分钟，用于计算 5 分钟和 15 分钟资金增量。

无论筛选结果多强，市场环境、板块共振、个股结构和实际买点有一项不满足，都应选择等待或空仓。

## 三、报告和辅助工具

这些工具都应在项目根目录执行：

```bash
# 扫描最新交易日报告，输出 5/5、4/5 和明日观察池
python3 tools/scan_reports.py --latest 10

# 扫描指定日期
python3 tools/scan_reports.py --date 20260824

# 扫描单份报告
python3 tools/scan_reports.py --file "筛选结果/20260824/A股筛选结果_20260824_0945.md"

# 跟踪一只股票在全天报告中的状态变化
python3 tools/track_stock.py 601666 --date 20260824

# 查询实时行情、五档、分时和近期日 K
python3 tools/query_quote.py 600188 000768 --minute --kline

# 读取决策记录中的持仓、观察池和 T+1 计划
python3 tools/get_position.py
python3 tools/get_position.py --date 20260824 --json

# 监控持仓股所属板块是否退潮
python3 tools/watch_sector.py 600219 有色金属 --date 20260824 --from 1005

# 验证指定日期观察池的 T+1 早盘表现
python3 tools/verify_t1.py 20260824
```

### 影子验证工具

影子验证只用于模拟数据统计，不能直接转化为真实仓买入依据：

```bash
# 更新四类影子样本并输出进度
python3 tools/shadow_tracker.py --date 20260824

# 只查看当前进度
python3 tools/shadow_tracker.py --report

# 只读检查框架、代码、影子库和权限门槛是否一致
python3 tools/validate_consistency.py

# 识别并记录龙头分歧候选；仅写入影子样本
python3 tools/detect_divergence_leader.py --date 20260824 --record
```

## 四、输出目录和运行状态

正常运行会产生以下本地数据：

| 路径 | 用途 | 是否应上传 GitHub |
| --- | --- | --- |
| `筛选结果/` | 盘中 Markdown 报告 | 否，可能包含个人分析和交易记录 |
| `决策记录/` | 决策、执行、复盘和持仓快照 | 否，个人隐私数据 |
| `daily-stock-analysis/scripts/holdings.json` | GUI 持仓列表和行情 | 否 |
| `daily-stock-analysis/scripts/gui_settings.json` | GUI 窗口位置 | 否 |
| `daily-stock-analysis/scripts/.kline_cache.json` | 日 K 缓存 | 否，运行时自动重建 |
| `daily-stock-analysis/scripts/flow_snapshot.json` | 最近 30 分钟资金快照 | 否，运行时自动重建 |
| `daily-stock-analysis/scripts/intersection_state.json` | 交集状态机跨快照状态 | 否，运行时自动重建 |
| `daily-stock-analysis/scripts/watchlist_breakout_state.json` | 观察池突破状态机 | 否，运行时自动重建 |
| `tools/shadow_data/shadow_samples.json` | 影子验证样本和 T+1 结算 | 否 |

这些文件不存在时，程序会使用空状态或重新拉取数据。删除缓存通常只会导致下一次运行较慢；删除持仓、决策记录或状态文件会丢失相应的本地信息，应先备份。

## 五、隐私和 GitHub 同步

项目代码可以同步到 GitHub，但建议把“框架源码”和“个人运行数据”分开处理。

### 本机私有目录

项目级 `.gitignore` 已统一排除个人报告和决策记录；它会随仓库同步，其他使用者克隆后也默认受到保护。`.git/info/exclude` 仍可作为某台机器的额外保护，但不是项目正常运行所必需：

```bash
cat >> .git/info/exclude <<'EOF'
筛选结果/
决策记录/
EOF
```

`.git/info/exclude` 不会被提交，也不会影响其他使用者。项目级 `.gitignore` 同时排除了缓存、持仓、GUI 设置、影子样本、回滚备份、`筛选结果/` 和 `决策记录/`。

同步前检查：

```bash
git status --short --ignored
git check-ignore -v \
  筛选结果/ \
  决策记录/ \
  daily-stock-analysis/scripts/holdings.json \
  tools/shadow_data/shadow_samples.json
```

提交前必须检查暂存区：

```bash
git add -A
git diff --cached --name-only
```

确认没有报告、决策记录、持仓、缓存或密钥后再提交。不要使用 `git add -f` 强制添加被排除的文件。

`sync_to_github.sh` 会执行 `git add -A`、自动提交并推送；使用前仍要确认本机私有目录已排除。GitHub 仓库若不是私有仓库，请不要上传真实持仓、交易金额、账户信息或个人决策记录。

## 六、网络问题排查

如果出现“无法连接行情服务”或筛选长时间无结果：

1. 默认使用 `auto`：启动或首次请求时会实测直连、`proxy_ports.json` 的候选端口、环境代理和系统代理，按最快可用路径请求。
2. 若直连受限，编辑 `daily-stock-analysis/scripts/proxy_ports.json` 的 `candidate_ports`，填入代理软件提供的本机 HTTP 端口，然后运行诊断：

   ```bash
   python3 daily-stock-analysis/scripts/network_path.py
   ```

3. 需要诊断单一路径时，可用 `--network-mode direct` 或 `--network-mode proxy`；正常使用建议保留 `auto`。
4. 看板无法连接时，确认 `8765` 端口没有被旧进程占用，并运行停止脚本后重新启动。
5. 行情接口部分失败时，不要把降级结果当成完整实时结果；优先等待网络恢复。

## 七、开发和测试

运行基础语法检查：

```bash
python3 -m py_compile \
  daily-stock-analysis/scripts/a_share_daily_screen.py \
  daily-stock-analysis/scripts/realtime_engine.py \
  daily-stock-analysis/scripts/realtime_dashboard.py \
  tools/*.py
```

运行项目测试：

```bash
python3 -m unittest discover -s daily-stock-analysis/scripts -p 'test_*.py'
```

## 八、模型使用建议（当前测试记录）

以下结论来自当前实际使用体验，属于经验记录，不代表模型的客观性能排名。

### 盘中分析

| 模型 | 当前评价 |
| --- | --- |
| Luna Max 1.5x | 非常保守 |
| DeepSeek V4 Flash | 整体偏保守 |
| DeepSeek V4 Flash 0731 | 整体偏保守 |
| **Gemini 3.7 Flash** | **当前使用，偏激进** |

### 不建议模型

- `hy3`：响应较慢。
- `DeepSeek V4 Pro`：成本较高。

### 自动复盘

- `Sol xhigh`：当前自动复盘模型。
- `Ox Alpha`：测试中，主要在深夜使用。

## 九、已知问题与待改进

以下问题已知存在，后续需要通过 Agent 修改代码或启动配置解决：

1. **筛选结果保存路径**：已修复。GUI、实时看板和 `.command` 失败回退路径均基于项目根目录解析，克隆到其他位置后可直接运行；命令行 `--save` 仍可按使用者需要指定路径。
2. **网络路径（已代码内实测择优，2026-09-09；同日方案 C 增强）**：`daily-stock-analysis/scripts/network_path.py` 对「直连 + 本机候选代理端口（软件无关）+ 环境代理 + 系统代理」并发实测真实东财接口延迟，按实际可用路径择优；不预设代理或直连优先，路径失败后会重测。看板不再因无代理而放弃筛选；诊断运行 `python3 daily-stock-analysis/scripts/network_path.py`。
   - **配置唯一来源**：`daily-stock-analysis/scripts/proxy_ports.json` 的 `candidate_ports`，`network_path.py` 与 `keep_proxy_alive.sh` 读同一份——**换代理软件只改这一处**。
   - **多端点探测**：主端点（push2delay）必须通该路径才可用；辅助端点（82push2 资金流 / push2his K线）不通只记降级 + 排序惩罚，不一票否决。单端点探测曾漏掉「某出口 82push2 超时但 push2delay 正常」的故障。
   - **切换粘性**：当前路径比最快路径慢不超过 50ms 就不换——实测两条路常只差 1~2ms，纯按延迟排序会导致抖动。
   - **熔断冷却**：连续失败 3 次冷却 60s；全部在冷却时仍放行，避免无路可走。
   - 「太慢」判定用主端点实测延迟，不用含惩罚的评分（否则降级惩罚会把所有路径误判成太慢）。
   - 测试：`scripts/test_network_path.py`（28 个用例，覆盖枚举/验活/降级/粘性/缓存/熔断/配置回退）。
   - 历史坑：旧版脚本硬编码 7897 并 `open -a "Clash Verge"`，会与新代理软件争夺系统代理、关掉 Verge 就断网。
   - 2026-09-08 实测行情接口直连可达（0.08~0.2s），旧结论"直连会被封锁"已不成立。盘中高峰稳定性仍待验证。

## 十、内置 Skill

项目版是唯一维护和执行入口；已安装旧版已停用，不再复制安装。在本工作区依据 AGENTS.md 加载项目文件。

项目内置盘中快速接入 Skill：[`skills/盘中/SKILL.md`](skills/盘中/SKILL.md)。

它支持 `/盘中`、`/盘中 决策`、`/盘中 盘问 <代码或名称>` 和 `/盘中 复盘` 四类入口，用于读取框架、整理最新报告、盘问单只股票和记录执行复盘。报告、决策记录和持仓数据仍只保留在使用者本机，不随 Skill 文件同步。

## 十一、规则文档

使用前建议先阅读：

- [`选股框架.md`](选股框架.md)：项目规则和参数总表。
- [`daily-stock-analysis/references/screeners.md`](daily-stock-analysis/references/screeners.md)：筛选条件和输出字段。
- [`daily-stock-analysis/references/trading-rules.md`](daily-stock-analysis/references/trading-rules.md)：市场、板块、个股、买点和仓位规则。

行情筛选不构成收益保证或个性化投资建议。任何真实交易都应以使用者自己的风险承受能力和交易纪律为准。
