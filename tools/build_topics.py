"""Build a syllabus topic list per chapter from the official NCERT textbooks (ncert.nic.in).

Each chapter PDF prints numbered section headings ("1.2 The Fundamental Theorem of Arithmetic");
those are the topics. Chapters are then matched to the solution PDFs in Chapters/ by title.

    python tools/build_topics.py            # download (cached), extract, write topics.json
    python tools/build_topics.py --report   # just show coverage against Chapters/
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

import pdfplumber
import truststore

truststore.inject_into_ssl()

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "downloads" / "ncert"
OUT = BASE / "topics.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
BOOKS = {  # code -> (class folder, subject folder, how many chapters the current edition has)
    "jemh": ("Class 10", "Maths", 14),
    "jesc": ("Class 10", "Science", 13),
    "iemh": ("Class 9", "Maths", 8),
    "iesc": ("Class 9", "Science", 13),
}
SKIP = {"introduction", "summary", "exercise", "exercises", "activity", "questions", "what you have learnt"}
RE_SECTION = re.compile(r"^\s*(\d+\.\d+)\s+([A-Z][A-Za-z0-9 ,’'\-\(\)/&]{3,70})\s*$", re.M)
RE_SUBSECTION = re.compile(r"^\s*(\d+\.\d+\.\d+)\s+([A-Z][A-Za-z0-9 ,’'\-\(\)/&]{3,70})\s*$", re.M)


def fetch(code, n):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{code}1{n:02d}.pdf"
    if not path.exists() or path.stat().st_size < 1000:
        req = urllib.request.Request(f"https://ncert.nic.in/textbook/pdf/{code}1{n:02d}.pdf", headers={"User-Agent": UA})
        path.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    return path


def chapter_info(path):
    """Title = the biggest text on page 1 (maths chapters lose their decorative first letter, which is fine
    for matching). Topics = numbered section headings; science books number them 1.2.3."""
    import collections

    with pdfplumber.open(str(path)) as pdf:
        page = pdf.pages[0]
        rows = collections.defaultdict(list)
        for ch in page.chars:
            rows[(round(ch["top"] / 4), round(ch["size"], 1))].append(ch)
        lines = []
        for (row, size), chars in rows.items():
            txt = "".join(c["text"] for c in sorted(chars, key=lambda c: c["x0"])).strip()
            if len(txt) > 2 and not txt.upper().startswith("CHAPTER") and not txt.isdigit():
                lines.append((size, row, txt))
        lines.sort(reverse=True)
        title = None
        if lines:
            big = lines[0][0]
            title = " ".join(t for _, _, t in sorted([l for l in lines if l[0] >= big * 0.85], key=lambda l: l[1]))
            title = re.sub(r"\s+\d+$", "", title).strip().title()
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    topics, seen = [], set()
    found = RE_SECTION.findall(text) or RE_SUBSECTION.findall(text)
    for num, name in found:
        name = re.sub(r"\s+", " ", name).strip(" .")
        key = name.lower()
        if key in SKIP or key in seen or len(name) < 4:
            continue
        seen.add(key)
        topics.append({"number": num, "name": name})
    return title, topics


def solution_chapters():
    out = {}
    root = BASE / "Chapters"
    for pdf in sorted(root.rglob("*.pdf")):
        cls, subj = pdf.parent.parent.name, pdf.parent.name
        name = re.sub(r"^Chapter \d+ - ", "", pdf.stem)
        out.setdefault((cls, subj), []).append(name)
    return out


def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(and|the|of|in|to|a|an|its|our)\b", " ", s)
    return " ".join(s.split())


def _same_word(a, b):
    # maths chapters drop their decorative first letter: "olynomials" == "polynomials"
    return a == b or (len(a) > 3 and len(b) > 3 and (a.endswith(b) or b.endswith(a)) and abs(len(a) - len(b)) <= 2)


def match(title, candidates):
    t = norm(title).split()
    best, score = None, 0
    for c in candidates:
        w = norm(c).split()
        if not w:
            continue
        hits = sum(any(_same_word(x, y) for y in w) for x in t)
        s = hits / max(len(set(t) | set(w)), 1)
        if s > score:
            best, score = c, s
    return (best, round(score, 2)) if score >= 0.5 else (None, round(score, 2))


SUGGEST_PROMPT = (
    "List the sections (topics) of the NCERT/CBSE chapter \"{chapter}\" in {cls} {subject} (Indian school "
    "curriculum, the full pre-2023 syllabus as taught with the usual NCERT textbook sections).\n"
    "Use the wording of the textbook's own numbered section headings, e.g. for Class 10 Maths Real Numbers: "
    "\"Euclid's Division Lemma\", \"The Fundamental Theorem of Arithmetic\", \"Revisiting Irrational Numbers\".\n"
    "Return JSON only: {{\"topics\": [\"...\", \"...\"]}} with 3 to 10 topics, most general first. "
    "No introduction, summary or exercise entries."
)


def suggest_topics(chapter, cls, subject):
    from openai import OpenAI
    import ai_fallback

    r = OpenAI(timeout=90, max_retries=3).chat.completions.create(
        model=ai_fallback.model_name(), response_format={"type": "json_object"}, max_completion_tokens=1200,
        messages=[{"role": "user", "content": SUGGEST_PROMPT.format(chapter=chapter, cls=cls, subject=subject)}])
    data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", r.choices[0].message.content.strip()))
    return [str(t).strip() for t in (data.get("topics") or []) if str(t).strip()][:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--suggest", action="store_true",
                    help="for chapters missing from the current NCERT edition, ask the AI for the syllabus topics")
    args = ap.parse_args()
    if args.suggest:  # work from the existing topics.json; do not re-read the textbooks
        sys.path.insert(0, str(BASE))
        data = json.loads(OUT.read_text(encoding="utf-8"))
        todo = [k for k, v in data.items() if not v["topics"]]
        print(f"asking the AI for topics of {len(todo)} chapter(s)")
        for k in todo:
            cls, subject, chapter = k.split("/")
            try:
                data[k] = {"topics": suggest_topics(chapter, cls, subject), "source": "AI suggested - please check"}
                print(f"  {k}: {len(data[k]['topics'])} topics")
            except Exception as e:
                print(f"  {k}: {type(e).__name__}: {e}")
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        return

    ncert = {}
    for code, (cls, subj, count) in BOOKS.items():
        for n in range(1, count + 1):
            try:
                title, topics = chapter_info(fetch(code, n))
            except Exception as e:
                print(f"  {code}{n:02d}: {type(e).__name__}: {e}")
                continue
            ncert.setdefault((cls, subj), []).append({"ncertChapter": n, "title": title, "topics": topics})
            print(f"  {cls} {subj} ch{n:02d}: {title} -> {len(topics)} topics")

    data, missing = {}, []
    for (cls, subj), chapters in solution_chapters().items():
        book = ncert.get((cls, subj), [])
        for name in chapters:
            hit, score = match(name, [c["title"] for c in book])
            entry = {"topics": [], "source": None}
            if hit:
                c = next(c for c in book if c["title"] == hit)
                entry = {"topics": [t["name"] for t in c["topics"]], "source": f"NCERT {cls} {subj} ch{c['ncertChapter']}"}
            else:
                missing.append(f"{cls}/{subj}/{name}")
            data[f"{cls}/{subj}/{name}"] = entry

    if not args.report:
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    have = sum(1 for v in data.values() if v["topics"])
    print(f"\n{have} of {len(data)} chapters have official NCERT topics; {len(missing)} not in the current edition:")
    for m in missing:
        print("   ", m)


if __name__ == "__main__":
    main()
