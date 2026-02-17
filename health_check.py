#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 全系统启动前自检脚本。异步执行，真实调用 RPC/Jupiter/风控/WebSocket/邮件，零污染。
用法: python health_check.py [--proxy]
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import traceback
from pathlib import Path

# 确保项目根在 path 且为 cwd
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

# Windows 控制台使用 UTF-8，以便正常输出 emoji
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 尽早加载 .env（后续步骤依赖）
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(ROOT) / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("HealthCheck")

# 步骤总数，用于 [n/N] 显示
TOTAL_STEPS = 7


async def test_configuration():
    """[1/7] 环境与配置：代理、.env、必要变量。"""
    logger.info("🛠️ [1/%d] 检查环境配置...", TOTAL_STEPS)
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        logger.info("✅ 检测到代理: %s", proxy)
    else:
        logger.info("☁️ 直连模式 (无代理)")

    if not Path(ROOT).joinpath(".env").is_file():
        logger.error("❌ 未找到 .env")
        return False

    helius_raw = os.getenv("HELIUS_API_KEY", "").strip()
    helius_keys = [k.strip() for k in helius_raw.split(",") if k.strip()]
    if not helius_keys:
        logger.error("❌ HELIUS_API_KEY 未配置（必填，可逗号分隔多个）")
        return False
    logger.info("✅ HELIUS_API_KEY 已配置（共 %d 个）", len(helius_keys))

    sol = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not sol:
        logger.warning("⚠️ SOLANA_PRIVATE_KEY 未配置（仅查价/监控可运行，无法真实交易）")
    else:
        logger.info("✅ SOLANA_PRIVATE_KEY 已配置")

    email_ok = all([os.getenv("EMAIL_SENDER"), os.getenv("EMAIL_PASSWORD"), os.getenv("EMAIL_RECEIVER")])
    if not email_ok:
        logger.warning("⚠️ 邮件未完整配置，将不发送开仓/清仓/日报")
    else:
        logger.info("✅ 邮件配置完整")

    jup_raw = os.getenv("JUP_API_KEY", "").strip()
    jup_keys = [k.strip() for k in jup_raw.split(",") if k.strip()]
    if jup_keys:
        logger.info("✅ JUP_API_KEY 已配置（共 %d 个）", len(jup_keys))
    else:
        logger.warning("⚠️ JUP_API_KEY 未配置（Jupiter 限流时可逗号分隔多个）")

    return True


