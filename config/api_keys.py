#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: API Key 池（Helius / Alchemy / Birdeye / Jupiter）
"""
import os
import threading

_raw_helius = os.getenv("HELIUS_API_KEY", "") or ""
_raw_alchemy = os.getenv("ALCHEMY_API_KEY", "") or ""
_raw_birdeye = os.getenv("BIRDEYE_API_KEY", "") or ""
_raw_jup = os.getenv("JUP_API_KEY", "") or ""
HELIUS_API_KEYS = [k.strip() for k in _raw_helius.split(",") if k.strip()]
ALCHEMY_API_KEYS = [k.strip() for k in _raw_alchemy.split(",") if k.strip()]
BIRDEYE_API_KEYS = [k.strip() for k in _raw_birdeye.split(",") if k.strip()]
JUP_API_KEYS = [k.strip() for k in _raw_jup.split(",") if k.strip()]


class _KeyPool:
    """单类 Key 池：不可用时切下一个。"""

    def __init__(self, keys: list, name: str = ""):
        self._keys = list(keys) if keys else []
        self._index = 0
        self._lock = threading.Lock()
        self._name = name

    def get_api_key(self) -> str:
        with self._lock:
            if not self._keys:
                return ""
            return self._keys[self._index % len(self._keys)]

    def mark_current_failed(self) -> None:
        with self._lock:
            if not self._keys:
                return
            self._index = (self._index + 1) % max(len(self._keys), 1)

    @property
    def size(self) -> int:
        return len(self._keys)


class HeliusKeyPool(_KeyPool):
    def __init__(self, keys: list):
        super().__init__(keys, "Helius")

    def get_rpc_url(self) -> str:
        key = self.get_api_key()
        return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else ""

    def get_wss_url(self) -> str:
        key = self.get_api_key()
        return f"wss://mainnet.helius-rpc.com/?api-key={key}" if key else ""

    def get_http_endpoint(self) -> str:
        key = self.get_api_key()
        return f"https://api.helius.xyz/v0/transactions/?api-key={key}" if key else ""


class AlchemyKeyPool(_KeyPool):
    def __init__(self, keys: list):
        super().__init__(keys, "Alchemy")

    def get_rpc_url(self) -> str:
        key = self.get_api_key()
        return f"https://solana-mainnet.g.alchemy.com/v2/{key}" if key else ""

    def get_wss_url(self) -> str:
        key = self.get_api_key()
        return f"wss://solana-mainnet.g.alchemy.com/v2/{key}" if key else ""

    def get_http_endpoint(self) -> str:
        return ""


class BirdeyeKeyPool(_KeyPool):
    def __init__(self, keys: list):
        super().__init__(keys, "Birdeye")


helius_key_pool = HeliusKeyPool(HELIUS_API_KEYS)
alchemy_key_pool = AlchemyKeyPool(ALCHEMY_API_KEYS)
birdeye_key_pool = BirdeyeKeyPool(BIRDEYE_API_KEYS)
jup_key_pool = _KeyPool(JUP_API_KEYS, "Jupiter")

SOLANA_PRIMARY_PROVIDER = (os.getenv("SOLANA_PRIMARY_PROVIDER", "auto") or "auto").strip().lower()
HELIUS_API_KEY = helius_key_pool.get_api_key()
WSS_ENDPOINT = helius_key_pool.get_wss_url()
HTTP_ENDPOINT = helius_key_pool.get_http_endpoint()
HELIUS_RPC_URL = helius_key_pool.get_rpc_url()
