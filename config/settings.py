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

# --- Helius / Alchemy / Birdeye / Jupiter API Key 池（各自独立：谁不可用谁自己换下一个）---
_raw_helius = os.getenv("HELIUS_API_KEY", "") or ""
_raw_alchemy = os.getenv("ALCHEMY_API_KEY", "") or ""
_raw_birdeye = os.getenv("BIRDEYE_API_KEY", "") or ""
_raw_jup = os.getenv("JUP_API_KEY", "") or ""
HELIUS_API_KEYS = [k.strip() for k in _raw_helius.split(",") if k.strip()]
ALCHEMY_API_KEYS = [k.strip() for k in _raw_alchemy.split(",") if k.strip()]
BIRDEYE_API_KEYS = [k.strip() for k in _raw_birdeye.split(",") if k.strip()]
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


class AlchemyKeyPool(_KeyPool):
    """Alchemy Key 池，提供 Solana RPC/WSS URL。Alchemy 使用标准 Solana JSON-RPC。"""

    def __init__(self, keys: list):
        super().__init__(keys, "Alchemy")

    def get_rpc_url(self) -> str:
        key = self.get_api_key()
        return f"https://solana-mainnet.g.alchemy.com/v2/{key}" if key else ""

    def get_wss_url(self) -> str:
        key = self.get_api_key()
        return f"wss://solana-mainnet.g.alchemy.com/v2/{key}" if key else ""

    def get_http_endpoint(self) -> str:
        """Alchemy 无独立解析交易 HTTP 端点，返回空。"""
        return ""


class BirdeyeKeyPool(_KeyPool):
    """Birdeye Key 池，提供行情/价格 API。"""

    def __init__(self, keys: list):
        super().__init__(keys, "Birdeye")


helius_key_pool = HeliusKeyPool(HELIUS_API_KEYS)
alchemy_key_pool = AlchemyKeyPool(ALCHEMY_API_KEYS)
birdeye_key_pool = BirdeyeKeyPool(BIRDEYE_API_KEYS)
jup_key_pool = _KeyPool(JUP_API_KEYS, "Jupiter")

# 统一 Solana RPC 入口：helius | alchemy | auto（有 Helius 用 Helius，否则 Alchemy）
SOLANA_PRIMARY_PROVIDER = (os.getenv("SOLANA_PRIMARY_PROVIDER", "auto") or "auto").strip().lower()

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
# DAILY_REPORT_TIME 与 DAILY_REPORT_HOUR 二选一，TIME 优先（支持 "8" 或 "08:00" 格式，取小时）
_report_time = os.getenv("DAILY_REPORT_TIME", "") or ""
_report_hour_raw = os.getenv("DAILY_REPORT_HOUR", "8")
if _report_time:
    # 解析 "08:00" 或 "8" 格式，取小时部分
    parts = str(_report_time).strip().split(":")
    try:
        DAILY_REPORT_HOUR = int(parts[0])
    except (ValueError, IndexError):
        DAILY_REPORT_HOUR = 8
else:
    DAILY_REPORT_HOUR = int(_report_hour_raw)
DAILY_REPORT_HOUR = max(0, min(23, DAILY_REPORT_HOUR))  # 限制在 0-23

# Jupiter API (v1)
JUP_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# 交易参数
SLIPPAGE_BPS = 200  # 买入滑点 2% (200 bps)，买入失败无所谓，不重试
# 卖出失败时按此列表依次提高滑点重试，避免滑点过低卖不出（流动性差/剧烈波动时）
SELL_SLIPPAGE_BPS_RETRIES = [200, 500, 1000]  # 2% -> 5% -> 10%
PRIORITY_FEE_SETTINGS = "auto"