async def test_rpc_and_jupiter():
    """[2/7] 真实 RPC 连接 + Jupiter 询价（与主程序一致路径）。"""
    logger.info("🔗 [2/%d] 测试 RPC 连接 & Jupiter 询价...", TOTAL_STEPS)
    try:
        from config.settings import (
            helius_key_pool,
            jup_key_pool,
            JUP_QUOTE_API,
            SOLANA_PRIVATE_KEY_BASE58,
        )
        from services.solana.trader import SolanaTrader
        from solders.keypair import Keypair

        trader = SolanaTrader()
        if not trader.keypair:
            logger.error("❌ 无法加载钱包，请检查 SOLANA_PRIVATE_KEY")
            await trader.close()
            return False

        rpc_url = helius_key_pool.get_rpc_url()
        logger.info("正在连接 RPC: %s...", rpc_url[:40] + "..")
        balance_resp = await trader.rpc_client.get_balance(trader.keypair.pubkey())
        balance_sol = balance_resp.value / 1_000_000_000
        logger.info("✅ RPC 连接成功 | 当前余额: %.4f SOL", balance_sol)

        # Jupiter v1 询价：0.1 SOL -> USDC
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        amount_lamports = int(0.1 * 1_000_000_000)
        params = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": USDC_MINT,
            "amount": str(amount_lamports),
            "slippageBps": 50,
        }
        headers = {"User-Agent": "DSF3-HealthCheck/1.0"}
        jup_key = jup_key_pool.get_api_key()
        if jup_key:
            headers["x-api-key"] = jup_key

        quote_resp = await trader.http_client.get(JUP_QUOTE_API, params=params, headers=headers)
        await trader.close()

        if quote_resp.status_code == 429:
            logger.warning("⚠️ Jupiter 限流 (429)，请稍后重试或配置 JUP_API_KEY")
            return False
        if quote_resp.status_code != 200:
            logger.error("❌ Jupiter 询价失败: HTTP %s %s", quote_resp.status_code, quote_resp.text[:200])
            return False

        data = quote_resp.json()
        out_amount = data.get("outAmount")
        if out_amount is not None:
            out_ui = int(out_amount) / 1_000_000  # USDC 6 decimals
            logger.info("✅ Jupiter 询价成功 | 0.1 SOL ≈ %.2f USDC", out_ui)
        else:
            logger.info("✅ Jupiter 询价返回 200（未解析 outAmount）")
        return True

    except Exception as e:
        logger.error("❌ RPC/Jupiter 测试异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_risk_control():
    """[3/7] 风控接口：DexScreener 流动性 + RugCheck 可选。"""
    logger.info("🛡️ [3/%d] 测试 DexScreener 风控接口...", TOTAL_STEPS)
    try:
        from services.risk_control import check_token_liquidity

        # 使用 JUP 代币作为已知有流动性的标的
        jup_mint = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
        has_pool, liq_usd, fdv = await check_token_liquidity(jup_mint)
        if has_pool and liq_usd > 0:
            logger.info("✅ DexScreener 连接成功 | JUP 流动性: $%s", f"{liq_usd:,.0f}")
            return True
        logger.error("❌ DexScreener 数据异常 (无池或流动性为 0)")
        return False
    except Exception as e:
        logger.error("❌ 风控检查异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_trader_state():
    """[4/7] Trader 状态加载与钱包一致性（不写入，只读）。"""
    logger.info("📂 [4/%d] 测试 Trader 状态加载...", TOTAL_STEPS)
    try:
        from config.settings import SOLANA_PRIVATE_KEY_BASE58, BASE_DIR
        from services.solana.trader import SolanaTrader
        from solders.keypair import Keypair

        if not SOLANA_PRIVATE_KEY_BASE58:
            logger.warning("⚠️ 未配置私钥，跳过 Trader 状态检查")
            return True

        trader = SolanaTrader()
        trader.load_state()
        # 仅验证能正常加载、不抛错；不写入
        kp = Keypair.from_base58_string(SOLANA_PRIVATE_KEY_BASE58)
        logger.info("✅ Trader 状态加载正常 | 钱包: %s...", str(kp.pubkey())[:16])
        await trader.close()
        return True
    except Exception as e:
        logger.error("❌ Trader 状态检查异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_websocket_and_helius_api():
    """[5/7] WebSocket 连接与 Helius HTTP API（地址交易列表）。"""
    logger.info("🔌 [5/%d] 测试 WebSocket & Helius API...", TOTAL_STEPS)
    try:
        from config.settings import (
            WSS_ENDPOINT,
            HELIUS_API_KEY,
            SOLANA_PRIVATE_KEY_BASE58,
        )
        import websockets

        if not WSS_ENDPOINT:
            logger.error("❌ WSS_ENDPOINT 为空（需配置 HELIUS_API_KEY）")
            return False

        # 1. WebSocket 连接与简单订阅
        logger.info("正在连接 WebSocket: %s...", WSS_ENDPOINT[:50] + "..")
        try:
            async with websockets.connect(WSS_ENDPOINT, ping_interval=20, ping_timeout=10) as ws:
                logger.info("✅ WebSocket 连接成功")
                # 可选：发送 slotSubscribe 确认通道畅通（不依赖钱包）
                sub_msg = {"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}
                await ws.send(json.dumps(sub_msg))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    data = json.loads(msg)
                    if "result" in data or "error" in data:
                        logger.info("✅ WebSocket 订阅响应正常")
                except asyncio.TimeoutError:
                    logger.info("✅ WebSocket 已连接（订阅响应超时可接受）")
        except websockets.exceptions.InvalidURI as e:
            logger.error("❌ WebSocket URI 无效: %s", e)
            return False
        except Exception as e:
            logger.error("❌ WebSocket 连接失败: %s", e)
            return False

        # 2. Helius HTTP API：地址交易（若有钱包）
        if not HELIUS_API_KEY:
            return True
        wallet = None
        if SOLANA_PRIVATE_KEY_BASE58:
            try:
                from solders.keypair import Keypair
                wallet = str(Keypair.from_base58_string(SOLANA_PRIVATE_KEY_BASE58).pubkey())
            except Exception:
                pass
        if not wallet:
            logger.info("✅ WebSocket 通过，跳过 Helius 地址 API（无钱包）")
            return True

        import httpx
        url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
        params = {"api-key": HELIUS_API_KEY, "limit": 1}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            logger.info("✅ Helius API 可达（地址交易列表正常）")
            return True
        if resp.status_code == 429:
            logger.warning("⚠️ Helius API 限流 (429)，服务可用")
            return True
        logger.error("❌ Helius API 请求失败: HTTP %s", resp.status_code)
        return False

    except Exception as e:
        logger.error("❌ WebSocket/Helius 测试异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_project_imports():
    """[6/7] 项目核心模块导入。"""
    logger.info("📦 [6/%d] 测试项目模块导入...", TOTAL_STEPS)
    try:
        from config.settings import helius_key_pool, jup_key_pool
        from services.dexscreener.dex_scanner import DexScanner
        from services.solana.trader import SolanaTrader
        from services import risk_control
        from services import notification
        from utils.logger import get_logger
        logger.info("✅ 项目模块导入正常 (config, services, utils)")
        return True
    except Exception as e:
        logger.error("❌ 项目导入失败: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_notification():
    """[7/7] 邮件发送（同步接口放线程执行）。"""
    logger.info("📧 [7/%d] 测试邮件发送...", TOTAL_STEPS)
    try:
        from services.notification import _send_email_sync
        from datetime import datetime

        subject = "DSF3 健康检查通过"
        content = "自检脚本运行成功，时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ok = await asyncio.to_thread(_send_email_sync, subject, content, None)
        if ok:
            logger.info("✅ 测试邮件发送成功")
            return True
        logger.warning("⚠️ 邮件未配置或发送失败（检查 EMAIL_*）")
        return True  # 邮件可选，不阻塞启动
    except Exception as e:
        logger.error("❌ 邮件测试异常: %s", e)
        logger.error(traceback.format_exc())
        # 邮件可选，仍返回 True 避免阻塞
        return True


async def main_async():
    """执行全部检查项，汇总结果。"""
    print("\n" + "=" * 50 + "\n   🚀 DSF3 健康检查 (完整版)\n" + "=" * 50 + "\n")

    checks = [
        test_configuration(),
        test_rpc_and_jupiter(),
        test_risk_control(),
        test_trader_state(),
        test_websocket_and_helius_api(),
        test_project_imports(),
        test_notification(),
    ]
    results = [await c for c in checks]

    if all(results):
        print("\n🎉 所有检查通过！系统状态：健康，可运行 python main.py\n")
        return 0
    print("\n🚫 存在失败项，请根据上方日志修复后再运行主程序\n")
    return 1


def main():
    parser = argparse.ArgumentParser(description="DSF3 启动前自检")
    parser.add_argument("--proxy", action="store_true", help="开启本地代理 (HTTP_PROXY/HTTPS_PROXY)")
    args = parser.parse_args()

    if args.proxy:
        proxy_url = "http://127.0.0.1:7890"
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        logger.info("🌍 已注入代理: %s", proxy_url)
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
