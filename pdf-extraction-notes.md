# Extracting a solutions PDF into structured data (no AI, no API cost)

Notes from working on `selfstudys.pdf` (NCERT Class 12 Maths, Chapter 1 Relations and Functions, 65 pages).

## The problem with plain text extraction

`pdftotext` / `pypdf` on this file returns broken sentences like:

```
R is not reflexive because .
R is not symmetric because .
```

Reason: **every math formula in the PDF is an embedded raster image, not text.**
The file contains ~1,946 such images. The text layer only holds the prose,
so any naive extraction silently drops all the maths.

Diagnostics that showed this:

```bash
pdfinfo selfstudys.pdf        # 65 pages, A4, produced by Aspose
pdffonts selfstudys.pdf       # fonts exist -> real text layer present
pdfimages -list selfstudys.pdf | wc -l   # ~3894 lines (images + smasks)
```

## The approach

1. Read each page's **words with coordinates** and each **image box with coordinates** (pdfplumber).
2. Group words and images into lines by vertical overlap, sort left-to-right.
3. Emit each line as text with a placeholder `[[eq:pNNN_eqNNN.png]]` wherever a formula sits.
4. Crop every formula out of a rendered page (pypdfium2 + Pillow) and save it as PNG.
5. Split the line stream into `Exercise -> Question -> {question, solution}` using
   the `EXERCISE`, `Question N:`, `Solution:` headings.
6. Write `structured.json` (for an app or DB) and `preview.md` (for eyeballing).

No model is involved at any step, so there is no token or pricing impact.

## Results on this file

* Runtime: about a minute.
* 5 exercises, 74 questions, all question numbers matching the PDF:
  * EXERCISE 1.1: 1-16
  * EXERCISE 1.2: 1-12
  * EXERCISE 1.3: 1-14
  * EXERCISE 1.4: 1-13
  * MISCELLANEOUS EXERCISE: 1-19
* 1,946 equation PNGs, ~20 MB total.

Sample record from `structured.json`:

```json
{
  "id": "EXERCISE_1.1_Q2",
  "number": 2,
  "page_start": 4,
  "question": {
    "text": "Show that the relation R in the set R of real numbers, defined as [[eq:p004_eq000.png]] is neither\nreflexive nor symmetric nor transitive.",
    "equations": ["p004_eq000.png"]
  },
  "solution": {
    "text": "[[eq:p004_eq001.png]]\n[[eq:p004_eq002.png]] because [[eq:p004_eq003.png]]\n...",
    "equations": ["p004_eq001.png", "p004_eq002.png", "..."]
  }
}
```

## How to run

```bash
pip install pdfplumber pypdfium2 pillow
python pdf_to_structured.py input.pdf out_dir
```

Output in `out_dir/`: `structured.json`, `preview.md`, `images/`.

## Getting formulas as text instead of images (optional, still free)

```bash
pip install "pix2tex[gui]"
python pdf_to_structured.py input.pdf out_dir --latex
```

pix2tex is a local LaTeX-OCR model: it runs on your own machine, so there are
still no API calls. It adds a `text_latex` field where each `[[eq:...]]`
placeholder is replaced by `$...$` LaTeX. It is slower and occasionally
misreads complicated fractions, so spot-check the output.

## Known limits

* Splitting depends on the heading wording (`Question N:`, `Solution:`). Other
  publishers need the regexes at the top of the script adjusted.
* Tables (e.g. the operation tables in Exercise 1.4) come out as rows of
  space-separated numbers, not as real table structures.
* Small symbols such as `∴` are sometimes saved as their own tiny images.
* `MIN_IMG_SIZE`, `DPI` and `PAD` at the top of the script control crop
  quality and which tiny images get skipped.

## The script

Saved as `pdf_to_structured.py`, reproduced in full below.

