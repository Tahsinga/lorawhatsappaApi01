"""
Storage for the WhatsApp stock bot (SQLite).

Design rule — stock is NEVER stored as one editable number.
Every change is an append-only *movement* (a signed delta) tagged with
who / when / why. The current quantity is DERIVED:  qty = SUM(all deltas)
for a (branch, sku). This gives you a full audit trail and lets you
reverse any mistake by posting an opposite movement — you never lose the
original figure.

Swapping to your real inventory DB later: keep these function names
(get_user, search_products, list_products, get_qty, get_product_name,
apply_adjustment) and main.py doesn't change.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).with_name("stock.db")
_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# EDIT THESE. wa_id = the sender's WhatsApp number in full international
# form, digits only, no '+' (e.g. Zimbabwe 077... -> 26377...).
# A user may ONLY adjust stock for their own branch.
# ---------------------------------------------------------------------------
SEED_USERS = [
    # wa_id,          name,            branch,   role
    ("263771234567",  "Tashinga",      "SISK",   "manager"),
    ("263772345678",  "Branch Clerk",  "SISK",   "clerk"),
    ("263716187842",  "You",           "SISK",   "manager"),
]

SEED_PRODUCTS = [
    # sku,      name,               branch,   opening_qty
    ("SKU001",  "Sugar 2kg",        "SISK",   120),
    ("SKU002",  "Cooking Oil 2L",   "SISK",   45),
    ("SKU003",  "Mealie Meal 10kg", "SISK",   80),
    ("SKU004",  "Salt 1kg",         "SISK",   200),
]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db() -> None:
    with _write_lock, closing(_conn()) as c, c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                wa_id TEXT PRIMARY KEY, name TEXT, branch TEXT, role TEXT
            );
            CREATE TABLE IF NOT EXISTS products(
                sku TEXT PRIMARY KEY, name TEXT
            );
            CREATE TABLE IF NOT EXISTS movements(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER, branch TEXT, sku TEXT,
                delta REAL, reason TEXT, actor_wa TEXT, note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mov ON movements(branch, sku);
            CREATE TABLE IF NOT EXISTS conversations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                wa_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
                message_type TEXT NOT NULL,
                body TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_ts ON conversations(ts DESC);
            CREATE TABLE IF NOT EXISTS bot_sessions(
                wa_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                branch TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            """
        )
        
        # Always sync users from SEED_USERS (so edits take effect on restart)
        for wa_id, name, branch, role in SEED_USERS:
            c.execute(
                "INSERT OR REPLACE INTO users VALUES(?,?,?,?)",
                (wa_id, name, branch, role)
            )
        
        # Seed products only once (preserve stock history)
        if not c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            for sku, name, branch, qty in SEED_PRODUCTS:
                c.execute("INSERT OR IGNORE INTO products VALUES(?,?)", (sku, name))
                c.execute(
                    "INSERT INTO movements(ts,branch,sku,delta,reason,actor_wa,note)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (int(time.time()), branch, sku, qty, "opening", "system", "seed opening balance"),
                )


@dataclass
class User:
    wa_id: str
    name: str
    branch: str
    role: str


def log_conversation(wa_id: str, direction: str, message_type: str, body: str) -> None:
    """Persist one inbound or outbound WhatsApp message for the dashboard."""
    with _write_lock, closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO conversations(ts,wa_id,direction,message_type,body) VALUES(?,?,?,?,?)",
            (int(time.time()), wa_id, direction, message_type, body),
        )


def list_conversations(limit: int = 100):
    """Return recent messages in chronological order for the dashboard."""
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT id,ts,wa_id,direction,message_type,body "
            "FROM conversations ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_bot_session(wa_id: str):
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT stage, branch FROM bot_sessions WHERE wa_id=?",
            (wa_id,),
        ).fetchone()
    return dict(row) if row else None


def set_bot_session(wa_id: str, stage: str, branch: str = "") -> None:
    with _write_lock, closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO bot_sessions(wa_id,stage,branch,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(wa_id) DO UPDATE SET stage=excluded.stage, "
            "branch=excluded.branch, updated_at=excluded.updated_at",
            (wa_id, stage, branch, int(time.time())),
        )


def get_user(wa_id: str) -> User | None:
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM users WHERE wa_id=?", (wa_id,)).fetchone()
        return User(**r) if r else None


def get_qty(branch: str, sku: str) -> float:
    with closing(_conn()) as c:
        r = c.execute(
            "SELECT COALESCE(SUM(delta),0) AS q FROM movements WHERE branch=? AND sku=?",
            (branch, sku),
        ).fetchone()
        return float(r["q"])


def get_product_name(sku: str) -> str | None:
    with closing(_conn()) as c:
        r = c.execute("SELECT name FROM products WHERE sku=?", (sku,)).fetchone()
        return r["name"] if r else None


def search_products(branch: str, query: str, limit: int = 11):
    """Return [(sku, name, qty)] matching name or sku (case-insensitive)."""
    like = f"%{query.strip()}%"
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT sku,name FROM products WHERE name LIKE ? OR sku LIKE ? ORDER BY name LIMIT ?",
            (like, like, limit),
        ).fetchall()
    return [(r["sku"], r["name"], get_qty(branch, r["sku"])) for r in rows]


def list_products(branch: str, limit: int = 10):
    with closing(_conn()) as c:
        rows = c.execute("SELECT sku,name FROM products ORDER BY name LIMIT ?", (limit,)).fetchall()
    return [(r["sku"], r["name"], get_qty(branch, r["sku"])) for r in rows]


def apply_adjustment(branch: str, sku: str, delta: float, reason: str,
                     actor_wa: str, note: str = "") -> float:
    """Append a movement and return the new derived quantity."""
    with _write_lock, closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO movements(ts,branch,sku,delta,reason,actor_wa,note) VALUES(?,?,?,?,?,?,?)",
            (int(time.time()), branch, sku, float(delta), reason, actor_wa, note),
        )
    return get_qty(branch, sku)
