r"""One-off migration: rewrite maths in the stored jobs from $...$ to \(...\) (and $$...$$ to \[...\]).

From 21 Sep 2026 the whole pipeline uses \(...\): the recogniser, the AI prompt, the review screen
and the export. Jobs extracted before that hold $...$ in structured.json and review.json, so this
brings them over. Without it those jobs still render (the review screen reads both), but the two
forms would sit side by side in your data.

    python tools/migrate_job_delims.py                 # show what would change
    python tools/migrate_job_delims.py --write         # do it (keeps a .bak beside each file)
    python tools/migrate_job_delims.py --write --to dollar   # undo

Stop the app first, so nothing writes a job while it is being converted.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from review import to_paren_delims  # noqa: E402
from tools.convert_delims import to_dollar_delims  # noqa: E402


def walk(value, convert):
    """Convert every string in a nested structure; returns (new_value, strings_changed)."""
    if isinstance(value, str):
        out = convert(value)
        return out, int(out != value)
    if isinstance(value, list):
        n = 0
        out = []
        for v in value:
            nv, c = walk(v, convert)
            out.append(nv)
            n += c
        return out, n
    if isinstance(value, dict):
        n = 0
        out = {}
        for k, v in value.items():
            nv, c = walk(v, convert)
            out[k] = nv
            n += c
        return out, n
    return value, 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", default=str(BASE / "jobs"))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--to", choices=["paren", "dollar"], default="paren")
    args = ap.parse_args()

    convert = to_paren_delims if args.to == "paren" else to_dollar_delims
    files = sorted(Path(args.jobs).rglob("*.json"))
    if not files:
        sys.exit(f"No .json files under {args.jobs}")

    total = touched = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            print(f"{path.relative_to(BASE)}: skipped ({e})")
            continue
        out, changed = walk(data, convert)
        if not changed:
            continue
        touched += 1
        total += changed
        print(f"{path.relative_to(BASE)}: {changed} string(s) {'changed' if args.write else 'would change'}")
        if args.write:
            shutil.copy2(path, path.with_suffix(".json.bak"))
            path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    print(f"\n{total} string(s) across {touched} file(s) of {len(files)} scanned.")
    if total and not args.write:
        print("Nothing written. Add --write to apply.")


if __name__ == "__main__":
    main()
