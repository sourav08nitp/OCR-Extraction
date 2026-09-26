"""Review & export: turn a job's structured.json into records in the question-bank schema.

Fields are split into what code fills automatically (from the PDF) and what a person fills in the
review screen (saved in jobs/<id>/review.json). Export merges both, manual values winning.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pdf_to_structured as pts

QUESTION_TYPES = ["subjective", "single_correct", "multiple_correct", "integer", "numerical",
                  "match_the_column", "comprehension", "assertion_reason", "true_false", "fill_in_the_blank"]
LEVELS = ["easy", "medium", "hard"]

# per-question fields a person can set; "" / None means "use the document default or the automatic value"
MANUAL_FIELDS = ["topic", "level", "questionType", "sectionName", "isPyq", "pyqExam", "pyqYear", "paper",
                 "flagged", "skip", "stemOverride", "solutionOverride", "aiReread", "questionNumber"]
# edited text refers to images as ![](img:NAME); they become real URLs on screen and in the export
IMG_REF = re.compile(r"!\[\]\(img:([^)\s]+)\)")
# document-level settings shown on the Document tab
DOC_FIELDS = ["documentId", "module", "chapter", "subject", "section", "sectionName", "questionType", "level",
              "topic", "isPyq", "pyqExam", "pyqYear", "paper", "answerFrom", "imageBaseUrl",
              "syllabusChapter", "topics"]
TOPICS_FILE = Path(__file__).resolve().parent / "topics.json"
REQUIRED = ["module", "chapter", "subject", "topic", "level", "questionType"]  # warned about on export

# Maths is \(...\) inline and \[...\] display everywhere: recogniser, AI prompt, review screen, export
# - the same form the question bank already stores. The export does not convert anything; the one
# place that can still produce $...$ is the model ignoring its prompt, so AI answers are normalised
# as they come in (see _reread_one and add_question). MATH_FIELDS is what carries maths.
MATH_FIELDS = ("stem", "answer", "explanation")

# How images sit in the exported text.
#   False - the question bank's own way: text fields are pure text, and a picture is reached through
#           questionImage / optionImages / images / imageCrops. Its 159 documents with a question
#           image all have real text in the stem and no reference to the picture inside it.
#   True  - keep the ![](url) markdown where the picture actually appeared in the text.
# Off by default so the reading app works today. Nothing is lost by that: jobs/<id>/review.json keeps
# the inline positions for good, the export is derived from it, and a push upserts by _id - so
# flipping this later and pushing again rewrites the same documents instead of making new ones.
INLINE_IMAGES = False
RE_DISPLAY_MATH = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.S)
RE_INLINE_MATH = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$", re.S)


def to_paren_delims(text):
    r"""$x$ -> \(x\) and $$x$$ -> \[x\]. An unpaired or escaped \$ (currency) is left alone."""
    if not text or "$" not in text:
        return text
    text = RE_DISPLAY_MATH.sub(lambda m: r"\[" + m.group(1) + r"\]", text)
    return RE_INLINE_MATH.sub(lambda m: r"\(" + m.group(1) + r"\)", text)


RE_PYQ = re.compile(r"\[?\(?\b(JEE(?:[ -]?(?:Main|Mains|Advanced|Adv))?|AIEEE|IIT[- ]?JEE|NEET|AIPMT|CBSE|NSEC|BITSAT)"
                    r"\b[^\]\)\n]{0,20}?\b((?:19|20)\d{2})\b", re.I)


# review.json is written by the app and by the tools/ scripts, sometimes at the same moment. Windows
# refuses to open or replace a file for the instant another writer is swapping it, so both sides
# retry briefly rather than failing - roughly two seconds, far longer than a swap takes.
_FILE_RETRIES, _FILE_WAIT = 40, 0.05


def object_id():
    """24-hex id in MongoDB ObjectId layout (4-byte time + 8 random bytes)."""
    return f"{int(time.time()):08x}{os.urandom(8).hex()}"


def _load(path, default, required=False):
    """A missing file is normal and gives the default. A file that exists but will not parse is not:
    for review.json that would hand back empty defaults, and the ids would be minted again - a whole
    duplicate set of questions in the database, quietly. Better to stop."""
    try:
        for attempt in range(_FILE_RETRIES):
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except PermissionError:
                if attempt == _FILE_RETRIES - 1:
                    raise
                time.sleep(_FILE_WAIT)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        if required:
            raise RuntimeError(f"{path} exists but could not be read ({e}); refusing to start over "
                               f"with new ids - fix or remove the file") from e
        return default


def _key_labels(doc):
    return {key: f"{ex['title']}|{q.get('label', q['number'])}" for key, ex, q in questions(doc)}


def ensure_current(job_dir):
    """Older results lack answer headings, labels, page regions or the newer line grouping: re-split them
    (fast, keeps LaTeX). Saved edits move with their question, matched by exercise and question number."""
    job_dir = Path(job_dir)
    out = job_dir / "out"
    doc = _load(out / "structured.json", None)
    if doc is None:
        raise FileNotFoundError("no result for this job")
    if doc.get("structure_version") != pts.STRUCTURE_VERSION:
        before = _key_labels(doc)
        doc = pts.restructure(job_dir / "input.pdf", out)
        review = load_review(job_dir)
        if review["questions"] or review["ids"]:
            after = {v: k for k, v in _key_labels(doc).items()}   # label -> new key
            moved = {"questions": {}, "ids": {}, "lost": {}}
            for field in ("questions", "ids"):   # add-* keys are ours, not the PDF's: carry them over as they are
                moved[field].update({k: v for k, v in review[field].items() if k.startswith("add-")})
            for old_key, label in before.items():
                new_key = after.get(label)
                for field in ("questions", "ids"):
                    if old_key in review[field]:
                        if new_key:
                            moved[field][new_key] = review[field][old_key]
                        elif field == "questions":
                            moved["lost"][label] = review[field][old_key]
            review["questions"], review["ids"] = moved["questions"], moved["ids"]
            if moved["lost"]:
                review.setdefault("orphanedEdits", {}).update(moved["lost"])
            save_review(job_dir, review)
    if not doc.get("figures_checked") and any(s.get("equations_latex") for s in pts._sections(doc)):
        pts.classify_figures(doc, out / "images")
        pts.build_text_latex(doc)
        doc["figures_checked"] = True
        pts.save(doc, out)
    # last, and never saved into structured.json: that file stays the pipeline's own output
    doc["added_questions"] = [_added_question(a) for a in load_review(job_dir)["addedQuestions"]]
    return doc


def load_review(job_dir):
    r = _load(Path(job_dir) / "review.json", {}, required=True)
    r.setdefault("document", {})
    r.setdefault("questions", {})
    r.setdefault("ids", {})
    r.setdefault("imageIds", {})      # image file name -> stable id, so exports keep the same reference
    r.setdefault("manualImages", {})  # images you cropped yourself: name -> {page, bbox in PDF points}
    r.setdefault("addedQuestions", [])  # questions you drew a box around yourself; see add_question()
    return r


def image_id(review, file):
    return review["imageIds"].setdefault(file, object_id())


def save_review(job_dir, review):
    """Write review.json atomically, through a temp file unique to this writer.

    A shared "review.tmp" is a race: two savers at once (the app pushing while a script runs) can
    have one replace review.json with the other's half-written temp. The file is then unreadable,
    load_review used to answer with empty defaults, and the next export minted a fresh documentId and
    a fresh id for every question - a duplicate set in the database with no images attached."""
    p = Path(job_dir) / "review.json"
    tmp = p.with_name(f"review.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(review, indent=1, ensure_ascii=False), encoding="utf-8")
        for attempt in range(_FILE_RETRIES):
            try:
                tmp.replace(p)          # atomic, but Windows refuses while a reader holds the target
                break
            except PermissionError:
                if attempt == _FILE_RETRIES - 1:
                    raise
                time.sleep(_FILE_WAIT)
    finally:
        if tmp.exists():
            tmp.unlink()


def _images_in(sec):
    """Images that stay pictures in this text: figures, plus formulas nobody could read confidently."""
    out = []
    for name in sec.get("equations", []):
        r = sec.get("equations_latex", {}).get(name)
        if r is None or r.get("source") == "figure" or r.get("needs_review") or r.get("latex") is None:
            if name not in out:
                out.append(name)
    return out


def _as_text(sec, image_url):
    """LaTeX text with any remaining image placeholder turned into a markdown image at its exact spot."""
    text = sec.get("text_latex", sec.get("text", ""))
    return re.sub(r"\[\[eq:([^\]]+)\]\]", lambda m: f"![]({image_url(m.group(1))})", text).strip()


def _review_text(sec, image_url):
    """Show an AI formula with its source crop at the original placeholder position, for Review only."""
    readings = sec.get("equations_latex", {})

    def replace(match):
        name = match.group(1)
        reading = readings.get(name) or {}
        latex = reading.get("latex")
        if latex is not None and not reading.get("needs_review"):
            if not latex:
                return ""
            formula = rf"\({latex}\)"
            return formula + f"[[src:{name}]]" if reading.get("source") == "ai" else formula
        return f"![]({image_url(name)})"

    return re.sub(r"\[\[eq:([^\]]+)\]\]", replace, sec.get("text", "")).strip()


def _norm_bbox(bbox, size):
    w, h = size
    x0, y0, x1, y1 = bbox
    return [round(max(0, x0 / w), 4), round(max(0, y0 / h), 4), round(min(1, x1 / w), 4), round(min(1, y1 / h), 4)]


def _section(text):
    """A question/solution part in the shape the rest of the code expects from the splitter."""
    return {"text": text, "text_latex": text, "equations": [], "equations_latex": {}}


def _added_question(entry):
    """One review["addedQuestions"] entry -> a question dict like the ones split out of the PDF."""
    return {"number": entry.get("number") or 0,
            "label": entry.get("label") or str(entry.get("number") or "?"),
            "question": _section(entry.get("stem", "")),
            "solution": _section(entry.get("solution", "")),
            "regions": [{"page": entry["page"], "bbox": entry["bbox"]}],
            "lineRegions": [],
            "solution_heading": None,
            "added": True,
            "addedId": entry["id"],
            "figuresSeen": entry.get("figuresSeen", 0),
            "exerciseTitle": entry.get("exercise") or "UNKNOWN"}


def questions(doc):
    """Stable (key, exercise, question) list; key is position based so saved edits survive re-opening.
    Questions you added by hand come last and keep an "add-<id>" key, which no re-split can move."""
    out = []
    for ei, ex in enumerate(doc["exercises"]):
        for qi, q in enumerate(ex["questions"]):
            out.append((f"{ei}-{qi}", ex, q))
    for q in doc.get("added_questions", []):
        out.append((f"add-{q['addedId']}", {"title": q["exerciseTitle"]}, q))
    return out


def auto_fields(doc, ex, q, image_url):
    """Everything code can fill for one question, plus notes on why it may need a look."""
    sizes = {int(k): v for k, v in doc.get("page_sizes", {}).items()}
    boxes = doc.get("image_boxes", {})
    stem = _as_text(q["question"], image_url)
    sol = _as_text(q["solution"], image_url)
    stem_imgs, sol_imgs = _images_in(q["question"]), _images_in(q["solution"])
    notes = []

    info = {**q["question"].get("equations_latex", {}), **q["solution"].get("equations_latex", {})}
    pyq = RE_PYQ.search(q["question"].get("text", ""))
    region = q["regions"][0] if q.get("regions") else None
    crops = []
    source_images = []
    for part in ("question", "solution"):
        sec = q[part]
        for name in dict.fromkeys(sec.get("equations", [])):
            reading = sec.get("equations_latex", {}).get(name) or {}
            if reading.get("source") != "ai" or not reading.get("latex") or reading.get("needs_review"):
                continue
            box = boxes.get(name)
            page = box.get("page") if box else None
            source_images.append({"name": name, "part": part, "latex": reading["latex"],
                                  "page": page,
                                  "bbox": _norm_bbox(box["bbox"], sizes[page]) if page in sizes else None})
    for name in stem_imgs + sol_imgs:
        b = boxes.get(name)
        if b and b["page"] in sizes:
            crops.append({"file": name, "url": image_url(name), "page": b["page"],
                          "bbox": _norm_bbox(b["bbox"], sizes[b["page"]]),
                          "kind": "figure" if info.get(name, {}).get("source") == "figure" else "unreadable_formula"})

    unresolved = [n for n in stem_imgs + sol_imgs if info.get(n, {}).get("needs_review")]
    if unresolved:
        notes.append(f"{len(unresolved)} formula(s) could not be read and stay as images")
    if not sol:
        notes.append("no answer/solution found under this question")
    if not stem:
        notes.append("question text is empty")
    if q.get("added"):
        notes.append("added by hand from a box you drew on the PDF")
        if q.get("figuresSeen") and not (stem_imgs or sol_imgs):
            notes.append(f"the AI saw {q['figuresSeen']} figure(s) in that box - add them with ✂ Crop")

    return {
        "questionNumber": q["number"],
        "label": q.get("label", str(q["number"])),
        "added": bool(q.get("added")),
        "exercise": ex["title"],
        "sectionName": None if ex["title"] == "UNKNOWN" else ex["title"],
        "chapter": doc.get("chapter"),
        "stem": stem,
        "solutionText": sol,
        "reviewStem": _review_text(q["question"], image_url),
        "reviewSolution": _review_text(q["solution"], image_url),
        "sourceImages": source_images,
        "solutionHeading": q.get("solution_heading"),
        "images": [image_url(n) for n in stem_imgs],
        "explanationImages": [image_url(n) for n in sol_imgs],
        "imageCrops": crops,
        "questionType": "subjective",  # options are not parsed yet, so every question is written-answer
        "isPyq": bool(pyq),
        "pyqExam": pyq.group(1).upper().replace(" ", "-") if pyq else None,
        "pyqYear": int(pyq.group(2)) if pyq else None,
        "flagged": bool(unresolved) or not sol,
        "regions": [{"page": r["page"], "bbox": _norm_bbox(r["bbox"], sizes[r["page"]])}
                    for r in q.get("regions", []) if r["page"] in sizes],
        "lineRegions": [{"page": r["page"], "bbox": _norm_bbox(r["bbox"], sizes[r["page"]])}
                        for r in q.get("lineRegions", []) if r["page"] in sizes],
        "sourceRegion": ({"page": region["page"], "bbox": _norm_bbox(region["bbox"], sizes[region["page"]])}
                         if region and region.get("page") in sizes else None),
        "notes": notes,
    }


def _pick(manual, docset, auto, field):
    for v in (manual.get(field), docset.get(field), auto.get(field)):
        if v not in (None, ""):
            return v
    return None


def build_record(doc, key, ex, q, review, image_url, now, px_size=None):
    a = auto_fields(doc, ex, q, image_url)
    m = review["questions"].get(key, {})
    d = review["document"]
    answer_from = d.get("answerFrom") or "auto"
    heading = a["solutionHeading"]
    # your edits (or an AI re-read) replace the automatic text; images follow whatever the text still refers to
    stem, stem_imgs = a["stem"], a["images"]
    sol, expl_imgs = a["solutionText"], a["explanationImages"]
    crops = list(a["imageCrops"])
    if m.get("stemOverride") is not None or m.get("solutionOverride") is not None:
        used = []
        if m.get("stemOverride") is not None:
            stem, names = _resolve(m["stemOverride"], image_url)
            stem_imgs = [image_url(n) for n in names]
            used += names
        else:
            used += [c["file"] for c in crops if c["url"] in stem_imgs]
        if m.get("solutionOverride") is not None:
            sol, names = _resolve(m["solutionOverride"], image_url)
            expl_imgs = [image_url(n) for n in names]
            used += names
        else:
            used += [c["file"] for c in crops if c["url"] in expl_imgs]
        crops = [c for c in crops if c["file"] in used]
    for name, box in review["manualImages"].items():   # images you cropped yourself
        if f"img:{name}" in (m.get("stemOverride") or "") + (m.get("solutionOverride") or ""):
            sizes = {int(k): v for k, v in doc.get("page_sizes", {}).items()}
            crops.append({"file": name, "url": image_url(name), "page": box["page"],
                          "bbox": _norm_bbox(box["bbox"], sizes[box["page"]]) if box["page"] in sizes else [0, 0, 1, 1],
                          "kind": "cropped by hand"})
    # the question bank has no separate field for answer images, so they join `images` alongside the
    # question's own, told apart by the `type` on each imageCrops entry
    if answer_from == "answer" or (answer_from == "auto" and heading == "answer"):
        answer, explanation = sol or None, None
    else:
        answer, explanation = None, sol or None
    is_pyq = _pick(m, d, a, "isPyq")
    qtype = _pick(m, d, a, "questionType")
    sizes = {int(k): v for k, v in doc.get("page_sizes", {}).items()}

    # choices live in `options`, not inline in the stem - and for a choice question the bank's
    # `answer` is the label ("B"), with the working in `explanation`
    stem, options = split_options(stem, qtype)
    if options:
        mark_correct(options, answer, explanation, sol)
        picked = next((o["label"] for o in options if o["isCorrect"]), "")
        if picked:
            explanation = explanation or answer or None
            answer = picked

    # one imageCrops entry per picture, in the bank's shape; `images` carries the same URLs
    # "explanation" marks a picture that belongs to the worked solution rather than the question.
    # The collection has no other home for one - it has never stored a solution image - so this is a
    # type value the reading app has to know about. Object names stay in the bucket's own vocabulary.
    bank_crops = []
    for c in crops:
        bank_crops.append({"url": c["url"],
                           "type": "explanation" if c["url"] in expl_imgs else "question",
                           "optionIndex": 0,
                           **_crop_px(c, sizes, px_size(c["file"]) if px_size else None)})

    if not INLINE_IMAGES:
        # the bank's text fields never hold an image reference; the pictures are still all here, in
        # `images` and `imageCrops`, and where each one sat in the text stays in review.json
        stem = _strip_images(stem)
        answer = _strip_images(answer)
        explanation = _strip_images(explanation)
    return {
        "id": review["ids"].setdefault(key, object_id()),
        "documentId": d.get("documentId"),
        "questionNumber": _to_int(m.get("questionNumber")) or a["questionNumber"],
        # section is never null in the question bank - every one of its 6,342 questions has a string,
        # and a null makes the reading app drop the row - so fall back to its usual "All sections"
        "path": {"module": d.get("module") or None,
                 "chapter": d.get("chapter") or a["chapter"],
                 "section": d.get("section") or "All sections"},
        "stem": stem,
        "options": options,
        "answer": answer or "",
        "explanation": explanation,
        "images": stem_imgs + [u for u in expl_imgs if u not in stem_imgs],
        "match": None,
        "passageId": None,
        "groupOrder": None,
        "isQuestionImage": bool(stem_imgs),
        # only a picture belonging to the question itself; a solution diagram is not the question's
        # image, and showing one as such was wrong
        "questionImage": (stem_imgs or [None])[0],
        "isOptionImage": False,
        "optionImages": [],
        "imageCrops": bank_crops,
        "questionType": qtype,
        "level": _pick(m, d, a, "level"),
        "sectionName": _pick(m, d, a, "sectionName"),
        "topic": _pick(m, d, a, "topic"),
        "subject": d.get("subject") or None,
        "flagged": bool(m["flagged"]) if m.get("flagged") is not None else a["flagged"],
        "isPyq": bool(is_pyq) if is_pyq is not None else False,
        "pyqExam": _pick(m, d, a, "pyqExam") if is_pyq else None,
        "pyqYear": _to_int(_pick(m, d, a, "pyqYear")) if is_pyq else None,
        "paper": _pick(m, d, a, "paper") if is_pyq else None,
        "sourceRegion": a["sourceRegion"],
        "createdAt": now,
        "updatedAt": now,
    }


OPTION_TYPES = {"single_correct", "multiple_correct", "multi_correct", "assertion_reason", "true_false"}
# "(a) 12    (b) 15" or one per line; letters only, so the (i)/(ii) of a multi-part question are left alone
RE_OPTION = re.compile(r"(?:^|\s)[\(\[]?([a-dA-D])[\)\].]\s+(?=\S)")
# "Ans. (c)", "Answer: C", "correct option is (d)" - and the other way round, "(b) is correct"
_FILLER = r"(?:\W+(?:is|are|the|option|answer|choice|ans)\b)*\W{0,4}"
RE_CORRECT = re.compile(r"(?:ans(?:wer)?|option|correct)\b" + _FILLER + r"[\(\[]?([a-dA-D])[\)\].]?(?![a-zA-Z0-9])"
                        r"|[\(\[]([a-dA-D])[\)\]]\s+(?:is\s+)?(?:the\s+)?correct", re.I)


def split_options(text, question_type):
    """MCQ stem -> (stem without the choices, [{label, body, isCorrect}]).

    Only for the types that have choices; everything else keeps its text untouched. The question bank
    stores choices in `options`, not inline in the stem, so they are moved rather than copied."""
    if not text or (question_type or "") not in OPTION_TYPES:
        return text, []
    marks = list(RE_OPTION.finditer(text))
    if len(marks) < 2:
        return text, []
    labels = [m.group(1).upper() for m in marks]
    if labels != sorted(set(labels), key=labels.index) or labels[0] != "A":   # must be A, B, C... once each
        return text, []

    options = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if not body:
            return text, []            # a bare "(a)" with nothing after it is not a choice list
        options.append({"label": labels[i], "body": body, "isCorrect": False})
    return text[:marks[0].start()].strip(), options


def mark_correct(options, *texts):
    """Tick the choice the solution names. Leaves them all false when it cannot tell."""
    for t in texts:
        m = RE_CORRECT.search(t or "")
        if m:
            want = (m.group(1) or m.group(2)).upper()
            if any(o["label"] == want for o in options):
                for o in options:
                    o["isCorrect"] = o["label"] == want
                return True
    return False


CROP_DPI = 200   # what region_png renders at; only used when an image file cannot be measured


def _crop_px(crop, sizes, pixels=None):
    """Our 0-1 bbox -> the bank's nx/ny/nw/nh.

    In the live collection nw/nh are exactly the pixel width and height of the stored image file
    (checked against six of them: ratio 1.000), and nx/ny are its position in that same pixel space.
    So the scale is taken from the actual file rather than assumed, and only falls back to CROP_DPI
    when the file is missing."""
    page, bbox = crop.get("page"), crop.get("bbox") or [0, 0, 0, 0]
    w, h = sizes.get(page, (612, 792))
    x0, y0, x1, y1 = bbox
    box_w, box_h = max((x1 - x0) * w, 1e-6), max((y1 - y0) * h, 1e-6)
    if pixels:
        px_w, px_h = pixels
        sx, sy = px_w / box_w, px_h / box_h
    else:
        sx = sy = CROP_DPI / 72
        px_w, px_h = box_w * sx, box_h * sy
    return {"nx": round(x0 * w * sx, 2), "ny": round(y0 * h * sy, 2),
            "nw": round(px_w, 2), "nh": round(px_h, 2)}


def image_sizes(job_dir):
    """name -> (width, height) in pixels, measured once per export and cached."""
    folder = Path(job_dir) / "out" / "images"
    cache = {}

    def size_of(name):
        if name not in cache:
            try:
                from PIL import Image
                with Image.open(folder / name) as im:
                    cache[name] = im.size
            except Exception:
                cache[name] = None
        return cache[name]
    return size_of


RE_MD_IMAGE = re.compile(r"[ \t]*!\[\]\([^)\s]*\)[ \t]*")


def _strip_images(text):
    """Take the ![](...) references out of a text field, leaving the words tidy."""
    if not text:
        return text
    out = RE_MD_IMAGE.sub(" ", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _resolve(text, image_url):
    """Edited text -> (text with real image URLs, image names in order)."""
    names = IMG_REF.findall(text or "")
    return IMG_REF.sub(lambda mm: f"![]({image_url(mm.group(1))})", text or "").strip(), names


def syllabus():
    """{"Class 10/Maths/Real Numbers": {"topics": [...], "source": ...}} built by tools/build_topics.py."""
    return _load(TOPICS_FILE, {})


RE_CLASS = re.compile(r"class\s*[-–—:]?\s*(XII|XI|IX|X|VIII|VII|VI|\d{1,2})\b", re.I)
ROMAN = {"VI": "6", "VII": "7", "VIII": "8", "IX": "9", "X": "10", "XI": "11", "XII": "12"}
_class_cache = {}


def detect_class(job_dir):
    """"(Class - IX)" in the page header -> "Class 9". Only the header is read: question text says
    things like "30 students of Class VIII" and would otherwise be mistaken for the book's class.
    Many of these books never print it, so None is a normal answer."""
    job_dir = str(job_dir)
    if job_dir in _class_cache:
        return _class_cache[job_dir]
    found = None
    try:
        import pdfplumber
        with _PDF_LOCK, pdfplumber.open(Path(job_dir) / "input.pdf") as pdf:
            for page in pdf.pages[:3]:
                band = page.crop((0, 0, page.width, page.height * 0.16)).extract_text() or ""
                m = RE_CLASS.search(band)
                if m:
                    n = m.group(1).upper()
                    found = "Class " + ROMAN.get(n, n)
                    break
    except Exception:
        found = None
    _class_cache[job_dir] = found
    return found


def guess_syllabus_key(pdf_name, subject=None, class_hint=None):
    """Best matching chapter in topics.json for a job, from its PDF file name.

    Returns None when two chapters match equally well and nothing says which class it is - both
    Class 9 and Class 10 have "Chapter 14 - Statistics.pdf", and their topic lists share no entries,
    so a silent guess tags a whole chapter from the wrong syllabus. Better to ask on the Document tab."""
    name = re.sub(r"^Chapter \d+\s*-\s*", "", Path(pdf_name).stem)
    scored = []
    for key in syllabus():
        cls, subj, chapter = key.split("/")
        if class_hint and cls.lower() != class_hint.lower():
            continue
        s = _title_score(name, chapter)
        if subject and subj.lower() not in (subject or "").lower() and (subject or "").lower() not in subj.lower():
            s -= 0.15
        scored.append((s, key))
    scored.sort(key=lambda t: (-t[0], t[1]))
    if not scored or scored[0][0] < 0.6:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.01:
        return None
    return scored[0][1]


def _title_score(a, b):
    wa, wb = set(_words(a)), set(_words(b))
    return len(wa & wb) / max(len(wa | wb), 1)


def _words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in {"and", "the", "of", "in", "to", "a", "an", "its", "our"}]


def allowed_topics(review, job_dir=None, doc=None):
    """The topic list this job is restricted to: your edited list, else the syllabus list for its chapter
    (picked on the Document tab, or worked out from the PDF name)."""
    own = review["document"].get("topics")
    if own:
        return list(own)
    key = review["document"].get("syllabusChapter") or (resolve_syllabus_key(job_dir, review, doc) if job_dir else None)
    return list(syllabus().get(key, {}).get("topics", [])) if key else []


def resolve_syllabus_key(job_dir, review, doc=None):
    name = review["document"].get("chapter") or ""
    try:
        name = json.loads((Path(job_dir) / "meta.json").read_text(encoding="utf-8")).get("filename") or name
    except (OSError, json.JSONDecodeError):
        pass
    if not name and doc:
        name = doc.get("chapter") or ""
    return guess_syllabus_key(name, review["document"].get("subject"), detect_class(job_dir))


TOPIC_PROMPT = (
    "You are tagging questions from a chapter of an Indian school textbook ({cls_subject}).\n"
    "{topic_rules}"
    "{level_rules}"
    "Questions:\n{questions}\n\n"
    'Reply with JSON only: {{"answers": {{"<id>": {{{fields}}}, ...}}}}. '
    "Use null for anything you cannot decide. Do not add any other keys."
)
TOPIC_RULES = ("For each question choose the syllabus topic it belongs to. Allowed topics (use EXACTLY one of "
               "these strings, never invent one):\n{topics}\n\n")
LEVEL_RULES = ("For each question also judge its difficulty for a student of this class:\n"
               "- \"easy\": direct recall, a one-step calculation or a definition straight from the chapter.\n"
               "- \"medium\": two or three steps, or applying a rule to a new situation - the usual textbook exercise.\n"
               "- \"hard\": long multi-step reasoning, proofs, tricky cases, or questions marked as harder.\n\n")


def fill_topics(job_dir, progress=None, redo=False, batch=12, fields=("topic", "level"), keys=None):
    """Ask the AI to tag every question with a topic (from the allowed list) and/or a difficulty level.
    keys: only these question keys, for tagging one question on its own."""
    import ai_fallback
    from openai import OpenAI

    job_dir = Path(job_dir)
    fields = [f for f in fields if f in ("topic", "level")]
    doc = ensure_current(job_dir)
    review = load_review(job_dir)
    topics = allowed_topics(review, job_dir, doc) if "topic" in fields else []
    if "topic" in fields and not topics:
        raise ValueError("no topic list for this chapter - pick a syllabus chapter on the Document tab first")
    if topics and not review["document"].get("syllabusChapter"):  # remember what we worked out
        key = resolve_syllabus_key(job_dir, review, doc)
        if key:
            review["document"]["syllabusChapter"] = key
            save_review(job_dir, review)

    items = []
    only = set(keys) if keys else None
    for key, ex, q in questions(doc):
        if only is not None and key not in only:
            continue
        m = review["questions"].get(key, {})
        if all(m.get(f) for f in fields) and not redo:
            continue
        text = (m.get("stemOverride") if m.get("stemOverride") is not None else _neutral(q["question"]))
        text = re.sub(r"!\[\]\([^)]*\)", " [image] ", text)
        items.append((key, re.sub(r"\s+", " ", text)[:400]))
    if not items:
        return {"total": 0, "tagged": 0, "unplaced": 0, "fields": fields, "values": {}}

    cls_subject = " ".join(filter(None, [(review["document"].get("syllabusChapter") or "").rsplit("/", 1)[0],
                                         review["document"].get("chapter")])) or "school chapter"
    shape = ", ".join(f'"{f}": ...' for f in fields)
    client = OpenAI(timeout=120, max_retries=3)
    tagged, unplaced, done = {}, 0, 0
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        prompt = TOPIC_PROMPT.format(
            cls_subject=cls_subject, fields=shape,
            topic_rules=TOPIC_RULES.format(topics="\n".join(f"- {t}" for t in topics)) if "topic" in fields else "",
            level_rules=LEVEL_RULES if "level" in fields else "",
            questions="\n".join(f"{k}: {t}" for k, t in chunk))
        try:
            r = client.chat.completions.create(model=ai_fallback.model_name(), max_completion_tokens=3000,
                                               response_format={"type": "json_object"},
                                               messages=[{"role": "user", "content": prompt}])
            ai_fallback.note_usage(r, "topics and levels")
            answers = (json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", r.choices[0].message.content.strip()))
                       .get("answers") or {})
        except Exception:
            answers = {}
        for k, _ in chunk:
            a = answers.get(k)
            a = a if isinstance(a, dict) else {fields[0]: a}      # tolerate a bare value
            got = {}
            if "topic" in fields and isinstance(a.get("topic"), str) and a["topic"] in topics:
                got["topic"] = a["topic"]
            if "level" in fields and isinstance(a.get("level"), str) and a["level"].lower() in LEVELS:
                got["level"] = a["level"].lower()
            if got:
                tagged[k] = got
            if len(got) < len(fields):
                unplaced += 1
        done += len(chunk)
        if progress:
            progress("AI choosing " + " and ".join(fields), done, len(items))

    review = load_review(job_dir)
    for k, got in tagged.items():
        review["questions"].setdefault(k, {}).update(got)
    save_review(job_dir, review)
    return {"total": len(items), "tagged": len(tagged), "unplaced": unplaced, "fields": fields,
            "values": tagged}   # so a single-question call can show the answer without a reload


def _neutral(sec):
    return _as_text(sec, lambda n: f"img:{n}")


def _plain(s):
    """Text stripped down for comparing an AI line with the original line it came from."""
    s = re.sub(r"!\[\]\([^)]*\)|\\\(.*?\\\)|\\\[.*?\\\]|\$[^$]*\$|\[\[FIGURE\]\]", " ", s or "", flags=re.S)
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def place_figures(ai_text, original_text, figures):
    """Put the figure images back where they were in the PDF: under the same piece of text they followed
    there (matched by words), at the end if nothing followed them, at the top if nothing preceded them.
    The AI's own [[FIGURE]] markers are only a fallback for figures whose original spot is unknown."""
    from difflib import SequenceMatcher

    text = (ai_text or "").strip()
    if not figures:
        return re.sub(r"\[\[FIGURE\]\]\s*", "", text).strip()
    marker_lines = [i for i, l in enumerate(text.split("\n")) if "[[FIGURE]]" in l]
    text = re.sub(r"\[\[FIGURE\]\]\s*", "", text)
    out = [l for l in text.split("\n")]

    # how each figure sat in the original: the text just before it, how far down it was, and whether
    # any text came after it (text on the same line, before the image, counts as "before")
    seq = []
    for line in original_text.split("\n"):
        pos = 0
        for m in IMG_REF.finditer(line):
            before = _plain(line[pos:m.start()])
            if before:
                seq.append(("text", before))
            seq.append(("img", m.group(1)))
            pos = m.end()
        tail = _plain(line[pos:])
        if tail:
            seq.append(("text", tail))
    total_text = sum(1 for kind, _ in seq if kind == "text")
    where, seen_text, last_line = {}, 0, None
    for i, (kind, val) in enumerate(seq):
        if kind == "text":
            last_line, seen_text = val, seen_text + 1
        else:
            where[val] = {"anchor": last_line, "after": seen_text,
                          "trailing": not any(k == "text" for k, _ in seq[i + 1:])}

    for k, name in enumerate(figures):
        w = where.get(name)
        if not w:                               # unknown in the original: use the AI's own marker if it gave one
            at = marker_lines[k] if k < len(marker_lines) else len(out)
        elif w.get("trailing") or not out:      # nothing followed it in the PDF: keep it at the end
            at = len(out)
        elif not w.get("anchor"):               # it came before any text
            at = 0
        else:
            best, score = None, 0.35
            for i, l in enumerate(out):
                p = _plain(l)
                if p:
                    r = SequenceMatcher(None, w["anchor"], p).ratio()
                    if r > score:
                        best, score = i, r
            # no line matched (the original text can be garbled): use the same relative position
            at = best + 1 if best is not None else round(len(out) * (w["after"] / max(total_text, 1)))
        out.insert(min(at, len(out)), f"![](img:{name})")
    return "\n".join(l for l in out if l.strip()).strip()


