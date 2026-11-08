import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

cursor.execute("""
    SELECT
        id,
        product_name,
        status,
        sold_date
    FROM inventory
""")

for row in cursor.fetchall():
    print(row)

conn.close()