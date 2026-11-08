import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

try:

    cursor.execute("""
        ALTER TABLE inventory
        ADD COLUMN sold_date TEXT
    """)

    print("sold_date column added.")

except Exception as e:

    print(e)

conn.commit()
conn.close()