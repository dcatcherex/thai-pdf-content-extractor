"""
quality.py — cheap, offline checks that flag pages a person should review.

No API calls. Each check returns a short Thai label for the review screen.
"""
from __future__ import annotations
import re

# Thai marks that should never appear twice in a row (above/below vowels,
# tone marks, thanthakhat...). A doubled mark is the same corruption this tool
# exists to avoid, so it signals a misread.
_MARKS = r"\u0E31\u0E34-\u0E3A\u0E47-\u0E4E"
DOUBLED_MARK = re.compile(f"([{_MARKS}])\\1")
# two DIFFERENT tone marks in a row (identical pairs are caught above)
TWO_TONE_MARKS = re.compile(r"([\u0E48-\u0E4B])(?!\1)[\u0E48-\u0E4B]")

_SEP_CELL = re.compile(r"^:?-{2,}:?$")


def doubled_marks(text: str) -> int:
    return len(DOUBLED_MARK.findall(text)) + len(TWO_TONE_MARKS.findall(text))


def table_empty_ratio(text: str) -> float | None:
    """Share of empty cells across all Markdown tables on the page, or None
    if there is no table big enough to judge (fewer than 6 body cells)."""
    total = empty = 0
    for line in text.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        filled = [c for c in cells if c]
        if filled and all(_SEP_CELL.match(c) for c in filled):
            continue                                  # |---|---| separator row
        total += len(cells)
        empty += sum(1 for c in cells if c in ("", "-", "—", "–"))
    if total < 6:
        return None
    return empty / total


def page_flags(markdown: str | None, status: str | None = "done",
               error: str | None = None) -> list[str]:
    """Reasons (Thai) this page should be reviewed. Empty list = looks fine."""
    if status != "done" or markdown is None:
        if error and "Truncated" in error:
            return ["ถอดความไม่จบหน้า (ยาวเกินกำหนด)"]
        if error and "Empty" in error:
            return ["โมเดลไม่ส่งข้อความกลับมา"]
        return ["ยังไม่ได้ถอดความ / ล้มเหลว"]
    flags = []
    body = re.sub(r"<!--.*?-->", "", markdown, flags=re.S).strip()
    if len(body) < 40 and "[FIGURE" not in body:
        flags.append("ข้อความสั้นมาก (อาจเป็นหน้าว่าง หรืออ่านไม่ครบ)")
    n = doubled_marks(body)
    if n:
        flags.append(f"พบสระ/วรรณยุกต์ซ้อนผิดปกติ {n} จุด")
    r = table_empty_ratio(body)
    if r is not None and r > 0.4:
        flags.append(f"ตารางมีช่องว่างมาก ({r:.0%}) อาจอ่านตารางไม่ครบ")
    return flags
