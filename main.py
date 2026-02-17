#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/17/2026 9:22 PM
@File       : main.py
@Description: 
"""
import asyncio
import logging
from services.solana.hunter_monitor import HunterMonitorController

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Main")


# === 1. 定义你的信号接收处理函数 ===
async def on_resonance_signal(signal_data):
    """
    当 HunterMonitor 发现共振时，会自动调用这个方法
    """
    token_address = signal_data['token_address']
    total_score = signal_data['total_score']
    hunters = signal_data['hunters']

    print("\n" + "=" * 50)
    print(f"🚨 [主程序] 收到买入信号！")
    print(f"💎 标的代币: {token_address}")
    print(f"kB 共振强度: {total_score} 分")
    print(f"👥 跟随猎手: {[h['address'][:6] for h in hunters]}")
    print("=" * 50 + "\n")

    # TODO: 在这里调用你的交易模块 (Trader)
    # 例如:
    # await trader.buy(token_address, amount_sol=0.5)
    # logger.info(f"✅ 已自动执行买入: {token_address}")


# === 2. 启动程序 ===
async def main():
    # 初始化监控器，把上面的函数传进去
    # 这里的 signal_callback 参数就是关键
    monitor = HunterMonitorController(signal_callback=on_resonance_signal)

    logger.info("系统启动中...")

    # 启动监控循环 (这会一直运行，不会退出)
    await monitor.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("系统已停止")