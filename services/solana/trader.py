#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@File    : trader.py
@Description: 交易执行核心 (真实交易版)
              1. 资金/份额/止盈逻辑 (保持不变)
              2. [新增] Jupiter + Helius 真实 Swap 逻辑
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple, Callable, Any

import httpx
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config.settings import (
    TRADING_MAX_SOL_PER_TOKEN, TRADING_MIN_BUY_SOL, TRADING_ADD_BUY_SOL,
    TRADING_SCORE_MULTIPLIER, TAKE_PROFIT_LEVELS,
    MIN_SHARE_VALUE_SOL, MIN_SELL_RATIO, FOLLOW_SELL_THRESHOLD,
    SOLANA_PRIVATE_KEY_BASE58, HELIUS_RPC_URL,
    JUPITER_QUOTE_API, JUPITER_SWAP_API, SLIPPAGE_BPS, PRIORITY_FEE_SETTINGS,
    BASE_DIR,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# 常量
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
TRADER_STATE_PATH = BASE_DIR / "data" / "trader_state.json"


class VirtualShare:
    def __init__(self, hunter_address: str, score: float, token_amount: float):
        self.hunter = hunter_address
        self.score = score
        self.token_amount = token_amount


class Position:
    def __init__(self, token_address: str, entry_price: float, decimals: int = 9):
        self.token_address = token_address
        self.average_price = entry_price
        self.decimals = decimals
        self.total_tokens = 0.0
        self.total_cost_sol = 0.0
        self.shares: Dict[str, VirtualShare] = {}
        self.tp_hit_levels: Set[float] = set()
        self.entry_time: float = 0.0  # 首次开仓时间，用于邮件
        self.trade_records: List[Dict] = []  # 每笔交易，用于清仓邮件


class SolanaTrader:
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.on_position_closed_callback: Optional[Callable[[dict], None]] = None  # 清仓时回调，传 snapshot

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

        # 初始化 RPC 客户端
        self.rpc_client = AsyncClient(HELIUS_RPC_URL, commitment=Confirmed)
        self.http_client = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        await self.rpc_client.close()
        await self.http_client.aclose()

    # ==========================================
    # 1. 核心交易接口 (逻辑层)
    # ==========================================

    async def execute_entry(self, token_address: str, hunters: List[Dict], total_score: float, current_price_ui: float):
        if not self.keypair: return
        if token_address in self.positions: return

        # 1. 获取精度 (这是关键)
        decimals = await self._get_decimals(token_address)
        # 如果获取失败返回 0，我们强制设为 9 (SOL) 或 6 (USDC)，这里设为 9 更通用
        if decimals == 0:
            logger.warning(f"⚠️ 无法获取 {token_address} 精度，默认使用 9")
            decimals = 9

        buy_sol = total_score * TRADING_SCORE_MULTIPLIER
        buy_sol = max(buy_sol, TRADING_MIN_BUY_SOL)
        buy_sol = min(buy_sol, TRADING_MAX_SOL_PER_TOKEN)

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

        # 4. 建仓 (传入 decimals)
        pos = Position(token_address, actual_price, decimals)
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
        self._rebalance_shares_logic(pos, hunters)
        self._save_state_safe()
        logger.info(f"✅ 开仓成功 | 均价: {actual_price:.6f} SOL | 持仓: {token_amount_ui:.2f}")

    async def execute_add_position(self, token_address: str, trigger_hunter: Dict, add_reason: str,
                                   current_price: float):
        """加仓逻辑"""
        if not self.keypair: return
        pos = self.positions.get(token_address)
        if not pos: return

        if pos.total_cost_sol >= TRADING_MAX_SOL_PER_TOKEN: return

        buy_sol = TRADING_ADD_BUY_SOL
        if pos.total_cost_sol + buy_sol > TRADING_MAX_SOL_PER_TOKEN:
            buy_sol = TRADING_MAX_SOL_PER_TOKEN - pos.total_cost_sol

        if buy_sol < 0.01: return

        logger.info(f"➕ [准备加仓] {token_address} | 金额: {buy_sol:.3f} SOL")

        # === 真实买入 ===
        tx_sig, token_got_raw = await self._jupiter_swap(
            input_mint=WSOL_MINT,
            output_mint=token_address,
            amount_in_ui=buy_sol,
            slippage_bps=SLIPPAGE_BPS
        )

        if not tx_sig: return

        # [关键修复] UI Amount 转换
        token_got_ui = token_got_raw / (10 ** pos.decimals)

        # 更新状态与均价 (一次计算即可)
        new_total_tokens = pos.total_tokens + token_got_ui
        pos.average_price = (pos.total_tokens * pos.average_price + buy_sol) / new_total_tokens
        pos.total_cost_sol += buy_sol
        pos.total_tokens = new_total_tokens

        pos.trade_records.append({
            "ts": time.time(),
            "type": "buy",
            "sol_spent": buy_sol,
            "sol_received": 0.0,
            "token_amount": token_got_ui,
            "note": "加仓",
            "pnl_sol": None,
        })
        # 份额分配
        hunter_addr = trigger_hunter['address']
        if hunter_addr in pos.shares:
            pos.shares[hunter_addr].token_amount += token_got_ui
        else:
            pos.shares[hunter_addr] = VirtualShare(hunter_addr, trigger_hunter.get('score', 0), token_got_ui)
            current_hunters_info = [{"address": h, "score": s.score} for h, s in pos.shares.items()]
            self._rebalance_shares_logic(pos, current_hunters_info)
        self._save_state_safe()

    async def execute_follow_sell(self, token_address: str, hunter_addr: str, sell_ratio: float, current_price: float):
        """跟随卖出逻辑。文档: 猎手卖出<5%不跟，跟随时我方至少卖该份额的 MIN_SELL_RATIO。"""
        if not self.keypair: return
        pos = self.positions.get(token_address)
        if not pos: return

        share = pos.shares.get(hunter_addr)
        if not share or share.token_amount <= 0: return

        # 猎手微调（卖出比例过小）不跟，避免噪音
        if sell_ratio < FOLLOW_SELL_THRESHOLD:
            logger.debug("跟随卖出跳过: 猎手卖出比例 %.1f%% < 阈值 %.0f%%", sell_ratio * 100, FOLLOW_SELL_THRESHOLD * 100)
            return

        actual_ratio = max(sell_ratio, MIN_SELL_RATIO)
        sell_amount_ui = share.token_amount * actual_ratio

        remaining = share.token_amount - sell_amount_ui
        is_dust = False
        if (remaining * current_price) < MIN_SHARE_VALUE_SOL:
            sell_amount_ui = share.token_amount
            is_dust = True

        logger.info(f"📉 [准备卖出] {token_address} | 数量: {sell_amount_ui:.2f}")

        # === 真实卖出 ===
        tx_sig, sol_got_ui = await self._jupiter_swap(
            input_mint=token_address,
            output_mint=WSOL_MINT,
            amount_in_ui=sell_amount_ui,
            slippage_bps=SLIPPAGE_BPS,
            is_sell=True,
            token_decimals=pos.decimals  # 传入正确的精度
        )

        if not tx_sig: return

        cost_this_sell = sell_amount_ui * pos.average_price
        pnl_sol = sol_got_ui - cost_this_sell
        pos.trade_records.append({
            "ts": time.time(),
            "type": "sell",
            "sol_spent": 0.0,
            "sol_received": sol_got_ui,
            "token_amount": sell_amount_ui,
            "note": "跟随卖出",
            "pnl_sol": pnl_sol,
        })
        pos.total_tokens -= sell_amount_ui
        share.token_amount -= sell_amount_ui
        if is_dust or share.token_amount <= 0:
            if hunter_addr in pos.shares:
                del pos.shares[hunter_addr]
        if pos.total_tokens <= 0:
            self._emit_position_closed(token_address, pos)
            del self.positions[token_address]
        self._save_state_safe()

    async def check_pnl_and_stop_profit(self, token_address: str, current_price_ui: float):
        """止盈逻辑"""
        if not self.keypair: return
        pos = self.positions.get(token_address)
        if not pos or pos.total_tokens <= 0: return
        if pos.average_price <= 0:
            logger.warning("止盈跳过: 均价异常 %.6f", pos.average_price)
            return

        pnl_pct = (current_price_ui - pos.average_price) / pos.average_price

        for level, sell_pct in TAKE_PROFIT_LEVELS:
            if pnl_pct >= level and level not in pos.tp_hit_levels:
                sell_amount = pos.total_tokens * sell_pct
                logger.info(f"💰 [止盈触发] {token_address} (+{pnl_pct * 100:.0f}%) | 卖出 {sell_amount:.2f}")

                # === 真实卖出 ===
                decimals = await self._get_decimals(token_address)
                tx_sig, sol_received = await self._jupiter_swap(
                    input_mint=token_address,
                    output_mint=WSOL_MINT,
                    amount_in_ui=sell_amount,
                    slippage_bps=SLIPPAGE_BPS,
                    is_sell=True,
                    token_decimals=decimals
                )

                if tx_sig:
                    cost_this_sell = sell_amount * pos.average_price
                    pnl_sol = sol_received - cost_this_sell
                    pos.trade_records.append({
                        "ts": time.time(),
                        "type": "sell",
                        "sol_spent": 0.0,
                        "sol_received": sol_received,
                        "token_amount": sell_amount,
                        "note": f"止盈{sell_pct * 100:.0f}%",
                        "pnl_sol": pnl_sol,
                    })
                    for share in pos.shares.values():
                        share.token_amount *= (1.0 - sell_pct)
                    pos.total_tokens -= sell_amount
                    pos.tp_hit_levels.add(level)
                    if pos.total_tokens <= 0:
                        self._emit_position_closed(token_address, pos)
                        del self.positions[token_address]
                self._save_state_safe()

    async def _jupiter_swap(self, input_mint: str, output_mint: str, amount_in_ui: float, slippage_bps: int,
                            is_sell: bool = False, token_decimals: int = 9) -> Tuple[Optional[str], float]:
        """
        通用 Swap 函数 (Jupiter v6 + Helius 广播，Auto 优先费)。
        开仓/加仓/跟随卖出/止盈均调用此方法，必须为类方法不可嵌套。
        """
        try:
            if not is_sell:
                amount_int = int(amount_in_ui * LAMPORTS_PER_SOL)
            else:
                amount_int = int(amount_in_ui * (10 ** token_decimals))

            quote_params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_int),
                "slippageBps": slippage_bps
            }
            quote_resp = await self.http_client.get(JUPITER_QUOTE_API, params=quote_params)
            if quote_resp.status_code != 200:
                logger.error("Quote Error: %s", quote_resp.text)
                return None, 0

            quote_data = quote_resp.json()
            out_amount_raw = int(quote_data.get("outAmount", 0))

            swap_payload = {
                "userPublicKey": str(self.keypair.pubkey()),
                "quoteResponse": quote_data,
                "wrapAndUnwrapSol": True,
                "prioritizationFeeLamports": PRIORITY_FEE_SETTINGS
            }
            swap_resp = await self.http_client.post(JUPITER_SWAP_API, json=swap_payload)
            if swap_resp.status_code != 200:
                logger.error("Swap Build Error: %s", swap_resp.text)
                return None, 0

            swap_data = swap_resp.json()
            swap_transaction_base64 = swap_data.get("swapTransaction")
            raw_tx = base64.b64decode(swap_transaction_base64)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signature = self.keypair.sign_message(tx.message.to_bytes_versioned(tx.message))
            signed_tx = VersionedTransaction.populate(tx.message, [signature])
            opts = TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
            sig = await self.rpc_client.send_raw_transaction(bytes(signed_tx), opts=opts)
            logger.info("⏳ 交易已广播: %s", sig)
            await asyncio.sleep(5)

            if not is_sell:
                return str(sig), out_amount_raw
            return str(sig), out_amount_raw / LAMPORTS_PER_SOL
        except Exception:
            logger.exception("Swap Exception")
            return None, 0

    async def _get_decimals(self, mint_address: str) -> int:
        """获取代币精度"""
        # 可以缓存这个结果
        try:
            # 简易实现：使用 get_token_supply
            pubkey = Pubkey.from_string(mint_address)
            resp = await self.rpc_client.get_token_supply(pubkey)
            return resp.value.decimals
        except Exception:
            logger.exception("获取 decimals 失败，使用默认 6")
            return 6  # 默认兜底

    # 辅助: 份额分配 (逻辑同前)
    def _rebalance_shares_logic(self, pos: Position, hunters: List[Dict]):
        # ... (保持之前的代码不变) ...
        count = len(hunters)
        if count == 0: return
        active_hunters = hunters[:3]
        total_tokens = pos.total_tokens
        new_shares = {}
        if len(active_hunters) >= 3:
            share_amt = total_tokens / 3.0
            for h in active_hunters:
                new_shares[h['address']] = VirtualShare(h['address'], h.get('score', 0), share_amt)
        else:
            total_score = sum(h.get('score', 0) for h in active_hunters)
            if total_score == 0: total_score = 1
            for h in active_hunters:
                ratio = h.get('score', 0) / total_score
                new_shares[h['address']] = VirtualShare(h['address'], h.get('score', 0), total_tokens * ratio)
        pos.shares = new_shares

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
            "tp_hit_levels": list(pos.tp_hit_levels),
            "shares": {
                addr: {"hunter": s.hunter, "score": s.score, "token_amount": s.token_amount}
                for addr, s in pos.shares.items()
            },
            "trade_records": list(pos.trade_records),
        }

    def _dict_to_position(self, d: Dict[str, Any]) -> Position:
        """从 dict 恢复 Position。"""
        pos = Position(
            d["token_address"],
            float(d.get("average_price", 0)),
            int(d.get("decimals", 9)),
        )
        pos.entry_time = float(d.get("entry_time", 0))
        pos.total_tokens = float(d.get("total_tokens", 0))
        pos.total_cost_sol = float(d.get("total_cost_sol", 0))
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
        """将当前持仓写入本地文件，失败只打日志。"""
        try:
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

    def save_state(self) -> None:
        """公开方法：持久化当前持仓到 data/trader_state.json。"""
        self._save_state_safe()

    def load_state(self) -> None:
        """从 data/trader_state.json 恢复持仓，启动时调用。"""
        if not TRADER_STATE_PATH.exists():
            return
        try:
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
