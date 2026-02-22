#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: MODELB 猎手挖掘器 - wallets.txt 榜单逐个分析
              复用 hunter_common 的高频、LP 检测；独立三维度分析与评分。
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Set, Tuple

import httpx

from config.settings import (
    DATA_MODELB_DIR,
    POOL_SIZE_LIMIT,
    WALLETS_TXT_PATH,
    SMART_MONEY_JSON_PATH,
    TRASH_WALLETS_TXT_PATH,
    SM_MODELB_TX_LIMIT,
    SM_MODELB_ENTRY_MIN_SCORE,
    SM_MODELB_ENTRY_MIN_PNL_RATIO,
    SM_MODELB_ENTRY_MIN_WIN_RATE,
    SM_MODELB_ENTRY_MIN_TOTAL_PROFIT_SOL,
    SM_MODELB_ENTRY_MIN_AVG_HOLD_SEC,
    SM_MODELB_ENTRY_MAX_DUST_COUNT,
    SM_MODELB_ENTRY_MIN_TRADE_COUNT,
    SM_MODELB_WALLET_ANALYZE_SLEEP_SEC,
)
from src.alchemy import alchemy_client
from src.helius import helius_client
from utils.logger import get_logger

from services.hunter_common import hunter_had_any_lp_anywhere, _is_frequent_trader_by_blocktimes
from services.modela import SmartMoneySearcher
from services.modelb.analyzer import analyze_wallet_modelb
from services.modelb.scoring import compute_hunter_score

logger = get_logger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _normalize_address(addr: str) -> str:
    """标准化 Solana 地址。"""
    if not addr or not isinstance(addr, str):
        return ""
    return addr.strip()


def _check_modelb_entry_risk(stats: dict) -> Tuple[bool, str]:
    """
    MODELB 入库风控评测：五项硬门槛，全部通过才可进入评分与入库判定。
    :return: (是否通过, 未通过时的原因，通过时为空)
    """
    pnl = stats.get("pnl_ratio", 0) or 0
    if pnl != float("inf") and pnl < SM_MODELB_ENTRY_MIN_PNL_RATIO:
        return False, f"盈亏比{pnl:.2f}<{SM_MODELB_ENTRY_MIN_PNL_RATIO}"
    wr = stats.get("win_rate", 0) or 0
    if wr < SM_MODELB_ENTRY_MIN_WIN_RATE:
        return False, f"胜率{wr:.1%}<{SM_MODELB_ENTRY_MIN_WIN_RATE*100:.0f}%"
    profit = stats.get("total_profit", 0) or 0
    if profit <= SM_MODELB_ENTRY_MIN_TOTAL_PROFIT_SOL:
        return False, f"总盈利{profit:.2f}SOL≤{SM_MODELB_ENTRY_MIN_TOTAL_PROFIT_SOL}SOL"
    avg_hold = stats.get("avg_hold_sec")
    if avg_hold is None or avg_hold <= SM_MODELB_ENTRY_MIN_AVG_HOLD_SEC:
        h = f"{avg_hold/60:.1f}min" if avg_hold is not None else "无数据"
        return False, f"单币平均持仓{h}≤5min"
    dust = stats.get("dust_count", 0) or 0
    if dust >= SM_MODELB_ENTRY_MAX_DUST_COUNT:
        return False, f"灰尘代币数{dust}≥{SM_MODELB_ENTRY_MAX_DUST_COUNT}"
    count = stats.get("count", 0) or 0
    if count < SM_MODELB_ENTRY_MIN_TRADE_COUNT:
        return False, f"交易代币数{count}<{SM_MODELB_ENTRY_MIN_TRADE_COUNT}"
    return True, ""


