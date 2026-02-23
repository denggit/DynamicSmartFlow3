#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : manual_verify_store.py
@Description: 需手动上链核对的代币集合。
              当交易已广播但 Alchemy/Helius 均无法验证时，收集 token 地址，
              供每小时报告汇总，提醒用户手动卖出（这些 token 未被程序跟踪）。
"""
import threading
from typing import List, Set

_store: Set[str] = set()
_lock = threading.Lock()


def add_manual_verify_token(token_address: str) -> None:
    """
    添加需手动上链核对的代币地址。
    线程安全。
    """
    if not token_address or not token_address.strip():
        return
    addr = token_address.strip()
    with _lock:
        _store.add(addr)


def get_and_clear_pending() -> List[str]:
    """
    获取当前待核对的全部 token 地址并清空集合。
    线程安全。用于每小时报告发送后清空，避免重复提醒。
    """
    with _lock:
        out = list(_store)
        _store.clear()
    return out


def get_pending_count() -> int:
    """获取当前待核对的 token 数量（不修改集合）。"""
    with _lock:
        return len(_store)
