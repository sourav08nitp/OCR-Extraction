r"""Rewrite maths delimiters in already-exported JSON files: $x$ -> \(x\) and $$x$$ -> \[x\].

New exports come out this way already (review.MATH_DELIMS). This is only for files saved earlier.

    python tools/convert_delims.py exports                 # show what would change
    python tools/convert_delims.py exports --write         # do it (keeps a .bak next to each file)
    python tools/convert_delims.py exports --write --to dollar   # go back to $...$
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from review import MATH_FIELDS, to_paren_delims  # noqa: E402

RE_PAREN_DISPLAY = re.compile(r"\\\[(.+?)\\\]", re.S)
RE_PAREN_INLINE = re.compile(r"\\\((.+?)\\\)", re.S)


def to_dollar_delims(text):
    r"""\(x\) -> $x$ and \[x\] -> $$x$$ - the reverse, in case something downstream wants $."""
    if not text or "\\" not in text:
        return text
    text = RE_PAREN_DISPLAY.sub(lambda m: "$$" + m.group(1) + "$$", text)
    return RE_PAREN_INLINE.sub(lambda m: "$" + m.group(1) + "$", text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of exported .json files (searched recursively)")
    ap.add_argument("--write", action="store_true", help="save the changes (without this it only reports)")
    ap.add_argument("--to", choices=["paren", "dollar"], default="paren")
    args = ap.parse_args()

    convert = to_paren_delims if args.to == "paren" else to_dollar_delims
    files = sorted(Path(args.folder).rglob("*.json"))
    if not files:
        sys.exit(f"No .json files under {args.folder}")

    total = 0
    for path in files:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"{path.name}: skipped ({e})")
            continue
        if not isinstance(records, list):
            print(f"{path.name}: skipped (not a list of questions)")
            continue

        changed = 0
        for r in records:
            if not isinstance(r, dict):
                continue
            for f in MATH_FIELDS:
                before = r.get(f)
                if isinstance(before, str):
                    after = convert(before)
                    if after != before:
                        r[f] = after
                        changed += 1
        total += changed
        print(f"{path.name}: {changed} field(s) {'changed' if args.write else 'would change'}")
        if changed and args.write:
            shutil.copy2(path, path.with_suffix(".json.bak"))
            path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{total} field(s) in {len(files)} file(s).")
    if total and not args.write:
        print("Nothing written. Add --write to apply.")


if __name__ == "__main__":
    main()
