#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 邮箱、日报、RPC 限流、看门狗等杂项
"""
import os

# 邮箱
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
BOT_NAME = os.getenv("BOT_NAME", "DynamicSmartFlow3")

# 日报
_report_time = os.getenv("DAILY_REPORT_TIME", "") or ""
_report_hour_raw = os.getenv("DAILY_REPORT_HOUR", "8")
if _report_time:
    parts = str(_report_time).strip().split(":")
    try:
        DAILY_REPORT_HOUR = int(parts[0])
    except (ValueError, IndexError):
        DAILY_REPORT_HOUR = 8
else:
    DAILY_REPORT_HOUR = int(_report_hour_raw)
DAILY_REPORT_HOUR = max(0, min(23, DAILY_REPORT_HOUR))

# RPC 限流
ALCHEMY_MIN_INTERVAL_SEC = float(os.getenv("ALCHEMY_MIN_INTERVAL_SEC", "1.2"))
HELIUS_MIN_INTERVAL_SEC = float(os.getenv("HELIUS_MIN_INTERVAL_SEC", "2"))

# DexScreener 扫描器
DEX_SCAN_POLL_INTERVAL_SEC = 300

# 其他
CRITICAL_EMAIL_COOLDOWN_SEC = 3600
WATCHDOG_RESTART_DELAY = 5
WATCHDOG_CHILD_WAIT_TIMEOUT = 10
