#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File    : position_report.py
@Description: 持仓与盈亏报告工具。
              自动读取 trader_state.json、trading_history.json 及月度汇总，
              输出当前持仓与整体盈利亏损报告。支持 --modela / --modelb / --dir 指定数据源。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 项目根目录
_TOOLS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOLS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.paths import DATA_MODELA_DIR, DATA_MODELB_DIR


def _get_data_dir(model: str | None, dir_override: Path | None) -> Path:
    """根据参数返回数据目录。"""
    if dir_override:
        return Path(dir_override)
    if model and model.upper() == "MODELB":
        return DATA_MODELB_DIR
    return DATA_MODELA_DIR


def _load_json(path: Path, default: list | dict | None = None):
    """安全加载 JSON 文件。"""
    if default is None:
        default = [] if path.name == "trading_history.json" else {}
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _load_trader_state(data_dir: Path) -> dict:
    """加载 trader_state.json。"""
    path = data_dir / "trader_state.json"
    return _load_json(path, {})


def _load_trading_history(data_dir: Path) -> list:
    """加载 trading_history.json。"""
    path = data_dir / "trading_history.json"
    data = _load_json(path, [])
    return data if isinstance(data, list) else []


def _load_summaries(data_dir: Path) -> list:
    """加载所有 summary_reportYYYYMM.json。"""
    out = []
    for f in sorted(data_dir.glob("summary_report*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                d = json.load(fp)
            if isinstance(d, dict) and "year" in d and "month" in d:
                out.append(d)
        except Exception:
            pass
    out.sort(key=lambda x: (x.get("year", 0), x.get("month", 0)))
    return out


def _format_addr(addr: str, max_len: int = 16) -> str:
    """地址缩写，便于展示。"""
    if not addr or len(addr) <= max_len:
        return addr or "-"
    return f"{addr[:8]}..{addr[-6:]}"


def generate_report(data_dir: Path, model_label: str) -> str:
    """
    生成持仓与盈亏报告。
    :param data_dir: 数据目录（data/modelA 或 data/modelB）
    :param model_label: 模式标签（MODELA / MODELB）
    :return: 报告正文
    """
    state = _load_trader_state(data_dir)
    history = _load_trading_history(data_dir)
    summaries = _load_summaries(data_dir)

    positions_data = state.get("positions") or {}
    # 仅有效持仓（total_tokens > 0）
    positions = {
        k: v for k, v in positions_data.items()
        if float(v.get("total_tokens", 0) or 0) > 0
    }

    # 累计已实现盈亏：月度汇总 + 当月卖出
    month_sells = [r for r in history if r.get("type") == "sell" and r.get("pnl_sol") is not None]
    total_pnl = sum(s.get("total_pnl", 0) for s in summaries) + sum(r.get("pnl_sol", 0) for r in month_sells)
    total_trades = sum(s.get("total_trades", 0) for s in summaries) + len(history)

    # 累计猎手盈亏
    hunter_pnl: dict[str, float] = {}
    for s in summaries:
        for addr, pnl in (s.get("hunter_pnl") or {}).items():
            if addr:
                hunter_pnl[addr] = hunter_pnl.get(addr, 0) + pnl
    for r in month_sells:
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

    lines = [
        f"【持仓与盈亏报告 | {model_label}】\n",
        f"数据目录: {data_dir}\n",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        "\n",
        "───────── 当前持仓 ─────────\n",
    ]

    if not positions:
        lines.append("  (无持仓)\n")
    else:
        total_cost_sol = 0.0
        for token_addr, pos in positions.items():
            cost = float(pos.get("total_cost_sol", 0) or 0)
            total_cost_sol += cost
            tokens = float(pos.get("total_tokens", 0) or 0)
            price = float(pos.get("average_price", 0) or 0)
            shares = pos.get("shares") or {}
            hunter_addrs = list(shares.keys())
            hunter_str = ", ".join(_format_addr(a) for a in hunter_addrs[:3])
            if len(hunter_addrs) > 3:
                hunter_str += f" 等{len(hunter_addrs)}人"
            lines.append(f"  {token_addr}\n")
            lines.append(f"    成本: {cost:.4f} SOL | 数量: {tokens:,.6f} | 均价: {price:.8f} SOL\n")
            lines.append(f"    猎手: {hunter_str}\n")
        lines.append(f"\n持仓合计成本: {total_cost_sol:.4f} SOL\n")

    lines.extend([
        "\n",
        "───────── 累计盈亏 ─────────\n",
        f"已实现盈亏: {total_pnl:+.4f} SOL\n",
        f"累计成交笔数: {total_trades}\n",
        "\n",
        "───────── 累计盈利猎手 TOP5 ─────────\n",
    ])
    if overall_profit_top5:
        for i, (addr, pnl) in enumerate(overall_profit_top5, 1):
            lines.append(f"  {i}. {addr} 累计盈利: {pnl:+.4f} SOL\n")
    else:
        lines.append("  (暂无数据)\n")
    lines.extend([
        "\n",
        "───────── 累计亏损猎手 TOP5 ─────────\n",
    ])
    if overall_loss_top5:
        for i, (addr, pnl) in enumerate(overall_loss_top5, 1):
            lines.append(f"  {i}. {addr} 累计亏损: {pnl:+.4f} SOL\n")
    else:
        lines.append("  (暂无数据)\n")

    # 最近若干笔交易
    recent = history[-20:] if history else []
    if recent:
        lines.extend([
            "\n",
            "───────── 最近交易（最多 20 笔） ─────────\n",
        ])
        for r in reversed(recent):
            dt = r.get("date", "")
            ts = r.get("ts") or 0
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"
            typ = r.get("type", "")
            token = _format_addr(r.get("token", ""), 20)
            note = r.get("note", "")
            if typ == "buy":
                sol = r.get("sol_spent") or 0
                lines.append(f"  [{dt} {time_str}] 买入 {token} {sol:.4f} SOL | {note}\n")
            else:
                pnl = r.get("pnl_sol")
                pnl_str = f" {pnl:+.4f} SOL" if pnl is not None else ""
                lines.append(f"  [{dt} {time_str}] 卖出 {token} | {note}{pnl_str}\n")

    return "".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="持仓与盈亏报告工具：读取 trader_state.json、trading_history.json 生成报告"
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--modela", action="store_true", help="使用 data/modelA 数据")
    grp.add_argument("--modelb", action="store_true", help="使用 data/modelB 数据")
    parser.add_argument("--dir", type=Path, help="指定数据目录（覆盖 --modela/--modelb）")
    parser.add_argument("-o", "--output", type=Path, help="输出到文件而非 stdout")
    args = parser.parse_args()

    model = "MODELB" if args.modelb else ("MODELA" if args.modela else None)
    data_dir = _get_data_dir(model, args.dir)
    model_label = "MODELB" if (args.modelb or (args.dir and "modelB" in str(args.dir))) else "MODELA"

    if not data_dir.exists():
        print(f"错误: 数据目录不存在: {data_dir}", file=sys.stderr)
        sys.exit(1)

    report = generate_report(data_dir, model_label)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"报告已写入: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
