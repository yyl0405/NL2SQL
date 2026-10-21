"""Synthetic manufacturing ERP data.

Nothing here comes from a real customer. Names, quantities and prices are
fabricated so the repository can run offline without exposing company schema,
credentials or business records.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

CUSTOMERS = [
    {"customer_id": "C001", "customer_name": "杭州智造科技有限公司", "region": "华东", "phone": "13800000001", "sales_rep": "张伟"},
    {"customer_id": "C002", "customer_name": "苏州精工自动化有限公司", "region": "华东", "phone": "13800000002", "sales_rep": "李娜"},
    {"customer_id": "C003", "customer_name": "深圳未来装备有限公司", "region": "华南", "phone": "13800000003", "sales_rep": "王强"},
    {"customer_id": "C004", "customer_name": "广州启航智能有限公司", "region": "华南", "phone": "13800000004", "sales_rep": "陈敏"},
    {"customer_id": "C005", "customer_name": "北京华创机器有限公司", "region": "华北", "phone": "13800000005", "sales_rep": "赵磊"},
    {"customer_id": "C006", "customer_name": "天津恒达制造有限公司", "region": "华北", "phone": "13800000006", "sales_rep": "孙悦"},
]

MATERIALS = [
    ("M001", "伺服电机", "电气件", "台", 20, 1200.0),
    ("M002", "控制器", "电气件", "台", 15, 800.0),
    ("M003", "精密导轨", "机械件", "根", 30, 300.0),
    ("M004", "轴承", "机械件", "个", 50, 60.0),
    ("M005", "线缆", "电气件", "米", 100, 8.0),
    ("M006", "机架", "结构件", "套", 5, 500.0),
    ("M007", "传感器", "电气件", "个", 25, 150.0),
    ("M008", "散热风扇", "电气件", "个", 10, 90.0),
    ("FG001", "整机设备", "成品", "台", 0, 12000.0),
]

# order_id, customer_id, order_date, due_date, status, sales_rep, internal_note
ORDERS = [
    ("ORD-1001", "C001", "2026-06-03", "2026-07-15", "shipped", "张伟", "VIP 客户，优先排产"),
    ("ORD-1002", "C002", "2026-06-08", "2026-06-30", "pending", "李娜", "客户要求月底前到货"),
    ("ORD-1003", "C003", "2026-06-12", "2026-07-20", "shipped", "王强", ""),
    ("ORD-1004", "C004", "2026-05-20", "2026-06-10", "pending", "陈敏", "等待客户确认图纸"),
    ("ORD-1005", "C005", "2026-06-18", "2026-07-25", "pending", "赵磊", ""),
    ("ORD-1006", "C006", "2026-06-25", "2026-07-30", "pending", "孙悦", "新客户首单"),
    ("ORD-1007", "C001", "2026-04-15", "2026-05-30", "closed", "张伟", "已结案"),
    ("ORD-1008", "C003", "2026-04-28", "2026-06-15", "shipped", "王强", ""),
    ("ORD-1009", "C005", "2026-05-05", "2026-06-20", "pending", "赵磊", "客户延迟提货"),
    ("ORD-1010", "C002", "2026-05-30", "2026-07-10", "shipped", "李娜", ""),
    ("ORD-1011", "C004", "2026-06-29", "2026-08-10", "pending", "陈敏", ""),
    ("ORD-1012", "C006", "2026-06-30", "2026-08-15", "pending", "孙悦", "含备件采购"),
]

ORDER_ITEMS = [
    ("ORD-1001", "FG001", 3, 12000.0, 0.02),
    ("ORD-1001", "M003", 10, 300.0, 0.0),
    ("ORD-1002", "FG001", 2, 12000.0, 0.0),
    ("ORD-1003", "FG001", 5, 11800.0, 0.01),
    ("ORD-1004", "M001", 20, 1200.0, 0.05),
    ("ORD-1005", "FG001", 1, 12000.0, 0.0),
    ("ORD-1005", "M007", 30, 150.0, 0.03),
    ("ORD-1006", "FG001", 4, 12000.0, 0.02),
    ("ORD-1007", "FG001", 2, 11500.0, 0.0),
    ("ORD-1008", "M001", 15, 1200.0, 0.02),
    ("ORD-1008", "M002", 10, 800.0, 0.0),
    ("ORD-1009", "FG001", 2, 12000.0, 0.01),
    ("ORD-1010", "M003", 50, 300.0, 0.05),
    ("ORD-1011", "FG001", 3, 12000.0, 0.0),
    ("ORD-1012", "FG001", 2, 12000.0, 0.03),
    ("ORD-1012", "M004", 100, 60.0, 0.02),
]

INVENTORY = [
    ("M001", "WH-A", 18, 4),
    ("M001", "WH-B", 10, 0),
    ("M002", "WH-A", 12, 3),
    ("M002", "WH-B", 5, 0),
    ("M003", "WH-A", 25, 10),
    ("M003", "WH-B", 20, 0),
    ("M004", "WH-A", 60, 20),
    ("M004", "WH-B", 30, 0),
    ("M005", "WH-A", 300, 50),
    ("M005", "WH-B", 120, 0),
    ("M006", "WH-A", 3, 1),
    ("M006", "WH-B", 2, 0),
    ("M007", "WH-A", 20, 5),
    ("M007", "WH-B", 8, 0),
    ("M008", "WH-A", 9, 2),
    ("M008", "WH-B", 6, 0),
    ("FG001", "WH-A", 1, 0),
]

PURCHASE_ORDERS = [
    ("PO-2001", "M001", "上海精工电机", 30, "2026-07-10", "in_transit"),
    ("PO-2002", "M002", "苏州电子科技", 20, "2026-07-05", "in_transit"),
    ("PO-2003", "M003", "无锡精密导轨", 50, "2026-07-20", "in_transit"),
    ("PO-2004", "M004", "宁波轴承集团", 200, "2026-06-25", "received"),
    ("PO-2005", "M001", "上海精工电机", 10, "2026-08-01", "in_transit"),
    ("PO-2006", "M007", "深圳传感技术", 40, "2026-07-15", "in_transit"),
]

WORK_ORDERS = [
    ("WO-3001", "ORD-1001", "FG001", 3, 3, "2026-06-05", "2026-06-20", "completed"),
    ("WO-3002", "ORD-1002", "FG001", 2, 0, "2026-06-10", "2026-06-28", "in_progress"),
    ("WO-3003", "ORD-1003", "FG001", 5, 4, "2026-06-15", "2026-07-10", "in_progress"),
    ("WO-3004", "ORD-1005", "FG001", 1, 1, "2026-06-20", "2026-06-30", "completed"),
    ("WO-3005", "ORD-1006", "FG001", 4, 2, "2026-06-27", "2026-07-20", "in_progress"),
    ("WO-3006", "ORD-1009", "FG001", 2, 1, "2026-05-10", "2026-06-25", "in_progress"),
    ("WO-3007", "ORD-1011", "FG001", 3, 0, "2026-07-01", "2026-08-01", "pending"),
    ("WO-3008", "ORD-1012", "FG001", 2, 0, "2026-07-05", "2026-08-05", "pending"),
]

SHIPMENTS = [
    ("SH-4001", "ORD-1001", 3, "2026-06-20"),
    ("SH-4002", "ORD-1003", 5, "2026-06-30"),
    ("SH-4003", "ORD-1007", 2, "2026-05-20"),
    ("SH-4004", "ORD-1008", 15, "2026-06-01"),
    ("SH-4005", "ORD-1010", 50, "2026-06-05"),
]

BOM = [
    ("FG001", "M001", 2.0),
    ("FG001", "M002", 1.0),
    ("FG001", "M003", 4.0),
    ("FG001", "M006", 1.0),
    ("M001", "M007", 1.0),
    ("M001", "M008", 2.0),
    ("M003", "M004", 2.0),
    ("M003", "M005", 1.0),
]

DIM_LOOKUP = [
    ("material_category", "ELEC", "电气件"),
    ("material_category", "MECH", "机械件"),
    ("material_category", "STRUCT", "结构件"),
    ("material_category", "FG", "成品"),
    ("order_status", "pending", "待发货"),
    ("order_status", "shipped", "已发货"),
    ("order_status", "closed", "已结案"),
]

AUDIT_ROWS = [
    ("AUD-9001", "schema_export", "2026-06-30 09:00:00"),
    ("AUD-9002", "permission_change", "2026-06-30 10:15:00"),
]

CUSTOMER_NAMES = tuple(row["customer_name"] for row in CUSTOMERS)
MATERIAL_NAMES = tuple(row[1] for row in MATERIALS)
MATERIAL_IDS = {row[1]: row[0] for row in MATERIALS}
REGIONS = tuple(dict.fromkeys(row["region"] for row in CUSTOMERS))

_SCHEMA = """
CREATE TABLE sales_order (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    region TEXT NOT NULL,
    order_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL,
    sales_rep TEXT,
    customer_phone TEXT,
    internal_note TEXT
);
CREATE TABLE order_item (
    order_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    discount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, material_id)
);
CREATE TABLE material (
    material_id TEXT PRIMARY KEY,
    material_name TEXT NOT NULL,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    safety_stock INTEGER NOT NULL DEFAULT 0,
    standard_cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE inventory (
    material_id TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    on_hand_qty INTEGER NOT NULL,
    locked_qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (material_id, warehouse)
);
CREATE TABLE purchase_order (
    po_id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    supplier_name TEXT NOT NULL,
    qty INTEGER NOT NULL,
    expected_date TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE work_order (
    wo_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    planned_qty INTEGER NOT NULL,
    completed_qty INTEGER NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE shipment (
    shipment_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    shipped_qty INTEGER NOT NULL,
    ship_date TEXT NOT NULL
);
CREATE TABLE bom (
    parent_material_id TEXT NOT NULL,
    child_material_id TEXT NOT NULL,
    qty_per_parent REAL NOT NULL,
    PRIMARY KEY (parent_material_id, child_material_id)
);
CREATE TABLE dim_lookup (
    lookup_type TEXT NOT NULL,
    lookup_code TEXT NOT NULL,
    lookup_name TEXT NOT NULL,
    PRIMARY KEY (lookup_type, lookup_code)
);
CREATE TABLE internal_audit (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_sales_order_date ON sales_order(order_date);
CREATE INDEX idx_sales_order_customer ON sales_order(customer_name);
CREATE INDEX idx_inventory_material ON inventory(material_id);
CREATE INDEX idx_purchase_status ON purchase_order(status);
CREATE INDEX idx_work_order_end ON work_order(planned_end);
"""


def seed(db_path: str | Path, force: bool = False) -> dict[str, Any]:
    path = Path(db_path)
    if path.exists() and not force:
        return {"created": False, "path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    if force and path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO material (material_id, material_name, category, unit, safety_stock, standard_cost) VALUES (?, ?, ?, ?, ?, ?)",
            MATERIALS,
        )
        customers_by_id = {row["customer_id"]: row for row in CUSTOMERS}
        orders_rows = []
        for order_id, customer_id, order_date, due_date, status, sales_rep, note in ORDERS:
            customer = customers_by_id[customer_id]
            orders_rows.append(
                (
                    order_id,
                    customer_id,
                    customer["customer_name"],
                    customer["region"],
                    order_date,
                    due_date,
                    status,
                    sales_rep,
                    customer["phone"],
                    note,
                )
            )
        conn.executemany(
            """
            INSERT INTO sales_order
                (order_id, customer_id, customer_name, region, order_date, due_date,
                 status, sales_rep, customer_phone, internal_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            orders_rows,
        )
        conn.executemany(
            "INSERT INTO order_item (order_id, material_id, qty, unit_price, discount) VALUES (?, ?, ?, ?, ?)",
            ORDER_ITEMS,
        )
        conn.executemany(
            "INSERT INTO inventory (material_id, warehouse, on_hand_qty, locked_qty) VALUES (?, ?, ?, ?)",
            INVENTORY,
        )
        conn.executemany(
            "INSERT INTO purchase_order (po_id, material_id, supplier_name, qty, expected_date, status) VALUES (?, ?, ?, ?, ?, ?)",
            PURCHASE_ORDERS,
        )
        conn.executemany(
            """
            INSERT INTO work_order
                (wo_id, order_id, material_id, planned_qty, completed_qty,
                 planned_start, planned_end, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            WORK_ORDERS,
        )
        conn.executemany(
            "INSERT INTO shipment (shipment_id, order_id, shipped_qty, ship_date) VALUES (?, ?, ?, ?)",
            SHIPMENTS,
        )
        conn.executemany(
            "INSERT INTO bom (parent_material_id, child_material_id, qty_per_parent) VALUES (?, ?, ?)",
            BOM,
        )
        conn.executemany(
            "INSERT INTO dim_lookup (lookup_type, lookup_code, lookup_name) VALUES (?, ?, ?)",
            DIM_LOOKUP,
        )
        conn.executemany(
            "INSERT INTO internal_audit (audit_id, action, created_at) VALUES (?, ?, ?)",
            AUDIT_ROWS,
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "created": True,
        "path": str(path),
        "orders": len(ORDERS),
        "materials": len(MATERIALS),
        "inventory_rows": len(INVENTORY),
    }


def ensure_demo_db(db_path: str | Path) -> dict[str, Any]:
    return seed(db_path, force=False)