def find_question(doc, key):
    for k, ex, q in questions(doc):
        if k == key:
            return ex, q
    raise KeyError(key)


_PDF_LOCK = threading.Lock()  # pdfium is not thread-safe; several readers at once corrupt the document


def region_png(pdf_path, regions, dpi=200, pad=6):
    """Render a question's highlighted box(es) from the PDF; boxes on several pages are stacked."""
    import io
    import pypdfium2 as pdfium
    from PIL import Image

    scale = dpi / 72
    parts = []
    with _PDF_LOCK:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            for r in regions:
                page = doc[r["page"] - 1]
                img = page.render(scale=scale).to_pil().convert("RGB")
                x0, y0, x1, y1 = r["bbox"]
                box = (max(0, int((x0 - pad) * scale)), max(0, int((y0 - pad) * scale)),
                       min(img.width, int((x1 + pad) * scale)), min(img.height, int((y1 + pad) * scale)))
                parts.append(img.crop(box))
        finally:
            doc.close()
    if not parts:
        raise ValueError("this question has no page region")
    w = max(p.width for p in parts)
    out = Image.new("RGB", (w, sum(p.height for p in parts) + 20 * (len(parts) - 1)), "white")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height + 20
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def _reread_one(job_dir, doc, key, m):
    """AI-read one question's box. Pure: returns the new override fields, saves nothing."""
    import ai_fallback

    _, q = find_question(doc, key)
    info = {**q["question"].get("equations_latex", {}), **q["solution"].get("equations_latex", {})}
    figures = [n for n in q["question"].get("equations", []) + q["solution"].get("equations", [])
               if info.get(n, {}).get("source") == "figure"]
    figures = list(dict.fromkeys(figures))
    if m.get("stemOverride") is not None or m.get("solutionOverride") is not None:
        # respect earlier edits: an image you removed from this question stays removed
        still = set(IMG_REF.findall((m.get("stemOverride") if m.get("stemOverride") is not None else _neutral(q["question"])) + "\n" +
                                    (m.get("solutionOverride") if m.get("solutionOverride") is not None else _neutral(q["solution"]))))
        figures = [f for f in figures if f in still]
    png = region_png(job_dir / "input.pdf", q.get("regions", []))
    res = ai_fallback.transcribe_region(png, len(figures))

    q_figs = [n for n in dict.fromkeys(q["question"].get("equations", [])) if n in figures]
    s_figs = [n for n in figures if n not in q_figs]
    stem = to_paren_delims(place_figures(res["question"], _neutral(q["question"]), q_figs))
    sol = to_paren_delims(place_figures(res["solution"], _neutral(q["solution"]), s_figs))

    return {"stemOverride": stem, "solutionOverride": sol, "aiReread": True, "katexErrors": res["katexErrors"]}


