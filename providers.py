"""
Pluggable vision backends. Each backend takes a base64 image + a prompt and
returns transcribed Markdown text. Selected at runtime via --provider.

This is the ONLY provider-specific code. The rest of the pipeline (render,
checkpoint, retry, assemble) is identical regardless of which provider runs
a given page — so you can split a document's pages across providers to spend
down prepaid balances on each.

Install only what you use:
    anthropic   ->  pip install anthropic       (ANTHROPIC_API_KEY)
    openai      ->  pip install openai           (OPENAI_API_KEY)
    gemini      ->  pip install google-genai     (GEMINI_API_KEY)

Default models and prices below were checked against the providers' pricing
pages on 2026-09-24. Override per run with the EXTRACTOR_MODEL env var (or the
--model flag), or edit DEFAULT_MODELS. Re-verify before a large run.

Optional run-wide knobs (set by the CLI flags in extractor.py / compare.py):
    EXTRACTOR_EFFORT        low|medium|high. Anthropic: output_config.effort.
                            OpenAI: reasoning_effort. Gemini 3: thinking_level.
                            Unset = each provider's default (Gemini: lowest
                            thinking level, since thinking is pure overhead
                            for transcription).
    EXTRACTOR_SERVICE_TIER  "flex" = OpenAI / Gemini flex tier (billed at batch
                            prices, ~50% off; slower, may return 429/503).
"""
from __future__ import annotations
import os

DEFAULT_MODELS = {
    # Anthropic: Sonnet 5 is cheaper per token than Sonnet 4.6 ($2/$10 vs
    # $3/$15) and reads images at high resolution (up to 2576 px long edge vs
    # 1568), which matters for small Thai vowel/tone marks.
    "anthropic": "claude-sonnet-5",
    # OpenAI: newest model at gpt-4.1's input price. UNVERIFIED on Thai tables
    # — run compare.py before a full run.
    "openai": "gpt-6-sol",
    # Gemini: same price as 2.5-flash ($0.30/$2.50), newer generation, and a
    # fixed ~1120 tokens per image. UNVERIFIED on Thai tables — run compare.py.
    "gemini": "gemini-3.8-flash",
}


def _model_for(provider: str, model: str | None = None) -> str:
    """An explicit `model` (per call — used by the web app, where several jobs
    with different models run at once in one process) wins over the
    EXTRACTOR_MODEL env var (per run — the CLIs), then DEFAULT_MODELS."""
    return model or os.environ.get("EXTRACTOR_MODEL") or DEFAULT_MODELS[provider]


# Standard per-MTok prices: (input, output), keyed by model ID. Used by
# estimate_cost.py. Snapshot of the providers' pricing pages on 2026-09-24 —
# they change, so verify before a large run.
PRICING = {
    "anthropic": {
        "claude-opus-5-5":   (4.00, 20.00),
        "claude-sonnet-5":   (2.00, 10.00),
        "claude-sonnet-4-6": (3.00, 15.00),
        "claude-haiku-4-5":  (1.00,  5.00),   # retirement: not before 2026-10-15
    },
    "openai": {
        "gpt-6-sol":     (2.00, 10.00),
        "gpt-6-luna":    (0.10,  0.50),
        "gpt-5.6-terra": (2.00, 12.00),
        "gpt-5.4-mini":  (0.75,  4.50),
        "gpt-4.1":       (2.00,  8.00),
        "gpt-4.1-mini":  (0.40,  1.60),
    },
    "gemini": {
        "gemini-3.8-flash":      (0.75, 3.75),  # promo; $1.50/$7.50 from 2027-01-01
        "gemini-3.5-flash-lite": (0.30, 2.50),
        "gemini-3.1-flash-lite": (0.25, 1.50),
        "gemini-2.5-flash":      (0.30, 2.50),
        "gemini-2.5-flash-lite": (0.10, 0.40),
    },
}

# Discount multiplier for the cheap asynchronous path of each provider:
#   anthropic: Message Batches API (batch_extractor.py)       -> 50% off
#   openai:    flex tier (--service-tier flex) or Batch API   -> 50% off
#   gemini:    flex tier (--service-tier flex) or Batch API   -> 50% off
# Prompt caching discounts also stack with batch, but don't apply to this
# workload (see ARCHITECTURE.md §8).
BATCH_MULTIPLIER = {"anthropic": 0.5, "openai": 0.5, "gemini": 0.5}


def _effort() -> str | None:
    v = (os.environ.get("EXTRACTOR_EFFORT") or "").strip().lower()
    return v or None


def _service_tier() -> str | None:
    v = (os.environ.get("EXTRACTOR_SERVICE_TIER") or "").strip().lower()
    return v if v and v not in ("standard", "default", "auto") else None


def gemini_thinking_config(model: str, effort: str | None) -> dict | None:
    """Thinking settings per Gemini family. Thinking tokens are billed as
    output and count against max_output_tokens, but add little to a faithful
    transcription, so default to the lowest level each model allows."""
    m = model.lower()
    if m.startswith("gemini-2.5-flash"):
        return {"thinking_budget": 0}            # 2.5 Flash / Flash-Lite: off
    if m.startswith("gemini-3"):
        if effort in ("low", "medium", "high"):
            return {"thinking_level": effort}
        # 3.7/3.8 Flash and Pro don't accept "minimal"; the rest do.
        if "pro" in m or "3.7-flash" in m or "3.8-flash" in m:
            return {"thinking_level": "low"}
        return {"thinking_level": "minimal"}
    return None                                   # other models: defaults


