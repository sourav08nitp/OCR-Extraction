"""Export finished chapters as bundles: the questions JSON and its images in one folder.

    exports/Class-9-Maths-Statistics/
        questions.json
        images/p011_eq003.png ...

That is the shape tools/push_mongo.py expects when it is asked to put the images in GridFS, and it
keeps a chapter's JSON and pictures together instead of a loose file and a zip that have to be paired
up by hand.

The app must be running (python app.py).

    python tools/export_bundle.py                 # every finished job
    python tools/export_bundle.py fe451bae59a0    # one job, by id

The app does the work (review.write_bundle), so this and the 📦 Bundle button in the review screen
always produce the same thing. Bundles go to exports/.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = "http://127.0.0.1:5000"


def api(path, timeout=300):
    with urllib.request.urlopen(APP + path, timeout=timeout) as r:
        return json.loads(r.read())


def post(path, timeout=600):
    """The app does the bundling (review.write_bundle), so this and the 📦 Bundle button agree."""
    req = urllib.request.Request(APP + path, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", nargs="*", help="job id(s); default is every finished job")
    args = ap.parse_args()

    try:
        jobs = api("/api/jobs")["jobs"]
    except urllib.error.URLError:
        sys.exit(f"Cannot reach the app at {APP} - start it first with:  python app.py")

    if args.job:
        wanted = set(args.job)
        jobs = [j for j in jobs if j["id"] in wanted]
        missing = wanted - {j["id"] for j in jobs}
        if missing:
            sys.exit("No such job: " + ", ".join(sorted(missing)))
    jobs = [j for j in jobs if j.get("status") == "done"]
    if not jobs:
        sys.exit("No finished jobs to export.")

    total_q = total_i = 0
    for n, job in enumerate(jobs, 1):
        print(f"[{n}/{len(jobs)}] {job.get('filename') or job['id']}")
        try:
            res = post(f"/api/jobs/{job['id']}/bundle")
        except urllib.error.HTTPError as e:
            print(f"   skipped: the app answered {e.code}")
            continue
        total_q += res["questions"]
        total_i += res["images"]
        print(f"   {res['folder']}: {res['questions']} question(s), {res['images']} image(s)"
              + (f", {len(res['missing'])} image file(s) missing" if res["missing"] else ""))

    print(f"\n{total_q} question(s) and {total_i} image(s) written.")


if __name__ == "__main__":
    main()
