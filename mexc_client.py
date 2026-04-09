"""
MEXC REST API client (публичные + приватные эндпоинты).
Использует aiohttp для async запросов.
"""
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)


class MEXCClient:
    BASE_URL = "https://api.mexc.com"
    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(self, api_key: str = "", secret: str = ""):
        self.api_key = api_key
        self.secret = secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
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

    async def _get(
        self,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = False
    ) -> Any:
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
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as e:
                logger.warning(f"Request failed ({attempt+1}/{self.MAX_RETRIES}): {e}")
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
        return None

    # ── Публичные методы ─────────────────────────────────────

    async def get_ticker_24h_all(self) -> List[Dict]:
        """Все тикеры 24ч — цена, объём, изменение."""
        data = await self._get("/api/v3/ticker/24hr")
        return data if isinstance(data, list) else []

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 60
    ) -> List[List]:
        """
        Свечи OHLCV.
        Возвращает список: [open_time, open, high, low, close, volume, ...]
        """
        data = await self._get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}
        )
        return data if isinstance(data, list) else []

    async def get_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        """Стакан заявок."""
        data = await self._get(
            "/api/v3/depth",
            params={"symbol": symbol, "limit": limit}
        )
        return data or {}

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        """
        Последние сделки.
        Поле isBuyerMaker: True → продавец агрессор (sell trade)
        """
        data = await self._get(
            "/api/v3/trades",
            params={"symbol": symbol, "limit": limit}
        )
        return data if isinstance(data, list) else []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
