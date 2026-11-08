import sqlite3

conn = sqlite3.connect("database/inventory.db")

cursor = conn.cursor()

cursor.execute("""
SELECT name
FROM sqlite_master
WHERE type='table';
""")

for table in cursor.fetchall():
    print(table)

conn.close()
