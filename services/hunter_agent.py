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
from typing import Awaitable, Dict, List, Callable, Optional

import websockets

from config.settings import (
    SYNC_POSITIONS_INTERVAL_SEC,
    SYNC_MIN_DELTA_RATIO,
    SYNC_PROTECTION_AFTER_START_SEC,
    NEW_HUNTER_ADD_WINDOW_SEC,
    USDC_PER_SOL,
    AGENT_WS_RECV_TIMEOUT,
    AGENT_GET_TX_TIMEOUT,
    AGENT_TOKEN_ACCOUNTS_TIMEOUT,
    AGENT_WS_ERROR_SLEEP_SEC,
    AGENT_POLL_SLEEP_SEC,
    ALCHEMY_MIN_INTERVAL_SEC,
    IGNORE_MINTS,
)
from src.alchemy import alchemy_client
from services.hunter_common import TransactionParser
from services.hunter_common.shared import _get_tx_timestamp
from utils.logger import get_logger

logger = get_logger(__name__)
# 猎手交易单独写入 monitor.log，便于查看时间与交易币种
trade_logger = get_logger("trade")

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
        # 猎手持仓归零跳过监控时触发（主程序需校验我方链上是否归零，未归零则清仓）
        self.on_hunter_zero_skip: Optional[Callable[..., Awaitable[None]]] = None
        # 活跃任务池: {token_address: TokenMission}
        self.active_missions: Dict[str, TokenMission] = {}

        # 地址反向索引: {hunter_address: Set[token_address]}
        # 用于 WebSocket 收到消息时快速找到是哪个 Token 的任务
        self.hunter_map = defaultdict(set)

        # 新增猎手加仓节流：1 分钟内同一 token 只发一次 HUNTER_BUY，避免多人同时入场重复跟仓
        self._last_new_hunter_signal_at: Dict[str, float] = {}

    async def start(self):
        """启动 Agent：只跑持仓同步兜底；交易信号由 Monitor 统一推送，避免自建 WS 漏单。"""
        logger.info("🕵️‍♂️ 启动 Hunter Agent (跟单管家，信号来自 Monitor)...")
        await self.sync_positions_loop()

    async def on_tx_from_monitor(self, tx: dict, active_hunters: set):
        """
        Monitor 消费队列命中钱包池后推送：同一笔 tx + 命中的猎手集合。
        用 Helius 解析格式解析 token 变动（Monitor 推送的 tx 为 Helius 格式）：
        1. 已跟仓的 (hunter, token)：发 HUNTER_SELL / HUNTER_BUY
        2. 新增猎手：池内猎手买入我们正在持有的 token 时，加入任务并发 HUNTER_BUY 触发加仓

        重要：必须使用 tx 的真实 blockTime，否则队列延迟会导致「基线前交易」被误判为加仓。
        例如猎手 13:32 买入触发共振，我们 13:33 才完成开仓并 start_tracking；
        若该 tx 在队列中延迟到 13:33 才被消费，若用 time.time() 会误以为「刚加仓」。
        """
        tx_time = _get_tx_timestamp(tx) or time.time()
        parser_cache = {}
        usdc_price_sol = 1.0 / USDC_PER_SOL if USDC_PER_SOL > 0 else 0.01
        for hunter in active_hunters:
            parser = parser_cache.get(hunter)
            if parser is None:
                parser = TransactionParser(hunter)
                parser_cache[hunter] = parser
            _, token_changes, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price_sol)
            token_changes = {m: d for m, d in token_changes.items() if m not in IGNORE_MINTS and abs(d) >= 1e-9}

            potential_tokens = self.hunter_map.get(hunter) or set()
            for mint, delta in token_changes.items():
                if mint in potential_tokens:
                    try:
                        await self.analyze_action(hunter, mint, delta, tx, tx_time)
                    except Exception:
                        logger.exception("on_tx_from_monitor analyze_action 异常 %s %s", hunter[:6], mint[:6])
                elif delta > 0:
                    # 单猎手模式：不再添加新猎手，只跟开仓时的那个猎手
                    mission = self.active_missions.get(mint)
                    if mission and hunter not in mission.hunter_states:
                        trade_logger.debug("单猎手模式: 新猎手 %s 买入不跟", hunter[:8])

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

        # 1. 【关键】先建立 hunter_map 索引，再拉链上余额。
        #    否则：_fetch_token_balance 耗时期间，Monitor 消费队列可能已把「猎手卖出」tx 推给 on_tx_from_monitor，
        #    此时 hunter_map 尚空，会漏跟卖。先登记后拉余额可避免该竞态。
        for hunter in hunters:
            self.hunter_map[hunter].add(token_address)
        # 2. 获取各猎手当前持仓快照
        for hunter in hunters:
            balance = await self._fetch_token_balance(hunter, token_address)
            mission.add_hunter(hunter, balance)

        # 3. 若所有猎手持仓均已归零，无需监控（猎手已卖光，无跟卖信号）
        if all(mission.hunter_states.get(h, 0) <= 0 for h in hunters):
            self.active_missions.pop(token_address, None)
            for hunter in hunters:
                if token_address in self.hunter_map.get(hunter, set()):
                    self.hunter_map[hunter].discard(token_address)
                    if not self.hunter_map[hunter]:
                        del self.hunter_map[hunter]
            logger.info(
                "⏭️ 猎手持仓已归零，跳过监控: %s | 猎手: %s",
                token_address[:12] + "..",
                ", ".join(h[:8] + ".." for h in hunters[:3]),
            )
            if self.on_hunter_zero_skip:
                try:
                    if asyncio.iscoroutinefunction(self.on_hunter_zero_skip):
                        await self.on_hunter_zero_skip(token_address)
                    else:
                        self.on_hunter_zero_skip(token_address)
                except Exception:
                    logger.exception("on_hunter_zero_skip 回调异常")
            return

        # 这里会触发 WebSocket 重连以更新订阅列表
        # (在 monitor_loop 里会自动处理)

    async def _handle_new_hunter_join(self, hunter: str, token_address: str, delta_ui: float):
        """
        新增猎手入场：池内猎手买入我们持有的 token 时，加入任务并触发 HUNTER_BUY。
        main 收到信号后加仓 0.1 SOL 并调用 add_hunter_to_mission（幂等）。
        节流：1 分钟内同一 token 多名新猎手加入时，只发一次 HUNTER_BUY，避免重复跟仓。
        窗口：开仓 10 分钟后加入的新猎手既不加入监控也不跟卖，直接忽略。
        """
        mission = self.active_missions.get(token_address)
        if not mission or hunter in mission.hunter_states:
            return

        now = time.time()
        if now - mission.creation_time > NEW_HUNTER_ADD_WINDOW_SEC:
            trade_logger.info("🔄 [Agent] 开仓已超 10 分钟，新增猎手 %s 不加入监控", hunter[:8])
            return

        balance = await self._fetch_token_balance(hunter, token_address)
        mission.add_hunter(hunter, balance)
        self.hunter_map[hunter].add(token_address)
        trade_logger.info(f"🆕 [Agent] 新增猎手入场 {hunter[:6]} -> {token_address[:6]} | 买入: {delta_ui:.2f}")

        last_at = self._last_new_hunter_signal_at.get(token_address, 0)
        if now - last_at < 60:
            trade_logger.info("🔄 [Agent] 1 分钟内已有新猎手加仓信号，本次仅加入监控不重复跟仓")
            return

        if self.signal_callback:
            self._last_new_hunter_signal_at[token_address] = now
            signal = {
                "type": "HUNTER_BUY",
                "token": token_address,
                "hunter": hunter,
                "add_amount_ui": delta_ui,
                "new_balance": balance,
                "timestamp": now,
                "is_new_hunter": True,
            }
            await self._trigger_callback(signal)

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
            self._last_new_hunter_signal_at.pop(token_address, None)

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
                            # 发现减仓（可能漏了订阅）。注意：old_bal 须正确，若 baseline 被基线前 tx 污染会误判
                            # 二次确认：间隔 1 秒再拉一次，若仍为 real_balance 才触发，避免 RPC 缓存/抖动误判
                            if delta < 0 and abs(delta) >= old_bal * SYNC_MIN_DELTA_RATIO:
                                await asyncio.sleep(1.0)
                                real_balance_2 = await self._fetch_token_balance(hunter, token_address)
                                if real_balance_2 is not None and abs(real_balance_2 - real_balance) < old_bal * 0.02:
                                    # 两次结果一致（误差 < 2%），视为真实减仓
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
                        await asyncio.sleep(max(AGENT_POLL_SLEEP_SEC, ALCHEMY_MIN_INTERVAL_SEC))
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
                    await asyncio.sleep(AGENT_WS_ERROR_SLEEP_SEC)
                    continue

                async with websockets.connect(alchemy_client.get_wss_url()) as ws:
                    logger.info(f"👀 Agent 已连接，正在监视 {len(monitored_hunters)} 名猎手的持仓变动...")

                    # 订阅 logs
                    payload = {
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": monitored_hunters}, {"commitment": "confirmed"}]
                    }
                    await ws.send(json.dumps(payload))

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=AGENT_WS_RECV_TIMEOUT)
                            data = json.loads(msg)

                            params = data.get("params") if isinstance(data, dict) else None
                            if params and isinstance(params, dict):
                                result = params.get("result")
                                if result is not None:
                                    await self.process_log(result)

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
                    alchemy_client.mark_current_failed()
                    logger.warning("⚠️ Alchemy WebSocket 429 限流，已切换 Key，%d 秒后重试", AGENT_WS_ERROR_SLEEP_SEC)
                else:
                    logger.exception("❌ Agent 监控异常，%d秒后重试", AGENT_WS_ERROR_SLEEP_SEC)
                await asyncio.sleep(AGENT_WS_ERROR_SLEEP_SEC)

    async def process_log(self, log_info):
        """处理链上日志。log_info 为 logsSubscribe 的 result，格式可能为 {value: {signature: ...}} 或 {signature: ...}。"""
        if not log_info or not isinstance(log_info, dict):
            return
        value = log_info.get("value") or log_info
        signature = value.get("signature") if isinstance(value, dict) else None
        if not signature:
            return

        # 1. 快速过滤: 这笔交易是否涉及我们关心的猎手？
        # 通过 Alchemy RPC 拉取交易详情
        try:
            tx = await alchemy_client.get_transaction(signature, timeout=AGENT_GET_TX_TIMEOUT)
            if not tx:
                return

            # 2. 解析交易：找出参与的猎手，并只处理非 IGNORE 代币的变动（真实交易）
            # accountKeys 可能为 [{pubkey: str}, ...] 或 [str, ...] 取决于 RPC 格式
            msg = (tx.get("transaction") or {}).get("message") or {}
            account_keys_raw = msg.get("accountKeys") or []
            account_keys = []
            for k in account_keys_raw:
                if isinstance(k, dict):
                    pk = k.get("pubkey")
                    if pk:
                        account_keys.append(pk)
                elif isinstance(k, str):
                    account_keys.append(k)
            involved_hunters = set(account_keys).intersection(self.hunter_map.keys())

            if not involved_hunters:
                return

            # 3. 对每个涉及的猎手进行分析
            # 注意：Alchemy getTransaction 返回标准 RPC 格式
            block_time = tx.get("blockTime", time.time())

            for hunter in involved_hunters:
                potential_tokens = self.hunter_map[hunter]
                token_changes = self._calculate_balance_changes(tx, hunter)
                # 与 SmartFlow3 一致：只把非 SOL/USDC/USDT 的变动当作真实交易，忽略 IGNORE_MINTS
                token_changes = {m: v for m, v in token_changes.items() if m not in IGNORE_MINTS}
                if not token_changes:
                    continue

                for token_addr, (delta_raw, decimals) in token_changes.items():
                    if token_addr not in potential_tokens:
                        continue
                    delta_ui = delta_raw / (10 ** decimals)
                    await self.analyze_action(hunter, token_addr, delta_ui, tx, block_time)

        except Exception:
            logger.exception("日志处理失败")

    def _calculate_balance_changes(self, tx_data, hunter_address):
        """
        从 RPC 格式的交易中计算 Token 余额变化。
        返回: Dict[mint, (delta_raw, decimals)]，主程序需转 UI 后传入 analyze_action。
        """
        result = {}
        meta = tx_data.get("meta")
        if not meta:
            return result

        pre_balances = {}
        post_balances = {}
        decimals_map = {}

        for bal in meta.get("preTokenBalances", []):
            if bal["owner"] != hunter_address:
                continue
            mint = bal["mint"]
            uita = bal.get("uiTokenAmount", {})
            raw = float(uita.get("amount", 0) or 0)
            dec = int(uita.get("decimals", 6) or 6)
            pre_balances[mint] = raw
            decimals_map[mint] = dec

        for bal in meta.get("postTokenBalances", []):
            if bal["owner"] != hunter_address:
                continue
            mint = bal["mint"]
            uita = bal.get("uiTokenAmount", {})
            raw = float(uita.get("amount", 0) or 0)
            dec = int(uita.get("decimals", 6) or 6)
            post_balances[mint] = raw
            decimals_map[mint] = dec

        all_mints = set(pre_balances.keys()).union(post_balances.keys())
        for mint in all_mints:
            pre = pre_balances.get(mint, 0)
            post = post_balances.get(mint, 0)
            delta_raw = post - pre
            dec = decimals_map.get(mint, 6)
            if abs(delta_raw) > 0:
                result[mint] = (delta_raw, dec)
        return result

    async def analyze_action(self, hunter, token, delta, tx, timestamp):
        """
        核心：分析行为并生成信号。

        基线前交易过滤：若 tx 发生在 start_tracking 之前，说明是「共振触发的那笔买入」，
        已包含在 start_tracking 时的链上 snapshot 中，不可再当作「加仓」处理，
        否则会误发 HUNTER_BUY 导致用户错误加仓；进而 sync_positions 发现「余额对不上」
        会误判为「减仓」导致用户错误减仓。
        """
        mission = self.active_missions.get(token)
        if not mission:
            return

        # 基线前交易：发生在 start_tracking 之前的 tx，已含在初始 snapshot 中，跳过
        if timestamp > 0 and timestamp < mission.start_time:
            trade_logger.debug(
                "⏭️ [Agent] 忽略基线前交易 %s %s delta=%.2f (tx_time=%.0f < start=%.0f)",
                hunter[:8], token[:6], delta, timestamp, mission.start_time
            )
            return

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
                    "add_amount_ui": delta,
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
        """通过 AlchemyClient 获取猎手当前的 Token 余额（UI 单位）"""
        try:
            result = await alchemy_client.get_token_accounts_by_owner(hunter, token_mint, timeout=AGENT_TOKEN_ACCOUNTS_TIMEOUT)
            if not result or not result.get("value"):
                return 0.0
            total_ui = 0.0
            for acc in result["value"]:
                info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                tamt = info.get("tokenAmount") or {}
                ui = tamt.get("uiAmount")
                if ui is not None:
                    total_ui += float(ui)
            return total_ui if total_ui > 0 else 0.0
        except Exception:
            logger.exception("获取余额失败")
            return 0.0
