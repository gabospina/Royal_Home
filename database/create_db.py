import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "inventory.db"

conn = sqlite3.connect(db_path)

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS inventory (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    product_name TEXT NOT NULL,

    brand TEXT,

    model TEXT,

    price REAL,

    category TEXT,

    condition TEXT,

    photo TEXT

)
""")

conn.commit()

conn.close()

print("Inventory table created successfully.")