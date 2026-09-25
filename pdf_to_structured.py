"""
Structured extractor for NCERT-style solution PDFs (no AI / no API calls).

The equations in these PDFs are embedded IMAGES, not text. Plain text
extraction therefore drops every formula. This script:

  1. Reads words + image boxes from each page with their positions.
  2. Rebuilds lines in reading order, putting an [[eq:<file>]] placeholder
     where each equation image sits.
  3. Crops each equation to a PNG file.
  4. Splits the stream into Exercise -> Question -> {question, solution}.
  5. Writes JSON (for your app/DB) and Markdown (for quick human review).

Usage:
    pip install pdfplumber pypdfium2 pillow
    python pdf_to_structured.py input.pdf out_dir

Optional (still free, runs locally): convert equation images to LaTeX with
pix2tex ->  pip install "pix2tex[gui]"  then run with --latex
"""

import json
import re
import sys
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium

DPI = 200                 # resolution of cropped equation images
MIN_IMG_SIZE = 4          # ignore tiny decorative images (points)
PAD = 0                   # padding around each crop (points)

RE_EXERCISE = re.compile(r"^(EXERCISE\s+[\d.]+|MISCELLANEOUS EXERCISE)\b", re.I)
# "Question 5:", "Question 4.1:", "Ques. 3", "Q.7" ; the full label (e.g. "4.1") is kept
RE_QUESTION = re.compile(r"^(?:Question|Ques\.?|Q\.)\s*(\d+(?:\.\d+)*)\s*[:.)]?", re.I)
# "Solution:", "Answer 14:", "Answer" alone, "Ans.", "Sol." -- but not prose like "Answer the following"
RE_SOLUTION = re.compile(r"^(?:(Solution|Answer|Sol|Ans)\s*(?:\d+(?:\.\d+)*)?\s*(?::|-|$)|(Sol|Ans)\.)", re.I)
RE_CHAPTER = re.compile(r"^Chapter\s+\d+.*", re.I)
RE_LEADING_IMAGES = re.compile(r"^(?:\[\[eq:[^\]]+\]\]\s*)+")
RE_PAGE_NUMBER = re.compile(r"(?:page\s*)?[-–(]?\s*\d{1,4}\s*[-–)]?(?:\s*(?:of|/)\s*\d{1,4})?", re.I)


# --------------------------------------------------------------------------
# Step 1-3: page -> ordered lines with equation placeholders
# --------------------------------------------------------------------------
def page_to_lines(plumber_page, pdfium_page, page_no, img_dir, crop=True, boxes=None):
    """crop=False re-reads positions only (images already saved); boxes collects name -> image position."""
    scale = DPI / 72
    rendered = None  # render lazily, only if the page has images

    tokens = []  # (top, bottom, x0, kind, value, x1)

    words = plumber_page.extract_words(keep_blank_chars=False, use_text_flow=False)
    if _is_scanned(plumber_page, words):
        tokens = _ocr_tokens(pdfium_page, page_no, img_dir, boxes)
        return _tokens_to_lines(tokens, plumber_page, page_no)

    for w in words:
        tokens.append((w["top"], w["bottom"], w["x0"], "text", w["text"], w["x1"]))

    for i, im in enumerate(plumber_page.images):
        x0, top, x1, bottom = im["x0"], im["top"], im["x1"], im["bottom"]
        if (x1 - x0) < MIN_IMG_SIZE or (bottom - top) < MIN_IMG_SIZE:
            continue
        name = f"p{page_no:03d}_eq{i:03d}.png"
        if crop:
            if rendered is None:
                rendered = pdfium_page.render(scale=scale).to_pil()
            box = (
                max(0, int((x0 - PAD) * scale)),
                max(0, int((top - PAD) * scale)),
                min(rendered.width, int((x1 + PAD) * scale)),
                min(rendered.height, int((bottom + PAD) * scale)),
            )
            rendered.crop(box).save(img_dir / name)
        if boxes is not None:
            boxes[name] = {"page": page_no, "bbox": [round(v, 1) for v in (x0, top, x1, bottom)]}
        tokens.append((top, bottom, x0, "eq", name, x1))
    return _tokens_to_lines(tokens, plumber_page, page_no)


