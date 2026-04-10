"""
MEXC REST API client
"""
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)


class MEXCClient:
    BASE_URL = "https://api.mexc.com"
    MAX_RETRIES = 2
    RETRY_DELAY = 1.5

    def __init__(self, api_key: str = "", secret: str = ""):
        self.api_key = api_key
        self.secret = secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._valid_symbols: Optional[Set[str]] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"X-MEXC-APIKEY": self.api_key} if self.api_key else {}
            )
        return self._session

    def _sign(self, params: dict) -> str:
        query = urlencode(sorted(params.items()))
        return hmac.new(
            self.secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    async def _get(self, path: str, params: Optional[Dict] = None, signed: bool = False) -> Any:
        url = f"{self.BASE_URL}{path}"
        params = params or {}

        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)

        for attempt in range(self.MAX_RETRIES):
            try:
                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        logger.warning("Rate limit hit, sleeping 5s")
                        await asyncio.sleep(5)
                        continue
                    if resp.status == 400:
                        # Логируем только для BTC чтобы диагностировать проблему
                        try:
                            err_body = await resp.text()
                        except Exception:
                            err_body = "?"
                        if 'BTCUSDT' in url:
                            logger.warning(f"400 Bad Request BTCUSDT: params={params} body={err_body[:100]}")
                        else:
                            logger.debug(f"400 Bad Request: {url} params={params} body={err_body[:100]}")
                        return None
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as e:
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY)
                else:
                    logger.debug(f"Request failed after retries: {path} — {e}")
        return None

    # ── Список активных символов ─────────────────────────────

    async def get_default_symbols(self) -> Set[str]:
        """Возвращает множество активных спот символов от MEXC."""
        if self._valid_symbols is not None:
            return self._valid_symbols
        data = await self._get("/api/v3/defaultSymbols")
        if data and isinstance(data.get("data"), list):
            self._valid_symbols = set(data["data"])
            logger.info(f"Loaded {len(self._valid_symbols)} active symbols from MEXC")
        else:
            self._valid_symbols = set()
        return self._valid_symbols

    async def refresh_valid_symbols(self):
        """Сбросить кэш и перезагрузить список символов."""
        self._valid_symbols = None
        await self.get_default_symbols()

    # ── Публичные методы ─────────────────────────────────────

    async def get_ticker_24h_all(self) -> List[Dict]:
        data = await self._get("/api/v3/ticker/24hr")
        return data if isinstance(data, list) else []

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 60) -> List[List]:
        data = await self._get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}
        )
        return data if isinstance(data, list) else []

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        data = await self._get("/api/v3/depth", params={"symbol": symbol, "limit": limit})
        return data or {}

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        data = await self._get("/api/v3/trades", params={"symbol": symbol, "limit": limit})
        return data if isinstance(data, list) else []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