# 单猎手跟单分档 (<60 / 60-80 / 80-90 / 90+)，只跟一个猎手
def get_tier_config(score: float) -> dict:
    """
    根据猎手分数返回 entry/add/max/stop_loss。
    <60: 0.03×3=0.09 | 60-80: 0.05×4=0.2 | 80-90: 0.1×5=0.5 | 90+: 0.2×5=1.0
    """
    if score >= 90:
        return {"entry_sol": 0.05, "add_sol": 0.05, "max_sol": 0.15, "stop_loss_pct": 0.40}
    if score >= 80:
        return {"entry_sol": 0.04, "add_sol": 0.04, "max_sol": 0.12, "stop_loss_pct": 0.40}
    if score >= 60:
        return {"entry_sol": 0.03, "add_sol": 0.03, "max_sol": 0.09, "stop_loss_pct": 0.40}
    # 60 分以下也跟，但额度小
    return {"entry_sol": 0.03, "add_sol": 0.00, "max_sol": 0.03, "stop_loss_pct": 0.40}

# 猎手加仓跟随阈值：只跟对方买入 ≥ 1 SOL 的单
HUNTER_ADD_THRESHOLD_SOL = 1.0

# 首买追高限制：首个猎手买入后若已涨 300%（现价/首买价 ≥ 4），坚决不买、不加仓
MAX_ENTRY_PUMP_MULTIPLIER = 4.0  # 4x = 300% 涨幅

# 止损：按档位由 get_tier_config 返回，无独立常量

# 卖出精度保护：全仓/接近全仓卖出时仅卖 99.9%，避免浮点转 int 时多出 1 wei 导致链上失败
SELL_BUFFER = 0.999  # 留 0.1% 缓冲，防止 rounding overflow

# 止盈策略 (触发倍数, 卖出比例)
# 1.5 = 收益率 150% (即 2.5倍)
TAKE_PROFIT_LEVELS = [
    (1.0, 0.50),   # 涨1倍卖一半，零风险保本
    (4.0, 0.50),   # 涨4倍再卖一半
    (10.0, 0.80)   # 涨10倍收网
]

# 份额管理
# 剩余份额价值不足 0.05 SOL 直接清仓
MIN_SHARE_VALUE_SOL = 0.01  # 份额粉尘阈值 (低于此值该份额清仓)
MIN_SELL_RATIO = 0.3  # 跟随卖出时我方最低减仓比例 (如文档: 猎手卖10%我们也至少卖30%)
FOLLOW_SELL_THRESHOLD = 0.05  # 猎手卖出比例低于此不跟 (微调忽略，文档: 大于5%才跟)

# 系统循环配置
PNL_CHECK_INTERVAL = 5  # 每 5 秒检查一次止盈/止损

# 钱包配置 (请替换为真实私钥)
SOLANA_PRIVATE_KEY_BASE58 = os.getenv("SOLANA_PRIVATE_KEY")

