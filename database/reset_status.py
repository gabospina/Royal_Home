import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

cursor.execute("""
    UPDATE inventory
    SET
        status='Available',
        sold_date=NULL
""")

conn.commit()

conn.close()

print("All inventory reset to Available.")