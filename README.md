> **数据源说明**：本版已按 2026-09 的实测结果适配取数路径（东财 `/webguest` 路由 + 腾讯日 K 多主机故障转移），行情、资金流与公告均可正常获取。第三方公开接口可能随时调整，若出现取数失败，先运行 `python3 tools/verify_em_webguest.py` 查看各入口状态，再参考 [`docs/东财请求频率与限流.md`](docs/东财请求频率与限流.md)。

# A 股量化筛选工作台

[![Latest release](https://img.shields.io/github/v/release/LuQTest/Big-A-chovy?label=latest%20release&style=flat-square)](https://github.com/LuQTest/Big-A-chovy/releases)
[![GitHub stars](https://img.shields.io/github/stars/LuQTest/Big-A-chovy?style=flat-square&label=stars)](https://github.com/LuQTest/Big-A-chovy/stargazers)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue?style=flat-square)](LICENSE)

这是一个面向 A 股盘中筛选、低吸候选、明日观察池和复盘分析的本地工具集。

它负责查询行情、计算筛选条件、生成报告和维护观察状态；**不会自动下单**。筛选结果只是发现候选，买卖、仓位和止损仍应按照项目规则人工确认。

### 功能概览

- 实时看板：自动刷新行情、资金、公告和筛选状态，并集中显示数据时点、来源、降级告警、任务占用和自动刷新状态。
- 筛选工作台：按本次任务选择筛选模式、交易板、公告检查和资金排名，运行一次筛选并查看结果。
- 报告库：同时读取根目录平铺报告与 `筛选结果/YYYYMMDD/` 归档，按报告文件名中的交易日和时间合并、排序，支持按日期/时间检索和继续查看筛选。
- 证据查询：提供行情/分时/五档、财务、交易日历、公告与事件、板块和上下文等查询工具，明确区分 `ok`、`empty`、`partial`、`stale` 和 `unavailable`。
- T+1 核验：只在官方交易日、目标日期和共享目标时刻都可验证时生成早盘证据；历史分钟目标不保证可追溯，不能用当前数据替代历史目标。

职责边界：`daily-stock-analysis` 仅是筛选与报告发现工具，不承担最终买卖、仓位或持仓裁决。最终决策入口是 `盘中` skill，交易规则以根目录 `选股框架.md` 为准，实际持仓以 `决策记录/` 为准。

## 版本标识

- 当前正式版：[v0.7.1](https://github.com/LuQTest/Big-A-chovy/releases/tag/v0.7.1)，源码发布、容器镜像和文档对应同一最终提交。
- Docker 镜像：`ghcr.io/luqtest/big-a-chovy:v0.7.1`，由 GitHub Actions 发布到 GitHub Container Registry；支持的架构以该次构建的清单为准。
- `v0.7.0` 正式版及其镜像继续保留作为回退；更早的预览 Release 页面已清理，Git tag 历史不被补丁版发布移动或覆盖。

完整更新记录见 [`CHANGELOG.md`](CHANGELOG.md)。

### 软件内更新提示

工作台和实时看板顶部会显示本机版本，发现同渠道新版本时给出提示、更新内容和升级命令。**不自动下载、不自动替换、不重启**，也不会在交易时段弹窗打断操作。

- **版本单一来源**：仓库根 `VERSION` 只保存共同的基础版本（如 `0.7.1`），渠道由发布 tag 提供——`v0.7.1` 是正式版、`v0.7.1-preview.2` 是预览版、`v0.7.1-docker.2` 是容器版。
- **只比同一渠道**：正式版只与正式版比，预览版只与预览版比，容器版只与容器版比。不同渠道的版本不互相比大小——`0.6.0-docker.2` 与 `0.6.0-preview.2` 是同一源码提交的两种打包形态。
- **升级命令按实际安装方式给出**：源码安装是 `git pull`；引用 GHCR 镜像的 Compose 是 `docker compose pull && docker compose up -d`；本仓库自带的 `docker-compose.yml` 用的是本地 `build`，因此提示 `git pull && docker compose up -d --build`。
- **离线、限流或上游异常时完全静默**：只是没有提示，软件照常使用。

版本对账（发布流水线会跑同一套校验）：

```bash
python3 tools/validate_version.py                 # VERSION 与 CHANGELOG 顶部条目
python3 tools/validate_version.py --tag v0.7.1    # 额外校验发布 tag 的基础版本与渠道
```

设计与边界见 [`docs/更新检查.md`](docs/更新检查.md)。

## GitHub Star History

仓库的 Star 数量和趋势图会随 GitHub 数据自动更新。点击徽章可查看当前 Star 列表，点击趋势图可查看详细历史：

<a href="https://www.star-history.com/?repos=LuQTest%2FBig-A-chovy&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=LuQTest/Big-A-chovy&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=LuQTest/Big-A-chovy&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=LuQTest/Big-A-chovy&type=date&legend=top-left" />
 </picture>
</a>

## 一、快速开始

### 环境要求

- macOS（双击 `.command` 启动器需要 macOS）。
- Python 3.10 或更高版本，建议使用 Python 3.13。
- 能访问行情接口的网络环境。默认 `auto` 模式会实测直连、本机候选代理端口、环境代理和系统代理后择优；如直连受限，请在 `daily-stock-analysis/scripts/proxy_ports.json` 中配置可用的本机 HTTP 代理端口。代理软件不限定为 Clash。

> Tkinter GUI、`.command` 启动器和部分 macOS 代理/进程管理逻辑仍按 macOS 编写；Windows 用户请使用上方 Web 工作台。命令行核心脚本可直接尝试，但不代表完整桌面 GUI 已适配。

脚本依赖尽量使用 Python 标准库，并对可选依赖提供了降级处理：

```bash
python3 -m pip install -r requirements.txt
```

`requests` 用于更稳定地访问行情接口；`pyyaml` 用于读取决策记录中的持仓快照。没有这些库时，部分功能仍可使用，但网络或 YAML 解析能力可能降级。

从源码安装或升级到正式版：

```bash
git fetch origin --tags
git checkout v0.7.1
python3 -m pip install -r requirements.txt
```

后续升级时先阅读 `CHANGELOG.md`，再切换到新的正式 tag；源码运行和 Docker 运行使用同一 tag 的代码，二者的功能与规则口径保持一致。

### Web 工作台（B/S 架构，推荐 Windows 用户使用）

不想受限于 macOS GUI？启动 Web 工作台，在浏览器里使用筛选、报告库和工具箱：

```bash
# Windows：双击根目录 启动工作台.bat，或：
uv run --python 3.13 --with requests --with pyyaml --with tzdata python daily-stock-analysis/scripts/web_workbench.py

# macOS / Linux
python3 daily-stock-analysis/scripts/web_workbench.py
```

- 工作台：<http://localhost:8765/workbench>（一次性筛选 + 报告库 + 工具箱）
- 实时看板：<http://localhost:8765/>（原版页面不变）

两个页面共用同一套导航与运行状态：顶部同一组视图切换「实时看板｜筛选工作台」（同页切换、标出当前位置），下方同一条运行状态条显示**数据时点 / 数据源 / 自动刷新 / 引擎占用**，并把会影响判读的异常做成旗标（降级数据、最近快照、行情不完整、东财冷却、代理断开、**公告检查或资金排名被关闭**）。看板顶部原来分散的「数据时间 / 数据源 / 下次刷新 / 行情不完整 / K 线缓存」五处展示已收敛进这条状态条，同一事实不再重复出现。

运行选项两页同名同极性：勾选=执行（`公告检查`、`资金排名`），作用域分别标注——工作台是**本次任务参数**（只影响这一次手动筛选），看板顶部是**看板默认**（持久设置，对每一轮自动刷新生效）；工作台会显示看板当前的这两个开关状态，避免改错地方。动作被占用时不静默，会在页面内提示原因（例如「看板自动刷新正在运行」）。

看板默认配置保存成功后会显示保存时间；配置冲突或保存失败会在页面内提示。已保存的配置从下一轮自动刷新开始生效，不会改变已经运行中的那一轮；工作台本次任务参数仍只作用于当前手动筛选。

Web 工作台默认只监听本机，避免报告、持仓和决策快照被局域网读取。确实需要手机或其他电脑访问时，显式运行：

```bash
python3 daily-stock-analysis/scripts/web_workbench.py --host 0.0.0.0
```

这会开放包含个人数据的接口，只应在可信局域网使用，禁止直接暴露到公网。

详细说明见 [`docs/web-workbench.md`](docs/web-workbench.md)。

### Docker 部署（公开仓库）

仓库提供一个不依赖 macOS GUI 的 Web 工作台与实时看板容器。源码、Docker 配置和 GitHub Actions 可以公开发布；报告、决策记录、持仓、影子样本和运行缓存仍保留在本机挂载目录，不会写入镜像。

```bash
cp .env.example .env       # 不需要代理时也可以跳过
docker compose up -d --build
```

浏览器打开 <http://localhost:8765/workbench> 使用工作台，打开 <http://localhost:8765/> 查看实时看板；查看状态或日志：

```bash
docker compose ps
docker compose logs -f dashboard
docker compose down
```

容器默认尝试直连行情接口。如果宿主机需要代理，在 `.env` 中填写容器可访问的地址，例如 Docker Desktop 下：

```dotenv
HTTP_PROXY=http://host.docker.internal:7890
HTTPS_PROXY=http://host.docker.internal:7890
```

Docker 运行版同时启动 Web 工作台和实时看板，不启动 Finder、macOS `.command` 启动器或桌面 GUI；宿主机端口默认只绑定 `127.0.0.1`，需要局域网访问时应明确修改 compose 端口映射并确认网络可信。它同样不会自动下单。正式 tag 与 Docker 镜像来自同一最终源码提交；GitHub Actions 负责构建并发布已验证架构的镜像到 GitHub Container Registry。

```bash
docker pull ghcr.io/luqtest/big-a-chovy:v0.7.1
```

`docker compose up -d --build` 会从当前源码 checkout 本地构建；如果需要直接使用已发布镜像，请使用上面的正式 tag，并自行确认宿主机挂载的报告、决策记录、持仓、影子样本和运行状态目录权限。源码版与镜像版都只提供筛选、证据和查询能力，不执行交易。

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
| `--boards main chinext star` | 交易板范围，默认 `main`（仅沪深主板）。可任意非空组合；勾选后该交易板股票**统一参与完整筛选**（同一门槛、同一排名、同一条状态机），未勾选的交易板不进入新增候选（见下） |

### 交易板范围（默认只筛沪深主板）

`--boards` 与看板配置里的 `enabled_boards` 决定**哪些交易板进入本轮筛选**；它与按行业计算的「板块共振」是两个概念，故不叫“板块”。

- **所选交易板统一参与完整筛选**：资金增量与状态 → 超短池／趋势池 → 双池交集 → 资金优选／低吸／明日观察池 → 现有状态机，**同一套门槛与排名**（三板都读 `tools/rule_config.py`，暂不单独放大创业板/科创板阈值）。因此超出当前涨幅或价格区间的两板股票被排除，属正常筛选结果。
- 默认只选**沪深主板**，输出与历史基线一致；勾选创业板（`300/301`）或科创板（`688/689`）后，两板股票直接出现在正常栏目中，**每行带「交易板」字段**。
- **未勾选的交易板不进入新增候选**；切换范围后旧范围留下的锁存／等待回踩／突破状态不继续生效，**已持仓股票的监控不受影响**。
- 行业共振的强势股计数按所选交易板统计，行业上涨比例与平均涨幅仍用全市场背景；「主板涨停／跌停」统计仍只指沪深主板。
- 公告、超大单为负、主导资格、数据完整性等门禁对三板**一视同仁**；数据降级或快照不完整时，报告会区分「未符合条件」与「数据不可用」。
- 北交所与其它代码不在选项内（归 `unknown`，一律排除）；ST/退市与字段不全的股票照旧排除。
- 范围在**每轮开始时冻结**：运行中改配置只影响下一轮；报告头部与结果（`meta.enabled_boards`／`board_scope_note`）记录本轮实际范围、各板计数与范围结论——数据完整且无候选是「未符合条件」，降级/快照不完整是「本轮结果不完整」，两者不会混为一谈。

> 历史快照说明：2026-09-30 之前的快照里，创业板/科创板只进过独立的「扩展交易板观察」列表。这类旧快照仍按原口径展示并标注为历史口径，**不要与当前「三板统一筛选」的结果直接比较**。

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

报告库会把根目录平铺报告和按日期归档的报告合并后，再按文件名中的交易日与时间排序；空的日期目录不会制造虚假报告，文件名无法解析日期/时间的文件也不会混入正常序列。

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

# 按需核验：腾讯约3秒聚合分笔（B/S/M、5/15分钟窗口）
python3 tools/query_ticks.py 600519 --json

# 建仓前财务证据：已披露利润表与动态/TTM PE分开显示
python3 tools/query_financials.py 600519 --json

# 隔夜事件、市场情绪和官方交易日历
python3 tools/query_events.py 600519 --date 2026-10-03 --json
python3 tools/query_sentiment.py --date 2026-10-03 --json
python3 tools/query_calendar.py --date 2026-10-03 --json

# 按需上下文：topic 可选 monitor/anomaly/themes/news/research/interaction/dragon_tiger/commodity
python3 tools/query_context.py 600519 --topic news --date 2026-10-03 --json

# 离线历史日线研究（通达信官网盘后 ZIP；非交易日/未发布会明确返回 unavailable）
python3 tools/query_history.py --date 2026-10-03 --code 600519 --json

# 显式启用跨日筹码估算；输入需有 date/high/low/close/turn，结果始终标注“筹码估算”
python3 tools/query_chips.py --history-json /path/to/history.json --code 600519 --decay 0.9 --json

# 读取决策记录中的持仓、观察池和 T+1 计划
python3 tools/get_position.py
python3 tools/get_position.py --date 20260824 --json

# 监控持仓股所属板块是否退潮
python3 tools/watch_sector.py 600219 有色金属 --date 20260824 --from 1005

# 验证指定日期观察池的 T+1 早盘表现
python3 tools/verify_t1.py 20260824
```

这些新增查询均是证据/研究工具，不自动改变筛选评分、状态机、真实仓权限或个人决策记录，也不会自动下单。历史估值/ST/停牌查询的 BaoStock 依赖是可选的；未安装时返回 `unsupported`。通达信日线包使用股、元单位，跨日筹码模型要求换手率和复权口径可核验；缺失或混口径会拒绝推演。

`verify_t1.py` 的早盘核验以官方交易日历确定 T+1 日期，并要求行情响应中的证券、交易日期和共享目标时刻全部匹配；当前目标为 09:45，不会用 09:44 或其他相邻分钟替代。当前分钟接口只保证当日实时/近期可用数据，历史目标分钟若无法追溯会明确返回不可用，不能据此判定历史目标价格或收益。

### 影子验证工具

影子验证只用于模拟数据统计，不能直接转化为真实仓买入依据：

> ⚠️ **真实样本库保护**：工程修复和离线回归不会自动解锁真实库写入。禁止对默认本机影子库运行无参数或带 `--date` 的 `shadow_tracker.py` 扫描，也禁止对真实库使用 `detect_divergence_leader.py --record`；`--report` 仅作只读查看。测试必须设置临时 `A_SHARE_SHADOW_DATA_DIR`，并只使用人工报告和模拟行情。缺少 `fcntl` 与 `msvcrt` 两种跨进程锁原语的平台会明确拒绝影子库写入；Windows 原生锁由 Windows CI 验收。真实历史库迁移、重算或恢复须另行制定方案并单独验收；测试通过、样本数量或代码版本均不构成授权。线下反馈提到的 `shadow_sample.py` 和 `每日收盘.bat` 不在当前工作副本中，先同步并核实后再使用。

```bash
# 只查看当前进度
python3 tools/shadow_tracker.py --report

# 只读检查框架、代码、影子库和权限门槛是否一致
python3 tools/validate_consistency.py

# `--record` 仅可用于人工报告目录和临时影子库；本轮不提供指向默认真实报告目录的写入命令。
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
| `daily-stock-analysis/scripts/.a_share_data_*.json` | 新数据源的按源缓存（日历、事件、上下文、分笔等） | 否，运行时自动重建 |
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

### 唯一的对外请求：版本检查

工作台与实时看板启动后会在后台查询一次 GitHub 的 Release 列表（`api.github.com`），用于提示新版本。这是本项目**唯一**的对外请求。

- 只发出版本检查请求本身，**不发送任何本机数据**：没有报告、没有持仓、没有决策记录、没有账户信息，也不带机器标识。
- 24 小时内不会重复查询；离线、限流或接口异常时静默失败，不影响任何筛选与查询功能。
- 关闭方式：启动前设置 `A_SHARE_UPDATE_CHECK=off`。
- 检查结果的缓存写在运行状态目录（默认 `daily-stock-analysis/scripts/`，可用 `A_SHARE_STATE_DIR` 定向到临时目录），与报告归档和决策记录无关。

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
6. 当前筛选列表、基本面查询和实时 1 分钟趋势分别使用东财 `push2/webguest` 的 `clist`、`ulist.np`、`stock/get`、`trends2` 路由。**日 K 按三档依次下沉：腾讯前复权（`ifzq.gtimg.cn` / `proxy.finance.qq.com` / `web.ifzq.gtimg.cn`，带连续失败熔断）→ 东财 `push2his` 前复权（`klt=101&fqt=1`，另有数字子域）→ 新浪（末档，可能不复权且可能缺当日 bar）**。前两档来自不同厂商，因此互相兜底：腾讯被 WAF 拦截时由东财顶上，东财被限流/封禁时由腾讯顶上。2026-09-28 复测：东财日 K 已恢复可用且为前复权（与腾讯同日收盘差 ≤0.2%），沪深主板/创业板/科创板/北交所均返回。降级不会被静默掩盖——报告头部「来源」按本轮实际来源生成，报告警告逐轮报出「本轮 N 只来自新浪」以及其中多少只**与前复权基准的日期或收盘不一致**（偏差 >1%；基准由前两档的成功结果维护）。同日实测：腾讯整链故障约 47 分钟期间走新浪，与前复权基准收盘差 1.4%–2.7%、5 日涨幅差最多 2.2 倍，超短池被压掉约一半。可用 `python3 tools/verify_em_webguest.py` 对比标准入口、`/webguest` 路由和 K 线降级路径。
7. 请求频率、限流参数，以及「限流」与「路径下线」的区分方法见 [`docs/东财请求频率与限流.md`](docs/东财请求频率与限流.md)。

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

### 端到端验证必须隔离运行状态

盘中状态机依赖持久化的运行状态文件（交集锁存／等待回踩、观察池突破确认次数、资金 5/15 分钟基准），验证跑会覆盖它们。**任何实跑验证（含 `--boards` 范围验证、手动跑引擎、起看板）都要先把状态与报告定向到临时目录**：

```bash
A_SHARE_STATE_DIR=/tmp/e2e/state A_SHARE_REPORT_DIR=/tmp/e2e/reports \
python3 daily-stock-analysis/scripts/a_share_daily_screen.py --boards main chinext --top 5
```

- `A_SHARE_STATE_DIR`：`flow_snapshot.json`、`intersection_state.json`、`watchlist_breakout_state.json`、`.kline_cache.json`、`.announcement_risk_cache.json`、`.em_cooldown`、`last_valid_result.json`。
- `A_SHARE_REPORT_DIR`：看板与工作台的报告归档目录（默认 `筛选结果/`）。验证产物混进当日报告序列会污染 `scan_reports` 与「继续看筛选」的结论。
- 用户配置与输入**不**跟随这两个变量：`dashboard_settings.json`、`holdings.json`、`proxy_ports.json`、`intersection_calibration.json` 仍在原位。
- 变量在**进程启动时**读取一次，必须在启动前设置；测试套件同样不得写入真实运行状态（`test_network_diagnostics` 已按此挡住状态落盘）。
- 单元测试默认不联网、不启动服务，因此不需要该变量；它是给**实跑**用的。

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

## 九、问题与改进跟踪

本节跟踪工程和运维问题。已完成项保留背景与验证状态，不再当作待办；交易规则的实验与待验证项见《选股框架》第六节。

### 已完成或持续观察

1. **筛选结果保存路径（已修复）**：GUI、实时看板和 `.command` 失败回退路径均基于项目根目录解析，克隆到其他位置后可直接运行；命令行 `--save` 仍可按使用者需要指定路径。
2. **网络路径自动择优（已实现，盘中高峰稳定性待观察）**：`daily-stock-analysis/scripts/network_path.py` 对「直连 + 本机候选代理端口（软件无关）+ 环境代理 + 系统代理」并发实测真实东财接口延迟，按实际可用路径择优；不预设代理或直连优先，路径失败后会重测。诊断运行 `python3 daily-stock-analysis/scripts/network_path.py`。
   - **前提**：本机若开启代理的 TUN / Fake-IP 模式，「直连」与「代理」实际是同一出口，该择优退化为网络健康探测（自查方法见「十三、容易踩的坑」第 1 条）。
   - **配置唯一来源**：`daily-stock-analysis/scripts/proxy_ports.json` 的 `candidate_ports`，`network_path.py` 与 `keep_proxy_alive.sh` 读同一份——**换代理软件只改这一处**。
   - **多端点探测**：主端点使用筛选器实际采用的 `push2/webguest` 列表路由；辅助端点检查 `82.push2/webguest` 与 `push2his` K 线。不通的辅助端点只记降级 + 排序惩罚，不一票否决。
   - **切换粘性**：当前路径比最快路径慢不超过 50ms 就不换——实测两条路常只差 1~2ms，纯按延迟排序会导致抖动。
   - **熔断冷却**：连续失败 3 次冷却 60s；全部在冷却时仍放行，避免无路可走。
   - 「太慢」判定用主端点实测延迟，不用含惩罚的评分（否则降级惩罚会把所有路径误判成太慢）。
   - 测试：`scripts/test_network_path.py`（29 个用例，覆盖枚举/验活/降级/粘性/缓存/熔断/配置回退）。
   - 历史坑：旧版脚本硬编码 7897 并 `open -a "Clash Verge"`，会与新代理软件争夺系统代理、关掉 Verge 就断网。
   - 2026-09-08 实测行情接口直连可达（0.08~0.2s），旧结论"直连会被封锁"已不成立。
3. **Windows 报告目录名提取（本地修补，待 Windows 复验）**：`tools/scan_reports.py` 和 `tools/track_stock.py` 原先用 `split('/')[-2]`，在 Windows 反斜杠路径上会触发 `IndexError`；当前工作副本已改为 `os.path.basename(os.path.dirname(...))`。线下反馈报告该补丁可正常扫描 88 份报告；本环境未重复运行验证。
4. **交易板范围（2026-10-01 验收通过）**：`enabled_boards` 支持沪深主板／创业板／科创板任意非空组合，所选交易板统一参与完整筛选（资金增量与状态 → 超短池／趋势池 → 双池交集 → 资金优选／低吸／明日观察池 → 状态机），默认仅主板。验收记录：仅主板／仅创业板／仅科创板／三板四种范围实跑均能进入正常池并运行状态机，未选交易板零混入（引擎实测科创板在快照中 621 只、预筛 0 只），两板的超大单为负一票否决与公告门禁正常生效，报告与两个页面口径一致。验收同时修掉六处缺陷，其中 `a_share_daily_screen.py` 的标注调用排在 `result` 赋值之前会让 CLI 每次成功运行都以 `UnboundLocalError` 收尾。
5. **一致性检查改为从本地影子库读进度（2026-10-01）**：`tools/validate_consistency.py` 原先要求 `选股框架.md` 写明「已采集X；完整结算Y/20」，与框架第六节「进度与金额只在本地影子库维护」相矛盾。现在框架侧只核对验证目标、验证方法与权限声明（并**禁止**框架出现进度数字），实际进度按统一完整结算口径从 `tools/shadow_data/shadow_samples.json` 读取，未结算不计入。
6. **测试套件不再污染状态（2026-10-01）**：两处跨用例污染已修，`python3 -m unittest discover` 由「7 个失败」转为全绿（353 个用例）。
   - `test_network_diagnostics` 跑真实 `main()` 却漏挡 `save_watchlist_breakout_state`，每跑一次套件就把**盘中观察池突破状态**按当天日期覆盖（已补 patch，与同处已有的交集/资金挡板一致）。
   - `test_kline_source_chain` 在整套 discover 下会全部误判：`realtime_engine` 导入时会把 `screen.fetch_kline` 换成带缓存的包装函数，缓存一命中用例就走不到取数链（单跑不导入引擎，所以单跑正常）。已在 setUp 锁死无缓存的原始链。
   - 环境状态文件现由 `runtime_paths.py` 统一解析，端到端验证的隔离开关见「七、开发和测试」。
7. **CLI 与看板收敛到同一条筛选流水线（2026-10-03，详见 [`docs/daily-stock-analysis-重构交付记录.md`](docs/daily-stock-analysis-重构交付记录.md)）**：
   - 新增共享核心 `a_share_daily_screen.run_screening_core`；CLI 的 `main()` 与 `realtime_engine.run_screening` 都调用它。出口差异（K 线缓存、分钟线、负超单观察、温度计、5 分钟量能）改为显式注入，删除了 `realtime_engine` 导入时 monkey-patch `screen.fetch_kline` 的全局副作用。
   - **`top` 只影响展示行数**：此前严格超短池先被 `top` 截短再算双池交集/准交集/资金优选/公告核验范围，页面显示条数会改变状态机输入。现在内部候选保留全量，展示裁剪放在最后。
   - **公告政策集中一处**：`avoid`/`unknown` 一票否决、`watch_risk` 仅减分不否决。修正了交集状态机把 `watch_risk` 当硬否决的行为（原 `test_intersection_state_machine` 断言了错误行为，已更新并注明依据）。
   - 观察池突破状态机改为两个入口共跑；`unknown` 不再升级为 `CONFIRMED`/`B_BREAKOUT`/`A_STRICT`。
   - 资金优选统一在公告核验后、同步 `Enriched.risk_status` 后再排名，并传入同一份 `flow_history`（此前看板提前排名且缺历史，`sector_boost` 可能少加分）。
   - 市场环境分级（`CASH` 禁止新开仓）在核心统一计算（此前仅看板计算）。
   - 新增回合状态提交门 `state_commit.py`：看板/工作台超时或失败的一轮**不得**提交资金基准/交集状态/观察池突破/K 线缓存，降级快照不清空上一份有效状态；状态全部改为临时文件 + `os.replace` 原子写入。
   - 超大单为负的框架一票否决现在也作用于交集状态机的新开仓门禁（此前只在报告里打标）。
   - TLS：新增 `tls_context.py` 统一做证书校验（2026-10-03 实测东财/腾讯/新浪全部行情主机证书链有效，直连与经代理开启校验都可取数）；移除全部 `verify=False`/`_create_unverified_context`。确有自签 CA 需求时配置 `A_SHARE_CA_BUNDLE`，而不是关闭校验。
   - 测试：新增合成行情夹具 `testing_fixtures.py` 与 30 个行为用例（`top` 不变性、风险矩阵、两个状态机、CLI/看板对照、超时并发、TLS）；`unittest discover` 由 353 增至 383 个用例全绿。

### 待处理

1. **工作台的网络前置检查可能跳过新浪全市场备用（2026-09-28 记录）**：`a_share_daily_screen.fetch_market()` 已有新浪备用；但 `realtime_dashboard.ScreeningScheduler.run_screening()` 在 `network_path.has_working_path()` 为假时会提前返回并保留旧快照，不调用筛选引擎。若东财探测全失败、但新浪仍可用，已有备用逻辑可能到不了。修复目标：全网断开时继续快速失败；东财路径不可用但新浪可达时，仍允许进入备用行情流程，并在结果中明确标注降级。待下一个交易时段做真实联调；盘后可先用模拟/受控故障场景验证。
2. **影子样本累计与结算保全（2026-10-05 修复候选）**：`shadow_tracker.py` 现遍历平铺/日期目录全历史报告并按“类别+证券+日期”增量去重；`--date` 仅限制新增报告，不清空旧样本。扫描与分歧记录共用完整的读→修改→校验→原子提交事务；兼容性 `save_db` 拒绝会删除/改写较新记录的旧快照；缺少可验证跨进程锁的平台明确拒绝写入。畸形库拒绝覆盖；自定义目标及 divergence 等类别保留。T+1 仅认官方日历确认日期、精确共享目标时刻和同日完整日 K；缺时点旧结算保留原始证据但不计入完整样本。离线人工夹具覆盖历史累计、线程和多进程并发、无锁平台拒写、坏库/写入失败、日期缺口、缺时点与旧记录迁移；这些测试不读取或改写真实影子库，且不会解除真实库保护。真实历史库迁移不在本轮范围。
3. **运行状态被验证跑覆盖（2026-10-01 记录，内容不可恢复）**：交易板范围验收期间的实跑改写了三个运行时状态文件（`flow_snapshot.json`、`intersection_state.json`、`watchlist_breakout_state.json`），**盘中原始内容已丢失且没有备份可回退**；现存的 9/30 收盘版本只作为验收数据保留，不能当作原始盘中状态。当时的口径记录：交集状态与观察池状态均为 9/30 16:12 收盘快照按当次范围重新评估的结果，日期字段为 `2026-09-30`。
   - **不能宣称当前状态"已恢复"或"完全干净"**：状态机的日期隔离依据的是**行情日期**而非自然日，假期重跑 9/30 旧行情仍会判定为同一交易日并读取同日状态，所以此次覆盖的影响面无法用日期判断排除。若后续发现盘中状态机行为异常（锁存／确认次数与预期不符），应优先怀疑这一条。
   - **已加隔离**：`daily-stock-analysis/scripts/runtime_paths.py` 统一提供运行状态路径，端到端验证可通过 `A_SHARE_STATE_DIR`／`A_SHARE_REPORT_DIR` 把状态与报告定向到临时目录（用法见「七、开发和测试」）。此后所有实跑验证必须先设置这两个变量，被测代码与测试不得再写真实运行状态。

## 十、内置 Skill

项目版是唯一维护和执行入口；已安装旧版已停用，不再复制安装。在本工作区依据 AGENTS.md 加载项目文件。

项目内置盘中快速接入 Skill：[`skills/盘中/SKILL.md`](skills/盘中/SKILL.md)。

它支持 `/盘中`、`/盘中 决策`、`/盘中 盘问 <代码或名称>` 和 `/盘中 复盘` 四类入口，用于读取框架、整理最新报告、盘问单只股票和记录执行复盘。报告、决策记录和持仓数据仍只保留在使用者本机，不随 Skill 文件同步。

## 十一、规则文档

交易规则与参数语义只读 [`选股框架.md`](选股框架.md)，决策流程使用上述项目盘中 Skill。共享机器参数维护在 [`tools/rule_config.py`](tools/rule_config.py)，筛选引擎不另设 AI 入口或交易规则说明。

行情筛选不构成收益保证或个性化投资建议。任何真实交易都应以使用者自己的风险承受能力和交易纪律为准。

## 十二、复盘节奏（建议每个交易周一次）

上游行情接口会不定期失效，执行纪律也会漂移，所以建议**每个交易周做一次复盘：约 5 个交易日，最长不超过 10 个**。完整检查清单见 [`docs/复盘节奏与周检查清单.md`](docs/复盘节奏与周检查清单.md)，包含数据源健康检查、全天报告扫描、单股回看和执行偏差对照。

需要说清楚的是：**这个节奏属于运维检查，不属于参数调优。** 一周的盈亏不能证明规则好坏（同期大盘涨跌往往是大头）；门槛类参数只在 `选股框架.md` 的参数总表与 `tools/rule_config.py` 维护，变更需要走「影子采样 → T+1 结算 → 达到样本门槛后评估转正」的流程。少于 5 个交易日就下结论、或者只改数字不留记录，都会让不同时期的样本失去可比性。

复盘结论写进**本地**决策记录即可，仓库只保留方法论。

## 十三、容易踩的坑（均为实测记录）

1. **别把「直连」当成真的直连。** 本机代理启用 **DNS 接管的 Fake-IP 模式**时，域名解析会被改写，代码里的「直连」也走同一条隧道。线索自查（本机实测有效）：`python3 -c "import socket;print(socket.gethostbyname('push2.eastmoney.com'))"` 返回 `198.18.x.x` 说明这次解析拿到的是 Fake-IP（Fake-IP 网段可配置，示例默认 `198.18.0.1/16`）；但**返回真实 IP 不能反过来证明 TUN 没有接管流量**。本机在该配置下实测：诊断里「直连 112ms / 代理 110ms」是同一出口的两个数字——换成 `redir-host` 等其他 DNS 模式结论可能不同——路径择优因此没有区分度，真正的冗余来自主机级故障转移。

2. **读报告头部的「来源」行。** 出现 `新浪日K(可能不复权)` 意味着当轮日 K 走了降级源，均线口径可能与平时不同；不要把这种轮次的趋势判定当成可比的结论。

3. **接口失败先看响应，再判断类别。** 先跑 `python3 tools/verify_em_webguest.py` 查看各端点的响应（`OK` / `EMPTY` / `FAIL`、HTTP 状态码或异常类型），再结合 [`docs/东财请求频率与限流.md`](docs/东财请求频率与限流.md) 判断属于「限流 / 路径下线 / WAF 拦截」哪一类——脚本本身只提供线索，不给这三类结论。这三类都不要靠改代码、重装或加大重试硬顶。

4. **`--mode` 只决定输出哪些模块，不放宽任何门禁。** `--mode low` 是「额外输出低吸分类」，不是「门槛更松」；建仓门禁与真实仓权限只由 [`选股框架.md`](选股框架.md) 决定。

5. **控制请求量。** 上游对请求强度敏感：2026-09 实测腾讯日 K 被 WAF 拦截时，未加熔断的一轮筛选请求从 167 放大到 1173。看板不要改成全天候轮询（交易时段外它本来不发请求）。

## 十四、使用边界与社区规范

本项目定位为**本地自用的开源行情研究、数据处理和规则筛选工具**。它不是证券公司或证券投资咨询机构，**不是荐股软件**，不提供证券投资咨询、荐股、代客理财、代客下单或证券账户管理服务。

项目输出仅供技术研究、数据核验和个人信息整理，不构成任何证券或期货的买卖建议、收益承诺或内幕信息。使用者应自行核验数据、独立判断，并自行承担交易、部署、修改或传播本项目产生的风险和后果。任何 fork、二次开发或对外部署均由相应使用者自行负责。

在法律允许范围内，作者不对任何第三方因使用、修改、部署或传播本项目产生的交易损失、数据错误、系统中断或合规后果承担责任；本声明不排除法律规定不得排除的责任。项目的实际功能、运营方式和是否有偿，仍以实际情况为准，不因本声明而改变适用法律法规下的认定。

项目社区只讨论代码、数据处理、测试、文档和本地运行问题：

- 不讨论具体股票的买入、卖出、持仓、仓位、目标价或实时交易决策；
- 不接受自动交易、券商交易接口、自动报单/撤单、代客理财或账户管理相关 PR；
- 不提交账户、持仓、交易金额、个人决策记录、API 密钥或其他敏感数据。

详细贡献规则见 [`CONTRIBUTING.md`](CONTRIBUTING.md)；Issue 提交前请使用仓库提供的模板。

## 十五、许可证

Copyright 2026 LuQTest and contributors.

除另有说明的第三方内容外，本项目发布的源码、文档和示例配置采用 [Apache License 2.0](LICENSE)。完整许可证使用 [Apache 官方英文文本](https://www.apache.org/licenses/LICENSE-2.0.txt)；项目版权与来源说明见 [NOTICE](NOTICE)，第三方来源和许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。第三方组件仍遵循各自许可证。

本许可不授予第三方行情、公告或其他外部数据的使用和再分发权；这些数据应遵循各数据提供方的条款。本地私有报告、决策记录、持仓、账户、运行状态及密钥不作为本项目开源发布内容。

贡献采用相同的 Apache-2.0 许可证，详见 [贡献指南](CONTRIBUTING.md)。社区讨论与 PR 接收范围属于项目维护规则，不增加对 Apache-2.0 许可权利的限制。
