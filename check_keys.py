"""
check_keys.py — show which API key each provider will actually use, and
(optionally) test each one with a free "list models" call.

    python check_keys.py          # where each key comes from (masked)
    python check_keys.py --ping   # also ask each provider if the key works

Keys are never printed in full — only the last 4 characters.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

VARS = {"anthropic": ["ANTHROPIC_API_KEY"],
        "openai": ["OPENAI_API_KEY"],
        "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]}


def mask(v):
    return f"...{v[-4:]} (len {len(v)})" if v else "(not set)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ping", action="store_true")
    args = ap.parse_args()

    system = {k: os.environ.get(k) for vs in VARS.values() for k in vs}
    import common
    common.load_env()
    here = Path(__file__).resolve().parent
    print(f".env found: {(here / '.env').exists() or (Path.cwd() / '.env').exists()}\n")

    for prov, names in VARS.items():
        for k in names:
            now, before = os.environ.get(k), system[k]
            src = ("(not set)" if not now else
                   ".env" if now != before and before is None else
                   ".env (overrode a DIFFERENT system value!)" if now != before else
                   "system environment / .env (same value)")
            print(f"{prov:9s} {k:18s} {mask(now):22s} source: {src}")
    print()

    if not args.ping:
        return
    tests = {
        "anthropic": lambda: len(list(__import__("anthropic").Anthropic().models.list())),
        "openai": lambda: len(list(__import__("openai").OpenAI().models.list())),
        "gemini": lambda: _gemini(),
    }
    for prov, fn in tests.items():
        try:
            print(f"{prov:9s} OK ({fn()} models visible)")
        except ImportError:
            print(f"{prov:9s} SDK not installed — skipped")
        except Exception as e:
            print(f"{prov:9s} FAILED: {str(e)[:200]}")


def _gemini():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    # Keep the client in a variable: a temporary Client() gets garbage-
    # collected (and closed) before the lazy model list finishes loading.
    client = genai.Client(api_key=key)
    names = [m.name for m in client.models.list()]
    wanted = [n for n in names if "3.8-flash" in n or "3.5-flash-lite" in n]
    return f"{len(names)}; e.g. {wanted[:3]}"


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
