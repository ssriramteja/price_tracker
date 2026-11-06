"""SQLite storage for comparisons (products), their listings and price history."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("PRICE_DB", Path(__file__).resolve().parent.parent / "data" / "prices.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    url TEXT,
    site TEXT NOT NULL,
    title TEXT,
    price REAL,
    currency TEXT,
    shipping REAL NOT NULL DEFAULT 0,
    in_stock INTEGER,
    manual INTEGER NOT NULL DEFAULT 0,
    method TEXT,
    error TEXT,
    last_checked TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price REAL NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_listing ON price_history(listing_id, checked_at);
"""

LISTING_FIELDS = {"url", "site", "title", "price", "currency", "shipping", "in_stock",
                  "manual", "method", "error", "last_checked"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- products

def create_product(name: str) -> dict:
    with connect() as c:
        cur = c.execute("INSERT INTO products(name, created_at) VALUES (?, ?)", (name, now()))
        return get_product(cur.lastrowid, c)


def get_product(pid: int, c: sqlite3.Connection | None = None) -> dict | None:
    conn = c or connect()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def list_products() -> list[dict]:
    with connect() as c:
        return _rows(c.execute("""
            SELECT p.*, COUNT(l.id) AS listing_count,
                   MIN(CASE WHEN l.price IS NOT NULL AND COALESCE(l.in_stock, 1) != 0
                            THEN l.price + l.shipping END) AS best_total
            FROM products p LEFT JOIN listings l ON l.product_id = p.id
            GROUP BY p.id ORDER BY p.created_at DESC
        """))


def rename_product(pid: int, name: str) -> None:
    with connect() as c:
        c.execute("UPDATE products SET name = ? WHERE id = ?", (name, pid))


def delete_product(pid: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM products WHERE id = ?", (pid,))


# ---------------------------------------------------------------- listings

def add_listing(product_id: int, **fields) -> dict:
    data = {k: v for k, v in fields.items() if k in LISTING_FIELDS}
    data.update(product_id=product_id, created_at=now())
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    with connect() as c:
        cur = c.execute(f"INSERT INTO listings({cols}) VALUES ({marks})", tuple(data.values()))
        lid = cur.lastrowid
        if data.get("price"):
            c.execute("INSERT INTO price_history(listing_id, price, checked_at) VALUES (?, ?, ?)",
                      (lid, data["price"], data.get("last_checked") or now()))
    return get_listing(lid)


def get_listing(lid: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM listings WHERE id = ?", (lid,)).fetchone()
        return dict(row) if row else None


def listings_for(product_id: int) -> list[dict]:
    with connect() as c:
        return _rows(c.execute("SELECT * FROM listings WHERE product_id = ?", (product_id,)))


def update_listing(lid: int, record_price: bool = False, **fields) -> dict | None:
    data = {k: v for k, v in fields.items() if k in LISTING_FIELDS}
    with connect() as c:
        if data:
            sets = ", ".join(f"{k} = ?" for k in data)
            c.execute(f"UPDATE listings SET {sets} WHERE id = ?", (*data.values(), lid))
        if record_price and data.get("price"):
            c.execute("INSERT INTO price_history(listing_id, price, checked_at) VALUES (?, ?, ?)",
                      (lid, data["price"], data.get("last_checked") or now()))
    return get_listing(lid)


def delete_listing(lid: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM listings WHERE id = ?", (lid,))


def history(lid: int, limit: int = 60) -> list[dict]:
    with connect() as c:
        rows = c.execute("""SELECT price, checked_at FROM price_history WHERE listing_id = ?
                            ORDER BY checked_at DESC, id DESC LIMIT ?""", (lid, limit))
        return list(reversed(_rows(rows)))
