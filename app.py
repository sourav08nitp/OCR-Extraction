"""Local web UI: upload a solutions PDF, run pdf_to_structured, view the result with rendered LaTeX.

    python app.py      ->  http://127.0.0.1:5000
"""

import io
import json
import os
import queue
import shutil
import sys
import zipfile
import threading
import traceback
import uuid
import warnings
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import ai_fallback
import pdf_to_structured
import review
import settings

warnings.filterwarnings("ignore")

_from_env_file = settings.load()   # .env in the project root; real environment variables win

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

jobs = {}              # job_id -> status dict
work = queue.Queue()   # one worker: pix2tex is heavy and the model is shared
_model = None


def _get_model(job):
    global _model
    if _model is None:
        job.update(stage="loading LaTeX model", done=0, total=0)
        _model = pdf_to_structured.load_latex_model()
    return _model


def _worker():
    while True:
        job_id = work.get()
        job = jobs[job_id]
        job["status"] = "running"
        job_dir = JOBS_DIR / job_id

        def progress(stage, done, total):
            job.update(stage=stage, done=done, total=total)

        try:
            if job.get("kind") == "ai-fix":
                _ai_fix_existing(job_dir / "out", progress)
            elif job.get("kind") == "reread-all":
                job["result"] = review.ai_reread_all(job_dir, progress, redo_edited=job.get("redo", False))
            elif job.get("kind") == "fill-topics":
                job["result"] = review.fill_topics(job_dir, progress, redo=job.get("redo", False),
                                                   fields=job.get("fields", ("topic", "level")))
            else:
                model = _get_model(job) if job["latex"] else None
                doc = pdf_to_structured.run(job_dir / "input.pdf", job_dir / "out", want_latex=job["latex"],
                                            progress=progress, model=model, use_ai=False)
                if job.get("ai"):
                    if review.has_scanned_pages(doc):
                        # scanned pages: one AI call per question reads better than one per formula, and costs ~10x less
                        job["result"] = review.ai_reread_all(job_dir, progress)
                    else:
                        _ai_fix_existing(job_dir / "out", progress)
            job.update(status="done", stage="done")
            _note_counts(job_dir)
        except Exception as e:
            traceback.print_exc()
            job.update(status="error", error=f"{type(e).__name__}: {e}")


def _note_counts(job_dir):
    """Keep the question count in meta.json so the job list does not have to open every result."""
    try:
        doc = json.loads((job_dir / "out" / "structured.json").read_text(encoding="utf-8"))
        meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
        meta["questions"] = sum(len(e["questions"]) for e in doc["exercises"])
        (job_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    except (OSError, json.JSONDecodeError, KeyError):
        pass


def _ai_fix_existing(out_dir, progress):
    path = out_dir / "structured.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    # results made before these checks existed: restore figures as images, re-check LaTeX rendering
    pdf_to_structured.classify_figures(doc, out_dir / "images")
    pdf_to_structured.validate_latex(doc)
    pdf_to_structured.ai_fix(doc, out_dir / "images", progress)
    pdf_to_structured.build_text_latex(doc)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "preview.md").write_text(pdf_to_structured.to_markdown(doc), encoding="utf-8")


threading.Thread(target=_worker, daemon=True).start()


