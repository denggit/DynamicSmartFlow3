#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : src/notification.py
@Description: 邮件通知服务。支持开仓/清仓/日报；发送均在独立线程执行，不阻塞主流程。
"""
import os
import smtplib
import threading
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config.settings import (
    EMAIL_SENDER, EMAIL_RECEIVER, EMAIL_PASSWORD,
    SMTP_SERVER, SMTP_PORT, BOT_NAME,
    HUNTER_MODE,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _send_email_sync(subject: str, content: str, attachment_path: str = None) -> bool:
    """
    同步发送邮件（仅内部使用，主流程应使用 send_email_in_thread）。
    """
    if not all([EMAIL_SENDER, EMAIL_RECEIVER, EMAIL_PASSWORD]):
        logger.warning("邮件未配置 (EMAIL_SENDER/RECEIVER/PASSWORD)，跳过发送")
        return False
    try:
        msg = MIMEMultipart()
        prefix = f"[{BOT_NAME}] " if BOT_NAME else ""
        full_subject = f"{prefix}{subject}"
        msg["Subject"] = full_subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg.attach(MIMEText(content, "plain", "utf-8"))
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(attachment_path))
                part["Content-Disposition"] = f'attachment; filename="{os.path.basename(attachment_path)}"'
                msg.attach(part)
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        logger.info("📧 邮件发送成功: %s", full_subject)
        return True
    except Exception as e:
        logger.exception("❌ 邮件发送失败: %s", e)
        return False


def send_email_in_thread(subject: str, content: str, attachment_path: str = None) -> None:
    """
    在新线程中发送邮件，不阻塞主线程。失败仅打日志。
    """
    def _run():
        _send_email_sync(subject, content, attachment_path)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def send_critical_error_email(subject: str, content: str) -> None:
    """
    严重错误告警邮件（跟单/程序异常时触发）。独立线程发送，不阻塞。
    标题会加 [严重错误] 前缀，便于区分。
    """
    full_subject = "🚨 严重错误 - " + (subject or "程序异常")
    send_email_in_thread(full_subject, content)


# --------------- 业务邮件内容构造（供 main/trader 回调使用） ---------------

def build_first_entry_content(
    token_address: str,
    entry_time: str,
    buy_sol: float,
    token_amount: float,
    price_sol: float,
    hunters_summary: str,
) -> str:
    """首次跟单某代币的邮件正文。"""
    return (
        f"【首次跟单】\n"
        f"代币: {token_address}\n"
        f"买入时间: {entry_time}\n"
        f"买入金额: {buy_sol:.4f} SOL\n"
        f"获得数量: {token_amount:.4f} Token\n"
        f"成交均价: {price_sol:.6f} SOL/Token\n"
        f"猎手概要: {hunters_summary}\n"
    )


def build_close_content(
    token_address: str,
    entry_time: str,
    trade_records: list,
    total_pnl_sol: float,
) -> str:
    """
    清仓邮件正文。trade_records 每项: ts, type(buy/sell), sol_spent, sol_received, token_amount, note。
    每笔一行，并写赚/亏；最后汇总本次总盈亏 (SOL)。
    """
    lines = [
        f"【清仓报告】\n",
        f"代币: {token_address}\n",
        f"首次买入时间: {entry_time}\n",
        "--- 每笔交易 ---\n",
    ]
    total_spent = 0.0
    total_received = 0.0
    for i, r in enumerate(trade_records, 1):
        ts = r.get("ts") or 0
        time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"
        typ = r.get("type", "")
        sol_spent = float(r.get("sol_spent") or 0)
        sol_received = float(r.get("sol_received") or 0)
        token_amount = r.get("token_amount", 0)
        note = r.get("note", "")
        pnl_sol = r.get("pnl_sol")
        total_spent += sol_spent
        total_received += sol_received
        if typ == "sell" and pnl_sol is not None:
            pnl_str = f"盈亏: {pnl_sol:+.4f} SOL"
        elif typ == "buy":
            pnl_str = f"支出: {sol_spent:.4f} SOL"
        else:
            pnl_str = ""
        lines.append(
            f"  {i}. [{time_str}] {note} | "
            f"类型:{typ} 数量:{token_amount} | "
            f"{pnl_str}\n"
        )
    lines.append("--- 汇总 ---\n")
    lines.append(f"总投入(SOL): {total_spent:.4f}\n")
    lines.append(f"总收回(SOL): {total_received:.4f}\n")
    lines.append(f"本次盈亏: {total_pnl_sol:+.4f} SOL\n")
    return "".join(lines)


def build_daily_report_content(
    today_pnl_sol: float,
    total_pnl_sol: float,
    details_lines: list,
) -> str:
    """日报正文（旧版简化格式，已由 build_detailed_daily_report 取代）。"""
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    lines = [
        f"【每日收益日报 | {mode_label}】\n",
        f"今日收益(SOL): {today_pnl_sol:+.4f}\n",
        f"累计收益(SOL): {total_pnl_sol:+.4f}\n",
        "--- 今日明细 ---\n",
    ]
    lines.extend(details_lines if details_lines else ["(无)\n"])
    return "".join(lines)


def build_manual_verify_report(tokens: list) -> str:
    """
    构建需手动核对的 token 报告正文。
    tokens: [(address, name_or_address), ...] 代币地址与名称（名称获取失败时用地址）
    """
    if not tokens:
        return ""
    lines = [
        "【需手动上链核对】\n",
        "以下代币交易已广播但验证失败（Alchemy/Helius 无法确认），可能已成交。\n",
        "⚠️ 这些代币未在程序跟踪中，请手动核实链上余额并卖出。\n",
        "---\n",
    ]
    for i, (addr, label) in enumerate(tokens, 1):
        lines.append(f"  {i}. {label}\n")
        lines.append(f"     地址: {addr}\n")
    return "".join(lines)


def build_detailed_daily_report(
    hunter_pool_count: int,
    hunter_pool_limit: int,
    today_tokens_traded: int,
    today_tokens_held: int,
    today_tokens_settled: int,
    today_pnl_sol: float,
    today_avg_roi_pct: float,
    today_win_count: int,
    today_loss_count: int,
    today_profit_factor: float,
    total_pnl_sol: float,
    total_trades: int,
    top_hunters: list,
    today_details: list,
    daily_profit_top5: list = None,
    daily_loss_top5: list = None,
    overall_profit_top5: list = None,
    overall_loss_top5: list = None,
    unrealized_by_hunter: list = None,
) -> str:
    """
    构建详细日报正文。
    top_hunters: [(hunter_addr_short, pnl_sol, rank), ...] 累计前五（兼容旧逻辑）
    daily_profit_top5 / daily_loss_top5: [(addr, pnl_sol), ...] 今日盈利/亏损前五，addr 为完整地址
    overall_profit_top5 / overall_loss_top5: [(addr, pnl_sol), ...] 累计盈利/亏损前五，addr 为完整地址
    unrealized_by_hunter: [(addr, tokens_str, cost_sol, value_sol, unrealized_sol), ...] 未清仓猎手持仓成本与估值
    today_details: 今日每笔交易/结算说明
    """
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    lines = [
        f"【每日交易日报 | {mode_label}】\n",
        "───────── 猎手池 ─────────\n",
        f"当前猎手数: {hunter_pool_count}/{hunter_pool_limit}\n",
        "\n",
        "───────── 今日概况 ─────────\n",
        f"交易代币数: {today_tokens_traded}\n",
        f"当前持仓: {today_tokens_held}\n",
        f"今日结算: {today_tokens_settled}\n",
        f"今日盈亏: {today_pnl_sol:+.4f} SOL\n",
    ]
    if today_tokens_settled > 0:
        lines.append(f"今日平均收益: {today_avg_roi_pct:+.1f}%\n")
        lines.append(f"今日胜/亏单: {today_win_count}/{today_loss_count}\n")
        if today_profit_factor != float("inf") and today_profit_factor >= 0:
            lines.append(f"今日盈亏比: {today_profit_factor:.2f}\n")
    lines.extend([
        "\n",
        "───────── 累计概况 ─────────\n",
        f"累计盈亏: {total_pnl_sol:+.4f} SOL\n",
        f"累计成交笔数: {total_trades}\n",
        "\n",
        "───────── 今日盈利猎手 TOP5 ─────────\n",
    ])
    if daily_profit_top5:
        for i, (addr, pnl) in enumerate(daily_profit_top5, 1):
            lines.append(f"  {i}. {addr} 今日盈利: {pnl:+.4f} SOL\n")
    else:
        lines.append("  (今日暂无)\n")
    lines.extend([
        "\n",
        "───────── 今日亏损猎手 TOP5 ─────────\n",
    ])
    if daily_loss_top5:
        for i, (addr, pnl) in enumerate(daily_loss_top5, 1):
            lines.append(f"  {i}. {addr} 今日亏损: {pnl:+.4f} SOL\n")
    else:
        lines.append("  (今日暂无)\n")
    lines.extend([
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
    if unrealized_by_hunter:
        lines.extend([
            "\n",
            "───────── 未清仓持仓估值 ─────────\n",
        ])
        for addr, tokens_str, cost_sol, value_sol, unrealized in unrealized_by_hunter:
            u_str = f" (浮盈 {unrealized:+.4f})" if unrealized >= 0 else f" (浮亏 {unrealized:+.4f})"
            tok_part = f" [{tokens_str}]" if tokens_str else ""
            lines.append(f"  {addr}{tok_part} 成本 {cost_sol:.4f} SOL → 估值 {value_sol:.4f} SOL{u_str}\n")
    lines.extend([
        "\n",
        "───────── 今日明细 ─────────\n",
    ])
    lines.extend(today_details if today_details else ["(今日无交易/结算)\n"])
    return "".join(lines)


def send_first_entry_email(
    token_address: str,
    entry_time: str,
    buy_sol: float,
    token_amount: float,
    price_sol: float,
    hunters_summary: str,
) -> None:
    """首次跟单后，在新线程发送邮件。标题带 MODELA/MODELB 避免混淆。"""
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    subject = f"📈 首次跟单 [{mode_label}]"
    content = build_first_entry_content(
        token_address, entry_time, buy_sol, token_amount, price_sol, hunters_summary
    )
    send_email_in_thread(subject, content)


def send_close_email(
    token_address: str,
    entry_time: str,
    trade_records: list,
    total_pnl_sol: float,
) -> None:
    """清仓后，在新线程发送邮件。标题带 MODELA/MODELB 避免混淆。"""
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    subject = f"📉 清仓报告 [{mode_label}]"
    content = build_close_content(token_address, entry_time, trade_records, total_pnl_sol)
    send_email_in_thread(subject, content)


def send_daily_report_email(today_pnl_sol: float, total_pnl_sol: float, details_lines: list) -> None:
    """日报：在新线程发送（简化版）。标题带 MODELA/MODELB 避免混淆。"""
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    subject = f"📊 每日收益日报 [{mode_label}]"
    content = build_daily_report_content(today_pnl_sol, total_pnl_sol, details_lines)
    send_email_in_thread(subject, content)


def send_detailed_daily_report_email(content: str) -> None:
    """详细日报：在新线程发送。标题带 MODELA/MODELB 避免多实例混淆。"""
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    subject = f"📊 每日交易日报 [{mode_label}]"
    send_email_in_thread(subject, content)


def send_hunter_changes_email(
    added: int = 0,
    removed: int = 0,
    replaced: int = 0,
    updated: int = 0,
    total_count: int = 0,
    attachment_path: str = None,
) -> None:
    """
    猎手库变化通知：新增/删除/替换/僵尸剔除/体检更新等。
    MODELA 附带 hunters.json，MODELB 附带 smart_money.json。
    """
    parts = []
    if added > 0:
        parts.append(f"新增 {added} 个")
    if removed > 0:
        parts.append(f"删除 {removed} 个")
    if replaced > 0:
        parts.append(f"替换 {replaced} 个")
    if updated > 0:
        parts.append(f"更新 {updated} 个")
    if not parts:
        return
    change_summary = "，".join(parts)
    mode_label = (HUNTER_MODE or "MODELA").strip().upper()
    attachment_name = "smart_money.json" if mode_label == "MODELB" else "hunters.json"
    content = (
        f"【猎手库变化 | {mode_label}】\n\n"
        f"变化: {change_summary}\n"
        f"当前猎手总数: {total_count}\n\n"
        f"附件: {attachment_name}（最新猎手数据）"
    )
    subject = f"📋 猎手库变化 [{mode_label}]"
    send_email_in_thread(subject, content, attachment_path)
