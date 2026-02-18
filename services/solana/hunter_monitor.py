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
from websockets.exceptions import InvalidStatusCode

# 导入配置和依赖模块
from config.settings import helius_key_pool
from services.dexscreener.dex_scanner import DexScanner
from services.helius.sm_searcher import SmartMoneySearcher, TransactionParser
from utils.logger import get_logger

logger = get_logger(__name__)

# 常量配置
HUNTER_DATA_FILE = "data/hunters.json"
HUNTER_DATA_BACKUP = "data/hunters_backup.json"
DISCOVERY_INTERVAL = 900  # 挖掘间隔 15分钟
MAINTENANCE_INTERVAL = 86400  # 维护间隔 1天 (大幅降低频率)
POOL_SIZE_LIMIT = 50  # 地址库上限
ZOMBIE_THRESHOLD = 86400 * 10  # 10天不交易视为僵尸 (清理标准)
AUDIT_EXPIRATION = 86400 * 15  # 体检有效期 15天 (重算分数标准)


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

                async with websockets.connect(helius_key_pool.get_wss_url()) as ws:
                    payload = {
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": monitored_addrs}, {"commitment": "confirmed"}]
                    }
                    await ws.send(json.dumps(payload))
                    logger.info(f"✅ WebSocket 订阅 {len(monitored_addrs)} 地址")

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                            data = json.loads(msg)
                            if "params" in data:
                                await self.process_transaction_log(data["params"]["result"])
                        except asyncio.TimeoutError:
                            await ws.ping()
                            # 检查列表变更
                            if set(self.storage.get_monitored_addresses()) != set(monitored_addrs):
                                break

            except InvalidStatusCode as e:
                if e.status_code == 429:
                    helius_key_pool.mark_current_failed()
                    logger.warning("⚠️ Helius WebSocket 429 限流，已切换 Key，5 秒后重试")
                else:
                    logger.exception("⚠️ WS 连接被拒绝: HTTP %s", e.status_code)
                await asyncio.sleep(5)
            except Exception:
                logger.exception("⚠️ WS 重连异常")
                await asyncio.sleep(5)

    async def process_transaction_log(self, log_info):
        signature = log_info['value']['signature']
        try:
            from httpx import AsyncClient
            async with AsyncClient() as client:
                url = helius_key_pool.get_http_endpoint()
                resp = await client.post(url, json={"transactions": [signature]}, timeout=10)
                if resp.status_code == 429 and helius_key_pool.size > 1:
                    helius_key_pool.mark_current_failed()
                if resp.status_code != 200:
                    return
                txs = resp.json()
                if not txs: return
                tx = txs[0]

                tx_accounts = set()
                if 'accountData' in tx:
                    for acc in tx['accountData']:
                        tx_accounts.add(acc.get('account'))

                active_hunters = set(self.storage.get_monitored_addresses()).intersection(tx_accounts)

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
                logger.info(f"📥 买入: {hunter[:6]} -> {mint}")
            elif sol_change > 0 and delta < 0:  # SELL
                if hunter in self.active_holdings[mint]:
                    del self.active_holdings[mint][hunter]
                    logger.info(f"📤 卖出: {hunter[:6]} -> {mint}")

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
            logger.info(f"🚨 共振触发: {mint} (人数:{count}, 分:{total_score})")
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
