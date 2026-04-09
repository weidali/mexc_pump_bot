"""
MEXC Futures (Contract) API client.
Base URL: https://contract.mexc.com
Docs: https://mexcdevelop.github.io/apidocs/contract_v1_en/

Используемые эндпоинты:
  GET /api/v1/contract/detail          — список фьючерсных контрактов
  GET /api/v1/contract/funding_rate/{symbol} — текущий funding rate
  GET /api/v1/contract/index_price/{symbol}  — индексная цена
  GET /api/v1/contract/ticker          — тикер (содержит holdVol = OI)
"""
import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class MEXCFuturesClient:
    BASE_URL = "https://contract.mexc.com"
    MAX_RETRIES = 2
    RETRY_DELAY = 1.0

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        # Кэш: спот-символ → фьючерсный символ (BTC_USDT etc)
        self._symbol_map: Dict[str, str] = {}
        self._map_loaded = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        url = f"{self.BASE_URL}{path}"
        for attempt in range(self.MAX_RETRIES):
            try:
                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        continue
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    # MEXC futures оборачивает ответ в {"success": true, "data": ...}
                    if isinstance(data, dict) and data.get("success"):
                        return data.get("data")
                    return None
            except aiohttp.ClientError as e:
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.RETRY_DELAY)
                else:
                    logger.debug(f"Futures request failed: {path} — {e}")
        return None

    # ── Маппинг спот → фьючерс ───────────────────────────────

    async def _load_symbol_map(self):
        """Загружает список контрактов и строит маппинг BTCUSDT → BTC_USDT."""
        if self._map_loaded:
            return
        data = await self._get("/api/v1/contract/detail")
        if not data:
            return
        contracts = data if isinstance(data, list) else data.get("list", [])
        for c in contracts:
            # symbol в futures: "BTC_USDT", baseCoin: "BTC", quoteCoin: "USDT"
            fut_symbol = c.get("symbol", "")
            base = c.get("baseCoin", "")
            quote = c.get("quoteCoin", "")
            if base and quote:
                spot_symbol = f"{base}{quote}"   # BTCUSDT
                self._symbol_map[spot_symbol] = fut_symbol
        self._map_loaded = True
        logger.info(f"Futures symbol map loaded: {len(self._symbol_map)} contracts")

    async def to_futures_symbol(self, spot_symbol: str) -> Optional[str]:
        """Конвертирует BTCUSDT → BTC_USDT."""
        await self._load_symbol_map()
        return self._symbol_map.get(spot_symbol)

    # ── Публичные методы ─────────────────────────────────────

    async def get_funding_rate(self, spot_symbol: str) -> Optional[Dict]:
        """
        Возвращает dict:
          fundingRate      float  — текущий funding rate
          nextSettleTime   int    — timestamp следующего расчёта
          maxFundingRate   float
          minFundingRate   float
        """
        fut = await self.to_futures_symbol(spot_symbol)
        if not fut:
            return None
        data = await self._get(f"/api/v1/contract/funding_rate/{fut}")
        if not data:
            return None
        return {
            "symbol":          spot_symbol,
            "futures_symbol":  fut,
            "funding_rate":    float(data.get("fundingRate", 0)),
            "max_funding":     float(data.get("maxFundingRate", 0)),
            "min_funding":     float(data.get("minFundingRate", 0)),
            "next_settle":     data.get("nextSettleTime"),
        }

    async def get_open_interest(self, spot_symbol: str) -> Optional[Dict]:
        """
        OI берём из тикера — поле holdVol (количество контрактов).
        Возвращает dict:
          open_interest      float  — OI в контрактах
          open_interest_usdt float  — OI в USDT (приблизительно)
          last_price         float
          volume24h          float
        """
        fut = await self.to_futures_symbol(spot_symbol)
        if not fut:
            return None
        data = await self._get("/api/v1/contract/ticker", params={"symbol": fut})
        if not data:
            return None
        # ticker возвращает список или один объект
        ticker = data[0] if isinstance(data, list) else data
        try:
            hold_vol    = float(ticker.get("holdVol", 0))
            last_price  = float(ticker.get("lastPrice", 0))
            vol24h      = float(ticker.get("volume24", 0))
            oi_usdt     = hold_vol * last_price
        except (TypeError, ValueError):
            return None

        return {
            "symbol":              spot_symbol,
            "futures_symbol":      fut,
            "open_interest":       hold_vol,
            "open_interest_usdt":  oi_usdt,
            "last_price":          last_price,
            "volume24h":           vol24h,
        }

    async def get_oi_and_funding(self, spot_symbol: str) -> Optional[Dict]:
        """Получает OI и Funding Rate одним вызовом (параллельно)."""
        oi, fr = await asyncio.gather(
            self.get_open_interest(spot_symbol),
            self.get_funding_rate(spot_symbol),
            return_exceptions=True
        )
        if isinstance(oi, Exception): oi = None
        if isinstance(fr, Exception): fr = None
        if oi is None and fr is None:
            return None
        return {
            "symbol":             spot_symbol,
            "open_interest":      oi.get("open_interest")      if oi else None,
            "open_interest_usdt": oi.get("open_interest_usdt") if oi else None,
            "funding_rate":       fr.get("funding_rate")       if fr else None,
            "next_settle":        fr.get("next_settle")        if fr else None,
        }

    async def get_all_tickers(self) -> List[Dict]:
        """Все тикеры фьючерсов — для быстрого batch-сбора OI."""
        data = await self._get("/api/v1/contract/ticker")
        if not data:
            return []
        return data if isinstance(data, list) else []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
