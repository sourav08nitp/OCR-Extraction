"""Second pass for formulas the local pipeline could not read confidently: ask an OpenAI vision model.

Every answer is checked with KaTeX (katex_check.js, same version/options as the web page). A formula
that still fails after one corrective retry keeps its original image, so nothing broken is shown.

Needs OPENAI_API_KEY_2 in the environment. Model: OPENAI_LATEX_MODEL (default gpt-5.4-mini).
"""

import base64
from collections import Counter
import hashlib
import io
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).parent
CACHE_FILE = BASE / "ai_cache.json"
DEFAULT_MODEL = "gpt-5.4-mini"
WORKERS = 6
PROMPT_VERSION = 4  # bump when the prompt or image preprocessing changes, so old cached answers are not reused

PROMPT = (
    "The image was cropped from a school textbook or solutions PDF (maths, physics, chemistry or biology).\n"
    "STEP 1 - decide what it is:\n"
    '  "formula": typeset maths/chemistry that can be written as LaTeX - equations, expressions, symbols, '
    "chemical equations, stacked calculation lines.\n"
    '  "figure": anything that is a picture rather than text - diagrams, labelled illustrations, graphs or plots, '
    "photos, tables, circuit or apparatus drawings, flowcharts, chemical structure drawings (rings/bond lines). "
    "A diagram with labels or a caption is still a figure: never transcribe just its labels or caption.\n"
    "STEP 2 - only for a formula: transcribe exactly what is shown into LaTeX that renders in KaTeX (inline). "
    "Include EVERYTHING visible, left to right: item labels such as (i), (ii), (iv), (a) - write them as "
    "\\text{(ii)} - and leading symbols such as \\Rightarrow or \\therefore. "
    "No $ signs. Keep words with \\text{...}. Use * for a binary operation star, \\therefore for the three-dot "
    "symbol, \\begin{array}{l}...\\end{array} for stacked lines. Do not solve, simplify or correct anything.\n"
    'Answer with JSON only: {"kind": "formula", "latex": "..."} or {"kind": "figure", "latex": null} '
    'or {"kind": "blank", "latex": null} if the image is empty or unreadable.'
)


USAGE_FILE = BASE / "ai_usage.json"
_usage_lock = threading.Lock()


