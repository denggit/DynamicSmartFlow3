#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 猎手评分模块
              MODELA 与 MODELB 使用不同的评分标准，分别由 modela / modelb 提供。
"""
from utils.scoring.modela import compute_hunter_score as compute_hunter_score_modela
from utils.scoring.modelb import compute_hunter_score as compute_hunter_score_modelb

__all__ = ["compute_hunter_score_modela", "compute_hunter_score_modelb"]
