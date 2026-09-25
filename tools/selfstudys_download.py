"""Download NCERT solution chapters from selfstudys.com through the site's own Download button, then zip them.

Uses your normal Chrome with its own profile folder, so you log in once and it is remembered.
It respects the site's limits: if the Download button does not appear (quota used up) or the site asks
for login / phone verification, it stops and tells you; run it again later and it resumes.

    python tools/selfstudys_download.py --list            # show chapters, download nothing
    python tools/selfstudys_download.py                   # download everything missing, then zip
    python tools/selfstudys_download.py --book 10-maths --limit 3
"""

import argparse
import json
import random
import re
import time
import urllib.request
import zipfile
from pathlib import Path

import truststore

# verify HTTPS against the Windows certificate store (what Chrome uses); Python's own bundle rejects this site's chain
truststore.inject_into_ssl()

SITE = "https://www.selfstudys.com"
BOOKS = {  # key -> (class folder, subject folder, book page)
    "10-maths":   ("Class 10", "Mathematics", "/books/ncert-solution/english/10th/class-10-mathematics/766"),
    "10-science": ("Class 10", "Science",     "/books/ncert-solution/english/10th/class-10-science/782"),
    "9-maths":    ("Class 9",  "Mathematics", "/books/ncert-solution/english/9th/class-9-mathematics/785"),
    "9-science":  ("Class 9",  "Science",     "/books/ncert-solution/english/9th/class-9-science/797"),
}
OUT = Path(__file__).resolve().parent.parent / "downloads" / "selfstudys"
PROFILE = OUT / ".chrome-profile"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
WAIT_BETWEEN = (8, 15)   # seconds between chapters, so we browse at a human pace
BUTTON_WAIT = 20         # seconds to wait for the site to show the Download button
PROMPT_WAIT = 300        # seconds to wait for you to answer a site prompt in the Chrome window


def chapters(book_path):
    """Chapter viewer links in the order the book page lists them."""
    req = urllib.request.Request(SITE + book_path, headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
    slug = book_path.split("/")[-2]
    seen, out = set(), []
    for m in re.finditer(rf'href="(/advance-pdf-viewer/ncert-solution/english/[^"]*/{re.escape(slug)}/([^/"]+)/(\d+))"', html):
        url, chap_slug, cid = m.groups()
        if cid in seen:
            continue
        seen.add(cid)
        title = re.sub(r"^chapter \d+\s*", "", chap_slug.replace("-", " ").strip(), flags=re.I).title()
        out.append({"url": SITE + url, "title": title, "id": cid})
    return out


def safe(name):
    return re.sub(r'[<>:"/\\|?*]+', "", name).strip()


def is_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


class Stop(Exception):
    """The site asked for something only a person should do (quota, login, verification)."""


def download_one(page, ch, dest):
    page.goto(ch["url"], wait_until="domcontentloaded", timeout=90_000)
    button = page.locator(".downloadPdfBtn:not(.hideThis)").first
    try:
        button.wait_for(state="visible", timeout=BUTTON_WAIT * 1000)
    except Exception:
        raise Stop("the Download button did not appear - the site's download quota is probably used up "
                   "for now (or it wants you to log in). Try again later or log in, then re-run.")
    try:
        with page.expect_download(timeout=15_000) as info:
            button.click()
        info.value.save_as(dest)
        return
    except Exception:
        pass
    # no file yet: the site is showing a prompt (class confirmation, login, email or phone verification).
    # Those are for a person to answer, so wait for them to do it in the Chrome window.
    print(f"\n  The site is asking something in the Chrome window (login / class / verification).\n"
          f"  Please answer it there - waiting up to {PROMPT_WAIT // 60} minutes for the download to start ... ",
          end="", flush=True)
    try:
        download = page.wait_for_event("download", timeout=PROMPT_WAIT * 1000)
        download.save_as(dest)
    except Exception:
        raise Stop("no download started after the prompt. If the site asked for phone verification or said the "
                   "limit is reached, that is its download limit - run the script again later; it resumes.")


def make_zips(keys, root=OUT):
    made = []
    for key in keys:
        cls, subj, _ = BOOKS[key]
        folder = root / cls / subj
        pdfs = sorted(folder.glob("*.pdf")) if folder.exists() else []
        if not pdfs:
            continue
        zpath = root / f"{cls.replace(' ', '-')}-{subj}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in pdfs:
                z.write(p, f"{cls}/{subj}/{p.name}")
        made.append((zpath, len(pdfs)))
    return made


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", choices=list(BOOKS), action="append", help="only these books (repeatable)")
    ap.add_argument("--list", action="store_true", help="list chapters and what is already downloaded, then exit")
    ap.add_argument("--limit", type=int, default=0, help="download at most this many chapters this run")
    ap.add_argument("--zip-from", metavar="FOLDER", help="only zip PDFs already in FOLDER/Class N/Subject "
                    "(e.g. the extension's Downloads\\selfstudys folder) and exit")
    args = ap.parse_args()
    keys = args.book or list(BOOKS)
    if args.zip_from:
        made = make_zips(keys, Path(args.zip_from))
        for zpath, n in made:
            print(f"zip: {zpath}  ({n} chapters)")
        if not made:
            print(f"No PDFs found under {args.zip_from}\\Class N\\Subject")
        return

    plan = []
    for key in keys:
        cls, subj, path = BOOKS[key]
        chs = chapters(path)
        for i, ch in enumerate(chs, 1):
            dest = OUT / cls / subj / f"{i:02d} - {safe(ch['title'])}.pdf"
            plan.append((key, ch, dest))
        print(f"{cls} {subj}: {len(chs)} chapters")

    todo = [(k, ch, d) for k, ch, d in plan if not is_pdf(d)]
    if args.list:
        for k, ch, d in plan:
            print(f"  [{'done' if is_pdf(d) else '    '}] {d.relative_to(OUT)}")
        print(f"\n{len(plan) - len(todo)} of {len(plan)} downloaded")
        return
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("Everything is already downloaded.")
    else:
        from playwright.sync_api import sync_playwright

        PROFILE.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False,
                                                        accept_downloads=True, viewport={"width": 1200, "height": 800})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(SITE, wait_until="domcontentloaded", timeout=90_000)
            input("\nA Chrome window opened. If you want to log in to selfstudys, do it there now.\n"
                  "Press Enter here to start downloading... ")
            done = 0
            try:
                for n, (key, ch, dest) in enumerate(todo, 1):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    print(f"[{n}/{len(todo)}] {dest.relative_to(OUT)} ... ", end="", flush=True)
                    download_one(page, ch, dest)
                    if not is_pdf(dest):
                        dest.unlink(missing_ok=True)
                        raise Stop("the downloaded file is not a PDF (maybe a login or error page). Stopping.")
                    done += 1
                    print(f"ok ({dest.stat().st_size // 1024} KB)")
                    if n < len(todo):
                        time.sleep(random.uniform(*WAIT_BETWEEN))
            except Stop as e:
                print(f"STOPPED\n  -> {e}")
            finally:
                ctx.close()
            print(f"\nDownloaded {done} chapter(s) this run.")

    for zpath, n in make_zips(keys):
        print(f"zip: {zpath}  ({n} chapters)")
    (OUT / "manifest.json").write_text(json.dumps(
        [{"book": k, "title": ch["title"], "url": ch["url"], "file": str(d.relative_to(OUT)), "downloaded": is_pdf(d)}
         for k, ch, d in plan], indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
