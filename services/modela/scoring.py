#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: MODELA 猎手评分
              高收益 token 挖掘模式专用。挖掘阶段与体检阶段共用。
              评分体系：胜率分 30% + 盈利分 40% + 盈亏比分 30%
"""

from typing import Dict, Any

from config.settings import (
    SM_PROFIT_SCORE_ZERO_PCT,
    SM_PROFIT_SCORE_FULL_PCT,
    SM_WIN_RATE_ZERO_PCT,
    SM_WIN_RATE_FULL_PCT,
    SM_PNL_RATIO_ZERO,
    SM_PNL_RATIO_FULL,
)


def compute_hunter_score(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    MODELA 猎手评分：胜率 30% + 盈利 40% + 盈亏比 30%。
    :param stats: 猎手表现统计（win_rate, total_profit, avg_roi_pct, pnl_ratio）
    :return: score, scores_detail 等
    """
    total_profit = stats.get("total_profit", 0)
    if total_profit < 0:
        return {
            "score": 0.0,
            "score_hit_rate": 0.0,
            "score_profit": 0.0,
            "score_pnl_ratio": 0.0,
            "scores_detail": "H:0/P:0/R:0",
        }

    wr = stats.get("win_rate", 0) * 100
    if wr < SM_WIN_RATE_ZERO_PCT:
        score_hit_rate = 0.0
    elif wr >= SM_WIN_RATE_FULL_PCT:
        score_hit_rate = 1.0
    else:
        score_hit_rate = 0.5 + 0.5 * (wr - SM_WIN_RATE_ZERO_PCT) / (
            SM_WIN_RATE_FULL_PCT - SM_WIN_RATE_ZERO_PCT
        )

    avg_roi_pct = stats.get("avg_roi_pct", 0.0)
    if avg_roi_pct < SM_PROFIT_SCORE_ZERO_PCT:
        score_profit = 0.0
    elif avg_roi_pct >= SM_PROFIT_SCORE_FULL_PCT:
        score_profit = 1.0
    else:
        score_profit = (avg_roi_pct - SM_PROFIT_SCORE_ZERO_PCT) / (
            SM_PROFIT_SCORE_FULL_PCT - SM_PROFIT_SCORE_ZERO_PCT
        )

    pnl_ratio = stats.get("pnl_ratio", 0.0)
    if pnl_ratio < SM_PNL_RATIO_ZERO:
        score_pnl_ratio = 0.0
    elif pnl_ratio >= SM_PNL_RATIO_FULL or pnl_ratio == float("inf"):
        score_pnl_ratio = 1.0
    else:
        score_pnl_ratio = (pnl_ratio - SM_PNL_RATIO_ZERO) / (
            SM_PNL_RATIO_FULL - SM_PNL_RATIO_ZERO
        )

    final_score = (score_hit_rate * 30) + (score_profit * 40) + (score_pnl_ratio * 30)
    final_score = round(final_score, 1)
    return {
        "score": final_score,
        "score_hit_rate": score_hit_rate,
        "score_profit": score_profit,
        "score_pnl_ratio": score_pnl_ratio,
        "scores_detail": f"H:{score_hit_rate:.2f}/P:{score_profit:.2f}/R:{score_pnl_ratio:.2f}",
    }
