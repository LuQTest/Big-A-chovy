# Web 工作台（B/S 架构）使用说明

> 把 FOR-BIG-A 的筛选、看板、报告和工具能力搬到浏览器：默认仅本机访问；如确实需要局域网设备访问，可显式绑定局域网地址。**仍然不会自动下单**，规则以《选股框架.md》为准。

## 架构

```
浏览器 (任意设备)                服务器 (一台常开机机器)
┌─────────────────────┐         ┌────────────────────────────────┐
│  /workbench 工作台   │  HTTP   │  web_workbench.py              │
│   ├ 筛选工作台       │ ──────► │   ├ /api/wb/screen/*  一次性筛选│
│   ├ 报告库          │         │   ├ /api/wb/reports/md 报告库   │
│   └ 工具箱          │         │   ├ /api/wb/quote|scan|... 工具 │
│  / 实时看板（原版）   │         │   ├ realtime_engine  筛选引擎   │
└─────────────────────┘         │   └ network_path    网络择优    │
                                └────────────────────────────────┘
```

- 后端：Python 标准库 `http.server`，复用看板已验证的 `realtime_engine` 管线（含 `network_path` 直连/代理实测择优）。
- 前端：原生 HTML/JS/CSS，无外部 CDN 依赖，离线可用；与实时看板同一套 Catppuccin 配色。
- 单端口 8765 同时提供工作台与原版实时看板，原有看板 API（`/api/data`、`/api/status` 等）完全不变；Docker 也通过工作台入口提供两套页面。

## 快速开始

### Windows

双击项目根目录 `启动工作台.bat`（自动检测 Python，缺失时回退到 uv 自动装 Python 3.13 + 依赖）。

或手动：

```powershell
uv run --python 3.13 --with requests --with pyyaml --with tzdata python daily-stock-analysis/scripts/web_workbench.py
```

### macOS / Linux

```bash
python3 daily-stock-analysis/scripts/web_workbench.py
```

### Docker（工作台与实时看板共用端口）

`docker-compose.yml` 启动工作台与实时看板，原有看板路径不变。宿主机端口默认只绑定 `127.0.0.1`，避免把报告和持仓快照暴露到局域网。

如确实需要局域网访问，请先确认网络可信，再把 compose 端口映射改为 `8765:8765`，不要把端口直接暴露到公网。

浏览器打开：

- 工作台：<http://localhost:8765/workbench>
- 实时看板：<http://localhost:8765/>

## 功能清单

| 页签 | 功能 | 对应原有入口 |
|---|---|---|
| 筛选工作台 | 选模块（严格双池/低吸/观察池）、条数、网络模式，后台执行+进度轮询，完成后在线渲染 Markdown 报告并落盘 `筛选结果/` | GUI 一次性筛选 / CLI `--mode all` |
| 报告库 | 浏览 `筛选结果/**/*.md`，点击在线阅读（含表格渲染、红涨绿跌） | 手动翻文件 |
| 工具箱 | 实时行情+五档、基本面查询（建仓前必验）、报告扫描（5/5、4/5）、持仓快照、T+1 观察池验证、单股全天跟踪 | `tools/query_quote.py` 等 6 个 CLI 工具 |
| 实时看板 | 原版页面与逻辑原样保留 | `realtime_dashboard.py` |

## API 一览（工作台新增，均带 `/api/wb/` 前缀）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/wb/screen/run` | 启动筛选任务，body: `{modes, top, network_mode, skip_announcements, skip_capital_ranking}` |
| GET | `/api/wb/job` | 任务状态（running/done/error、耗时、是否降级） |
| GET | `/api/wb/result` | 最近任务完整 JSON |
| GET | `/api/wb/report` | 最近任务 Markdown |
| GET | `/api/wb/reports` | 报告列表（按修改时间倒序） |
| GET | `/api/wb/md?path=` | 读取单份报告（限制在 `筛选结果/` 内，防路径穿越） |
| GET | `/api/wb/quote?codes=&minute=&kline=` | 行情/分时/日K |
| GET | `/api/wb/scan?date=&latest=&file=` | 报告扫描；`file` 只能指向 `筛选结果/` 下的 Markdown |
| GET | `/api/wb/position?date=` | 持仓/观察池快照 |
| GET | `/api/wb/verify_t1?date=` | T+1 验证 |
| GET | `/api/wb/track?code=&date=` | 单股跟踪 |
| GET | `/api/wb/financials?code=` | 基本面 |

## 并发与安全设计

- 手动筛选任务与看板自动刷新**共用同一把筛选锁**，同一时刻只有一个引擎在跑，避免模块级数据竞争。
- 筛选超时（900s）不会杀死引擎线程（Python 无法杀线程），而是进入「僵尸收割」：锁由收割线程等引擎真正结束后释放，期间新任务返回明确的 busy 原因。
- 报告读取和报告扫描严格限制在 `筛选结果/` 目录内，并拒绝符号链接越界；查询参数支持 UTF-8 与 GBK 双编码解码（兼容 Windows 中文命令行客户端）。
- 工作台默认监听 `127.0.0.1`，不发送通配符 CORS；带 `Origin` 的跨源 API 请求会被拒绝。
- 报告、持仓、决策记录仍只保存在本机，不会自动写入镜像或上传 GitHub（`.gitignore` 原样生效）；但工作台页面会按请求把这些本地数据展示给当前浏览器，因此不要在不可信网络使用 `--host 0.0.0.0`。

## Windows 性能注意（实测）

- 盘中全模式筛选：首次约 70~90 秒（K 线缓存冷）；缓存命中后更快。
- **收盘后（15:00+）东财接口行为变化，全模式可能拖到 10 分钟以上**——建议收盘后只跑 `strict` 单模式，或等下一交易日再用；超时后后台任务会显示"仍在执行中"。
- Windows 每次新建 SSL 连接需加载系统证书库，冷启动比 macOS 慢属于正常现象。
- `--no-dashboard-refresh`：不启动看板盘中自动刷新/预热（纯手动模式，适合非交易时段或低配机）。

## 已知边界

- 工作台不提供下单、改仓、止损单等任何交易执行能力（与项目红线一致）。
- Tkinter GUI（macOS 专用）保持原样，未迁移；如需 GUI 功能可在工作台提 issue。
