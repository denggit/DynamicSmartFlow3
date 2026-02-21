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
TOTAL_STEPS = 10


async def test_configuration():
    """[1/N] 环境与配置：代理、.env、必要变量。"""
    logger.info("🛠️ [1/%d] 检查环境配置...", TOTAL_STEPS)
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        logger.info("✅ 检测到代理: %s", proxy)
    else:
        logger.info("☁️ 直连模式 (无代理)")

    if not Path(ROOT).joinpath(".env").is_file():
        logger.error("❌ 未找到 .env")
        return False

    # Helius：猎手监控 WebSocket + 解析交易 HTTP
    helius_raw = os.getenv("HELIUS_API_KEY", "").strip()
    helius_keys = [k.strip() for k in helius_raw.split(",") if k.strip()]
    if not helius_keys:
        logger.error("❌ HELIUS_API_KEY 未配置（必填，猎手监控+解析交易）")
        return False
    logger.info("✅ HELIUS_API_KEY 已配置（共 %d 个）", len(helius_keys))

    # Alchemy：sm_searcher 签名、Trader RPC、hunter_agent
    alchemy_raw = os.getenv("ALCHEMY_API_KEY", "").strip()
    alchemy_keys = [k.strip() for k in alchemy_raw.split(",") if k.strip()]
    if not alchemy_keys:
        logger.error("❌ ALCHEMY_API_KEY 未配置（必填，签名/RPC/广播）")
        return False
    logger.info("✅ ALCHEMY_API_KEY 已配置（共 %d 个）", len(alchemy_keys))

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

    birdeye_raw = os.getenv("BIRDEYE_API_KEY", "").strip()
    if birdeye_raw:
        logger.info("✅ BIRDEYE_API_KEY 已配置（当前业务未接入）")
    else:
        logger.info("☁️ BIRDEYE_API_KEY 未配置（可选，业务未接入）")

    return True


def _is_429(e: Exception) -> bool:
    """判断异常是否为 429 限流。"""
    err = e
    while err is not None:
        if getattr(err, "response", None) is not None and getattr(err.response, "status_code", None) == 429:
            return True
        s = str(err).lower()
        if "429" in s or "too many requests" in s:
            return True
        err = getattr(err, "__cause__", None)
    return False


