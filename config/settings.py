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
BOT_NAME = os.getenv("BOT_NAME", "DynamicSmartFlow3")  # 邮件标题前缀

# --- 日报配置 ---
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "8"))  # 每日几点发日报 (0-23)

# Jupiter API (v1)
JUP_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# 交易参数
SLIPPAGE_BPS = 200  # 滑点 2% (200 bps)
PRIORITY_FEE_SETTINGS = "auto"

# 单猎手跟单分档 (60-80 / 80-90 / 90+)，只跟一个猎手
def get_tier_config(score: float) -> dict:
    """根据猎手分数返回该档位的 entry/add/max/stop_loss。60 分以下返回 None。"""
    if score >= 90:
        return {"entry_sol": 0.2, "add_sol": 0.2, "max_sol": 1.0, "stop_loss_pct": 0.5}
    if score >= 80:
        return {"entry_sol": 0.1, "add_sol": 0.1, "max_sol": 0.5, "stop_loss_pct": 0.4}
    if score >= 60:
        return {"entry_sol": 0.05, "add_sol": 0.05, "max_sol": 0.2, "stop_loss_pct": 0.3}
    return None

# 猎手加仓跟随阈值：只跟对方买入 ≥ 1 SOL 的单
HUNTER_ADD_THRESHOLD_SOL = 1.0

# 兼容旧逻辑（不再使用）
TRADING_MAX_SOL_PER_TOKEN = 0.5
TRADING_MIN_BUY_SOL = 0.05
TRADING_ADD_BUY_SOL = 0.1
TRADING_SCORE_MULTIPLIER = 0.001

# 首买追高限制：首个猎手买入后若已涨 300%（现价/首买价 ≥ 4），坚决不买、不加仓
MAX_ENTRY_PUMP_MULTIPLIER = 4.0  # 4x = 300% 涨幅

# 止损：按档位 (60-80: 30% / 80-90: 40% / 90+: 50%)，由 get_tier_config 返回
STOP_LOSS_PCT = 0.4  # 默认兜底（80-90 档）

# 卖出精度保护：全仓/接近全仓卖出时仅卖 99.9%，避免浮点转 int 时多出 1 wei 导致链上失败
SELL_BUFFER = 0.999  # 留 0.1% 缓冲，防止 rounding overflow

# 止盈策略 (触发倍数, 卖出比例)
# 1.5 = 收益率 150% (即 2.5倍)
TAKE_PROFIT_LEVELS = [
    (1.0, 0.5),  # 赚 100% -> 卖 50%
    (3.0, 0.3),  # 赚 300% -> 卖 30%
    (5.0, 0.3),  # 赚 500% -> 卖 30%
    (7.0, 0.3),  # 赚 700% -> 卖 30%
    (10.0, 0.5)  # 赚 1000% -> 卖 50%
]

# 份额管理
# 剩余份额价值不足 0.05 SOL 直接清仓
MIN_SHARE_VALUE_SOL = 0.05  # 份额粉尘阈值 (低于此值该份额清仓)
MIN_SELL_RATIO = 0.3  # 跟随卖出时我方最低减仓比例 (如文档: 猎手卖10%我们也至少卖30%)
FOLLOW_SELL_THRESHOLD = 0.05  # 猎手卖出比例低于此不跟 (微调忽略，文档: 大于5%才跟)

# 系统循环配置
PNL_CHECK_INTERVAL = 5  # 每 5 秒检查一次止盈/止损

# 钱包配置 (请替换为真实私钥)
SOLANA_PRIVATE_KEY_BASE58 = os.getenv("SOLANA_PRIVATE_KEY")

# ==================== 猎手挖掘 (sm_searcher) ====================
MIN_TOKEN_AGE_SEC = 3600  # 最少上市 1 小时 (太新的币数据少，难找好猎手)
MAX_TOKEN_AGE_SEC = 21600  # 最多上市 12 小时 (放宽年龄)
MAX_BACKTRACK_PAGES = 50  # 最多回溯 50 页 (5万笔交易)
RECENT_TX_COUNT_FOR_FREQUENCY = 100  # 频繁交易判断的样本数
MIN_AVG_TX_INTERVAL_SEC = 300  # 平均间隔 < 5 分钟视为频繁交易
MIN_NATIVE_LAMPORTS_FOR_REAL = int(0.01 * 1e9)  # 至少 0.01 SOL 的 native 转账才算真实
SM_MIN_DELAY_SEC = 15  # 初筛：开盘后至少 15 秒买入才计入
SM_MAX_DELAY_SEC = 10800  # 3 小时内买入都算
SM_MIN_TOKEN_PROFIT_PCT = 200.0  # 猎手/初筛买家在该代币至少赚 200% 才入库
SM_AUDIT_TX_LIMIT = 500  # 体检时拉取交易笔数
SM_FREQUENCY_CHECK_TX_LIMIT = 120  # 频率检测只需约 100 笔真实交易，先拉前 120 笔；通过后再拉满 500
SM_EARLY_TX_PARSE_LIMIT = 360  # 初筛：最多解析多少笔早期交易（按时间取前 N 笔）
SM_MIN_BUY_SOL = 0.1  # 初筛：单笔买入最少 SOL
SM_MAX_BUY_SOL = 50.0  # 初筛：单笔买入最多 SOL
SM_MIN_WIN_RATE = 0.4  # 猎手入库：胜率至少 40%
SM_MIN_TOTAL_PROFIT = 100.0  # 猎手入库：或总利润 ≥ 100 SOL 可放宽胜率
SM_MIN_HUNTER_SCORE = 60  # 猎手入库最低分数
SCANNED_HISTORY_FILE = "data/scanned_tokens.json"

