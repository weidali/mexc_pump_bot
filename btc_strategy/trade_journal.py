"""
TradeJournal — журнал сделок по BTC стратегии.
Хранит все сделки с результатами, считает статистику.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)


class TradeJournal:
    def __init__(self, db_path: str):
        self.path = db_path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS btc_trades (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date   TEXT NOT NULL,
                    direction    TEXT NOT NULL,
                    entry        REAL NOT NULL,
                    stop_loss    REAL NOT NULL,
                    tp1          REAL NOT NULL,
                    tp2          REAL NOT NULL,
                    risk         REAL NOT NULL,
                    exit_price   REAL,
                    exit_price2  REAL,
                    pnl_total    REAL,
                    status       TEXT,
                    duration_min INTEGER,
                    ny_high      REAL,
                    ny_low       REAL,
                    open_time    TEXT,
                    close_time   TEXT,
                    created_at   TEXT NOT NULL
                )
            """)
            await db.commit()
        logger.info("TradeJournal initialized")

    async def save_trade(self, result) -> int:
        now = datetime.now(timezone.utc).isoformat()
        s = result.setup
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                INSERT INTO btc_trades
                (trade_date, direction, entry, stop_loss, tp1, tp2, risk,
                 exit_price, exit_price2, pnl_total, status, duration_min,
                 ny_high, ny_low, open_time, close_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s.setup_time[:10],
                s.direction,
                s.entry_price,
                s.stop_loss,
                s.tp1,
                s.tp2,
                s.risk,
                result.exit_price,
                result.exit_price_tp2,
                result.pnl_total,
                result.status.value,
                result.duration_min,
                s.ny_range.high,
                s.ny_range.low,
                result.open_time,
                result.close_time,
                now,
            ))
            await db.commit()
            return cursor.lastrowid

    async def get_stats(self, days: int = 30) -> Dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM btc_trades WHERE created_at >= ? ORDER BY created_at DESC",
                (since,)
            ) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]

        if not rows:
            return {"total": 0}

        wins   = [r for r in rows if r["pnl_total"] and r["pnl_total"] > 0]
        losses = [r for r in rows if r["pnl_total"] and r["pnl_total"] <= 0]
        full_wins = [r for r in rows if r["status"] == "win_full"]
        partial   = [r for r in rows if r["status"] == "win_partial"]

        total_pnl = sum(r["pnl_total"] for r in rows if r["pnl_total"])
        avg_win  = sum(r["pnl_total"] for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r["pnl_total"] for r in losses) / len(losses) if losses else 0
        winrate  = len(wins) / len(rows) * 100 if rows else 0

        return {
            "total":       len(rows),
            "wins":        len(wins),
            "losses":      len(losses),
            "full_wins":   len(full_wins),
            "partial":     len(partial),
            "winrate":     round(winrate, 1),
            "total_pnl":   round(total_pnl, 2),
            "avg_win":     round(avg_win, 2),
            "avg_loss":    round(avg_loss, 2),
            "profit_factor": round(abs(sum(r["pnl_total"] for r in wins) /
                               sum(r["pnl_total"] for r in losses)), 2) if losses else 999,
            "days":        days,
        }

    async def get_recent_trades(self, limit: int = 10) -> List[Dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM btc_trades ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    @staticmethod
    def format_stats(s: Dict) -> str:
        if s.get("total", 0) == 0:
            return "📭 Сделок пока нет."

        pnl_icon = "✅" if s["total_pnl"] >= 0 else "❌"
        return (
            f"📊 <b>Статистика BTC стратегии ({s['days']}д)</b>\n\n"
            f"📋 Всего сделок: <b>{s['total']}</b>\n"
            f"✅ Прибыльных: <b>{s['wins']}</b> ({s['winrate']}%)\n"
            f"  • Полная цель (TP2): {s['full_wins']}\n"
            f"  • Частичная (TP1+б/у): {s['partial']}\n"
            f"❌ Убыточных: <b>{s['losses']}</b>\n\n"
            f"💰 Общий P&L: {pnl_icon} <b>${s['total_pnl']:+,.2f}</b> на 1 BTC\n"
            f"📈 Средняя прибыль: <b>+${s['avg_win']:,.2f}</b>\n"
            f"📉 Средний убыток: <b>-${abs(s['avg_loss']):,.2f}</b>\n"
            f"⚖️ Profit Factor: <b>{s['profit_factor']}</b>"
        )
