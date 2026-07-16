"""
Tracks which ASINs we've already posted, so scheduled runs don't repost
the same deal. Backed by a plain JSON file that the GitHub Actions
workflow commits back into the repo after each run (see
.github/workflows/post-deals.yml).
"""
import json
from pathlib import Path

STORAGE_PATH = Path(__file__).resolve().parent.parent / "data" / "posted_deals.json"
MAX_HISTORY = 2000  # cap file size so it doesn't grow forever


def load_posted(path: Path = STORAGE_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return set(data.get("asins", []))
    except (json.JSONDecodeError, OSError):
        # corrupt or unreadable file — start fresh rather than crash the run
        return set()


def save_posted(asins: set[str], path: Path = STORAGE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # keep only the most recently added entries
    trimmed = list(asins)[-MAX_HISTORY:]
    with open(path, "w") as f:
        json.dump({"asins": trimmed}, f, indent=2)
