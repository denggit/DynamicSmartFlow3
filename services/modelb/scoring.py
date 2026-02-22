#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: MODELB 三维度评分（与 MODELA 完全分离）
              盈利力 45% + 持久力 35% + 真实性 20%
"""

from typing import Dict, Any


def compute_hunter_score(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    MODELB 三维度评分。
    :param stats: analyze_wallet_modelb 返回的完整统计
    :return: score, scores_detail, profit_dim, persist_dim, auth_dim 等
    """
    pnl_ratio = stats.get("pnl_ratio", 0) or 0
    if pnl_ratio == float("inf"):
        pnl_ratio = 10.0
    score_pnl = min(25.0, pnl_ratio * 12.5) if pnl_ratio >= 0 else 0
    avg_roi = stats.get("avg_roi_pct", 0) or 0
    score_avg_roi = min(10.0, max(0, avg_roi / 5))
    max_roi = stats.get("max_roi_pct", 0) or 0
    score_max_roi = min(10.0, max(0, max_roi / 10))
    profit_dim = score_pnl + score_avg_roi + score_max_roi
    max_loss = stats.get("max_single_loss_pct", 0) or 0
    if max_loss > 99.0:
        profit_dim = max(0, profit_dim - 10)
    profit_dim = round(profit_dim, 1)

    wr = (stats.get("win_rate", 0) or 0) * 100
    if wr < 40:
        score_wr = 10.0 * (wr / 40) if wr >= 0 else 0
    elif wr >= 80:
        score_wr = 30.0
    else:
        score_wr = 10.0 + 20.0 * (wr - 40) / 40
    txs_per_day = stats.get("txs_per_day", 0) or 0
    score_activity = 5.0 if txs_per_day >= 1 else 0.0
    persist_dim = score_wr + score_activity
    dust_ratio = stats.get("dust_ratio", 0) or 0
    if dust_ratio >= 0.5:
        dust_deduction = 20.0
    elif dust_ratio >= 0.1:
        dust_deduction = 5.0 + 15.0 * (dust_ratio - 0.1) / 0.4
    else:
        dust_deduction = 0.0
    persist_dim = max(0, persist_dim - dust_deduction)
    persist_dim = round(persist_dim, 1)

    avg_hold = stats.get("avg_hold_sec")
    score_hold = 5.0 if (avg_hold is not None and avg_hold <= 86400) else 0.0
    prof_hold = stats.get("profitable_avg_hold_sec")
    loss_hold = stats.get("loss_avg_hold_sec")
    if prof_hold is not None and loss_hold is not None and loss_hold > 0:
        if prof_hold > 2 * loss_hold:
            score_hold_ratio = 10.0
        elif prof_hold > loss_hold:
            score_hold_ratio = 5.0
        else:
            score_hold_ratio = 0.0
    else:
        score_hold_ratio = 0.0
    closed_ratio = stats.get("closed_ratio", 0) or 0
    if closed_ratio > 0.9:
        score_closed = 5.0
    elif closed_ratio > 0.7:
        score_closed = 3.0
    elif closed_ratio > 0.5:
        score_closed = 1.0
    else:
        score_closed = 0.0
    auth_dim = score_hold + score_hold_ratio + score_closed
    auth_dim = round(auth_dim, 1)
    final_score = round(profit_dim + persist_dim + auth_dim, 1)
    return {
        "score": final_score,
        "profit_dim": profit_dim,
        "persist_dim": persist_dim,
        "auth_dim": auth_dim,
        "scores_detail": f"盈:{profit_dim:.1f}/持:{persist_dim:.1f}/真:{auth_dim:.1f}",
        "profit_pnl": score_pnl,
        "profit_avg_roi": score_avg_roi,
        "profit_max_roi": score_max_roi,
        "persist_win_rate": score_wr,
        "persist_activity": score_activity,
        "persist_dust_deduction": dust_deduction,
        "auth_hold": score_hold,
        "auth_hold_ratio": score_hold_ratio,
        "auth_closed": score_closed,
    }
