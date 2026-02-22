#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 数据路径统一管理
              data/ 下按 modelA、modelB 分离，各自形态数据放入对应子目录。
"""
from config.base import BASE_DIR

DATA_DIR = BASE_DIR / "data"
DATA_MODELA_DIR = DATA_DIR / "modelA"
DATA_MODELB_DIR = DATA_DIR / "modelB"

# MODELA 专用
HUNTER_JSON_PATH = str(DATA_MODELA_DIR / "hunters.json")
HUNTER_BACKUP_PATH = str(DATA_MODELA_DIR / "hunters_backup.json")

# MODELB 专用（wallets.txt、smart_money.json、trash_wallets.txt 均在 data/modelB/）

# 共用（交易记录、状态等）
TRADING_HISTORY_PATH = DATA_DIR / "trading_history.json"
SUMMARY_FILE_PREFIX = "summary_report"
CLOSED_PNL_PATH = DATA_DIR / "closed_pnl.json"
TRADER_STATE_PATH = DATA_DIR / "trader_state.json"
