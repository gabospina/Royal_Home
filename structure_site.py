from pathlib import Path

IGNORE = {
    "venv",
    "__pycache__",
    ".git",
    ".vscode",
    ".idea",
    "node_modules"
}

def print_tree(path, prefix=""):
    items = [
        item for item in sorted(path.iterdir(),
        key=lambda x: (x.is_file(), x.name.lower()))
        if item.name not in IGNORE
    ]

    for i, item in enumerate(items):
        connector = "└── " if i == len(items)-1 else "├── "

        print(prefix + connector + item.name)

        if item.is_dir():
            extension = "    " if i == len(items)-1 else "│   "
            print_tree(item, prefix + extension)

root = Path.cwd()

print(root.name)
print_tree(root)