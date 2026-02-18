#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 主入口。协调 Monitor/Agent/Trader，接入风控、邮件（开仓/清仓/日报）。
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from config.settings import (
    PNL_CHECK_INTERVAL,
    HUNTER_ADD_THRESHOLD_SOL,
    DAILY_REPORT_HOUR,
    BASE_DIR,
)
from services.dexscreener.dex_scanner import DexScanner
from services.solana.hunter_agent import HunterAgentController
from services.solana.hunter_monitor import HunterMonitorController
from services.solana.trader import SolanaTrader
from services import risk_control
from services import notification
from utils.logger import get_logger

logger = get_logger("Main")

# 持仓与清仓记录持久化路径（程序挂掉后重启可恢复）
TRADER_STATE_DIR = BASE_DIR / "data"
CLOSED_PNL_PATH = TRADER_STATE_DIR / "closed_pnl.json"

trader = SolanaTrader()
trader.load_state()  # 启动时从本地恢复持仓
agent = HunterAgentController()
price_scanner = DexScanner()

# 清仓记录，用于日报统计；启动时从文件恢复
closed_pnl_log = []


def _load_closed_pnl_log() -> None:
    """从 data/closed_pnl.json 恢复历史清仓记录。"""
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
    """将清仓记录写入本地，避免重启后日报统计丢失。"""
    try:
        TRADER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CLOSED_PNL_PATH, "w", encoding="utf-8") as f:
            json.dump(closed_pnl_log, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("保存清仓记录失败")


def _on_position_closed(snapshot: dict) -> None:
    """清仓回调：记入日志并起线程发清仓邮件，不阻塞主流程。"""
    token_address = snapshot["token_address"]
    entry_time = snapshot["entry_time"]
    trade_records = snapshot["trade_records"]
    total_pnl_sol = snapshot["total_pnl_sol"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    closed_pnl_log.append({"date": today_str, "token": token_address, "pnl_sol": total_pnl_sol})
    _save_closed_pnl_log()
    entry_time_str = datetime.fromtimestamp(entry_time).strftime("%Y-%m-%d %H:%M:%S") if entry_time else "-"
    notification.send_close_email(token_address, entry_time_str, trade_records, total_pnl_sol)


# =========================================
# 事件回调
# =========================================

async def on_monitor_signal(signal):
    """[Monitor -> Trader] 发现开仓信号：风控 -> 开仓 -> 发首次跟单邮件 -> 启动 Agent。"""
    token = signal["token_address"]
    hunters = signal["hunters"]
    total_score = signal["total_score"]

    # 1. 风控：避免貔貅/不能卖/高税
    if not await risk_control.check_is_safe_token(token):
        logger.warning("风控未通过，跳过开仓: %s", token)
        return

    # 2. 价格
    price = await price_scanner.get_token_price(token)
    if not price:
        logger.error("无法获取 %s 价格，取消开仓", token)
        return

    # 3. 开仓
    await trader.execute_entry(token, hunters, total_score, price)
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

    # 5. Agent 启动监控
    hunter_addrs = [h["address"] for h in hunters]
    await agent.start_tracking(token, hunter_addrs)


async def on_agent_signal(signal):
    """[Agent -> Trader] 猎手异动：跟随卖出或加仓。"""
    msg_type = signal["type"]
    token = signal["token"]
    hunter_addr = signal["hunter"]

    price = await price_scanner.get_token_price(token)
    if not price:
        return

    if msg_type == "HUNTER_SELL":
        await trader.execute_follow_sell(token, hunter_addr, signal["sell_ratio"], price)
        if token not in trader.positions:
            await agent.stop_tracking(token)

    elif msg_type == "HUNTER_BUY":
        add_amount_raw = signal["add_amount_raw"]
        pos = trader.positions.get(token)
        decimals = pos.decimals if pos else 6
        add_amount_ui = add_amount_raw / (10 ** decimals)
        add_sol_value = add_amount_ui * price
        if add_sol_value >= HUNTER_ADD_THRESHOLD_SOL:
            hunter_info = {"address": hunter_addr, "score": 50}
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
                    if price:
                        await trader.check_pnl_and_stop_profit(token, price)
                        if token not in trader.positions:
                            await agent.stop_tracking(token)
                    await asyncio.sleep(0.5)
        except Exception:
            logger.exception("PnL Loop Error")
        await asyncio.sleep(PNL_CHECK_INTERVAL)


# =========================================
# 后台任务：每日日报（独立逻辑，到点发邮件）
# =========================================

async def daily_report_loop():
    """每天 DAILY_REPORT_HOUR 点发送日报邮件（今日收益 + 累计收益，SOL）。"""
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

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_pnl = sum(e["pnl_sol"] for e in closed_pnl_log if e["date"] == today_str)
        total_pnl = sum(e["pnl_sol"] for e in closed_pnl_log)
        details = [f"  {e['date']} {e['token'][:12]}.. {e['pnl_sol']:+.4f} SOL\n" for e in closed_pnl_log if e["date"] == today_str]
        if not details:
            details = ["(今日无清仓记录)\n"]
        notification.send_daily_report_email(today_pnl, total_pnl, details)


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


async def main():
    _load_closed_pnl_log()
    trader.on_position_closed_callback = _on_position_closed
    await restore_agent_from_trader()
    monitor = HunterMonitorController(signal_callback=on_monitor_signal)
    monitor.set_agent(agent)  # 跟仓信号由 Monitor 统一推送，避免 Agent 自建 WS 漏单
    agent.signal_callback = on_agent_signal

    await asyncio.gather(
        monitor.start(),
        agent.start(),
        pnl_monitor_loop(),
        daily_report_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("主程序被用户中断")