# ==================== 猎手挖掘 (sm_searcher) ====================
MIN_TOKEN_AGE_SEC = 1800  # 最少上市 30 (太新的币数据少，难找好猎手)
MAX_TOKEN_AGE_SEC = 21600  # 最多上市 6 小时 (放宽年龄)
MAX_BACKTRACK_PAGES = 50  # 最多回溯 50 页 (5万笔交易)
RECENT_TX_COUNT_FOR_FREQUENCY = 100  # 频繁交易判断的样本数
MIN_SUCCESSFUL_TX_FOR_FREQUENCY = 10  # blockTime 频率检测：成功交易(err=null)少于此次否决（死号/新号/矩阵号）
MAX_FAILURE_RATE_FOR_FREQUENCY = 0.30  # 失败率超过 30% 即否决（Spam Bot 反向指标）
MIN_AVG_TX_INTERVAL_SEC = 300  # 平均间隔 < 5 分钟视为频繁交易
MIN_NATIVE_LAMPORTS_FOR_REAL = int(0.01 * 1e9)  # 至少 0.01 SOL 的 native 转账才算真实
SM_MIN_DELAY_SEC = 30  # 初筛：开盘后至少 30 秒买入才计入
SM_MAX_DELAY_SEC = 10800  # 3 小时内买入都算
SM_MIN_TOKEN_PROFIT_PCT = 50.0  # 入库门槛：该代币当下至少赚 100% 才能入池；≥200%×1，100%~200%×0.9；体检时 30 天内若掉到 <50% 则踢出
SM_AUDIT_TX_LIMIT = 300  # 体检时拉取交易笔数
SM_LP_CHECK_TX_LIMIT = 100  # LP 预检 + 频率检测：先拉 100 笔查 LP 行为（加池/撤池）及频率，有则直接淘汰，通过后再拉满 500
SM_EARLY_TX_PARSE_LIMIT = 300  # 初筛：最多解析多少笔早期交易（按时间取前 N 笔）
SM_USE_ATA_FIRST = os.getenv("SM_USE_ATA_FIRST", "true").lower() in ("1", "true", "yes")  # 先用 ATA 算 ROI，达标再拉主钱包，省 Helius
SM_ATA_SIG_LIMIT = 50  # ATA 模式最多拉签名数（通常 2~20 笔，1 次 POST=100 credits）
SM_MIN_BUY_SOL = 0.1  # 初筛：单笔买入最少 SOL
SM_MAX_BUY_SOL = 50.0  # 初筛：单笔买入最多 SOL
# 入库硬门槛（取代原 40% 胜率 / 100 SOL）
SM_ENTRY_MIN_PNL_RATIO = 2.0    # 总盈亏比 >= 2
SM_ENTRY_MIN_WIN_RATE = 0.25     # 胜率 >= 25%
SM_ENTRY_MIN_TRADE_COUNT = 7   # 有效代币项目数 >= 7
# 盈利分：< 20% 零分，20%~60% 线性，≥60% 满分
SM_PROFIT_SCORE_ZERO_PCT = 20.0   # < 20% → 盈利分=0
SM_PROFIT_SCORE_FULL_PCT = 60.0   # ≥60% → 盈利分=1
# ROI 软门槛乘数：≥100%×1，75%~100%×0.9，50%~75%×0.75
TIER_ONE_ROI = 100
TIER_TWO_ROI = 75
TIER_THREE_ROI = 50
SM_ROI_MULT_ONE = 1.0
SM_ROI_MULT_TWO = 0.9
SM_ROI_MULT_THREE = 0.75
# 体检踢出：盈亏比<2 或 胜率<20% 或 利润<=0
SM_AUDIT_MIN_PNL_RATIO = 2.0
SM_AUDIT_MIN_WIN_RATE = 0.25
# 体检踢出：最近 30 天最大收益 < 50% 直接踢出（入库需 100%+，可能未结算，结算后若掉到 50% 以下则踢出）
SM_AUDIT_KICK_MAX_ROI_30D_PCT = 50.0
SM_MIN_HUNTER_SCORE = 0  # 入库不设最低分（满足四项门槛即可）；池子按分数排序替换
# 胜率分：< 15% 零分，15%~35% 线性 0.5~1，≥35% 满分
SM_WIN_RATE_ZERO_PCT = 15.0   # < 15% → 胜率分=0
SM_WIN_RATE_FULL_PCT = 35.0   # ≥35% → 胜率分=1
# 盈亏比分：总盈亏比 = 盈利单利润和/亏损单亏损和；< 1.5 零分，1.5~3 线性，> 3 满分
SM_PNL_RATIO_ZERO = 1.5   # < 1.5 → 盈亏比分=0
SM_PNL_RATIO_FULL = 3.0   # > 3.0 → 盈亏比分=1
SCANNED_HISTORY_FILE = str(BASE_DIR / "data" / "scanned_tokens.json")
# 钱包黑名单：劣质猎手不再分析，节省 Helius API
WALLET_BLACKLIST_FILE = str(BASE_DIR / "data" / "wallet_blacklist.json")
WALLET_BLACKLIST_MIN_SCORE = 50  # 评分低于此加入黑名单
WALLET_BLACKLIST_LOSS_USDC = 30.0  # 亏损超过此金额 (USDC) 且胜率低则加入
WALLET_BLACKLIST_WIN_RATE = 0.4  # 亏损+胜率低于此加入黑名单