def crop_region(job_dir, key, part, page, bbox_norm):
    """Cut a rectangle you dragged on the PDF into a new image and add it to this question.
    part: "stem" (question) or "sol" (answer/solution). bbox_norm: [x0, y0, x1, y1] as 0-1 of the page."""
    job_dir = Path(job_dir)
    doc = ensure_current(job_dir)
    _, q = find_question(doc, key)
    sizes = {int(k): v for k, v in doc.get("page_sizes", {}).items()}
    if page not in sizes:
        raise ValueError(f"page {page} is not in this PDF")
    w, h = sizes[page]
    x0, y0, x1, y1 = bbox_norm
    box = [round(min(x0, x1) * w, 1), round(min(y0, y1) * h, 1), round(max(x0, x1) * w, 1), round(max(y0, y1) * h, 1)]
    if box[2] - box[0] < 5 or box[3] - box[1] < 5:
        raise ValueError("that selection is too small")

    review = load_review(job_dir)
    n = 1 + len(review["manualImages"])
    name = f"manual_{n:03d}.png"
    while (job_dir / "out" / "images" / name).exists():
        n += 1
        name = f"manual_{n:03d}.png"
    png = region_png(job_dir / "input.pdf", [{"page": page, "bbox": box}], pad=0)
    (job_dir / "out" / "images" / name).write_bytes(png)

    review["manualImages"][name] = {"page": page, "bbox": box}
    m = review["questions"].setdefault(key, {})
    field = "stemOverride" if part == "stem" else "solutionOverride"
    current = m.get(field)
    if current is None:
        current = _neutral(q["question"] if part == "stem" else q["solution"])
    m[field] = (current.rstrip() + f"\n![](img:{name})").strip()
    save_review(job_dir, review)
    return {"name": name, "field": field, "text": m[field]}


