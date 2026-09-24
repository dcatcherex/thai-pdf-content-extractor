"""
estimate_cost.py — estimate what an extraction run will cost, per provider and
model, before you commit to it.

It renders a sample of pages, computes each model's image-token count from the
page size (every provider counts image tokens differently), adds the prompt,
and multiplies by the per-MTok prices in providers.PRICING. Output tokens can't
be measured without running, so they're an estimated range (Thai markdown is
token-heavy).

Usage:
    python estimate_cost.py doc.pdf
    python estimate_cost.py doc.pdf --dpi 200
    python estimate_cost.py doc.pdf --pages 0-199 --out-tokens 600-1000

Image-token rules used (checked 2026-09-24):
  - Claude 4.7+ (Sonnet 5, Opus 5.x, ...): 28x28-px patches,
    ceil(w/28)*ceil(h/28), after downscaling to <=2576 px long edge and
    <=4784 tokens. Older Claude (Sonnet 4.6, Haiku 4.5): <=1568 px, <=1568 tokens.
  - Gemini 3.x: fixed ~1120 tokens/image (default media resolution).
    Gemini 2.5: 258 tokens per 768x768 tile.
  - OpenAI: varies by model; (w*h)/750 is only a rough proxy — use OpenAI's
    image cost calculator for exact numbers.
  - Claude 4.7+ use a newer tokenizer (~30% more tokens for the same text), so
    their output estimate is scaled by 1.3.

Caveats: output tokens are an estimate; prices are a snapshot — verify before a
large run. "Cheap path" = Anthropic Batch API, or OpenAI/Gemini flex tier (or
their batch APIs) — all ~50% off.

Requires: pip install pymupdf
"""
from __future__ import annotations
import argparse, math
from pathlib import Path

import fitz  # PyMuPDF
import common
from providers import PRICING, BATCH_MULTIPLIER

CLAUDE_HIGH_RES = ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-7",
                   "claude-opus-4-8", "claude-fable", "claude-mythos")


def _claude_tokens(w, h, max_edge, max_tokens):
    s = min(1.0, max_edge / max(w, h))
    while True:
        t = math.ceil(w * s / 28) * math.ceil(h * s / 28)
        if t <= max_tokens:
            return t
        s *= math.sqrt(max_tokens / t) * 0.995


def image_tokens(model: str, w: int, h: int) -> float:
    m = model.lower()
    if m.startswith("claude"):
        if m.startswith(CLAUDE_HIGH_RES):
            return _claude_tokens(w, h, 2576, 4784)
        return _claude_tokens(w, h, 1568, 1568)
    if m.startswith("gemini-3"):
        return 1120
    if m.startswith("gemini-2.5"):
        if w <= 384 and h <= 384:
            return 258
        return math.ceil(w / 768) * math.ceil(h / 768) * 258
    return (w * h) / 750          # OpenAI: rough proxy


def output_factor(model: str) -> float:
    return 1.3 if model.lower().startswith(CLAUDE_HIGH_RES) else 1.0


def sample_sizes(doc, pages, dpi):
    sample = pages[:: max(1, len(pages) // 8)] or pages[:1]
    sizes = []
    for p in sample:
        pix = doc[p].get_pixmap(dpi=dpi)
        sizes.append((pix.width, pix.height))
    return sizes


def fmt(x):
    return f"${x:,.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--pages", default="",
                    help="restrict to a range e.g. '0-199' (default: all)")
    ap.add_argument("--out-tokens", default="750-1100",
                    help="estimated output tokens/page range (default 750-1100)")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    n_pages = len(doc)
    pages = common.parse_pages_arg(args.pages, n_pages)
    npg = len(pages)
    sizes = sample_sizes(doc, pages, args.dpi)
    prompt_tok = len(common.PROMPT) / 3.5
    olo, ohi = (int(x) for x in args.out_tokens.split("-"))
    w, h = sizes[0]

    print(f"\nPDF: {Path(args.pdf).name}")
    print(f"Pages to process: {npg} (of {n_pages})  |  DPI: {args.dpi}  |  "
          f"page render ~{w}x{h}px")
    print(f"Prompt: ~{prompt_tok:.0f} tokens/page.  Output estimate: "
          f"{olo}-{ohi} tokens/page (x1.3 on Claude 4.7+ tokenizer)\n")

    for prov, models in PRICING.items():
        disc = BATCH_MULTIPLIER.get(prov, 1.0)
        cheap = "BATCH (-50%)" if prov == "anthropic" else "FLEX/BATCH (-50%)"
        print(prov.upper())
        print(f"  {'model':24s} {'img tok/pg':>10s} {'standard':>18s} {cheap:>20s}")
        for model, (pin, pout) in models.items():
            img = sum(image_tokens(model, a, b) for a, b in sizes) / len(sizes)
            tin = (img + prompt_tok) * npg / 1e6
            f = output_factor(model)
            lo = tin * pin + olo * f * npg / 1e6 * pout
            hi = tin * pin + ohi * f * npg / 1e6 * pout
            print(f"  {model:24s} {img:>10,.0f} {fmt(lo)+'–'+fmt(hi):>18s} "
                  f"{fmt(lo*disc)+'–'+fmt(hi*disc):>20s}")
        print()

    print("Notes: OpenAI image tokens are a rough proxy. Prices from "
          "providers.PRICING (snapshot 2026-09-24) — verify before a large run.")


if __name__ == "__main__":
    main()
