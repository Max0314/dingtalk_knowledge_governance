"""Local preview server against the SQLite copy of the baseline. Dev only."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("KG_DATABASE_URL", "sqlite:///./runtime/local_ui.db")

import uvicorn

if __name__ == "__main__":
    # --port wins; else the harness-assigned PORT (autoPort preview); else 39021.
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else int(os.environ.get("PORT", "39021"))
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)
