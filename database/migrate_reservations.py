import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS reservations (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    inventory_id INTEGER,

    customer_name TEXT,

    phone TEXT,

    email TEXT,

    message TEXT,

    reservation_date TEXT

)
""")

conn.commit()
conn.close()

print("Reservations table ready.")