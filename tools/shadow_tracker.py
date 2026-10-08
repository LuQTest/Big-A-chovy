#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
20样本量化影子验证系统 (Shadow Tracking System) - 权威升级版
来源：《选股框架.md》与《CLAUDE.md》2026-08-21 优化待验证项

负责采集与追踪 4 大待验证创新机制的实战样本：
1. coalition: 机构游资合力主升（超大单≥2000万、大单>0、主力≥5000万、
   20%<=超单/主力<50%、5分增量≥1000万、连续两期主力与超大单均未衰减、
   主买比≥1.5）；采样只接受生产报告明确标签 `✓(合力)`，不在采集端数值推导
2. breakout: 明日观察池突破升级状态机 (CONFIRMED / B_BREAKOUT / A_STRICT)
3. sector_boost: 主线板块协同加分器 (20亿锚点 + 3只共振 + 协同加15分)
4. divergence: 龙头分歧识别(divergence_leader·待验证项⑨)，由
   detect_divergence_leader.py --record 全日序列判定后写入；本模块只负责
   保留与 T+1 结算，不做采集端数值推导

严格仅用于模拟仓影子验证，记录 T+1 09:45 收益、最大浮盈、最大回撤与假突破率。
"""

import os
import sys
import json
import re
import argparse
import importlib
import copy
import tempfile
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Any, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific import
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - platform-specific import
    msvcrt = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "daily-stock-analysis" / "scripts"
for import_path in (PROJECT_ROOT, TOOLS_DIR, SCRIPTS_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from tools.report_parser import parse_screening_report
from tools.rule_config import (
    RULE_CONFIG,
    is_complete_shadow_result,
    normalize_hhmm,
    shadow_targets,
)

_shadow_dir_override = os.environ.get("A_SHARE_SHADOW_DATA_DIR", "").strip()
SHADOW_DATA_DIR = (
    Path(_shadow_dir_override).expanduser()
    if _shadow_dir_override
    else Path(__file__).resolve().parent / "shadow_data"
)
SHADOW_DB_FILE = SHADOW_DATA_DIR / "shadow_samples.json"
T1_PENDING = "待补算"
T1_SOURCE_DAILY_KLINE = "daily_kline"
T1_SOURCE_REPORT_SNAPSHOTS_ONLY = "report_snapshots_only"
T1_SOURCE_UNAVAILABLE = "unavailable"
T1_SOURCE_LEGACY_UNVERIFIED = "legacy_unverified"
_DB_THREAD_LOCK = threading.RLock()


class ShadowDatabaseError(ValueError):
    """The on-disk shadow database is malformed and must not be overwritten."""


class ShadowDatabaseConflict(ShadowDatabaseError):
    """A stale whole-database snapshot would remove or overwrite newer data."""


def _msvcrt_lock_available() -> bool:
    """Return whether the Windows byte-range locking primitive is usable."""
    return msvcrt is not None and all(
        getattr(msvcrt, name, None) is not None
        for name in ("locking", "LK_LOCK", "LK_UNLCK")
    )


def _prepare_msvcrt_lock_file(handle) -> None:
    """Ensure byte zero exists before asking msvcrt to lock it.

    ``msvcrt.locking`` locks ``nbytes`` from the descriptor's current file
    position and may lock beyond EOF.  An explicit byte makes creation and
    cleanup deterministic across Windows filesystems and Python runtimes.
    """
    handle.seek(0, os.SEEK_END)
    if handle.tell() < 1:
        handle.seek(0)
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)


def _empty_db() -> Dict[str, Any]:
    targets = shadow_targets()
    return {
        "version": "2.0",
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targets": targets,
        "samples": {category: [] for category in targets},
    }


def _validate_db(db: Any) -> Dict[str, Any]:
    if not isinstance(db, dict):
        raise ShadowDatabaseError("影子样本库顶层结构不是对象")
    normalized = copy.deepcopy(db)
    targets = normalized.setdefault("targets", {})
    samples = normalized.setdefault("samples", {})
    if not isinstance(targets, dict) or not isinstance(samples, dict):
        raise ShadowDatabaseError("影子样本库 targets/samples 必须是对象")
    for category, meta in targets.items():
        if not isinstance(meta, dict):
            raise ShadowDatabaseError(f"影子样本库 targets.{category} 必须是对象")
    for category, rows in samples.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ShadowDatabaseError(f"影子样本库 samples.{category} 必须是对象数组")
        if any(row.get("t1_result") is not None and not isinstance(row.get("t1_result"), dict) for row in rows):
            raise ShadowDatabaseError(f"影子样本库 samples.{category} 的 t1_result 必须是对象或 null")
    for category, meta in shadow_targets().items():
        targets.setdefault(category, dict(meta))
        samples.setdefault(category, [])
    for category in set(targets) | set(samples):
        targets.setdefault(category, {"name": category, "target_samples": int(RULE_CONFIG["shadow"]["target_samples"])})
        samples.setdefault(category, [])
        target = targets[category].get("target_samples")
        if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
            raise ShadowDatabaseError(f"影子样本库 targets.{category}.target_samples 必须是正整数")
    normalized.setdefault("version", "2.0")
    normalized.setdefault("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return normalized


@contextmanager
def _database_lock():
    """Serialize a complete database transaction across threads and processes."""
    if fcntl is None and not _msvcrt_lock_available():
        raise ShadowDatabaseError(
            "当前平台没有可用的跨进程文件锁，拒绝写入影子样本库以避免并发丢失历史"
        )
    SHADOW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SHADOW_DB_FILE.with_name(f".{SHADOW_DB_FILE.name}.lock")
    with _DB_THREAD_LOCK:
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return

            # Windows has no fcntl.  Use the C runtime's byte-range lock
            # instead of silently downgrading to a thread lock or allowing an
            # unsafe write.  LK_LOCK retries for a bounded period; failure is
            # still a write refusal.
            _prepare_msvcrt_lock_file(handle)
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise ShadowDatabaseError(
                    "无法获取 Windows 跨进程文件锁，拒绝写入影子样本库以避免并发丢失历史"
                ) from exc
            try:
                yield
            finally:
                # The locked region starts at the descriptor's current
                # position; seek back before unlocking, including after an
                # exception in the transaction body.
                pending_error = sys.exc_info()
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError as exc:
                    # Preserve a transaction exception if there is one; on a
                    # normal exit, an unlock failure is itself a write
                    # refusal rather than a silent safety downgrade.
                    if pending_error[0] is None:
                        raise ShadowDatabaseError(
                            "无法释放 Windows 跨进程文件锁，拒绝完成影子样本库写入"
                        ) from exc


def _atomic_save_db(db: Dict[str, Any]) -> None:
    validated = _validate_db(db)
    validated["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    serialized = json.dumps(validated, ensure_ascii=False, indent=2, allow_nan=False)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=SHADOW_DATA_DIR,
            prefix=f".{SHADOW_DB_FILE.name}.", suffix=f".{uuid.uuid4().hex}.tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, SHADOW_DB_FILE)
        try:
            dir_fd = os.open(SHADOW_DATA_DIR, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        db.clear()
        db.update(validated)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def init_db() -> Dict[str, Any]:
    """初始化或加载影子样本数据库。"""
    if SHADOW_DB_FILE.exists():
        try:
            data = json.loads(SHADOW_DB_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ShadowDatabaseError(f"影子样本库 JSON 无法读取，拒绝覆盖: {exc}") from exc
        return _validate_db(data)
    return _empty_db()


def _snapshot_signature(row: Dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _assert_snapshot_preserves_current(current: Dict[str, Any], candidate: Dict[str, Any]) -> None:
    """Reject stale whole-file snapshots that omit or alter committed data."""
    for key, value in current.items():
        if key == "last_updated":
            continue
        if key not in candidate or candidate[key] != value:
            if key in {"targets", "samples"}:
                continue
            raise ShadowDatabaseConflict(f"旧快照缺少或改写了数据库字段 {key!r}，拒绝覆盖")

    current_targets = current.get("targets") or {}
    candidate_targets = candidate.get("targets") or {}
    for category, metadata in current_targets.items():
        if candidate_targets.get(category) != metadata:
            raise ShadowDatabaseConflict(
                f"旧快照未保留较新的目标配置 {category!r}，拒绝覆盖"
            )

    current_samples = current.get("samples") or {}
    candidate_samples = candidate.get("samples") or {}
    for category, current_rows in current_samples.items():
        proposed_rows = candidate_samples.get(category)
        if proposed_rows is None:
            raise ShadowDatabaseConflict(f"旧快照缺少类别 {category!r}，拒绝覆盖")
        required = Counter(_snapshot_signature(row) for row in current_rows)
        proposed = Counter(_snapshot_signature(row) for row in proposed_rows)
        if required - proposed:
            raise ShadowDatabaseConflict(
                f"旧快照会删除或改写 {category!r} 类中已提交的样本，拒绝覆盖"
            )


def mutate_db(mutator: Callable[[Dict[str, Any]], Any]) -> Tuple[Any, Dict[str, Any]]:
    """Run read → mutate → validate → atomic commit under one process lock.

    The callback receives the latest database snapshot while the interprocess
    lock is held and must mutate it in place. The result and committed snapshot
    are returned after the lock-protected write succeeds.
    """
    with _database_lock():
        db = init_db()
        result = mutator(db)
        validated = _validate_db(db)
        _atomic_save_db(validated)
        return result, copy.deepcopy(validated)


def save_db(db: Dict[str, Any]) -> None:
    """Save a snapshot only if it still contains every currently committed value.

    New rows and targets can be added for compatibility with existing callers,
    but a snapshot that predates another write is rejected instead of replacing
    the newer database. Concurrent production writers should use ``mutate_db``.
    """
    candidate = _validate_db(db)

    def commit_snapshot(current: Dict[str, Any]) -> None:
        _assert_snapshot_preserves_current(current, candidate)
        current.clear()
        current.update(copy.deepcopy(candidate))

    mutate_db(commit_snapshot)


def parse_val(val_str: Any) -> float:
    if val_str is None or val_str in ("-", "None", ""):
        return 0.0
    s = str(val_str).replace("+", "").replace("%", "").replace("pct", "").replace(",", "").strip()
    if "亿" in s:
        try:
            return float(s.replace("亿", "")) * 10000.0
        except ValueError:
            return 0.0
    if "万" in s:
        try:
            return float(s.replace("万", ""))
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def collect_samples_from_report(filepath: str, db: Dict[str, Any]) -> int:
    """从单份筛选报告中提取合力主升、观察池突破与板块协同影子样本。"""
    rep = parse_screening_report(filepath)
    date_str = rep["date"]
    time_str = rep["time"]
    if not re.fullmatch(r"\d{8}", str(date_str)):
        raise ValueError(f"报告文件名缺少有效交易日期: {filepath}")
    tables = rep.get("tables")
    if not isinstance(tables, dict):
        raise ValueError(f"报告表格结构异常: {filepath}")
    for table_name in ("low_absorb_short", "tomorrow_watchlist", "capital_ranking"):
        rows = tables.get(table_name) or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"报告表格 {table_name} 结构异常: {filepath}")

    samples = db.setdefault("samples", {})
    if not isinstance(samples, dict):
        raise ShadowDatabaseError("影子样本库 samples 必须是对象")
    for category in ("coalition", "breakout", "sector_boost"):
        rows = samples.setdefault(category, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ShadowDatabaseError(f"影子样本库 samples.{category} 必须是对象数组")
    existing_keys = {
        cat: {
            f"{str(sample.get('code')).strip()}_{str(sample.get('date')).strip()}"
            for sample in samples[cat]
            if sample.get("code") and sample.get("date")
        }
        for cat in ("coalition", "breakout", "sector_boost")
    }

    added_count = 0

    # 1. 采集 合力主升 (coalition) 样本（严格互斥：20% <= 超单占比 < 50%）
    low_short = tables.get("low_absorb_short") or []
    for r in low_short:
        code = str(r.get("代码") or "").strip()
        if not code:
            continue
        name = r.get("名称", "")
        super_lead = r.get("超单主导", "")
        super_str = r.get("超大单", "0")
        super_wan = parse_val(super_str)
        main_pct = parse_val(r.get("主力净占比", "0"))
        amt_wan = parse_val(r.get("成交额", "0"))
        main_net_wan = parse_val(r.get("主力净额", "0"))
        if main_net_wan == 0.0 and main_pct > 0 and amt_wan > 0:
            main_net_wan = amt_wan * (main_pct / 100.0)
        inc5_wan = parse_val(r.get("5分钟增量", "0"))
        price = parse_val(r.get("现价", "0"))

        super_ratio = (super_wan / main_net_wan * 100) if main_net_wan > 0 else 0.0
        # 影子样本只接受生产报告明确写出的严格合力标签。数值字段仅
        # 用于记录样本，不得在采集端重新推导标签并绕过主买比、历史快照
        # 与大单条件。
        is_pure_coalition = super_lead == "✓(合力)"

        sample_key = f"{code}_{date_str}"
        if is_pure_coalition and sample_key not in existing_keys["coalition"]:
            sample = {
                "id": f"COAL_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "plate": r.get("板块", "-"),
                "super_wan": round(super_wan, 1),
                "main_net_wan": round(main_net_wan, 1),
                "super_ratio": round(super_ratio, 1),
                "inc5_wan": round(inc5_wan, 1),
                "t1_result": None,
            }
            db["samples"]["coalition"].append(sample)
            existing_keys["coalition"].add(sample_key)
            added_count += 1

    # 2. 采集 观察池突破状态机 (breakout) 样本
    watchlist = tables.get("tomorrow_watchlist") or []
    for r in watchlist:
        code = str(r.get("代码") or "").strip()
        if not code:
            continue
        name = r.get("名称", "")
        state = r.get("突破状态", "")
        price = parse_val(r.get("当前价", r.get("现价", "0")))
        trigger = parse_val(r.get("触发价", "0"))
        dom = r.get("超单主导", "")

        is_breakout = (
            state in ("CONFIRMED", "B_BREAKOUT", "A_STRICT") or
            ("已站稳" in r.get("状态说明", "") and price >= trigger and trigger > 0)
        )
        sample_key = f"{code}_{date_str}"
        if is_breakout and sample_key not in existing_keys["breakout"]:
            sample = {
                "id": f"BRK_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "benchmark_trigger": trigger,
                "plate": r.get("板块", "-"),
                "state": state,
                "confirm_count": r.get("确认次数", "2次"),
                "dominance": dom,
                "t1_result": None,
            }
            db["samples"]["breakout"].append(sample)
            existing_keys["breakout"].add(sample_key)
            added_count += 1

    # 3. 采集 主线板块协同 (sector_boost) 样本
    cap_rank = tables.get("capital_ranking") or []
    for r in cap_rank:
        code = str(r.get("代码") or "").strip()
        if not code:
            continue
        name = r.get("名称", "")
        reason = (
            r.get("评分依据") or r.get("理由") or r.get("资金理由") or
            r.get("capital_reason") or r.get("capital_reason", "") or ""
        )
        price = parse_val(r.get("现价", "0"))
        boost_val = parse_val(r.get("sector_boost", 0))

        is_boosted = "主线板块协同" in reason or "20亿锚点" in reason or "锚点带动" in reason or boost_val > 0
        sample_key = f"{code}_{date_str}"
        if is_boosted and sample_key not in existing_keys["sector_boost"]:
            sample = {
                "id": f"BOOST_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "plate": r.get("板块", "-"),
                "score": parse_val(r.get("评分", "0")),
                "boost_points": 15,
                "t1_result": None,
            }
            db["samples"]["sector_boost"].append(sample)
            existing_keys["sector_boost"].add(sample_key)
            added_count += 1

    return added_count


_TRADING_CALENDAR = None


def _verified_next_trading_date(date_str: str) -> Optional[str]:
    """Return the official next exchange date, or None when it cannot be verified."""
    global _TRADING_CALENDAR
    try:
        normalized = datetime.strptime(str(date_str), "%Y%m%d").date()
        if _TRADING_CALENDAR is None:
            from tools.data_sources.cache import JsonCache
            from tools.data_sources.calendar import TradingCalendarService

            calendar_cache = JsonCache(
                "shadow_tracker_calendar",
                path=SHADOW_DATA_DIR / "trading_calendar_cache.json",
            )
            _TRADING_CALENDAR = TradingCalendarService(cache=calendar_cache, request_timeout=4.0)
        result = _TRADING_CALENDAR.next_trading_day(normalized, max_days=370)
        if result.status == "ok" and isinstance(result.data, dict):
            target = datetime.strptime(result.data.get("date", ""), "%Y-%m-%d").date()
            return target.strftime("%Y%m%d")
    except Exception:
        return None
    return None


def _all_report_files(reports_dir: str) -> List[str]:
    """Enumerate report snapshots under flat, date-folder, and nested archives."""
    found: List[Tuple[str, str, str]] = []
    if not os.path.isdir(reports_dir):
        return []
    for root, _dirs, files in os.walk(reports_dir):
        for filename in files:
            if not filename.endswith(".md"):
                continue
            if not filename.startswith("A股筛选结果_"):
                continue
            match = re.search(r"(\d{8})_(\d{4})", filename)
            if match:
                found.append((match.group(1), match.group(2), os.path.join(root, filename)))
    return [path for _day, _time, path in sorted(found)]


def find_next_trading_day_reports(reports_dir: str, date_str: str) -> List[str]:
    """Return only reports from a calendar-verified T+1 date; never infer across gaps."""
    next_date = _verified_next_trading_date(date_str)
    if not next_date:
        return []
    return [
        path for path in _all_report_files(reports_dir)
        if (match := re.search(r"(\d{8})_(\d{4})", os.path.basename(path)))
        and match.group(1) == next_date
    ]


def pending_t1_label(reports_dir: str, date_str: str) -> str:
    """生成待结算提示；有下一交易日报告时带真实日期，否则不猜日期。"""
    try:
        next_reports = find_next_trading_day_reports(reports_dir, date_str)
        if next_reports:
            next_date = parse_screening_report(next_reports[0]).get("date")
            if next_date:
                return f"待下一个交易日({next_date})"
    except Exception:
        pass
    return "待下一个交易日"


def fetch_t1_day_kline_extremes(code: str, t1_date: str) -> Optional[Tuple[float, float]]:
    """尝试从日K数据获取该股票在次日交易日的真实全日最高价与最低价。"""
    try:
        a_share_daily_screen = importlib.import_module("a_share_daily_screen")
        k_rows, _source = a_share_daily_screen.fetch_kline(code)
        target_fmt = f"{t1_date[:4]}-{t1_date[4:6]}-{t1_date[6:]}" if len(t1_date) == 8 else t1_date
        for kr in (k_rows or []):
            row_date = str(kr.get("date") or kr.get("day") or "")[:10]
            if row_date.replace("/", "-") == target_fmt.replace("/", "-"):
                h = float(kr.get("high", 0))
                l = float(kr.get("low", 0))
                if h > 0 and l > 0 and h >= l:
                    return h, l
    except Exception:
        pass
    return None


def calculate_t1_for_sample(sample: Dict[str, Any], t1_reports: List[str]) -> Optional[Dict[str, Any]]:
    """根据 T+1 次日报告与日K全日极值真实核算 09:45 收益、最大浮盈、最大回撤与假突破。"""
    if not t1_reports:
        return None

    code = sample["code"]
    trigger_price = float(sample["trigger_price"])
    if trigger_price <= 0:
        return None

    expected_t1_date = _verified_next_trading_date(str(sample.get("date") or ""))
    if not expected_t1_date:
        return None
    target_time = normalize_hhmm(RULE_CONFIG["execution"]["t1_exit_window"]["target"])
    target_text = f"{target_time[:2]}:{target_time[2:]}"
    p_0945: Optional[float] = None
    t1_date = expected_t1_date

    for r_file in t1_reports:
        try:
            rep = parse_screening_report(r_file)
            if rep.get("date") != expected_t1_date:
                continue
            t_str = rep.get("time", "")
            try:
                normalized_time = normalize_hhmm(t_str)
            except (TypeError, ValueError):
                normalized_time = ""

            for rows in rep.get("tables", {}).values():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and row.get("代码") == code:
                        if normalized_time != target_time or p_0945 is not None:
                            continue
                        price = parse_val(row.get("现价") or row.get("当前价") or row.get("价格"))
                        if price > 0:
                            # 只接受共享规则定义的精确时刻，不以 09:44/09:46/收盘价代替。
                            p_0945 = price
        except Exception:
            continue

    # 结合日K获取全日真实极值（防止中途退出候选表导致漏统计日内极值）。
    # 缺少精确 09:45 快照时，目标价格/收益必须为空，不能回退到第一份快照。
    day_extremes = fetch_t1_day_kline_extremes(code, t1_date) if t1_date else None
    if day_extremes is not None:
        extremes_complete = True
        p_high, p_low = day_extremes
    else:
        extremes_complete = False

    t1_ret = (p_0945 - trigger_price) / trigger_price * 100 if p_0945 is not None else None
    if extremes_complete:
        max_gain = round((p_high - trigger_price) / trigger_price * 100, 2)
        max_dd = round((p_low - trigger_price) / trigger_price * 100, 2)
        stop_pct = float(RULE_CONFIG["shadow"]["false_breakout_stop_pct"])
        is_false_breakout: Any = p_low < trigger_price * (1.0 - stop_pct / 100.0)
        extremes_source = T1_SOURCE_DAILY_KLINE
    else:
        max_gain = T1_PENDING
        max_dd = T1_PENDING
        is_false_breakout = T1_PENDING
        extremes_source = T1_SOURCE_REPORT_SNAPSHOTS_ONLY

    return {
        # checked 表示完整T+1结算，不是“找到了某个报告快照”。
        "checked": extremes_complete and p_0945 is not None,
        "t1_date": t1_date,
        "t1_date_verified": True,
        "target_time": target_text,
        "target_snapshot_found": p_0945 is not None,
        "t1_0945_price": round(p_0945, 2) if p_0945 is not None else None,
        "t1_0945_return_pct": round(t1_ret, 2) if t1_ret is not None else None,
        "t1_max_gain_pct": max_gain,
        "t1_max_drawdown_pct": max_dd,
        "is_false_breakout": is_false_breakout,
        "extremes_complete": extremes_complete,
        "source": extremes_source,
    }


def update_all_t1_metrics(db: Dict[str, Any], reports_dir: Optional[str] = None) -> None:
    """自动核算所有样本的 T+1 次日真实表现（含 divergence 等全部机制类别）。"""
    reports_dir = reports_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))
    validated = _validate_db(db)
    db.clear()
    db.update(validated)
    for category in sorted(db["samples"].keys()):
        for sample in db["samples"][category]:
            date_str = sample["date"]
            old_result = sample.get("t1_result")
            legacy_evidence = None
            try:
                old_target_time_ok = (
                    normalize_hhmm(old_result.get("target_time"))
                    == normalize_hhmm(RULE_CONFIG["execution"]["t1_exit_window"]["target"])
                ) if isinstance(old_result, dict) else False
            except (TypeError, ValueError):
                old_target_time_ok = False
            if (
                isinstance(old_result, dict)
                and old_result.get("checked") is True
                and old_result.get("extremes_complete") is True
                and old_result.get("source") == T1_SOURCE_DAILY_KLINE
                and (
                    not old_target_time_ok
                    or old_result.get("t1_date_verified") is not True
                    or old_result.get("target_snapshot_found") is not True
                )
                and not old_result.get("legacy_evidence")
            ):
                legacy_evidence = {key: copy.deepcopy(value) for key, value in old_result.items() if key != "legacy_evidence"}
                old_result["legacy_evidence"] = legacy_evidence
                old_result["checked"] = False
                old_result["extremes_complete"] = False
                old_result["source"] = T1_SOURCE_LEGACY_UNVERIFIED
                old_result["review_required"] = True
                old_result["completeness_note"] = "旧结算未记录精确目标时点，需重新核验"

            t1_reports = find_next_trading_day_reports(reports_dir, date_str)
            if t1_reports:
                res = calculate_t1_for_sample(sample, t1_reports)
                if res:
                    if is_complete_shadow_result(old_result) and not is_complete_shadow_result(res):
                        # A previously verified exact-time settlement survives later source outages.
                        sample["t1_result"] = old_result
                    else:
                        if legacy_evidence is not None:
                            res["legacy_evidence"] = legacy_evidence
                            if not is_complete_shadow_result(res):
                                res["review_required"] = True
                        elif isinstance(old_result, dict) and old_result.get("legacy_evidence"):
                            res["legacy_evidence"] = copy.deepcopy(old_result["legacy_evidence"])
                            if not is_complete_shadow_result(res):
                                res["review_required"] = True
                        sample["t1_result"] = res
            if sample.get("t1_result") is None:
                sample["t1_result"] = {
                    "checked": False,
                    "t1_date": pending_t1_label(reports_dir, date_str),
                    "t1_0945_price": None,
                    "t1_0945_return_pct": None,
                    "t1_max_gain_pct": T1_PENDING,
                    "t1_max_drawdown_pct": T1_PENDING,
                    "is_false_breakout": T1_PENDING,
                    "extremes_complete": False,
                    "source": T1_SOURCE_UNAVAILABLE,
                }
                if legacy_evidence is not None:
                    sample["t1_result"]["legacy_evidence"] = legacy_evidence
                    sample["t1_result"]["review_required"] = True
            elif (
                isinstance(sample.get("t1_result"), dict)
                and not sample["t1_result"].get("checked")
                and sample["t1_result"].get("source") == T1_SOURCE_UNAVAILABLE
            ):
                # 迁移旧版本写入的固定日期提示，避免历史样本继续显示错误日期。
                sample["t1_result"]["t1_date"] = pending_t1_label(reports_dir, date_str)


def generate_report(db: Dict[str, Any]) -> str:
    """生成 20 样本影子验证进度与指标汇总报告。"""
    lines = []
    lines.append("# 📊 20 样本量化影子验证系统进度报表 (权威定版)")
    lines.append(f"**更新时点**：`{db.get('last_updated', '-')}` ｜ **风控状态**：`待验证 · 仅模拟仓权限`\n")

    lines.append("## 一、四大待验证项目进度汇总")
    lines.append("| 待验证项目 | 目标样本 | 已收集样本 | 收集完成度 | 胜率 (09:45>0) | 平均 09:45 收益 | 平均最大浮盈 | 平均最大回撤 | 假突破率 | 当前状态 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    for cat, meta in db["targets"].items():
        samples = db["samples"].get(cat, [])
        count = len(samples)
        target = meta["target_samples"]
        pct = count / target * 100

        # 计算指标
        evaluated_samples = [s for s in samples if is_complete_shadow_result(s.get("t1_result"))]
        pending_extremes = any(
            s.get("t1_result")
            and not is_complete_shadow_result(s["t1_result"])
            and s["t1_result"].get("extremes_complete") is not True
            and s["t1_result"].get("t1_0945_price") is not None
            for s in samples
        )
        pending_target = any(
            s.get("t1_result")
            and not is_complete_shadow_result(s["t1_result"])
            and s["t1_result"].get("target_snapshot_found") is not True
            for s in samples
        )
        if evaluated_samples:
            wins = sum(1 for s in evaluated_samples if (s["t1_result"].get("t1_0945_return_pct") or 0) > 0)
            win_rate = f"{wins / len(evaluated_samples) * 100:.1f}%"
            avg_ret = f"{sum(s['t1_result']['t1_0945_return_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            avg_gain = f"{sum(s['t1_result']['t1_max_gain_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            avg_dd = f"{sum(s['t1_result']['t1_max_drawdown_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            false_bo = f"{sum(1 for s in evaluated_samples if s['t1_result'].get('is_false_breakout')) / len(evaluated_samples) * 100:.1f}%"
        else:
            win_rate = (
                "0.0% (待补精确09:45快照)" if pending_target
                else ("0.0% (待补算日K极值)" if pending_extremes else "0.0% (待下一个交易日结算)")
            )
            avg_ret = "0.00%"
            avg_gain = "0.00%"
            avg_dd = "0.00%"
            false_bo = "0.0%"

        status = (
            "🟢 验证达标"
            if count >= target and len(evaluated_samples) >= target
            else "🟡 影子数据采集中"
        )
        lines.append(f"| **{meta['name']}** | {target} | **{count}** | {pct:.1f}% | {win_rate} | {avg_ret} | {avg_gain} | {avg_dd} | {false_bo} | {status} |")

    lines.append("\n## 二、当前已入库影子样本明细")
    for cat, meta in db["targets"].items():
        samples = db["samples"].get(cat, [])
        lines.append(f"\n### 📌 {meta['name']} 样本池 ({len(samples)}/{meta['target_samples']})")
        if samples:
            lines.append("| 样本ID | 代码 | 名称 | 入库日期 | 触发时点 | 触发价 | 板块 | 关键量化特征 | T+1 09:45 收益 | 结算状态 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for s in samples:
                feat = (
                    f"超单:{s.get('super_wan',0):.0f}万/主力:{s.get('main_net_wan',0):.0f}万(占比{s.get('super_ratio',0):.1f}%)"
                    if cat == "coalition"
                    else (f"状态:{s.get('state')} 触发基准:{s.get('benchmark_trigger')}" if cat == "breakout"
                          else (f"场景:{s.get('scenario','-')} 主力:{s.get('mainp_pct',0)}% 超单:{s.get('xl_wan',0):+.0f}万 回落:{s.get('pullback_pct',0)}%"
                                if cat == "divergence"
                                else f"协同评分:{s.get('score', '-')} (+{s.get('boost_points', 15)}分)"))
                )
                t1_res = s.get("t1_result") or {}
                t1_txt = t1_res.get("t1_0945_return_pct")
                t1_disp = f"{t1_txt:+.2f}%" if t1_txt is not None else "-"
                if is_complete_shadow_result(t1_res):
                    status_disp = f"✅ 已核算({t1_res.get('t1_date')})"
                elif t1_res.get("review_required"):
                    status_disp = "⚠️ 旧结算缺少目标时点，待复核"
                elif t1_res.get("target_snapshot_found") is False:
                    status_disp = f"⏳ 缺少精确{t1_res.get('target_time') or '09:45'}目标快照"
                elif t1_res.get("extremes_complete") is False and t1_res.get("t1_0945_price") is not None:
                    status_disp = f"⏳ 待补算日K极值({t1_res.get('source', T1_SOURCE_REPORT_SNAPSHOTS_ONLY)})"
                else:
                    status_disp = "⏳ 待下一个交易日结算"
                lines.append(
                    f"| `{s.get('id', '-')}` | {s.get('code', '-')} | {s.get('name', '-')} | {s.get('date', '-')} | "
                    f"{s.get('trigger_time', '-')} | {parse_val(s.get('trigger_price')):.2f} | {s.get('plate', '-')} | "
                    f"{feat} | {t1_disp} | {status_disp} |"
                )
        else:
            lines.append("*（暂无样本）*")

    return "\n".join(lines)


def scan_and_update(date_str: Optional[str] = None, reports_dir: Optional[str] = None) -> None:
    """Accumulate samples from all history (or one requested date) and settle them safely."""
    if date_str is not None and not re.fullmatch(r"\d{8}", str(date_str)):
        raise ValueError("--date 必须为 YYYYMMDD")
    reports_dir = reports_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))

    def merge_scan(db: Dict[str, Any]) -> int:
        total_added = 0
        files = _all_report_files(reports_dir)
        if date_str is not None:
            files = [
                path for path in files
                if (match := re.search(r"(\d{8})_(\d{4})", os.path.basename(path)))
                and match.group(1) == date_str
            ]
        for report_path in files:
            total_added += collect_samples_from_report(report_path, db)
        update_all_t1_metrics(db, reports_dir=reports_dir)
        return total_added

    # The shared transaction owns the lock before loading the latest snapshot.
    total_added, db = mutate_db(merge_scan)

    print(f"=== 影子系统扫描完成：新增 {total_added} 个样本，当前总样本库状态已更新 ===")
    print(generate_report(db))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="20样本量化影子验证系统")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD")
    parser.add_argument("--report", action="store_true", help="输出当前影子验证报表")
    args = parser.parse_args()

    if args.report:
        db = init_db()
        print(generate_report(db))
    else:
        scan_and_update(args.date)
