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
)
from config.paths import DATA_ACTIVE_DIR
from utils.logger import get_logger, LOGS_ROOT
from src.dexscreener.dex_scanner import DexScanner
from services.hunter_agent import HunterAgentController
from services.hunter_monitor import HunterMonitorController
from services.trader import SolanaTrader
from src.rugcheck import risk_control
from services import notification
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


def _on_position_closed(snapshot: dict) -> None:
    """
    清仓回调：记入日志、后台线程写 closed_pnl、新线程发清仓邮件。
    不阻塞跟单主流程。closed_pnl_log.append 在锁内执行，避免多线程竞态。
    """
    token_address = snapshot["token_address"]
    entry_time = snapshot["entry_time"]
    trade_records = snapshot["trade_records"]
    total_pnl_sol = snapshot["total_pnl_sol"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    with _CLOSED_PNL_LOCK:
        closed_pnl_log.append({"date": today_str, "token": token_address, "pnl_sol": total_pnl_sol})
    threading.Thread(target=_save_closed_pnl_log, daemon=True).start()  # 不阻塞
    entry_time_str = datetime.fromtimestamp(entry_time).strftime("%Y-%m-%d %H:%M:%S") if entry_time else "-"
    notification.send_close_email(token_address, entry_time_str, trade_records, total_pnl_sol)


# =========================================
# 事件回调
# =========================================

# 黑名单校验用（main() 中注入 monitor.sm_searcher）
_sm_searcher_for_blacklist: list = []


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
    await trader.execute_entry(
        token, hunters, total_score, price, halve_position=halve,
        entry_liquidity_usd=entry_liq_usd,
    )
    pos = trader.positions.get(token)
    if not pos:
        return

    # 4. 首次跟单邮件（新线程发送，不阻塞）
    entry_time_str = datetime.fromtimestamp(pos.entry_time).strftime("%Y-%m-%d %H:%M:%S")
    hunters_summary = ", ".join(f"{h.get('address', '')}..({h.get('score', 0)})" for h in hunters[:5])
    notification.send_first_entry_email(
        token_address=token,
        entry_time=entry_time_str,
        buy_sol=pos.total_cost_sol,
        token_amount=pos.total_tokens,
        price_sol=price,
        hunters_summary=hunters_summary or "-",
    )

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

def _build_daily_report_from_history(trader_instance):
    """
    从 月度汇总 + 当月 trading_history + 当前持仓 + hunters.json 生成详细日报内容。
    仅加载少量数据（当月记录 + 若干月度 summary 文件），不占用大量内存。
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
    # 累计：各月度汇总 + 当月（history 经裁剪后仅为当月记录）
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

    # 猎手池数量（MODELA: hunters.json, MODELB: smart_money.json）
    hunter_pool_count = 0
    if HUNTER_POOL_PATH.exists():
        try:
            with open(HUNTER_POOL_PATH, "r", encoding="utf-8") as f:
                hunters_data = json.load(f)
            hunter_pool_count = len(hunters_data) if isinstance(hunters_data, dict) else 0
        except Exception:
            pass

    # 跟单猎手 TOP5：各月 hunter_pnl 合并 + 当月卖出
    hunter_pnl = {}
    for s in summaries:
        for addr, pnl in (s.get("hunter_pnl") or {}).items():
            if addr:
                hunter_pnl[addr] = hunter_pnl.get(addr, 0) + pnl
    for r in [x for x in history if x.get("type") == "sell" and x.get("pnl_sol") is not None]:
        addr = r.get("hunter_addr") or ""
        if addr:
            hunter_pnl[addr] = hunter_pnl.get(addr, 0) + (r.get("pnl_sol") or 0)
    top_hunters = sorted(hunter_pnl.items(), key=lambda x: -x[1])[:5]
    top_hunters = [(f"{addr[:12]}..", pnl, i + 1) for i, (addr, pnl) in enumerate(top_hunters)]

    # 今日明细
    today_details = []
    for r in today_records:
        ts = r.get("ts") or 0
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"
        typ = r.get("type", "")
        token = (r.get("token") or "")[:12]
        note = r.get("note", "")
        if typ == "buy":
            sol = r.get("sol_spent") or 0
            today_details.append(f"  [{time_str}] {token}.. 买入 {sol:.4f} SOL | {note}\n")
        else:
            pnl = r.get("pnl_sol")
            pnl_str = f" {pnl:+.4f} SOL" if pnl is not None else ""
            today_details.append(f"  [{time_str}] {token}.. 卖出 | {note}{pnl_str}\n")
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
        top_hunters=top_hunters,
        today_details=today_details,
    )
    return content


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
            # 日报生成含文件读写，放到线程池执行，不阻塞主流程与跟单
            content = await asyncio.to_thread(_build_daily_report_from_history, trader)
            notification.send_detailed_daily_report_email(content)
        except Exception:
            logger.exception("❌ 日报生成失败")


# =========================================
# 主入口
# =========================================

async def restore_agent_from_trader() -> None:
    """启动时根据已恢复的持仓，恢复 Agent 对每个代币的监控。"""
    for token_address, pos in trader.positions.items():
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
    trader.on_position_closed_callback = _on_position_closed
    trader.on_trade_recorded = append_trade_in_background  # 后台线程写入，不阻塞跟单
    await restore_agent_from_trader()
    def get_tracked_tokens():
        out = set(trader.positions.keys())
        out |= set(getattr(agent, "active_missions", {}).keys())
        return out

    monitor = HunterMonitorController(
        signal_callback=on_monitor_signal,
        tracked_tokens_getter=get_tracked_tokens,
        position_check=lambda t: t in trader.positions,
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

    if immediate_audit:
        await monitor.run_immediate_audit()

    await asyncio.gather(
        monitor.start(),
        agent.start(),
        pnl_monitor_loop(),
        liquidity_structural_check_loop(),
        daily_report_loop(),
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