def _job_or_404(job_id):
    if job_id not in jobs:
        # finished jobs from an earlier server run are still on disk
        if not job_id.isalnum() or not (JOBS_DIR / job_id / "out" / "structured.json").is_file():
            abort(404)
        meta = {}
        try:
            meta = json.loads((JOBS_DIR / job_id / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        jobs[job_id] = {"id": job_id, "filename": meta.get("filename", "input.pdf"), "latex": meta.get("latex"),
                        "ai": meta.get("ai"), "status": "done", "stage": "done", "done": 0, "total": 0, "error": None}
    return jobs[job_id]


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/upload")
def upload():
    f = request.files.get("pdf")
    if not f or not f.filename:
        return jsonify(error="No file uploaded"), 400
    head = f.stream.read(5)
    f.stream.seek(0)
    if head != b"%PDF-":
        return jsonify(error="That file is not a PDF"), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir()
    f.save(job_dir / "input.pdf")
    want_ai = request.form.get("ai") == "1" and ai_fallback.available()
    (job_dir / "meta.json").write_text(json.dumps(  # so the name survives a server restart
        {"filename": Path(f.filename).name, "latex": request.form.get("latex") == "1" or want_ai, "ai": want_ai}),
        encoding="utf-8")
    jobs[job_id] = {
        "id": job_id, "filename": Path(f.filename).name,
        "latex": request.form.get("latex") == "1" or want_ai, "ai": want_ai,
        "status": "queued", "stage": "waiting in queue", "done": 0, "total": 0, "error": None,
    }
    work.put(job_id)
    return jsonify(job_id=job_id)


@app.get("/api/jobs")
def list_jobs():
    """Everything uploaded so far, newest first, for the list on the home page."""
    out = []
    for d in JOBS_DIR.iterdir():
        if not d.is_dir() or not (d / "input.pdf").exists():
            continue
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        live = jobs.get(d.name, {})
        has_result = (d / "out" / "structured.json").is_file()
        out.append({
            "id": d.name,
            "filename": live.get("filename") or meta.get("filename") or "input.pdf",
            "status": live.get("status") or ("done" if has_result else "unknown"),
            "stage": live.get("stage"), "done": live.get("done", 0), "total": live.get("total", 0),
            "error": live.get("error"),
            "questions": meta.get("questions"),
            "hasResult": has_result,
            "uploadedAt": (d / "input.pdf").stat().st_mtime,
        })
    out.sort(key=lambda j: j["uploadedAt"], reverse=True)
    return jsonify(jobs=out)


@app.delete("/api/jobs/<job_id>")
def job_delete(job_id):
    """Delete an uploaded PDF and everything extracted from it. Cannot be undone.

    A job still in the queue or being worked on is refused: the worker holds the folder open, and
    half-deleting it underneath would leave a broken result behind."""
    d = JOBS_DIR / job_id
    if not d.is_dir():
        abort(404)
    status = jobs.get(job_id, {}).get("status")
    if status in ("queued", "running"):
        return jsonify(error=f"this one is {status} - wait for it to finish, then delete it"), 409
    name = jobs.get(job_id, {}).get("filename") or job_id
    try:
        shutil.rmtree(d)
    except OSError as e:
        return jsonify(error=f"could not delete it: {e}"), 500
    jobs.pop(job_id, None)
    return jsonify(deleted=job_id, filename=name)


@app.get("/api/config")
def config():
    return jsonify(ai_available=ai_fallback.available(), ai_model=ai_fallback.model_name())


@app.post("/api/jobs/<job_id>/ai-fix")
def job_ai_fix(job_id):
    job = _job_or_404(job_id)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY is not set on the server"), 400
    if job["status"] in ("queued", "running"):
        return jsonify(error="This job is still running"), 409
    job.update(kind="ai-fix", status="queued", stage="waiting in queue", done=0, total=0, error=None)
    work.put(job_id)
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = dict(_job_or_404(job_id))
    job["queue_position"] = work.qsize() if job["status"] == "queued" else 0
    return jsonify(job)


@app.get("/api/jobs/<job_id>/result")
def job_result(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    out = JOBS_DIR / job_id / "out"
    doc = json.loads((out / "structured.json").read_text(encoding="utf-8"))
    if not doc.get("figures_checked"):
        # made before figure detection existed: diagrams/graphs may hold LaTeX or AI captions; restore them
        pdf_to_structured.classify_figures(doc, out / "images")
        pdf_to_structured.build_text_latex(doc)
        doc["figures_checked"] = True
        (out / "structured.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "preview.md").write_text(pdf_to_structured.to_markdown(doc), encoding="utf-8")
    return send_from_directory(out, "structured.json", mimetype="application/json")


@app.get("/review")
def review_page():
    return send_from_directory(app.static_folder, "review.html")


@app.get("/api/jobs/<job_id>/pdf")
def job_pdf(job_id):
    _job_or_404(job_id)
    return send_from_directory(JOBS_DIR / job_id, "input.pdf", mimetype="application/pdf")


def _review_payload(job_id):
    job_dir = JOBS_DIR / job_id
    doc = review.ensure_current(job_dir)
    saved = review.load_review(job_dir)
    url = lambda n: f"img:{n}"  # same neutral form as edited text; the page turns it into a real URL
    qs = []
    for key, ex, q in review.questions(doc):
        qs.append({"key": key, "auto": review.auto_fields(doc, ex, q, url), "manual": saved["questions"].get(key, {})})
    syl = review.syllabus()
    name = jobs.get(job_id, {}).get("filename") or doc.get("chapter") or ""
    guessed = saved["document"].get("syllabusChapter") or review.guess_syllabus_key(
        name, saved["document"].get("subject"), review.detect_class(job_dir))
    return {
        "document": saved["document"],
        "syllabus": {k: v["topics"] for k, v in syl.items()},
        "syllabusSources": {k: v.get("source") for k, v in syl.items()},
        "suggestedSyllabusChapter": guessed,
        "topicOptions": review.allowed_topics({**saved, "document": {**saved["document"],
                                                                     "syllabusChapter": guessed}}),
        "suggested": {"chapter": doc.get("chapter"), "imageBaseUrl": "images/"},
        "questions": qs,
        "pageSizes": doc.get("page_sizes", {}),
        "options": {"questionType": review.QUESTION_TYPES, "level": review.LEVELS},
        "required": review.REQUIRED,
        "aiAvailable": ai_fallback.available(),
        "aiModel": ai_fallback.model_name(),
    }


@app.get("/api/jobs/<job_id>/review")
def job_review(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    return jsonify(_review_payload(job_id))


@app.put("/api/jobs/<job_id>/review")
def job_review_save(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    body = request.get_json(silent=True) or {}
    job_dir = JOBS_DIR / job_id
    saved = review.load_review(job_dir)
    doc_in = body.get("document") or {}
    saved["document"] = {k: doc_in[k] for k in review.DOC_FIELDS if k in doc_in}
    keys = {k for k, _, _ in review.questions(review.ensure_current(job_dir))}
    saved["questions"] = {k: {f: v for f, v in (m or {}).items() if f in review.MANUAL_FIELDS}
                          for k, m in (body.get("questions") or {}).items() if k in keys}
    review.save_review(job_dir, saved)
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/reread-all")
def job_reread_all(job_id):
    job = _job_or_404(job_id)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY is not set on the server"), 400
    if job["status"] in ("queued", "running"):
        return jsonify(error="This job is still running"), 409
    job.update(kind="reread-all", redo=request.args.get("redo") == "1", status="queued",
               stage="waiting in queue", done=0, total=0, error=None, result=None)
    work.put(job_id)
    return jsonify(job_id=job_id)


@app.post("/api/jobs/<job_id>/fill-topics")
def job_fill_topics(job_id):
    job = _job_or_404(job_id)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY is not set on the server"), 400
    if job["status"] in ("queued", "running"):
        return jsonify(error="This job is still running"), 409
    fields = tuple((request.args.get("fields") or "topic,level").split(","))
    job.update(kind="fill-topics", redo=request.args.get("redo") == "1", fields=fields, status="queued",
               stage="waiting in queue", done=0, total=0, error=None, result=None)
    work.put(job_id)
    return jsonify(job_id=job_id)


@app.post("/api/jobs/<job_id>/questions/add")
def job_question_add(job_id):
    """Box drawn on the PDF -> AI reads it -> a new question the splitter had missed."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    body = request.get_json(silent=True) or {}
    use_ai = bool(body.get("ai", True))
    if use_ai and not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY is not set on the server"), 400
    try:
        out = review.add_question(JOBS_DIR / job_id, int(body.get("page", 0)),
                                  [float(v) for v in body.get("bbox", [])], use_ai)
        return jsonify(out)
    except (ValueError, TypeError, IndexError) as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 502


@app.delete("/api/jobs/<job_id>/questions/<key>")
def job_question_delete(job_id, key):
    """Remove a question added by hand. Questions found in the PDF are hidden with "skip" instead."""
    _job_or_404(job_id)
    try:
        return jsonify(review.remove_added_question(JOBS_DIR / job_id, key))
    except KeyError:
        return jsonify(error="only questions you added by hand can be deleted"), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 500


@app.post("/api/jobs/<job_id>/questions/<key>/crop")
def job_question_crop(job_id, key):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    body = request.get_json(silent=True) or {}
    try:
        out = review.crop_region(JOBS_DIR / job_id, key, body.get("part", "stem"),
                                 int(body.get("page", 0)), [float(v) for v in body.get("bbox", [])])
        return jsonify(out)
    except KeyError:
        abort(404)
    except (ValueError, TypeError, IndexError) as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 500


@app.post("/api/jobs/<job_id>/questions/<key>/topic")
def job_question_topic(job_id, key):
    """Topic and/or level for one question, instead of running the whole chapter."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY is not set on the server"), 400
    body = request.get_json(silent=True) or {}
    fields = tuple(f for f in body.get("fields", ["topic", "level"]) if f in ("topic", "level")) or ("topic", "level")
    try:
        out = review.fill_topics(JOBS_DIR / job_id, fields=fields, keys=[key], redo=True)
        return jsonify({**out, "value": out["values"].get(key, {})})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 502


@app.post("/api/jobs/<job_id>/questions/<key>/ai")
def job_question_ai(job_id, key):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY is not set on the server"), 400
    try:
        return jsonify(review.ai_reread(JOBS_DIR / job_id, key))
    except KeyError:
        abort(404)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 502


def _mongo_settings():
    """Where a push would go. The connection string stays in the environment, never in the project."""
    import supabase_store
    images = "supabase" if supabase_store.configured() else "gridfs"
    return {"available": bool(os.environ.get("MONGODB_URI")),
            "db": os.environ.get("MONGODB_DB") or "questionbank",
            "collection": os.environ.get("MONGODB_COLLECTION") or "questions",
            "imageUrl": os.environ.get("MONGODB_IMAGE_URL") or "/files/",
            "sessionId": os.environ.get("MONGODB_SESSION_ID") or None,
            "images": images,
            "bucket": supabase_store.settings()[2] if images == "supabase" else None}


@app.get("/api/mongo")
def mongo_status():
    return jsonify(_mongo_settings())


@app.post("/api/jobs/<job_id>/bundle")
def job_bundle(job_id):
    """Write exports/<chapter>/questions.json + images/ for this job."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    name = review.bundle_name(job.get("filename"), job_id)
    try:
        return jsonify(review.write_bundle(JOBS_DIR / job_id, name=name))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 500


@app.post("/api/jobs/<job_id>/push")
def job_push(job_id):
    """Bundle this chapter and push it to MongoDB. body: {write: bool, images: "gridfs"|"skip"}."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    cfg = _mongo_settings()
    if not cfg["available"]:
        return jsonify(error="MONGODB_URI is not set - add it to the .env file in the project root, then restart app.py"), 400
    body = request.get_json(silent=True) or {}
    sys.path.insert(0, str(BASE / "tools"))
    try:
        import push_mongo
    except ImportError as e:
        return jsonify(error=f"pymongo is not installed: {e}"), 400

    name = review.bundle_name(job.get("filename"), job_id)
    try:
        bundle = review.write_bundle(JOBS_DIR / job_id, name=name)
        client, database = push_mongo.connect(db=cfg["db"])
        try:
            records = json.loads((Path(bundle["folder"]) / "questions.json").read_text(encoding="utf-8"))
            res = push_mongo.push_records(
                records, Path(bundle["folder"]) / "images", database=database,
                collection=cfg["collection"], write=bool(body.get("write")),
                images=body.get("images", "auto"), chapter=name,
                file_name=job.get("filename") or name,
                session_id=body.get("sessionId") or os.environ.get("MONGODB_SESSION_ID"))
        finally:
            client.close()
        return jsonify({**res, "bundle": bundle, "db": cfg["db"], "collection": cfg["collection"],
                        "chapter": name})
    except RuntimeError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 502


@app.get("/api/jobs/<job_id>/export")
def job_export(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    records = review.export(JOBS_DIR / job_id)
    missing = sum(1 for r in records if review.missing_fields(r))
    body = json.dumps(records, indent=2, ensure_ascii=False)
    name = f"questions_{job_id}.json"
    return app.response_class(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "X-Records": str(len(records)),
        "X-Records-Missing-Fields": str(missing)})


@app.get("/api/jobs/<job_id>/images.zip")
def job_images_zip(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    records = review.export(JOBS_DIR / job_id)
    files = sorted({Path(c["url"]).name for r in records for c in r["imageCrops"]})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(JOBS_DIR / job_id / "out" / "images" / f, f"images/{f}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"images_{job_id}.zip")


@app.get("/api/jobs/<job_id>/images/<name>")
def job_image(job_id, name):
    _job_or_404(job_id)
    return send_from_directory(JOBS_DIR / job_id / "out" / "images", name, max_age=86400)


if __name__ == "__main__":
    from werkzeug.serving import WSGIRequestHandler
    # keep-alive: a result page loads hundreds of small images
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    if _from_env_file:
        print("from .env: " + ", ".join(_from_env_file))
    print("MongoDB:   " + (f"{settings.describe('MONGODB_URI')} -> "
                           f"{_mongo_settings()['db']}.{_mongo_settings()['collection']}"
                           if os.environ.get("MONGODB_URI")
                           else "not configured - add MONGODB_URI to .env to push from the app"))
    print("Open http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
