#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : hunter_monitor.py
@Description: 猎手监控核心模块 (Hunter Monitor V3 - 低功耗版)
              1. [线程A] 挖掘: 定时补充新猎手
              2. [线程B] 监控: 实时监听交易 + 更新活跃时间 + 触发共振
              3. [线程C] 维护: 每 10 天检查，对超过 20 天未体检的猎手重算分数
"""

import asyncio
import json
import os
import shutil
import time
from collections import defaultdict
from typing import Dict, List, Callable, Optional, Set, Awaitable

import websockets

# 导入配置和依赖模块
from config.settings import (
    BASE_DIR,
    DATA_DIR,
    DATA_MODELA_DIR,
    DATA_MODELB_DIR,
    USDC_PER_SOL,
    MAX_ENTRY_PUMP_MULTIPLIER,
    DISCOVERY_INTERVAL,
    MAINTENANCE_INTERVAL,
    POOL_SIZE_LIMIT,
    ZOMBIE_THRESHOLD,
    AUDIT_EXPIRATION,
    RECENT_SIG_TTL_SEC,
    DISCOVERY_INTERVAL_WHEN_FULL_SEC,
    FETCH_TX_MAX_RETRIES,
    FETCH_TX_RETRY_DELAY_BASE,
    SIG_QUEUE_BATCH_SIZE,
    SIG_QUEUE_DRAIN_TIMEOUT,
    WALLET_WS_RESUBSCRIBE_SEC,
    HOLDINGS_TTL_SEC,
    HOLDINGS_PRUNE_INTERVAL_SEC,
    SM_AUDIT_MIN_PNL_RATIO,
    SM_AUDIT_MIN_WIN_RATE,
    SM_ROI_MULT_ONE,
    SM_ROI_MULT_TWO,
    SM_ROI_MULT_THREE,
    MAINTENANCE_DAYS,
    TIER_ONE_ROI,
    TIER_TWO_ROI,
    TIER_THREE_ROI,
    HUNTER_MODE,
    HUNTER_JSON_PATH,
    HUNTER_BACKUP_PATH,
    SMART_MONEY_JSON_PATH,
    WS_COMMITMENT,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    EMPTY_POOL_RETRY_SLEEP_SEC,
    WS_RECONNECT_SLEEP_SEC,
    CONSUME_QUEUE_EMPTY_SLEEP_SEC,
    CONSUME_QUEUE_ERROR_SLEEP_SEC,
    AUDIT_BETWEEN_HUNTERS_SLEEP_SEC,
    HTTP_CLIENT_DEFAULT_TIMEOUT,
)
from src import helius_client
from src.dexscreener.dex_scanner import DexScanner
from services.hunter_common import TransactionParser
from services.modela import SmartMoneySearcher
from services.modelb import SmartMoneySearcherB
from services.modelb.searcher import check_modelb_entry_criteria, _stored_entry_passes_criteria
from services.modela.scoring import compute_hunter_score as compute_hunter_score_modela
from services.modelb.scoring import compute_hunter_score as compute_hunter_score_modelb
from utils.logger import get_logger

logger = get_logger(__name__)


def _get_scorer():
    """按 HUNTER_MODE 返回对应评分函数。"""
    return compute_hunter_score_modelb if HUNTER_MODE == "MODELB" else compute_hunter_score_modela


def _roi_multiplier(roi_pct: float) -> float:
    """
    体检时用「最近 30 天最大收益率」给评分乘数：
    ≥200%×1，100%~200%×0.9，50~100%×0.75
    （<50% 已在体检前直接踢出，不会调用此处）
    """
    if roi_pct >= TIER_ONE_ROI:
        return SM_ROI_MULT_ONE
    if roi_pct >= TIER_TWO_ROI:
        return SM_ROI_MULT_TWO
    if roi_pct >= TIER_THREE_ROI:
        return SM_ROI_MULT_THREE
    return 0.75


def _apply_audit_update(info: dict, new_stats: dict, now: float, addr: str) -> None:
    """
    体检通过：更新猎手信息。
    MODELA：用 max_roi_30d 乘数调整评分。
    MODELB：直接使用三维度评分，无乘数。
    """
    score_result = _get_scorer()(new_stats)
    base_score = score_result["score"]
    if HUNTER_MODE == "MODELB":
        final_score = round(base_score, 1)
        info["profit_dim"] = round(score_result.get("profit_dim", 0), 1)
        info["persist_dim"] = round(score_result.get("persist_dim", 0), 1)
        info["auth_dim"] = round(score_result.get("auth_dim", 0), 1)
        info["dust_ratio"] = f"{new_stats.get('dust_ratio', 0):.1%}"
        info["closed_ratio"] = f"{new_stats.get('closed_ratio', 0):.1%}"
        info["trade_frequency"] = new_stats.get("trade_frequency", 0)
    else:
        max_roi_30d = new_stats.get("max_roi_30d", 0)
        mult = _roi_multiplier(max_roi_30d)
        final_score = round(base_score * mult, 1)
        info["max_roi_30d"] = max_roi_30d
    info["total_profit"] = f"{new_stats['total_profit']:.2f} SOL"
    info["win_rate"] = f"{new_stats['win_rate']:.1%}"
    pnl_r = new_stats.get("pnl_ratio", 0)
    info["pnl_ratio"] = f"{pnl_r:.2f}" if pnl_r != float("inf") else "∞"
    info["avg_roi_pct"] = f"{new_stats.get('avg_roi_pct', 0):.1f}%"
    info["score"] = final_score
    info["scores_detail"] = score_result["scores_detail"]
    info["last_audit"] = now
    if score_result["score"] == 0:
        logger.warning("📉 猎手 %s 表现恶化 (负盈利)，分数归零", addr[:12])
    else:
        logger.info("✅ 猎手 %s 体检完成 | 评分: %.1f | %s", addr[:12], final_score, score_result["scores_detail"])
# 猎手交易单独写入 monitor.log，便于查看时间与交易币种
trade_logger = get_logger("trade")

class HunterStorage:
    """
    负责猎手数据的持久化存储与动态管理
    """

    def __init__(self):
        self.hunters: Dict[str, Dict] = {}  # {address: {score, last_active, last_audit...}}
        self.ensure_data_dir()
        self.load_hunters()

    def ensure_data_dir(self):
        DATA_MODELA_DIR.mkdir(parents=True, exist_ok=True)

    def load_hunters(self):
        if os.path.exists(HUNTER_JSON_PATH):
            try:
                with open(HUNTER_JSON_PATH, 'r', encoding='utf-8') as f:
                    self.hunters = json.load(f)
                logger.info(f"📂 已加载 {len(self.hunters)} 名猎手数据")
            except Exception:
                logger.exception("❌ 加载猎手数据失败")
                if os.path.exists(HUNTER_BACKUP_PATH):
                    shutil.copy(HUNTER_BACKUP_PATH, HUNTER_JSON_PATH)
                    self.load_hunters()

    def save_hunters(self):
        try:
            if os.path.exists(HUNTER_JSON_PATH):
                shutil.copy(HUNTER_JSON_PATH, HUNTER_BACKUP_PATH)
            with open(HUNTER_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.hunters, f, indent=4, ensure_ascii=False)
        except Exception:
            logger.exception("❌ 保存猎手数据失败")

    def update_last_active(self, address: str, timestamp: float):
        """实时更新猎手最后交易时间"""
        if address in self.hunters:
            self.hunters[address]['last_active'] = timestamp

    def get_monitored_addresses(self) -> List[str]:
        return list(self.hunters.keys())

    def get_hunter_score(self, address: str) -> float:
        """返回猎手分数，确保为数值类型（JSON 可能为 int）。"""
        v = self.hunters.get(address, {}).get('score', 0)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def prune_and_update(self, new_hunters: List[Dict] = None) -> Dict:
        """
        库满时的优胜劣汰。
        返回: {"added": n, "removed_zombie": n, "replaced": n} 供变化通知使用。
        """
        now = time.time()
        added, removed_zombie, replaced = 0, 0, 0

        # 1. 清理僵尸 (15天未交易)
        zombies = []
        for addr, info in self.hunters.items():
            last_active = info.get('last_active', 0)
            if last_active == 0: continue  # 刚入库的新人豁免

            if (now - last_active) > ZOMBIE_THRESHOLD:
                zombies.append(addr)

        for z in zombies:
            logger.info(f"💀 清理僵尸地址 (15天未动): {z}..")
            del self.hunters[z]
        removed_zombie = len(zombies)

        # 2. 处理新猎手（sm_searcher 已过滤，满足四项门槛即可）
        if new_hunters:
            for h in new_hunters:
                addr = h['address']
                h['last_active'] = h.get('last_active', now)
                h['last_audit'] = h.get('last_audit', now)  # 新人入库算作刚体检

                if addr in self.hunters:
                    # 同一挖掘批次中同一地址可能来自不同代币，只在高分时更新，避免低分覆盖高分
                    old_score = float(self.hunters[addr].get('score', 0) or 0)
                    new_score = float(h.get('score', 0) or 0)
                    if new_score <= old_score:
                        continue
                    old_audit = self.hunters[addr].get('last_audit', 0)
                    self.hunters[addr].update(h)
                    self.hunters[addr]['last_audit'] = old_audit
                    continue

                if len(self.hunters) < POOL_SIZE_LIMIT:
                    self.hunters[addr] = h
                    added += 1
                    logger.info(f"🆕 新猎手入库: {addr} (分:{h['score']})")
                else:
                    # 库满 PK：按分数升序取最低
                    def _score_val(item):
                        return float(item[1].get('score', 0) or 0)
                    sorted_hunters = sorted(self.hunters.items(), key=_score_val)
                    lowest_addr, lowest_val = sorted_hunters[0]

                    if float(h.get('score', 0) or 0) > _score_val((lowest_addr, lowest_val)):
                        logger.info(f"♻️ 优胜劣汰: {h['score']}分 替换了 {lowest_val.get('score', 0)}分")
                        del self.hunters[lowest_addr]
                        self.hunters[addr] = h
                        replaced += 1

        # 移除已废弃字段（无业务引用）
        for info in self.hunters.values():
            info.pop("entry_delay", None)
            info.pop("cost", None)

        self.save_hunters()
        return {"added": added, "removed_zombie": removed_zombie, "replaced": replaced}


class SmartMoneyStorage:
    """
    MODELB 专用存储：读写 smart_money.json。
    与 HunterStorage 接口一致，供 Monitor 透明使用。
    """

    def __init__(self):
        self.hunters: Dict[str, Dict] = {}
        self.data_file = SMART_MONEY_JSON_PATH
        self.ensure_data_dir()
        self.load_hunters()

    def ensure_data_dir(self):
        DATA_MODELB_DIR.mkdir(parents=True, exist_ok=True)

    def load_hunters(self):
        """从 smart_money.json 加载猎手数据，并移除历史遗留的不达标条目。"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    self.hunters = json.load(f)
                if not isinstance(self.hunters, dict):
                    self.hunters = {}
                # 校验存量数据：移除 avg_roi_pct≤10% 等不达标条目
                to_remove = [addr for addr, info in self.hunters.items() if not _stored_entry_passes_criteria(info)]
                for addr in to_remove:
                    del self.hunters[addr]
                if to_remove:
                    logger.warning(
                        "[MODELB 加载清理] 移除 %d 条不达标历史数据: %s",
                        len(to_remove), ", ".join(a[:12] + ".." for a in to_remove[:5])
                        + (" ..." if len(to_remove) > 5 else ""),
                    )
                    self.save_hunters()
                logger.info(f"📂 [MODELB] 已加载 {len(self.hunters)} 名猎手 (smart_money.json)")
            except Exception:
                logger.exception("❌ 加载 smart_money.json 失败")

    def save_hunters(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.hunters, f, indent=4, ensure_ascii=False)
        except Exception:
            logger.exception("❌ 保存 smart_money.json 失败")

    def update_last_active(self, address: str, timestamp: float):
        if address in self.hunters:
            self.hunters[address]["last_active"] = timestamp

    def get_monitored_addresses(self) -> List[str]:
        return list(self.hunters.keys())

    def get_hunter_score(self, address: str) -> float:
        v = self.hunters.get(address, {}).get("score", 0)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def prune_and_update(self, new_hunters: List[Dict] = None) -> Dict:
        """
        MODELB：new_hunters 由 pipeline 直接写入文件，此处仅做僵尸清理。
        返回统计供兼容。
        """
        now = time.time()
        zombies = [
            addr
            for addr, info in self.hunters.items()
            if info.get("last_active", 0) != 0 and (now - info.get("last_active", 0)) > ZOMBIE_THRESHOLD
        ]
        for z in zombies:
            logger.info(f"💀 [MODELB] 清理僵尸地址 (15天未动): {z}..")
            del self.hunters[z]
        self.save_hunters()
        return {"added": 0, "removed_zombie": len(zombies), "replaced": 0}


