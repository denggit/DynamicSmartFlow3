#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
运行主程序前的健康检查脚本。
检查：Python 版本、依赖包、.env 配置、钱包格式、关键 API 连通性。
用法: python health_check.py
"""
import sys
import os

# 确保能导入项目模块（以项目根为 cwd）
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

OK = "\033[92m[OK]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"


def check_python():
    """Python >= 3.10"""
    v = sys.version_info
    if v.major >= 3 and v.minor >= 10:
        print(f"  {OK} Python {v.major}.{v.minor}.{v.micro}")
        return True
    print(f"  {FAIL} 需要 Python >= 3.10，当前 {v.major}.{v.minor}.{v.micro}")
    return False


def check_packages():
    """必需依赖是否已安装（包名 vs 导入名：python-dotenv -> dotenv）"""
    required = [
        ("solana", "solana"),
        ("solders", "solders"),
        ("httpx", "httpx"),
        ("websockets", "websockets"),
        ("python-dotenv", "dotenv"),
    ]
    missing = []
    for display_name, import_name in required:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(display_name)
    if not missing:
        print(f"  {OK} 依赖包: solana, solders, httpx, websockets, python-dotenv")
        return True
    print(f"  {FAIL} 缺少依赖: {', '.join(missing)}，请执行 pip install -r requirements.txt")
    return False


def check_env_file():
    """是否存在 .env"""
    path = os.path.join(ROOT, ".env")
    if os.path.isfile(path):
        print(f"  {OK} .env 存在")
        return True
    print(f"  {FAIL} 未找到 .env，请在项目根目录创建并配置")
    return False


def check_env_vars():
    """加载 .env 并检查关键变量"""
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(ROOT) / ".env"
        load_dotenv(dotenv_path=env_path)
    except Exception as e:
        print(f"  {FAIL} 加载 .env 失败: {e}")
        return False

    helius_raw = os.getenv("HELIUS_API_KEY", "").strip()
    helius_keys = [k.strip() for k in helius_raw.split(",") if k.strip()]
    sol_key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    email_ok = all([
        os.getenv("EMAIL_SENDER"),
        os.getenv("EMAIL_PASSWORD"),
        os.getenv("EMAIL_RECEIVER"),
    ])

    all_ok = True
    if helius_keys:
        n = len(helius_keys)
        print(f"  {OK} HELIUS_API_KEY 已配置" + (f"（共 {n} 个 Key 池）" if n > 1 else ""))
    else:
        print(f"  {FAIL} HELIUS_API_KEY 未配置（必填，可逗号分隔多个）")
        all_ok = False

    if sol_key:
        print(f"  {OK} SOLANA_PRIVATE_KEY 已配置")
    else:
        print(f"  {WARN} SOLANA_PRIVATE_KEY 未配置（仅查价/监控可运行，无法真实交易）")

    if email_ok:
        print(f"  {OK} 邮件配置完整 (EMAIL_SENDER/PASSWORD/RECEIVER)")
    else:
        print(f"  {WARN} 邮件未完整配置，将不发送开仓/清仓/日报邮件")

    jup_raw = os.getenv("JUP_API_KEY", "").strip()
    jup_keys = [k.strip() for k in jup_raw.split(",") if k.strip()]
    if jup_keys:
        print(f"  {OK} JUP_API_KEY 已配置" + (f"（共 {len(jup_keys)} 个）" if len(jup_keys) > 1 else "（可选）"))
    else:
        print(f"  {WARN} JUP_API_KEY 未配置（可选，Jupiter 限流时可逗号分隔多个）")

    return all_ok


def check_wallet():
    """私钥格式是否可解析为 Keypair"""
    key = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not key:
        print(f"  {WARN} 未配置私钥，跳过钱包检查")
        return True
    try:
        from solders.keypair import Keypair
        kp = Keypair.from_base58_string(key)
        print(f"  {OK} 钱包格式正确 (Pubkey: {str(kp.pubkey())[:16]}...)")
        return True
    except Exception as e:
        print(f"  {FAIL} 私钥格式错误: {e}")
        return False


def check_network():
    """关键 API 是否可达（Helius RPC 用 POST JSON-RPC；DexScreener GET；Jupiter Quote 需带 x-api-key）"""
    import urllib.request
    import urllib.error
    import json

    helius_keys = [k.strip() for k in os.getenv("HELIUS_API_KEY", "").strip().split(",") if k.strip()]
    first_helius = helius_keys[0] if helius_keys else ""
    jup_keys = [k.strip() for k in os.getenv("JUP_API_KEY", "").strip().split(",") if k.strip()]
    first_jup = jup_keys[0] if jup_keys else ""

    all_ok = True

    # 1. Helius RPC：必须 POST JSON-RPC，GET 会 404
    if first_helius:
        helius_url = f"https://mainnet.helius-rpc.com/?api-key={first_helius}"
        try:
            data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getHealth"}).encode("utf-8")
            req = urllib.request.Request(
                helius_url, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "DSF3-HealthCheck/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read().decode()
                if "result" in body or "ok" in body.lower():
                    print(f"  {OK} Helius RPC 可达")
                else:
                    print(f"  {OK} Helius RPC 可达 (响应: {body[:80]}...)")
        except urllib.error.HTTPError as e:
            print(f"  {FAIL} Helius RPC HTTP {e.code}")
            all_ok = False
        except Exception as e:
            print(f"  {FAIL} Helius RPC 不可达: {e}")
            all_ok = False
    else:
        print(f"  {WARN} 未配置 HELIUS_API_KEY，跳过 Helius RPC 检查")

    # 2. DexScreener
    try:
        req = urllib.request.Request(
            "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112",
            headers={"User-Agent": "DSF3-HealthCheck/1.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as _:
            print(f"  {OK} DexScreener 可达")
    except Exception as e:
        print(f"  {FAIL} DexScreener 不可达: {e}")
        all_ok = False

    # 3. Jupiter Quote：v1 需 x-api-key，未配置 key 时可能 401
    jup_url = "https://api.jup.ag/swap/v1/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=So11111111111111111111111111111111111111112&amount=1000000&slippageBps=50"
    try:
        headers = {"User-Agent": "DSF3-HealthCheck/1.0"}
        if first_jup:
            headers["x-api-key"] = first_jup
        req = urllib.request.Request(jup_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as _:
            print(f"  {OK} Jupiter Quote 可达")
    except urllib.error.HTTPError as e:
        if e.code == 401 and not first_jup:
            print(f"  {WARN} Jupiter Quote 返回 401，请配置 JUP_API_KEY 后重试")
        else:
            print(f"  {FAIL} Jupiter Quote HTTP {e.code}")
            all_ok = False
    except Exception as e:
        print(f"  {FAIL} Jupiter Quote 不可达: {e}")
        all_ok = False

    return all_ok


def check_project_imports():
    """项目核心模块是否能正常导入"""
    try:
        from config.settings import helius_key_pool, SOLANA_PRIVATE_KEY_BASE58
        from services.dexscreener.dex_scanner import DexScanner
        from services.solana.trader import SolanaTrader
        from services import risk_control
        from services import notification
        from utils.logger import get_logger
        print(f"  {OK} 项目模块导入正常 (config, services, utils)")
        return True
    except Exception as e:
        print(f"  {FAIL} 项目模块导入失败: {e}")
        return False


def main():
    print("=" * 50)
    print("  DSF3 健康检查")
    print("=" * 50)

    results = []
    results.append(("Python 版本", check_python()))
    results.append(("依赖包", check_packages()))
    results.append((".env 文件", check_env_file()))
    results.append(("环境变量", check_env_vars()))
    results.append(("钱包格式", check_wallet()))
    results.append(("网络连通", check_network()))
    results.append(("项目导入", check_project_imports()))

    print("=" * 50)
    passed = sum(1 for _, r in results if r)
    total = len(results)
    if passed == total:
        print(f"  全部通过 ({passed}/{total})，可以运行 python main.py")
    else:
        print(f"  通过 {passed}/{total} 项，请修复上述 [FAIL] 后再运行主程序")
    print("=" * 50)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