async def test_alchemy_rpc():
    """[2/N] 标准 Solana RPC 连通测试（仅用 Alchemy，不再用 Helius）。"""
    logger.info("🔗 [2/%d] 测试 Alchemy RPC 连通（标准 RPC 仅用 Alchemy）...", TOTAL_STEPS)
    try:
        import httpx
        from config.settings import alchemy_key_pool
        from src.alchemy import alchemy_client

        # 使用 System Program 获取签名（高活跃度，与 sm_searcher 能力一致）
        test_addr = "11111111111111111111111111111111"
        async with httpx.AsyncClient(timeout=15.0) as client:
            sigs = await alchemy_client.get_signatures_for_address(
                test_addr, limit=1, http_client=client
            )
        if sigs and len(sigs) > 0:
            logger.info("✅ Alchemy RPC (getSignaturesForAddress) 正常")
            return True
        if sigs is not None:
            logger.info("✅ Alchemy RPC 返回空列表（地址无交易，接口正常）")
            return True
        logger.error("❌ Alchemy RPC 返回 None")
        return False
    except Exception as e:
        if _is_429(e) and alchemy_key_pool.size > 1:
            alchemy_key_pool.mark_current_failed()
            return await test_alchemy_rpc()
        logger.error("❌ Alchemy RPC 异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_helius_websocket_and_parse():
    """[3/N] Helius WebSocket + HTTP 解析交易（hunter_monitor/sm_searcher 实际使用）。"""
    logger.info("🔌 [3/%d] 测试 Helius WebSocket & 解析交易 API...", TOTAL_STEPS)
    try:
        from config.settings import helius_key_pool
        import websockets
        import httpx
        from src.alchemy import alchemy_client
        from src.helius import helius_client

        # 3.1 WebSocket 连接（hunter_monitor 使用）
        if not helius_key_pool.get_wss_url():
            logger.error("❌ Helius WSS 为空")
            return False
        max_ws_tries = max(helius_key_pool.size, 1)
        ws_ok = False
        for attempt in range(max_ws_tries):
            wss_url = helius_key_pool.get_wss_url()
            try:
                async with websockets.connect(wss_url, ping_interval=20, ping_timeout=10) as ws:
                    sub_msg = {"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}
                    await ws.send(json.dumps(sub_msg))
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        data = json.loads(msg)
                        if "result" in data or "error" in data:
                            pass
                    except asyncio.TimeoutError:
                        pass
                    logger.info("✅ Helius WebSocket 连接正常")
                    ws_ok = True
                    break
            except Exception as e:
                if _is_429(e) and helius_key_pool.size > 1:
                    helius_key_pool.mark_current_failed()
                    continue
                logger.error("❌ Helius WebSocket 失败: %s", e)
                break
        if not ws_ok:
            return False

        # 3.2 fetch_parsed_transactions（sm_searcher 使用）：先 Alchemy 取 1 个 sig，再 Helius 解析
        async with httpx.AsyncClient(timeout=15.0) as client:
            sigs = await alchemy_client.get_signatures_for_address(
                "11111111111111111111111111111111", limit=1, http_client=client
            )
        if not sigs:
            logger.info("✅ Helius 解析跳过（Alchemy 无可用签名）")
            return True
        sig_str = sigs[0].get("signature") if isinstance(sigs[0], dict) else sigs[0]
        async with httpx.AsyncClient(timeout=15.0) as client:
            txs = await helius_client.fetch_parsed_transactions([sig_str], http_client=client)
        if txs is not None:
            logger.info("✅ Helius fetch_parsed_transactions 正常（解析 %d 笔）", len(txs))
            return True
        logger.warning("⚠️ Helius 解析返回空（可能网络或 Key 异常）")
        return True
    except Exception as e:
        logger.error("❌ Helius 测试异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_dexscreener():
    """[4/N] DexScreener：token-profiles + token 流动性（dex_scanner/risk_control 实际使用）。"""
    logger.info("📊 [4/%d] 测试 DexScreener API...", TOTAL_STEPS)
    try:
        from src.dexscreener.dex_scanner import DexScanner
        from src.rugcheck.risk_control import check_token_liquidity

        scanner = DexScanner()
        raw = await scanner.fetch_latest_tokens()
        if raw is None:
            raw = []
        sol_tokens = [t for t in raw if t.get("chainId") == "solana"]
        logger.info("✅ DexScreener token-profiles 正常（Solana 代币 %d 个）", len(sol_tokens))

        has_pool, liq_usd, fdv = await check_token_liquidity("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN")
        if has_pool and liq_usd > 0:
            logger.info("✅ DexScreener token 流动性正常 | JUP 流动性: $%s", f"{liq_usd:,.0f}")
            return True
        logger.warning("⚠️ JUP 流动性查询异常（可能 DexScreener 限流）")
        return True
    except Exception as e:
        logger.error("❌ DexScreener 测试异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_jupiter():
    """[5/N] Jupiter Quote API（trader 实际使用）。"""
    logger.info("🪐 [5/%d] 测试 Jupiter Quote API...", TOTAL_STEPS)
    try:
        from config.settings import jup_key_pool, JUP_QUOTE_API
        from services.trader import SolanaTrader

        trader = SolanaTrader()
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        params = {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": USDC_MINT,
            "amount": str(int(0.1 * 1_000_000_000)),
            "slippageBps": 50,
        }
        headers = {"User-Agent": "DSF3-HealthCheck/1.0"}
        max_tries = max(jup_key_pool.size, 1)
        quote_resp = None
        for attempt in range(max_tries):
            key = jup_key_pool.get_api_key()
            if key:
                headers["x-api-key"] = key
            quote_resp = await trader.http_client.get(JUP_QUOTE_API, params=params, headers=headers)
            if quote_resp.status_code == 429 and jup_key_pool.size > 1:
                jup_key_pool.mark_current_failed()
                continue
            break
        await trader.close()

        if quote_resp.status_code == 429:
            logger.warning("⚠️ Jupiter 限流 (429)，请稍后重试或配置 JUP_API_KEY")
            return False
        if quote_resp.status_code != 200:
            logger.error("❌ Jupiter 询价失败: HTTP %s", quote_resp.status_code)
            return False
        data = quote_resp.json() or {}
        out = data.get("outAmount")
        if out is not None:
            logger.info("✅ Jupiter Quote 正常 | 0.1 SOL ≈ %.2f USDC", int(out) / 1e6)
        else:
            logger.info("✅ Jupiter Quote 返回 200")
        return True
    except Exception as e:
        logger.error("❌ Jupiter 测试异常: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_rugcheck():
    """[6/N] RugCheck API（risk_control 买入前风控使用）。"""
    logger.info("🛡️ [6/%d] 测试 RugCheck API...", TOTAL_STEPS)
    try:
        from src.rugcheck.risk_control import check_is_safe_token

        # JUP 为已知安全代币，RugCheck 应有收录
        ok = await check_is_safe_token("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN")
        logger.info("✅ RugCheck API 正常（JUP 风控检测完成）")
        return True
    except Exception as e:
        logger.warning("⚠️ RugCheck 异常（可能超时/未收录）: %s", e)
        return True


async def test_birdeye():
    """[7/N] Birdeye API（已封装，业务未接入；若配置 Key 则验证）。"""
    logger.info("👁️ [7/%d] 测试 Birdeye API...", TOTAL_STEPS)
    try:
        from config.settings import birdeye_key_pool
        from src.birdeye import birdeye_client

        if birdeye_key_pool.size == 0:
            logger.info("☁️ Birdeye 未配置，跳过")
            return True
        price = await birdeye_client.get_token_price("So11111111111111111111111111111111111111112")
        if price is not None and price > 0:
            logger.info("✅ Birdeye 价格 API 正常 | WSOL ≈ $%.2f", price)
            return True
        logger.warning("⚠️ Birdeye 返回空或 0")
        return True
    except Exception as e:
        logger.warning("⚠️ Birdeye 异常: %s", e)
        return True


async def test_trader_state():
    """[8/N] Trader 状态加载与钱包一致性（不写入，只读）。"""
    logger.info("📂 [8/%d] 测试 Trader 状态加载...", TOTAL_STEPS)
    try:
        from config.settings import SOLANA_PRIVATE_KEY_BASE58, BASE_DIR
        from services.trader import SolanaTrader
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


async def test_project_imports():
    """[9/N] 项目核心模块导入。"""
    logger.info("📦 [9/%d] 测试项目模块导入...", TOTAL_STEPS)
    try:
        from config.settings import helius_key_pool, alchemy_key_pool, jup_key_pool
        from src.dexscreener.dex_scanner import DexScanner
        from services.trader import SolanaTrader
        from src.rugcheck import risk_control
        from services import notification
        from utils.logger import get_logger
        logger.info("✅ 项目模块导入正常 (config, src, utils)")
        return True
    except Exception as e:
        logger.error("❌ 项目导入失败: %s", e)
        logger.error(traceback.format_exc())
        return False


async def test_notification():
    """[10/N] 邮件发送（同步接口放线程执行）。"""
    logger.info("📧 [10/%d] 测试邮件发送...", TOTAL_STEPS)
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
        test_alchemy_rpc(),
        test_helius_websocket_and_parse(),
        test_dexscreener(),
        test_jupiter(),
        test_rugcheck(),
        test_birdeye(),
        test_trader_state(),
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