# ==================== DexScreener 扫描 ====================
DEX_MIN_LIQUIDITY_USD = 10000  # 最低流动性 (USD)
DEX_MIN_VOL_1H_USD = 50000  # 1 小时最低成交额 (USD)
DEX_MIN_24H_GAIN_PCT = 500.0  # 过去 24 小时涨幅 > 500% 才算热门币

# ==================== 风控 (risk_control) ====================
MAX_ACCEPTABLE_BUY_TAX_PCT = 25.0  # 买入税超过此比例拒绝
MAX_SAFE_SCORE = 2000  # RugCheck 风险分超过此值拒绝
MIN_LIQUIDITY_USD = 10000.0  # 池子流动性最低门槛防撤池 ($10k)
MAX_TOP2_10_COMBINED_PCT = 0.30  # 防老鼠仓：第 2~10 名合计上限
MAX_SINGLE_HOLDER_PCT = 0.10  # 防老鼠仓：单一地址上限
MAX_ENTRY_FDV_USD = 1000000.0  # 最大可接受入场 FDV (USD)
MIN_LIQUIDITY_TO_FDV_RATIO = 0.03  # 流动性/市值比至少 3%

# ==================== 猎手监控 (hunter_monitor) ====================
DISCOVERY_INTERVAL = 900  # 挖掘间隔 15 分钟
MAINTENANCE_INTERVAL = 86400  # 维护间隔 1 天
POOL_SIZE_LIMIT = 300  # 猎手池上限
ZOMBIE_THRESHOLD_DAYS = 15  # 多少天未交易视为僵尸
AUDIT_EXPIRATION_DAYS = 5  # 体检有效期 (天)
ZOMBIE_THRESHOLD = 86400 * ZOMBIE_THRESHOLD_DAYS
AUDIT_EXPIRATION = 86400 * AUDIT_EXPIRATION_DAYS
DISCOVERY_INTERVAL_WHEN_FULL_SEC = 86400  # 池满时挖掘间隔 24 小时
RECENT_SIG_TTL_SEC = 90  # 同一 signature 去重 TTL
FETCH_TX_MAX_RETRIES = 3  # 拉取交易重试次数
FETCH_TX_RETRY_DELAY_BASE = 2  # 重试延迟基数 (秒)
SIG_QUEUE_BATCH_SIZE = 15  # 批量拉取每批 signature 数
SIG_QUEUE_DRAIN_TIMEOUT = 0.3  # 凑批超时 (秒)
WALLET_WS_RESUBSCRIBE_SEC = 300  # WebSocket 重连间隔
HOLDINGS_TTL_SEC = 7200  # token 2 小时无新买入则清理
HOLDINGS_PRUNE_INTERVAL_SEC = 43200  # 每 12 小时扫描清理

# ==================== USDC 折算 (跟单 / 挖掘) ====================
USDC_PER_SOL = 100.0  # 1 SOL ≈ 100 USDC，用于 USDC 折算 SOL（不请求 API，默认 100U）

# ==================== 跟单管家 (hunter_agent) ====================
SYNC_POSITIONS_INTERVAL_SEC = 30  # 持仓同步间隔
SYNC_MIN_DELTA_RATIO = 0.01  # 变化 < 1% 视为误差不触发
SYNC_PROTECTION_AFTER_START_SEC = 60  # 启动后 60 秒内不同步
NEW_HUNTER_ADD_WINDOW_SEC = 600  # 开仓 10 分钟内才处理新猎手 (单猎手模式已禁用)

# ==================== 交易 (trader) ====================
TX_VERIFY_MAX_WAIT_SEC = 15  # 交易确认最大等待秒
TRADER_RPC_TIMEOUT = 30.0  # RPC 请求超时

# ==================== 其他 ====================
CRITICAL_EMAIL_COOLDOWN_SEC = 3600  # 严重错误邮件冷却 1 小时
WATCHDOG_RESTART_DELAY = 5  # 看门狗重启延迟 (秒)
