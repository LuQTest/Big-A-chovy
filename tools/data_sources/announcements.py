"""Announcement evidence and fail-closed primary/fallback orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .cninfo import CNInfoAnnouncementSource
from .contracts import Result, ResultStatus, result_empty, result_error, result_ok
from tools.rule_config import RULE_CONFIG


ANNOUNCEMENT_SOURCE = "announcement_evidence"

# A title that contains a hard-risk term and a resolution/negation phrase is
# ambiguous without reading the underlying notice. Keep it fail-closed as
# unknown instead of letting an ignore/document-type keyword produce clean.
_RISK_RESOLUTION_MARKERS = (
    "解除", "解冻", "撤销", "终止", "不再", "不存在", "未发生", "未涉及", "不涉及",
)

# These aliases all describe the total number of matching records, regardless
# of where an API places them in its response envelope. ``count`` describes
# the number of rows in the returned page and is validated separately.
_ANNOUNCEMENT_TOTAL_FIELDS = (
    "_provider_total", "total", "total_hits", "totalHits", "totalCount",
    "total_count", "totalAnnouncement",
)
_ANNOUNCEMENT_PAGE_ROW_COUNT_FIELDS = ("count", "row_count", "rowCount")


def _announcement_config() -> dict[str, list[str]]:
    """Read the single announcement policy owned by ``rule_config``."""
    config = RULE_CONFIG["risk"]["announcement"]
    return {
        "hard_keywords": list(config["hard_keywords"]),
        "watch_keywords": list(config["watch_keywords"]),
        "ignore_keywords": list(config["ignore_keywords"]),
    }


def classify_announcement_titles(titles: Iterable[str]) -> dict[str, list[str]]:
    """Classify titles using the same policy as the production screener."""
    config = _announcement_config()
    result = {"avoid": [], "watch_risk": [], "review": [], "ignored": [], "other": []}
    for value in titles:
        title = str(value or "").strip()
        if not title:
            continue
        bucket = "other"
        hard_match = any(word in title for word in config["hard_keywords"])
        watch_match = any(word in title for word in config["watch_keywords"])
        ignored = any(word in title for word in config["ignore_keywords"])
        if hard_match and any(marker in title for marker in _RISK_RESOLUTION_MARKERS):
            bucket = "review"
        elif hard_match:
            bucket = "avoid"
        elif watch_match:
            bucket = "watch_risk"
        elif ignored:
            bucket = "ignored"
        result[bucket].append(title)
    return result


def classify_announcement_risk(titles: Iterable[str]) -> dict[str, Any]:
    """Return the canonical risk result used by both evidence and screening."""
    config = _announcement_config()
    normalized = [str(value or "").strip() for value in titles if str(value or "").strip()]
    classification = classify_announcement_titles(normalized)
    hard = sorted({word for title in classification["avoid"] for word in config["hard_keywords"] if word in title})
    review = sorted({word for title in classification["review"] for word in config["hard_keywords"] if word in title})
    watch = sorted({word for title in classification["watch_risk"] for word in config["watch_keywords"] if word in title})
    if hard:
        risk = RULE_CONFIG["risk"]["statuses"]["avoid"]
    elif review:
        risk = RULE_CONFIG["risk"]["statuses"]["unknown"]
    elif watch:
        risk = RULE_CONFIG["risk"]["statuses"]["watch_risk"]
    else:
        risk = RULE_CONFIG["risk"]["statuses"]["clean"]
    return {
        "announcement_risk": risk,
        "announcement_keywords": hard or review or watch,
        "announcement_titles": normalized[:3],
        "announcement_ignored_titles": classification["ignored"][:3],
        "announcement_review_required": bool(review),
        "classification": classification,
    }


def _business_failure(payload: Mapping[str, Any]) -> str | None:
    """Find business-error envelopes before accepting an empty list."""
    if "error" in payload and payload.get("error") not in (None, "", False, 0):
        return f"业务 error={payload.get('error')!r}"
    if "success" in payload:
        success = payload.get("success")
        if success not in (True, 1, "1", "true", "True", "ok", "OK"):
            return f"业务 success={success!r}"
    if "code" in payload and payload.get("code") not in (None, "", 0, "0", 200, "200"):
        return f"业务 code={payload.get('code')!r}"
    if "data" in payload and payload.get("data") is None:
        return "业务 data=null"
    for key in ("data", "result"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            failure = _business_failure(nested)
            if failure:
                return failure
    return None


def extract_primary_announcement_container(
    value: Any,
) -> tuple[list[Any], list[Mapping[str, Any]]] | None:
    """Return announcement rows and every original envelope layer.

    Keeping the individual mappings is important: flattening nested payloads
    with ``dict.update`` discards contradictory outer totals when an inner
    object repeats a field name.
    """
    if isinstance(value, list):
        return value, []
    if not isinstance(value, Mapping):
        return None
    for key in ("rows", "list", "announcements"):
        rows = value.get(key)
        if isinstance(rows, list):
            return rows, [value]
    for key in ("data", "result"):
        if key not in value:
            continue
        found = extract_primary_announcement_container(value[key])
        if found is not None:
            rows, layers = found
            return rows, [value, *layers]
    return None


def _non_negative_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"公告 {field} 不是非负整数: {value!r}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        raise ValueError(f"公告 {field} 不是非负整数: {value!r}")
    if parsed < 0:
        raise ValueError(f"公告 {field} 为负数: {parsed}")
    return parsed


def extract_primary_announcement_total(metadata_layers: Iterable[Mapping[str, Any]]) -> int | None:
    """Validate and return the consistent total across same-meaning fields."""
    totals: list[tuple[str, int]] = []
    for layer_index, layer in enumerate(metadata_layers):
        for field in _ANNOUNCEMENT_TOTAL_FIELDS:
            if field not in layer or layer.get(field) in (None, ""):
                continue
            totals.append((f"layer[{layer_index}].{field}", _non_negative_integer(layer[field], field=field)))
    if not totals:
        return None
    values = {value for _field, value in totals}
    if len(values) != 1:
        detail = ", ".join(f"{field}={value}" for field, value in totals)
        raise ValueError(f"公告总量字段冲突: {detail}")
    return totals[0][1]


def validate_primary_announcement_page(
    rows: list[Any], metadata_layers: Iterable[Mapping[str, Any]], page_size: int,
) -> int | None:
    """Validate total-count evidence and any explicit page-row count."""
    layers = list(metadata_layers)
    total = extract_primary_announcement_total(layers)
    for layer_index, layer in enumerate(layers):
        for field in _ANNOUNCEMENT_PAGE_ROW_COUNT_FIELDS:
            if field not in layer or layer.get(field) in (None, ""):
                continue
            page_count = _non_negative_integer(layer[field], field=field)
            if page_count != len(rows):
                raise ValueError(
                    f"公告页内数量不匹配: layer[{layer_index}].{field}={page_count}, rows={len(rows)}"
                )
    _validate_page_count(rows, total, page_size)
    return total


def _validate_row_shape(rows: list[Any], *, code: str | None = None) -> list[dict[str, Any]]:
    """Require every non-empty announcement row to carry a usable title."""
    title_keys = ("title", "announcementTitle", "noticeTitle", "notice_title", "art_title", "artTitle", "TITLE")
    code_keys = ("SECURITY_CODE", "securityCode", "stockCode", "secCode", "security_code")
    normalized: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, str):
            title = item.strip()
            if not title:
                raise ValueError("公告记录标题为空")
            normalized.append({"title": title})
            continue
        if not isinstance(item, Mapping):
            raise ValueError("公告记录不是对象")
        title = next((str(item.get(key) or "").strip() for key in title_keys if str(item.get(key) or "").strip()), "")
        if not title:
            raise ValueError("公告非空页缺少可解析标题")
        if code:
            for key in code_keys:
                value = str(item.get(key) or "").strip()
                if value and value.split(".", 1)[0].zfill(6) != code:
                    raise ValueError(f"公告返回其他证券: {value}")
        row = dict(item)
        row["title"] = title
        normalized.append(row)
    return normalized


def _validate_page_count(rows: list[Any], total: Any, page_size: int) -> None:
    if total in (None, ""):
        if not rows:
            raise ValueError("公告空页缺少明确的 total=0")
        return
    if isinstance(total, bool):
        raise ValueError(f"公告 total 不是非负整数: {total!r}")
    try:
        if isinstance(total, int):
            total_int = total
        elif isinstance(total, str) and total.strip().isdecimal():
            total_int = int(total.strip())
        else:
            raise ValueError(f"公告 total 不是非负整数: {total!r}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"公告 total 不是非负整数: {total!r}") from exc
    if total_int < 0:
        raise ValueError(f"公告 total 为负数: {total_int}")
    expected = min(total_int, max(1, int(page_size)))
    if len(rows) != expected:
        raise ValueError(f"公告页不完整: total={total_int}, page_size={page_size}, rows={len(rows)}")


def _primary_container(value: Any) -> tuple[list[Any], list[Mapping[str, Any]]] | None:
    """Compatibility name for the shared, non-flattening container extractor."""
    return extract_primary_announcement_container(value)


def _primary_rows(value: Any, *, page_size: int = 30) -> tuple[list[dict[str, Any]], str]:
    if isinstance(value, Result):
        if value.status in {ResultStatus.OK.value, ResultStatus.EMPTY.value} and isinstance(value.data, list):
            return _validate_row_shape(value.data), value.source_url
        raise ValueError(value.error.get("message") if isinstance(value.error, Mapping) else "主公告源不可用")
    if isinstance(value, Mapping):
        failure = _business_failure(value)
        if failure:
            raise ValueError(f"主公告源{failure}")
        found = _primary_container(value)
        if found is None:
            raise ValueError("主公告源缺少公告列表")
        rows, metadata_layers = found
        validate_primary_announcement_page(rows, metadata_layers, page_size)
        source_url = str(value.get("source_url") or next(
            (layer.get("source_url") for layer in metadata_layers if layer.get("source_url")), ""
        ))
        return _validate_row_shape(rows), source_url
    if isinstance(value, list):
        validate_primary_announcement_page(value, [], page_size)
        return _validate_row_shape(value), ""
    raise ValueError("主公告源返回结构异常")


def fetch_announcement_evidence(
    code: str,
    *,
    primary: Callable[[], Any] | None = None,
    fallback: CNInfoAnnouncementSource | None = None,
    fallback_factory: Callable[[], CNInfoAnnouncementSource] | None = None,
    page_size: int = 30,
) -> Result:
    """Try the current primary source, then CNINFO, preserving failure state."""
    primary_error: str | None = None
    if primary is not None:
        try:
            rows, source_url = _primary_rows(primary(), page_size=page_size)
            normalized = []
            for row in rows:
                title = str(row.get("title") or row.get("announcementTitle") or "").strip()
                if not title:
                    continue
                item = dict(row)
                item["title"] = title
                item.setdefault("source", "primary_announcement")
                normalized.append(item)
            status = ResultStatus.OK if normalized else ResultStatus.EMPTY
            risk = classify_announcement_risk(item["title"] for item in normalized)
            return Result(
                status=status,
                data={"rows": normalized, "classification": risk["classification"], "announcement_risk": risk["announcement_risk"], "announcement_keywords": risk["announcement_keywords"]},
                source="primary_announcement",
                source_url=source_url,
                freshness="fresh",
            )
        except Exception as exc:  # primary errors are evidence for fallback, not clean state
            primary_error = str(exc)
    fallback = fallback_factory() if fallback_factory is not None else fallback or CNInfoAnnouncementSource()
    result = fallback.fetch(code, page_size=page_size)
    if result.status in {ResultStatus.OK.value, ResultStatus.EMPTY.value}:
        rows = result.data if isinstance(result.data, list) else []
        risk = classify_announcement_risk(item.get("title", "") for item in rows if isinstance(item, Mapping))
        result.data = {"rows": rows, "classification": risk["classification"], "announcement_risk": risk["announcement_risk"], "announcement_keywords": risk["announcement_keywords"]}
        result.source = f"{result.source}:fallback"
        if primary_error:
            result.warnings.append(f"主公告源失败，已切换巨潮: {primary_error}")
        return result
    message = "主公告源与巨潮公告均不可用"
    if primary_error:
        message += f"；主源: {primary_error}"
    if isinstance(result.error, Mapping) and result.error.get("message"):
        message += f"；巨潮: {result.error['message']}"
    return result_error(ResultStatus.UNAVAILABLE, source=ANNOUNCEMENT_SOURCE, source_url=result.source_url, code="all_announcement_sources_failed", message=message, retryable=True)
