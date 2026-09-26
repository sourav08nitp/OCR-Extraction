"""Second pass for formulas the local pipeline could not read confidently: ask an OpenAI vision model.

Every answer is checked with KaTeX (katex_check.js, same version/options as the web page). A formula
that still fails after one corrective retry keeps its original image, so nothing broken is shown.

Needs OPENAI_API_KEY in the environment. Model: OPENAI_LATEX_MODEL (default gpt-5.4-mini).
"""

import base64
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
    return bool(os.environ.get("OPENAI_API_KEY"))


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


REGION_PROMPT = (
    "The image shows ONE question from a school textbook solutions book (maths, physics, chemistry or biology), "
    "usually followed by its answer or solution. Transcribe it faithfully.\n"
    'Reply with JSON only: {"question": "...", "solution": "..."}.\n'
    "Rules:\n"
    "- Plain text for words. ALL maths and chemical formulas inline between \\\\( and \\\\) (KaTeX syntax), "
    "for example \\\\(x^2 + 1\\\\); a step that is only maths goes on its own line, still between \\\\( and \\\\). "
    "Never use $ signs as maths delimiters.\n"
    "- Keep item labels such as (i), (ii), (a) and symbols such as \\Rightarrow, \\therefore. Keep one line per step.\n"
    "- Leave out the heading words (Q.2., Question 2:, Sol., Answer:, Ans.) - only the content.\n"
    "- Leave out page headers, page numbers, chapter-title banners and anything from a different question.\n"
    "- Where a diagram, graph, table picture or figure appears, write [[FIGURE]] on its own line instead of describing "
    "it{fig_hint}.\n"
    "- Do not solve, simplify or correct anything. If there is no solution in the image, use an empty string."
)
# maths spans in what the model returns; $...$ is accepted too because the model occasionally falls back to it
RE_MATH = re.compile(r"\\\((.+?)\\\)|\\\[(.+?)\\\]|\$([^$]+)\$", re.S)


def maths_spans(text):
    """Every maths span in a transcription, whatever delimiter the model used."""
    # exactly one group matches per span; the others come back as ""
    return [m.group(1) or m.group(2) or m.group(3) or "" for m in RE_MATH.finditer(text or "")]


def transcribe_region(png_bytes, n_figures=0, part=None):
    r"""One whole question (its highlighted box) -> {"question", "solution", "katexErrors"} with \(...\) maths."""
    from openai import OpenAI

    client = OpenAI(timeout=120, max_retries=3)
    hint = f" (this question has {n_figures} figure(s))" if n_figures else ""
    prompt = REGION_PROMPT.replace("{fig_hint}", hint)
    if part in ("stem", "sol"):
        field = "question" if part == "stem" else "solution"
        prompt += (f"\nThis selected box contains only the {field} part of an existing question. "
                   f"Transcribe everything visible into the JSON '{field}' field and leave the other field empty. "
                   "Do not solve, complete, or invent content outside this box.")
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}}]
    messages = [{"role": "user", "content": content}]

    def ask():
        r = client.chat.completions.create(model=model_name(), messages=messages, max_completion_tokens=8000,
                                           response_format={"type": "json_object"})
        note_usage(r, "whole question re-read")
        text = r.choices[0].message.content
        d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        return str(d.get("question") or ""), str(d.get("solution") or ""), text

    def bad_math(*texts):
        maths = [m for t in texts for m in maths_spans(t)]
        return [f"\\({m}\\): {e}" for m, e in zip(maths, katex_errors(maths)) if e]

    question, solution, raw = ask()
    errors = bad_math(question, solution)
    if errors:  # one corrective retry, like the single-formula path
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "These maths pieces do not render in KaTeX:\n" + "\n".join(errors[:15]) +
                                                 "\nReply with the corrected JSON only."}]
        q2, s2, _ = ask()
        e2 = bad_math(q2, s2)
        if len(e2) < len(errors):
            question, solution, errors = q2, s2, e2
    return {"question": question, "solution": solution, "katexErrors": errors}


def transcribe(items, progress=None):
    """items: list of (image_path, context_sentence).
    -> {image_path: {"kind": formula|figure|blank|None, "latex": str|None, "error": str|None}}.
    latex None means keep the image; kind "figure" means it is a picture and should stay one."""
    from openai import OpenAI

    client = OpenAI(timeout=90, max_retries=3)
    model = model_name()
    cache = _Cache()
    results, done = {}, [0]
    lock = threading.Lock()

    def work(item):
        path, context = item
        png = Path(path).read_bytes()
        key = f"v{PROMPT_VERSION}:{model}:{hashlib.md5(png).hexdigest()}"
        hit = cache.get(key)
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