class TruncatedOutput(Exception):
    """Raised when a provider stopped generating because it hit max_tokens.
    Truncated transcriptions silently drop the end of a page and must be
    treated as failures, not partial successes."""


class EmptyOutput(Exception):
    """Raised when a provider returned no text at all."""


def check_output(text, truncated: bool) -> str:
    """Shared success/failure gate for every backend. Raises TruncatedOutput
    if the model was cut off at the token ceiling, EmptyOutput if there's no
    text, else returns the text unchanged."""
    if truncated:
        raise TruncatedOutput("truncated at max_tokens")
    if not text or not text.strip():
        raise EmptyOutput("empty output")
    return text


def media_type_of(img_b64: str) -> str:
    """Sniff the base64-encoded image's media type from its prefix, since
    render_page_b64() may fall back from PNG to JPEG for oversized pages."""
    if img_b64.startswith("iVBOR"):
        return "image/png"
    if img_b64.startswith("/9j/"):
        return "image/jpeg"
    return "image/png"


def call_anthropic(img_b64: str, prompt: str, max_tokens: int,
                   model: str | None = None) -> str:
    from anthropic import Anthropic
    client = Anthropic()
    extra = {}
    if _effort():
        # extra_body so older SDK versions without output_config still work
        extra["extra_body"] = {"output_config": {"effort": _effort()}}
    msg = client.messages.create(
        model=_model_for("anthropic", model),
        max_tokens=max_tokens,
        **extra,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": media_type_of(img_b64), "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return check_output(text, msg.stop_reason == "max_tokens")


def call_openai(img_b64: str, prompt: str, max_tokens: int,
                model: str | None = None) -> str:
    from openai import OpenAI
    tier = _service_tier()
    # Flex requests can queue for minutes; the docs suggest a 15-min timeout.
    client = OpenAI(timeout=900.0) if tier == "flex" else OpenAI()
    media_type = media_type_of(img_b64)
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {
            "url": f"data:{media_type};base64,{img_b64}"}},
    ]
    # NOTE: no 'temperature' here on purpose — newer OpenAI reasoning models
    # (gpt-5.x) reject a non-default temperature.
    kwargs = dict(model=_model_for("openai", model),
                  messages=[{"role": "user", "content": content}])
    if tier:
        kwargs["service_tier"] = tier
    if _effort():
        kwargs["reasoning_effort"] = _effort()   # reasoning models only
    # Newer OpenAI models (gpt-5.x) require 'max_completion_tokens';
    # older ones (gpt-4.1, gpt-4o) use 'max_tokens'. Try new, fall back.
    try:
        resp = client.chat.completions.create(
            max_completion_tokens=max_tokens, **kwargs)
    except Exception as e:
        if "max_completion_tokens" in str(e) or "max_tokens" in str(e):
            resp = client.chat.completions.create(max_tokens=max_tokens, **kwargs)
        else:
            raise
    text = resp.choices[0].message.content or ""
    truncated = resp.choices[0].finish_reason == "length"
    return check_output(text, truncated)


def gemini_client():
    """google-genai client with the key passed explicitly: the library otherwise
    prefers GOOGLE_API_KEY over GEMINI_API_KEY, so a stray GOOGLE_API_KEY in the
    system environment would be used instead of the key in .env."""
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return genai.Client(api_key=key) if key else genai.Client()


def gemini_generation_config(model: str, max_tokens: int) -> dict:
    """Shared by the live call and the batch JSONL, so both behave the same."""
    cfg = {"max_output_tokens": max_tokens, "temperature": 0}
    thinking = gemini_thinking_config(model, _effort())
    if thinking:
        cfg["thinking_config"] = thinking
    return cfg


def call_gemini(img_b64: str, prompt: str, max_tokens: int,
                model: str | None = None) -> str:
    import base64
    from google import genai
    from google.genai import types
    client = gemini_client()
    model = _model_for("gemini", model)
    # Plain dict config: newer fields (thinking_level, service_tier) need a
    # recent google-genai; upgrade it if you see a validation error.
    cfg = gemini_generation_config(model, max_tokens)
    if _service_tier():
        cfg["service_tier"] = _service_tier()
    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(
                data=base64.b64decode(img_b64), mime_type=media_type_of(img_b64)),
            prompt,
        ],
        config=cfg,
    )
    text = resp.text or ""
    truncated = False
    candidates = getattr(resp, "candidates", None)
    if candidates:
        fr = getattr(candidates[0], "finish_reason", None)
        if fr is not None:
            truncated = str(getattr(fr, "name", fr)).upper().endswith("MAX_TOKENS")
    return check_output(text, truncated)


BACKENDS = {
    "anthropic": call_anthropic,
    "openai": call_openai,
    "gemini": call_gemini,
}


def get_backend(provider: str):
    if provider not in BACKENDS:
        raise ValueError(
            f"unknown provider '{provider}'. Choose from {list(BACKENDS)}")
    return BACKENDS[provider]
