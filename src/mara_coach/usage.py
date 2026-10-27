"""Claude API usage and cost: one `api_usage` row per coach answer, and a spend report.

Rows hold token counts and cost only, never question or answer text. Cost is computed at write
time from PRICES, so a later price change doesn't rewrite history.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

PRICES_AS_OF = "2026-10-06"  # Anthropic first-party list prices (claude-api skill model table)

# USD per million tokens: input, output, cache read, cache write (5-minute TTL).
# Cache writes are 1.25x input; Haiku's cache read is 0.1x input (derived, not listed).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def cost_usd(model: str, tokens: dict[str, int]) -> float | None:
    """Cost of one answer, or None if the model has no price here."""
    price = PRICES.get(model)
    if price is None:
        return None
    return sum(tokens.get(k, 0) * p for k, p in zip(TOKEN_KEYS, price, strict=True)) / 1e6


def record(
    conn: sqlite3.Connection,
    channel: str,
    model: str,
    tokens: dict[str, int],
    steps: int,
    at: str | None = None,
) -> None:
    columns = ["channel", "model", *TOKEN_KEYS, "steps", "cost_usd"]
    values: list[Any] = [channel, model, *(tokens.get(k, 0) for k in TOKEN_KEYS), steps]
    values.append(cost_usd(model, tokens))
    if at is not None:
        columns.append("at")
        values.append(at)
    with conn:
        conn.execute(
            f"INSERT INTO api_usage({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
            values,
        )


def rows(conn: sqlite3.Connection, since: str | None = None) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM api_usage WHERE at >= ? ORDER BY at, id", (since or "",))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def report(conn: sqlite3.Connection, since: str, today: str) -> dict[str, Any]:
    """Spend from `since` through `today` (UTC dates): totals, per channel and model, per answer,
    and a 30-day projection at the same daily rate."""
    found = rows(conn, since)
    days = (date.fromisoformat(today) - date.fromisoformat(since)).days + 1
    total = sum(r["cost_usd"] or 0 for r in found)

    def group(key: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in found:
            g = out.setdefault(r[key], {"answers": 0, "cost_usd": 0.0})
            g["answers"] += 1
            g["cost_usd"] += r["cost_usd"] or 0
        return out

    return {
        "since": since,
        "through": today,
        "days": days,
        "answers": len(found),
        "cost_usd": round(total, 4),
        "avg_cost_per_answer_usd": round(total / len(found), 4) if found else None,
        "projected_30_day_usd": round(total / days * 30, 2) if days > 0 else None,
        "tokens": {k: sum(r[k] for r in found) for k in TOKEN_KEYS},
        "by_channel": group("channel"),
        "by_model": group("model"),
        "unpriced_answers": sum(1 for r in found if r["cost_usd"] is None),
        "prices_as_of": PRICES_AS_OF,
    }


def since_days(today: str, days: int) -> str:
    return (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
