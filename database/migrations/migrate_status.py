import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

try:

    cursor.execute("""
        ALTER TABLE inventory
        ADD COLUMN status TEXT DEFAULT 'Available'
    """)

    print("Status column added.")

except Exception as e:

    print("Migration already exists:", e)

conn.commit()
conn.close()