r"""Push exported chapter JSON into MongoDB.

The export is plain JSON, so three fields need converting before they are the types a MongoDB
document normally uses. Everything else (including the \(...\) LaTeX) goes in unchanged.

    id          "6aafd46b74656109930bf698"   -> _id, ObjectId
    documentId  "6aafd3873380ccdde5c8fd23"   -> ObjectId
    createdAt   "2026-09-20T12:42:42.260Z"   -> BSON date
    updatedAt   same

The connection string comes from MONGODB_URI, read from the .env file in the project root (see
.env.example, and settings.py for how it is loaded). A real environment variable wins over .env, so
you can still override it for one terminal:

    $env:MONGODB_URI = "mongodb+srv://user:pass@cluster.mongodb.net"     (PowerShell)

Then:

    python tools/push_mongo.py exports --db questionbank --collection questions
    python tools/push_mongo.py exports --db questionbank --collection questions --write

Without --write nothing is sent: it connects, converts and reports what it would do.
Documents are upserted by _id, so running it twice does not create duplicates; the second run
updates. Pass --string-ids if your collection stores ids as strings rather than ObjectId.

IMAGES
------
Point it at bundles made by tools/export_bundle.py (questions.json + images/ in one folder):

    python tools/export_bundle.py
    python tools/push_mongo.py exports --db questionbank --collection questions --write

--images decides where the pictures go:

    auto      (default) Supabase when SUPABASE_URL is in .env, otherwise skip
    supabase  upload to the bucket as <question id>_<type>_<index>; records get the public URLs
    gridfs    store them in MongoDB itself; the records get MONGODB_IMAGE_URL + the id
    skip      leave the URLs alone - for when you upload the images/ folder somewhere yourself

Either way each file is stored once under a name derived from the question id, so a second run finds
it instead of uploading again, and every reference is rewritten - `images`, `questionImage`,
`imageCrops[].url` and the ![](...) ones inside the question text.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from exam_names import normalize_exam
try:
    import settings
    settings.load()            # .env in the project root; real environment variables win
except ImportError:
    pass

DATE_FIELDS = ("createdAt", "updatedAt")
URL_FIELDS = ("images", "optionImages")   # the document fields that are lists of image URLs
IMG_IN_TEXT = re.compile(r"!\[\]\(([^)\s]+)\)")
TEXT_FIELDS = ("stem", "answer", "explanation")


def to_document(record, string_ids=False):
    """One exported question -> one MongoDB document."""
    from bson import ObjectId

    doc = dict(record)
    if "exam" in doc:
        doc["exam"] = normalize_exam(doc["exam"])
    oid = lambda v: v if string_ids or not isinstance(v, str) or len(v) != 24 else ObjectId(v)

    if "id" in doc:
        doc["_id"] = oid(doc.pop("id"))
    if doc.get("documentId"):
        doc["documentId"] = oid(doc["documentId"])
    for f in DATE_FIELDS:
        if isinstance(doc.get(f), str):
            doc[f] = datetime.fromisoformat(doc[f].replace("Z", "+00:00"))
    # path.section is non-nullable where these are read; a null makes the read throw and the page
    # then shows an empty list rather than an error, which is a slow thing to diagnose
    if isinstance(doc.get("path"), dict) and not doc["path"].get("section"):
        doc["path"]["section"] = "All sections"
    return doc


def image_map(record):
    """url -> (local file name, object name) for every image this question refers to.

    The object name is the one the question bank already uses:

        <question _id>_<kind>_<index>_<epoch ms>      6a748d64...fbc2_question_0_1786023516979

    The trailing stamp is taken from the question id's own first four bytes, which are its creation
    time in seconds, rather than from the clock. It therefore lands in the same range as the existing
    names but never changes, so pushing a chapter twice finds the picture already in the bucket
    instead of uploading a second copy and orphaning the first."""
    qid = str(record.get("_id") or record.get("id") or "")
    try:
        stamp = int(qid[:8], 16) * 1000
    except ValueError:
        stamp = 0
    out = {}
    for i, c in enumerate(record.get("imageCrops") or []):
        url = c.get("url")
        if not url:
            continue
        # the bucket's names only ever use question/option/ai; an explanation crop is still filed
        # under "question" there, and the record's own `type` is what says which it is
        kind = "option" if c.get("type") == "option" else "question"
        out[url] = (Path(url).name, f"{qid}_{kind}_{i}_{stamp}")
    return out


def rewrite_urls(record, mapping):
    """Point every image reference at its stored location - the lists, questionImage, imageCrops and
    the ![](...) ones inside the question text. mapping is {old url: new url}."""
    new = lambda u: mapping.get(u, u)
    for f in URL_FIELDS:
        if isinstance(record.get(f), list):
            record[f] = [new(u) for u in record[f]]
    if record.get("questionImage"):
        record["questionImage"] = new(record["questionImage"])
    for c in record.get("imageCrops") or []:
        if c.get("url"):
            c["url"] = new(c["url"])
    for f in TEXT_FIELDS:
        if isinstance(record.get(f), str):
            record[f] = IMG_IN_TEXT.sub(lambda m: f"![]({new(m.group(1))})", record[f])
    return record


def put_images(record, images_dir, fs, write, prefix="/files/"):
    """Store this question's images in GridFS under the ids the export already gave them.
    Returns (stored, reused, missing, {old url: new url})."""
    from bson import ObjectId

    stored = reused = 0
    missing, mapping = [], {}
    for url, (name, obj) in sorted(image_map(record).items()):
        path = images_dir / name
        if not path.exists():
            missing.append(name)
            continue
        oid = ObjectId(hashlib.md5(obj.encode()).hexdigest()[:24])   # stable id from the object name
        mapping[url] = prefix + str(oid)
        if fs.exists(oid):
            reused += 1
            continue
        if write:
            fs.put(path.read_bytes(), _id=oid, filename=obj,
                   contentType="image/png", metadata={"documentId": record.get("documentId")})
        stored += 1
    return stored, reused, missing, mapping


def put_images_supabase(record, images_dir, write):
    """Same, but the files go to a Supabase Storage bucket and the records get its public URLs.
    Objects are named <question id>_<type>_<index>, the convention already used in the bucket."""
    import supabase_store

    stored = reused = 0
    missing, mapping = [], {}
    for url, (name, obj) in sorted(image_map(record).items()):
        path = images_dir / name
        if not path.exists():
            missing.append(name)
            continue
        dest = obj          # <question id>_<type>_<index>, matching what is already in the bucket
        mapping[url] = supabase_store.public_url(dest)
        if not write:
            stored += 1          # reporting only: we do not ask Supabase what is already there
            continue
        if supabase_store.upload(path, dest) == "stored":
            stored += 1
        else:
            reused += 1
    return stored, reused, missing, mapping


DOCUMENTS = "ingest_documents"


def push_document(records, database, *, file_name, session_id=None, write=False, now=None):
    """Upsert the ingest_documents row the questions hang off.

    The app finds questions through session -> ingest_documents.sessionId -> question.documentId, so
    without this row a pushed chapter is invisible however correct its questions are. Keyed on the
    documentId the questions already carry, so it can be re-run."""
    from bson import Int64, ObjectId

    if not records:
        return None
    first = records[0]
    doc_id = first.get("documentId")
    if not doc_id:
        return None
    doc_id = doc_id if isinstance(doc_id, ObjectId) else ObjectId(str(doc_id))
    now = now or datetime.now(timezone.utc)
    kinds = [r.get("questionType") for r in records if r.get("questionType")]
    fields = {
        "fileName": file_name,
        # The PDF never went to Drive - it was extracted here. This cannot be "" for every chapter:
        # driveFileId carries a UNIQUE index, so a second one would be rejected as a duplicate key.
        # A per-document value keeps it unique and is obviously not a Drive id.
        "driveFileId": f"local:{doc_id}",
        "uploadGroupId": "",          # what Prisma's @default("") would have written
        "deletedAt": None,
        "kind": "question",          # the PDF holds the questions; their answers are in the same file
        "answerLayout": "inline",
        "source": "module",
        "status": "extracted",
        "flagged": False,
        "questionCount": Int64(len(records)),   # every existing row stores this as a 64-bit int
        "questionType": max(set(kinds), key=kinds.count) if kinds else None,
        "sectionName": "All sections",
        "subject": first.get("subject"),
        "exam": normalize_exam(first.get("exam")),
        "pyq": any(r.get("isPyq") for r in records),
        "pyqExam": None,
        "pyqYear": None,
        "paper": None,
        "pageRange": None,
        "topics": [],
        # section is non-nullable in the reading app's schema (Prisma P2032 on a null), and a failed
        # read shows up there as "no files", not as an error - so never let a null through
        "path": {**(first.get("path") or {}),
                 "section": (first.get("path") or {}).get("section") or "All sections"},
        "updatedAt": now,
        "extractedAt": now,
    }
    if session_id:
        fields["sessionId"] = session_id if isinstance(session_id, ObjectId) else ObjectId(str(session_id))
    if write:
        database[DOCUMENTS].update_one({"_id": doc_id},
                                       {"$set": fields, "$setOnInsert": {"createdAt": now}}, upsert=True)
    return {"documentId": str(doc_id), "sessionId": str(fields.get("sessionId") or ""),
            "fileName": file_name, "questionCount": len(records)}


def connect(uri=None, db=None):
    """A client and database from MONGODB_URI / MONGODB_DB, failing early if it cannot be reached."""
    uri = uri or os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set - put it in the .env file in the project root")
    from pymongo import MongoClient
    # tz_aware: BSON always stores UTC, but without this reads come back as naive datetimes
    client = MongoClient(uri, serverSelectionTimeoutMS=15000, tz_aware=True)
    client.admin.command("ping")
    return client, client[db or os.environ.get("MONGODB_DB") or "questionbank"]


def image_mode(choice="auto"):
    """"auto" means Supabase when it is configured, since that is where the images are meant to live."""
    if choice != "auto":
        return choice
    sys.path.insert(0, str(BASE))
    import supabase_store
    return "supabase" if supabase_store.configured() else "skip"


def push_records(records, images_dir=None, *, database=None, collection=None, write=False,
                 images="auto", image_url=None, string_ids=False, chapter=None,
                 file_name=None, session_id=None):
    """Push one chapter. Shared by the command line and the web app so both behave identically.
    Returns counts; with write=False nothing is sent and nothing is uploaded."""
    from pymongo import UpdateOne

    coll = database[collection or os.environ.get("MONGODB_COLLECTION") or "questions"]
    mode = image_mode(images)
    fs = None
    if mode != "skip":
        if images_dir is None or not Path(images_dir).is_dir():
            raise RuntimeError("no images/ folder for this chapter - make a bundle first")
        if mode == "gridfs":
            from gridfs import GridFS
            fs = GridFS(database)

    docs = [to_document(r, string_ids) for r in records]
    stored = reused = 0
    missing, mapping = [], {}
    for d in docs:
        if mode == "gridfs":
            s, r_, miss, m = put_images(d, Path(images_dir), fs, write,
                                        image_url or os.environ.get("MONGODB_IMAGE_URL") or "/files/")
        elif mode == "supabase":
            s, r_, miss, m = put_images_supabase(d, Path(images_dir), write)
        else:
            continue
        stored += s
        reused += r_
        missing += miss
        mapping.update(m)
    if mapping:
        docs = [rewrite_urls(d, mapping) for d in docs]

    new = updated = 0
    if write and docs:
        res = coll.bulk_write([UpdateOne({"_id": d["_id"]}, {"$set": d}, upsert=True) for d in docs],
                              ordered=False)
        new, updated = res.upserted_count, res.modified_count
    doc_row = push_document(docs, database, file_name=file_name or chapter,
                            session_id=session_id or os.environ.get("MONGODB_SESSION_ID"),
                            write=write)
    return {"questions": len(docs), "new": new, "updated": updated, "imagesStored": stored,
            "imagesReused": reused, "missing": sorted(set(missing)), "wrote": bool(write),
            "images": mode, "document": doc_row}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default=str(BASE / "exports"), help="folder of exported .json files")
    ap.add_argument("--db", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--write", action="store_true", help="actually send them (without this it only reports)")
    ap.add_argument("--string-ids", action="store_true", help="keep ids as strings instead of ObjectId")
    ap.add_argument("--images", choices=["auto", "skip", "gridfs", "supabase"], default="auto",
                    help="where the pictures go. auto = supabase when it is configured in .env, else skip")
    ap.add_argument("--image-url", default=None,
                    help='rewrite every image reference to this prefix + id, e.g. "/files/"')
    args = ap.parse_args()

    files = sorted(Path(args.folder).rglob("*.json"))
    if not files:
        sys.exit(f"No .json files under {args.folder}")
    try:
        client, database = connect(db=args.db)
    except ImportError:
        sys.exit("pymongo is not installed:  pip install pymongo")
    except RuntimeError as e:
        sys.exit(f"{e}. See the note at the top of this file.")
    mode = image_mode(args.images)
    if mode == "supabase":
        import supabase_store
        ok, msg = supabase_store.check()
        print(("images -> Supabase: " if ok else "Supabase problem: ") + msg)
        if not ok:
            sys.exit(1)
    print(f"connected: {args.db}.{args.collection}"
          f"{'' if args.write else '   (reporting only - add --write to send)'}\n")

    total = img_new = img_old = 0
    missing_all = []
    for path in files:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"{path.name}: skipped ({e})")
            continue
        if not isinstance(records, list):
            print(f"{path.name}: skipped (not a list of questions)")
            continue
        try:
            r = push_records(records, path.parent / "images", database=database,
                             collection=args.collection, write=args.write, images=args.images,
                             image_url=args.image_url, string_ids=args.string_ids,
                             chapter=path.parent.name)
        except RuntimeError as e:
            sys.exit(f"{path.parent}: {e} (make bundles with tools/export_bundle.py)")
        if args.write:
            print(f"{path.name}: {r['new']} new, {r['updated']} updated")
        else:
            print(f"{path.name}: {r['questions']} question(s) ready")
        total += r["questions"]
        img_new += r["imagesStored"]
        img_old += r["imagesReused"]
        missing_all += r["missing"]

    print(f"\n{total} question(s) in {len(files)} file(s).")
    if mode != "skip":
        where = "Supabase" if mode == "supabase" else "GridFS"
        print(f"{img_new} image(s) {'uploaded to' if args.write else 'ready for'} {where}, "
              f"{img_old} already there.")
    if missing_all:
        uniq = sorted(set(missing_all))
        print(f"WARNING: {len(uniq)} image(s) referenced but not in the images/ folder: "
              + ", ".join(uniq[:5]) + (" ..." if len(uniq) > 5 else ""))
    if not args.write:
        print("Nothing sent. Add --write to push.")
    client.close()


if __name__ == "__main__":
    main()
