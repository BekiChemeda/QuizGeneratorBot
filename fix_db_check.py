import os

def fix_file(filepath, replacements):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    new_content = content
    for old, new in replacements.items():
        new_content = new_content.replace(old, new)
    
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Fixed {filepath}")
    else:
        print(f"No changes needed for {filepath}")

# Fix app/utils.py
fix_file('app/utils.py', {
    'if not db:': 'if db is None:'
})

# Fix app/bot.py
# We need to be careful with "if db:" because it might match "if db_something:"
# But "if db:" (with colon) is safe.
# Also "if db else"
fix_file('app/bot.py', {
    'if db:': 'if db is not None:',
    'if db else': 'if db is not None else',
    'if not db:': 'if db is None:'
})
