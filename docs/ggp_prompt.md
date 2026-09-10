# ggp.v1 — Gemini 轮询结构化输出提示词

将本文件作为 Gemini 执行 `ggp` 的输出约束。完整字段定义见同目录的 `ggp_output_schema.json`。

## 输出硬约束

每轮只输出一个 JSON 对象：首字符必须是 `{`，末字符必须是 `}`。禁止 Markdown 代码围栏、标题、解释、前后置文本、注释和第二个对象。

必须严格使用 `ggp.v1` 字段。不得新增字段，不得改名，不得把数字格式化成带单位的字符串。

- 股票代码永远是六位字符串，例如 `"000591"`，不能输出数字 `591`。
- 不知道的值用 `null`；没有项目用 `[]`；不要用 `"-"`、`"未知"`、`"N/A"` 代替。
- 百分比用数字，例如 `2.35` 表示 2.35%。金额字段以万元为单位，使用数字。
- `decision` 下的 `real_open`、`simulated_buy`、`watch_or_real_blocked`、`fully_cash` 四个字段永远同时输出。
- `fully_cash` 只有在 `real_open` 和 `simulated_buy` 都为空时才为 `true`；此时仍可有观察项。
- `status=partial` 时保留已核验事实，并把缺失原因写入 `errors` 或 `warnings`，不得补猜。

## ggp 执行范围

1. 读取《选股框架.md》、最新《决策记录/YYYYMMDD.md》和上次提问到当前时间之间的全部筛选报告；不能只看最新报告。
2. 继续看筛选：覆盖超短池、趋势观察/确认池、双池交集、低吸 A/B/C 和每份报告底部的明日观察池。
3. 盘问候选：需要实时行情时读取报价；准备真实仓开仓时必须核验分笔、五档、基本面盈利状态和 YTD，并把事实写入候选的 `support`。
4. 盘问持仓：读取模拟仓、真实仓、`real_mother` 和现金，写入 `holdings`；把次日 T+1 计划写入 `t1_plan`。

## 决策分层

交易条件与权限只引用《选股框架.md》“一、建仓门禁”“四、分层裁决”“六、实验登记”，不在提示词复制阈值。

- `real_open`：框架正式门禁全过的真实仓建议，每个标的必须填写七项非空 `support` 与完整 `buy_plan`；人类显示层必须保留七项支撑。
- `simulated_buy`：框架允许的模拟候选，实验类别与真实仓卡点必须写清。仅影子采样的龙头分歧放观察层，不能升级为可买。
- `watch_or_real_blocked`：仅观察、数据缺失、门禁不合格或仅影子采样的标的，说明 `blockers`。
- `fully_cash`：仅表示没有新增可买候选，不表示已有持仓为空。

预测不能代替规则；实际成交须有用户反馈；T+1执行顺序引用框架“二、离场”。

## 固定顶层形状

```json
{
  "schema_version": "ggp.v1",
  "status": "complete",
  "as_of": null,
  "summary": "",
  "poll": {
    "last_user_message_at": null,
    "reports_checked": [],
    "report_count": 0,
    "latest_report": null,
    "data_sources": []
  },
  "market": {
    "summary": "",
    "indices": [],
    "breadth": {
      "advancing": null,
      "declining": null,
      "flat": null,
      "limit_up": null,
      "limit_down": null
    }
  },
  "decision": {
    "overall": "fully_cash",
    "real_open": [],
    "simulated_buy": [],
    "watch_or_real_blocked": [],
    "fully_cash": true,
    "reason": ""
  },
  "holdings": {
    "simulated": [],
    "real": [],
    "real_mother": [],
    "cash": {
      "real_available": null
    }
  },
  "t1_plan": [],
  "errors": [],
  "warnings": []
}
```

输出前自检：JSON 可解析；所有顶层字段都存在；代码为六位字符串；四个决策字段都存在；`fully_cash` 与两个可买数组一致；没有 Markdown 或额外文本。
