"""``python -m jobhunter.console.help`` rewrites the generated blocks in every help file."""

from jobhunter.console.help import HELP_DIR, regenerate

if __name__ == "__main__":
    changed = regenerate()
    for path in changed:
        print(f"updated {path.relative_to(HELP_DIR.parent.parent.parent.parent)}")
    if not changed:
        print("help blocks are current")