def _tokens_to_lines(tokens, plumber_page, page_no):
    """Words become rows by vertical overlap. Images join the row they sit on, but a tall image (a figure,
    often in a column beside the text) gets its own row - otherwise it would swallow every line next to it."""
    text_toks = sorted((t for t in tokens if t[3] == "text"), key=lambda t: (t[0], t[2]))
    img_toks = sorted((t for t in tokens if t[3] != "text"), key=lambda t: (t[0], t[2]))
    rows = []
    for tok in text_toks:
        top, bottom = tok[0], tok[1]
        mid = (top + bottom) / 2
        placed = False
        for row in rows:
            if row["top"] - 2 <= mid <= row["bottom"] + 2:
                row["items"].append(tok)
                row["top"] = min(row["top"], top)
                row["bottom"] = max(row["bottom"], bottom)
                placed = True
                break
        if not placed:
            rows.append({"top": top, "bottom": bottom, "items": [tok]})

    heights = sorted(r["bottom"] - r["top"] for r in rows)
    line_h = heights[len(heights) // 2] if heights else 12
    for tok in img_toks:
        top, bottom = tok[0], tok[1]
        mid = (top + bottom) / 2
        row = None
        if bottom - top <= 1.6 * line_h:  # inline formula: goes on the line it sits on
            row = next((r for r in rows if r["top"] - 2 <= mid <= r["bottom"] + 2), None)
        if row is None:
            rows.append({"top": top, "bottom": bottom, "items": [tok]})
        else:
            row["items"].append(tok)
            row["top"], row["bottom"] = min(row["top"], top), max(row["bottom"], bottom)

    rows.sort(key=lambda r: r["top"])
    lines = []
    h = plumber_page.height
    for row in rows:
        parts = []
        items = sorted(row["items"], key=lambda t: t[2])
        for _, _, _, kind, val, _ in items:
            parts.append(val if kind == "text" else f"[[eq:{val}]]")
        line = " ".join(parts).strip()
        in_margin = row["bottom"] > h * 0.92 or row["top"] < h * 0.05
        if in_margin and RE_PAGE_NUMBER.fullmatch(line):  # printed page numbers, not content
            continue
        if line:
            lines.append({"page": page_no, "text": line, "top": row["top"], "bottom": row["bottom"],
                          "x0": min(t[2] for t in items), "x1": max(t[5] for t in items)})
    return lines


# --------------------------------------------------------------------------
# Scanned pages: OCR the words, cut everything else with ink out as images
# --------------------------------------------------------------------------
OCR_MIN_CONF = 0.80        # below this an OCR line is treated as "unreadable ink" and cropped instead
OCR_MIN_LETTERS = 0.5      # lines that are mostly digits/symbols (maths) are cropped, not trusted as text
_ocr_engine = None
RE_OCR_PAGE_HEADER = re.compile(r"p\s*a\s*g\s*e\s*[|lI1!]*\s*\d*", re.I)


def _is_scanned(plumber_page, words):
    """Almost no real text, but the page is full: either a page-sized image (a scan) or text that was
    converted to drawn outlines (hundreds of curves). Both need OCR."""
    if len(words) >= 25:
        return False
    area = plumber_page.width * plumber_page.height
    # one page-sized scan, or several image strips that together cover most of the page (some books
    # export a scanned page as two or three slices), or text drawn as outlines
    covered = sum((im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) for im in plumber_page.images) / area
    if covered > 0.45:
        return True
    return len(plumber_page.curves) + len(plumber_page.rects) >= 200


RE_OCR_HEADING = re.compile(
    r"^(Q\s*\.\s*\d+(?:\.\d+)*\s*[.:)]?|Sol\s*[.:]|Ans\s*[.:]|Answer\s*\d*(?:\.\d+)?\s*:?|Question\s*\d+(?:\.\d+)*\s*:?"
    r"|Solution\s*:?|EXERCISE\s*[\d.]+|MISCELLANEOUS EXERCISE)", re.I)


def _ocr_fix(text):
    import unicodedata
    text = unicodedata.normalize("NFKC", text).strip()   # full-width "Ｐａｇｅ" -> "Page"
    text = re.sub(r"^Q\s*\.\s*[lI|](?=[.\s:)])", "Q.1", text)            # "Q.l." -> "Q.1."
    text = re.sub(r"^(Q\s*\.\s*\d*)[lI|](?=[.\s:)])", r"\g<1>1", text)   # "Q.1l." -> "Q.11."
    return text


def _ocr_tokens(pdfium_page, page_no, img_dir, boxes):
    """Tokens for a scanned page. Cached per page so re-splitting does not re-run OCR."""
    import numpy as np
    from scipy import ndimage

    cache = Path(img_dir) / f"ocr_p{page_no:03d}.json"
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        global _ocr_engine
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        scale = DPI / 72
        img = pdfium_page.render(scale=scale).to_pil().convert("RGB")
        arr = np.asarray(img)
        result, _ = _ocr_engine(arr)
        H, W = arr.shape[:2]
        ink = arr.mean(axis=2) < 170
        text = []
        for box, t, conf in result or []:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            x0, y0, x1, y1 = max(0, min(xs)), max(0, min(ys)), min(W, max(xs)), min(H, max(ys))
            t = _ocr_fix(t)
            letters = sum(c.isalpha() for c in t) / max(1, len(t.replace(" ", "")))
            if RE_OCR_PAGE_HEADER.fullmatch(t) and y1 < H * 0.08:
                ink[int(y0):int(y1) + 1, int(x0):int(x1) + 1] = False   # "Page | 1" header: drop entirely
                continue
            head = RE_OCR_HEADING.match(t)
            if float(conf) >= OCR_MIN_CONF and letters >= OCR_MIN_LETTERS and len(t) >= 2:
                text.append([y0 / scale, y1 / scale, x0 / scale, x1 / scale, t])
                ink[max(0, int(y0) - 3):int(y1) + 4, max(0, int(x0) - 3):int(x1) + 4] = False
            elif head and float(conf) >= 0.6:
                # "Sol. (i) 0.36 ..." : keep the heading word as text so the splitter sees it,
                # leave the maths after it as ink to be cropped
                hx1 = x0 + (x1 - x0) * head.end() / max(1, len(t))
                text.append([y0 / scale, y1 / scale, x0 / scale, hx1 / scale, head.group(0).strip()])
                ink[max(0, int(y0) - 3):int(y1) + 4, max(0, int(x0) - 3):int(hx1) + 3] = False
        # leftover ink = maths, low-confidence text, diagrams: group nearby strokes into regions
        grown = ndimage.binary_dilation(ink, structure=np.ones((11, 35), bool))
        labels, _ = ndimage.label(grown)
        regions = []
        for k, sl in enumerate(ndimage.find_objects(labels)):
            if sl is None:
                continue
            ys, xs = sl
            h, w = ys.stop - ys.start, xs.stop - xs.start
            if w > 0.85 * W or h > 0.85 * H:           # page frames and borders
                continue
            if ys.stop < 0.18 * H and w > 0.35 * W:     # running chapter banner at the top of the page
                continue
            if ink[sl].sum() < 40 or (w < 14 and h < 14):  # specks
                continue
            name = f"p{page_no:03d}_ocr{len(regions):03d}.png"
            y0, y1 = max(0, ys.start - 2), min(H, ys.stop + 2)
            x0, x1 = max(0, xs.start - 2), min(W, xs.stop + 2)
            img.crop((x0, y0, x1, y1)).save(Path(img_dir) / name)
            regions.append([name, x0 / scale, y0 / scale, x1 / scale, y1 / scale])
        data = {"text": text, "regions": regions}
        cache.write_text(json.dumps(data), encoding="utf-8")

    tokens = [(top, bottom, x0, "text", t, x1) for top, bottom, x0, x1, t in data["text"]]
    for name, x0, top, x1, bottom in data["regions"]:
        if boxes is not None:
            boxes[name] = {"page": page_no, "bbox": [round(v, 1) for v in (x0, top, x1, bottom)]}
        tokens.append((top, bottom, x0, "eq", name, x1))
    return tokens


# --------------------------------------------------------------------------
# Step 4: lines -> Exercise / Question / Solution structure
# --------------------------------------------------------------------------
def structure(lines):
    doc = {"chapter": None, "exercises": []}
    exercise = None
    question = None
    section = None  # "question" or "solution"

    for ln in lines:
        full = ln["text"]
        # a bullet or decoration image can sit before the heading word: look past it, but keep it in the text
        lead = RE_LEADING_IMAGES.match(full)
        text = full[lead.end():] if lead else full
        lead_text = full[:lead.end()].strip() if lead else ""

        if doc["chapter"] is None and RE_CHAPTER.match(text):
            doc["chapter"] = text
            continue

        m = RE_EXERCISE.match(text)
        if m:
            exercise = {"title": m.group(1).upper(), "questions": []}
            doc["exercises"].append(exercise)
            question, section = None, None
            continue

        m = RE_QUESTION.match(text)
        if m:
            if exercise is None:
                exercise = {"title": "UNKNOWN", "questions": []}
                doc["exercises"].append(exercise)
            label = m.group(1)
            question = {
                "number": int(label.split(".")[-1]),
                "label": label,
                "page_start": ln["page"],
                "question": [],
                "solution": [],
                "solution_heading": None,
                "_lines": [],
            }
            exercise["questions"].append(question)
            section = "question"
            question["_lines"].append(ln)
            rest = " ".join(x for x in (lead_text, text[m.end():].strip()) if x)
            if rest:
                question["question"].append(rest)
            continue

        ms = RE_SOLUTION.match(text)
        if ms and question is not None and section == "question":
            section = "solution"
            word = (ms.group(1) or ms.group(2)).lower()
            question["solution_heading"] = "answer" if word.startswith("ans") else "solution"
            question["_lines"].append(ln)
            rest = " ".join(x for x in (lead_text, text[ms.end():].strip()) if x)
            if rest:
                question["solution"].append(rest)
            continue

        if question is not None and section:
            question[section].append(full)
            question["_lines"].append(ln)

    # Final shape: joined text + list of equation files per section + where it sits on the page(s)
    for ex in doc["exercises"]:
        for q in ex["questions"]:
            for key in ("question", "solution"):
                joined = "\n".join(q[key])
                q[key] = {
                    "text": joined,
                    "equations": re.findall(r"\[\[eq:([^\]]+)\]\]", joined),
                }
            lines = q.pop("_lines")
            q["regions"] = _regions(lines)
            # one box per line as well: with figures in a side column the page-wide box looks far too tall
            q["lineRegions"] = [{"page": ln["page"], "bbox": [round(ln["x0"], 1), round(ln["top"], 1),
                                                              round(ln["x1"], 1), round(ln["bottom"], 1)]}
                                for ln in lines if "top" in ln]
            q["id"] = f"{ex['title'].replace(' ', '_')}_Q{q['label']}"
    return doc


def _regions(lines):
    """One bounding box (PDF points, top-left origin) per page the question spans."""
    by_page = {}
    for ln in lines:
        if "top" not in ln:
            continue
        b = by_page.setdefault(ln["page"], [ln["x0"], ln["top"], ln["x1"], ln["bottom"]])
        b[0], b[1] = min(b[0], ln["x0"]), min(b[1], ln["top"])
        b[2], b[3] = max(b[2], ln["x1"]), max(b[3], ln["bottom"])
    return [{"page": p, "bbox": [round(v, 1) for v in b]} for p, b in sorted(by_page.items())]


# --------------------------------------------------------------------------
# Optional: local LaTeX OCR (free, no API)
# --------------------------------------------------------------------------
RE_FONT_WRAP = re.compile(r"(\\[A-Za-z]+)?\\(?:mathrm|mathbf|mathit|boldsymbol|textbf|textrm)\s*\{([A-Za-z0-9 ]{1,12})\}")


def _unwrap(m):
    # keep a space after a preceding command so "\in\mathbf{R}" becomes "\in R", not "\inR"
    return (m.group(1) + " " if m.group(1) else "") + m.group(2).replace(" ", "")


def clean_latex(s):
    # pix2tex tends to wrap plain letters in font commands that the source doesn't have
    prev = None
    while prev != s:
        prev = s
        s = RE_FONT_WRAP.sub(_unwrap, s)
    s = re.sub(r"\\(?:textstyle|displaystyle)\s*", "", s)
    # the binary operation "*" (Ex 1.4) is read as a superscript star or bullet
    s = re.sub(r"\^\{\s*(?:\*|\\ast|\\bullet)+\s*\}", "*", s)
    s = re.sub(r"^\s*\\cdot\s*\\cdot\s*", r"\\therefore ", s)
    s = re.sub(r"\{\\cal\s+([A-Za-z])\}", r"{\1}", s)
    s = re.sub(r"\\big[lr]?\s*([()\[\]])", r"\1", s)
    s = re.sub(r"\\operatorname\{([a-z]+)\}", r"\\text{ \1 }", s)
    # prose inside formula images: \mathrm{~is~not~transitive} -> \text{ is not transitive }
    s = re.sub(r"\\mathrm\{([^{}]*~[^{}]*)\}", lambda m: r"\text{" + m.group(1).replace("~", " ") + "}", s)
    s = re.sub(r"\\[,;!]\s*(\\right)", r"\1", s)
    s = re.sub(r"\\(in|notin|to|neq|leq|geq|times|subset|cup|cap)(?=[A-Z])", r"\\\1 ", s)
    s = re.sub(r"\\(?:,|;|!|quad|qquad)\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cuda_works():
    import torch
    if not torch.cuda.is_available():
        return False
    try:
        # is_available() can be True while kernels still fail (e.g. driver too old for the build)
        return torch.ones(4, device="cuda").sum().item() == 4
    except Exception:
        return False


def load_latex_model():
    import pix2tex.cli
    from munch import Munch
    from pix2tex.cli import LatexOCR

    # pix2tex copies every prediction to the system clipboard (meant for its screenshot tool),
    # which floods Windows clipboard history during a batch run
    pix2tex.cli.clipboard.copy = lambda *_a, **_k: None
    use_gpu = _cuda_works()
    print(f"LaTeX model on {'GPU' if use_gpu else 'CPU'}")
    # pix2tex defaults to no_cuda=True, so the GPU is only used if we ask for it
    return LatexOCR(Munch({"config": "settings/config.yaml", "checkpoint": "checkpoints/weights.pth",
                           "no_cuda": not use_gpu, "no_resize": False}))


def _print_progress(stage, done, total):
    print(f"{stage} {done}/{total}", end="\r")


SMALL_IMG_PX = 100  # narrower crops are usually one symbol: use the glyph matcher first


def recognise_all(paths, model, templates, progress=_print_progress):
    """paths -> {path: {"latex", "source", "confidence", "needs_review", ["raw"]}}"""
    import hashlib

    from PIL import Image, ImageOps
    from glyph_match import ACCEPT, classify
    from latex_batch import predict_batch

    by_hash, owner = {}, {}  # identical crops (repeated formulas) are recognised once
    pending = []             # (hash, image, is_small, is_tall) still needing pix2tex
    for i, p in enumerate(paths):
        progress("symbols", i + 1, len(paths))
        h = hashlib.md5(p.read_bytes()).hexdigest()
        owner[p] = h
        if h in by_hash:
            continue
        im = Image.open(p).convert("RGB")
        why = figure_reason(im)
        if why:  # diagrams, graphs, photos stay images at their exact position
            by_hash[h] = figure_entry(why)
            continue
        if "_ocr" in p.name:
            # crops from scanned pages: pix2tex misreads these (tested: ~4 of 12 right), so they go to the AI
            by_hash[h] = {"latex": None, "source": "scan", "confidence": None, "needs_review": True,
                          "reason": "maths from a scanned page: needs the AI step"}
            continue
        small = im.width < SMALL_IMG_PX
        if small:
            tex, score = classify(im, templates)
            if score >= ACCEPT:
                by_hash[h] = {"latex": tex, "source": "glyph", "confidence": round(score, 2), "needs_review": False}
                continue
        by_hash[h] = None  # reserved; filled after the batched pass
        pending.append((h, ImageOps.expand(im, border=12, fill="white"), small, im.height > TALL_BLOCK_PX))

    if pending and model is None:  # scanned-only chapters never need the pix2tex model
        model = load_latex_model()
    raws = predict_batch(model, [im for _, im, _, _ in pending], progress=progress) if pending else []
    for (h, _, small, tall), raw in zip(pending, raws):
        tex = clean_latex(raw) if raw else None
        r = {"latex": tex, "raw": raw, "source": "pix2tex", "confidence": None,
             "needs_review": small or tall or not tex}
        if tall and tex:
            r["reason"] = "multi-line block: pix2tex is unreliable on these"
        by_hash[h] = r
    return {p: by_hash[owner[p]] for p in paths}


FIGURE_MIN_W, FIGURE_MIN_H = 150, 100  # coloured crops smaller than this are spell-check squiggles, not figures
TALL_BLOCK_PX = 250                    # pix2tex output for taller crops (5+ stacked lines) goes to review


def figure_reason(im):
    """Why an image looks like a figure (diagram, graph, photo, table) rather than a formula; None if formula.
    Calibrated on NCERT maths, a biology and a chemistry paper: equations are black-on-white with no long
    vertical lines; figures are coloured, or have long horizontal *and* vertical lines (axes, grids, tables)."""
    import numpy as np

    if im.width < FIGURE_MIN_W or im.height < FIGURE_MIN_H:
        return None
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    colour = float(((a.max(2) - a.min(2)) > 40).mean())
    if colour > 0.005:
        return f"coloured image ({colour:.1%} coloured pixels)"
    ink = a.mean(2) < 150

    def has_line(mat, need):
        for row in mat:
            d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
            starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
            if len(starts) and (ends - starts).max() >= need:
                return True
        return False

    if has_line(ink, 0.5 * ink.shape[1]) and has_line(ink.T, 0.5 * ink.shape[0]):
        return "long horizontal and vertical lines (graph, table or diagram)"
    return None


def figure_entry(reason, by="code"):
    return {"latex": None, "source": "figure", "kind": "figure", "confidence": None,
            "needs_review": False, "reason": f"{reason} [{by}]"}


def classify_figures(doc, img_dir):
    """Re-check an existing result: turn anything that looks like a figure back into an image. Returns count."""
    from PIL import Image

    n = 0
    for sec in _sections(doc):
        for name, r in sec.get("equations_latex", {}).items():
            if r.get("source") in ("figure", "skipped"):
                continue
            why = figure_reason(Image.open(img_dir / name).convert("RGB"))
            if why:
                r.clear()
                r.update(figure_entry(why))
                n += 1
    return n


def _sections(doc):
    for ex in doc["exercises"]:
        for q in ex["questions"]:
            for key in ("question", "solution"):
                yield q[key]


def image_sections(doc, name):
    """Every question or solution section that still refers to this image."""
    return [sec for sec in _sections(doc) if name in sec.get("equations", [])]


def set_image_result(doc, name, result):
    """Apply a reviewed reading to every occurrence of the same PDF image."""
    sections = image_sections(doc, name)
    for sec in sections:
        sec.setdefault("equations_latex", {})[name] = dict(result)
    if sections:
        build_text_latex(doc)
    return len(sections)


def remove_image(doc, name):
    """Remove an image from extracted text and export, while keeping its PNG on disk."""
    sections = image_sections(doc, name)
    token = f"[[eq:{name}]]"
    for sec in sections:
        sec["equations"] = [n for n in sec["equations"] if n != name]
        sec.get("equations_latex", {}).pop(name, None)
        sec["text"] = re.sub(r"[ \t]{2,}", " ", sec["text"].replace(token, "")).strip()
    if sections:
        deleted = doc.setdefault("deleted_images", [])
        if name not in deleted:
            deleted.append(name)
        build_text_latex(doc)
    return len(sections)


def validate_latex(doc):
    """Mark formulas whose LaTeX KaTeX cannot render as needing review. Returns how many failed."""
    from ai_fallback import katex_errors

    entries = [r for sec in _sections(doc) for r in sec.get("equations_latex", {}).values()
               if r["latex"] and not r["needs_review"]]
    failed = 0
    for r, err in zip(entries, katex_errors([r["latex"] for r in entries])):
        if err:
            r.update(needs_review=True, katex_error=err)
            failed += 1
    return failed


def build_text_latex(doc):
    for sec in _sections(doc):
        latex_text = sec["text"]
        for name, r in sec.get("equations_latex", {}).items():
            # uncertain results keep the image placeholder rather than inserting wrong maths
            if r["latex"] is not None and not r["needs_review"]:
                latex_text = latex_text.replace(f"[[eq:{name}]]", rf"\({r['latex']}\)" if r["latex"] else "")
        sec["text_latex"] = re.sub(r"[ \t]{2,}", " ", latex_text)


def ai_fix(doc, img_dir, progress=_print_progress):
    """Send every formula still needing review to the OpenAI model. The model first decides whether the
    image is a formula or a figure; figures stay images. Returns (resolved, still_failing)."""
    import ai_fallback

    todo = {}  # name -> context sentence
    for sec in _sections(doc):
        for line in sec["text"].split("\n"):
            for name in re.findall(r"\[\[eq:([^\]]+)\]\]", line):
                r = sec.get("equations_latex", {}).get(name)
                if r and r["needs_review"] and name not in todo:
                    todo[name] = re.sub(r"\[\[eq:[^\]]+\]\]", "[formula]", line).strip()
    if not todo:
        return 0, 0
    answers = ai_fallback.transcribe([(img_dir / n, ctx) for n, ctx in todo.items()], progress)
    for sec in _sections(doc):
        for name, r in sec.get("equations_latex", {}).items():
            a = answers.get(img_dir / name)
            if a is None:
                continue
            if a.get("kind") == "figure":
                r.clear()
                r.update(figure_entry(f"AI ({ai_fallback.model_name()}) identified a figure", by="ai"))
            elif a["latex"]:
                r.update(latex=a["latex"], source="ai", model=ai_fallback.model_name(),
                         needs_review=False, ai_error=None)
                r.pop("katex_error", None)
                r.pop("reason", None)
            else:
                r["ai_error"] = a["error"]
    resolved = sum(1 for n in todo if answers[img_dir / n]["latex"] or answers[img_dir / n].get("kind") == "figure")
    return resolved, len(todo) - resolved


def add_latex(doc, img_dir, model=None, progress=_print_progress, use_ai=False):
    from glyph_match import build_templates

    names = list(dict.fromkeys(n for sec in _sections(doc) for n in sec["equations"]))
    found = recognise_all([img_dir / n for n in names], model, build_templates(), progress)
    doc["figures_checked"] = True
    for sec in _sections(doc):
        # copies, so fixing one occurrence later doesn't silently change a shared dict
        sec["equations_latex"] = {n: dict(found[img_dir / n]) for n in sec["equations"]}
    bad_katex = validate_latex(doc)
    count = lambda s: sum(r["source"] == s for r in found.values())
    n_review = sum(r["needs_review"] for r in found.values()) + bad_katex
    print(f"\nLaTeX: {len(names)} images | figures: {count('figure')} | glyph: {count('glyph')} "
          f"| pix2tex: {count('pix2tex')} | KaTeX failures: {bad_katex} | needs review: {n_review}")
    if use_ai:
        import ai_fallback
        fixed, left = ai_fix(doc, img_dir, progress)
        print(f"\nAI fallback ({ai_fallback.model_name()}): resolved {fixed}, still to check {left}")
    build_text_latex(doc)


# --------------------------------------------------------------------------
# Step 5: outputs
# --------------------------------------------------------------------------
def to_markdown(doc):
    out = [f"# {doc['chapter'] or 'Document'}\n"]
    for ex in doc["exercises"]:
        out.append(f"\n## {ex['title']}\n")
        for q in ex["questions"]:
            out.append(f"\n### Question {q['number']}  (page {q['page_start']})\n")
            for key, label in (("question", "**Question**"), ("solution", "**Solution**")):
                src = q[key].get("text_latex", q[key]["text"])
                body = re.sub(r"\[\[eq:([^\]]+)\]\]", r"![](images/\1)", src)
                out.append(f"{label}\n\n{body.replace(chr(10), '  ' + chr(10))}\n")
    return "\n".join(out)


def run(pdf_path, out_dir, want_latex=False, progress=_print_progress, model=None, use_ai=False):
    pdf_path, out_dir = Path(pdf_path), Path(out_dir)
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    doc = _read_and_structure(pdf_path, img_dir, crop=True, progress=progress)
    if want_latex:
        add_latex(doc, img_dir, model=model, progress=progress, use_ai=use_ai)
    save(doc, out_dir)
    return doc


STRUCTURE_VERSION = 7  # 6: headings behind a bullet image; 7: pages split into image strips count as scanned
#                        4: tall figures get their own line instead of swallowing the text beside them


def _read_and_structure(pdf_path, img_dir, crop, progress=_print_progress):
    all_lines, boxes, pages = [], {}, {}
    pdfium_doc = pdfium.PdfDocument(str(pdf_path))
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages):
                pages[idx + 1] = [round(page.width, 1), round(page.height, 1)]
                all_lines += page_to_lines(page, pdfium_doc[idx], idx + 1, img_dir, crop=crop, boxes=boxes)
                progress("pages", idx + 1, len(pdf.pages))
    finally:
        pdfium_doc.close()
    doc = structure(all_lines)
    doc.update(image_boxes=boxes, page_sizes=pages, structure_version=STRUCTURE_VERSION)
    return doc


def restructure(pdf_path, out_dir):
    """Re-split an existing result with the current rules without redoing crops or LaTeX:
    formula results are carried over by image name (names depend only on page and image order)."""
    out_dir = Path(out_dir)
    old = json.loads((out_dir / "structured.json").read_text(encoding="utf-8"))
    known = {}
    for sec in _sections(old):
        known.update(sec.get("equations_latex", {}))
    doc = _read_and_structure(pdf_path, out_dir / "images", crop=False, progress=lambda *a: None)
    if known:
        for sec in _sections(doc):
            sec["equations_latex"] = {n: dict(known[n]) for n in sec["equations"] if n in known}
        doc["figures_checked"] = old.get("figures_checked", False)
        build_text_latex(doc)
    for name in old.get("deleted_images", []):
        remove_image(doc, name)
    save(doc, out_dir)
    return doc


def save(doc, out_dir):
    out_dir = Path(out_dir)
    (out_dir / "structured.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "preview.md").write_text(to_markdown(doc), encoding="utf-8")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    out_dir = Path(sys.argv[2])
    use_ai = "--ai" in sys.argv
    doc = run(sys.argv[1], out_dir, want_latex="--latex" in sys.argv or use_ai, use_ai=use_ai)
    print()
    img_dir = out_dir / "images"

    n_q = sum(len(e["questions"]) for e in doc["exercises"])
    n_eq = len(list(img_dir.glob("*.png")))
    print(f"Exercises: {len(doc['exercises'])} | Questions: {n_q} | Equation images: {n_eq}")
    for e in doc["exercises"]:
        print(f"  {e['title']}: questions {[q['number'] for q in e['questions']]}")


if __name__ == "__main__":
    main()
