"""
compare.py — run the same pages through multiple vision providers side-by-side
so you can judge transcription quality (especially dense Thai tables) before
committing a large, cost-split extraction run.

Defaults: 3 pages starting at page 0, all three providers.

Examples:
    python compare.py doc.pdf
    python compare.py doc.pdf --count 5 --start 180
    python compare.py doc.pdf --pages 2,182,200 --providers anthropic,gemini
    python compare.py doc.pdf --start 50 --count 2 --out ./compare_runs

Output (saved for later review) in --out (default ./comparisons/<timestamp>/):
    report.html          side-by-side rendered view (open in a browser)
    report.md            same content as Markdown
    results.json         raw transcriptions + timing per page/provider
    page_XXXX.png        the rendered source page, for eyeballing against output

Only the providers you actually pass need their SDK + API key installed.
A provider that errors (missing key, bad model, truncated/empty output) is
recorded as an error in the report rather than aborting the whole comparison.
"""
from __future__ import annotations
import argparse, base64, html, json, time
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import providers
import common

common.load_env()

# Same ceiling as extractor.py / batch_extractor.py — see those for rationale.
MAX_TOKENS = 16384


def parse_pages(args, n_pages):
    if args.pages:
        nums = sorted({int(x) for x in args.pages.split(",") if x.strip() != ""})
    else:
        nums = list(range(args.start, args.start + args.count))
    bad = [n for n in nums if n < 0 or n >= n_pages]
    if bad:
        raise SystemExit(f"pages out of range (doc has {n_pages}): {bad}")
    return nums


def split_entry(entry):
    """'gemini' -> ('gemini', default model); 'gemini:gemini-2.5-flash' ->
    ('gemini', 'gemini-2.5-flash'). Lets one report compare several models
    of the same provider side by side."""
    prov, _, model = entry.partition(":")
    prov = prov.strip()
    return prov, (model.strip() or providers.DEFAULT_MODELS[prov])


def run_one(entry, img_b64, retries=2, backoff=4.0):
    """Call one provider[:model] entry. Returns (text, seconds, error).
    Retries transient errors (overload/rate-limit/timeout) a few times.
    TruncatedOutput/EmptyOutput are NOT transient — they're reported as errors
    (clearly labeled) rather than retried."""
    provider, model = split_entry(entry)
    backend = providers.get_backend(provider)
    t0 = time.time()
    import os
    saved = os.environ.pop("EXTRACTOR_MODEL", None)
    os.environ["EXTRACTOR_MODEL"] = model
    transient = ("503", "429", "unavailable", "overload", "high demand",
                 "rate limit", "timeout", "temporarily")
    try:
        last = None
        for attempt in range(retries + 1):
            try:
                text = backend(img_b64, common.PROMPT, MAX_TOKENS)
                return text, round(time.time() - t0, 2), None
            except (providers.TruncatedOutput, providers.EmptyOutput) as e:
                return (None, round(time.time() - t0, 2),
                        f"{type(e).__name__}: {e}")
            except Exception as e:
                last = str(e)[:400]
                if attempt < retries and any(t in last.lower() for t in transient):
                    print(" (retrying)", end="", flush=True)
                    time.sleep(backoff * (2 ** attempt))
                    continue
                return None, round(time.time() - t0, 2), last
        return None, round(time.time() - t0, 2), last
    finally:
        os.environ.pop("EXTRACTOR_MODEL", None)
        if saved is not None:
            os.environ["EXTRACTOR_MODEL"] = saved


