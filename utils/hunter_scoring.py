#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : hunter_scoring.py
@Description: 猎手评分统一模块
              挖掘阶段（sm_searcher）与体检阶段（hunter_monitor）共用同一套评分逻辑，
              保证逻辑一致性，单一数据源。

              评分体系：胜率分 30% + 盈利分 40% + 盈亏比分 30%
              - 胜率分：< 15% 零分，15%~35% 线性 0.5~1，≥35% 满分
              - 盈利分：avg_roi_pct < 20% 零分，20%~100% 线性，≥100% 满分
              - 盈亏比分：总盈亏比 = 盈利和/亏损和；< 1.5 零分，1.5~3 线性，> 3 满分
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
    根据猎手表现统计计算综合评分。

    评分体系：胜率分 30% + 盈利分 40% + 盈亏比分 30%

    :param stats: 猎手表现统计，需包含：
        - win_rate: float, 0~1，胜率
        - total_profit: float, 总利润 (SOL)
        - avg_roi_pct: float, 代币维度平均盈利率 (%)
        - pnl_ratio: float, 总盈亏比 = 盈利单利润和/亏损单亏损和
    :return: 评分结果 dict，包含：
        - score: float, 最终分数 (0~100)
        - score_hit_rate: float, 胜率分分量
        - score_profit: float, 盈利分分量
        - score_pnl_ratio: float, 盈亏比分分量
        - scores_detail: str, 格式化详情 "H:x.xx/P:x.xx/R:x.xx"
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

    # 胜率分：< 15% 零分，15%~35% 线性 0.5~1，≥35% 满分
    wr = stats["win_rate"] * 100
    if wr < SM_WIN_RATE_ZERO_PCT:
        score_hit_rate = 0.0
    elif wr >= SM_WIN_RATE_FULL_PCT:
        score_hit_rate = 1.0
    else:
        # 15% ~ 35%: 0.5 + 0.5 × (wr - 15) / 20
        score_hit_rate = 0.5 + 0.5 * (wr - SM_WIN_RATE_ZERO_PCT) / (
            SM_WIN_RATE_FULL_PCT - SM_WIN_RATE_ZERO_PCT
        )

    # 盈利分：< 20% 零分，20%~100% 线性，≥100% 满分
    avg_roi_pct = stats.get("avg_roi_pct", 0.0)
    if avg_roi_pct < SM_PROFIT_SCORE_ZERO_PCT:
        score_profit = 0.0
    elif avg_roi_pct >= SM_PROFIT_SCORE_FULL_PCT:
        score_profit = 1.0
    else:
        score_profit = (avg_roi_pct - SM_PROFIT_SCORE_ZERO_PCT) / (
            SM_PROFIT_SCORE_FULL_PCT - SM_PROFIT_SCORE_ZERO_PCT
        )

    # 盈亏比分：总盈亏比 = 盈利和/亏损和；< 1.5 零分，1.5~3 线性，> 3 满分
    pnl_ratio = stats.get("pnl_ratio", 0.0)
    if pnl_ratio < SM_PNL_RATIO_ZERO:
        score_pnl_ratio = 0.0
    elif pnl_ratio >= SM_PNL_RATIO_FULL or pnl_ratio == float("inf"):
        score_pnl_ratio = 1.0
    else:
        score_pnl_ratio = (pnl_ratio - SM_PNL_RATIO_ZERO) / (
            SM_PNL_RATIO_FULL - SM_PNL_RATIO_ZERO
        )

    # 最终分数
    final_score = (score_hit_rate * 30) + (score_profit * 40) + (score_pnl_ratio * 30)
    final_score = round(final_score, 1)

    return {
        "score": final_score,
        "score_hit_rate": score_hit_rate,
        "score_profit": score_profit,
        "score_pnl_ratio": score_pnl_ratio,
        "scores_detail": f"H:{score_hit_rate:.2f}/P:{score_profit:.2f}/R:{score_pnl_ratio:.2f}",
    }