class HunterMonitorController:
    def __init__(
        self,
        signal_callback: Optional[Callable] = None,
        tracked_tokens_getter: Optional[Callable[[], Set[str]]] = None,
        position_check: Optional[Callable[[str], bool]] = None,
    ):
        self.mode = (HUNTER_MODE or "MODELA").strip().upper()
        if self.mode == "MODELB":
            self.storage = SmartMoneyStorage()
            self.sm_searcher = SmartMoneySearcherB(dex_scanner=None)
        else:
            self.storage = HunterStorage()
            self.sm_searcher = SmartMoneySearcher()
        self.dex_scanner = DexScanner()
        if self.mode == "MODELB":
            self.sm_searcher.dex_scanner = self.dex_scanner
        self.signal_callback = signal_callback
        self.tracked_tokens_getter = tracked_tokens_getter  # 主程序注入，返回正在跟仓的 token 集合
        self.position_check = position_check  # 主程序注入，(token) -> 是否已有仓位；有则不再触发共振

        # 实时持仓状态池
        self.active_holdings = defaultdict(dict)
        # 首个猎手买入时的价格（用于 300% 追高限制）
        self._first_buy_price: Dict[str, float] = {}
        # 首个买入该代币的猎手地址；若其在共振前清仓，则永不再触发共振
        self._first_buyer: Dict[str, str] = {}
        # 已发出共振信号的代币（用于判断「首买者清仓时是否已共振」）
        self._resonance_emitted: Set[str] = set()
        # 永久禁止共振的代币（首买者在共振前已清仓）
        self._blacklisted_mints: Set[str] = set()
        # 去重：同一 signature 在 TTL 内只处理一次
        self._recent_sigs: Dict[str, float] = {}  # signature -> 首次处理时间
        # 【Program WS】只产 signature，中后段消费；钱包池不参与 WS 订阅
        self._sig_queue: asyncio.Queue = asyncio.Queue()
        # 跟仓阶段：命中钱包池的 tx 推给 Agent，避免 Agent 自建 WS 漏单
        self.agent: Optional[Callable] = None
        # Helius credit 耗尽时的保命回调：清仓 + 致命错误告警（主程序注入）
        self.on_helius_credit_exhausted: Optional[Callable[[], Awaitable[None]]] = None
        self._helius_emergency_triggered = False
        # 体检踢出猎手时的兜底回调：若该猎手正在跟仓，清仓对应持仓（主程序注入）
        self.on_hunter_removed: Optional[Callable[[str], Awaitable[None]]] = None

    def set_agent(self, agent) -> None:
        """主程序注入 Agent，Monitor 消费队列命中后会把 (tx, active_hunters) 推给 Agent。"""
        self.agent = agent

    async def _trigger_hunter_removed(self, addr: str) -> None:
        """体检踢出猎手时调用，触发兜底清仓（若该猎手正在跟仓）。"""
        if self.on_hunter_removed:
            try:
                await self.on_hunter_removed(addr)
            except Exception:
                logger.exception("on_hunter_removed 回调异常: %s", addr[:12])

    def set_on_helius_credit_exhausted(self, callback: Callable[[], Awaitable[None]]) -> None:
        """主程序注入：Helius credit 耗尽（429）时触发保命操作：清仓所有 + 致命错误告警。"""
        self.on_helius_credit_exhausted = callback

    async def start(self):
        logger.info(
            "🚀 启动 Hunter Monitor 系统 [%s] (transactionSubscribe 按猎手地址，只收猎手相关交易)",
            self.mode,
        )
        tasks = [
            asyncio.create_task(self.discovery_loop()),
            asyncio.create_task(self._program_ws_loop()),       # 只拿 signature 入队
            asyncio.create_task(self._consume_sig_queue_loop()), # 批量拉取 + 钱包过滤 + 发消息
            asyncio.create_task(self.maintenance_loop()),
            asyncio.create_task(self._prune_holdings_loop()),   # 每 12 小时清理超时且未跟仓的 active_holdings
        ]
        await asyncio.gather(*tasks)

    # --- 线程 1: 挖掘 ---
    async def discovery_loop(self):
        logger.info("🕵️ [线程1] 挖掘启动 [%s]", self.mode)
        while True:
            try:
                if self.mode == "MODELB":
                    # MODELB: wallets.txt → 三维度分析 → smart_money.json
                    await self.sm_searcher.run_pipeline()
                    self.storage.load_hunters()  # 刷新内存
                    await asyncio.to_thread(self.storage.prune_and_update, [])
                else:
                    # MODELA: DexScreener 热门币 → sm_searcher → hunters.json
                    new_hunters = await self.sm_searcher.run_pipeline(self.dex_scanner)
                    if new_hunters:
                        await asyncio.to_thread(self.storage.prune_and_update, new_hunters)
            except Exception:
                logger.exception("❌ 挖掘异常")
            if len(self.storage.hunters) >= POOL_SIZE_LIMIT:
                await asyncio.sleep(DISCOVERY_INTERVAL_WHEN_FULL_SEC)
            else:
                await asyncio.sleep(DISCOVERY_INTERVAL)

    async def _prune_holdings_loop(self):
        """每 12 小时扫描 active_holdings：超过 2 小时无新猎手买入且未跟仓的 token 删除。"""
        logger.info("🧹 [Holdings 清理] 启动，每 12 小时扫描")
        while True:
            try:
                await asyncio.sleep(HOLDINGS_PRUNE_INTERVAL_SEC)
                tracked = set()
                if self.tracked_tokens_getter:
                    try:
                        tracked = self.tracked_tokens_getter()
                    except Exception:
                        logger.exception("tracked_tokens_getter 异常")
                now = time.time()
                to_remove = []
                for mint, holders in list(self.active_holdings.items()):
                    if mint in tracked:
                        continue
                    if not holders:
                        to_remove.append(mint)
                        continue
                    newest = max(holders.values())
                    if now - newest >= HOLDINGS_TTL_SEC:
                        to_remove.append(mint)
                for mint in to_remove:
                    del self.active_holdings[mint]
                    self._first_buy_price.pop(mint, None)
                    self._first_buyer.pop(mint, None)
                if to_remove:
                    logger.info("🧹 [Holdings 清理] 删除 %d 条超时未跟仓记录: %s", len(to_remove), to_remove[:5])
            except Exception:
                logger.exception("_prune_holdings_loop 异常")

    # --- 【WS 订阅】Helius transactionSubscribe：accountInclude 一次传所有猎手地址，只收猎手相关交易 ---
    async def _program_ws_loop(self):
        """使用 transactionSubscribe + accountInclude，一次订阅最多 5 万地址，只收猎手相关 tx。"""
        while True:
            try:
                addrs = self.storage.get_monitored_addresses()
                if not addrs:
                    logger.info("👀 猎手池为空，%d 秒后重试订阅", EMPTY_POOL_RETRY_SLEEP_SEC)
                    await asyncio.sleep(EMPTY_POOL_RETRY_SLEEP_SEC)
                    continue

                async with websockets.connect(
                    helius_client.get_wss_url(),
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=None,
                    max_size=None,
                ) as ws:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "transactionSubscribe",
                        "params": [
                            {"accountInclude": addrs, "failed": False},
                            {
                                "commitment": WS_COMMITMENT,
                                "encoding": "jsonParsed",
                                "transactionDetails": "signatures",
                                "maxSupportedTransactionVersion": 0,
                            },
                        ],
                    }
                    await ws.send(json.dumps(payload))
                    logger.info(
                        "📤 已发送 transactionSubscribe（accountInclude %d 个猎手），只收猎手相关交易。示例地址: %s ...",
                        len(addrs),
                        addrs[0][:12] + "..." if addrs else "(无)",
                    )
                    sub_ok = False
                    recv_count = 0
                    idle_60s_count = 0
                    recv_deadline = time.time() + WALLET_WS_RESUBSCRIBE_SEC

                    while True:
                        try:
                            timeout = min(60, max(1, int(recv_deadline - time.time())))
                            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                            data = json.loads(msg)

                            if data.get("method") == "transactionNotification":
                                idle_60s_count = 0
                                res = (data.get("params") or {}).get("result") or {}
                                sig = res.get("signature")
                                if sig:
                                    recv_count += 1
                                    if not sub_ok:
                                        sub_ok = True
                                        logger.info("✅ 订阅已正常，已收到首笔交易推送")
                                    logger.info(
                                        "📨 [猎手交易] sig=%s (本连接第 %d 笔)",
                                        sig[:20] + "..." if len(sig) > 20 else sig,
                                        recv_count,
                                    )
                                    self._sig_queue.put_nowait(sig)
                            elif "error" in data:
                                logger.warning("⚠️ WebSocket 返回错误: %s", data.get("error"))
                            elif "id" in data and "result" in data:
                                logger.info("📩 订阅确认 id=%s result=%s", data.get("id"), data.get("result"))
                            else:
                                logger.info(
                                    "收到 WebSocket 消息: method=%s id=%s",
                                    data.get("method"),
                                    data.get("id"),
                                )
                        except asyncio.TimeoutError:
                            await ws.ping()
                            idle_60s_count += 1
                            if time.time() >= recv_deadline:
                                logger.info("🔄 到达重订阅间隔，重连以刷新猎手池（本次共收到 %d 笔）", recv_count)
                                break
                            if idle_60s_count >= 10:
                                logger.info(
                                    "监控运行中 | 已 %d 分钟无新推送（本连接共 %d 笔）",
                                    idle_60s_count,
                                    recv_count,
                                )
                                idle_60s_count = 0

            except Exception as e:
                status_code = getattr(e, "status_code", None)
                if status_code == 429 or "429" in str(e).lower():
                    helius_client.mark_current_failed()
                    logger.warning("⚠️ Helius WebSocket 429 限流，已切换 Key，%d 秒后重试", WS_RECONNECT_SLEEP_SEC)
                else:
                    logger.exception("⚠️ WS 重连异常")
                await asyncio.sleep(WS_RECONNECT_SLEEP_SEC)

    # --- 【钱包池过滤 + 轻量解析】消费队列：批量拉取 → 只保留命中钱包池且真实交易的 tx ---
    @staticmethod
    def _involved_accounts(tx: dict) -> set:
        """从 feePayer + nativeTransfers + tokenTransfers 收集参与账户，不依赖 accountData。"""
        out = set()
        fp = tx.get("feePayer") or tx.get("fee_payer")
        if fp:
            out.add(fp)
        for nt in tx.get("nativeTransfers", []):
            for k in ("fromUserAccount", "toUserAccount"):
                a = nt.get(k)
                if a:
                    out.add(a)
        for tt in tx.get("tokenTransfers", []):
            for k in ("fromUserAccount", "toUserAccount"):
                a = tt.get(k)
                if a:
                    out.add(a)
        return out

    @staticmethod
    def _is_real_trade_light(tx: dict) -> bool:
        """轻量判断：有 SOL/Token 实质变动即可。"""
        return bool(tx.get("tokenTransfers") or tx.get("nativeTransfers"))

    async def _consume_sig_queue_loop(self):
        """从队列取 signature → 去重 → 批量 POST /transactions → 钱包池过滤 → 轻量真实交易 → 发消息。"""
        logger.info("📥 [消费队列] 启动（批量拉取 + 钱包池过滤）")
        from httpx import AsyncClient
        while True:
            try:
                batch = []
                for _ in range(SIG_QUEUE_BATCH_SIZE):
                    try:
                        sig = await asyncio.wait_for(self._sig_queue.get(), timeout=SIG_QUEUE_DRAIN_TIMEOUT)
                        if sig:
                            batch.append(sig)
                    except asyncio.TimeoutError:
                        break
                if not batch:
                    await asyncio.sleep(CONSUME_QUEUE_EMPTY_SLEEP_SEC)
                    continue

                now = time.time()
                to_fetch = [s for s in batch if (s not in self._recent_sigs) or (now - self._recent_sigs[s]) >= RECENT_SIG_TTL_SEC]
                if not to_fetch:
                    continue
                for s in to_fetch:
                    self._recent_sigs[s] = now
                for sig in list(self._recent_sigs.keys()):
                    if now - self._recent_sigs[sig] > RECENT_SIG_TTL_SEC * 2:
                        del self._recent_sigs[sig]

                url = helius_client.get_http_endpoint()
                async with AsyncClient(timeout=HTTP_CLIENT_DEFAULT_TIMEOUT) as client:
                    for attempt in range(FETCH_TX_MAX_RETRIES):
                        # Helius 按次计费(100 credits/次)，每批最多 100 笔，尽量凑满以节省 credit
                        resp = await client.post(url, json={"transactions": to_fetch[:100]})
                        if resp.status_code == 429 and helius_client.size >= 1:
                            helius_client.mark_current_failed()
                            url = helius_client.get_http_endpoint()
                        if resp.status_code != 200:
                            if attempt < FETCH_TX_MAX_RETRIES - 1:
                                await asyncio.sleep(FETCH_TX_RETRY_DELAY_BASE + attempt)
                            continue
                        txs = resp.json() or []
                        for tx in txs:
                            if not tx:
                                continue
                            involved = self._involved_accounts(tx)
                            hunter_set = set(self.storage.get_monitored_addresses())
                            if not hunter_set:
                                continue
                            active_hunters = involved & hunter_set
                            if not active_hunters:
                                continue
                            if not self._is_real_trade_light(tx):
                                continue
                            logger.info("本笔涉及 %d 名猎手: %s", len(active_hunters), [h for h in list(active_hunters)[:5]])
                            for hunter in active_hunters:
                                self.storage.update_last_active(hunter, time.time())
                                await self._process_one_tx(hunter, tx)
                            # 跟仓：同一笔 tx 推给 Agent，避免 Agent 自建 WS 漏单导致卖不掉
                            if self.agent and hasattr(self.agent, "on_tx_from_monitor"):
                                try:
                                    await self.agent.on_tx_from_monitor(tx, active_hunters)
                                except Exception:
                                    logger.exception("Agent.on_tx_from_monitor 异常")
                        break
                    else:
                        logger.warning("批量拉取失败（已重试 %d 次）", FETCH_TX_MAX_RETRIES)
                        # 若为 429（credit 耗尽/限流）且未触发过，执行保命：清仓所有 + 致命错误告警
                        if (
                            resp.status_code == 429
                            and not self._helius_emergency_triggered
                            and self.on_helius_credit_exhausted
                        ):
                            self._helius_emergency_triggered = True
                            try:
                                await self.on_helius_credit_exhausted()
                            except Exception:
                                logger.exception("on_helius_credit_exhausted 回调异常")
            except Exception:
                logger.exception("消费队列异常")
                await asyncio.sleep(CONSUME_QUEUE_ERROR_SLEEP_SEC)

    def _get_usdc_price_sol(self) -> float:
        """1 USDC = ? SOL，使用配置固定值，不请求 API。"""
        return 1.0 / USDC_PER_SOL if USDC_PER_SOL > 0 else 0.01

    async def _process_one_tx(self, hunter: str, tx: dict):
        """单笔命中猎手的 tx：解析买卖、写 monitor.log、触发共振。"""
        usdc_price = self._get_usdc_price_sol()
        parser = TransactionParser(hunter)
        sol_change, token_changes, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price)
        for mint, delta in token_changes.items():
            if abs(delta) < 1e-9:
                continue
            if sol_change < 0 and delta > 0:
                was_first = len(self.active_holdings[mint]) == 0
                self.active_holdings[mint][hunter] = time.time()
                if was_first:
                    self._first_buyer[mint] = hunter
                    try:
                        p = await self.dex_scanner.get_token_price(mint)
                        if p and p > 0:
                            self._first_buy_price[mint] = p
                    except Exception:
                        pass
                trade_logger.info(f"📥 买入: {hunter} -> {mint}")
                holders = self.active_holdings[mint]
                trade_logger.info("📊 %s 当前被 %d 个猎手持仓", mint, len(holders))
            elif sol_change > 0 and delta < 0:
                if hunter in self.active_holdings[mint]:
                    first_buyer = self._first_buyer.get(mint)
                    del self.active_holdings[mint][hunter]
                    # 首买者在共振前清仓 → 永久禁止该代币共振
                    if first_buyer == hunter and mint not in self._resonance_emitted:
                        self._blacklisted_mints.add(mint)
                        trade_logger.info("🚫 首买者 %s 已清仓且未达共振，代币 %s 永久禁止共振", hunter[:8], mint[:8])
                trade_logger.info(f"📤 卖出: {hunter} -> {mint}")
            await self.check_resonance(mint)

    async def analyze_action(self, hunter, tx):
        usdc_price = self._get_usdc_price_sol()
        parser = TransactionParser(hunter)
        sol_change, token_changes, ts = parser.parse_transaction(tx, usdc_price_sol=usdc_price)

        for mint, delta in token_changes.items():
            if abs(delta) < 1e-9: continue

            if sol_change < 0 and delta > 0:  # BUY
                self.active_holdings[mint][hunter] = time.time()
                trade_logger.info(f"📥 买入: {hunter} -> {mint}")
            elif sol_change > 0 and delta < 0:  # SELL
                if hunter in self.active_holdings[mint]:
                    del self.active_holdings[mint][hunter]
                    trade_logger.info(f"📤 卖出: {hunter} -> {mint}")

            await self.check_resonance(mint)

    async def check_resonance(self, mint):
        if mint in self._blacklisted_mints:
            return
        holders = self.active_holdings[mint]
        if not holders: return

        # 只跟最早交易该 token 的猎手，其余猎手不跟（分数决定跟仓额度，无最低分限制）
        first_buyer = self._first_buyer.get(mint)
        if not first_buyer or first_buyer not in holders:
            trade_logger.debug("共振跳过: %s 无首买者", mint[:8])
            return

        lead_addr = first_buyer
        lead_score = self.storage.get_hunter_score(first_buyer)
        # 黑名单过滤：老鼠仓等永不跟仓
        if self.sm_searcher and self.sm_searcher.is_blacklisted(lead_addr):
            trade_logger.warning("🚫 共振跳过: %s 猎手 %s.. 在黑名单内（如老鼠仓）", mint[:8], lead_addr[:12])
            return
        if True:  # 单猎手共振条件：有一个 ≥60 分猎手持仓即可
            if self.position_check and self.position_check(mint):
                return
            # 首买追高限制：首个猎手买入后已涨 300% 则坚决不买
            first_price = self._first_buy_price.get(mint)
            if first_price and first_price > 0:
                try:
                    curr_price = await self.dex_scanner.get_token_price(mint)
                    if curr_price and curr_price >= first_price * MAX_ENTRY_PUMP_MULTIPLIER:
                        trade_logger.info(
                            "🚫 共振跳过: %s 首买后已涨 %.0f%% (%.6f -> %.6f)",
                            mint[:8], (curr_price / first_price - 1) * 100, first_price, curr_price
                        )
                        return
                except Exception:
                    pass
            self._resonance_emitted.add(mint)
            lead_hunter_info = self.storage.hunters.get(lead_addr, {"address": lead_addr, "score": lead_score})
            if isinstance(lead_hunter_info.get("score"), (int, float)):
                pass
            else:
                lead_hunter_info = dict(lead_hunter_info)
                lead_hunter_info["score"] = lead_score
            trade_logger.info(f"🚨 共振触发: {mint} (跟单猎手 {lead_addr[:8]}.. 分:{lead_score})")
            if self.signal_callback:
                signal = {
                    "token_address": mint,
                    "hunters": [lead_hunter_info],
                    "total_score": lead_score,
                    "timestamp": time.time()
                }
                if asyncio.iscoroutinefunction(self.signal_callback):
                    await self.signal_callback(signal)
                else:
                    self.signal_callback(signal)

    async def run_immediate_audit(self) -> None:
        """
        启动时可选执行：立即对 data/hunters.json 中所有猎手做一次审计体检。
        体检规则：pnl/wr/profit 未达标踢出；30 天最大收益 < 50% 直接踢出；其余按 max_roi_30d 加权更新。
        """
        from httpx import AsyncClient

        current = list(self.storage.hunters.items())
        if not current:
            logger.info("📋 猎手池为空，跳过立即审计")
            return

        logger.info("🩺 [立即审计] 开始对 %d 名猎手逐一体检...", len(current))
        removed = 0
        updated = 0

        async with AsyncClient() as client:
            for addr, info in current:
                if addr not in self.storage.hunters:
                    continue
                try:
                    new_stats = await self.sm_searcher.analyze_hunter_performance(client, addr)
                    if new_stats is None:
                        continue
                    if new_stats.get("_lp_detected"):
                        if addr in self.storage.hunters:
                            del self.storage.hunters[addr]
                            removed += 1
                            await self._trigger_hunter_removed(addr)
                        if HUNTER_MODE == "MODELB":
                            self.sm_searcher.add_to_trash(addr)
                        logger.info("🚫 剔除 %s.. (体检发现 LP 行为，已加入 trash)", addr[:12])
                        continue
                    is_modelb = HUNTER_MODE == "MODELB"
                    if is_modelb:
                        audit_pass, reasons = check_modelb_entry_criteria(new_stats)
                        if not audit_pass:
                            del self.storage.hunters[addr]
                            removed += 1
                            await self._trigger_hunter_removed(addr)
                            logger.info("🚫 剔除 %s.. (体检未过: %s)", addr[:12], "/".join(reasons))
                        else:
                            _apply_audit_update(info, new_stats, time.time(), addr)
                            updated += 1
                    else:
                        pnl_min = SM_AUDIT_MIN_PNL_RATIO
                        wr_min = SM_AUDIT_MIN_WIN_RATE
                        roi_threshold = TIER_THREE_ROI
                        pnl_ok = (new_stats.get("pnl_ratio", 0) or 0) >= pnl_min if new_stats.get("pnl_ratio") != float("inf") else True
                        wr_ok = new_stats["win_rate"] >= wr_min
                        profit_ok = new_stats["total_profit"] > 0
                        roi_val = new_stats.get("max_roi_pct", 0) or new_stats.get("max_roi_30d", 0)

                        if not (pnl_ok and wr_ok and profit_ok):
                            del self.storage.hunters[addr]
                            removed += 1
                            await self._trigger_hunter_removed(addr)
                            logger.info("🚫 剔除 %s.. (盈亏比/胜率/利润未达标)", addr[:12])
                        elif roi_val < roi_threshold:
                            del self.storage.hunters[addr]
                            removed += 1
                            await self._trigger_hunter_removed(addr)
                            logger.info("🚫 剔除 %s.. (最大收益 %.0f%% < %s%%)", addr[:12], roi_val, roi_threshold)
                        else:
                            _apply_audit_update(info, new_stats, time.time(), addr)
                            updated += 1
                except Exception:
                    logger.exception("审计猎手 %s 异常，跳过", addr[:12])
                await asyncio.sleep(AUDIT_BETWEEN_HUNTERS_SLEEP_SEC)

        await asyncio.to_thread(self.storage.save_hunters)
        logger.info("🩺 [立即审计] 完成 | 剔除 %d 名 | 更新 %d 名", removed, updated)

    # --- 线程 3: 维护 (Maintenance - 优化版) ---
    async def maintenance_loop(self):
        """
        每 10 天检查，对超过 20 天未体检的猎手重新审计；并清理频繁交易者。
        """
        logger.info("🛠️ [线程3] 维护线程启动 (每 %d 天检查体检)", MAINTENANCE_DAYS)

        # 启动时先睡一会，错开高峰，或者直接运行一次也行
        # 这里选择立即运行第一次，然后按天循环

        while True:
            try:
                logger.info("🏥 开始例行维护（检查体检、清理僵尸）...")
                now = time.time()

                # 1. 遍历检查是否需要体检
                current_hunters = list(self.storage.hunters.items())
                needs_audit_count = 0

                from httpx import AsyncClient
                async with AsyncClient() as client:
                    # 0. 体检剔除：pnl_ratio<2 或 wr<20% 或 profit<=0 或 30天最大收益<50%
                    audit_removed = []
                    # 1. 频繁交易剔除：最近 100 笔平均间隔 < 5 分钟的踢出猎手池
                    frequent_removed = []
                    for addr, _ in current_hunters:
                        if await self.sm_searcher.is_frequent_trader(client, addr):
                            frequent_removed.append(addr)
                    for addr in frequent_removed:
                        if addr in self.storage.hunters:
                            del self.storage.hunters[addr]
                            await self._trigger_hunter_removed(addr)
                            logger.info("🚫 踢出频繁交易猎手 %s.. (平均间隔<5分钟)", addr)
                    if frequent_removed:
                        current_hunters = list(self.storage.hunters.items())

                    # 合并 audit_removed 与 frequent_removed 供后续统计；audit 时动态踢人
                    for addr, info in current_hunters:
                        last_audit = info.get('last_audit', 0)

                        # 核心逻辑：超过 20 天才重新打分
                        if (now - last_audit) > AUDIT_EXPIRATION:
                            needs_audit_count += 1  # 进入体检分支即计数（含 LP 踢出、未达标踢出、更新）
                            logger.info(f"🩺 猎手 {addr} 超过{AUDIT_EXPIRATION // 86400}天未体检，正在重新审计...")

                            # 重新跑一遍分析
                            new_stats = await self.sm_searcher.analyze_hunter_performance(client, addr)
                            if new_stats and new_stats.get("_lp_detected"):
                                if addr in self.storage.hunters:
                                    del self.storage.hunters[addr]
                                    audit_removed.append(addr)
                                    await self._trigger_hunter_removed(addr)
                                if HUNTER_MODE == "MODELB":
                                    self.sm_searcher.add_to_trash(addr)
                                logger.info("🚫 体检踢出 %s.. (发现 LP 行为，已加入 trash)", addr[:12])
                            elif new_stats:
                                is_modelb = HUNTER_MODE == "MODELB"
                                if is_modelb:
                                    audit_pass, reasons = check_modelb_entry_criteria(new_stats)
                                    if not audit_pass:
                                        if addr in self.storage.hunters:
                                            del self.storage.hunters[addr]
                                            audit_removed.append(addr)
                                            await self._trigger_hunter_removed(addr)
                                        logger.info("🚫 体检踢出 %s.. (%s)", addr[:12], "/".join(reasons))
                                    else:
                                        _apply_audit_update(info, new_stats, now, addr)
                                else:
                                    pnl_min = SM_AUDIT_MIN_PNL_RATIO
                                    wr_min = SM_AUDIT_MIN_WIN_RATE
                                    roi_threshold = TIER_THREE_ROI
                                    pnl_ok = (new_stats.get("pnl_ratio", 0) or 0) >= pnl_min if new_stats.get("pnl_ratio") != float("inf") else True
                                    wr_ok = new_stats["win_rate"] >= wr_min
                                    profit_ok = new_stats["total_profit"] > 0
                                    roi_val = new_stats.get("max_roi_pct", 0) or new_stats.get("max_roi_30d", 0)

                                    if not (pnl_ok and wr_ok and profit_ok):
                                        if addr in self.storage.hunters:
                                            del self.storage.hunters[addr]
                                            audit_removed.append(addr)
                                            await self._trigger_hunter_removed(addr)
                                        logger.info("🚫 体检踢出 %s.. (盈亏比/胜率/利润未达标)", addr[:12])
                                    elif roi_val < roi_threshold:
                                        if addr in self.storage.hunters:
                                            del self.storage.hunters[addr]
                                            audit_removed.append(addr)
                                            await self._trigger_hunter_removed(addr)
                                        logger.info("🚫 体检踢出 %s.. (最大收益 %.0f%% < %s%%)", addr[:12], roi_val, roi_threshold)
                                    else:
                                        _apply_audit_update(info, new_stats, now, addr)

                            await asyncio.sleep(AUDIT_BETWEEN_HUNTERS_SLEEP_SEC)  # 慢慢跑，不着急

                if needs_audit_count == 0:
                    logger.info("✨ 所有猎手均在体检有效期内，无需更新")

                # 2. 清理僵尸 & 存盘 (每次维护都做一次清理，放线程池不阻塞监控)
                await asyncio.to_thread(self.storage.prune_and_update, [])
                logger.info("✅ 维护完成")

            except Exception:
                logger.exception("❌ 维护失败")

            # 每 10 天检查一次是否要体检
            logger.info(f"💤 维护线程休眠 {MAINTENANCE_DAYS} 天...")
            await asyncio.sleep(MAINTENANCE_INTERVAL)


if __name__ == "__main__":
    async def mock_cb(sig):
        logger.info("🔥 信号: %s", sig['token_address'])


    try:
        if os.name == 'nt':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(HunterMonitorController(mock_cb).start())
    except KeyboardInterrupt:
        logger.info("Monitor 被用户中断")
