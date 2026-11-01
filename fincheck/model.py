"""Gemini generateContent REST adapter; no SDK or third-party dependencies."""

import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


class ModelError(RuntimeError):
    pass


def load_env(root: Path) -> None:
    path = root / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"GEMINI_API_KEY", "GEMINI_MODEL", "SEC_USER_AGENT"}:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class Gemini:
    def __init__(self):
        self.key = os.getenv("GEMINI_API_KEY", "")
        self.model = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"
        if not self.key:
            raise ModelError("Set GEMINI_API_KEY in your local .env first.")
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", self.model):
            raise ModelError("Invalid GEMINI_MODEL name.")

    def generate(self, messages: list, declarations: list, instruction: str) -> dict:
        body = {"contents": messages, "systemInstruction": {"parts": [{"text": instruction}]},
                "tools": [{"functionDeclarations": declarations}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
                "generationConfig": {"temperature": 0, "maxOutputTokens": 4096}}
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.key})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    payload = json.load(response)
                candidates = payload.get("candidates", [])
                if not candidates or not candidates[0].get("content", {}).get("parts"):
                    raise ModelError("Gemini returned no usable content (possibly a blocked response).")
                return {"content": candidates[0]["content"],
                        "usage": payload.get("usageMetadata"), "retries": attempt}
            except urllib.error.HTTPError as error:
                if error.code in {429, 500, 502, 503, 504} and attempt == 0:
                    try:
                        delay = min(30, max(1, float(error.headers.get("Retry-After", "10"))))
                    except (TypeError, ValueError):
                        delay = 10
                    time.sleep(delay)
                    continue
                # Never echo response bodies, URLs containing credentials, or headers.
                raise ModelError(f"Gemini HTTP {error.code}. Check key, enabled API, model access and quota.") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise ModelError("Gemini connection failed after two attempts.") from None
            except (ValueError, KeyError):
                raise ModelError("Gemini returned malformed JSON.") from None