def extract_region(job_dir, key, part, page, bbox_norm):
    """Read a selected PDF box into one existing question part, keeping the other part intact."""
    import math
    import ai_fallback

    if part not in ("stem", "sol"):
        raise ValueError("choose question or answer")
    if len(bbox_norm) != 4 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in bbox_norm):
        raise ValueError("selection must be four coordinates within the page")
    job_dir = Path(job_dir)
    doc = ensure_current(job_dir)
    _, q = find_question(doc, key)
    sizes = {int(k): v for k, v in doc.get("page_sizes", {}).items()}
    if page not in sizes:
        raise ValueError(f"page {page} is not in this PDF")
    w, h = sizes[page]
    x0, y0, x1, y1 = bbox_norm
    box = [min(x0, x1) * w, min(y0, y1) * h, max(x0, x1) * w, max(y0, y1) * h]
    if box[2] - box[0] < 5 or box[3] - box[1] < 5:
        raise ValueError("that selection is too small to read")
    saved = load_review(job_dir)
    field = "stemOverride" if part == "stem" else "solutionOverride"
    original = saved["questions"].get(key, {}).get(field)
    if original is None:
        original = _neutral(q["question" if part == "stem" else "solution"])
    info = {**q["question"].get("equations_latex", {}), **q["solution"].get("equations_latex", {})}
    candidates = list(dict.fromkeys(q["question"].get("equations", []) + q["solution"].get("equations", []) + IMG_REF.findall(original)))
    boxes = {**doc.get("image_boxes", {}), **saved["manualImages"]}
    figures = []
    for name in candidates:
        b = boxes.get(name)
        if not b or b["page"] != page or (info.get(name, {}).get("source") != "figure" and name not in saved["manualImages"]):
            continue
        bx0, by0, bx1, by1 = b["bbox"]
        if box[0] <= (bx0 + bx1) / 2 <= box[2] and box[1] <= (by0 + by1) / 2 <= box[3]:
            figures.append(name)
    result = ai_fallback.transcribe_region(region_png(job_dir / "input.pdf", [{"page": page, "bbox": box}], pad=0), len(figures), part=part)
    text = "\n\n".join(t.strip() for t in (result["question"], result["solution"]) if t.strip())
    if not text:
        raise ValueError("AI found no readable text in that selection; the question was left unchanged")
    missing_figures = max(0, text.count("[[FIGURE]]") - len(figures))
    text = to_paren_delims(place_figures(text, original, figures))
    if not text.strip():
        raise ValueError("AI found no readable text in that selection; the question was left unchanged")
    saved = load_review(job_dir)
    manual = saved["questions"].setdefault(key, {})
    manual[field] = text
    save_review(job_dir, saved)
    return {"field": field, "text": manual[field], "katexErrors": result["katexErrors"], "missingFigures": missing_figures}


