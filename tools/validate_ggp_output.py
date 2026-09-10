#!/usr/bin/env python3
"""Validate the stable ggp.v1 output contract without third-party packages.

Usage:
  python3 tools/validate_ggp_output.py response.json
  cat response.json | python3 tools/validate_ggp_output.py -
"""

from __future__ import annotations

import argparse
import json
from math import isfinite
import sys
from typing import Any, Iterable


TOP_LEVEL = {
    "schema_version", "status", "as_of", "summary", "poll", "market",
    "decision", "holdings", "t1_plan", "errors", "warnings",
}
DECISION_FIELDS = {
    "overall", "real_open", "simulated_buy", "watch_or_real_blocked",
    "fully_cash", "reason",
}
POSITION_BUCKETS = {"simulated", "real", "real_mother"}
SUPPORT_FIELDS = {"sector", "capital", "order_book", "vwap", "fundamentals", "simulation", "risk_reward"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _check_code(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        errors.append(f"{path} must be a six-character stock-code string")


def _check_string_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{path} must be an array of strings")


def _check_candidate(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return
    required = {
        "code", "name", "source", "classification", "signal_type", "current",
        "capital", "risk", "buy_plan", "blockers", "support",
    }
    missing = required - value.keys()
    if missing:
        errors.append(f"{path} missing: {', '.join(sorted(missing))}")
    _check_code(value.get("code"), f"{path}.code", errors)
    if not isinstance(value.get("name"), str):
        errors.append(f"{path}.name must be a string")
    _check_string_list(value.get("blockers"), f"{path}.blockers", errors)
    for section in ("current", "capital", "risk", "buy_plan", "support"):
        if not isinstance(value.get(section), dict):
            errors.append(f"{path}.{section} must be an object")


def _check_position(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return
    _check_code(value.get("code"), f"{path}.code", errors)
    if not isinstance(value.get("name"), str):
        errors.append(f"{path}.name must be a string")


def _check_real_candidate(value: Any, path: str, errors: list[str]) -> None:
    """核验已声明真实仓建议的证据完整性；不替代行情核验和框架裁决。"""
    if not isinstance(value, dict):
        return
    if value.get("classification") != "real_open" or value.get("signal_type") != "absolute":
        errors.append(f"{path} requires real_open classification and absolute signal")
    risk = value.get("risk")
    if not isinstance(risk, dict) or risk.get("announcement") not in {"clean", "watch_risk"} or risk.get("vetoes") != []:
        errors.append(f"{path} has missing risk evidence or a veto")
    if value.get("blockers") != []:
        errors.append(f"{path} must not contain unresolved blockers")
    capital = value.get("capital") or {}
    if not isinstance(capital, dict) or capital.get("dominance_type") != "absolute":
        errors.append(f"{path} requires absolute capital dominance")
    elif not _is_number(capital.get("super_net_wan")) or capital["super_net_wan"] <= 0:
        errors.append(f"{path} requires positive super_net_wan")
    support = value.get("support") or {}
    if not isinstance(support, dict) or any(not isinstance(support.get(key), str) or not support[key].strip() for key in SUPPORT_FIELDS):
        errors.append(f"{path} requires all seven non-empty support fields")
    plan = value.get("buy_plan") or {}
    if not isinstance(plan, dict):
        return
    prices = ("buy_low", "buy_high", "stop", "take_profit")
    if any(not _is_number(plan.get(key)) or plan[key] <= 0 for key in prices):
        errors.append(f"{path} requires complete positive buy/stop/take-profit prices")
    elif not plan["stop"] < plan["buy_low"] <= plan["buy_high"] < plan["take_profit"]:
        errors.append(f"{path} has inconsistent buy/stop/take-profit prices")
    if not isinstance(plan.get("t1_action"), str) or not plan["t1_action"].strip():
        errors.append(f"{path} requires a T+1 action")


def validate(obj: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["top-level value must be a JSON object"]

    missing = TOP_LEVEL - obj.keys()
    extra = obj.keys() - TOP_LEVEL
    if missing:
        errors.append(f"top-level missing: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"top-level extra fields: {', '.join(sorted(extra))}")
    if obj.get("schema_version") != "ggp.v1":
        errors.append("schema_version must be ggp.v1")
    if obj.get("status") not in {"complete", "partial", "error"}:
        errors.append("status must be complete, partial, or error")
    if not isinstance(obj.get("summary"), str):
        errors.append("summary must be a string")
    _check_string_list(obj.get("errors"), "errors", errors)
    _check_string_list(obj.get("warnings"), "warnings", errors)

    poll = obj.get("poll")
    if not isinstance(poll, dict):
        errors.append("poll must be an object")
    else:
        for key in ("last_user_message_at", "latest_report"):
            if poll.get(key) is not None and not isinstance(poll.get(key), str):
                errors.append(f"poll.{key} must be a string or null")
        _check_string_list(poll.get("reports_checked"), "poll.reports_checked", errors)
        if not isinstance(poll.get("report_count"), int) or poll.get("report_count", -1) < 0:
            errors.append("poll.report_count must be a non-negative integer")
        _check_string_list(poll.get("data_sources"), "poll.data_sources", errors)

    market = obj.get("market")
    if not isinstance(market, dict):
        errors.append("market must be an object")
    else:
        if not isinstance(market.get("summary"), str):
            errors.append("market.summary must be a string")
        if not isinstance(market.get("indices"), list):
            errors.append("market.indices must be an array")
        else:
            for i, item in enumerate(market["indices"]):
                if not isinstance(item, dict):
                    errors.append(f"market.indices[{i}] must be an object")
        breadth = market.get("breadth")
        if not isinstance(breadth, dict):
            errors.append("market.breadth must be an object")

    decision = obj.get("decision")
    if not isinstance(decision, dict):
        errors.append("decision must be an object")
    else:
        missing_decision = DECISION_FIELDS - decision.keys()
        extra_decision = decision.keys() - DECISION_FIELDS
        if missing_decision:
            errors.append(f"decision missing: {', '.join(sorted(missing_decision))}")
        if extra_decision:
            errors.append(f"decision extra fields: {', '.join(sorted(extra_decision))}")
        if decision.get("overall") not in {"real_open", "simulated_only", "fully_cash"}:
            errors.append("decision.overall is invalid")
        if not isinstance(decision.get("reason"), str):
            errors.append("decision.reason must be a string")
        if not isinstance(decision.get("fully_cash"), bool):
            errors.append("decision.fully_cash must be boolean")
        else:
            real_open = decision.get("real_open")
            simulated_buy = decision.get("simulated_buy")
            if not isinstance(real_open, list):
                errors.append("decision.real_open must be an array")
            if not isinstance(simulated_buy, list):
                errors.append("decision.simulated_buy must be an array")
            if isinstance(real_open, list) and isinstance(simulated_buy, list):
                expected_cash = not real_open and not simulated_buy
                if decision["fully_cash"] != expected_cash:
                    errors.append("decision.fully_cash disagrees with real_open/simulated_buy")
                expected_overall = "real_open" if real_open else "simulated_only" if simulated_buy else "fully_cash"
                if decision.get("overall") != expected_overall:
                    errors.append("decision.overall disagrees with candidate arrays")
        for key in ("real_open", "simulated_buy", "watch_or_real_blocked"):
            rows = decision.get(key)
            if isinstance(rows, list):
                for i, row in enumerate(rows):
                    _check_candidate(row, f"decision.{key}[{i}]", errors)
                    if key == "real_open":
                        _check_real_candidate(row, f"decision.{key}[{i}]", errors)
                    elif key == "simulated_buy" and isinstance(row, dict) and row.get("signal_type") == "divergence_leader":
                        errors.append(f"decision.{key}[{i}] divergence_leader is shadow collection only")

    holdings = obj.get("holdings")
    if not isinstance(holdings, dict):
        errors.append("holdings must be an object")
    else:
        for key in POSITION_BUCKETS:
            rows = holdings.get(key)
            if not isinstance(rows, list):
                errors.append(f"holdings.{key} must be an array")
            else:
                for i, row in enumerate(rows):
                    _check_position(row, f"holdings.{key}[{i}]", errors)
        cash = holdings.get("cash")
        if not isinstance(cash, dict):
            errors.append("holdings.cash must be an object")
        elif cash.get("real_available") is not None and not _is_number(cash.get("real_available")):
            errors.append("holdings.cash.real_available must be a number or null")

    if not isinstance(obj.get("t1_plan"), list):
        errors.append("t1_plan must be an array")
    else:
        for i, item in enumerate(obj["t1_plan"]):
            if not isinstance(item, dict):
                errors.append(f"t1_plan[{i}] must be an object")
            else:
                _check_code(item.get("code"), f"t1_plan[{i}].code", errors)
                for key in ("name", "priority", "action", "condition"):
                    if not isinstance(item.get(key), str):
                        errors.append(f"t1_plan[{i}].{key} must be a string")
    return errors


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a ggp.v1 JSON response")
    parser.add_argument("path", help="JSON file to validate, or - for stdin")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
        obj = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID: {exc}")
        return 1

    errors = validate(obj)
    if errors:
        print("INVALID")
        for error in errors:
            print(f"- {error}")
        return 1
    print("OK: ggp.v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
