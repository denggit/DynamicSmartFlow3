#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : trading_history.py
@Description: 交易历史记录，仅用于日报生成。
              每次买卖时追加写入 trading_history.json，
              每月汇总到 summary_reportYYYYMM.json，避免长期积累大量记录占用内存。
              日报时读取：当月 trading_history + 历史月度 summary，不常驻内存。
"""
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from config.settings import TRADING_HISTORY_PATH, DATA_DIR, SUMMARY_FILE_PREFIX
from utils.logger import get_logger

logger = get_logger(__name__)

SUMMARY_DIR = DATA_DIR
SUMMARY_PREFIX = SUMMARY_FILE_PREFIX
_LOCK = threading.Lock()


def _summary_path(year: int, month: int) -> Path:
    """月度汇总文件路径，如 summary_report202602.json"""
    return SUMMARY_DIR / f"{SUMMARY_PREFIX}{year}{month:02d}.json"


def _records_for_month(history: List[Dict], year: int, month: int) -> List[Dict]:
    """筛选指定年月的记录（按 date 字段 YYYY-MM 解析）。"""
    prefix = f"{year}-{month:02d}-"
    return [r for r in history if (r.get("date") or "").startswith(prefix)]


def _build_month_summary(records: List[Dict], year: int, month: int) -> Dict[str, Any]:
    """从记录构建月度汇总。"""
    sells = [r for r in records if r.get("type") == "sell" and r.get("pnl_sol") is not None]
    total_pnl = sum(r.get("pnl_sol", 0) for r in sells)
    hunter_pnl: Dict[str, float] = {}
    for r in sells:
        addr = r.get("hunter_addr") or ""
        if addr:
            hunter_pnl[addr] = hunter_pnl.get(addr, 0) + (r.get("pnl_sol") or 0)
    win_count = sum(1 for r in sells if (r.get("pnl_sol") or 0) > 0)
    loss_count = sum(1 for r in sells if (r.get("pnl_sol") or 0) < 0)
    wins = sum(r.get("pnl_sol", 0) for r in sells if (r.get("pnl_sol") or 0) > 0)
    losses = sum(-(r.get("pnl_sol", 0)) for r in sells if (r.get("pnl_sol") or 0) < 0)
    profit_factor = wins / losses if losses > 0 else (float("inf") if wins > 0 else 0)
    return {
        "year": year,
        "month": month,
        "total_pnl": total_pnl,
        "total_trades": len(records),
        "hunter_pnl": hunter_pnl,
        "win_count": win_count,
        "loss_count": loss_count,
        "profit_factor": profit_factor,
    }


def ensure_monthly_summaries_and_trim() -> None:
    """
    将非当月记录汇总为月度文件，并从 trading_history 中移除，减少内存占用。
    仅在日报时调用，在独立线程/异步中执行，不阻塞主流程。
    """
    now = datetime.now()
    curr_year, curr_month = now.year, now.month
    try:
        with _LOCK:
            if not TRADING_HISTORY_PATH.exists():
                return
            with open(TRADING_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, list) or not history:
                return
            # 找出所有非当月的 (year, month)
            months_to_summarize: List[Tuple[int, int]] = []
            for r in history:
                d = r.get("date") or ""
                if len(d) >= 7 and d[4] == "-" and d[7] == "-":
                    try:
                        y, m = int(d[:4]), int(d[5:7])
                        if (y, m) != (curr_year, curr_month) and (y, m) not in months_to_summarize:
                            months_to_summarize.append((y, m))
                    except ValueError:
                        pass
            if not months_to_summarize:
                return
            months_to_summarize.sort()
            to_remove: List[Dict] = []
            for ym in months_to_summarize:
                y, m = ym
                path = _summary_path(y, m)
                recs = _records_for_month(history, y, m)
                if not recs:
                    continue
                if not path.exists():
                    summary = _build_month_summary(recs, y, m)
                    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(summary, f, ensure_ascii=False, indent=2)
                    logger.info("📊 已生成月度汇总 %04d-%02d (%d 条记录)", y, m, len(recs))
                to_remove.extend(recs)  # 无论新建还是已有汇总，都从 history 移除该月记录
            # 从 history 中移除已汇总月份记录，只保留当月及异常记录（列表推导清晰且避免重复 remove 的边界问题）
            history = [r for r in history if r not in to_remove]
            with open(TRADING_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("❌ 月度汇总与裁剪失败")


def load_all_summaries() -> List[Dict[str, Any]]:
    """加载所有 summary_reportYYYYMM.json，按年月排序。"""
    out: List[Dict[str, Any]] = []
    if not SUMMARY_DIR.exists():
        return out
    for f in SUMMARY_DIR.glob(f"{SUMMARY_PREFIX}*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict) and "year" in data and "month" in data:
                out.append(data)
        except Exception:
            logger.warning("加载汇总失败: %s", f.name)
    out.sort(key=lambda x: (x.get("year", 0), x.get("month", 0)))
    return out


def load_data_for_report() -> Tuple[List[Dict], List[Dict]]:
    """
    供日报使用：先执行月度汇总与裁剪，再加载当月记录 + 所有月度汇总。
    返回 (current_month_records, summaries)。
    仅加载少量数据，不影响主程序内存。
    """
    ensure_monthly_summaries_and_trim()
    history = load_history()
    summaries = load_all_summaries()
    return history, summaries


def append_trade(record: Dict[str, Any]) -> None:
    """
    同步追加一条交易记录到 trading_history.json（内部用，不阻塞主流程请用 append_trade_in_background）。
    """
    try:
        TRADING_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            history: List[Dict] = []
            if TRADING_HISTORY_PATH.exists():
                try:
                    with open(TRADING_HISTORY_PATH, "r", encoding="utf-8") as f:
                        history = json.load(f)
                except Exception:
                    history = []
            if not isinstance(history, list):
                history = []
            history.append(record)
            with open(TRADING_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("❌ 追加交易记录失败")


def append_trade_in_background(record: Dict[str, Any]) -> None:
    """
    在后台线程追加交易记录，不阻塞主流程/跟单。
    主程序应使用此接口，避免写入文件影响交易。
    """
    rec = dict(record)  # 深拷贝关键字段，避免调用方后续修改影响

    def _run():
        try:
            append_trade(rec)
        except Exception:
            logger.exception("❌ 后台追加交易记录失败")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def load_history() -> List[Dict[str, Any]]:
    """加载完整交易历史，日报时调用。"""
    try:
        if not TRADING_HISTORY_PATH.exists():
            return []
        with open(TRADING_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        logger.exception("❌ 加载交易历史失败")
        return []
