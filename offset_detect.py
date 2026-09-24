"""
offset_detect.py — work out --page-offset automatically.

Reads the printed page number off a few sample pages with a vision model and
takes the majority vote of (pdf_index - printed_number). A handful of small,
low-resolution calls — a fraction of a baht.
"""
from __future__ import annotations
import re
from collections import Counter

import common
import providers

NUMBER_PROMPT = (
    "This is one page of a printed book. Find the PAGE NUMBER printed in the "
    "page header or footer (not chapter, section or list numbers in the body). "
    "Reply with ONLY that number using Arabic digits (convert Thai digits "
    "๐-๙ to 0-9). If this page has no printed page number, reply NONE."
)
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def parse_number(reply: str) -> int | None:
    m = re.search(r"\d{1,4}", (reply or "").translate(THAI_DIGITS))
    return int(m.group()) if m else None


def sample_pages(n_pages: int, samples: int = 5) -> list[int]:
    """Spread across the middle of the book (front matter often has none)."""
    if n_pages <= samples:
        return list(range(n_pages))
    lo, hi = int(n_pages * 0.25), int(n_pages * 0.75)
    step = max(1, (hi - lo) // max(1, samples - 1))
    return sorted({min(n_pages - 1, lo + i * step) for i in range(samples)})


def detect_offset(doc, provider: str, model: str | None = None,
                  samples: int = 5, dpi: int = 100) -> dict:
    """Returns {"offset": int|None, "agree": int, "readings": [(pdf_index, n|None)],
    "confident": bool}. confident = a clear majority of pages agree."""
    backend = providers.get_backend(provider)
    readings = []
    for pno in sample_pages(len(doc), samples):
        img = common.render_page_b64(doc, pno, dpi)
        try:
            kw = {"model": model} if model else {}
            reply = backend(img, NUMBER_PROMPT, 512, **kw)
        except Exception:
            reply = ""
        readings.append((pno, parse_number(reply)))
    offsets = Counter(p - n for p, n in readings if n is not None)
    if not offsets:
        return {"offset": None, "agree": 0, "readings": readings, "confident": False}
    offset, agree = offsets.most_common(1)[0]
    return {"offset": offset, "agree": agree, "readings": readings,
            "confident": agree >= max(2, (len(readings) + 1) // 2)}
