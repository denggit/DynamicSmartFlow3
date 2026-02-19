#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : hunter_scoring.py
@Description: 猎手评分统一模块
              挖掘阶段（sm_searcher）与体检阶段（hunter_monitor）共用同一套评分逻辑，
              保证逻辑一致性，单一数据源。
"""

from typing import Dict, Any

from config.settings import (
    SM_PROFIT_SCORE_REF_PCT,
    SM_WIN_RATE_FULL_PCT,
    SM_WIN_RATE_MID_PCT,
    SM_WIN_RATE_ZERO_PCT,
)


def compute_hunter_score(stats: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据猎手表现统计计算综合评分。

    评分体系：胜率分 30% + 盈利分 40% + 回撤分 30%
    - 胜率分：70%+ 满分，40% 得 0.6，10% 及以下 0；两段线性插值
    - 盈利分：代币维度平均盈利率 (%)，≥10% 满分，≤0% 零分，线性插值
    - 回撤分：1 - |worst_roi|/100，限制在 [0,1]

    :param stats: 猎手表现统计，需包含：
        - win_rate: float, 0~1，胜率
        - worst_roi: float, 最差单笔 ROI (%)
        - total_profit: float, 总利润 (SOL)
        - avg_roi_pct: float, 可选，代币维度平均盈利率 (%)
    :return: 评分结果 dict，包含：
        - score: float, 最终分数 (0~100)
        - score_hit_rate: float, 胜率分分量
        - score_profit: float, 盈利分分量
        - score_drawdown: float, 回撤分分量
        - scores_detail: str, 格式化详情 "H:x.xx/P:x.xx/D:x.xx"
    """
    total_profit = stats.get("total_profit", 0)

    if total_profit < 0:
        return {
            "score": 0.0,
            "score_hit_rate": 0.0,
            "score_profit": 0.0,
            "score_drawdown": 0.0,
            "scores_detail": "H:0/P:0/D:0",
        }

    # 胜率分：70%+ 满分，40% 得 0.6，10% 及以下 0；两段线性
    wr = stats["win_rate"] * 100
    if wr <= SM_WIN_RATE_ZERO_PCT:
        score_hit_rate = 0.0
    elif wr >= SM_WIN_RATE_FULL_PCT:
        score_hit_rate = 1.0
    elif wr <= SM_WIN_RATE_MID_PCT:
        score_hit_rate = 0.6 * (wr - SM_WIN_RATE_ZERO_PCT) / (
            SM_WIN_RATE_MID_PCT - SM_WIN_RATE_ZERO_PCT
        )
    else:
        score_hit_rate = 0.6 + 0.4 * (wr - SM_WIN_RATE_MID_PCT) / (
            SM_WIN_RATE_FULL_PCT - SM_WIN_RATE_MID_PCT
        )

    # 盈利分：代币维度平均盈利率，≥10% 满分，≤0% 零分
    avg_roi_pct = stats.get("avg_roi_pct", 0.0)
    if avg_roi_pct <= 0:
        score_profit = 0.0
    else:
        score_profit = min(1.0, avg_roi_pct / SM_PROFIT_SCORE_REF_PCT)

    # 回撤分：1 - |worst_roi|/100，限制在 [0,1]
    score_drawdown = 1 - abs(stats["worst_roi"] / 100)
    score_drawdown = max(0, min(1, score_drawdown))

    # 最终分数
    final_score = (score_hit_rate * 30) + (score_profit * 40) + (score_drawdown * 30)
    final_score = round(final_score, 1)

    return {
        "score": final_score,
        "score_hit_rate": score_hit_rate,
        "score_profit": score_profit,
        "score_drawdown": score_drawdown,
        "scores_detail": f"H:{score_hit_rate:.2f}/P:{score_profit:.2f}/D:{score_drawdown:.2f}",
    }
