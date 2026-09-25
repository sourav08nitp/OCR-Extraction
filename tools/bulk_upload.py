"""Upload many chapter PDFs to the running app at once; they queue up and appear in the web UI.

The app must be running (python app.py). Jobs are processed one at a time, in order.

    python tools/bulk_upload.py Chapters                        # everything not uploaded yet
    python tools/bulk_upload.py "Chapters/Class 10/Science"      # one folder
    python tools/bulk_upload.py Chapters --wait --tag --export   # also tag topics/levels and save the JSON
    python tools/bulk_upload.py Chapters --wait --tag --reread --export   # + AI re-read of every question

Options:
  --no-ai        do not use the OpenAI fallback during extraction
  --wait         wait for each PDF to finish before starting the next step
  --tag          after extraction, fill topics and levels with AI (needs --wait)
  --reread       after extraction, AI re-reads every question - better for chemistry and scanned PDFs
  --export       save each finished chapter's JSON into exports/
  --force        upload again even if a PDF with the same name is already there
  --module NAME  what to put in path.module (default: NCERT)
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = "http://127.0.0.1:5000"
EXPORTS = BASE / "exports"


def api(path, method="GET", data=None, as_json=True, timeout=120):
    req = urllib.request.Request(APP + path, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if as_json else body


def upload(pdf):
    """Multipart POST without extra libraries."""
    boundary = "----boards" + str(int(time.time() * 1000))
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; filename="{pdf.name}"\r\n'
             f"Content-Type: application/pdf\r\n\r\n".encode() + pdf.read_bytes() + b"\r\n"]
    for field, value in (("latex", "1"), ("ai", "1" if USE_AI else "0")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(APP + "/api/upload", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["job_id"]


def wait_for(job_id, label):
    last = ""
    while True:
        s = api(f"/api/jobs/{job_id}")
        if s["status"] in ("done", "error"):
            return s
        line = f"   {label}: {s.get('stage','')}" + (f" {s['done']}/{s['total']}" if s.get("total") else "")
        if line != last:
            print(line.ljust(len(last)), end="\r", flush=True)
            last = line
        time.sleep(3)


def describe(pdf):
    """Class 10/Science/Chapter 01 - Life Processes.pdf -> class, subject and chapter for the Document tab."""
    subject = pdf.parent.name
    chapter = re.sub(r"^Chapter\s*\d+\s*-\s*", "", pdf.stem).strip()
    klass = pdf.parent.parent.name if re.match(r"(?i)class\s*\d+$", pdf.parent.parent.name) else None
    return klass, subject, chapter


def syllabus_key(klass, subject, chapter, keys):
    """Exact topics.json key from the folder path. Both classes have "Chapter 14 - Statistics.pdf",
    and the file name alone cannot tell them apart - the folder can."""
    if not klass:
        return None
    want = f"{klass}/{subject}/{chapter}".lower()
    return next((k for k in keys if k.lower() == want), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of PDFs (searched recursively)")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--tag", action="store_true")
    ap.add_argument("--reread", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--module", default="NCERT")
    ap.add_argument("--limit", type=int, default=0, help="only do this many PDFs this run")
    args = ap.parse_args()

    global USE_AI
    USE_AI = not args.no_ai
    needs_wait = args.tag or args.reread or args.export
    if needs_wait:
        args.wait = True

    try:
        existing = {j["filename"] for j in api("/api/jobs")["jobs"]}
    except urllib.error.HTTPError as e:
        sys.exit(f"The app answered {e.code} for /api/jobs - it is running an older version. Restart it: python app.py")
    except urllib.error.URLError:
        sys.exit(f"Cannot reach the app at {APP} - start it first with:  python app.py")

    pdfs = sorted(p for p in Path(args.folder).rglob("*.pdf"))
    todo = [p for p in pdfs if args.force or p.name not in existing]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(pdfs)} PDF(s) found, {len(pdfs) - len(todo)} already uploaded, {len(todo)} to do\n")
    if args.export:
        EXPORTS.mkdir(exist_ok=True)

    try:
        syl_keys = list(json.loads((BASE / "topics.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        syl_keys = []
    for n, pdf in enumerate(todo, 1):
        klass, subject, chapter = describe(pdf)
        print(f"[{n}/{len(todo)}] {pdf.parent.parent.name} / {subject} / {chapter}")
        try:
            job_id = upload(pdf)
        except Exception as e:
            print(f"   upload failed: {e}")
            continue
        print(f"   job {job_id}")
        if not args.wait:
            continue

        s = wait_for(job_id, "extracting")
        if s["status"] == "error":
            print(f"   FAILED: {s.get('error')}")
            continue
        q = api(f"/api/jobs/{job_id}")
        print(f"   extracted{'' if q.get('total') is None else ''}")

        # fill in what the Document tab would ask for
        doc = {"module": args.module, "subject": subject, "chapter": chapter, "section": "All sections"}
        key = syllabus_key(klass, subject, chapter, syl_keys)
        if key:
            doc["syllabusChapter"] = key      # exact, so --tag never picks the other class's topic list
            print(f"   syllabus: {key}")
        elif klass:
            print(f"   no topic list for {klass}/{subject}/{chapter} - pick one on the Document tab before tagging")
        review = {"document": doc, "questions": {}}
        try:
            api(f"/api/jobs/{job_id}/review", "PUT", review)
        except Exception as e:
            print(f"   could not set the document fields: {e}")

        if args.reread:
            api(f"/api/jobs/{job_id}/reread-all", "POST", {})
            r = wait_for(job_id, "AI re-reading")
            print(f"   re-read: {(r.get('result') or {}).get('done', 0)} question(s)")
        if args.tag:
            api(f"/api/jobs/{job_id}/fill-topics", "POST", {})
            r = wait_for(job_id, "topics and levels")
            res = r.get("result") or {}
            print(f"   tagged: {res.get('tagged', 0)} question(s), {res.get('unplaced', 0)} incomplete")
        if args.export:
            name = f"{pdf.parent.parent.name} - {subject} - {chapter}".replace(" ", "-")
            data = api(f"/api/jobs/{job_id}/export", as_json=False, timeout=300)
            (EXPORTS / f"{name}.json").write_bytes(data)
            print(f"   saved exports/{name}.json ({len(json.loads(data))} questions)")

    print("\nOpen http://127.0.0.1:5000 to see them all.")


if __name__ == "__main__":
    main()
