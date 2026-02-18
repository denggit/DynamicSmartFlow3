#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : hunter_agent.py
@Description: 猎手行动监控 (Hunter Agent) - 负责"持仓后"的跟单管理
              1. 任务管理: 接收主程序的监控任务 (Token + Hunters)
              2. 状态追踪: 实时维护猎手在该 Token 上的持仓数量
              3. 信号触发:
                 - 加仓信号 (Buy Dip)
                 - 止盈/止损信号 (Sell Ratio)
              4. 动态扩容: 支持中途加入新猎手 (15分钟内)
"""

import asyncio
import json
import time
from collections import defaultdict
from typing import Dict, List, Callable, Optional

import httpx
import websockets

from config.settings import helius_key_pool
from services.helius.sm_searcher import IGNORE_MINTS, TransactionParser
from utils.logger import get_logger

logger = get_logger(__name__)
# 猎手交易单独写入 monitor.log，便于查看时间与交易币种
trade_logger = get_logger("trade")

# 与 SmartFlow3 一致：定时同步持仓，防止漏订阅导致错过猎手卖出
SYNC_POSITIONS_INTERVAL_SEC = 30  # 每 30 秒拉一次链上余额做兜底
SYNC_MIN_DELTA_RATIO = 0.01  # 变化比例小于 1% 视为误差，不触发信号
SYNC_PROTECTION_AFTER_START_SEC = 60  # 任务启动后 60 秒内不同步，避免链上延迟误判


class TokenMission:
    """
    单个代币的监控任务
    """

    def __init__(self, token_address: str, creation_time: float):
        self.token_address = token_address
        self.creation_time = creation_time
        # 猎手持仓状态: {hunter_address: current_token_balance}
        self.hunter_states: Dict[str, float] = {}
        # 猎手初始成本(可选，用于算盈亏): {hunter_address: initial_sol_cost}
        self.hunter_costs: Dict[str, float] = {}

        self.is_active = True
        self.start_time = time.time()

    def add_hunter(self, hunter_address: str, initial_balance: float = 0.0):
        """添加或更新猎手"""
        # 如果是新加的，记录初始状态
        if hunter_address not in self.hunter_states:
            self.hunter_states[hunter_address] = initial_balance
            logger.info(
                f"➕ [任务 {self.token_address[:6]}] 新增监控猎手: {hunter_address} (初始持仓: {initial_balance:.2f})")

    def update_balance(self, hunter_address: str, delta_amount: float):
        """更新余额并返回 (旧余额, 新余额)"""
        if hunter_address not in self.hunter_states:
            self.hunter_states[hunter_address] = 0.0

        old_bal = self.hunter_states[hunter_address]
        new_bal = max(0, old_bal + delta_amount)  # 防止负数
        self.hunter_states[hunter_address] = new_bal
        return old_bal, new_bal


class HunterAgentController:
    """
    总控制器：管理所有 Token 的监控任务
    """

    def __init__(self, signal_callback: Optional[Callable] = None):
        self.signal_callback = signal_callback

        # 活跃任务池: {token_address: TokenMission}
        self.active_missions: Dict[str, TokenMission] = {}

        # 地址反向索引: {hunter_address: Set[token_address]}
        # 用于 WebSocket 收到消息时快速找到是哪个 Token 的任务
        self.hunter_map = defaultdict(set)

    async def start(self):
        """启动 Agent：只跑持仓同步兜底；交易信号由 Monitor 统一推送，避免自建 WS 漏单。"""
        logger.info("🕵️‍♂️ 启动 Hunter Agent (跟单管家，信号来自 Monitor)...")
        await self.sync_positions_loop()

    async def on_tx_from_monitor(self, tx: dict, active_hunters: set):
        """
        Monitor 消费队列命中钱包池后推送：同一笔 tx + 命中的猎手集合。
        用 Helius 格式解析 token 变动，只处理 Agent 正在跟仓的 (hunter, token)，发 HUNTER_SELL/HUNTER_BUY。
        """
        for hunter in active_hunters:
            if hunter not in self.hunter_map:
                continue
            potential_tokens = self.hunter_map[hunter]
            if not potential_tokens:
                continue
            parser = TransactionParser(hunter)
            _, token_changes, _ = parser.parse_transaction(tx)
            token_changes = {m: d for m, d in token_changes.items() if m not in IGNORE_MINTS and abs(d) >= 1e-9}
            for mint, delta in token_changes.items():
                if mint not in potential_tokens:
                    continue
                try:
                    await self.analyze_action(hunter, mint, delta, None, time.time())
                except Exception:
                    logger.exception("on_tx_from_monitor analyze_action 异常 %s %s", hunter[:6], mint[:6])

    # === 1. 任务管理接口 (供主程序调用) ===

    async def start_tracking(self, token_address: str, hunters: List[str], creation_time: float = 0):
        """
        [指令] 开始监控一个新币
        """
        if token_address in self.active_missions:
            logger.warning(f"⚠️ 任务已存在: {token_address}")
            return

        logger.info(f"🆕 收到监控指令: {token_address} | 初始猎手: {len(hunters)} 人")

        mission = TokenMission(token_address, creation_time or time.time())
        self.active_missions[token_address] = mission

        # 1. 立即获取这些猎手当前的持仓 (Snapshot)
        # 这是一个关键步骤，因为猎手可能在我们介入前已经买入了多次
        for hunter in hunters:
            balance = await self._fetch_token_balance(hunter, token_address)
            mission.add_hunter(hunter, balance)
            self.hunter_map[hunter].add(token_address)

        # 这里会触发 WebSocket 重连以更新订阅列表
        # (在 monitor_loop 里会自动处理)

    async def add_hunter_to_mission(self, token_address: str, new_hunter: str):
        """
        [指令] 动态加人 (当 Token 还在15分钟内，有新大佬进场时)
        """
        mission = self.active_missions.get(token_address)
        if not mission: return

        # 检查是否还在 黄金观察窗 (例如 15分钟)
        # 如果 token 已经很老了，加人意义不大，但这由主程序判断

        if new_hunter not in mission.hunter_states:
            balance = await self._fetch_token_balance(new_hunter, token_address)
            mission.add_hunter(new_hunter, balance)
            self.hunter_map[new_hunter].add(token_address)

    async def stop_tracking(self, token_address: str):
        """
        [指令] 停止监控 (当我们清仓后)
        """
        if token_address in self.active_missions:
            logger.info(f"🛑 停止监控任务: {token_address}")
            mission = self.active_missions.pop(token_address)

            # 清理索引
            for hunter in mission.hunter_states:
                if token_address in self.hunter_map[hunter]:
                    self.hunter_map[hunter].remove(token_address)
                    if not self.hunter_map[hunter]:
                        del self.hunter_map[hunter]

    async def sync_positions_loop(self):
        """
        定时拉取猎手链上持仓，与本地状态对比；若发现已卖出但我们未收到订阅，补发 HUNTER_SELL。
        与 SmartFlow3 的 monitor_sync_positions 思路一致，防止漏订阅错过跟卖。
        """
        logger.info("🛡️ 持仓同步防漏单线程已启动 (每 %s 秒检查一次)...", SYNC_POSITIONS_INTERVAL_SEC)
        while True:
            try:
                await asyncio.sleep(SYNC_POSITIONS_INTERVAL_SEC)
                missions = list(self.active_missions.items())
                if not missions:
                    continue

                now = time.time()
                for token_address, mission in missions:
                    if (now - mission.start_time) < SYNC_PROTECTION_AFTER_START_SEC:
                        continue
                    for hunter in list(mission.hunter_states.keys()):
                        try:
                            real_balance = await self._fetch_token_balance(hunter, token_address)
                            if real_balance is None:
                                continue
                            old_bal = mission.hunter_states[hunter]
                            delta = real_balance - old_bal
                            if abs(delta) < 1e-9:
                                continue
                            # 发现减仓（可能漏了订阅）
                            if delta < 0 and abs(delta) >= old_bal * SYNC_MIN_DELTA_RATIO:
                                mission.hunter_states[hunter] = max(0.0, real_balance)
                                sell_amount = abs(delta)
                                ratio = (sell_amount / old_bal) if old_bal > 0 else 1.0
                                new_bal = mission.hunter_states[hunter]
                                trade_logger.info(
                                    f"📉 [Agent 同步] 猎手 {hunter} 卖出 {token_address[:6]} | "
                                    f"数量: {sell_amount:.2f} | 比例: {ratio:.1%} (剩 {new_bal:.2f}) [漏订阅兜底]"
                                )
                                if self.signal_callback:
                                    signal = {
                                        "type": "HUNTER_SELL",
                                        "token": token_address,
                                        "hunter": hunter,
                                        "sell_ratio": ratio,
                                        "remaining_balance": new_bal,
                                        "timestamp": now,
                                    }
                                    await self._trigger_callback(signal)
                            elif delta > 0:
                                mission.hunter_states[hunter] = real_balance
                        except Exception:
                            logger.debug("同步单猎手余额异常", exc_info=True)
                        await asyncio.sleep(0.3)
            except Exception:
                logger.exception("sync_positions_loop 异常")

    # === 2. 核心监控逻辑 ===

    async def monitor_loop(self):
        """WebSocket 监听循环"""
        while True:
            try:
                # 获取所有需要监听的猎手地址
                monitored_hunters = list(self.hunter_map.keys())

                if not monitored_hunters:
                    await asyncio.sleep(5)
                    continue

                async with websockets.connect(helius_key_pool.get_wss_url()) as ws:
                    logger.info(f"👀 Agent 已连接，正在监视 {len(monitored_hunters)} 名猎手的持仓变动...")

                    # 订阅 logs
                    payload = {
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": monitored_hunters}, {"commitment": "confirmed"}]
                    }
                    await ws.send(json.dumps(payload))

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                            data = json.loads(msg)

                            if "params" in data:
                                await self.process_log(data["params"]["result"])

                        except asyncio.TimeoutError:
                            await ws.ping()
                            # 检查是否有新任务加入 (通过对比订阅列表长度)
                            current_hunters = list(self.hunter_map.keys())
                            if len(current_hunters) != len(monitored_hunters):
                                logger.info("🔄 监控列表变动，重启 WebSocket...")
                                break

            except Exception as e:
                status_code = getattr(e, "status_code", None)
                is_429 = status_code == 429 or "429" in str(e).lower()
                if is_429:
                    helius_key_pool.mark_current_failed()
                    logger.warning("⚠️ Helius WebSocket 429 限流，已切换 Key，5 秒后重试")
                else:
                    logger.exception("❌ Agent 监控异常，5秒后重试")
                await asyncio.sleep(5)

    async def process_log(self, log_info):
        """处理链上日志"""
        signature = log_info['value']['signature']

        # 1. 快速过滤: 这笔交易是否涉及我们关心的猎手？
        # (Helius mentions 已经做了一层，但这里我们需要知道具体是哪个猎手)
        # 为了准确，我们必须拉取交易详情

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    helius_key_pool.get_rpc_url(),
                    json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                          "params": [signature, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}]},
                    timeout=10
                )
                if resp.status_code == 429 and helius_key_pool.size > 1:
                    helius_key_pool.mark_current_failed()
                if resp.status_code != 200:
                    return
                data = resp.json()
                if "result" not in data or not data["result"]: return
                tx = data["result"]

                # 2. 解析交易：找出参与的猎手，并只处理非 IGNORE 代币的变动（真实交易）
                # 获取交易涉及的所有账号
                account_keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
                involved_hunters = set(account_keys).intersection(self.hunter_map.keys())

                if not involved_hunters: return

                # 3. 对每个涉及的猎手进行分析
                # 注意：这里需要把 tx 转换成 TransactionParser 能懂的格式 (Helius API vs RPC 格式略有不同)
                # 为了复用 sm_searcher 的 parser，我们最好做适配
                # 这里简单处理，提取 timestamp
                block_time = tx.get("blockTime", time.time())

                for hunter in involved_hunters:
                    potential_tokens = self.hunter_map[hunter]
                    token_changes = self._calculate_balance_changes(tx, hunter)
                    # 与 SmartFlow3 一致：只把非 SOL/USDC/USDT 的变动当作真实交易，忽略 IGNORE_MINTS
                    token_changes = {m: d for m, d in token_changes.items() if m not in IGNORE_MINTS}
                    if not token_changes:
                        continue

                    for token_addr, delta in token_changes.items():
                        # 只处理我们在监控的 Token
                        if token_addr in potential_tokens:
                            await self.analyze_action(hunter, token_addr, delta, tx, block_time)

        except Exception:
            logger.exception("日志处理失败")

    def _calculate_balance_changes(self, tx_data, hunter_address):
        """从 RPC 格式的交易中计算 Token 余额变化"""
        changes = defaultdict(float)
        meta = tx_data["meta"]
        if not meta: return changes

        # 建立索引: AccountIndex -> Mint
        # 需要遍历 preTokenBalances 和 postTokenBalances

        pre_balances = {}  # {mint: amount}
        post_balances = {}

        for bal in meta.get("preTokenBalances", []):
            if bal["owner"] == hunter_address:
                pre_balances[bal["mint"]] = float(
                    bal["uiTokenAmount"]["amount"])  # 使用 raw amount (整数) 避免精度问题? 不，用 float 吧，方便

        for bal in meta.get("postTokenBalances", []):
            if bal["owner"] == hunter_address:
                post_balances[bal["mint"]] = float(bal["uiTokenAmount"]["amount"])

        # 计算差值
        all_mints = set(pre_balances.keys()).union(post_balances.keys())
        for mint in all_mints:
            pre = pre_balances.get(mint, 0)
            post = post_balances.get(mint, 0)
            delta = post - pre
            if abs(delta) > 0:
                changes[mint] = delta

        return changes

    async def analyze_action(self, hunter, token, delta, tx, timestamp):
        """核心：分析行为并生成信号"""
        mission = self.active_missions.get(token)
        if not mission: return

        # 更新本地状态
        old_bal, new_bal = mission.update_balance(hunter, delta)

        # 获取 SOL 的变化 (判断是买还是卖，还是转账)
        # 简单判定：
        # delta > 0: 加仓
        # delta < 0: 减仓

        # 1. 卖出信号 (Sell Signal)
        if delta < 0:
            sell_amount = abs(delta)
            # 计算卖出比例
            # 注意：分母应该是 old_bal
            if old_bal > 0:
                ratio = sell_amount / old_bal
            else:
                ratio = 1.0  # 异常情况，视为全卖

            trade_logger.info(
                f"📉 [Agent] 猎手 {hunter} 卖出 {token[:6]} | 数量: {sell_amount:.2f} | 比例: {ratio:.1%} (剩 {new_bal:.2f})")

            # 触发回调
            if self.signal_callback:
                signal = {
                    "type": "HUNTER_SELL",
                    "token": token,
                    "hunter": hunter,
                    "sell_ratio": ratio,
                    "remaining_balance": new_bal,
                    "timestamp": timestamp
                }
                await self._trigger_callback(signal)

        # 2. 买入信号 (Buy/Add Signal)
        elif delta > 0:
            # 估算买入金额 (SOL)
            # 需要解析 nativeSol 变化，这里简化处理，只通知仓位增加

            # 计算加仓比例 (相对于之前的持仓)
            if old_bal > 0:
                increase_ratio = delta / old_bal
            else:
                increase_ratio = 1.0  # 建仓

            trade_logger.info(
                f"📈 [Agent] 猎手 {hunter} 加仓 {token[:6]} | 数量: +{delta:.2f} | 增幅: {increase_ratio:.1%}")

            if self.signal_callback:
                signal = {
                    "type": "HUNTER_BUY",
                    "token": token,
                    "hunter": hunter,
                    "add_amount_raw": delta,
                    "new_balance": new_bal,
                    "timestamp": timestamp
                }
                await self._trigger_callback(signal)

    async def _trigger_callback(self, signal):
        if asyncio.iscoroutinefunction(self.signal_callback):
            await self.signal_callback(signal)
        else:
            self.signal_callback(signal)

    async def _fetch_token_balance(self, hunter, token_mint):
        """RPC 辅助：获取猎手当前的 Token 余额"""
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        hunter,
                        {"mint": token_mint},
                        {"encoding": "jsonParsed"}
                    ]
                }
                resp = await client.post(helius_key_pool.get_rpc_url(), json=payload, timeout=5)
                if resp.status_code == 429 and helius_key_pool.size > 1:
                    helius_key_pool.mark_current_failed()
                data = resp.json()

                if "result" in data and data["result"]["value"]:
                    # 可能有多个账户，取总和
                    total = 0.0
                    for acc in data["result"]["value"]:
                        info = acc["account"]["data"]["parsed"]["info"]
                        total += float(info["tokenAmount"]["amount"])  # 使用 raw amount 吗？还是 uiAmount?
                        # 这里为了和上面的 calculate_balance_changes 一致，最好用 raw amount
                        # 但 RPC 返回的是 uiAmount...
                        # 修正：calculate_balance_changes 里我们用的是 uiTokenAmount['amount'] (即 raw)
                        # 所以这里也取 amount
                    return total
                return 0.0
        except Exception:
            logger.exception("获取余额失败")
            return 0.0
