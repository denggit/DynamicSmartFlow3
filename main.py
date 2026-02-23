#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 主入口。协调 Monitor/Agent/Trader，接入风控、邮件（开仓/清仓/日报）。
"""
import argparse
import asyncio
import json
import threading
from datetime import datetime
from pathlib import Path

from config.settings import (
    PNL_CHECK_INTERVAL,
    HUNTER_ADD_THRESHOLD_SOL,
    MAX_ENTRY_PUMP_MULTIPLIER,
    DAILY_REPORT_HOUR,
    POOL_SIZE_LIMIT,
    HUNTER_MODE,
    HUNTER_JSON_PATH,
    SMART_MONEY_JSON_PATH,
    DATA_DIR,
    DATA_MODELA_DIR,
    DATA_MODELB_DIR,
    CLOSED_PNL_PATH,
    PNL_LOOP_RATE_LIMIT_SLEEP_SEC,
    LIQUIDITY_STRUCTURAL_CHECK_INTERVAL_SEC,
    LIQUIDITY_COLLAPSE_THRESHOLD_USD,
    LIQUIDITY_DROP_RATIO,
    LIQUIDITY_CHECK_DEXSCREENER_INTERVAL_SEC,
    RECONCILE_INTERVAL_SEC,
    RECONCILE_TX_LIMIT,
    MANUAL_VERIFY_REPORT_INTERVAL_SEC,
)
from config.paths import DATA_ACTIVE_DIR
from utils.logger import get_logger, LOGS_ROOT
from src.dexscreener.dex_scanner import DexScanner
from services.hunter_agent import HunterAgentController
from services.hunter_monitor import HunterMonitorController
from services.trader import SolanaTrader
from src.rugcheck import risk_control
from services import notification
from services.manual_verify_store import add_manual_verify_token, get_and_clear_pending
from utils.trading_history import append_trade, append_trade_in_background, load_history, load_data_for_report

logger = get_logger("Main")
# 启动时首行显示当前模式、数据目录、日志目录，避免 MODELA/MODELB 混淆
logger.info("🦌 当前模式: %s | 数据: %s | 日志: %s", HUNTER_MODE or "MODELA", DATA_ACTIVE_DIR, LOGS_ROOT)

# 启动时确保 data、logs 及对应 modelA/modelB 目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_MODELA_DIR.mkdir(parents=True, exist_ok=True)
DATA_MODELB_DIR.mkdir(parents=True, exist_ok=True)
LOGS_ROOT.mkdir(parents=True, exist_ok=True)

trader = SolanaTrader()
trader.load_state()  # 启动时从本地恢复持仓
agent = HunterAgentController()
price_scanner = DexScanner()

# 清仓记录（兼容旧逻辑，日报已改用 trading_history.json）
closed_pnl_log = []
_CLOSED_PNL_LOCK = threading.Lock()  # 防止多线程同时写 closed_pnl.json 导致竞态丢失
# 猎手池文件：MODELA 用 hunters.json，MODELB 用 smart_money.json
HUNTER_POOL_PATH = Path(SMART_MONEY_JSON_PATH) if (HUNTER_MODE or "MODELA").strip().upper() == "MODELB" else Path(HUNTER_JSON_PATH)


def _load_closed_pnl_log() -> None:
    """从 data/modelA|modelB/closed_pnl.json 恢复历史清仓记录。"""
    global closed_pnl_log
    if not CLOSED_PNL_PATH.exists():
        return
    try:
        with open(CLOSED_PNL_PATH, "r", encoding="utf-8") as f:
            closed_pnl_log[:] = json.load(f)
        if closed_pnl_log:
            logger.info("📂 已从本地恢复 %s 条清仓记录", len(closed_pnl_log))
    except Exception:
        logger.exception("加载清仓记录失败")


def _save_closed_pnl_log() -> None:
    """将清仓记录写入本地（按模式放入 data/modelA/ 或 data/modelB/），避免重启后日报统计丢失。带锁防多线程竞态。"""
    with _CLOSED_PNL_LOCK:
        snapshot = list(closed_pnl_log)  # 在锁内复制，避免写时被并发修改
    try:
        CLOSED_PNL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CLOSED_PNL_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("保存清仓记录失败")


# 猎手连续亏损计数：达到 3 次触发即时体检
_hunter_consecutive_losses: dict = {}


def _on_position_closed_base(snapshot: dict) -> None:
    """
    清仓回调基逻辑：记入日志、后台线程写 closed_pnl。
    不阻塞跟单主流程。closed_pnl_log.append 在锁内执行，避免多线程竞态。
    清仓不再发邮件，周报中会汇总。
    """
    token_address = snapshot["token_address"]
    total_pnl_sol = snapshot["total_pnl_sol"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    with _CLOSED_PNL_LOCK:
        closed_pnl_log.append({"date": today_str, "token": token_address, "pnl_sol": total_pnl_sol})
    threading.Thread(target=_save_closed_pnl_log, daemon=True).start()  # 不阻塞


# =========================================
# 事件回调
# =========================================

# 黑名单校验用（main() 中注入 monitor.sm_searcher）
_sm_searcher_for_blacklist: list = []

# 跟仓买入失败且验证持仓确认失败时放弃的 token 集合。同一共振周期内不再重试，避免买在更高位置。
_entry_failed_tokens: set = set()


async def on_monitor_signal(signal):
    """[Monitor -> Trader] 发现开仓信号：风控 -> 开仓 -> 发首次跟单邮件 -> 启动 Agent。"""
    try:
        sm_searcher = _sm_searcher_for_blacklist[0] if _sm_searcher_for_blacklist else None
        await _on_monitor_signal_impl(signal, sm_searcher)
    except Exception:
        logger.exception("on_monitor_signal 处理异常，单次失败不影响主循环")


async def _on_monitor_signal_impl(signal, sm_searcher=None):
    """on_monitor_signal 实际逻辑，便于 try/except 隔离。"""
    token = signal["token_address"]
    hunters = signal["hunters"]
    # 黑名单二次过滤（老鼠仓等永不跟仓）
    if sm_searcher:
        hunters = [h for h in hunters if not sm_searcher.is_blacklisted(h.get("address", ""))]
    if not hunters:
        logger.warning("风控/黑名单过滤后无有效猎手，跳过开仓: %s", token)
        return
    total_score = signal["total_score"]

    # 1. 风控：避免貔貅/不能卖/高税；(can_buy, halve_position)
    can_buy, halve = await risk_control.check_is_safe_token(token)
    if not can_buy:
        logger.warning("风控未通过，跳过开仓: %s", token)
        return

    # 2. 价格
    price = await price_scanner.get_token_price(token)
    if price is None or price <= 0:
        logger.error("无法获取 %s 价格或价格为 0，取消开仓", token)
        return

    # 2.5 入场流动性（用于跟仓期结构风险：LP 撤池/净减 30% 即清仓）
    _, entry_liq_usd, _ = await risk_control.check_token_liquidity(token)

    # 3. 开仓（halve 时减半仓且禁止加仓）
    definitely_failed = await trader.execute_entry(
        token, hunters, total_score, price, halve_position=halve,
        entry_liquidity_usd=entry_liq_usd,
    )
    pos = trader.positions.get(token)
    if not pos:
        # 仅当确定从未广播（Quote/Swap 失败）时加入放弃集；已广播但验证失败则不加入，
        # 避免实际已买入却误放弃后续跟仓
        if definitely_failed is True:
            _entry_failed_tokens.add(token)
            logger.info("🚫 跟仓买入确定失败（Quote/Swap 未通过），放弃 %s（本周期不再重试）", token[:16] + "..")
        elif definitely_failed is False:
            add_manual_verify_token(token)
            logger.warning(
                "⚠️ 买入失败但可能已成交（RPC 验证超时），不加入放弃集：%s。"
                "若实际已持仓请手动处理或等待链上对账",
                token[:16] + "..",
            )
        else:
            # definitely_failed is None：未尝试（keypair/已有仓位/无猎手等）
            logger.info("⏭️ 开仓未执行: %s（trader 已跳过，详见上方日志）", token[:16] + "..")
        return

    # 4. 买入不再发邮件，周报中会汇总

    # 5. Agent 启动监控（只跟单一个猎手）
    hunter_addrs = [h["address"] for h in hunters]
    await agent.start_tracking(token, hunter_addrs)


async def on_agent_signal(signal):
    """[Agent -> Trader] 猎手异动：跟随卖出或加仓。"""
    try:
        await _on_agent_signal_impl(signal)
    except Exception:
        logger.exception("on_agent_signal 处理异常，单次失败不影响主循环")


async def _on_agent_signal_impl(signal):
    """on_agent_signal 实际逻辑，便于 try/except 隔离。"""
    msg_type = signal["type"]
    token = signal["token"]
    hunter_addr = signal["hunter"]

    price = await price_scanner.get_token_price(token)
    if price is None:
        return  # 无法获取价格才跳过；price=0 时也应跟卖（退出）

    if msg_type == "HUNTER_SELL":
        await trader.execute_follow_sell(token, hunter_addr, signal["sell_ratio"], price)
        if token not in trader.positions:
            await trader.ensure_fully_closed(token)  # 关监控前校验链上归零，未归零则清仓
            await agent.stop_tracking(token)

    elif msg_type == "HUNTER_BUY":
        pos = trader.positions.get(token)
        if not pos:
            return
        # 减半仓开仓的持仓不允许加仓（流动性/FDV/分数触达风控减半门槛）
        if getattr(pos, "no_addon", False):
            logger.info("🚫 加仓跳过: %s 为减半仓持仓，禁止加仓", token[:8])
            return
        # 首买追高限制：入场后已涨 300% 则不加仓
        if pos.average_price > 0 and price >= pos.average_price * MAX_ENTRY_PUMP_MULTIPLIER:
            logger.info("🚫 加仓跳过: %s 已涨 %.0f%% 不追高", token[:8], (price / pos.average_price - 1) * 100)
            return
        add_amount_ui = signal.get("add_amount_ui")
        if add_amount_ui is None:
            decimals = pos.decimals if pos.decimals is not None and pos.decimals > 0 else 9
            add_amount_ui = signal.get("add_amount_raw", 0) / (10 ** decimals)
        add_sol_value = add_amount_ui * price
        if add_sol_value >= HUNTER_ADD_THRESHOLD_SOL:
            hunter_info = {"address": hunter_addr, "score": pos.lead_hunter_score}
            await trader.execute_add_position(token, hunter_info, "猎手大额加仓", price)
            await agent.add_hunter_to_mission(token, hunter_addr)


# =========================================
# 后台任务：止盈循环
# =========================================

async def pnl_monitor_loop():
    """定期轮询持仓价格，触发止盈。"""
    logger.info("💸 启动 PnL 监控循环...")
    while True:
        try:
            active_tokens = trader.get_active_tokens()
            if active_tokens:
                for token in active_tokens:
                    price = await price_scanner.get_token_price(token)
                    if price is not None:  # 含 0：代币归零时也应触发止损
                        await trader.check_pnl_and_stop_profit(token, price)
                        if token not in trader.positions:
                            await trader.ensure_fully_closed(token)  # 关监控前校验链上归零，未归零则清仓
                            await agent.stop_tracking(token)
                    await asyncio.sleep(PNL_LOOP_RATE_LIMIT_SLEEP_SEC)
        except Exception:
            logger.exception("PnL Loop Error")
        await asyncio.sleep(PNL_CHECK_INTERVAL)


# =========================================
# 后台任务：流动性结构风险检查（买入后 LP 撤池/净减 30% 即清仓）
# =========================================

async def liquidity_structural_check_loop():
    """
    每 60 分钟调用 DexScreener 查持仓代币流动性。
    若流动性 < 100U（REMOVE LIQUIDITY 等价）或 当前 < 入场×0.7（LP 净减 30%），立即清仓。
    兜底买前风险放任的一种手段。
    """
    logger.info(
        "🛡️ 启动流动性结构风险监控（每 %d 分钟）...",
        LIQUIDITY_STRUCTURAL_CHECK_INTERVAL_SEC // 60,
    )
    while True:
        try:
            await asyncio.sleep(LIQUIDITY_STRUCTURAL_CHECK_INTERVAL_SEC)
            active = trader.get_active_tokens()
            if not active:
                continue
            dex_interval = max(1.0, LIQUIDITY_CHECK_DEXSCREENER_INTERVAL_SEC)
            for token in active:
                pos = trader.positions.get(token)
                if not pos or pos.total_tokens <= 0:
                    continue
                _, curr_liq_usd, _ = await risk_control.check_token_liquidity(token)
                entry_liq = getattr(pos, "entry_liquidity_usd", 0.0)

                # 条件1：流动性崩塌（REMOVE LIQUIDITY 等价）
                if curr_liq_usd < LIQUIDITY_COLLAPSE_THRESHOLD_USD:
                    logger.warning(
                        "🛑 [结构风险] %s 流动性崩塌 ($%.0f < $%.0f)，触发清仓",
                        token[:16] + "..", curr_liq_usd, LIQUIDITY_COLLAPSE_THRESHOLD_USD,
                    )
                    ok = await trader.force_close_position_for_structural_risk(
                        token, "流动性结构风险(REMOVE_LIQUIDITY)"
                    )
                    if ok and token not in trader.positions:
                        await trader.ensure_fully_closed(token)
                        await agent.stop_tracking(token)
                    await asyncio.sleep(dex_interval)
                    continue

                # 条件2：LP 净减少 30%
                if entry_liq > 0 and curr_liq_usd < entry_liq * LIQUIDITY_DROP_RATIO:
                    logger.warning(
                        "🛑 [结构风险] %s 流动性净减 %.0f%% (入场 $%.0f -> 当前 $%.0f)，触发清仓",
                        token[:16] + "..",
                        (1 - curr_liq_usd / entry_liq) * 100,
                        entry_liq,
                        curr_liq_usd,
                    )
                    ok = await trader.force_close_position_for_structural_risk(
                        token, "流动性结构风险(LP净减30%+)"
                    )
                    if ok and token not in trader.positions:
                        await trader.ensure_fully_closed(token)
                        await agent.stop_tracking(token)
                # 每查完一个代币后间隔，避免 DexScreener 请求过密被管控
                await asyncio.sleep(dex_interval)
        except Exception:
            logger.exception("流动性结构风险检查异常")
        # 循环末尾无 sleep，因开头已 sleep 60 分钟


# =========================================
# 后台任务：每日日报（从 trading_history.json 读取，仅日报时读）
# =========================================

async def _build_daily_report_from_history(trader_instance, price_scanner_instance):
    """
    从 月度汇总 + 当月 trading_history + 当前持仓 + hunters.json 生成详细日报内容。
    含今日/累计猎手盈利亏损 TOP5、未清仓持仓估值。price_scanner 用于拉取当前价格。
    """
    history, summaries = load_data_for_report()  # 内部会先做月度汇总与裁剪
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_records = [r for r in history if r.get("date") == today_str]
    sell_today = [r for r in today_records if r.get("type") == "sell" and r.get("pnl_sol") is not None]

    # 今日交易代币数、当前持仓、今日结算数
    today_tokens = set(r.get("token", "") for r in today_records if r.get("token"))
    today_tokens_traded = len(today_tokens)
    today_tokens_held = len([t for t, p in trader_instance.positions.items() if p.total_tokens > 0])
    today_tokens_settled = len(set(r.get("token") for r in sell_today if r.get("token")))

    today_pnl = sum(r.get("pnl_sol") or 0 for r in sell_today)
    # 累计：各月度汇总 + 当月
    month_sells = [r for r in history if r.get("type") == "sell" and r.get("pnl_sol") is not None]
    total_pnl = sum(s.get("total_pnl", 0) for s in summaries) + sum(r.get("pnl_sol", 0) for r in month_sells)
    total_trades = sum(s.get("total_trades", 0) for s in summaries) + len(history)

    # 今日平均收益、胜负单、盈亏比
    today_win_count = sum(1 for r in sell_today if (r.get("pnl_sol") or 0) > 0)
    today_loss_count = sum(1 for r in sell_today if (r.get("pnl_sol") or 0) < 0)
    today_wins = sum(r.get("pnl_sol", 0) for r in sell_today if (r.get("pnl_sol") or 0) > 0)
    today_losses = sum(-(r.get("pnl_sol", 0)) for r in sell_today if (r.get("pnl_sol") or 0) < 0)
    today_profit_factor = today_wins / today_losses if today_losses > 0 else (float("inf") if today_wins > 0 else 0)
    today_avg_roi_pct = 0.0
    if sell_today:
        costs = []
        for r in sell_today:
            amt = r.get("token_amount") or 0
            pr = r.get("price") or 0
            if amt > 0 and pr > 0:
                costs.append(amt * pr)
        if costs:
            today_avg_roi_pct = (today_pnl / sum(costs)) * 100 if sum(costs) > 0 else 0

    # 猎手池数量
    hunter_pool_count = 0
    if HUNTER_POOL_PATH.exists():
        try:
            with open(HUNTER_POOL_PATH, "r", encoding="utf-8") as f:
                hunters_data = json.load(f)
            hunter_pool_count = len(hunters_data) if isinstance(hunters_data, dict) else 0
        except Exception:
            pass

    # 今日猎手盈亏（按猎人合并多 token）
    daily_hunter_pnl = {}
    for r in sell_today:
        addr = r.get("hunter_addr") or ""
        if addr:
            daily_hunter_pnl[addr] = daily_hunter_pnl.get(addr, 0) + (r.get("pnl_sol") or 0)
    daily_profit_top5 = sorted(
        [(a, p) for a, p in daily_hunter_pnl.items() if p > 0],
        key=lambda x: -x[1],
    )[:5]
    daily_loss_top5 = sorted(
        [(a, p) for a, p in daily_hunter_pnl.items() if p < 0],
        key=lambda x: x[1],
    )[:5]
    daily_profit_top5 = [(a, p) for a, p in daily_profit_top5]
    daily_loss_top5 = [(a, p) for a, p in daily_loss_top5]

    # 累计猎手盈亏
    hunter_pnl = {}
    for s in summaries:
        for addr, pnl in (s.get("hunter_pnl") or {}).items():
            if addr:
                hunter_pnl[addr] = hunter_pnl.get(addr, 0) + pnl
    for r in [x for x in history if x.get("type") == "sell" and x.get("pnl_sol") is not None]:
        addr = r.get("hunter_addr") or ""
        if addr:
            hunter_pnl[addr] = hunter_pnl.get(addr, 0) + (r.get("pnl_sol") or 0)
    overall_profit_top5 = sorted(
        [(a, p) for a, p in hunter_pnl.items() if p > 0],
        key=lambda x: -x[1],
    )[:5]
    overall_loss_top5 = sorted(
        [(a, p) for a, p in hunter_pnl.items() if p < 0],
        key=lambda x: x[1],
    )[:5]
    overall_profit_top5 = [(a, p) for a, p in overall_profit_top5]
    overall_loss_top5 = [(a, p) for a, p in overall_loss_top5]

    # 未清仓持仓估值：按猎手合并（仅当能获取价格时计入），带代币名称
    unrealized_by_hunter = []
    if trader_instance.positions and price_scanner_instance:
        hunter_cost: dict = {}
        hunter_value: dict = {}
        hunter_tokens: dict = {}  # addr -> [symbol or addr, ...]
        tokens_to_fetch = [
            (t, p) for t, p in trader_instance.positions.items()
            if p and p.total_tokens > 0
        ]
        for token_address, pos in tokens_to_fetch:
            try:
                price, symbol = await price_scanner_instance.get_token_price_and_symbol(token_address)
                await asyncio.sleep(0.3)  # 避免 DexScreener 限流
            except Exception:
                price, symbol = None, None
            if not price or price <= 0:
                continue
            token_label = symbol or token_address
            total_cost = pos.total_cost_sol
            total_tokens = pos.total_tokens
            for addr, share in pos.shares.items():
                if share.token_amount <= 0:
                    continue
                ratio = share.token_amount / total_tokens
                cost = ratio * total_cost
                value = share.token_amount * price
                hunter_cost[addr] = hunter_cost.get(addr, 0) + cost
                hunter_value[addr] = hunter_value.get(addr, 0) + value
                hunter_tokens.setdefault(addr, []).append(token_label)
        for addr in hunter_cost:
            c = hunter_cost[addr]
            v = hunter_value.get(addr, 0)
            tokens_str = ", ".join(dict.fromkeys(hunter_tokens.get(addr, [])))  # 去重保序
            unrealized_by_hunter.append((addr, tokens_str, c, v, v - c))
        unrealized_by_hunter.sort(key=lambda x: -x[4])  # 按浮盈排序

    # 今日明细：拉取代币 symbol，展示完整地址与名称
    token_symbol_cache: dict = {}
    today_token_addrs = list(set(r.get("token", "") for r in today_records if r.get("token")))
    for token_addr in today_token_addrs:
        if token_addr and token_addr not in token_symbol_cache:
            if price_scanner_instance:
                try:
                    _, sym = await price_scanner_instance.get_token_price_and_symbol(token_addr)
                    token_symbol_cache[token_addr] = sym or token_addr
                    await asyncio.sleep(0.3)
                except Exception:
                    token_symbol_cache[token_addr] = token_addr
            else:
                token_symbol_cache[token_addr] = token_addr
    today_details = []
    for r in today_records:
        ts = r.get("ts") or 0
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"
        typ = r.get("type", "")
        token_addr = r.get("token") or ""
        token_label = f"{token_symbol_cache.get(token_addr, token_addr)} ({token_addr})" if token_addr else "(未知)"
        note = r.get("note", "")
        if typ == "buy":
            sol = r.get("sol_spent") or 0
            today_details.append(f"  [{time_str}] {token_label} 买入 {sol:.4f} SOL | {note}\n")
        else:
            pnl = r.get("pnl_sol")
            pnl_str = f" {pnl:+.4f} SOL" if pnl is not None else ""
            today_details.append(f"  [{time_str}] {token_label} 卖出 | {note}{pnl_str}\n")
    if not today_details:
        today_details = ["(今日无交易)\n"]

    content = notification.build_detailed_daily_report(
        hunter_pool_count=hunter_pool_count,
        hunter_pool_limit=POOL_SIZE_LIMIT,
        today_tokens_traded=today_tokens_traded,
        today_tokens_held=today_tokens_held,
        today_tokens_settled=today_tokens_settled,
        today_pnl_sol=today_pnl,
        today_avg_roi_pct=today_avg_roi_pct,
        today_win_count=today_win_count,
        today_loss_count=today_loss_count,
        today_profit_factor=today_profit_factor,
        total_pnl_sol=total_pnl,
        total_trades=total_trades,
        top_hunters=[],
        today_details=today_details,
        daily_profit_top5=daily_profit_top5,
        daily_loss_top5=daily_loss_top5,
        overall_profit_top5=overall_profit_top5,
        overall_loss_top5=overall_loss_top5,
        unrealized_by_hunter=unrealized_by_hunter,
    )
    return content


async def reconcile_loop():
    """定期链上对账：检测手动清仓、同步 trader_state，补录 trading_history。"""
    logger.info("📋 链上对账任务已启动，每 %d 小时执行", RECONCILE_INTERVAL_SEC // 3600)
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SEC)
        try:
            synced_tokens, appended = await trader.reconcile_from_chain(
                tx_limit=RECONCILE_TX_LIMIT,
                on_trade_callback=append_trade_in_background,
            )
            for token in synced_tokens:
                await trader.ensure_fully_closed(token)
                await agent.stop_tracking(token)
            if synced_tokens or appended > 0:
                logger.info("📤 [链上对账] 完成：移除 %d 个已归零持仓，补录 %d 条交易", len(synced_tokens), appended)
        except Exception:
            logger.exception("链上对账异常")


async def daily_report_loop():
    """每天 DAILY_REPORT_HOUR 点发送详细日报（从 trading_history.json 读取）。"""
    logger.info("📊 日报任务已启动，每日 %s 点发送", DAILY_REPORT_HOUR)
    while True:
        now = datetime.now()
        next_run = now.replace(
            hour=DAILY_REPORT_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        if next_run <= now:
            from datetime import timedelta
            next_run += timedelta(days=1)
        wait_sec = (next_run - datetime.now()).total_seconds()
        await asyncio.sleep(max(1, wait_sec))

        try:
            # 日报生成含文件读写与价格拉取，使用 async 以支持 price_scanner
            content = await _build_daily_report_from_history(trader, price_scanner)
            notification.send_detailed_daily_report_email(content)
        except Exception:
            logger.exception("❌ 日报生成失败")


async def manual_verify_report_loop():
    """每小时汇总需手动核对的 token，发送邮件提醒用户手动卖出。"""
    logger.info("📋 需手动核对报告任务已启动，每 %d 分钟执行", MANUAL_VERIFY_REPORT_INTERVAL_SEC // 60)
    while True:
        await asyncio.sleep(MANUAL_VERIFY_REPORT_INTERVAL_SEC)
        try:
            addrs = get_and_clear_pending()
            if not addrs:
                continue
            tokens_with_names = []
            for addr in addrs:
                try:
                    _, symbol = await price_scanner.get_token_price_and_symbol(addr)
                    label = symbol or addr[:16] + ".."
                except Exception:
                    label = addr[:16] + ".."
                tokens_with_names.append((addr, label))
            content = notification.build_manual_verify_report(tokens_with_names)
            if content:
                notification.send_email_in_thread("⚠️ 需手动上链核对的代币（请手动卖出）", content)
                logger.info("📧 已发送需手动核对报告，共 %d 个代币", len(tokens_with_names))
        except Exception:
            logger.exception("需手动核对报告异常")


# =========================================
# 主入口
# =========================================

async def restore_agent_from_trader() -> None:
    """启动时根据已恢复的持仓，恢复 Agent 对每个代币的监控。"""
    for token_address, pos in list(trader.positions.items()):  # 副本迭代，避免 start_tracking 回调删持仓导致 RuntimeError
        if pos.total_tokens <= 0:
            continue
        hunter_addrs = list(pos.shares.keys())
        if hunter_addrs:
            await agent.start_tracking(token_address, hunter_addrs)
            logger.info("🔄 恢复监控: %s (%s 名猎手)", token_address, len(hunter_addrs))


def _migrate_closed_pnl_to_history():
    """将旧 closed_pnl.json 数据迁移到 trading_history，保证累计收益连续性。"""
    existing = load_history()
    existing_dates = {(r.get("date"), r.get("token"), r.get("note")) for r in existing}
    migrated = 0
    for e in closed_pnl_log:
        date, token, pnl = e.get("date"), e.get("token"), e.get("pnl_sol", 0)
        if not date or not token or (date, token, "历史迁移") in existing_dates:
            continue
        append_trade({
            "date": date,
            "ts": 0,
            "token": token,
            "type": "sell",
            "sol_spent": 0.0,
            "sol_received": pnl if pnl > 0 else 0,
            "token_amount": 0,
            "price": 0,
            "hunter_addr": "",
            "pnl_sol": pnl,
            "note": "历史迁移",
        })
        existing_dates.add((date, token, "历史迁移"))
        migrated += 1
    if migrated > 0:
        logger.info("📂 已迁移 %d 条历史清仓记录到 trading_history", migrated)


async def main(immediate_audit: bool = False):
    """主入口。HUNTER_MODE 在进程启动时确定，修改 .env 后需重启生效。"""
    _load_closed_pnl_log()
    await asyncio.to_thread(_migrate_closed_pnl_to_history)  # 后台线程迁移，不阻塞启动
    trader.on_trade_recorded = append_trade_in_background  # 后台线程写入，不阻塞跟单

    async def _on_hunter_zero_skip(token_address: str) -> None:
        """猎手持仓归零跳过监控时：校验我方链上是否归零，未归零则清仓；链上已归零或查询失败则同步移除 trader_state。"""
        await trader.ensure_fully_closed(token_address, remove_if_chain_unknown=True)

    agent.on_hunter_zero_skip = _on_hunter_zero_skip  # 必须在 restore_agent_from_trader 前设置，否则恢复时漏删过时持仓
    await restore_agent_from_trader()

    def get_tracked_tokens():
        out = set(trader.positions.keys())
        out |= set(getattr(agent, "active_missions", {}).keys())
        return out

    monitor = HunterMonitorController(
        signal_callback=on_monitor_signal,
        tracked_tokens_getter=get_tracked_tokens,
        position_check=lambda t: t in trader.positions or t in _entry_failed_tokens,
    )
    _sm_searcher_for_blacklist.append(monitor.sm_searcher)  # 供开仓前黑名单二次过滤
    monitor.set_agent(agent)  # 跟仓信号由 Monitor 统一推送，避免 Agent 自建 WS 漏单
    agent.signal_callback = on_agent_signal

    async def _on_helius_credit_exhausted():
        """Helius credit 耗尽（429）时的保命操作：清仓所有持仓 + 致命错误告警。"""
        closed = await trader.emergency_close_all_positions()
        logger.critical(
            "🚨 [致命错误] Helius credit 已耗尽（429），无法解析猎手交易！"
            "已紧急清仓 %d 个持仓，请立即检查 Helius 用量并充值。",
            closed,
        )

    monitor.set_on_helius_credit_exhausted(_on_helius_credit_exhausted)

    async def _on_hunter_removed(hunter_addr: str) -> None:
        """体检踢出猎手时：若该猎手正在跟仓，兜底清仓对应持仓。"""
        closed = await trader.emergency_close_positions_by_hunter(hunter_addr)
        if closed > 0:
            logger.warning("🛑 体检踢出猎手 %s..，已兜底清仓其 %d 个跟仓", hunter_addr[:12], closed)

    monitor.on_hunter_removed = _on_hunter_removed

    def _on_position_closed(snapshot: dict) -> None:
        """清仓回调：记日志 + 连续亏损 3 次即时时体检。"""
        _on_position_closed_base(snapshot)
        hunter_addrs = snapshot.get("hunter_addrs") or []
        total_pnl_sol = snapshot.get("total_pnl_sol", 0)
        if total_pnl_sol < 0:
            for addr in hunter_addrs:
                _hunter_consecutive_losses[addr] = _hunter_consecutive_losses.get(addr, 0) + 1
                if _hunter_consecutive_losses[addr] >= 3:
                    logger.warning("🩺 猎手 %s.. 连续 3 次亏损，触发即时体检", addr[:12])
                    asyncio.create_task(monitor.run_audit_for_hunter(addr))
                    _hunter_consecutive_losses[addr] = 0
        else:
            for addr in hunter_addrs:
                _hunter_consecutive_losses[addr] = 0

    trader.on_position_closed_callback = _on_position_closed

    if immediate_audit:
        await monitor.run_immediate_audit()

    await asyncio.gather(
        monitor.start(),
        agent.start(),
        pnl_monitor_loop(),
        liquidity_structural_check_loop(),
        reconcile_loop(),
        daily_report_loop(),
        manual_verify_report_loop(),
    )


def _parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="DynamicSmartFlow3 主程序")
    parser.add_argument(
        "--immediate-audit",
        action="store_true",
        help="启动时立即对猎手池做审计体检（MODELA: hunters.json；MODELB: smart_money.json；未达标踢出，其余更新）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(main(immediate_audit=args.immediate_audit))
    except KeyboardInterrupt:
        logger.info("主程序被用户中断")
    finally:
        # 程序退出时关闭 trader 的 RPC/HTTP 连接，避免资源泄露
        try:
            asyncio.run(trader.close())
        except Exception:
            logger.debug("trader.close() 忽略异常（可能已关闭）")