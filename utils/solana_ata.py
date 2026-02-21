#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : solana_ata.py
@Description: Solana 关联代币账户 (ATA) 地址离线推导。
              无需 RPC，根据钱包地址和 Token Mint 本地计算 ATA 地址。
              支持 SPL Token 与 Token-2022（Pump.fun 等新代币常用）。
"""

from solders.pubkey import Pubkey

# SPL Token 标准常量（Solana 主网）
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def _derive_ata(owner_pk: Pubkey, mint_pk: Pubkey, token_program_id: Pubkey) -> str:
    """单次 ATA 推导，指定 token program。"""
    seeds = [bytes(owner_pk), bytes(token_program_id), bytes(mint_pk)]
    pda, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
    return str(pda)


def get_associated_token_address(owner: str, mint: str, token_program: str = "Token") -> str:
    """
    根据钱包地址和 Token Mint 离线推导 ATA（关联代币账户）地址。
    Solana 的 ATA 是 PDA，种子为 [owner, TOKEN_PROGRAM_ID, mint]。
    无需调用 RPC，纯本地计算。

    Args:
        owner: 钱包公钥（Base58）
        mint: 代币 Mint 地址（Base58）
        token_program: "Token"（标准 SPL）| "Token2022"（Pump.fun 等新代币常用）

    Returns:
        ATA 地址（Base58 字符串）
    """
    owner_pk = Pubkey.from_string(owner)
    mint_pk = Pubkey.from_string(mint)
    if token_program == "Token2022":
        return _derive_ata(owner_pk, mint_pk, TOKEN_2022_PROGRAM_ID)
    return _derive_ata(owner_pk, mint_pk, TOKEN_PROGRAM_ID)
