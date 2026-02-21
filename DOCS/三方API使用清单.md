# 三方 API 使用清单

> 全量梳理项目中使用的外部 API，便于成本管控、限流排查与架构规划。

---

## 一、汇总表

| 第三方 | API 类型 | 端点/用途 | 配置 Key | 主要用途 |
|--------|----------|-----------|----------|----------|
| **Helius** | RPC / HTTP / WebSocket | 见下表 | `HELIUS_API_KEY` | 解析交易、猎手监控订阅 |
| **Alchemy** | RPC / WebSocket | 见下表 | `ALCHEMY_API_KEY` | 签名、交易详情、广播、跟单 Agent |
| **DexScreener** | REST | 见下表 | 无需 Key | 代币价格、流动性、热门币扫描 |
| **Jupiter** | REST | 见下表 | `JUP_API_KEY`（可选） | Swap 询价、成交 |
| **RugCheck** | REST | 见下表 | 无需 Key | 代币风险检测 |
| **Birdeye** | REST / WebSocket | 见下表 | `BIRDEYE_API_KEY` | 价格、市场数据（已封装，业务暂未接入） |

---

## 二、Helius

| 端点 | 方法 | 调用位置 | 说明 |
|------|------|----------|------|
| `https://mainnet.helius-rpc.com/?api-key={key}` | RPC | 健康检查等 | 标准 JSON-RPC |
| `wss://mainnet.helius-rpc.com/?api-key={key}` | WebSocket | `hunter_monitor.py` | `transactionSubscribe` 订阅猎手交易 |
| `https://api.helius.xyz/v0/transactions` | POST | `helius/http.py` `fetch_parsed_transactions` | 批量解析交易（`tokenTransfers`、`nativeTransfers` 等） |
| `https://api.helius.xyz/v0/addresses/{addr}/transactions` | GET | `health_check.py` | 地址交易列表（健康检查） |

**业务使用**：
- `sm_searcher.py`：`fetch_parsed_transactions`（猎手代币 ROI、LP 检测、体检）
- `hunter_monitor.py`：WebSocket 订阅 + HTTP 拉取解析交易（猎手买卖监控）

---

## 三、Alchemy

| 端点 | 方法 | 调用位置 | 说明 |
|------|------|----------|------|
| `https://solana-mainnet.g.alchemy.com/v2/{key}` | RPC | `alchemy/rpc.py` | `getSignaturesForAddress`、`getTransaction`、`getTokenAccountsByOwner`、`sendRawTransaction` |
| `wss://solana-mainnet.g.alchemy.com/v2/{key}` | WebSocket | `hunter_agent.py` | 跟单 Agent 的 `transactionSubscribe`（可选） |

**业务使用**：
- `sm_searcher.py`：`get_signatures_for_address`（频率预检、ATA 签名）
- `hunter_agent.py`：WebSocket 订阅、`get_transaction`、`get_token_accounts_by_owner`
- `trader.py`：RPC 客户端（广播交易、余额查询等）

---

## 四、DexScreener

| 端点 | 方法 | 调用位置 | 说明 |
|------|------|----------|------|
| `https://api.dexscreener.com/token-profiles/latest/v1` | GET | `dex_scanner.py` `fetch_latest_tokens` | 热门/社交更新代币 |
| `https://api.dexscreener.com/latest/dex/tokens/{token}` | GET | `dex_scanner.py`、`sm_searcher.py`、`risk_control.py` | 代币交易对、价格、流动性 |

**业务使用**：
- `dex_scanner.py`：热门币扫描、价格
- `sm_searcher.py`：代币年龄验证、涨幅、价格（`_get_token_price_sol`、`verify_token_age_via_dexscreener`）
- `hunter_monitor.py`：首买价格（`dex_scanner.get_token_price`）
- `risk_control.py`：流动性、FDV（`check_token_liquidity`）

---

## 五、Jupiter

| 端点 | 方法 | 调用位置 | 说明 |
|------|------|----------|------|
| `https://api.jup.ag/swap/v1/quote` | GET | `trader.py` `_jupiter_swap` | 询价 |
| `https://api.jup.ag/swap/v1/swap` | POST | `trader.py` `_jupiter_swap` | 构建 Swap 交易 |

**业务使用**：
- `trader.py`：买入、卖出、加仓、止盈；PnL > 200% 时用 Quote 校验真实可卖价

---

## 六、RugCheck

| 端点 | 方法 | 调用位置 | 说明 |
|------|------|----------|------|
| `https://api.rugcheck.xyz/v1/tokens/{mint}/report` | GET | `risk_control.py` `check_is_safe_token` | 代币风险报告 |

