import os, glob

icons_dir = os.path.join(os.path.dirname(__file__), 'media', 'items', 'icons')

# List CaiPu icons
caipu_icons = sorted([f for f in os.listdir(icons_dir) if f.startswith('CaiPu')])
print(f'CaiPu icons ({len(caipu_icons)}):')
for f in caipu_icons:
    print(f'  {f}')

# List food item icons (1048-1112)
food_icons = sorted([f for f in os.listdir(icons_dir) if f[:4].isdigit() and int(f[:4]) >= 1048])
print(f'\nFood item icons ({len(food_icons)}):')
for f in food_icons:
    print(f'  {f}')
