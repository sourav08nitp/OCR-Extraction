"""Locate vector-drawn chemical structures on PDF pages and crop them as PNGs.

Word/ChemDraw structures are stored as line and curve drawing commands plus text labels, so text
extraction loses them. Here, drawing objects close to each other are merged into clusters, text
labels lying inside a cluster are added to its box, and the box is rendered from the page.
"""

import json
import re
import sys
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium

DPI = 250
GAP = 9           # drawing pieces closer than this (points) belong to the same structure
PAD = 4
MIN_SIZE = 18     # ignore tiny clusters (bullets, underlines) smaller than this in both directions
HEADER, FOOTER = 72, 735   # page furniture on these Resonance pages (points from top)
OPTION_LABEL = re.compile(r"\(\d\)|\([A-Da-d]\)|\((?:I|II|III|IV|V|VI|VII|VIII|IX|X)\)|I|II|III|IV|V|VI|VII|VIII|IX|X|\d+\.")


def _boxes(page):
    out = []
    for o in page.curves + page.lines:
        x0, top, x1, bottom = o["x0"], o["top"], o["x1"], o["bottom"]
        if top < HEADER or bottom > FOOTER:
            continue
        if x1 - x0 > page.width * 0.6:  # full-width rules under headings
            continue
        out.append([x0, top, x1, bottom])
    return out


def _near(a, b, gap):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _cluster(boxes, gap):
    clusters = []
    for b in boxes:
        hit = [c for c in clusters if _near(c, b, gap)]
        merged = list(b)
        for c in hit:
            merged = [min(merged[0], c[0]), min(merged[1], c[1]), max(merged[2], c[2]), max(merged[3], c[3])]
            clusters.remove(c)
        clusters.append(merged)
    changed = True
    while changed:  # merging can make earlier clusters touch
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if _near(clusters[i], clusters[j], gap):
                    a, b = clusters[i], clusters.pop(j)
                    clusters[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    changed = True
                    break
            if changed:
                break
    return clusters


def _with_labels(box, words, gap=6):
    x0, top, x1, bottom = box
    grew = True
    while grew:  # atom labels (CH3, O, OH) touching the drawing belong to it
        grew = False
        for w in words:
            if _near([x0, top, x1, bottom], [w["x0"], w["top"], w["x1"], w["bottom"]], gap) and not (
                    x0 <= w["x0"] and w["x1"] <= x1 and top <= w["top"] and w["bottom"] <= bottom):
                if w["x1"] - w["x0"] > 60:  # a long word is prose, not a label
                    continue
                if OPTION_LABEL.fullmatch(w["text"]):  # "(1)", "(B)", "IV" belong to the question, not the molecule
                    continue
                x0, top, x1, bottom = min(x0, w["x0"]), min(top, w["top"]), max(x1, w["x1"]), max(bottom, w["bottom"])
                grew = True
    return [x0, top, x1, bottom]


def find(pdf_path, out_dir, pages=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    found = []
    doc = pdfium.PdfDocument(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            if pages and i + 1 not in pages:
                continue
            words = [w for w in page.extract_words() if HEADER <= w["top"] and w["bottom"] <= FOOTER]
            clusters = [c for c in _cluster(_boxes(page), GAP)
                        if (c[2] - c[0]) >= MIN_SIZE or (c[3] - c[1]) >= MIN_SIZE]
            if not clusters:
                continue
            img = doc[i].render(scale=DPI / 72).to_pil()
            s = DPI / 72
            for k, c in enumerate(sorted((_with_labels(c, words) for c in clusters), key=lambda b: (b[1], b[0]))):
                name = f"p{i + 1:02d}_s{k:02d}.png"
                box = (int((c[0] - PAD) * s), int((c[1] - PAD) * s), int((c[2] + PAD) * s), int((c[3] + PAD) * s))
                img.crop(box).save(out_dir / name)
                found.append({"file": name, "page": i + 1, "bbox_pt": [round(v, 1) for v in c]})
    doc.close()
    (out_dir / "structures.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    return found


if __name__ == "__main__":
    pages = {int(p) for p in sys.argv[3].split(",")} if len(sys.argv) > 3 else None
    res = find(sys.argv[1], sys.argv[2], pages)
    print(f"{len(res)} structure regions")
