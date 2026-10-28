import json
import os
import urllib.request

from .config import load_env


class ModelError(RuntimeError):
    pass


def available():
    load_env()
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


def chat(system, user, *, json_mode=False, temperature=0):
    """OpenAI-compatible chat completions; no SDK dependency."""
    if not available():
        raise ModelError("Set LLM_API_KEY and LLM_MODEL to enable model calls")
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": os.environ["LLM_MODEL"],
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise ModelError(str(exc)) from exc