```python
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
PAD = 1.5                 # padding around each crop (points)

RE_EXERCISE = re.compile(r"^(EXERCISE\s+[\d.]+|MISCELLANEOUS EXERCISE)\b", re.I)
RE_QUESTION = re.compile(r"^Question\s+(\d+)\s*:?", re.I)
RE_SOLUTION = re.compile(r"^Solution\s*:?", re.I)
RE_CHAPTER = re.compile(r"^Chapter\s+\d+.*", re.I)


# --------------------------------------------------------------------------
# Step 1-3: page -> ordered lines with equation placeholders
# --------------------------------------------------------------------------
def page_to_lines(plumber_page, pdfium_page, page_no, img_dir):
    scale = DPI / 72
    rendered = None  # render lazily, only if the page has images

    tokens = []  # (top, bottom, x0, kind, value)

    for w in plumber_page.extract_words(keep_blank_chars=False, use_text_flow=False):
        tokens.append((w["top"], w["bottom"], w["x0"], "text", w["text"]))

    for i, im in enumerate(plumber_page.images):
        x0, top, x1, bottom = im["x0"], im["top"], im["x1"], im["bottom"]
        if (x1 - x0) < MIN_IMG_SIZE or (bottom - top) < MIN_IMG_SIZE:
            continue
        if rendered is None:
            rendered = pdfium_page.render(scale=scale).to_pil()
        box = (
            max(0, int((x0 - PAD) * scale)),
            max(0, int((top - PAD) * scale)),
            min(rendered.width, int((x1 + PAD) * scale)),
            min(rendered.height, int((bottom + PAD) * scale)),
        )
        name = f"p{page_no:03d}_eq{i:03d}.png"
        rendered.crop(box).save(img_dir / name)
        tokens.append((top, bottom, x0, "eq", name))

    # Group tokens into rows by vertical overlap
    tokens.sort(key=lambda t: (t[0], t[2]))
    rows = []
    for tok in tokens:
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

    rows.sort(key=lambda r: r["top"])
    lines = []
    for row in rows:
        parts = []
        for _, _, _, kind, val in sorted(row["items"], key=lambda t: t[2]):
            parts.append(val if kind == "text" else f"[[eq:{val}]]")
        line = " ".join(parts).strip()
        if line:
            lines.append({"page": page_no, "text": line})
    return lines


# --------------------------------------------------------------------------
# Step 4: lines -> Exercise / Question / Solution structure
# --------------------------------------------------------------------------
def structure(lines):
    doc = {"chapter": None, "exercises": []}
    exercise = None
    question = None
    section = None  # "question" or "solution"

    for ln in lines:
        text = ln["text"]

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
            question = {
                "number": int(m.group(1)),
                "page_start": ln["page"],
                "question": [],
                "solution": [],
            }
            exercise["questions"].append(question)
            section = "question"
            rest = text[m.end():].strip()
            if rest:
                question["question"].append(rest)
            continue

        if RE_SOLUTION.match(text) and question is not None:
            section = "solution"
            rest = RE_SOLUTION.sub("", text, count=1).strip()
            if rest:
                question["solution"].append(rest)
            continue

        if question is not None and section:
            question[section].append(text)

    # Final shape: joined text + list of equation files per section
    for ex in doc["exercises"]:
        for q in ex["questions"]:
            for key in ("question", "solution"):
                joined = "\n".join(q[key])
                q[key] = {
                    "text": joined,
                    "equations": re.findall(r"\[\[eq:([^\]]+)\]\]", joined),
                }
            q["id"] = f"{ex['title'].replace(' ', '_')}_Q{q['number']}"
    return doc


# --------------------------------------------------------------------------
# Optional: local LaTeX OCR (free, no API)
# --------------------------------------------------------------------------
def add_latex(doc, img_dir):
    from PIL import Image
    from pix2tex.cli import LatexOCR

    model = LatexOCR()
    cache = {}
    for ex in doc["exercises"]:
        for q in ex["questions"]:
            for key in ("question", "solution"):
                sec = q[key]
                latex_text = sec["text"]
                for name in sec["equations"]:
                    if name not in cache:
                        try:
                            cache[name] = model(Image.open(img_dir / name))
                        except Exception:
                            cache[name] = None
                    if cache[name]:
                        latex_text = latex_text.replace(f"[[eq:{name}]]", f"${cache[name]}$")
                sec["text_latex"] = latex_text


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
                body = re.sub(r"\[\[eq:([^\]]+)\]\]", r"![](images/\1)", q[key]["text"])
                out.append(f"{label}\n\n{body.replace(chr(10), '  ' + chr(10))}\n")
    return "\n".join(out)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    pdf_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    want_latex = "--latex" in sys.argv
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    all_lines = []
    pdfium_doc = pdfium.PdfDocument(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages):
            all_lines += page_to_lines(page, pdfium_doc[idx], idx + 1, img_dir)
            print(f"page {idx + 1}/{len(pdf.pages)}", end="\r")
    print()

    doc = structure(all_lines)
    if want_latex:
        add_latex(doc, img_dir)

    (out_dir / "structured.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "preview.md").write_text(to_markdown(doc), encoding="utf-8")

    n_q = sum(len(e["questions"]) for e in doc["exercises"])
    n_eq = len(list(img_dir.glob("*.png")))
    print(f"Exercises: {len(doc['exercises'])} | Questions: {n_q} | Equation images: {n_eq}")
    for e in doc["exercises"]:
        print(f"  {e['title']}: questions {[q['number'] for q in e['questions']]}")


if __name__ == "__main__":
    main()
```
