#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/17/2026 9:22 PM
@File       : main.py
@Description: 
"""
import asyncio

from config.settings import PNL_CHECK_INTERVAL, HUNTER_ADD_THRESHOLD_SOL
from services.dexscreener.dex_scanner import DexScanner
from services.solana.hunter_agent import HunterAgentController
from services.solana.hunter_monitor import HunterMonitorController
from services.solana.trader import SolanaTrader
from utils.logger import get_logger

logger = get_logger("Main")

trader = SolanaTrader()
agent = HunterAgentController()
price_scanner = DexScanner()  # 用于查价格


# =========================================
# 事件回调处理
# =========================================

async def on_monitor_signal(signal):
    """
    [Monitor -> Trader] 发现开仓信号
    """
    token = signal['token_address']
    hunters = signal['hunters']
    total_score = signal['total_score']

    # 1. 查当前价格 (Trader 需要价格算买入量)
    price = await price_scanner.get_token_price(token)
    if not price:
        logger.error(f"无法获取 {token} 价格，取消开仓")
        return

    # 2. Trader 开仓
    await trader.execute_entry(token, hunters, total_score, price)

    # 3. Agent 启动监控
    hunter_addrs = [h['address'] for h in hunters]
    await agent.start_tracking(token, hunter_addrs)


async def on_agent_signal(signal):
    """
    [Agent -> Trader] 发现猎手异动
    """
    msg_type = signal['type']
    token = signal['token']
    hunter_addr = signal['hunter']

    # 查一次价格用于计算
    price = await price_scanner.get_token_price(token)
    if not price: return

    if msg_type == 'HUNTER_SELL':
        # 跟随卖出
        await trader.execute_follow_sell(token, hunter_addr, signal['sell_ratio'], price)

    elif msg_type == 'HUNTER_BUY':
        # 判断加仓量
        # Agent 发来的 add_amount 是 Token 数量
        add_amount_raw = signal['add_amount_raw']

        # 我们需要 decimals 才能算出 SOL 价值
        # 可以从 trader.positions 里拿 (如果我们持仓的话)
        pos = trader.positions.get(token)
        if pos:
            decimals = pos.decimals
        else:
            # 如果没持仓(极少见)，需要去查
            decimals = 6  # 假设

        add_amount_ui = add_amount_raw / (10 ** decimals)
        add_sol_value = add_amount_ui * price

        # 规则: 猎手加仓价值 > 1 SOL 时跟
        if add_sol_value >= HUNTER_ADD_THRESHOLD_SOL:
            # 构造猎手信息 (需要去 storage 查 score，这里简化处理)
            # 假设我们只关心这是个"有效加仓"
            hunter_info = {"address": hunter_addr, "score": 50}  # 这里的score最好从monitor拿

            await trader.execute_add_position(token, hunter_info, "猎手大额加仓", price)

            # 如果是新猎手，加入 Agent 监控
            await agent.add_hunter_to_mission(token, hunter_addr)


# =========================================
# 后台任务: 价格轮询与止盈
# =========================================

async def pnl_monitor_loop():
    """
    定期轮询所有持仓代币的价格，检查是否触发止盈
    """
    logger.info("💸 启动 PnL 监控循环...")
    while True:
        try:
            active_tokens = trader.get_active_tokens()
            if active_tokens:
                # 批量查价格 (DexScanner 需要实现 get_prices_batch 更好，这里循环查)
                for token in active_tokens:
                    price = await price_scanner.get_token_price(token)
                    if price:
                        await trader.check_pnl_and_stop_profit(token, price)
                    await asyncio.sleep(0.5)  # 防限流

        except Exception:
            logger.exception("PnL Loop Error")

        await asyncio.sleep(PNL_CHECK_INTERVAL)


# =========================================
# 主入口
# =========================================

async def main():
    # 1. 绑定回调
    monitor = HunterMonitorController(signal_callback=on_monitor_signal)
    agent.signal_callback = on_agent_signal

    # 2. 启动服务
    # 使用 gather 并发运行
    await asyncio.gather(
        monitor.start(),  # 负责发现
        agent.start(),  # 负责盯人
        pnl_monitor_loop()  # 负责止盈
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("主程序被用户中断")