# ==================== DexScreener 扫描 ====================
DEX_MIN_LIQUIDITY_USD = 10000  # 最低流动性 (USD)
DEX_MIN_VOL_1H_USD = 50000  # 1 小时最低成交额 (USD)
DEX_MIN_24H_GAIN_PCT = 500.0  # 年龄在范围内时，涨幅 > 500% 才执行猎手挖掘；未达标不写 scanned，下次重试

# ==================== 风控 (risk_control) ====================
MAX_ACCEPTABLE_BUY_TAX_PCT = 25.0  # 买入税超过此比例拒绝
MAX_SAFE_SCORE = 2000  # RugCheck 风险分超过此值拒绝
MIN_LIQUIDITY_USD = 10000.0  # 池子流动性最低门槛防撤池 ($10k)
MIN_LP_LOCKED_PCT = 70.0  # LP 至少锁仓或销毁比例，否则拒绝（防撤池）
MAX_TOP2_10_COMBINED_PCT = 0.30  # 防老鼠仓：第 2~10 名合计上限
MAX_SINGLE_HOLDER_PCT = 0.10  # 防老鼠仓：单一地址上限
MAX_ENTRY_FDV_USD = 1000000.0  # 最大可接受入场 FDV (USD)
MIN_LIQUIDITY_TO_FDV_RATIO = 0.03  # 流动性/市值比至少 3%

# ==================== 猎手监控 (hunter_monitor) ====================
DISCOVERY_INTERVAL = 1800  # 挖掘间隔 30 分钟
POOL_SIZE_LIMIT = 100  # 猎手池上限
ZOMBIE_THRESHOLD_DAYS = 15  # 多少天未交易视为僵尸
AUDIT_EXPIRATION_DAYS = 20  # 体检有效期 (天)，超过需重新体检
MAINTENANCE_DAYS = 10   # 维护间隔 10 天，每 10 天检查一次是否要体检
ZOMBIE_THRESHOLD = 86400 * ZOMBIE_THRESHOLD_DAYS
AUDIT_EXPIRATION = 86400 * AUDIT_EXPIRATION_DAYS
MAINTENANCE_INTERVAL = 86400 * MAINTENANCE_DAYS
DISCOVERY_INTERVAL_WHEN_FULL_SEC = 86400  # 池满时挖掘间隔 24 小时
RECENT_SIG_TTL_SEC = 90  # 同一 signature 去重 TTL
FETCH_TX_MAX_RETRIES = 3  # 拉取交易重试次数
FETCH_TX_RETRY_DELAY_BASE = 2  # 重试延迟基数 (秒)
SIG_QUEUE_BATCH_SIZE = 100  # 批量拉取每批 signature 数（Helius 100 credits/次，凑满 100 笔更省）
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
TX_VERIFY_RETRY_DELAY_SEC = 15  # 初次验证失败后，等待链上传播再二次验证
TX_VERIFY_RETRY_MAX_WAIT_SEC = 30  # 二次验证最大等待秒（切换 RPC 后，避免限流误判）
# 验证失败时的链上余额兜底：Key 超额 429 时 RPC 查不到状态，需等待限流恢复后重试
TX_VERIFY_RECONCILIATION_DELAY_SEC = 30  # 验证失败后先等待此秒数再查余额，让 429 冷却
TX_VERIFY_RECONCILIATION_RETRIES = 5  # 余额查询重试次数，每次切换 Key + 退避
TRADER_RPC_TIMEOUT = 30.0  # RPC 请求超时

# ==================== RPC 限流（防 429）====================
ALCHEMY_MIN_INTERVAL_SEC = float(os.getenv("ALCHEMY_MIN_INTERVAL_SEC", "1.2"))  # Alchemy RPC 请求最小间隔，免费版易 429 可调大
HELIUS_MIN_INTERVAL_SEC = float(os.getenv("HELIUS_MIN_INTERVAL_SEC", "2"))  # Helius 解析请求最小间隔，还未被真正使用

# ==================== 其他 ====================
CRITICAL_EMAIL_COOLDOWN_SEC = 3600  # 严重错误邮件冷却 1 小时
WATCHDOG_RESTART_DELAY = 5  # 看门狗重启延迟 (秒)
