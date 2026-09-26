"""腾讯日 K 主机与请求的单一来源（标准库实现，不依赖 requests）。

为什么单独一个模块：这套主机列表原先在筛选引擎、实时看板、三个查询工具里各写了一份。
2026-09-26 实测 `web.ifzq.gtimg.cn` 的 `/appstock/app/fqkline/get` 被 WAF 拦截（HTTP 501，
代理与直连都一样），当时只改了引擎，另外三处仍然打在失效主机上——正是"改一处漏三处"。
主机列表收口到这里，任何一处失效只改这里。

各调用方保留自己的传输层：
- 筛选引擎用 `fetch_json`（带多网络路径实测择优）；
- 命令行工具与本模块的 `fetch_kline_json` 用标准库直连。
"""
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# 按可用性排序；2026-09-26 实测前两个正常，第三个被 WAF 拦截但保留以便恢复后自动启用。
TENCENT_KLINE_URLS: Tuple[str, ...] = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)

DEFAULT_TIMEOUT = 6


def kline_urls() -> List[str]:
    return list(TENCENT_KLINE_URLS)


def _symbol(code: str) -> str:
    normalized = str(code).strip().lower()
    if normalized.startswith(("sh", "sz", "bj")):
        return normalized
    return ("sh" if normalized.startswith(("6", "9")) else "sz") + normalized


def fetch_kline_json(
    code: str,
    count: int = 90,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    require_qfq: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], str]:
    """按主机顺序拉一只股票的日 K 原始 JSON，返回 (payload, 实际使用的主机 url)。

    传 start（YYYY-MM-DD）时按日期区间取数（如 YTD 需要锚定去年末收盘），
    否则按 count 取最近若干根。全部主机失败时抛 RuntimeError，不返回空壳数据——
    调用方据此标注数据缺失，不要静默当成 0 或空。

    require_qfq=True 时只接受前复权数据（`qfqday`）：若某台主机只返回未复权的 `day`，
    不算成功，继续试下一台；全都只有未复权数据则抛错，避免把未复权价当复权价用。
    指数没有复权概念，取指数时应保持 False。
    """
    symbol = _symbol(code)
    if start:
        param = f"{symbol},day,{start},{end or ''},{count},qfq"
    else:
        param = f"{symbol},day,,,{count},qfq"
    query = urllib.parse.urlencode({"param": param})
    request_headers = {"User-Agent": "Mozilla/5.0", **(headers or {})}
    errors: List[str] = []
    for base in TENCENT_KLINE_URLS:
        url = f"{base}?{query}"
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(
                req, context=ssl._create_unverified_context(), timeout=timeout
            ) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                errors.append(f"{base}: 响应不是对象")
                continue
            # 只看有没有 data 不够：某些主机可能返回空壳（无目标代码、无 K 线行），
            # 那不算成功，必须继续尝试下一台，否则会把一次可恢复的失败当成最终结果。
            rows = kline_rows(payload, code, require_qfq=require_qfq)
            if rows:
                return payload, base
            if kline_rows(payload, code):
                errors.append(f"{base}: 只有未复权数据，但本次要求前复权")
            else:
                errors.append(f"{base}: 无 {_symbol(code)} 的 K 线数据")
        except Exception as exc:  # 包含 WAF 拦截页（非 JSON）
            errors.append(f"{base}: {type(exc).__name__}")
    raise RuntimeError("腾讯日 K 全部主机失败 -> " + "; ".join(errors))


def adjustment_type(payload: Dict[str, Any], code: str) -> str:
    """返回该响应实际提供的复权类型：'qfq'（前复权）/ 'raw'（未复权）/ ''（无数据）。"""
    symbol = _symbol(code)
    node = ((payload or {}).get("data") or {}).get(symbol) or {}
    if node.get("qfqday"):
        return "qfq"
    if node.get("day"):
        return "raw"
    return ""


def kline_rows(payload: Dict[str, Any], code: str, *, require_qfq: bool = False) -> List[List[str]]:
    """从原始 JSON 取出 K 线行；取不到返回空列表。

    require_qfq=True 时**不**回退到未复权的 `day`：宁可返回空让调用方报"无前复权数据"，
    也不能把未复权价标成前复权展示（除权日前后会失真）。
    """
    symbol = _symbol(code)
    node = ((payload or {}).get("data") or {}).get(symbol) or {}
    qfq = node.get("qfqday")
    if qfq:
        return list(qfq)
    if require_qfq:
        return []
    return list(node.get("day") or [])
