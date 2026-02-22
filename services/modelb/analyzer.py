#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: MODELB 专用分析器
              基于 300 笔交易，按「项目=同一 token」维度统计，供三维度评分使用。
"""

import asyncio
import time
from collections import defaultdict
from typing import Dict, List, Optional, Any

from config.settings import IGNORE_MINTS, USDC_MINT, USDC_PER_SOL
from services.hunter_common import (
    TransactionParser,
    TokenAttributionCalculator,
    _get_tx_timestamp,
)
from utils.logger import get_logger

logger = get_logger(__name__)

CLOSED_REMAINING_RATIO = 0.05
DUST_VALUE_USD = 0.01
NEW_BUY_EXCLUDE_HOURS = 24


def _build_modelb_projects(
    txs: List[dict],
    hunter_address: str,
    usdc_price_sol: Optional[float] = None,
) -> Dict[str, dict]:
    """从交易列表构建 MODELB 项目（每个 token 一个项目）。"""
    parser = TransactionParser(hunter_address)
    calc = TokenAttributionCalculator()
    projects = defaultdict(lambda: {
        "buy_sol": 0.0, "sell_sol": 0.0, "tokens": 0.0,
        "total_bought_tokens": 0.0, "total_sold_tokens": 0.0,
        "first_buy_ts": None, "last_sell_ts": None,
    })
    txs_sorted = sorted(txs, key=lambda x: _get_tx_timestamp(x))
    for tx in txs_sorted:
        try:
            sol_change, token_changes, ts = parser.parse_transaction(tx, usdc_price_sol)
            if not token_changes:
                continue
            buy_attrs, sell_attrs = calc.calculate_attribution(sol_change, token_changes)
            for mint, delta in token_changes.items():
                if mint in IGNORE_MINTS:
                    continue
                if abs(delta) < 1e-12:
                    continue
                p = projects[mint]
                if delta > 0:
                    p["total_bought_tokens"] += delta
                    p["tokens"] += delta
                    if p["first_buy_ts"] is None:
                        p["first_buy_ts"] = ts
                else:
                    p["total_sold_tokens"] += abs(delta)
                    p["tokens"] += delta
                    p["last_sell_ts"] = ts
                if mint in buy_attrs:
                    p["buy_sol"] += buy_attrs[mint]
                if mint in sell_attrs:
                    p["sell_sol"] += sell_attrs[mint]
        except Exception:
            logger.debug("modelb_analyzer 解析交易跳过", exc_info=True)
    return dict(projects)


async def analyze_wallet_modelb(
    txs: List[dict],
    hunter_address: str,
    dex_scanner,
) -> Optional[Dict[str, Any]]:
    """
    MODELB 专用分析：三维度指标。
    :return: 完整 stats dict 供 modelb 评分使用，失败返回 None
    """
    if not txs:
        return None
    now = time.time()
    usdc_price_sol = await dex_scanner.get_token_price(USDC_MINT) if dex_scanner else None
    projects = _build_modelb_projects(txs, hunter_address, usdc_price_sol)
    min_buy_sol = 0.05
    valid = {}
    for mint, p in projects.items():
        if p["buy_sol"] < min_buy_sol:
            continue
        total_bought = p["total_bought_tokens"]
        if total_bought <= 0:
            continue
        tokens_held = p["tokens"]
        is_closed = tokens_held <= 0 or tokens_held < CLOSED_REMAINING_RATIO * total_bought
        p["is_closed"] = is_closed
        if is_closed:
            roi = (p["sell_sol"] - p["buy_sol"]) / p["buy_sol"] * 100 if p["buy_sol"] > 0 else 0
            p["roi_pct"] = roi
            p["net_profit"] = p["sell_sol"] - p["buy_sol"]
        else:
            price_sol = None
            if hasattr(dex_scanner, 'get_token_price'):
                price_sol = await dex_scanner.get_token_price(mint)
                await asyncio.sleep(0.15)
            if price_sol is not None and price_sol > 0:
                current_val_sol = tokens_held * price_sol
                realized = p["sell_sol"]
                net = realized + current_val_sol - p["buy_sol"]
                roi = (net / p["buy_sol"]) * 100 if p["buy_sol"] > 0 else 0
                p["roi_pct"] = roi
                p["net_profit"] = net
                p["current_value_sol"] = current_val_sol
            else:
                p["roi_pct"] = (p["sell_sol"] - p["buy_sol"]) / p["buy_sol"] * 100 if p["buy_sol"] > 0 else 0
                p["net_profit"] = p["sell_sol"] - p["buy_sol"]
                p["current_value_sol"] = 0
        valid[mint] = p
    if not valid:
        return None

    dust_count = 0
    for mint, p in valid.items():
        if p["is_closed"]:
            continue
        tokens_held = p["tokens"]
        if tokens_held < CLOSED_REMAINING_RATIO * p["total_bought_tokens"]:
            continue
        first_buy = p.get("first_buy_ts") or 0
        if (now - first_buy) < NEW_BUY_EXCLUDE_HOURS * 3600:
            continue
        price_usd = None
        if hasattr(dex_scanner, 'get_token_price_usd'):
            price_usd = await dex_scanner.get_token_price_usd(mint)
            await asyncio.sleep(0.15)
        if price_usd is not None and price_usd > 0:
            value_usd = p["tokens"] * price_usd
            if value_usd < DUST_VALUE_USD:
                dust_count += 1
        else:
            price_sol = None
            if hasattr(dex_scanner, 'get_token_price'):
                price_sol = await dex_scanner.get_token_price(mint)
                await asyncio.sleep(0.15)
            if price_sol is not None:
                value_usd = p["tokens"] * price_sol * USDC_PER_SOL
                if value_usd < DUST_VALUE_USD:
                    dust_count += 1
    total_projects = len(valid)
    dust_ratio = dust_count / total_projects if total_projects > 0 else 0

    total_profit = sum(p["net_profit"] for p in valid.values())
    total_wins = sum(p["net_profit"] for p in valid.values() if p["net_profit"] > 0)
    total_losses = sum(abs(p["net_profit"]) for p in valid.values() if p["net_profit"] < 0)
    pnl_ratio = total_wins / total_losses if total_losses > 0 else (float("inf") if total_wins > 0 else 0)
    rois = [p["roi_pct"] for p in valid.values()]
    avg_roi = sum(rois) / len(rois) if rois else 0
    max_roi = max(rois) if rois else 0
    min_roi = min(rois) if rois else 0
    max_single_loss_pct = -min_roi if min_roi < 0 else 0

    win_rate = len([p for p in valid.values() if p["net_profit"] > 0]) / total_projects if total_projects > 0 else 0
    txs_per_day = len(txs) / max(1, (now - min(_get_tx_timestamp(t) for t in txs)) / 86400)

    closed_projects = [p for p in valid.values() if p["is_closed"]]
    hold_times = []
    profitable_hold = []
    loss_hold = []
    for p in closed_projects:
        fb = p.get("first_buy_ts")
        ls = p.get("last_sell_ts")
        if fb and ls and ls > fb:
            sec = ls - fb
            hold_times.append(sec)
            if p["net_profit"] > 0:
                profitable_hold.append(sec)
            else:
                loss_hold.append(sec)
    avg_hold_sec = sum(hold_times) / len(hold_times) if hold_times else None
    profitable_avg_hold = sum(profitable_hold) / len(profitable_hold) if profitable_hold else None
    loss_avg_hold = sum(loss_hold) / len(loss_hold) if loss_hold else None
    closed_ratio = len(closed_projects) / total_projects if total_projects > 0 else 0

    return {
        "pnl_ratio": pnl_ratio,
        "total_profit": total_profit,
        "avg_roi_pct": avg_roi,
        "max_roi_pct": max_roi,
        "max_single_loss_pct": max_single_loss_pct,
        "win_rate": win_rate,
        "trade_frequency": total_projects,
        "txs_per_day": txs_per_day,
        "dust_ratio": dust_ratio,
        "dust_count": dust_count,
        "avg_hold_sec": avg_hold_sec,
        "profitable_avg_hold_sec": profitable_avg_hold,
        "loss_avg_hold_sec": loss_avg_hold,
        "closed_ratio": closed_ratio,
        "closed_count": len(closed_projects),
        "count": total_projects,
        "total_wins": total_wins,
        "total_losses": total_losses,
    }
