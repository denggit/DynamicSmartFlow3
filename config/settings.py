#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/16/2026 10:35 PM
@File       : settings.py
@Description: 
"""
import os
import threading
from pathlib import Path

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

from dotenv import load_dotenv

load_dotenv(dotenv_path=ENV_PATH)

# --- Helius / Jupiter API Key 池（各自独立：谁不可用谁自己换下一个，用尽后回到第一个重试）---
_raw_helius = os.getenv("HELIUS_API_KEY", "") or ""
_raw_jup = os.getenv("JUP_API_KEY", "") or ""
HELIUS_API_KEYS = [k.strip() for k in _raw_helius.split(",") if k.strip()]
JUP_API_KEYS = [k.strip() for k in _raw_jup.split(",") if k.strip()]


class _KeyPool:
    """单类 Key 池：不可用时切下一个，最后一个用尽后回到第一个重试。"""

    def __init__(self, keys: list, name: str = ""):
        self._keys = list(keys) if keys else []
        self._index = 0
        self._lock = threading.Lock()
        self._name = name

    def get_api_key(self) -> str:
        with self._lock:
            if not self._keys:
                return ""
            return self._keys[self._index % len(self._keys)]

    def mark_current_failed(self) -> None:
        """当前 key 不可用，切换到下一个；已是最后一个则回到第一个。"""
        with self._lock:
            if not self._keys:
                return
            self._index = (self._index + 1) % max(len(self._keys), 1)

    @property
    def size(self) -> int:
        return len(self._keys)


class HeliusKeyPool(_KeyPool):
    """Helius Key 池，并提供 RPC/WSS/HTTP URL。"""

    def __init__(self, keys: list):
        super().__init__(keys, "Helius")

    def get_rpc_url(self) -> str:
        key = self.get_api_key()
        return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else ""

    def get_wss_url(self) -> str:
        key = self.get_api_key()
        return f"wss://mainnet.helius-rpc.com/?api-key={key}" if key else ""

    def get_http_endpoint(self) -> str:
        key = self.get_api_key()
        return f"https://api.helius.xyz/v0/transactions/?api-key={key}" if key else ""


helius_key_pool = HeliusKeyPool(HELIUS_API_KEYS)
jup_key_pool = _KeyPool(JUP_API_KEYS, "Jupiter")

HELIUS_API_KEY = helius_key_pool.get_api_key()
WSS_ENDPOINT = helius_key_pool.get_wss_url()
HTTP_ENDPOINT = helius_key_pool.get_http_endpoint()
HELIUS_RPC_URL = helius_key_pool.get_rpc_url()

# --- 邮箱配置 ---
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
BOT_NAME = os.getenv("BOT_NAME", "DSF3")  # 邮件标题前缀

# --- 日报配置 ---
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "8"))  # 每日几点发日报 (0-23)

# Jupiter API (v1)
JUP_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# 交易参数
SLIPPAGE_BPS = 200  # 滑点 2% (200 bps)
PRIORITY_FEE_SETTINGS = "auto"

# 仓位限制
TRADING_MAX_SOL_PER_TOKEN = 0.5  # 单币最大持仓 (SOL)
TRADING_MIN_BUY_SOL = 0.1  # 单次最低买入 (SOL)
TRADING_ADD_BUY_SOL = 0.1  # 加仓固定金额 (SOL)

# 入场计算
TRADING_SCORE_MULTIPLIER = 0.001  # 总分 * 倍数 = 买入SOL
# 例如: 160分 * 0.001 = 0.16 SOL

# 猎手加仓跟随阈值
HUNTER_ADD_THRESHOLD_SOL = 1.0  # 猎手加仓超过 1 SOL 我们才跟

# 止损：亏损超过此比例直接全仓清仓
STOP_LOSS_PCT = 0.5  # 亏损 50% 止损

# 止盈策略 (触发倍数, 卖出比例)
# 1.5 = 收益率 150% (即 2.5倍)
TAKE_PROFIT_LEVELS = [
    (1.5, 0.5),  # 赚 150% -> 卖 50%
    (3.0, 0.5),  # 赚 300% -> 卖 50%
    (10.0, 0.5)  # 赚 1000% -> 卖 50%
]

# 份额管理
# 剩余份额价值不足 0.05 SOL 直接清仓
MIN_SHARE_VALUE_SOL = 0.05  # 份额粉尘阈值 (低于此值该份额清仓)
MIN_SELL_RATIO = 0.3  # 跟随卖出时我方最低减仓比例 (如文档: 猎手卖10%我们也至少卖30%)
FOLLOW_SELL_THRESHOLD = 0.05  # 猎手卖出比例低于此不跟 (微调忽略，文档: 大于5%才跟)

# 系统循环配置
PNL_CHECK_INTERVAL = 5  # 每 5 秒检查一次价格止盈

# 钱包配置 (请替换为真实私钥)
SOLANA_PRIVATE_KEY_BASE58 = os.getenv("SOLANA_PRIVATE_KEY")