class SmartMoneySearcherB:
    """
    MODELB 猎手挖掘器：从 wallets.txt 读取 GMGN 榜单，独立三维度分析评分后入库。
    """

    def __init__(self, dex_scanner=None):
        self.wallets_path = Path(WALLETS_TXT_PATH)
        self.smart_money_path = Path(SMART_MONEY_JSON_PATH)
        self.trash_path = Path(TRASH_WALLETS_TXT_PATH)
        self.tx_limit = SM_MODELB_TX_LIMIT
        self.pool_limit = POOL_SIZE_LIMIT
        self.dex_scanner = dex_scanner
        self._modela_searcher = SmartMoneySearcher()

    def _ensure_data_dir(self) -> None:
        DATA_MODELB_DIR.mkdir(parents=True, exist_ok=True)

    def load_wallets_from_file(self) -> List[str]:
        if not self.wallets_path.exists():
            logger.warning("⚠️ wallets.txt 不存在，路径: %s", self.wallets_path)
            return []
        try:
            raw = self.wallets_path.read_text(encoding="utf-8")
            addrs = [
                _normalize_address(line)
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            addrs = [a for a in addrs if len(a) >= 32]
            logger.info("📂 从 wallets.txt 加载 %d 个钱包地址", len(addrs))
            return addrs
        except Exception:
            logger.exception("加载 wallets.txt 失败")
            return []

    def load_smart_money(self) -> Dict[str, dict]:
        self._ensure_data_dir()
        if not self.smart_money_path.exists():
            return {}
        try:
            with open(self.smart_money_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.exception("加载 smart_money.json 失败")
            return {}

    def load_trash_wallets(self) -> Set[str]:
        self._ensure_data_dir()
        if not self.trash_path.exists():
            return set()
        try:
            raw = self.trash_path.read_text(encoding="utf-8")
            return {_normalize_address(line) for line in raw.splitlines() if line.strip()}
        except Exception:
            logger.exception("加载 trash_wallets.txt 失败")
            return set()

    def _save_smart_money(self, hunters: Dict[str, dict]) -> None:
        self._ensure_data_dir()
        try:
            with open(self.smart_money_path, "w", encoding="utf-8") as f:
                json.dump(hunters, f, indent=4, ensure_ascii=False)
        except Exception:
            logger.exception("保存 smart_money.json 失败")

    def _add_to_trash(self, address: str, trash_set: Set[str]) -> None:
        if _normalize_address(address) in trash_set:
            return
        self._ensure_data_dir()
        trash_set.add(_normalize_address(address))
        try:
            with open(self.trash_path, "a", encoding="utf-8") as f:
                f.write(address.strip() + "\n")
        except Exception:
            logger.exception("追加 trash_wallets.txt 失败")

    def _remove_from_wallets_file(self, addrs_to_remove: Set[str]) -> None:
        if not addrs_to_remove or not self.wallets_path.exists():
            return
        to_remove = {_normalize_address(a) for a in addrs_to_remove}
        try:
            raw = self.wallets_path.read_text(encoding="utf-8")
            lines = raw.splitlines()
            kept = []
            removed_count = 0
            for line in lines:
                if line.strip().startswith("#") or not line.strip():
                    kept.append(line)
                    continue
                a = _normalize_address(line)
                if a and a in to_remove:
                    removed_count += 1
                    continue
                kept.append(line)
            if removed_count > 0:
                self.wallets_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                logger.info("📝 [MODELB] 已从 wallets.txt 移除 %d 个已分析地址", removed_count)
        except Exception:
            logger.exception("更新 wallets.txt 失败")

    async def get_signatures(self, client, address: str, limit: int = 100, before: str = None):
        return await alchemy_client.get_signatures_for_address(
            address, limit=limit, before=before, http_client=client
        )

    async def fetch_parsed_transactions(self, client, signatures: list) -> list:
        if not signatures:
            return []
        return await helius_client.fetch_parsed_transactions(signatures, http_client=client)

    async def _analyze_wallet(
        self, client, address: str, trash_set: Set[str]
    ) -> dict | None:
        sigs = await self.get_signatures(client, address, limit=self.tx_limit)
        if not sigs:
            return None
        if _is_frequent_trader_by_blocktimes(sigs):
            logger.info("⏭️ [MODELB] 剔除频繁交易地址 %s..", address[:12])
            self._add_to_trash(address, trash_set)
            return None
        txs = await self.fetch_parsed_transactions(client, sigs)
        if not txs:
            return None
        if hunter_had_any_lp_anywhere(txs):
            logger.warning("⚠️ [MODELB] LP 淘汰: %s.. 扔入 trash", address[:12])
            self._add_to_trash(address, trash_set)
            return None

        if not self.dex_scanner:
            from src.dexscreener.dex_scanner import DexScanner
            self.dex_scanner = DexScanner()

        stats = await analyze_wallet_modelb(txs, address, self.dex_scanner)
        if stats is None:
            return None

        # 1. 风控评测：五项硬门槛，未通过则直接淘汰
        risk_ok, risk_reason = _check_modelb_entry_risk(stats)
        if not risk_ok:
            logger.info("[MODELB 风控未过] %s.. | %s", address[:12], risk_reason)
            return None

        # 2. 评分
        score_result = compute_hunter_score(stats)
        final_score = score_result["score"]

        # 3. 判定入库：分数达标
        if final_score < SM_MODELB_ENTRY_MIN_SCORE:
            logger.info(
                "[MODELB 落榜] %s.. | 分: %.1f < %s | %s",
                address[:12], final_score, SM_MODELB_ENTRY_MIN_SCORE,
                score_result.get("scores_detail", ""),
            )
            return None

        now = time.time()
        pnl = stats.get("pnl_ratio", 0)
        pnl_str = "∞" if pnl == float("inf") else f"{pnl:.2f}"
        return {
            "address": address,
            "score": round(final_score, 1),
            "win_rate": f"{stats['win_rate']:.1%}",
            "pnl_ratio": pnl_str,
            "total_profit": f"{stats['total_profit']:.2f} SOL",
            "avg_roi_pct": f"{stats.get('avg_roi_pct', 0):.1f}%",
            "trade_frequency": stats.get("trade_frequency", 0),
            "profit_dim": round(score_result.get("profit_dim", 0), 1),
            "persist_dim": round(score_result.get("persist_dim", 0), 1),
            "auth_dim": round(score_result.get("auth_dim", 0), 1),
            "scores_detail": score_result.get("scores_detail", ""),
            "dust_ratio": f"{stats.get('dust_ratio', 0):.1%}",
            "closed_ratio": f"{stats.get('closed_ratio', 0):.1%}",
            "last_active": now,
            "last_audit": now,
            "source": "MODELB_GMGN",
        }

    async def run_pipeline(self) -> List[dict]:
        logger.info("🕵️ [MODELB] 启动 SM 榜单挖掘（wallets.txt → smart_money.json）")
        wallets = self.load_wallets_from_file()
        if not wallets:
            return []

        smart_money = self.load_smart_money()
        trash_set = self.load_trash_wallets()
        smart_keys = {_normalize_address(k) for k in smart_money}
        trash_norm = {_normalize_address(t) for t in trash_set}
        to_process = [
            w for w in wallets
            if _normalize_address(w) not in smart_keys and _normalize_address(w) not in trash_norm
        ]
        if not to_process:
            return []

        logger.info("📋 待分析 %d 个新钱包", len(to_process))
        new_hunters: List[dict] = []
        processed_addrs: Set[str] = set()

        async with httpx.AsyncClient() as client:
            for addr in to_process:
                try:
                    result = await self._analyze_wallet(client, addr, trash_set)
                    processed_addrs.add(_normalize_address(addr))
                    if result:
                        key = _normalize_address(addr)
                        if len(smart_money) < self.pool_limit:
                            smart_money[key] = result
                            new_hunters.append(result)
                        else:
                            sorted_items = sorted(smart_money.items(), key=lambda i: float(i[1].get("score", 0) or 0))
                            lowest_key, lowest_info = sorted_items[0]
                            new_score = float(result.get("score", 0) or 0)
                            if new_score > float(lowest_info.get("score", 0) or 0):
                                del smart_money[lowest_key]
                                smart_money[key] = result
                                new_hunters.append(result)
                    await asyncio.sleep(SM_MODELB_WALLET_ANALYZE_SLEEP_SEC)
                except Exception:
                    logger.exception("分析钱包 %s 异常，跳过并从 wallets.txt 移除", addr[:12])
                    processed_addrs.add(_normalize_address(addr))

        self._save_smart_money(smart_money)
        self._remove_from_wallets_file(processed_addrs)
        return new_hunters

    def is_blacklisted(self, address: str) -> bool:
        """MODELB 仅用 trash_wallets 作为垃圾名单，与黑名单语义一致。"""
        trash = self.load_trash_wallets()
        return _normalize_address(address) in trash

    def add_to_trash(self, address: str) -> None:
        """将地址加入 trash_wallets（体检踢出 LP 等时调用）。"""
        trash = self.load_trash_wallets()
        self._add_to_trash(address, trash)

    async def analyze_hunter_performance(self, client, hunter_address, exclude_token=None, pre_fetched_txs=None):
        if pre_fetched_txs is None:
            sigs = await self.get_signatures(client, hunter_address, limit=self.tx_limit)
            if not sigs:
                return None
            txs = await self.fetch_parsed_transactions(client, sigs)
        else:
            txs = pre_fetched_txs
        if not txs:
            return None
        if hunter_had_any_lp_anywhere(txs):
            return {"_lp_detected": True}
        if not self.dex_scanner:
            from src.dexscreener.dex_scanner import DexScanner
            self.dex_scanner = DexScanner()
        return await analyze_wallet_modelb(txs, hunter_address, self.dex_scanner)

    async def is_frequent_trader(self, client, address: str) -> bool:
        return await self._modela_searcher.is_frequent_trader(client, address)