def note_usage(resp, what):
    """Add one API call to ai_usage.json, so the cost of a chapter can be seen afterwards."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    with _usage_lock:
        try:
            data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        row = data.setdefault(model_name(), {}).setdefault(what, {"calls": 0, "input": 0, "output": 0})
        row["calls"] += 1
        row["input"] += getattr(u, "prompt_tokens", 0) or 0
        row["output"] += getattr(u, "completion_tokens", 0) or 0
        USAGE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def available():
    return bool(os.environ.get("OPENAI_API_KEY_2"))


def create_client(timeout):
    """Use the configured second key explicitly, without SDK fallback to the first key."""
    from openai import OpenAI

    key = os.environ.get("OPENAI_API_KEY_2")
    if not key:
        raise ValueError("OPENAI_API_KEY_2 is not set on the server")
    return OpenAI(api_key=key, timeout=timeout, max_retries=3)


def model_name():
    return os.environ.get("OPENAI_LATEX_MODEL", DEFAULT_MODEL)


def katex_errors(latex_list):
    """-> list of None (renders) or error message, one per input."""
    if not latex_list:
        return []
    r = subprocess.run(["node", str(BASE / "katex_check.js")], input=json.dumps(latex_list),
                       capture_output=True, text=True, encoding="utf-8", cwd=BASE, check=True)
    return json.loads(r.stdout)


def _clean(text):
    s = (text or "").strip()
    s = re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", s).strip()
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2]
    elif s.startswith("$") and s.endswith("$"):
        s = s[1:-1]
    elif s.startswith(r"\(") and s.endswith(r"\)"):
        s = s[2:-2]
    return s.strip()


class _Cache:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = value

    def save(self):
        with self.lock:
            tmp = CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CACHE_FILE)


def _for_model(png_bytes, min_height=96):
    # many crops are a single symbol ~30 px tall; enlarge and add margin so the model can read them
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if im.height < min_height:
        s = min_height / im.height
        im = im.resize((round(im.width * s), min_height), Image.LANCZOS)
    im = ImageOps.expand(im, border=24, fill="white")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _parse(text):
    """-> (kind, latex). Tolerates code fences or a bare LaTeX reply from an off-script model."""
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        d = json.loads(s)
        kind = str(d.get("kind", "")).lower()
        latex = _clean(d.get("latex") or "") or None
    except (json.JSONDecodeError, AttributeError):
        kind, latex = "formula", _clean(s) or None
    if kind not in ("formula", "figure", "blank"):
        kind = "formula" if latex else "blank"
    return kind, (latex if kind == "formula" else None)


def _ask(client, model, png_bytes, context, error=None, previous=None):
    content = [{"type": "text", "text": PROMPT + (f"\n\nThe sentence it appears in (for context only): {context}" if context else "")},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(_for_model(png_bytes)).decode()}}]
    messages = [{"role": "user", "content": content}]
    if error:
        messages += [{"role": "assistant", "content": previous},
                     {"role": "user", "content": f"KaTeX could not render that LaTeX: {error}\n"
                                                 'Reply with corrected JSON {"kind": "formula", "latex": "..."} only.'}]
    resp = client.chat.completions.create(model=model, messages=messages, max_completion_tokens=4000,
                                          response_format={"type": "json_object"})
    note_usage(resp, "single formula")
    text = resp.choices[0].message.content
    return (*_parse(text), text)


TRANSCRIPTION_RULES = (
    "Read the entire supplied image in natural reading order, top to bottom and left to right within each column. "
    "Before replying, check the top, bottom, margins, every line, and every (a)/(b)/(i)/(ii) subpart for omissions. "
    "Preserve all visible words, numbers, options, units, superscripts, subscripts, signs, arrows, "
    "punctuation, worked steps, and repeated lines. Do not summarize, solve, simplify, correct, or invent text. "
    "If a character is genuinely unreadable, write [unclear] in its place rather than guessing. "
    r"Use plain text for prose and \(...\) for each mathematical or chemical expression; never use $ delimiters. "
    "Put separate calculation steps on separate lines. Keep option and subpart labels exactly. "
    "For a readable table, transcribe every row and cell in Markdown table form. "
    "For a diagram, graph, photograph, or drawing, insert [[FIGURE]] on its own line at its position; "
    "do not replace a figure with a guess about its contents."
)

REGION_PROMPT = (
    "Transcribe the visible textbook question and its printed answer/solution from this image. "
    "Separate them only where the page actually switches from question to answer; "
    "if no answer is visible, use an empty solution. "
    'Return JSON only: {"question":"...","solution":"..."}.\n' + TRANSCRIPTION_RULES
)

CROP_PROMPT = (
    "This image is a user-selected rectangle from a PDF. It may contain a complete question, "
    "a partial sentence, several subparts, a worked answer, a table, or a figure. "
    "Transcribe EVERYTHING inside the rectangle, including printed Q./Sol./Ans. labels if visible. "
    "Do not discard text because it seems to belong to another question or page. "
    "Put the entire transcription in the {field} field and leave the other field empty. "
    'Return JSON only: {{"question":"...","solution":"..."}}.\n' + TRANSCRIPTION_RULES
)

PAGE_QUESTIONS_PROMPT = (
    "Audit this entire scanned textbook page after local OCR. Find EVERY explicitly numbered question "
    "whose Q./Question heading begins on this page, including questions near the top and bottom. "
    "For each, transcribe all question text and every answer/solution line visible on THIS page. "
    "Do not add a continuation from the previous page as a new question. "
    "Do not invent the remainder of a question or answer that continues onto the next page. "
    "Include its printed exercise heading if visible. Give the bounding rectangle from the first "
    "question line through the last visible answer line as [left,top,right,bottom] fractions of page width/height. "
    "If no explicitly numbered question begins here, return an empty array. "
    'Return JSON only: {"questions":[{"number":1,"exercise":"EXERCISE 1.1",'
    '"question":"...","solution":"...","bbox":[0.1,0.2,0.9,0.8]}]}.\n' + TRANSCRIPTION_RULES
)

PAGE_LINES_PROMPT = (
    "The local OCR could not read this PDF page. Transcribe every visible line in reading order, "
    "including chapter and exercise headings, question numbers, answer headings, answer choices, "
    "tables, formulas, and worked solution steps. Put each printed line in a separate JSON string. "
    "Keep Q./Question and Sol./Answer prefixes exactly so the question splitter can find boundaries. "
    'Return JSON only: {"lines":["first printed line","next printed line"]}.\n' + TRANSCRIPTION_RULES
)

LATEX_REPAIR_PROMPT = (
    "Repair the maths, chemistry notation, and broken symbols in the CURRENT TEXT using the PDF image as ground truth. "
    "The image may also contain the other half of the question: return ONLY the selected {part} text. "
    "Preserve every prose sentence, answer choice, calculation step, label, and line break in the current text. "
    "Replace non-printing control characters, replacement glyphs, and visible boxes with the symbols printed in the PDF. "
    "For a reaction, retain reactants, products, coefficients, states, and text above/below the arrow. "
    r"Put each mathematical or chemical expression inside \(...\), with KaTeX-compatible LaTeX "
    r"(for example O_2 and \xrightarrow{{\text{{Heat}}}}). Do not use $ delimiters. "
    "Keep each existing ![](img:...) reference exactly in its original position; do not invent or remove images. "
    "Do not solve, summarize, or change the meaning. If the PDF does not clarify a symbol, retain its original image reference. "
    'Return JSON only: {{"text":"the complete corrected selected part"}}.\nCURRENT TEXT:\n{current}'
)

BAD_TEXT_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")
# maths spans in what the model returns; $...$ is accepted too because the model occasionally falls back to it
RE_MATH = re.compile(r"\\\((.+?)\\\)|\\\[(.+?)\\\]|\$([^$]+)\$", re.S)


def maths_spans(text):
    """Every maths span in a transcription, whatever delimiter the model used."""
    # exactly one group matches per span; the others come back as ""
    return [m.group(1) or m.group(2) or m.group(3) or "" for m in RE_MATH.finditer(text or "")]


def _vision_image(png_bytes, *, margin=True):
    """Preserve small print on dense PDF crops when the configured model supports it."""
    model = model_name().lower()
    detail = "original" if re.match(r"^gpt-(?:5\.(?:4|5|6)|6)(?:[-.]|$)", model) else "high"
    return {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(
            _for_model(png_bytes) if margin else png_bytes).decode(),
        "detail": detail}}


def transcribe_region(png_bytes, n_figures=0, part=None):
    r"""One whole question (its highlighted box) -> {"question", "solution", "katexErrors"} with \(...\) maths."""
    client = create_client(timeout=120)
    prompt = REGION_PROMPT
    if part in ("stem", "sol"):
        field = "question" if part == "stem" else "solution"
        prompt = CROP_PROMPT.format(field=field)
    if n_figures:
        prompt += f"\nThe current extraction found {n_figures} figure(s); keep their positions in the text."
    content = [{"type": "text", "text": prompt},
               _vision_image(png_bytes)]
    messages = [{"role": "user", "content": content}]

    def ask():
        r = client.chat.completions.create(model=model_name(), messages=messages, max_completion_tokens=12000,
                                           response_format={"type": "json_object"})
        note_usage(r, "whole question re-read")
        if r.choices[0].finish_reason == "length":
            raise ValueError("AI response was cut off; select a smaller area or retry")
        text = r.choices[0].message.content
        d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        return str(d.get("question") or ""), str(d.get("solution") or ""), text

    def bad_math(*texts):
        maths = [m for t in texts for m in maths_spans(t)]
        return [f"\\({m}\\): {e}" for m, e in zip(maths, katex_errors(maths)) if e]

    question, solution, raw = ask()
    errors = bad_math(question, solution)
    bad_symbols = bool(BAD_TEXT_CHARS.search(question + solution))
    if errors or bad_symbols:  # one corrective retry, like the single-formula path
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Correct the invalid maths and any non-printing control characters using the image. "
                                                 "Keep all text and image markers.\n" + "\n".join(errors[:15]) +
                                                 "\nReply with the corrected JSON only."}]
        q2, s2, _ = ask()
        e2 = bad_math(q2, s2)
        if ((bad_symbols and not BAD_TEXT_CHARS.search(q2 + s2)) or
                (not BAD_TEXT_CHARS.search(q2 + s2) and len(e2) < len(errors))):
            question, solution, errors = q2, s2, e2
    if BAD_TEXT_CHARS.search(question + solution):
        raise ValueError("AI returned unreadable control characters; no text was saved. Try Fix LaTeX on the selected part")
    return {"question": question, "solution": solution, "katexErrors": errors}


def repair_text_latex(png_bytes, current_text, part):
    """Repair one existing review field from its PDF source without touching the other field."""
    if part not in ("question", "answer") or not current_text.strip():
        raise ValueError("Choose a nonempty question or answer to repair")
    images = re.findall(r"!\[\]\([^)]+\)", current_text)
    def prose_words(value):
        plain = RE_MATH.sub(" ", value)
        plain = re.sub(r"!\[\]\([^)]+\)", " ", plain)
        return Counter(re.findall(r"[a-z]{3,}", plain.lower()))

    original_words = prose_words(current_text)
    client = create_client(timeout=120)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": LATEX_REPAIR_PROMPT.format(
            part=part, current=json.dumps(current_text, ensure_ascii=True))},
        _vision_image(png_bytes)]}]
    last_error = None
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model_name(), messages=messages, max_completion_tokens=12000,
            response_format={"type": "json_object"})
        note_usage(response, "review LaTeX repair")
        if response.choices[0].finish_reason == "length":
            raise ValueError("AI response was cut off; the original text was kept")
        raw = response.choices[0].message.content or ""
        try:
            corrected = json.loads(raw).get("text")
        except json.JSONDecodeError:
            corrected = None
        if not isinstance(corrected, str) or not corrected.strip():
            last_error = "AI did not return the corrected text"
        elif BAD_TEXT_CHARS.search(corrected):
            last_error = "AI still returned unreadable control characters"
        elif re.findall(r"!\[\]\([^)]+\)", corrected) != images:
            last_error = "AI changed an image reference"
        elif (sum(original_words.values()) >= 10 and
              sum((original_words & prose_words(corrected)).values()) < .8 * sum(original_words.values())):
            last_error = "AI omitted too much of the original wording"
        elif (corrected.count(r"\(") != corrected.count(r"\)") or
              corrected.count(r"\[") != corrected.count(r"\]")):
            last_error = "AI returned unmatched LaTeX delimiters"
        elif re.search(r"(?<!\\)[_^]", re.sub(r"!\[\]\([^)]+\)|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]", " ", corrected)):
            last_error = "AI left a subscript or superscript outside LaTeX"
        else:
            errors = [e for e in katex_errors(maths_spans(corrected)) if e]
            if not errors:
                return {"text": corrected.strip(), "katexErrors": []}
            last_error = "KaTeX could not render: " + "; ".join(errors[:3])
        if attempt == 0:
            messages.extend([{"role": "assistant", "content": raw},
                             {"role": "user", "content": last_error +
                              ". Correct it from the PDF. Preserve the full text and all image references. "
                              'Reply with JSON {"text":"..."} only.'}])
    raise ValueError(last_error + "; the original text was kept")


def transcribe_page_questions(png_bytes):
    """Audit one scanned page for question starts the local OCR failed to find."""
    client = create_client(timeout=180)
    response = client.chat.completions.create(
        model=model_name(),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PAGE_QUESTIONS_PROMPT}, _vision_image(png_bytes, margin=False)]}],
        max_completion_tokens=16000, response_format={"type": "json_object"})
    note_usage(response, "scanned page question recovery")
    if response.choices[0].finish_reason == "length":
        raise ValueError("AI page response was cut off; no partial questions were saved")
    payload = json.loads(response.choices[0].message.content or "{}")
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise ValueError("AI page response did not contain a question list")
    return [q for q in questions if isinstance(q, dict)]


def transcribe_page_lines(png_bytes):
    """Emergency full-page text pass when the local OCR reader raises an error."""
    client = create_client(timeout=180)
    response = client.chat.completions.create(
        model=model_name(),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PAGE_LINES_PROMPT}, _vision_image(png_bytes, margin=False)]}],
        max_completion_tokens=16000, response_format={"type": "json_object"})
    note_usage(response, "scanned page OCR fallback")
    if response.choices[0].finish_reason == "length":
        raise ValueError("AI page response was cut off; no partial page was saved")
    payload = json.loads(response.choices[0].message.content or "{}")
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise ValueError("AI page response did not contain text lines")
    return [line.strip() for line in lines if isinstance(line, str) and line.strip()]


def transcribe(items, progress=None, *, force=False):
    """items: list of (image_path, context_sentence).
    -> {image_path: {"kind": formula|figure|blank|None, "latex": str|None, "error": str|None}}.
    latex None means keep the image; kind "figure" means it is a picture and should stay one.
    force requests a fresh AI response, even when a successful result is cached."""
    client = create_client(timeout=90)
    model = model_name()
    cache = _Cache()
    results, done = {}, [0]
    lock = threading.Lock()

    def work(item):
        path, context = item
        png = Path(path).read_bytes()
        key = f"v{PROMPT_VERSION}:{model}:{hashlib.md5(png).hexdigest()}"
        hit = None if force else cache.get(key)
        # A failed conversion must be retried rather than replayed indefinitely.
        if hit is not None and (hit.get("error") or
                                (hit.get("kind") == "formula" and not hit.get("latex"))):
            hit = None
        if hit is None:
            try:
                kind, tex, raw = _ask(client, model, png, context)
                err = None
                if kind == "formula" and tex:
                    err = katex_errors([tex])[0]
                    if err:
                        kind2, tex2, _ = _ask(client, model, png, context, error=err, previous=raw)
                        err2 = katex_errors([tex2])[0] if tex2 else "empty reply"
                        tex, err = (tex2, None) if kind2 == "formula" and err2 is None else (None, err2)
                elif kind == "formula":
                    tex, err = None, "model returned no LaTeX"
                elif kind == "blank":
                    err = "model found no readable content"
                hit = {"kind": kind, "latex": tex, "error": err}
                cache.put(key, hit)
            except Exception as e:  # network/API problems: keep the image, try again next run
                hit = {"kind": None, "latex": None, "error": f"{type(e).__name__}: {e}"[:300]}
        with lock:
            results[path] = hit
            done[0] += 1
            if progress:
                progress("AI checking formulas and figures", done[0], len(items))

    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(work, items))
    finally:
        cache.save()
    return results
