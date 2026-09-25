"""Recognise single-symbol equation images (x, f, 3, *, ∴, ⇒ ...) by template matching
against glyphs rendered from local Windows fonts. pix2tex is unreliable on these."""

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path("C:/Windows/Fonts")
SIZE = 32          # normalised glyph canvas (px)
INK = 140          # grayscale below this counts as ink (drops light spell-check squiggles)
ACCEPT = 0.7      # minimum match score; below this the caller should fall back to pix2tex

SYMBOLS = {
    "∴": r"\therefore", "∵": r"\because", "⇒": r"\Rightarrow", "⇔": r"\Leftrightarrow",
    "→": r"\to", "∈": r"\in", "∉": r"\notin", "∀": r"\forall", "∃": r"\exists",
    "≠": r"\neq", "≤": r"\leq", "≥": r"\geq", "×": r"\times", "∘": r"\circ",
    "∪": r"\cup", "∩": r"\cap", "⊂": r"\subset", "φ": r"\phi", "−": "-", "*": "*",
    "+": "+", "=": "=", "<": "<", ">": ">", "(": "(", ")": ")", "|": "|",
}


def _font(name, size=64):
    return ImageFont.truetype(str(FONT_DIR / name), size)


def ink_mask(im):
    a = np.asarray(im.convert("L"), dtype=np.uint8) < INK
    return a


def normalise(mask):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None, 0.0
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = m.shape
    aspect = w / h
    side = max(h, w)
    canvas = np.zeros((side, side), dtype=np.uint8)
    canvas[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = m * 255
    small = Image.fromarray(canvas).resize((SIZE, SIZE), Image.BOX).filter(ImageFilter.GaussianBlur(1.2))
    v = np.asarray(small, dtype=np.float32).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return (v / n if n else v), aspect


def _render(ch, font):
    img = Image.new("L", (140, 140), 255)
    ImageDraw.Draw(img).text((30, 20), ch, font=font, fill=0)
    return img


def build_templates():
    specs = []
    italic, upright, sym = _font("timesi.ttf"), _font("times.ttf"), _font("seguisym.ttf")
    cambria = ImageFont.truetype(str(FONT_DIR / "cambria.ttc"), 64)
    for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        specs.append((c, c, italic))
    # upright l/I/O/o dropped: they are indistinguishable from the digits 1 and 0
    for c in "0123456789abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ":
        specs.append((c, c, upright))
    for ch, tex in SYMBOLS.items():
        for f in (upright, sym, cambria):
            specs.append((ch, tex, f))
    templates = []
    for ch, tex, f in specs:
        vec, aspect = normalise(ink_mask(_render(ch, f)))
        if vec is not None:
            templates.append((tex, vec, aspect))
    return templates


def _match(mask, templates):
    vec, aspect = normalise(mask)
    best, best_score = None, -1.0
    for tex, tv, ta in templates:
        ratio = min(aspect, ta) / max(aspect, ta)
        score = float(vec @ tv) * (0.6 + 0.4 * ratio)
        if score > best_score:
            best, best_score = tex, score
    return best, best_score


def _pieces(mask):
    cols = mask.any(axis=0)
    out, start = [], None
    for x, on in enumerate(list(cols) + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            out.append(mask[:, start:x])
            start = None
    return out


def classify(im, templates):
    """Return (latex, score). '' = blank image. Score < ACCEPT means not confident."""
    mask = ink_mask(im)
    if mask.sum() < 4:
        return "", 1.0
    whole = _match(mask, templates)
    if whole[1] >= ACCEPT:
        return whole

    # Try reading it as a short run of characters, e.g. "10", "-x", "L_1"
    pieces = [p for p in _pieces(mask) if p.sum() >= 3]
    if not 2 <= len(pieces) <= 4:
        return whole
    rows = [np.nonzero(p.any(axis=1))[0] for p in pieces]
    base_top, base_bot = rows[0].min(), rows[0].max()
    base_h = base_bot - base_top + 1
    script_templates = [t for t in templates if t[0].isalnum() or t[0] in "+-*"]
    tokens, scores = [], []  # (kind, tex) with kind in inline / sub / sup
    for n, (p, r) in enumerate(zip(pieces, rows)):
        h = (r.max() - r.min() + 1) / base_h
        top = (r.min() - base_top) / base_h
        centre = top + h / 2
        # thresholds measured on this book: scripts sit clearly above/below the base character,
        # while inline operators (x - y, 5 * 7) sit near its middle
        kind = "inline"
        if n and h < 0.75:
            if centre < 0.2:
                kind = "sup"
            elif centre > 0.7 and top > 0.4:
                kind = "sub"
        tex, s = _match(p, script_templates if kind != "inline" else templates)
        scores.append(s)
        tokens.append((kind, tex))
    score = min(scores)
    if score <= whole[1]:
        return whole

    joined, k = "", 0
    while k < len(tokens):
        kind, tex = tokens[k]
        if kind == "inline":
            # "\in" + "R" must not become the undefined command "\inR"
            if re.search(r"\\[A-Za-z]+$", joined) and tex[:1].isalpha():
                joined += " "
            joined += tex
            k += 1
            continue
        run = []
        while k < len(tokens) and tokens[k][0] == kind:  # f, -, 1 -> f^{-1}, not f^{-}^{1}
            run.append(tokens[k][1])
            k += 1
        joined += ("^" if kind == "sup" else "_") + "{" + "".join(run) + "}"
    return joined, score
