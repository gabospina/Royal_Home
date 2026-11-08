import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

cursor.execute("DELETE FROM inventory")

conn.commit()
conn.close()

print("Inventory cleared.")