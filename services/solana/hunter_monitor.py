#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : hunter_monitor.py
@Description: 猎手监控核心模块 (Hunter Monitor V3 - 低功耗版)
              1. [线程A] 挖掘: 定时补充新猎手
              2. [线程B] 监控: 实时监听交易 + 更新活跃时间 + 触发共振
              3. [线程C] 维护: 每日巡检，仅对超过 15 天未体检的猎手重算分数
"""

import asyncio
import json
import os
import shutil
import time
from collections import defaultdict
from typing import Dict, List, Callable, Optional

import websockets

# 导入配置和依赖模块
from config.settings import helius_key_pool
from services.dexscreener.dex_scanner import DexScanner
from services.helius.sm_searcher import SmartMoneySearcher, TransactionParser, tx_has_real_trade
from utils.logger import get_logger

logger = get_logger(__name__)
# 猎手交易单独写入 monitor.log，便于查看时间与交易币种
trade_logger = get_logger("trade")

# 常量配置
HUNTER_DATA_FILE = "data/hunters.json"
HUNTER_DATA_BACKUP = "data/hunters_backup.json"
DISCOVERY_INTERVAL = 900  # 挖掘间隔 15分钟
MAINTENANCE_INTERVAL = 86400  # 维护间隔 1天 (大幅降低频率)
POOL_SIZE_LIMIT = 50  # 地址库上限
ZOMBIE_THRESHOLD = 86400 * 10  # 10天不交易视为僵尸 (清理标准)
AUDIT_EXPIRATION = 86400 * 15  # 体检有效期 15天 (重算分数标准)

# Helius 消耗控制：仅做去重，不限制监听数量
RECENT_SIG_TTL_SEC = 90  # 同一 signature 在此时间内不重复拉取（去重）
DISCOVERY_INTERVAL_WHEN_FULL_SEC = 43200  # 猎手池已满(50)时，挖掘间隔改为 12 小时

# 与 SmartFlow3 一致：拉取交易详情时重试（WebSocket 推送时 Helius 可能尚未索引）
FETCH_TX_MAX_RETRIES = 3
FETCH_TX_RETRY_DELAY_BASE = 2  # 第 i 次重试前等待 2+i 秒
# 使用 Helius transactionSubscribe（按账户包含），支持多地址；logsSubscribe 的 mentions 仅支持单地址且易漏 Swap
TRANSACTION_COMMITMENT = "processed"


class HunterStorage:
    """
    负责猎手数据的持久化存储与动态管理
    """

    def __init__(self):
        self.hunters: Dict[str, Dict] = {}  # {address: {score, last_active, last_audit...}}
        self.ensure_data_dir()
        self.load_hunters()

    def ensure_data_dir(self):
        if not os.path.exists("data"):
            os.makedirs("data")

    def load_hunters(self):
        if os.path.exists(HUNTER_DATA_FILE):
            try:
                with open(HUNTER_DATA_FILE, 'r', encoding='utf-8') as f:
                    self.hunters = json.load(f)
                logger.info(f"📂 已加载 {len(self.hunters)} 名猎手数据")
            except Exception:
                logger.exception("❌ 加载猎手数据失败")
                if os.path.exists(HUNTER_DATA_BACKUP):
                    shutil.copy(HUNTER_DATA_BACKUP, HUNTER_DATA_FILE)
                    self.load_hunters()

    def save_hunters(self):
        try:
            if os.path.exists(HUNTER_DATA_FILE):
                shutil.copy(HUNTER_DATA_FILE, HUNTER_DATA_BACKUP)
            with open(HUNTER_DATA_FILE, 'w', encoding='utf-8') as f:
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
        return self.hunters.get(address, {}).get('score', 0)

    def prune_and_update(self, new_hunters: List[Dict] = None):
        """
        库满时的优胜劣汰
        """
        now = time.time()

        # 1. 清理僵尸 (10天未交易)
        zombies = []
        for addr, info in self.hunters.items():
            last_active = info.get('last_active', 0)
            if last_active == 0: continue  # 刚入库的新人豁免

            if (now - last_active) > ZOMBIE_THRESHOLD:
                zombies.append(addr)

        for z in zombies:
            logger.info(f"💀 清理僵尸地址 (10天未动): {z[:6]}..")
            del self.hunters[z]

        # 2. 处理新猎手
        if new_hunters:
            for h in new_hunters:
                addr = h['address']
                h['last_active'] = h.get('last_active', now)
                h['last_audit'] = h.get('last_audit', now)  # 新人入库算作刚体检

                if addr in self.hunters:
                    # 如果已存在，更新信息，但保留原有的 last_audit (除非这次是强制更新)
                    old_audit = self.hunters[addr].get('last_audit', 0)
                    self.hunters[addr].update(h)
                    self.hunters[addr]['last_audit'] = old_audit
                    continue

                if len(self.hunters) < POOL_SIZE_LIMIT:
                    self.hunters[addr] = h
                    logger.info(f"🆕 新猎手入库: {addr[:6]} (分:{h['score']})")
                else:
                    # 库满 PK
                    sorted_hunters = sorted(self.hunters.items(), key=lambda x: x[1].get('score', 0))
                    lowest_addr, lowest_val = sorted_hunters[0]

                    if h['score'] > lowest_val.get('score', 0):
                        logger.info(f"♻️ 优胜劣汰: {h['score']}分 替换了 {lowest_val.get('score', 0)}分")
                        del self.hunters[lowest_addr]
                        self.hunters[addr] = h

        self.save_hunters()


class HunterMonitorController:
    def __init__(self, signal_callback: Optional[Callable] = None):
        self.storage = HunterStorage()
        self.dex_scanner = DexScanner()
        self.sm_searcher = SmartMoneySearcher()
        self.signal_callback = signal_callback

        # 实时持仓状态池
        self.active_holdings = defaultdict(dict)
        # Helius 消耗控制：仅去重
        self._recent_sigs: Dict[str, float] = {}  # signature -> 首次处理时间

    async def start(self):
        logger.info("🚀 启动 Hunter Monitor 系统 (V3 低功耗版)...")
        tasks = [
            asyncio.create_task(self.discovery_loop()),
            asyncio.create_task(self.realtime_monitor_loop()),
            asyncio.create_task(self.maintenance_loop())
        ]
        await asyncio.gather(*tasks)

    # --- 线程 1: 挖掘 ---
    async def discovery_loop(self):
        logger.info("🕵️ [线程1] 挖掘启动")
        while True:
            try:
                new_hunters = await self.sm_searcher.run_pipeline(self.dex_scanner)
                if new_hunters:
                    self.storage.prune_and_update(new_hunters)
            except Exception:
                logger.exception("❌ 挖掘异常")
            # 池满时降低挖掘频率，避免无意义消耗 credit
            if len(self.storage.hunters) >= POOL_SIZE_LIMIT:
                await asyncio.sleep(DISCOVERY_INTERVAL_WHEN_FULL_SEC)
            else:
                await asyncio.sleep(DISCOVERY_INTERVAL)

    # --- 线程 2: 监控 ---
    async def realtime_monitor_loop(self):
        logger.info("👀 [线程2] 监控启动")
        while True:
            try:
                monitored_addrs = self.storage.get_monitored_addresses()
                if not monitored_addrs:
                    await asyncio.sleep(10)
                    continue

                async with websockets.connect(
                    helius_key_pool.get_wss_url(),
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=None,
                    max_size=None,
                ) as ws:
                    # Helius transactionSubscribe：按 accountInclude 推送，任意猎手参与的交易都会推（支持多地址）
                    # logsSubscribe 的 mentions 仅支持单地址且 Swap 常不把地址写进日志，会漏单
                    payload = {
                        "jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
                        "params": [
                            {"accountInclude": monitored_addrs},
                            {
                                "commitment": TRANSACTION_COMMITMENT,
                                "encoding": "jsonParsed",
                                "transactionDetails": "signatures",
                                "maxSupportedTransactionVersion": 0,
                            }
                        ]
                    }
                    await ws.send(json.dumps(payload))
                    logger.info(f"📤 已发送 transactionSubscribe ({len(monitored_addrs)} 地址)，进入接收循环")
                    sub_was_unconfirmed = True
                    idle_60s_count = 0

                    # 主循环：处理 transactionNotification（Helius 按账户推送）
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                            data = json.loads(msg)
                            if data.get("method") != "transactionNotification":
                                logger.info("收到 WebSocket 消息: method=%s id=%s", data.get("method"), data.get("id"))
                                continue
                            idle_60s_count = 0
                            res = data.get("params") or {}
                            result = res.get("result") or {}
                            sig = result.get("signature")
                            if not sig:
                                logger.warning("transactionNotification 缺少 signature")
                                continue
                            if sub_was_unconfirmed:
                                logger.info("✅ 订阅已正常，已收到交易推送")
                                sub_was_unconfirmed = False
                            logger.info("收到交易推送: %s..", sig[:20])
                            # 复用原有处理：只传 signature 结构，后续会拉 Helius 解析后的详情
                            await self.process_transaction_log({"value": {"signature": sig}})
                        except asyncio.TimeoutError:
                            await ws.ping()
                            idle_60s_count += 1
                            # 每 10 分钟打一条存活日志，便于区分「程序在等」和「程序挂了」
                            if idle_60s_count >= 10:
                                logger.info("监控运行中 | 已 %d 分钟无新推送（猎手有交易时会有日志）", idle_60s_count)
                                idle_60s_count = 0
                            # 检查列表变更
                            if set(self.storage.get_monitored_addresses()) != set(monitored_addrs):
                                break

            except Exception as e:
                status_code = getattr(e, "status_code", None)
                is_429 = status_code == 429 or "429" in str(e).lower()
                if is_429:
                    helius_key_pool.mark_current_failed()
                    logger.warning("⚠️ Helius WebSocket 429 限流，已切换 Key，5 秒后重试")
                else:
                    logger.exception("⚠️ WS 重连异常")
                await asyncio.sleep(5)

    async def process_transaction_log(self, log_info):
        """处理单条 logsNotification 的 result，与 SmartFlow3 结构一致：params.result.value.signature。"""
        value = log_info.get("value") or {}
        signature = value.get("signature")
        if not signature:
            logger.warning("process_transaction_log 缺少 value.signature: %s", str(log_info)[:200])
            return
        now = time.time()

        # 去重：同一 signature 在 TTL 内只拉一次，避免重复扣 credit
        if signature in self._recent_sigs and (now - self._recent_sigs[signature]) < RECENT_SIG_TTL_SEC:
            return
        self._recent_sigs[signature] = now
        for sig in list(self._recent_sigs.keys()):
            if now - self._recent_sigs[sig] > RECENT_SIG_TTL_SEC * 2:
                del self._recent_sigs[sig]

        try:
            from httpx import AsyncClient
            payload = {"transactions": [signature]}
            tx = None
            async with AsyncClient(timeout=10.0) as client:
                for attempt in range(FETCH_TX_MAX_RETRIES):
                    url = helius_key_pool.get_http_endpoint()
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 429 and helius_key_pool.size >= 1:
                        helius_key_pool.mark_current_failed()
                    if resp.status_code != 200:
                        if attempt < FETCH_TX_MAX_RETRIES - 1:
                            await asyncio.sleep(FETCH_TX_RETRY_DELAY_BASE + attempt)
                        continue
                    txs = resp.json()
                    if txs and len(txs) > 0:
                        tx = txs[0]
                        break
                    # Helius 可能尚未索引，与 SmartFlow3 一致：重试 + 退避
                    if attempt < FETCH_TX_MAX_RETRIES - 1:
                        logger.debug("交易 %s.. 尚未索引，%d 秒后重试", signature[:16], FETCH_TX_RETRY_DELAY_BASE + attempt)
                        await asyncio.sleep(FETCH_TX_RETRY_DELAY_BASE + attempt)
            if not tx:
                logger.warning("拉取交易详情失败（已重试 %d 次）: %s..", FETCH_TX_MAX_RETRIES, signature[:16])
                return

            # 与 SmartFlow3 一致：非真实交易（无 token 买卖 / 无 meaningful native）直接跳过，不参与统计
            if not tx_has_real_trade(tx):
                logger.debug("本笔非真实交易，跳过: %s..", signature[:16])
                return

            # 从交易中收集参与账户：Helius 可能无 accountData，用 feePayer + 各类 transfer 的 from/to
            tx_accounts = set()
            fp = tx.get("feePayer") or tx.get("fee_payer")
            if fp:
                tx_accounts.add(fp)
            for nt in tx.get("nativeTransfers", []):
                for key in ("fromUserAccount", "toUserAccount"):
                    a = nt.get(key)
                    if a:
                        tx_accounts.add(a)
            for tt in tx.get("tokenTransfers", []):
                for key in ("fromUserAccount", "toUserAccount"):
                    a = tt.get(key)
                    if a:
                        tx_accounts.add(a)
            if "accountData" in tx:
                for acc in tx["accountData"]:
                    a = acc.get("account")
                    if a:
                        tx_accounts.add(a)

            active_hunters = set(self.storage.get_monitored_addresses()).intersection(tx_accounts)
            if not active_hunters:
                logger.debug("本笔无监控猎手参与，跳过: %s..", signature[:16])
                return
            logger.info("本笔涉及 %d 名猎手: %s", len(active_hunters), [h[:8] for h in list(active_hunters)[:5]])

            for hunter in active_hunters:
                self.storage.update_last_active(hunter, time.time())
                await self.analyze_action(hunter, tx)
        except Exception:
            logger.exception("process_transaction_log 异常")

    async def analyze_action(self, hunter, tx):
        parser = TransactionParser(hunter)
        sol_change, token_changes, ts = parser.parse_transaction(tx)

        for mint, delta in token_changes.items():
            if abs(delta) < 1e-9: continue

            if sol_change < 0 and delta > 0:  # BUY
                self.active_holdings[mint][hunter] = time.time()
                trade_logger.info(f"📥 买入: {hunter[:6]} -> {mint}")
            elif sol_change > 0 and delta < 0:  # SELL
                if hunter in self.active_holdings[mint]:
                    del self.active_holdings[mint][hunter]
                    trade_logger.info(f"📤 卖出: {hunter[:6]} -> {mint}")

            await self.check_resonance(mint)

    async def check_resonance(self, mint):
        holders = self.active_holdings[mint]
        if not holders: return
        addrs = list(holders.keys())
        scores = [self.storage.get_hunter_score(a) for a in addrs]
        count = len(addrs)
        total_score = sum(scores)

        c1 = count >= 3
        c2 = count >= 2 and any(s >= 90 for s in scores)
        c3 = count >= 2 and total_score >= 160

        if c1 or c2 or c3:
            trade_logger.info(f"🚨 共振触发: {mint} (人数:{count}, 分:{total_score})")
            if self.signal_callback:
                signal = {
                    "token_address": mint,
                    "hunters": [self.storage.hunters[a] for a in addrs],
                    "total_score": total_score,
                    "timestamp": time.time()
                }
                if asyncio.iscoroutinefunction(self.signal_callback):
                    await self.signal_callback(signal)
                else:
                    self.signal_callback(signal)

    # --- 线程 3: 维护 (Maintenance - 优化版) ---
    async def maintenance_loop(self):
        """
        [优化] 每日巡检 + 15天体检逻辑
        """
        logger.info("🛠️ [线程3] 维护线程启动 (每日运行)")

        # 启动时先睡一会，错开高峰，或者直接运行一次也行
        # 这里选择立即运行第一次，然后按天循环

        while True:
            try:
                logger.info("🏥 开始每日例行维护...")
                now = time.time()

                # 1. 遍历检查是否需要体检
                current_hunters = list(self.storage.hunters.items())
                needs_audit_count = 0

                from httpx import AsyncClient
                async with AsyncClient() as client:
                    # 0. 频繁交易剔除：最近 100 笔平均间隔 < 5 分钟的踢出猎手池
                    frequent_removed = []
                    for addr, _ in current_hunters:
                        if await self.sm_searcher.is_frequent_trader(client, addr):
                            frequent_removed.append(addr)
                    for addr in frequent_removed:
                        if addr in self.storage.hunters:
                            del self.storage.hunters[addr]
                            logger.info("🚫 踢出频繁交易猎手 %s.. (平均间隔<5分钟)", addr[:8])
                    if frequent_removed:
                        current_hunters = list(self.storage.hunters.items())

                    for addr, info in current_hunters:
                        last_audit = info.get('last_audit', 0)

                        # 核心逻辑：超过 15 天才重新打分
                        if (now - last_audit) > AUDIT_EXPIRATION:
                            logger.info(f"🩺 猎手 {addr[:6]} 超过15天未体检，正在重新审计...")

                            # 重新跑一遍分析
                            new_stats = await self.sm_searcher.analyze_hunter_performance(client, addr)
                            if new_stats:
                                # 更新核心数据
                                info['total_profit'] = f"{new_stats['total_profit']:.2f} SOL"
                                info['win_rate'] = f"{new_stats['win_rate']:.1%}"
                                info['last_audit'] = now  # 更新体检时间戳

                                # 惩罚机制：如果以前很牛，现在亏钱了，分数归零等待淘汰
                                if new_stats['total_profit'] < 0:
                                    info['score'] = 0
                                    logger.warning(f"📉 猎手 {addr[:6]} 表现恶化 (负盈利)，分数归零")
                                else:
                                    logger.info(f"✅ 猎手 {addr[:6]} 体检完成，状态良好")

                            needs_audit_count += 1
                            await asyncio.sleep(2)  # 慢慢跑，不着急

                if needs_audit_count == 0:
                    logger.info("✨ 所有猎手均在体检有效期内，无需更新")

                # 2. 清理僵尸 & 存盘 (每次维护都做一次清理)
                self.storage.prune_and_update([])
                logger.info("✅ 维护完成")

            except Exception:
                logger.exception("❌ 维护失败")

            # 每天睡一次
            logger.info(f"💤 维护线程休眠 1 天...")
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