def _slot_for(doc, page):
    """Which exercise a question cropped from this page belongs to, and the next free number in it."""
    on_page, numbers = {}, {}
    for _, ex, q in questions(doc):
        title = ex["title"]
        numbers.setdefault(title, []).append(_to_int(q.get("number")) or 0)
        if any(r.get("page") == page for r in q.get("regions", [])):
            on_page[title] = on_page.get(title, 0) + 1
    if on_page:
        title = max(on_page, key=on_page.get)          # the exercise that owns most of this page
    elif doc.get("exercises"):
        title = doc["exercises"][-1]["title"]
    else:
        title = "UNKNOWN"
    return title, max(numbers.get(title, [0])) + 1


def add_question(job_dir, page, bbox_norm, use_ai=True):
    """Draw a box round a question the splitter missed: crop it, read it with AI, keep it as a question.

    It lives in review.json, not structured.json, so re-splitting the PDF never drops or moves it.
    bbox_norm is [x0, y0, x1, y1] as 0-1 of the page, like crop_region."""
    import ai_fallback

    job_dir = Path(job_dir)
    doc = ensure_current(job_dir)
    sizes = {int(k): v for k, v in doc.get("page_sizes", {}).items()}
    if page not in sizes:
        raise ValueError(f"page {page} is not in this PDF")
    w, h = sizes[page]
    x0, y0, x1, y1 = bbox_norm
    box = [round(min(x0, x1) * w, 1), round(min(y0, y1) * h, 1),
           round(max(x0, x1) * w, 1), round(max(y0, y1) * h, 1)]
    if box[2] - box[0] < 20 or box[3] - box[1] < 20:
        raise ValueError("that box is too small to hold a question")

    stem = solution = ""
    katex_errors, figures_seen = [], 0
    if use_ai:
        res = ai_fallback.transcribe_region(region_png(job_dir / "input.pdf", [{"page": page, "bbox": box}]), 0)
        stem, solution = res["question"].strip(), res["solution"].strip()
        katex_errors = res["katexErrors"]
        # there are no extracted images behind a box you drew, so a [[FIGURE]] marker has nothing to
        # point at: drop it and say so, and the crop buttons can add the picture afterwards
        figures_seen = (stem + solution).count("[[FIGURE]]")
        stem = to_paren_delims(place_figures(stem, "", []))
        solution = to_paren_delims(place_figures(solution, "", []))

    review = load_review(job_dir)
    exercise, number = _slot_for(doc, page)
    entry = {"id": object_id()[:12], "page": page, "bbox": box, "exercise": exercise,
             "number": number, "label": str(number), "stem": stem, "solution": solution,
             "figuresSeen": figures_seen,
             "addedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    review["addedQuestions"].append(entry)
    save_review(job_dir, review)
    return {"key": f"add-{entry['id']}", "stem": stem, "solution": solution,
            "exercise": exercise, "number": number, "katexErrors": katex_errors}


def remove_added_question(job_dir, key):
    """Drop a question you added by hand, with any edits made to it. Only works on added ones."""
    job_dir = Path(job_dir)
    review = load_review(job_dir)
    added_id = key[4:] if key.startswith("add-") else None
    kept = [a for a in review["addedQuestions"] if a["id"] != added_id]
    if len(kept) == len(review["addedQuestions"]):
        raise KeyError(key)
    review["addedQuestions"] = kept
    review["questions"].pop(key, None)
    review["ids"].pop(key, None)
    save_review(job_dir, review)
    return {"removed": key}


def ai_reread(job_dir, key):
    """AI-read one question's box and save it as that question's edited text."""
    job_dir = Path(job_dir)
    doc = ensure_current(job_dir)
    review = load_review(job_dir)
    out = _reread_one(job_dir, doc, key, review["questions"].get(key, {}))
    review = load_review(job_dir)  # re-load: the AI call took a while and other edits may have been saved
    review["questions"].setdefault(key, {}).update({k: v for k, v in out.items() if k != "katexErrors"})
    save_review(job_dir, review)
    return out


def ai_reread_all(job_dir, progress=None, workers=4, redo_edited=False):
    """AI-read every question of a job. Questions you already edited are left alone unless redo_edited."""
    from concurrent.futures import ThreadPoolExecutor

    job_dir = Path(job_dir)
    doc = ensure_current(job_dir)
    review = load_review(job_dir)
    todo = []
    for key, _, q in questions(doc):
        m = review["questions"].get(key, {})
        edited = m.get("stemOverride") is not None or m.get("solutionOverride") is not None
        if q.get("regions") and (redo_edited or not edited or m.get("aiReread")):
            todo.append(key)
    done, failed, lock = [0], {}, threading.Lock()
    results = {}

    def work(key):
        try:
            results[key] = _reread_one(job_dir, doc, key, review["questions"].get(key, {}))
        except Exception as e:
            failed[key] = f"{type(e).__name__}: {e}"[:200]
        with lock:
            done[0] += 1
            if progress:
                progress("AI reading questions", done[0], len(todo))

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, todo))

    review = load_review(job_dir)
    for key, out in results.items():
        review["questions"].setdefault(key, {}).update({k: v for k, v in out.items() if k != "katexErrors"})
    save_review(job_dir, review)
    return {"total": len(todo), "done": len(results), "failed": failed,
            "withKatexWarnings": sum(1 for r in results.values() if r["katexErrors"])}


