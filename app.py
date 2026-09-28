"""Local web UI: upload a solutions PDF, run pdf_to_structured, view the result with rendered LaTeX.

    python app.py      ->  http://127.0.0.1:5000
"""

import io
import json
import os
import queue
import shutil
import sys
import tempfile
import zipfile
import threading
import traceback
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import ai_fallback
import pdf_to_structured
import review
import settings
from exam_names import normalize_exam

warnings.filterwarnings("ignore")

_from_env_file = settings.load()   # .env in the project root; real environment variables win

BASE = Path(__file__).parent
JOBS_DIR = Path(tempfile.gettempdir()) / "ocr-extraction-sessions"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
# PDFs are source material, not project data.  They live outside the checkout only
# for as long as the local extractor/review viewer needs them.
TEMP_INPUT_DIR = Path(tempfile.gettempdir()) / "ocr-extraction-inputs"
TEMP_INPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

jobs = {}              # job_id -> status dict
work = queue.Queue()   # one worker: pix2tex is heavy and the model is shared
_model = None
_image_edit_lock = threading.Lock()


def _get_model(job):
    global _model
    if _model is None:
        job.update(stage="loading LaTeX model", done=0, total=0)
        _model = pdf_to_structured.load_latex_model()
    return _model


def _meta(job_id):
    try:
        return json.loads((JOBS_DIR / job_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_meta(job_id, meta):
    (JOBS_DIR / job_id / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _source_path(job_id, job=None):
    details = job or jobs.get(job_id, {})
    raw = details.get("tempPdf") or _meta(job_id).get("tempPdf")
    return Path(raw) if raw else TEMP_INPUT_DIR / f"{job_id}.pdf"


def _link_source(job_id, source):
    """Expose a temporary source to legacy review code without storing it in the repo."""
    link = JOBS_DIR / job_id / "input.pdf"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(source)


def _ensure_source(job_id, job=None):
    """Retrieve the Drive source into system temp only when the review UI needs it."""
    source = _source_path(job_id, job)
    if source.is_file():
        _link_source(job_id, source)
        return source
    details = job or jobs.get(job_id, {})
    meta = _meta(job_id)
    drive_file_id = details.get("driveFileId") or meta.get("driveFileId")
    if not drive_file_id:
        raise RuntimeError("The temporary PDF is gone and no Google Drive file is linked to this job")
    import drive_store
    drive_store.download_pdf(drive_file_id, source)
    _link_source(job_id, source)
    meta["tempPdf"] = str(source)
    _save_meta(job_id, meta)
    if details is not None:
        details["tempPdf"] = str(source)
    return source


def _discard_source(job_id, job=None):
    """Remove only the system-temp copy and its repository symlink."""
    source = _source_path(job_id, job)
    link = JOBS_DIR / job_id / "input.pdf"
    removed = False
    try:
        if source.is_relative_to(TEMP_INPUT_DIR) and source.exists():
            source.unlink()
            removed = True
    except OSError:
        pass
    try:
        if link.is_symlink():
            link.unlink()
    except OSError:
        pass
    return removed


def _assign_session_project(session_id, project_id):
    """Assign an OCR-only session to an OCR project folder."""
    if session_id:
        import ocr_store
        ocr_store.update_session(session_id, projectId=project_id or None)


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
                doc = pdf_to_structured.run(_ensure_source(job_id, job), job_dir / "out", want_latex=job["latex"],
                                            progress=progress, model=model, use_ai=False)
                if job.get("ai"):
                    if review.has_scanned_pages(doc):
                        # scanned pages: one AI call per question reads better than one per formula, and costs ~10x less
                        job["result"] = review.ai_reread_all(job_dir, progress)
                    else:
                        _ai_fix_existing(job_dir / "out", progress)
            job.update(status="done", stage="done")
            try:
                import ocr_store
                ocr_store.update_session(job_id, status="ready")
            except Exception:
                pass
            _note_counts(job_dir)
        except Exception as e:
            traceback.print_exc()
            job.update(status="error", error=f"{type(e).__name__}: {e}")
            try:
                import ocr_store
                ocr_store.update_session(job_id, status="error")
            except Exception:
                pass
            _discard_source(job_id, job)


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

    import drive_store
    if not drive_store.configured():
        return jsonify(error="Google Drive is not configured; the PDF is not kept locally"), 400

    job_id = uuid.uuid4().hex[:12]
    source = TEMP_INPUT_DIR / f"{job_id}.pdf"
    try:
        # The browser posts to this local server, which immediately files the PDF
        # in Drive. The only local copy is this system-temporary processing file.
        f.save(source)
        import ocr_store
        session_id = job_id
        ocr_store.create_session(session_id, f"OCR · {Path(f.filename).name}",
                                 project_id=request.form.get("projectId") or None)
        drive_file_id = drive_store.upload_pdf(source, Path(f.filename).name)
        ocr_store.update_session(session_id, driveFileId=drive_file_id, status="extracting")
    except Exception as e:
        try:
            source.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify(error=f"Could not upload the PDF to Google Drive: {e}"), 502

    job_dir = JOBS_DIR / job_id
    job_dir.mkdir()
    _link_source(job_id, source)
    want_ai = request.form.get("ai") == "1" and ai_fallback.available()
    _save_meta(job_id, {  # source data stays in Drive; only its id/path are recorded locally
        "filename": Path(f.filename).name, "latex": request.form.get("latex") == "1" or want_ai,
        "ai": want_ai, "sessionId": session_id, "projectId": request.form.get("projectId") or None,
        "driveFileId": drive_file_id, "tempPdf": str(source)})
    jobs[job_id] = {
        "id": job_id, "filename": Path(f.filename).name,
        "latex": request.form.get("latex") == "1" or want_ai, "ai": want_ai,
        "sessionId": session_id, "projectId": request.form.get("projectId") or None,
        "driveFileId": drive_file_id, "tempPdf": str(source),
        "status": "queued", "stage": "waiting in queue", "done": 0, "total": 0, "error": None,
    }
    work.put(job_id)
    return jsonify(job_id=job_id, sessionId=session_id, driveFileId=drive_file_id)


@app.get("/api/jobs")
def list_jobs():
    """Everything uploaded so far, newest first, for the list on the home page."""
    out = []
    for d in JOBS_DIR.iterdir():
        meta_path = d / "meta.json"
        source_link = d / "input.pdf"
        if not d.is_dir() or (not meta_path.is_file() and not source_link.exists()):
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
            "sessionId": live.get("sessionId") or meta.get("sessionId"),
            "projectId": live.get("projectId") or meta.get("projectId"),
            "driveFileId": live.get("driveFileId") or meta.get("driveFileId"),
            "filename": live.get("filename") or meta.get("filename") or "input.pdf",
            "status": live.get("status") or ("done" if has_result else "unknown"),
            "stage": live.get("stage"), "done": live.get("done", 0), "total": live.get("total", 0),
            "error": live.get("error"),
            "questions": meta.get("questions"),
            "hasResult": has_result,
            "workflow": review.workflow_status(d) if has_result else {"stage": "review"},
            "uploadedAt": (meta_path if meta_path.is_file() else source_link).stat().st_mtime,
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
        _discard_source(job_id, jobs.get(job_id))
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
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
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
    review.deduplicate_saved_images(JOBS_DIR / job_id, doc)
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
    job = _job_or_404(job_id)
    try:
        return send_file(_ensure_source(job_id, job), mimetype="application/pdf", download_name=job.get("filename"))
    except RuntimeError as e:
        return jsonify(error=str(e)), 404


def _review_payload(job_id):
    _ensure_source(job_id, _job_or_404(job_id))
    job_dir = JOBS_DIR / job_id
    doc = review.ensure_current(job_dir)
    saved = review.load_review(job_dir)
    meta = _meta(job_id)
    for field in ("sessionId", "projectId", "driveFileId"):
        if meta.get(field) and not saved["document"].get(field):
            saved["document"][field] = meta[field]
    review.save_review(job_dir, saved)
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
        "documentOptions": review.document_options(JOBS_DIR),
        "workflow": review.workflow_status(job_dir),
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
    document_id = saved["document"].get("documentId")
    session_id = saved["document"].get("sessionId")
    drive_file_id = saved["document"].get("driveFileId")
    project_id = saved["document"].get("projectId")
    saved["document"] = {k: doc_in[k] for k in review.DOC_FIELDS if k in doc_in}
    if "exam" in saved["document"]:
        saved["document"]["exam"] = normalize_exam(saved["document"]["exam"])
    if document_id and not saved["document"].get("documentId"):
        saved["document"]["documentId"] = document_id
    if session_id and not saved["document"].get("sessionId"):
        saved["document"]["sessionId"] = session_id
    if drive_file_id and not saved["document"].get("driveFileId"):
        saved["document"]["driveFileId"] = drive_file_id
    if project_id and not saved["document"].get("projectId"):
        saved["document"]["projectId"] = project_id
    if saved["document"].get("sessionId"):
        try:
            _assign_session_project(saved["document"]["sessionId"], saved["document"].get("projectId"))
        except Exception as e:
            return jsonify(error=f"Could not save the session project: {e}"), 502
    keys = {k for k, _, _ in review.questions(review.ensure_current(job_dir))}
    saved["questions"] = {k: {f: v for f, v in (m or {}).items() if f in review.MANUAL_FIELDS}
                          for k, m in (body.get("questions") or {}).items() if k in keys}
    review.save_review(job_dir, saved)
    return jsonify(ok=True, workflow=review.workflow_status(job_dir),
                   documentId=saved["document"].get("documentId"),
                   sessionId=saved["document"].get("sessionId"),
                   projectId=saved["document"].get("projectId"),
                   driveFileId=saved["document"].get("driveFileId"))


@app.patch("/api/sessions/<session_id>/project")
def session_project(session_id):
    body = request.get_json(silent=True) or {}
    project_id = body.get("projectId")
    if project_id is not None and (not isinstance(project_id, str) or len(project_id.strip()) > 200):
        return jsonify(error="projectId must be a string up to 200 characters"), 400
    try:
        _assign_session_project(session_id, project_id.strip() if isinstance(project_id, str) else None)
        return jsonify(sessionId=session_id, projectId=project_id.strip() if isinstance(project_id, str) else None)
    except Exception as e:
        return jsonify(error=f"Could not update the session project: {e}"), 502


@app.patch("/api/sessions/<session_id>")
def session_rename(session_id):
    body = request.get_json(silent=True) or {}
    label = str(body.get("label") or "").strip()
    if not label or len(label) > 200:
        return jsonify(error="Session name must be between 1 and 200 characters"), 400
    try:
        import ocr_store
        ocr_store.update_session(session_id, label=label)
        return jsonify(sessionId=session_id, label=label)
    except Exception as e:
        return jsonify(error=f"Could not rename the session: {e}"), 502


@app.delete("/api/sessions/<session_id>")
def session_delete(session_id):
    try:
        import ocr_store
        removed = ocr_store.delete_session(session_id)
        if not removed:
            return jsonify(error="OCR session not found"), 404
        # Session workspaces live only under the system temp directory.
        if (JOBS_DIR / session_id).is_dir():
            _discard_source(session_id, jobs.get(session_id))
            shutil.rmtree(JOBS_DIR / session_id)
        jobs.pop(session_id, None)
        return jsonify(deleted=session_id)
    except Exception as e:
        return jsonify(error=f"Could not delete the OCR session: {e}"), 502


@app.patch("/api/jobs/<job_id>/questions/<key>/text")
def job_question_text(job_id, key):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    body = request.get_json(silent=True)
    fields = ("stemOverride", "solutionOverride")
    if not isinstance(body, dict) or not body or any(k not in fields or not isinstance(v, str)
                                                                  for k, v in body.items()):
        return jsonify(error="Provide question or solution text as strings"), 400
    job_dir = JOBS_DIR / job_id
    doc = review.ensure_current(job_dir)
    try:
        ex, q = review.find_question(doc, key)
    except KeyError:
        abort(404)
    saved = review.load_review(job_dir)
    manual = saved["questions"].get(key, {})
    auto = review.auto_fields(doc, ex, q, lambda n: f"img:{n}")
    texts = {field: manual[field] if manual.get(field) is not None else auto[automatic]
             for field, automatic in (("stemOverride", "stem"), ("solutionOverride", "solutionText"))}
    changed = {k: v for k, v in body.items() if v != texts[k]}
    if changed:
        saved["questions"].setdefault(key, {}).update(changed)
        review.save_review(job_dir, saved)
    return jsonify(**{**texts, **body}, workflow=review.workflow_status(job_dir))


@app.post("/api/jobs/<job_id>/reread-all")
def job_reread_all(job_id):
    job = _job_or_404(job_id)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
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
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
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
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
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


@app.post("/api/jobs/<job_id>/questions/<key>/extract")
def job_question_extract(job_id, key):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(review.extract_region(JOBS_DIR / job_id, key, body.get("part"), int(body.get("page", 0)),
                                             [float(v) for v in body.get("bbox", [])]))
    except KeyError:
        abort(404)
    except (ValueError, TypeError, IndexError) as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 502


@app.post("/api/jobs/<job_id>/questions/<key>/image-text")
def job_question_image_text(job_id, key):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(review.transcribe_question_image(JOBS_DIR / job_id, key, body.get("part"), body.get("name")))
    except KeyError:
        abort(404)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f"AI image conversion failed: {e}"[:300]), 502


@app.post("/api/jobs/<job_id>/questions/<key>/topic")
def job_question_topic(job_id, key):
    """Topic and/or level for one question, instead of running the whole chapter."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    if not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
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
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400
    try:
        return jsonify(review.ai_reread(JOBS_DIR / job_id, key))
    except KeyError:
        abort(404)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"{type(e).__name__}: {e}"[:300]), 502


def _mongo_settings():
    """Where a push would go. The connection string stays in the environment, never in the project."""
    import drive_store
    import supabase_store
    images = "supabase" if supabase_store.configured() else "gridfs"
    return {"available": bool(os.environ.get("MONGODB_URI")),
            "db": os.environ.get("MONGODB_DB") or "questionbank",
            "collection": os.environ.get("MONGODB_COLLECTION") or "questions",
            "imageUrl": os.environ.get("MONGODB_IMAGE_URL") or "/files/",
            "sessionId": os.environ.get("MONGODB_SESSION_ID") or None,
            "images": images,
            "bucket": supabase_store.settings()[2] if images == "supabase" else None,
            "driveConfigured": drive_store.configured()}


@app.get("/api/mongo")
def mongo_status():
    return jsonify(_mongo_settings())


@app.get("/api/ocr/projects")
def ocr_projects():
    try:
        import ocr_store
        return jsonify(ocr_store.project_tree())
    except Exception as e:
        return jsonify(error=f"Could not load OCR projects: {e}"), 502


@app.post("/api/ocr/projects")
def ocr_project_create():
    try:
        import ocr_store
        body = request.get_json(silent=True) or {}
        project_id = ocr_store.create_project(body.get("label"))
        return jsonify(id=project_id, label=str(body.get("label")).strip()), 201
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f"Could not create OCR project: {e}"), 502


@app.patch("/api/ocr/projects/<project_id>")
def ocr_project_rename(project_id):
    try:
        import ocr_store
        ocr_store.update_project(project_id, (request.get_json(silent=True) or {}).get("label"))
        return jsonify(id=project_id)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except LookupError as e:
        return jsonify(error=str(e)), 404
    except Exception as e:
        return jsonify(error=f"Could not rename OCR project: {e}"), 502


@app.delete("/api/ocr/projects/<project_id>")
def ocr_project_delete(project_id):
    try:
        import ocr_store
        if not ocr_store.delete_project(project_id):
            return jsonify(error="Project not found"), 404
        return jsonify(deleted=project_id)
    except Exception as e:
        return jsonify(error=f"Could not delete OCR project: {e}"), 502


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


@app.post("/api/jobs/<job_id>/finalize")
def job_finalize(job_id):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    try:
        return jsonify(workflow=review.finalize_bundle(
            JOBS_DIR / job_id, review.bundle_name(job.get("filename"), job_id)))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=f"Could not finalize: {e}"[:300]), 500


@app.post("/api/jobs/<job_id>/push")
def job_push(job_id):
    """Bundle this chapter, file its source PDF in Drive, then push session-scoped records to MongoDB."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    cfg = _mongo_settings()
    if not cfg["available"]:
        return jsonify(error="MONGODB_URI is not set - add it to the .env file in the project root, then restart app.py"), 400
    body = request.get_json(silent=True) or {}
    if body.get("write") and not cfg["driveConfigured"]:
        return jsonify(error="Google Drive is not configured - add the Drive OAuth values to .env, then restart app.py"), 400
    sys.path.insert(0, str(BASE / "tools"))
    try:
        import push_mongo
    except ImportError as e:
        return jsonify(error=f"pymongo is not installed: {e}"), 400

    name = review.bundle_name(job.get("filename"), job_id)
    try:
        signature = review.review_signature(JOBS_DIR / job_id)
        bundle = review.write_bundle(JOBS_DIR / job_id, name=name)
        client, database = push_mongo.connect(db=cfg["db"])
        try:
            records = json.loads((Path(bundle["folder"]) / "questions.json").read_text(encoding="utf-8"))
            document = review.load_review(JOBS_DIR / job_id)["document"]
            session_meta = _meta(job_id)
            drive_file_id = document.get("driveFileId") or session_meta.get("driveFileId")
            if body.get("write") and not drive_file_id:
                import drive_store
                drive_file_id = drive_store.upload_pdf(
                    JOBS_DIR / job_id / "input.pdf", job.get("filename") or f"{name}.pdf",
                    exam=document.get("exam"), subject=document.get("subject"),
                    module=document.get("module"), chapter=document.get("chapter") or name,
                )
            res = push_mongo.push_records(
                records, Path(bundle["folder"]) / "images", database=database,
                collection=cfg["collection"], write=bool(body.get("write")),
                images=body.get("images", "auto"), chapter=name,
                file_name=job.get("filename") or name,
                # OCR sessions are deliberately separate from ingest sessions.
                session_id=None, create_session=False,
                drive_file_id=drive_file_id,
                session_context=None)
        finally:
            client.close()
        source_deleted = False
        if res.get("wrote") and res.get("document"):
            saved = review.load_review(JOBS_DIR / job_id)
            # Preserve the OCR session id; this push intentionally creates no ingest session.
            saved["document"]["driveFileId"] = res["document"]["driveFileId"]
            review.save_review(JOBS_DIR / job_id, saved)
            try:
                (JOBS_DIR / job_id / "input.pdf").unlink()
                source_deleted = True
            except OSError:
                # The Drive file and MongoDB links are already durable; a local cleanup retry must not undo a push.
                pass
        workflow = review.workflow_status(JOBS_DIR / job_id)
        if res.get("wrote") and not res.get("missing") and not bundle["missing"]:
            workflow = review.mark_pushed(JOBS_DIR / job_id, signature,
                                          {"db": cfg["db"], "collection": cfg["collection"]})
        return jsonify({**res, "bundle": bundle, "db": cfg["db"], "collection": cfg["collection"],
                        "chapter": name, "workflow": workflow, "sourceDeleted": source_deleted})
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


@app.post("/api/jobs/<job_id>/images/<name>/action")
def job_image_action(job_id, name):
    """Re-read, keep, or remove one extracted image everywhere it occurs in a job."""
    job = _job_or_404(job_id)
    if job["status"] != "done":
        abort(409)
    if Path(name).name != name or not name.lower().endswith(".png"):
        return jsonify(error="invalid image name"), 400
    action = (request.get_json(silent=True) or {}).get("action")
    if action not in ("ai", "skip", "remove"):
        return jsonify(error="action must be ai, skip, or remove"), 400
    if action == "ai" and not ai_fallback.available():
        return jsonify(error="OPENAI_API_KEY_2 is not set on the server"), 400

    job_dir = JOBS_DIR / job_id
    out = job_dir / "out"
    path = out / "structured.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    sections = pdf_to_structured.image_sections(doc, name)
    if not sections:
        abort(404)

    result = None
    if action == "ai":
        image = out / "images" / name
        if not image.is_file():
            return jsonify(error="the image file is missing"), 404
        token = f"[[eq:{name}]]"
        context = next((line.replace(token, "[image]") for sec in sections
                        for line in sec["text"].splitlines() if token in line), "")
        try:
            answer = ai_fallback.transcribe([(image, context)], force=True)[image]
        except Exception as e:
            return jsonify(error=f"AI re-read failed: {e}"[:300]), 502
        if answer.get("kind") == "figure":
            result = pdf_to_structured.figure_entry(f"AI ({ai_fallback.model_name()}) identified a figure", by="ai")
        elif answer.get("latex"):
            result = {"latex": answer["latex"], "source": "ai", "model": ai_fallback.model_name(),
                      "confidence": None, "needs_review": False}
        else:
            return jsonify(error=answer.get("error") or "AI could not read this image; it was left unchanged"), 422

    with _image_edit_lock:
        if job["status"] != "done":
            abort(409)
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not pdf_to_structured.image_sections(doc, name):
            abort(404)
        if action == "ai":
            count = pdf_to_structured.set_image_result(doc, name, result)
        elif action == "skip":
            count = pdf_to_structured.set_image_result(doc, name, {
                "latex": None, "source": "skipped", "kind": "image", "confidence": None,
                "needs_review": False, "reason": "kept as an image by user"})
        else:
            count = pdf_to_structured.remove_image(doc, name)
        if action in ("ai", "remove"):
            saved = review.load_review(job_dir)
            token = f"![](img:{name})"
            replacement = (rf"\({result['latex']}\)" if action == "ai" and result.get("latex")
                           else "" if action == "remove" else token)
            changed = False
            for manual in saved["questions"].values():
                for field in ("stemOverride", "solutionOverride"):
                    value = manual.get(field)
                    if isinstance(value, str) and token in value and replacement != token:
                        manual[field] = value.replace(token, replacement).strip()
                        changed = True
            if changed:
                review.save_review(job_dir, saved)
        pdf_to_structured.save(doc, out)
    return jsonify(ok=True, action=action, occurrences=count, result=result)


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