def build_html(results, providers_list, models, out_dir):
    def cell(text, err):
        if err:
            return f'<td class="err"><b>ERROR</b><br>{html.escape(err)}</td>'
        return f'<td><pre>{html.escape(text or "")}</pre></td>'

    rows = []
    for r in results:
        pno = r["page"]
        header = (f'<tr><th colspan="{len(providers_list)+1}" class="pghdr">'
                  f'Page {pno} '
                  f'<a href="page_{pno:04d}.png">[source image]</a></th></tr>')
        timing = "<tr><td><b>time (s)</b></td>" + "".join(
            f'<td>{r["by_provider"][p]["seconds"]}</td>'
            for p in providers_list) + "</tr>"
        content = "<tr><td><b>output</b></td>" + "".join(
            cell(r["by_provider"][p]["text"], r["by_provider"][p]["error"])
            for p in providers_list) + "</tr>"
        rows.append(header + timing + content)

    head_cells = "".join(
        f'<th>{p}<br><span class="model">{models[p]}</span></th>'
        for p in providers_list)
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Provider comparison</title><style>
body{{font-family:system-ui,sans-serif;margin:24px;background:#faf9f7}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}
th,td{{border:1px solid #ddd;padding:8px;vertical-align:top;text-align:left}}
th{{background:#f0ede8}} .model{{font-weight:400;color:#888;font-size:11px}}
.pghdr{{background:#2d2a26;color:#fff;font-size:15px}}
.err{{background:#fdecea;color:#a3261c}}
pre{{white-space:pre-wrap;word-wrap:break-word;font-size:12px;margin:0;
font-family:'Sarabun','Noto Sans Thai',monospace}}
td:first-child,th:first-child{{width:90px;background:#f7f5f2}}
</style></head><body>
<h1>Thai PDF — provider comparison</h1>
<p>Generated {datetime.now():%Y-%m-%d %H:%M}. Compare transcription quality,
especially on Thai tables, before committing a cost-split run.</p>
<table><tr><th>field</th>{head_cells}</tr>
{''.join(rows)}
</table></body></html>"""
    (out_dir / "report.html").write_text(doc, encoding="utf-8")


def build_md(results, providers_list, models, out_dir):
    parts = ["# Thai PDF — provider comparison\n",
             f"_Generated {datetime.now():%Y-%m-%d %H:%M}_\n"]
    for r in results:
        parts.append(f"\n## Page {r['page']}  (source: page_{r['page']:04d}.png)\n")
        for p in providers_list:
            d = r["by_provider"][p]
            parts.append(f"\n### {p} — `{models[p]}` ({d['seconds']}s)\n")
            if d["error"]:
                parts.append(f"\n> ERROR: {d['error']}\n")
            else:
                parts.append("\n```\n" + (d["text"] or "") + "\n```\n")
    (out_dir / "report.md").write_text("".join(parts), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--count", type=int, default=3,
                    help="how many pages (default 3)")
    ap.add_argument("--start", type=int, default=0,
                    help="first page, 0-based (default 0)")
    ap.add_argument("--pages", default="",
                    help="explicit comma list e.g. '2,182,200' (overrides "
                         "--count/--start)")
    ap.add_argument("--providers", default="anthropic,openai,gemini",
                    help="comma list of provider or provider:model, e.g. "
                         "'anthropic,gemini:gemini-2.5-flash,"
                         "gemini:gemini-3.5-flash-lite' (default: all three "
                         "providers with their default models)")
    ap.add_argument("--effort", default="", choices=["", "low", "medium", "high"],
                    help="apply one effort/thinking level to every entry")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--out", default="./comparisons",
                    help="base dir; a timestamped subfolder is created")
    args = ap.parse_args()
    if args.effort:
        import os
        os.environ["EXTRACTOR_EFFORT"] = args.effort

    providers_list = [p.strip() for p in args.providers.split(",") if p.strip()]
    for p in providers_list:
        providers.get_backend(split_entry(p)[0])  # validate names early

    doc = fitz.open(args.pdf)
    pages = parse_pages(args, len(doc))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    models = {p: split_entry(p)[1] for p in providers_list}
    print(f"Comparing pages {pages} across {providers_list}")
    print(f"Saving to {out_dir}\n")

    results = []
    for pno in pages:
        pix = doc[pno].get_pixmap(dpi=args.dpi)
        pix.save(str(out_dir / f"page_{pno:04d}.png"))
        img_b64 = common.render_page_b64(doc, pno, args.dpi)
        by_provider = {}
        for p in providers_list:
            print(f"  page {pno} -> {p} ...", end="", flush=True)
            text, secs, err = run_one(p, img_b64)
            by_provider[p] = {"text": text, "seconds": secs, "error": err}
            print(f" {'ERR' if err else 'ok'} ({secs}s)")
        results.append({"page": pno, "by_provider": by_provider})

    (out_dir / "results.json").write_text(
        json.dumps({"pages": pages, "providers": providers_list,
                    "models": models, "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    build_html(results, providers_list, models, out_dir)
    build_md(results, providers_list, models, out_dir)
    print(f"\nDone. Open {out_dir/'report.html'} in a browser to compare.")


if __name__ == "__main__":
    main()
