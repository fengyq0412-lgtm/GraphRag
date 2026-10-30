"""Load local settings without adding a third-party dependency."""

import os
import re
from pathlib import Path


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
ENV_KEYS = {"LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_TIMEOUT", "LLM_ENABLE_THINKING", "GRAPHRAG_DB"}


def load_env(path=ENV_FILE):
    """Read project .env; explicit process environment variables take precedence."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in ENV_KEYS or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        os.environ[key] = value