def has_scanned_pages(doc):
    return any("_ocr" in n for n in doc.get("image_boxes", {}))


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def missing_fields(record):
    miss = [f for f in REQUIRED if f not in ("module", "chapter") and record.get(f) in (None, "")]
    miss += [f for f in ("module", "chapter") if not record["path"].get(f)]
    return miss


def export(job_dir):
    doc = ensure_current(job_dir)
    review = load_review(job_dir)
    review["document"].setdefault("documentId", object_id())
    base = review["document"].get("imageBaseUrl") or "images/"
    image_url = lambda name: base.rstrip("/") + "/" + name
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    px_size = image_sizes(job_dir)
    records = [build_record(doc, key, ex, q, review, image_url, now, px_size)
               for key, ex, q in questions(doc) if not review["questions"].get(key, {}).get("skip")]
    save_review(job_dir, review)  # keeps generated ids stable across exports
    return records


BUNDLES = Path(__file__).resolve().parent / "exports"


def bundle_name(pdf_name, job_id):
    """Chapter 15 - Probability.pdf -> Probability. Falls back to the job id."""
    stem = re.sub(r"^Chapter\s*\d+\s*-\s*", "", Path(pdf_name or "").stem).strip()
    return re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-") or job_id


def write_bundle(job_dir, out_root=None, name=None):
    """A chapter's questions.json and its images in one folder, ready for tools/push_mongo.py.

    Keeping the JSON and the pictures together is what lets the push put them in the same place
    without pairing a loose file with a zip by hand."""
    job_dir = Path(job_dir)
    records = export(job_dir)
    folder = Path(out_root or BUNDLES) / (name or bundle_name(None, job_dir.name))
    (folder / "images").mkdir(parents=True, exist_ok=True)
    (folder / "questions.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    wanted = sorted({Path(c["url"]).name for r in records for c in (r.get("imageCrops") or [])})
    src = job_dir / "out" / "images"
    copied, missing = 0, []
    for f in wanted:
        if (src / f).exists():
            (folder / "images" / f).write_bytes((src / f).read_bytes())
            copied += 1
        else:
            missing.append(f)
    for stale in (folder / "images").iterdir():   # images dropped from the chapter since last time
        if stale.name not in wanted:
            stale.unlink()
    return {"folder": str(folder), "name": folder.name, "questions": len(records),
            "images": copied, "missing": missing}
