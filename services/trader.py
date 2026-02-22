#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : trader.py
@Description: 交易执行核心 (真实交易版)
              1. 资金/份额/止盈逻辑 (保持不变)
              2. Jupiter + Alchemy RPC 真实 Swap 逻辑
"""

import asyncio
import base64
import json
import math
import threading
import time
from typing import Dict, List, Set, Optional, Tuple, Callable, Any

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config.settings import (
    get_tier_config, TAKE_PROFIT_LEVELS,
    MIN_SHARE_VALUE_SOL, MIN_SELL_RATIO, FOLLOW_SELL_THRESHOLD, SELL_BUFFER,
    SOLANA_PRIVATE_KEY_BASE58,
    JUP_QUOTE_API, JUP_SWAP_API, SLIPPAGE_BPS, SELL_SLIPPAGE_BPS_RETRIES, jup_key_pool,
    TX_VERIFY_MAX_WAIT_SEC, TX_VERIFY_RETRY_DELAY_SEC, TX_VERIFY_RETRY_MAX_WAIT_SEC,
    TX_VERIFY_RECONCILIATION_DELAY_SEC, TX_VERIFY_RECONCILIATION_RETRIES,
    TRADER_RPC_TIMEOUT,
    WSOL_MINT,
    LAMPORTS_PER_SOL,
    TRADER_STATE_PATH,
    TRADER_BIRDEYE_PRICE_TIMEOUT,
    TRADER_RPC_ERROR_SLEEP_SEC,
    TRADER_VERIFY_RETRY_SLEEP_SEC,
)
from src.alchemy import alchemy_client
from utils.logger import get_logger

logger = get_logger(__name__)

# 防止多线程并发写 trader_state.json 导致文件损坏
_STATE_FILE_LOCK = threading.Lock()


def _is_rate_limit_error(e: Exception) -> bool:
    """
    检测是否为 429 / 限流类错误。SolanaRpcException 的 __cause__ 为 HTTPStatusError，
    str(e) 可能不含 429，需同时检查 __cause__。
    """
    parts = [str(e).lower()]
    cause = getattr(e, "__cause__", None)
    if cause:
        parts.append(str(cause).lower())
    combined = " ".join(parts)
    return any(
        x in combined for x in ("429", "too many requests", "rate", "limit", "credit")
    )


# 常量：WSOL_MINT、LAMPORTS_PER_SOL、TRADER_STATE_PATH 已移至 config.settings


class VirtualShare:
    def __init__(self, hunter_address: str, score: float, token_amount: float):
        self.hunter = hunter_address
        self.score = score
        self.token_amount = token_amount


class Position:
    def __init__(self, token_address: str, entry_price: float, decimals: int = 9, lead_hunter_score: float = 0):
        self.token_address = token_address
        self.average_price = entry_price
        self.decimals = decimals
        self.total_tokens = 0.0
        self.total_cost_sol = 0.0
        self.shares: Dict[str, VirtualShare] = {}
        self.tp_hit_levels: Set[float] = set()
        self.entry_time: float = 0.0  # 首次开仓时间，用于邮件
        self.trade_records: List[Dict] = []  # 每笔交易，用于清仓邮件
        self.lead_hunter_score: float = lead_hunter_score  # 跟单猎手分数，用于分档止损/加仓


class SolanaTrader:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.on_position_closed_callback: Optional[Callable[[dict], None]] = None  # 清仓时回调
        self.on_trade_recorded: Optional[Callable[[dict], None]] = None  # 每笔买卖后回调，用于 trading_history

        # 初始化钱包
        if not SOLANA_PRIVATE_KEY_BASE58:
            logger.error("❌ 未配置 SOLANA_PRIVATE_KEY，无法进行真实交易！")
            self.keypair = None
        else:
            try:
                self.keypair = Keypair.from_base58_string(SOLANA_PRIVATE_KEY_BASE58)
                logger.info(f"🤖 钱包已加载: {self.keypair.pubkey()}")
            except Exception:
                logger.exception("❌ 私钥格式错误")
                self.keypair = None

        # Alchemy Client (RPC) / Jupiter 各自独立，谁不可用谁自己换下一个
        self._jup_pool = jup_key_pool
        rpc_url = alchemy_client.get_rpc_url()
        if not rpc_url or not (rpc_url.startswith("http://") or rpc_url.startswith("https://")):
            logger.error(
                "❌ Alchemy RPC URL 无效: %r（请检查 ALCHEMY_API_KEY；若设了 HTTP_PROXY/HTTPS_PROXY 需为完整 URL）",
                rpc_url[:80] if rpc_url else "(空)",
            )
        self.rpc_client = AsyncClient(rpc_url, commitment=Confirmed)
        self.http_client = httpx.AsyncClient(timeout=TRADER_RPC_TIMEOUT)

    def _jup_headers(self) -> dict:
        """Jupiter 请求头，与 SmartFlow3 一致；若有 JUP Key 则带上 x-api-key。"""
        key = self._jup_pool.get_api_key()
        base = {"Accept": "application/json", "Content-Type": "application/json"}
        if not key:
            return base
        base["x-api-key"] = key
        return base

    async def _recreate_rpc_client(self) -> None:
        """
        当前 Alchemy key 不可用（429 等）时，切换 Alchemy 池内下一个并重建 RPC 客户端。
        若仅配置 1 个 Key，切换无效，需在 .env 中配置多个：ALCHEMY_API_KEY=key1,key2,key3
        """
        try:
            await self.rpc_client.close()
        except Exception:
            pass
        alchemy_client.mark_current_failed()
        rpc_url = alchemy_client.get_rpc_url()
        if not rpc_url or not (rpc_url.startswith("http://") or rpc_url.startswith("https://")):
            logger.error("❌ 切换 Key 后 Alchemy RPC URL 仍无效: %r", rpc_url[:50] if rpc_url else "(空)")
        self.rpc_client = AsyncClient(rpc_url, commitment=Confirmed)
        if alchemy_client.size <= 1:
            logger.warning("⚠️ 仅配置 1 个 Alchemy Key，429 时切换无效，建议配置多个: ALCHEMY_API_KEY=key1,key2,key3")
        else:
            logger.info("🔄 已切换 Alchemy Key，重建 RPC 客户端")

    async def close(self):
        await self.rpc_client.close()
        await self.http_client.aclose()

    async def _fetch_own_token_balance(self, token_mint: str) -> Optional[float]:
        """
        获取我方钱包在链上的 Token 余额（UI 单位）。
        通过 AlchemyClient.get_token_accounts_by_owner 调用，429 时由 Client 内部切换 Key 重试。
        """
        if not self.keypair:
            return None
        owner_b58 = str(self.keypair.pubkey())
        result = await alchemy_client.get_token_accounts_by_owner(
            owner_b58, token_mint, http_client=self.http_client, timeout=TRADER_RPC_TIMEOUT
        )
        if result is None:
            return None  # 请求失败
        if not result.get("value"):
            return 0.0  # 无持仓
        total_ui = 0.0
        for acc in result["value"]:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            tamt = info.get("tokenAmount") or {}
            ui = tamt.get("uiAmount")
            if ui is not None:
                total_ui += float(ui)
        return total_ui if total_ui > 0 else None

    async def _fetch_own_token_balance_raw(self, token_mint: str) -> Optional[int]:
        """
        获取我方钱包在链上的 Token 余额（raw 单位），用于交易验证失败时的兜底 reconciliation。
        """
        if not self.keypair:
            return None
        owner_b58 = str(self.keypair.pubkey())
        result = await alchemy_client.get_token_accounts_by_owner(
            owner_b58, token_mint, http_client=self.http_client, timeout=TRADER_RPC_TIMEOUT
        )
        if result is None or not result.get("value"):
            return None
        total_raw = 0
        for acc in result["value"]:
            info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            tamt = info.get("tokenAmount") or {}
            amt_str = tamt.get("amount")
            if amt_str is not None:
                try:
                    total_raw += int(amt_str)
                except (ValueError, TypeError):
                    pass
        return total_raw if total_raw > 0 else None

    async def ensure_fully_closed(self, token_address: str) -> None:
        """
        关闭监控前校验：链上仓位是否已归零。若未归零则执行清仓，避免遗漏 dust 或状态不同步。
        """
        if not self.keypair:
            return
        chain_bal = await self._fetch_own_token_balance(token_address)
        if chain_bal is None:
            return
        if chain_bal < 1e-9:  # 视为 0
            return
        logger.warning(
            "⚠️ 关闭监控前发现链上仍有持仓 %.6f，执行清仓",
            chain_bal
        )
        decimals = await self._get_decimals(token_address)
        decimals = decimals or 6
        tx_sig, _ = await self._jupiter_sell_with_retry(
            input_mint=token_address,
            output_mint=WSOL_MINT,
            amount_in_ui=chain_bal,
            token_decimals=decimals,
        )
        if not tx_sig:
            logger.warning("❌ 关闭前清仓失败: %s", token_address)

    async def emergency_close_all_positions(self) -> int:
        """
        保命操作：紧急清仓所有持仓。用于 Helius credit 耗尽等致命场景，无法继续跟单时立即平仓。
        使用 Alchemy RPC + Jupiter 执行，不依赖 Helius。
        :return: 成功清仓的数量
        """
        if not self.keypair:
            return 0
        tokens = list(self.positions.keys())
        if not tokens:
            return 0
        logger.warning("🚨 [紧急清仓] 开始清仓 %d 个持仓...", len(tokens))
        closed = 0
        for token_address in tokens:
            pos = self.positions.get(token_address)
            if not pos:
                continue
            try:
                chain_bal = await self._fetch_own_token_balance(token_address)
                sell_amount = chain_bal if chain_bal is not None else pos.total_tokens * SELL_BUFFER
                if sell_amount is None or sell_amount <= 0:
                    del self.positions[token_address]
                    closed += 1
                    continue
                decimals = await self._get_decimals(token_address) or pos.decimals
                tx_sig, sol_received = await self._jupiter_sell_with_retry(
                    input_mint=token_address,
                    output_mint=WSOL_MINT,
                    amount_in_ui=sell_amount,
                    token_decimals=decimals,
                )
                if tx_sig:
                    cost = sell_amount * pos.average_price
                    pnl_sol = sol_received - cost
                    pos.trade_records.append({
                        "ts": time.time(),
                        "type": "sell",
                        "sol_spent": 0.0,
                        "sol_received": sol_received,
                        "token_amount": sell_amount,
                        "note": "紧急清仓(Helius credit耗尽)",
                        "pnl_sol": pnl_sol,
                    })
                    if self.on_trade_recorded:
                        lead = list(pos.shares.keys())[0] if pos.shares else ""
                        self.on_trade_recorded({
                            "date": time.strftime("%Y-%m-%d", time.localtime()),
                            "ts": time.time(),
                            "token": token_address,
                            "type": "sell",
                            "sol_spent": 0.0,
                            "sol_received": sol_received,
                            "token_amount": sell_amount,
                            "price": pos.average_price,
                            "hunter_addr": lead,
                            "pnl_sol": pnl_sol,
                            "note": "紧急清仓(Helius credit耗尽)",
                        })
                    self._emit_position_closed(token_address, pos)
                    del self.positions[token_address]
                    closed += 1
                else:
                    logger.warning("❌ 紧急清仓失败: %s (链上余额 %.2f)", token_address, sell_amount)
            except Exception:
                logger.exception("紧急清仓异常: %s", token_address)
        self._save_state_in_background()
        return closed

    # ==========================================
    # 1. 核心交易接口 (逻辑层)
    # ==========================================

    async def execute_entry(self, token_address: str, hunters: List[Dict], total_score: float, current_price_ui: float):
        """开仓：只跟单一个猎手，按分数档位决定买入金额。"""
        if not self.keypair: return
        if token_address in self.positions: return
        if not hunters:
            return
        lead = hunters[0]  # 只跟单猎手（共振时已取最高分）
        score = float(lead.get('score', 0))
        tier = get_tier_config(score)

        # 1. 获取精度
        decimals = await self._get_decimals(token_address)
        if decimals == 0:
            logger.warning(f"⚠️ 无法获取 {token_address} 精度，默认使用 9")
            decimals = 9

        buy_sol = tier["entry_sol"]

        logger.info(f"🚀 [准备开仓] {token_address} | 计划: {buy_sol:.3f} SOL")

        # 2. 执行买入 (返回 Raw Amount)
        tx_sig, token_amount_raw = await self._jupiter_swap(
            input_mint=WSOL_MINT,
            output_mint=token_address,
            amount_in_ui=buy_sol,
            slippage_bps=SLIPPAGE_BPS
        )

        if not tx_sig: return

        # 3. 转换 UI Amount
        token_amount_ui = token_amount_raw / (10 ** decimals)

        # 计算均价
        if token_amount_ui > 0:
            actual_price = buy_sol / token_amount_ui
        else:
            actual_price = current_price_ui

        # 4. 建仓 (传入 decimals, lead_hunter_score)
        pos = Position(token_address, actual_price, decimals, lead_hunter_score=score)
        pos.total_cost_sol = buy_sol
        pos.total_tokens = token_amount_ui
        pos.entry_time = time.time()
        pos.trade_records.append({
            "ts": pos.entry_time,
            "type": "buy",
            "sol_spent": buy_sol,
            "sol_received": 0.0,
            "token_amount": token_amount_ui,
            "note": "首次开仓",
            "pnl_sol": None,
        })

        self.positions[token_address] = pos
        self._rebalance_shares_logic(pos, [lead])  # 只跟单猎手
        self._save_state_in_background()
        hunter_addr = lead.get("address", "")
        if self.on_trade_recorded:
            self.on_trade_recorded({
                "date": time.strftime("%Y-%m-%d", time.localtime()),
                "ts": pos.entry_time,
                "token": token_address,
                "type": "buy",
                "sol_spent": buy_sol,
                "sol_received": 0.0,
                "token_amount": token_amount_ui,
                "price": actual_price,
                "hunter_addr": hunter_addr,
                "pnl_sol": None,
                "note": "首次开仓",
            })
        logger.info(f"✅ 开仓成功 | 均价: {actual_price:.6f} SOL | 持仓: {token_amount_ui:.2f}")

    async def execute_add_position(self, token_address: str, trigger_hunter: Dict, add_reason: str,
                                   current_price: float):
        """
        加仓逻辑。只跟单猎手的加仓，猎手加仓 ≥ 1 SOL 才跟。按档位决定加仓金额与上限。
        """
        if not self.keypair: return
        pos = self.positions.get(token_address)
        if not pos: return

        # 只跟单猎手：加仓必须来自已在份额中的猎手
        hunter_addr = trigger_hunter.get('address')
        if hunter_addr not in pos.shares:
            return

        if pos.tp_hit_levels:
            logger.info("💰 [加仓跳过] %s 止盈已触发，禁止加仓", token_address[:8])
            return

        score = float(trigger_hunter.get('score', 0)) or pos.lead_hunter_score
        tier = get_tier_config(score) or get_tier_config(pos.lead_hunter_score)
        if not tier:
            return
        max_sol = tier["max_sol"]
        add_sol = tier["add_sol"]

        if pos.total_cost_sol >= max_sol:
            return
        if pos.total_cost_sol + add_sol > max_sol:
            add_sol = max_sol - pos.total_cost_sol
        if add_sol < 0.01:
            return

        logger.info(f"➕ [准备加仓] {token_address} | 金额: {add_sol:.3f} SOL")

        # === 真实买入 ===
        tx_sig, token_got_raw = await self._jupiter_swap(
            input_mint=WSOL_MINT,
            output_mint=token_address,
            amount_in_ui=add_sol,
            slippage_bps=SLIPPAGE_BPS
        )

        if not tx_sig: return

        # [关键修复] UI Amount 转换
        token_got_ui = token_got_raw / (10 ** pos.decimals)

        # 更新状态与均价 (一次计算即可)
        new_total_tokens = pos.total_tokens + token_got_ui
        pos.average_price = (pos.total_tokens * pos.average_price + add_sol) / new_total_tokens
        pos.total_cost_sol += add_sol
        pos.total_tokens = new_total_tokens

        pos.trade_records.append({
            "ts": time.time(),
            "type": "buy",
            "sol_spent": add_sol,
            "sol_received": 0.0,
            "token_amount": token_got_ui,
            "note": "加仓",
            "pnl_sol": None,
        })
        # 份额分配（只跟单猎手）
        if hunter_addr in pos.shares:
            pos.shares[hunter_addr].token_amount += token_got_ui
        else:
            pos.shares[hunter_addr] = VirtualShare(hunter_addr, trigger_hunter.get('score', 0), token_got_ui)
            current_hunters_info = [{"address": h, "score": s.score} for h, s in pos.shares.items()]
            self._rebalance_shares_logic(pos, current_hunters_info)
        if self.on_trade_recorded:
            self.on_trade_recorded({
                "date": time.strftime("%Y-%m-%d", time.localtime()),
                "ts": time.time(),
                "token": token_address,
                "type": "buy",
                "sol_spent": add_sol,
                "sol_received": 0.0,
                "token_amount": token_got_ui,
                "price": current_price,
                "hunter_addr": hunter_addr,
                "pnl_sol": None,
                "note": "加仓",
            })
        self._save_state_in_background()

    async def execute_follow_sell(self, token_address: str, hunter_addr: str, sell_ratio: float, current_price: float):
        """跟随卖出逻辑。文档: 猎手卖出<5%不跟，跟随时我方至少卖该份额的 MIN_SELL_RATIO。"""
        if not self.keypair: return
        pos = self.positions.get(token_address)
        if not pos: return

        share = pos.shares.get(hunter_addr)
        if not share or share.token_amount <= 0: return

        # 猎手微调（卖出比例过小）不跟，避免噪音
        if sell_ratio < FOLLOW_SELL_THRESHOLD:
            logger.debug("跟随卖出跳过: 猎手卖出比例 %.1f%% < 阈值 %.0f%%", sell_ratio * 100,
                         FOLLOW_SELL_THRESHOLD * 100)
            return

        actual_ratio = max(sell_ratio, MIN_SELL_RATIO)
        sell_amount_ui = share.token_amount * actual_ratio

        remaining = share.token_amount - sell_amount_ui
        is_dust = False
        if (remaining * current_price) < MIN_SHARE_VALUE_SOL:
            sell_amount_ui = share.token_amount
            is_dust = True

        sell_amount_ui = min(sell_amount_ui, share.token_amount)

        # 链上余额为准：查到多少卖多少；查余额失败时兜底 99.9% 防超卖
        chain_bal = await self._fetch_own_token_balance(token_address)
        if chain_bal is not None:
            if sell_amount_ui > chain_bal:
                logger.warning(
                    "⚠️ 状态与链上不一致: 计划卖 %.2f 但链上仅 %.2f，以链上为准",
                    sell_amount_ui, chain_bal
                )
                sell_amount_ui = min(sell_amount_ui, chain_bal)
        # 卖出前再拉一次链上余额，应对连续多笔跟卖时的延迟
        chain_bal2 = await self._fetch_own_token_balance(token_address)
        if chain_bal2 is not None and sell_amount_ui > chain_bal2:
            sell_amount_ui = min(sell_amount_ui, chain_bal2)
            logger.debug("二次校验链上余额 %.2f，最终卖出 %.2f", chain_bal2, sell_amount_ui)
            # 使用 chain_bal2（二次校验成功）进行状态同步，避免 chain_bal 为 None 时 TypeError
            if chain_bal2 < pos.total_tokens * 0.99:
                old_total = pos.total_tokens
                pos.total_tokens = chain_bal2
                if old_total > 0:
                    ratio = chain_bal2 / old_total
                    for s in pos.shares.values():
                        s.token_amount *= ratio
        else:
            # 查余额失败，兜底 99.9%
            sell_amount_ui = min(sell_amount_ui, share.token_amount * SELL_BUFFER)
        if sell_amount_ui <= 0:
            logger.warning("链上无持仓或余额为 0，同步状态并停止监控")
            self._sync_zero_and_close_position(token_address, pos)
            return

        logger.info(f"📉 [准备卖出] {token_address} | 数量: {sell_amount_ui:.2f}")

        # === 真实卖出（失败时按 2%/5%/10% 滑点递增重试，重试前会检查链上余额）===
        tx_sig, sol_got_ui = await self._jupiter_sell_with_retry(
            input_mint=token_address,
            output_mint=WSOL_MINT,
            amount_in_ui=sell_amount_ui,
            token_decimals=pos.decimals,
        )

        if not tx_sig:
            logger.warning("❌ 跟随卖出失败 (无 tx_sig): %s 数量 %.2f", token_address, sell_amount_ui)
            chain_after = await self._fetch_own_token_balance(token_address)
            if chain_after is not None and chain_after < 1e-9:
                logger.info("链上持仓已归零（交易或已成功），同步状态并停止监控")
                self._sync_zero_and_close_position(token_address, pos)
            return

        cost_this_sell = sell_amount_ui * pos.average_price
        pnl_sol = sol_got_ui - cost_this_sell
        ts_now = time.time()
        pos.trade_records.append({
            "ts": ts_now,
            "type": "sell",
            "sol_spent": 0.0,
            "sol_received": sol_got_ui,
            "token_amount": sell_amount_ui,
            "note": "跟随卖出",
            "pnl_sol": pnl_sol,
        })
        if self.on_trade_recorded:
            self.on_trade_recorded({
                "date": time.strftime("%Y-%m-%d", time.localtime(ts_now)),
                "ts": ts_now,
                "token": token_address,
                "type": "sell",
                "sol_spent": 0.0,
                "sol_received": sol_got_ui,
                "token_amount": sell_amount_ui,
                "price": pos.average_price,
                "hunter_addr": hunter_addr,
                "pnl_sol": pnl_sol,
                "note": "跟随卖出",
            })
        pos.total_tokens -= sell_amount_ui
        share.token_amount -= sell_amount_ui
        if is_dust or share.token_amount <= 0:
            if hunter_addr in pos.shares:
                del pos.shares[hunter_addr]
        if pos.total_tokens <= 0:
            self._emit_position_closed(token_address, pos)
            del self.positions[token_address]
        self._save_state_in_background()

    async def check_pnl_and_stop_profit(self, token_address: str, current_price_ui: float):
        """
        止盈与止损逻辑。
        止损：亏损达到 get_tier_config(lead_hunter_score).stop_loss_pct 时全仓止损（当前档位均为 40%）。
        止盈：盈利达到 TAKE_PROFIT_LEVELS 各级阈值时按对应比例分批卖出（如 1000% 卖 80%）。
        """
        if not self.keypair: return
        pos = self.positions.get(token_address)
        if not pos or pos.total_tokens <= 0: return
        if pos.average_price <= 0:
            logger.warning("止盈跳过: 均价异常 %.6f", pos.average_price)
            return

        pnl_pct = (current_price_ui - pos.average_price) / pos.average_price

        # DexScreener 价格可能因 base/quote 解析错误虚高，当 pnl>200% 时用 Jupiter 校验真实可卖价
        if pnl_pct > 2.0:
            jupiter_implied_pnl = await self._get_jupiter_implied_pnl(
                token_address, pos.average_price, pos.decimals
            )
            if jupiter_implied_pnl is not None and jupiter_implied_pnl < 0.5:
                logger.warning(
                    "止盈跳过: DexScreener 显示 +%.0f%% 但 Jupiter 校验仅 %.0f%%，以 Jupiter 为准",
                    pnl_pct * 100, jupiter_implied_pnl * 100
                )
                pnl_pct = jupiter_implied_pnl

        tier = get_tier_config(pos.lead_hunter_score) or {}
        stop_loss_pct = tier.get("stop_loss_pct", 0.4)
        if pnl_pct <= -stop_loss_pct:
            # Birdeye 二次验价：DexScreener 可能因假暴涨/假暴跌插针误触发止损，用 Birdeye 交叉验证
            proceed_stop_loss = True
            try:
                from config.settings import birdeye_key_pool
                if birdeye_key_pool.size > 0:
                    from src.birdeye import birdeye_client
                    logger.warning(
                        "📉 DexScreener 报亏损触及止损线: %.0f%%，启动 Birdeye 二次验价防插针...",
                        pnl_pct * 100,
                    )
                    full = await birdeye_client.get_price_full(token_address, timeout=TRADER_BIRDEYE_PRICE_TIMEOUT)
                    if full and full.get("priceInNative") is not None:
                        be_price_sol = float(full["priceInNative"])
                        if be_price_sol > 0 and pos.average_price > 0:
                            be_pnl_pct = (be_price_sol - pos.average_price) / pos.average_price
                            if be_pnl_pct > -stop_loss_pct:
                                logger.info(
                                    "🛡️ Birdeye 验价拦截：真实亏损 %.0f%% 未达止损 %.0f%%，疑似 DexScreener 插针，跳过止损",
                                    be_pnl_pct * 100,
                                    stop_loss_pct * 100,
                                )
                                proceed_stop_loss = False
                            else:
                                logger.info("🛡️ Birdeye 验价确认：真实亏损 %.0f%%，执行止损", be_pnl_pct * 100)
            except Exception as e:
                logger.debug("Birdeye 二次验价异常，回退 DexScreener 判定: %s", e)
            if not proceed_stop_loss:
                return

            chain_bal = await self._fetch_own_token_balance(token_address)
            sell_amount = chain_bal if chain_bal is not None else pos.total_tokens * SELL_BUFFER
            if chain_bal is not None and chain_bal < pos.total_tokens * 0.99:
                logger.warning("⚠️ 止损前状态与链上不一致: 内部 %.2f vs 链上 %.2f", pos.total_tokens, chain_bal)
            if sell_amount <= 0:
                logger.warning("链上无持仓，跳过止损")
                return
            logger.info(f"🛑 [止损触发] {token_address} (亏损 {pnl_pct * 100:.0f}%) | 全仓清仓 {sell_amount:.2f}")

            decimals = await self._get_decimals(token_address)
            tx_sig, sol_received = await self._jupiter_sell_with_retry(
                input_mint=token_address,
                output_mint=WSOL_MINT,
                amount_in_ui=sell_amount,
                token_decimals=decimals,
            )

            if not tx_sig:
                logger.warning("❌ 止损卖出失败 (无 tx_sig): %s", token_address)
                chain_after = await self._fetch_own_token_balance(token_address)
                if chain_after is not None and chain_after < 1e-9:
                    logger.info("链上持仓已归零，同步状态并停止监控")
                    self._sync_zero_and_close_position(token_address, pos)
                self._save_state_in_background()
                return

            if tx_sig:
                cost_this_sell = sell_amount * pos.average_price
                pnl_sol = sol_received - cost_this_sell
                ts_now = time.time()
                pos.trade_records.append({
                    "ts": ts_now,
                    "type": "sell",
                    "sol_spent": 0.0,
                    "sol_received": sol_received,
                    "token_amount": sell_amount,
                    "note": f"止损{stop_loss_pct * 100:.0f}%",
                    "pnl_sol": pnl_sol,
                })
                if self.on_trade_recorded:
                    lead = list(pos.shares.keys())[0] if pos.shares else ""
                    self.on_trade_recorded({
                        "date": time.strftime("%Y-%m-%d", time.localtime(ts_now)),
                        "ts": ts_now,
                        "token": token_address,
                        "type": "sell",
                        "sol_spent": 0.0,
                        "sol_received": sol_received,
                        "token_amount": sell_amount,
                        "price": pos.average_price,
                        "hunter_addr": lead,
                        "pnl_sol": pnl_sol,
                        "note": f"止损{stop_loss_pct * 100:.0f}%",
                    })
                self._emit_position_closed(token_address, pos)
                del self.positions[token_address]
            self._save_state_in_background()
            return

        for level, sell_pct in TAKE_PROFIT_LEVELS:
            if pnl_pct >= level and level not in pos.tp_hit_levels:
                sell_amount = pos.total_tokens * sell_pct
                remaining_after = pos.total_tokens * (1.0 - sell_pct)
                if (remaining_after * current_price_ui) < MIN_SHARE_VALUE_SOL:
                    sell_amount = pos.total_tokens
                    logger.info("剩余价值不足 %.4f SOL，直接全仓止盈", MIN_SHARE_VALUE_SOL)
                chain_bal = await self._fetch_own_token_balance(token_address)
                if chain_bal is not None:
                    sell_amount = min(sell_amount, chain_bal)
                    if chain_bal < pos.total_tokens * 0.99:
                        logger.warning("⚠️ 止盈前状态与链上不一致: 内部 %.2f vs 链上 %.2f", pos.total_tokens, chain_bal)
                else:
                    sell_amount = min(sell_amount, pos.total_tokens * SELL_BUFFER)  # 查余额失败，兜底 99.9%
                if sell_amount <= 0:
                    logger.warning("链上无持仓，跳过止盈")
                    continue
                logger.info(f"💰 [止盈触发] {token_address} (+{pnl_pct * 100:.0f}%) | 卖出 {sell_amount:.2f}")

                # === 真实卖出（失败时按 2%/5%/10% 滑点递增重试）===
                decimals = await self._get_decimals(token_address)
                tx_sig, sol_received = await self._jupiter_sell_with_retry(
                    input_mint=token_address,
                    output_mint=WSOL_MINT,
                    amount_in_ui=sell_amount,
                    token_decimals=decimals,
                )

                if not tx_sig:
                    logger.warning("❌ 止盈卖出失败 (无 tx_sig): %s 数量 %.2f", token_address, sell_amount)
                    chain_after = await self._fetch_own_token_balance(token_address)
                    if chain_after is not None and chain_after < 1e-9:
                        logger.info("链上持仓已归零，同步状态并停止监控")
                        self._sync_zero_and_close_position(token_address, pos)
                    self._save_state_in_background()
                    return

                if tx_sig:
                    cost_this_sell = sell_amount * pos.average_price
                    pnl_sol = sol_received - cost_this_sell
                    ts_now = time.time()
                    sell_pct_actual = sell_amount / pos.total_tokens if pos.total_tokens > 0 else 1.0
                    pos.trade_records.append({
                        "ts": ts_now,
                        "type": "sell",
                        "sol_spent": 0.0,
                        "sol_received": sol_received,
                        "token_amount": sell_amount,
                        "note": f"止盈{sell_pct_actual * 100:.0f}%",
                        "pnl_sol": pnl_sol,
                    })
                    if self.on_trade_recorded:
                        lead = list(pos.shares.keys())[0] if pos.shares else ""
                        self.on_trade_recorded({
                            "date": time.strftime("%Y-%m-%d", time.localtime(ts_now)),
                            "ts": ts_now,
                            "token": token_address,
                            "type": "sell",
                            "sol_spent": 0.0,
                            "sol_received": sol_received,
                            "token_amount": sell_amount,
                            "price": pos.average_price,
                            "hunter_addr": lead,
                            "pnl_sol": pnl_sol,
                            "note": f"止盈{sell_pct_actual * 100:.0f}%",
                        })
                    for share in pos.shares.values():
                        share.token_amount *= (1.0 - sell_pct_actual)
                    pos.total_tokens -= sell_amount
                    pos.tp_hit_levels.add(level)
                    if pos.total_tokens <= 0:
                        self._emit_position_closed(token_address, pos)
                        del self.positions[token_address]
                self._save_state_in_background()

    async def _jupiter_sell_with_retry(
        self, input_mint: str, output_mint: str, amount_in_ui: float, token_decimals: int = 9
    ) -> Tuple[Optional[str], float]:
        """
        卖出专用：按 SELL_SLIPPAGE_BPS_RETRIES 依次尝试，滑点递增直至成功或耗尽。
        卖出失败（滑点不足等）时优先重试而非直接放弃。
        重试前检查链上余额，避免前次交易已成功但验证超时导致重复卖出（6024 超卖错误）。
        """
        slippage_list = SELL_SLIPPAGE_BPS_RETRIES if SELL_SLIPPAGE_BPS_RETRIES else [SLIPPAGE_BPS]
        current_amount = amount_in_ui
        for i, bps in enumerate(slippage_list):
            if current_amount <= 0:
                logger.info("链上持仓已为 0，无需继续卖出重试")
                return None, 0.0
            tx_sig, sol_out = await self._jupiter_swap(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_in_ui=current_amount,
                slippage_bps=bps,
                is_sell=True,
                token_decimals=token_decimals,
            )
            if tx_sig is not None:
                if i > 0:
                    logger.info("✅ 卖出成功 (滑点 %.1f%%)", bps / 100)
                return tx_sig, sol_out
            if i < len(slippage_list) - 1:
                chain_bal = await self._fetch_own_token_balance(input_mint)
                if chain_bal is not None:
                    current_amount = min(current_amount, chain_bal)
                    if chain_bal < 1e-9:
                        logger.info("链上持仓已归零（前次卖出或已成功），停止重试")
                        return None, 0.0
                    logger.warning(
                        "❌ 卖出失败，按链上余额 %.2f 重试下一档滑点 %.1f%%",
                        current_amount, slippage_list[i + 1] / 100,
                    )
                else:
                    logger.warning("❌ 卖出失败，尝试下一档滑点 %.1f%% (无法查链上余额)", slippage_list[i + 1] / 100)
        return None, 0.0

    async def _jupiter_swap(self, input_mint: str, output_mint: str, amount_in_ui: float, slippage_bps: int,
                            is_sell: bool = False, token_decimals: int = 9) -> Tuple[Optional[str], float]:
        """
        通用 Swap 函数 (Jupiter v1 + Alchemy RPC 广播)。Alchemy/Jupiter 各自独立切 key，
        遇 429 时先 backoff 等待再切换 key 重试。
        """
        max_attempts = max(3, alchemy_client.size)
        for attempt in range(max_attempts):
            try:
                if not is_sell:
                    amount_int = int(amount_in_ui * LAMPORTS_PER_SOL)
                else:
                    # 卖出使用 floor，避免浮点转 int 时多出 1 raw unit 导致链上超卖失败
                    amount_int = math.floor(amount_in_ui * (10 ** token_decimals))

                # 与 SmartFlow3 一致：添加 onlyDirectRoutes / asLegacyTransaction 以提高路由兼容性
                quote_params = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount_int),
                    "slippageBps": slippage_bps,
                    "onlyDirectRoutes": "false",
                    "asLegacyTransaction": "false",
                }
                quote_resp = await self.http_client.get(
                    JUP_QUOTE_API, params=quote_params, headers=self._jup_headers()
                )
                if quote_resp.status_code == 429:
                    self._jup_pool.mark_current_failed()
                    if attempt < max_attempts - 1:
                        backoff_sec = 5 + attempt * 3  # 5s, 8s, 11s...
                        logger.warning("Jupiter Quote 429，%ds 后重试 (attempt %d/%d)", backoff_sec, attempt + 1,
                                       max_attempts)
                        await asyncio.sleep(backoff_sec)
                        continue
                if quote_resp.status_code != 200:
                    logger.error("Quote Error: %s", quote_resp.text)
                    return None, 0

                quote_data = quote_resp.json()
                out_amount_raw = int(quote_data.get("outAmount", 0))

                # 与 SmartFlow3 完全一致：仅使用 computeUnitPriceMicroLamports
                swap_payload = {
                    "userPublicKey": str(self.keypair.pubkey()),
                    "quoteResponse": quote_data,
                    "wrapAndUnwrapSol": True,
                    "computeUnitPriceMicroLamports": "auto",
                }
                swap_resp = await self.http_client.post(
                    JUP_SWAP_API, json=swap_payload, headers=self._jup_headers()
                )
                if swap_resp.status_code == 429:
                    self._jup_pool.mark_current_failed()
                    if attempt < max_attempts - 1:
                        backoff_sec = 5 + attempt * 3
                        logger.warning("Jupiter Swap Build 429，%ds 后重试 (attempt %d/%d)", backoff_sec, attempt + 1,
                                       max_attempts)
                        await asyncio.sleep(backoff_sec)
                        continue
                if swap_resp.status_code != 200:
                    logger.error("Swap Build Error: %s", swap_resp.text)
                    return None, 0

                swap_data = swap_resp.json()
                swap_transaction_base64 = swap_data.get("swapTransaction") or swap_data.get("transaction")
                if not swap_transaction_base64:
                    logger.error("Swap 响应缺少 swapTransaction: %s", swap_data)
                    return None, 0
                raw_tx = base64.b64decode(swap_transaction_base64)
                tx = VersionedTransaction.from_bytes(raw_tx)
                signature = self.keypair.sign_message(to_bytes_versioned(tx.message))
                signed_tx = VersionedTransaction.populate(tx.message, [signature])
                opts = TxOpts(skip_preflight=True, max_retries=3)
                result = await self.rpc_client.send_transaction(signed_tx, opts=opts)
                sig_str = str(getattr(result, "value", result))
                logger.info("⏳ 交易已广播: %s", sig_str)
                await asyncio.sleep(TRADER_RPC_ERROR_SLEEP_SEC)

                # 验证交易是否真正确认，避免广播成功但链上执行失败时误更新状态
                verified = await self._verify_tx_confirmed(sig_str, max_wait_sec=TX_VERIFY_MAX_WAIT_SEC)
                if not verified:
                    # 初次验证失败可能是 RPC 限流/超时导致误判，交易实则已成功。二次验证降低漏记风险。
                    logger.info(
                        "⏳ 初次验证超时/无响应，%ds 后切换 RPC 进行二次验证: %s",
                        TX_VERIFY_RETRY_DELAY_SEC, sig_str,
                    )
                    await asyncio.sleep(TX_VERIFY_RETRY_DELAY_SEC)
                    if alchemy_client.size >= 1:
                        await self._recreate_rpc_client()
                    verified = await self._verify_tx_confirmed(
                        sig_str, max_wait_sec=TX_VERIFY_RETRY_MAX_WAIT_SEC
                    )
                    if verified:
                        logger.info("⚠️ 二次验证成功，交易已确认（初检可能受 RPC 限流影响）: %s", sig_str)
                    else:
                        # 兜底（仅买入）：RPC 验证超时/失败时（含 Key 超额 429 查不到链上状态），
                        # 等待限流恢复后多次重试查余额，避免「查不到就说失败」导致漏跟卖
                        if not is_sell:
                            min_expected = int(out_amount_raw * 0.99)
                            logger.info(
                                "⏳ 验证失败，%ds 后开始链上余额兜底（Key 超额 429 时需等待限流恢复）: %s",
                                TX_VERIFY_RECONCILIATION_DELAY_SEC, sig_str,
                            )
                            await asyncio.sleep(TX_VERIFY_RECONCILIATION_DELAY_SEC)
                            for recon_attempt in range(TX_VERIFY_RECONCILIATION_RETRIES):
                                chain_raw = await self._fetch_own_token_balance_raw(output_mint)
                                if chain_raw is not None and chain_raw >= min_expected:
                                    logger.warning(
                                        "⚠️ 买入验证超时但链上余额已到账 (raw %s >= %s)，以链上为准视为成功: %s",
                                        chain_raw, min_expected, sig_str,
                                    )
                                    return sig_str, out_amount_raw
                                if recon_attempt < TX_VERIFY_RECONCILIATION_RETRIES - 1:
                                    if alchemy_client.size >= 1:
                                        alchemy_client.mark_current_failed()
                                        await self._recreate_rpc_client()
                                    backoff = 10 + recon_attempt * 5
                                    logger.info(
                                        "⏳ 余额兜底第 %d/%d 次未查到，%ds 后切换 Key 重试",
                                        recon_attempt + 1, TX_VERIFY_RECONCILIATION_RETRIES, backoff,
                                    )
                                    await asyncio.sleep(backoff)
                        # 余额兜底耗尽仍失败：极可能所有 Key 均已 429，必须报致命错误
                        logger.critical(
                            "🚨 致命：所有 RPC Key 或已 429 超额，链上余额无法查询，交易 %s 无法确认。"
                            "可能导致漏跟卖，请立即检查 API 配额并增加 Key！",
                            sig_str,
                        )
                        return None, 0

                # 显式记录买入/卖出确认，便于排查与审计
                if is_sell:
                    logger.info("✅ 卖出已确认: %s", sig_str)
                else:
                    logger.info("✅ 买入已确认: %s", sig_str)

                if not is_sell:
                    return sig_str, out_amount_raw
                return sig_str, out_amount_raw / LAMPORTS_PER_SOL
            except Exception as e:
                if attempt < max_attempts - 1 and alchemy_client.size >= 1 and _is_rate_limit_error(e):
                    backoff_sec = 8 + attempt * 4  # send_raw_transaction 429 需较长等待
                    logger.warning("Alchemy RPC 限流 (send_raw_transaction)，%ds backoff 后切换 Key 重试: %s",
                                   backoff_sec, e)
                    await asyncio.sleep(backoff_sec)
                    await self._recreate_rpc_client()
                    continue
                if _is_rate_limit_error(e):
                    logger.critical(
                        "🚨 致命：所有 RPC Key 已 429 超额，交易无法执行。请立即检查 API 配额并增加 Key！",
                        exc_info=True,
                    )
                else:
                    logger.exception("Swap Exception")
                return None, 0
        # 循环耗尽未返回：所有 attempt 均失败
        logger.critical(
            "🚨 致命：Swap 重试 %d 次后仍失败，RPC Key 或已全部 429 超额。请检查 API 配额！",
            max_attempts,
        )
        return None, 0

    async def _get_jupiter_implied_pnl(
            self, token_mint: str, average_price: float, decimals: int
    ) -> Optional[float]:
        """
        用 Jupiter Quote 卖少量 token，推算真实可卖价，用于校验 DexScreener 是否虚高。
        返回 (implied_price - avg) / avg，失败返回 None。
        """
        if average_price <= 0:
            return None
        sample_amount_ui = max(100.0, min(1e6, 0.00001 / average_price))  # 约 0.00001 SOL 等值，避免过大
        try:
            amount_raw = math.floor(sample_amount_ui * (10 ** decimals))
            if amount_raw <= 0:
                return None
            params = {
                "inputMint": token_mint,
                "outputMint": WSOL_MINT,
                "amount": str(amount_raw),
                "slippageBps": 100,
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "false",
            }
            resp = await self.http_client.get(JUP_QUOTE_API, params=params, headers=self._jup_headers())
            if resp.status_code != 200:
                return None
            out_raw = int((resp.json() or {}).get("outAmount", 0))
            sol_out = out_raw / LAMPORTS_PER_SOL
            if sol_out <= 0:
                return None
            implied_price = sol_out / sample_amount_ui
            return (implied_price - average_price) / average_price
        except Exception:
            logger.debug("Jupiter 校验价格异常", exc_info=True)
        return None

    async def _verify_tx_confirmed(self, sig_str: str, max_wait_sec: int | None = None) -> bool:
        """
        轮询 get_signature_statuses，确认交易成功落地。
        链上失败（滑点等）时返回 False。遇 Alchemy 429 时切换 Key 继续轮询，避免限流误判。
        """
        if max_wait_sec is None:
            max_wait_sec = TX_VERIFY_MAX_WAIT_SEC
        try:
            from solders.signature import Signature
            sig = Signature.from_string(sig_str) if isinstance(sig_str, str) else sig_str
            for _ in range(max_wait_sec):
                try:
                    resp = await self.rpc_client.get_signature_statuses([sig])
                except Exception as e:
                    if _is_rate_limit_error(e) and alchemy_client.size > 1:
                        logger.warning("验证交易时 Alchemy 429，切换 Key 继续: %s", e)
                        await self._recreate_rpc_client()
                        await asyncio.sleep(TRADER_VERIFY_RETRY_SLEEP_SEC)
                        continue
                    logger.debug("验证交易确认异常", exc_info=True)
                    await asyncio.sleep(TRADER_VERIFY_RETRY_SLEEP_SEC)
                    continue
                vals = getattr(resp, "value", None) or []
                if not vals:
                    await asyncio.sleep(TRADER_VERIFY_RETRY_SLEEP_SEC)
                    continue
                st = vals[0]
                if st is None:
                    await asyncio.sleep(TRADER_VERIFY_RETRY_SLEEP_SEC)
                    continue
                err = getattr(st, "err", None)
                if err is not None:
                    logger.warning("交易链上执行失败 err=%s", err)
                    return False
                conf = getattr(st, "confirmation_status", None) or ""
                if conf in ("confirmed", "finalized") or getattr(st, "confirmationStatus", "") in (
                "confirmed", "finalized"):
                    return True
                await asyncio.sleep(1)
        except Exception:
            logger.debug("验证交易确认异常", exc_info=True)
        return False

    async def _get_decimals(self, mint_address: str) -> int:
        """
        获取代币精度。Pump.fun 代币多为 6 位，遇 429/限流时不再重试，
        直接返回默认值；但必须切换 Alchemy Key，否则后续 send_transaction 会继续打同一 Key。
        """
        try:
            pubkey = Pubkey.from_string(mint_address)
            resp = await self.rpc_client.get_token_supply(pubkey)
            return resp.value.decimals
        except Exception as e:
            if _is_rate_limit_error(e):
                logger.warning("获取 decimals 遇限流，切换 Key 并使用默认 6: %s", e)
                if alchemy_client.size >= 1:
                    await self._recreate_rpc_client()
            else:
                logger.exception("获取 decimals 失败，使用默认 6")
            return 6  # pump.fun 代币常见精度

    def _rebalance_shares_logic(self, pos: Position, hunters: List[Dict]):
        """
        份额分配：谁卖跟谁跑。
        - 1 个猎手：100% 份额，只跟这一个人买卖（除非后续有新猎手进场会触发重新分配）
        - 2 个猎手：按分数比例分配
        - 3 个及以上：均分三份（取前三人）
        """
        count = len(hunters)
        if count == 0:
            return
        total_tokens = pos.total_tokens
        new_shares = {}

        if count == 1:
            # 单猎手跟仓：全部份额归其一人，只需跟其买卖
            h = hunters[0]
            new_shares[h['address']] = VirtualShare(h['address'], h.get('score', 0), total_tokens)
        elif count >= 3:
            # 三人及以上：均分三份
            active = hunters[:3]
            share_amt = total_tokens / 3.0
            for h in active:
                new_shares[h['address']] = VirtualShare(h['address'], h.get('score', 0), share_amt)
        else:
            # 两人：按分数比例分配
            total_score = sum(h.get('score', 0) for h in hunters)
            if total_score == 0:
                total_score = 1
            for h in hunters:
                ratio = h.get('score', 0) / total_score
                new_shares[h['address']] = VirtualShare(h['address'], h.get('score', 0), total_tokens * ratio)
        pos.shares = new_shares

    def _sync_zero_and_close_position(self, token_address: str, pos: Position) -> None:
        """
        链上持仓为 0 时同步内部状态并触发清仓回调，便于 hunter_agent 停止监控。
        用于：链上无持仓时跳过卖出、卖出失败但链上已归零（验证超时导致误判）。
        """
        if token_address not in self.positions:
            return
        self._emit_position_closed(token_address, pos)
        del self.positions[token_address]
        self._save_state_in_background()
        logger.info("📤 已同步清仓状态并移除持仓记录: %s", token_address[:16] + "..")

    def _emit_position_closed(self, token_address: str, pos: Position) -> None:
        """清仓时构造 snapshot 并触发回调（发邮件等）。"""
        total_spent = sum(float(r.get("sol_spent") or 0) for r in pos.trade_records)
        total_received = sum(float(r.get("sol_received") or 0) for r in pos.trade_records)
        snapshot = {
            "token_address": token_address,
            "entry_time": pos.entry_time,
            "trade_records": list(pos.trade_records),
            "total_pnl_sol": total_received - total_spent,
        }
        if self.on_position_closed_callback:
            try:
                self.on_position_closed_callback(snapshot)
            except Exception:
                logger.exception("清仓回调执行异常")

    # ==========================================
    # 持仓持久化（程序挂掉后重启可恢复跟单状态）
    # ==========================================

    def _position_to_dict(self, pos: Position) -> Dict[str, Any]:
        """将 Position 转为可 JSON 序列化的 dict。"""
        return {
            "token_address": pos.token_address,
            "entry_time": pos.entry_time,
            "average_price": pos.average_price,
            "decimals": pos.decimals,
            "total_tokens": pos.total_tokens,
            "total_cost_sol": pos.total_cost_sol,
            "lead_hunter_score": pos.lead_hunter_score,
            "tp_hit_levels": list(pos.tp_hit_levels),
            "shares": {
                addr: {"hunter": s.hunter, "score": s.score, "token_amount": s.token_amount}
                for addr, s in pos.shares.items()
            },
            "trade_records": list(pos.trade_records),
        }

    def _dict_to_position(self, d: Dict[str, Any]) -> Position:
        """从 dict 恢复 Position。decimals 至少为 1 防除零。"""
        decimals = max(1, int(d.get("decimals", 9)))
        pos = Position(
            d["token_address"],
            float(d.get("average_price", 0)),
            decimals,
        )
        pos.entry_time = float(d.get("entry_time", 0))
        pos.total_tokens = float(d.get("total_tokens", 0))
        pos.total_cost_sol = float(d.get("total_cost_sol", 0))
        pos.lead_hunter_score = float(d.get("lead_hunter_score", 0))
        pos.tp_hit_levels = set(float(x) for x in d.get("tp_hit_levels", []))
        for addr, s in (d.get("shares") or {}).items():
            pos.shares[addr] = VirtualShare(
                s.get("hunter", addr),
                float(s.get("score", 0)),
                float(s.get("token_amount", 0)),
            )
        pos.trade_records = list(d.get("trade_records") or [])
        return pos

    def _save_state_safe(self) -> None:
        """同步写入当前持仓到本地文件（内部用）。带锁防多线程并发写损坏。"""
        try:
            with _STATE_FILE_LOCK:
                TRADER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "positions": {
                        token: self._position_to_dict(pos)
                        for token, pos in self.positions.items()
                        if pos.total_tokens > 0
                    }
                }
                with open(TRADER_STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("保存持仓状态失败")

    def _save_state_in_background(self) -> None:
        """后台线程持久化持仓，不阻塞跟单。"""

        def _run():
            self._save_state_safe()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def save_state(self) -> None:
        """公开方法：持久化当前持仓到 data/trader_state.json（后台线程，不阻塞）。"""
        self._save_state_in_background()

    def load_state(self) -> None:
        """从 data/modelA|modelB/trader_state.json 恢复持仓，启动时调用。与保存共用锁，避免读时正在写。"""
        if not TRADER_STATE_PATH.exists():
            return
        try:
            with _STATE_FILE_LOCK:
                with open(TRADER_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            positions_data = data.get("positions") or {}
            for token, pd in positions_data.items():
                pos = self._dict_to_position(pd)
                if pos.total_tokens > 0:
                    self.positions[token] = pos
            if self.positions:
                logger.info("📂 已从本地恢复 %s 个持仓", len(self.positions))
        except Exception:
            logger.exception("加载持仓状态失败")

    def get_active_tokens(self) -> List[str]:
        return [t for t, p in self.positions.items() if p.total_tokens > 0]
