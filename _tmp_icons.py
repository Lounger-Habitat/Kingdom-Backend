import sqlite3
conn = sqlite3.connect('db.sqlite3')
cur = conn.cursor()

# Check existing recipe item icons
cur.execute("SELECT item_id, item_name, icon_image FROM items_item WHERE item_id BETWEEN 3001 AND 3030")
print('Recipe items:')
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]:20s} icon={r[2]}')

# Check existing food item icons
cur.execute("SELECT item_id, item_name, icon_image FROM items_item WHERE item_id BETWEEN 1048 AND 1112 AND item_type = '食物' ORDER BY item_id")
print('\nFood items:')
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]:20s} icon={r[2]}')

conn.close()
