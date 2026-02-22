#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 数据路径统一管理
              data/ 下按 modelA、modelB 分离，各自形态数据放入对应子目录。
              交易记录、状态等按当前 HUNTER_MODE 放入对应目录。
"""
import os

from config.base import BASE_DIR

DATA_DIR = BASE_DIR / "data"
DATA_MODELA_DIR = DATA_DIR / "modelA"
DATA_MODELB_DIR = DATA_DIR / "modelB"

# 当前模式对应的数据目录（交易记录、持仓状态等按模式分离）
_HUNTER_MODE = (os.getenv("HUNTER_MODE", "MODELA") or "MODELA").strip().upper()
DATA_ACTIVE_DIR = DATA_MODELB_DIR if _HUNTER_MODE == "MODELB" else DATA_MODELA_DIR

# MODELA 专用（data/modelA/）
HUNTER_JSON_PATH = str(DATA_MODELA_DIR / "hunters.json")
HUNTER_BACKUP_PATH = str(DATA_MODELA_DIR / "hunters_backup.json")

# MODELB 专用（data/modelB/）— wallets.txt、smart_money.json、trash_wallets.txt 在 hunter.py 中定义

# 交易记录、状态（按模式放入 modelA 或 modelB）
TRADING_HISTORY_PATH = DATA_ACTIVE_DIR / "trading_history.json"
SUMMARY_FILE_PREFIX = "summary_report"
CLOSED_PNL_PATH = DATA_ACTIVE_DIR / "closed_pnl.json"
TRADER_STATE_PATH = DATA_ACTIVE_DIR / "trader_state.json"
SUMMARY_DIR = DATA_ACTIVE_DIR
