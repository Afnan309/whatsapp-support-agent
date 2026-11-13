"""Store data the agent can look up: customers (by WhatsApp number), orders and returns.

Sample data for a fictional outdoor-gear shop. 'Today' is 2026-11-08 for return-window checks.
Swap this module for Shopify / WooCommerce API calls in production.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "store.db"
TODAY = date(2026, 11, 8)
RETURN_DAYS = 30

CUSTOMERS = [  # whatsapp number (E.164 without +), name, email
    ("15550100101", "Sarah Mitchell", "sarah.m@example.com"), ("15550100102", "David Kim", "dkim@example.com"),
    ("15550100103", "Priya Patel", "priya.p@example.com"), ("15550100104", "Marcus Johnson", "marcus.j@example.com"),
    ("15550100105", "Elena Rossi", "elena.r@example.com"), ("15550100106", "Tom Becker", "tbecker@example.com"),
    ("15550100107", "Aisha Bello", "aisha.b@example.com"), ("15550100108", "Lucas Martin", "lucas.m@example.com"),
]
ORDERS = [  # id, phone, items, total, status, carrier, tracking, ordered, shipped, eta, delivered
    ("PK-10231", "15550100101", "Summit 2P Tent", 289.00, "delivered", "UPS", "1Z84A7W90312345671", "2026-10-12", "2026-10-13", "2026-10-17", "2026-10-16"),
    ("PK-10342", "15550100101", "Trail Runner X (size 9)", 139.00, "shipped", "UPS", "1Z84A7W90312349902", "2026-11-04", "2026-11-05", "2026-11-10", None),
    ("PK-10288", "15550100102", "Alpine 55L Pack, Headlamp Pro", 264.00, "delivered", "FedEx", "773912845501", "2026-09-20", "2026-09-21", "2026-09-25", "2026-09-24"),
    ("PK-10355", "15550100102", "Storm Shell Jacket (M)", 249.00, "processing", None, None, "2026-11-07", None, "2026-11-13", None),
    ("PK-10301", "15550100103", "GPS Trail Watch", 349.00, "delivered", "USPS", "9400111202555842331190", "2026-10-25", "2026-10-26", "2026-10-30", "2026-10-29"),
    ("PK-10319", "15550100104", "Ridge 4P Tent, Compact Stove", 508.00, "shipped", "FedEx", "773912849987", "2026-11-01", "2026-11-03", "2026-11-09", None),
    ("PK-10267", "15550100104", "Granite Hiking Boot (size 11)", 179.00, "delivered", "UPS", "1Z84A7W90312340018", "2026-09-28", "2026-09-29", "2026-10-03", "2026-10-02"),
    ("PK-10333", "15550100105", "Down Puffer Vest (S), Thermal Base Layer", 228.00, "delivered", "USPS", "9400111202555842338821", "2026-10-30", "2026-10-31", "2026-11-04", "2026-11-04"),
    ("PK-10350", "15550100106", "Satellite Messenger", 299.00, "cancelled", None, None, "2026-11-05", None, None, None),
    ("PK-10347", "15550100107", "Titanium Cook Set, Insulated Bottle x2", 169.00, "shipped", "USPS", "9400111202555842339954", "2026-11-03", "2026-11-04", "2026-11-08", None),
    ("PK-10298", "15550100108", "Fleece Hoodie (L)", 89.00, "delivered", "UPS", "1Z84A7W90312342210", "2026-10-22", "2026-10-23", "2026-10-27", "2026-10-26"),
]
CARRIER_URL = {"UPS": "https://www.ups.com/track?tracknum={}", "FedEx": "https://www.fedex.com/fedextrack/?trknbr={}",
               "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={}"}


class Store:
    def __init__(self, path: Path = DB, reset: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        if reset:
            path.unlink(missing_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers(phone TEXT PRIMARY KEY, name TEXT, email TEXT);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, phone TEXT, items TEXT, total REAL, status TEXT,
                carrier TEXT, tracking TEXT, ordered TEXT, shipped TEXT, eta TEXT, delivered TEXT);
            CREATE TABLE IF NOT EXISTS returns(id TEXT PRIMARY KEY, order_id TEXT, reason TEXT, created TEXT);
            CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, phone TEXT, reason TEXT, summary TEXT, created TEXT, status TEXT);""")
        if not self.conn.execute("SELECT 1 FROM customers").fetchone():
            self.conn.executemany("INSERT INTO customers VALUES (?,?,?)", CUSTOMERS)
            self.conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)", ORDERS)
        self.conn.commit()

    def customer(self, phone: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone()
        return dict(r) if r else None

    def order(self, order_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id.upper(),)).fetchone()
        return dict(r) if r else None

    def orders_for(self, phone: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM orders WHERE phone=? ORDER BY ordered DESC", (phone,))]

    def tracking_url(self, o: dict) -> str | None:
        return CARRIER_URL[o["carrier"]].format(o["tracking"]) if o.get("tracking") else None

    def return_eligibility(self, o: dict) -> tuple[bool, str]:
        if self.conn.execute("SELECT 1 FROM returns WHERE order_id=?", (o["id"],)).fetchone():
            return False, "a return is already open for this order"
        if o["status"] != "delivered":
            return False, f"the order is {o['status']}, so it can't be returned yet"
        days = (TODAY - date.fromisoformat(o["delivered"])).days
        if days > RETURN_DAYS:
            return False, f"it was delivered {days} days ago and our return window is {RETURN_DAYS} days"
        return True, f"delivered {days} days ago, within the {RETURN_DAYS}-day window"

    def create_return(self, order_id: str, reason: str) -> str:
        rma = "RMA-" + uuid.uuid4().hex[:6].upper()
        self.conn.execute("INSERT INTO returns VALUES (?,?,?,?)", (rma, order_id, reason, TODAY.isoformat()))
        self.conn.commit()
        return rma

    def create_ticket(self, phone: str, reason: str, summary: str) -> str:
        tid = "TK-" + uuid.uuid4().hex[:5].upper()
        self.conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?, 'open')", (tid, phone, reason, summary, TODAY.isoformat()))
        self.conn.commit()
        return tid