**业务使用**：
- `main.py`：通过 `risk_control.check_is_safe_token` 做买入前风控

---

## 七、Birdeye

| 端点 | 方法 | 封装位置 | 说明 |
|------|------|----------|------|
| `https://public-api.birdeye.so/defi/price` | GET | `birdeye/price.py` | 代币价格（USD） |
| `https://public-api.birdeye.so/defi/v3/token/market-data` | GET | `birdeye/market.py` | 市场数据（流动性、FDV 等） |
| `wss://public-api.birdeye.so/socket/solana` | WebSocket | `birdeye/ws.py` | 行情推送 |

**当前状态**：`birdeye_client` 已封装，但**未**被业务模块引用；价格与风控仍主要依赖 DexScreener。

---

## 八、调用链路图

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   main.py       │────▶│ DexScanner      │────▶│ DexScreener API │ (热门币)
│  猎手挖掘/跟单   │     │ dex_scanner     │     │ 无 Key          │
└────────┬────────┘     └─────────────────┘     └─────────────────┘
         │
         │              ┌─────────────────┐     ┌─────────────────┐
         ├────────────▶│ sm_searcher     │────▶│ Alchemy RPC     │ (签名)
         │              │ 猎手挖掘/体检    │     │ getSignatures   │
         │              └────────┬────────┘     └─────────────────┘
         │                       │
         │                       │              ┌─────────────────┐
         │                       └─────────────▶│ Helius HTTP     │ (解析交易)
         │                                      │ /v0/transactions│
         │
         │              ┌─────────────────┐     ┌─────────────────┐
         ├────────────▶│ hunter_monitor  │────▶│ Helius WSS      │ (订阅)
         │              │ 猎手交易监控     │     │ transactionSub  │
         │              └────────┬────────┘     └────────┬────────┘
         │                       │                      │
         │                       │                      ▼
         │                       │              ┌─────────────────┐
         │                       └─────────────▶│ Helius HTTP     │ (解析)
         │                                      │ /v0/transactions│
         │
         │              ┌─────────────────┐     ┌─────────────────┐
         ├────────────▶│ hunter_agent    │────▶│ Alchemy WSS/RPC │ (跟单)
         │              │ 跟单执行         │     │ getTransaction  │
         │              └─────────────────┘     └─────────────────┘
         │
         │              ┌─────────────────┐     ┌─────────────────┐
         ├────────────▶│ trader          │────▶│ Jupiter API     │ (Swap)
         │              │ 交易执行         │     │ Quote + Swap    │
         │              └────────┬────────┘     └─────────────────┘
         │                       │
         │                       │              ┌─────────────────┐
         │                       └─────────────▶│ Alchemy RPC     │ (广播)
         │                                      │ sendRawTx       │
         │
         │              ┌─────────────────┐     ┌─────────────────┐
         └────────────▶│ risk_control    │────▶│ RugCheck API    │ (风控)
                       │ 买入前风控       │     │ + DexScreener   │
                       └─────────────────┘     └─────────────────┘
```

---

## 九、环境变量配置

| 变量名 | 是否必填 | 说明 |
|--------|----------|------|
| `HELIUS_API_KEY` | 必填 | 可逗号分隔多个，限流时自动切换 |
| `ALCHEMY_API_KEY` | 必填 | 可逗号分隔多个，限流时自动切换 |
| `JUP_API_KEY` | 可选 | 可逗号分隔多个，Jupiter 限流时使用 |
| `BIRDEYE_API_KEY` | 可选 | 当前业务未接入 |
| `DexScreener` | - | 无需 Key |
| `RugCheck` | - | 无需 Key |

---

---

## 十、健康检查覆盖

`python health_check.py` 会依次检测：

| 步骤 | 检测项 | 对应 API |
|------|--------|----------|
| 1 | 环境配置 | HELIUS / ALCHEMY / JUP / BIRDEYE Key |
| 2 | Alchemy RPC 连通 | getSignaturesForAddress（标准 Solana RPC 仅用 Alchemy，不用 Helius） |
| 3 | Helius | WebSocket + fetch_parsed_transactions |
| 4 | DexScreener | token-profiles + token 流动性 |
| 5 | Jupiter | Quote API |
| 6 | RugCheck | token report |
| 7 | Birdeye | get_token_price（可选） |
| 8 | Trader 状态 | 本地加载 |
| 9 | 项目模块 | 导入校验 |
| 10 | 邮件 | 发送测试 |

*最后更新：2026-02*
