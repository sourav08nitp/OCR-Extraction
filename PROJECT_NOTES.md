# Boards Extractor — project notes

Last updated: 22 Sep 2026

Turn exam-prep solution PDFs into structured data (exercise → question → solution) with the maths as
LaTeX, and view the result in a local web app. It started with an NCERT maths PDF. A Resonance
chemistry PDF was then analysed as the next target.

---

## 1. Quick start

```bash
python app.py
```

Open **http://127.0.0.1:5000**. Use `127.0.0.1`, not `localhost`: on Windows `localhost` makes every
request about 250 ms slower. Drop in a PDF, keep "Convert formula images to LaTeX" ticked, and click
**Extract**.

Command line, without the web app:

```bash
python pdf_to_structured.py book.pdf out            # structure + formula images only (seconds)
python pdf_to_structured.py book.pdf out --latex    # + local LaTeX conversion (~5 min/chapter on GPU)
python pdf_to_structured.py book.pdf out --ai       # + OpenAI fallback for uncertain formulas
```

Output in `out/`: `structured.json`, `preview.md`, `images/`.

Copy `.env.example` to `.env` if you want the MongoDB push buttons - see "The .env file" below.

Stop `app.py` when you're not using it. Once the LaTeX model is loaded it holds about 1.8 GB of RAM
(see section 7).

---

## 2. The NCERT pipeline (done and working)

Source: `selfstudys.pdf`, NCERT Class 12 Maths Ch. 1, 65 pages. Every formula is an **embedded
image** (~1,946), so plain text extraction drops all the maths.

```
PDF ──► words + image boxes (pdfplumber)
    ──► lines in reading order, [[eq:pNNN_eqNNN.png]] where each formula sits
    ──► formula crops (pypdfium2 render at 200 DPI, no padding)
    ──► Exercise / Question / Solution split (regex on headings)
    ──► LaTeX for each formula:
          1. symbol matcher   (glyph_match.py)   – single characters: x, f, *, ∴, ⇒, 1², L₁
          2. pix2tex, batched (latex_batch.py)   – everything else, on the GPU
          3. KaTeX check      (katex_check.js)   – anything that won't render → "to check"
          4. OpenAI fallback  (ai_fallback.py)   – optional, only for "to check" items
    ──► structured.json + preview.md + web view
```

### Results on the NCERT chapter

| Measure | Value |
|---|---|
| Exercises / questions found | 5 / 74. All question numbers match the book |
| Formula images | 1,946 |
| Read by the symbol matcher | 608 (all spot checks correct) |
| Read by pix2tex | 977 (about 8 in 10 clean) |
| Left for review without AI | 361 |
| AI fallback test | 7 of 7 flagged formulas fixed correctly (3-question sample) |
| Time per chapter | ~5 min on the GTX 1650 GPU, ~8 min on CPU only |

### Files

| File | What it does |
|---|---|
| `pdf_to_structured.py` | The pipeline: page reading, splitting, LaTeX steps, `run()` entry point |
| `glyph_match.py` | Template matcher for tiny formula crops (renders Times glyphs, compares shapes, detects sub/superscripts) |
| `latex_batch.py` | Batched pix2tex: groups same-size images and decodes them together |
| `ai_fallback.py` | OpenAI vision fallback with KaTeX check, one corrective retry, and an answer cache (`ai_cache.json`) |
| `katex_check.js` | Server-side KaTeX 0.16.11 render check (same version as the web page) |
| `app.py` | Flask web app: upload, progress, results, "Fix N with AI", job storage in `jobs/` |
| `static/index.html` | Results page: rendered maths, click a formula to compare with its image, clickable counts that open a per-formula panel |
| `find_structures.py` | (Chemistry prep) finds vector-drawn structures on a page and crops them |
| `experiments/molscribe_trial/` | MolScribe test: script, 13 test crops, results sheet (its environment was deleted; see section 6C to rebuild) |
| `pdf-extraction-notes.md` | The original notes. **Its script copy is out of date**; the code files are current |

### Problems found and fixed along the way

- **pix2tex output noise:** `\mathrm{x}`/`\mathbf{x}` wrappers, `*` read as a superscript, `∴` read as
  `\cdot\cdot`, `\inR` with no space after the command. All cleaned up in `clean_latex()`.
- **Tiny crops:** pix2tex returns garbage for single characters. That's why the symbol matcher exists.
- **Symbol matcher scripts:** `x − y` was read as `x^{-}y`. Fixed with measured position thresholds.
  Script pieces are matched only against digits, letters and + − *, and neighbouring script pieces are
  merged (`f^{-1}`, not `f^{-}^{1}`). **Existing results in `jobs/` still have the old readings until
  that PDF is re-run.**
- **Clipboard flood:** pix2tex copies every prediction to the Windows clipboard. Disabled in
  `load_latex_model()`.
- **Duplicate formulas:** identical crops are recognised once (about 20% fewer pix2tex calls).
- **Web app:** page jumping while images loaded, question links erasing the job from the URL,
  slow image loading over `localhost`, and jobs lost on server restart. All fixed.

---

## 2b. Figures and review/export (added 19 Sep 2026)

**Figure vs formula.** Diagrams, graphs and photos used to be pushed through LaTeX. In a biology paper
the AI replaced whole diagrams with their caption text, and pix2tex turned diagrams into nonsense LaTeX.
Now:
- `figure_reason()` in `pdf_to_structured.py` catches figures by code: coloured and at least 150×100 px,
  or long horizontal *and* vertical lines (axes, grids, tables). Calibrated on NCERT maths (0 of 1,946
  false positives), biology (8/8 diagrams) and chemistry (3/3 graphs).
- Figures skip LaTeX entirely and stay images at their exact position (`source: "figure"`).
- The AI prompt first classifies each image as formula / figure / blank and only transcribes formulas
  (`PROMPT_VERSION` 3). Tested: diagram → figure, graph → figure, equation block → correct LaTeX.
- pix2tex output from very tall crops (>250 px, 5+ stacked lines) goes to review, since pix2tex garbles these.
- Old results are fixed automatically when opened (`figures_checked` flag).

**Splitter fixes.**
- `Answer` / `Ans.` / `Sol.` headings are recognised; before, answers stayed inside the question text.
- Labels like "Question 4.1" keep their full label; before, every question in the chemistry paper was numbered 4.
- Each question records its page region(s); image positions and page sizes are saved.
- Printed page numbers in the top or bottom margin are dropped.
- `restructure()` re-splits old jobs in seconds and keeps their LaTeX. Opening a job in the review screen
  re-splits it automatically when `STRUCTURE_VERSION` has moved on, and saved edits are carried across by
  exercise + question number (anything that cannot be matched is parked in `review.json` under
  `orphanedEdits` instead of being lost).
- **Figures in a side column (fixed 20 Sep 2026, version 4).** Lines used to be built from words *and* images
  together, so a tall figure beside the text swallowed every line it overlapped. Result on Class 9 Maths Ch 7
  (Triangles): scrambled text, questions 3/4/6 missing, "EXERCISE 7.1" undetected, and Q2 holding Q1's
  solution and 33 images. Now rows are built from words only; an image joins the row it sits on, and a tall
  one (> 1.6 line heights) gets its own row. After the fix that chapter gives 8/8/5/6/4 questions per
  exercise (matches the book) and Q2 has 13 images and no foreign text. No change to the other PDFs
  (NCERT 5/74, biology 1/21, Number Systems 6/26 before and after).
- **Per-line highlight boxes (version 5).** Each question also stores one box per line (`lineRegions`), and the
  review screen highlights those instead of one page-wide rectangle, which looked far too tall when figures
  sit in a column beside the text. `sourceRegion` in the export is still the single box.

**Review & export screen** (`/review#job=<id>`, the "Review & export →" button on the results page):
- **Left:** the PDF (pdf.js), with the selected question highlighted in yellow.
- **Right, Document tab:** module, chapter, subject, path section; defaults for question type, level,
  topic and section name; PYQ settings; where answer text goes (auto: "Answer" → `answer`,
  "Solution" → `explanation`); image base URL.
- **Right, Questions tab:** each question's automatic content (rendered) plus fields to fill (topic, level,
  type, section name, PYQ, flagged, skip), with Ready / Needs input / Flagged filters and a
  "copy topic to following questions" shortcut.
- Edits autosave to `jobs/<id>/review.json`.
- **Export JSON** gives records in the question-bank schema, with ids that stay the same across exports.
  **Images (.zip)** gives every image the export refers to.
- Code: `review.py` (schema mapping, required fields: module, chapter, subject, topic, level, questionType)
  and `static/review.html`.
**Per-question fixes (added 20 Sep 2026)** — every question card has:
- **✕ on any image** (removes it from that question, e.g. a chapter banner),
- **✎ Edit text** (edit question and solution; maths in `$…$`, images as `![](img:NAME)` lines),
- **✨ Re-read with AI** (sends that question's highlighted box to the AI and replaces its text; your figures
  are re-inserted as the original crops, and images you removed stay removed). `place_figures()` puts each
  figure back where the PDF had it: under the same text it followed there (matched word-by-word with
  difflib, since the original OCR line may be garbled), at the end when nothing followed it, at the top when
  nothing preceded it. The PDF's position wins over the AI's own `[[FIGURE]]` marker; the marker is only used
  for a figure whose original spot is unknown.
- **✂ Crop → question / ✂ Crop → answer**: drag a box on the PDF and that part of the page is cut out
  (rendered at 200 DPI), saved as `manual_NNN.png` and appended to that part of the question. Recorded in
  `review.json` under `manualImages` with its page and position, so the export gives it an id and
  `imageCrops` entry like any other image (`kind: "cropped by hand"`). Esc cancels.
- **↺ Reset to automatic**.
Edits are stored per question in `review.json` (`stemOverride`, `solutionOverride`, `aiReread`) and the export
uses them, recomputing `images` / `explanationImages` / `imageCrops` from what the text still refers to.

**Re-read all with AI** (header button, or automatic at upload for scanned PDFs): one AI call per question
instead of one per formula. For Class 9 Maths Ch 1 that is 26 calls instead of 284 (~10x cheaper) and reads
better, because the AI sees the whole question. Measured: 23 questions in 20 s, 0 failures. Questions you
edited yourself are skipped. Note: pdfium is not thread-safe - region rendering is behind a lock.

**Topics from the syllabus (added 20 Sep 2026)** — `topics.json`, built by `tools/build_topics.py`:
- Downloads the official NCERT chapter PDFs (ncert.nic.in) into `downloads/ncert/`, reads each chapter's
  title (largest text on page 1) and its numbered section headings (`1.2 …`, science books `1.2.3 …`).
- Matches them to the chapters in `Chapters/`. **17 of 61** get official topics; the rest are missing from the
  current rationalised editions (Class 9 Maths is down to 8 chapters, Class 9 Science is a new book).
- `--suggest` asks the AI for the remaining **44** chapters' topics in NCERT section wording; those are marked
  "AI suggested - please check". Every chapter now has a list.
- In the review screen (Document tab): **Syllabus chapter** (guessed from the PDF name) and an editable
  **Topic list**. A question's Topic is a dropdown **restricted to that list**.
- **🏷 Fill topics & levels with AI** gives every question a topic from the list **and** a difficulty
  (easy / medium / hard, judged for that class) in the same call (batches of 12). `fields=topic,level` on the
  endpoint picks which of the two to fill. Tested on Class 9 Maths Ch 5: all 9 questions got both, spread
  3 easy / 5 medium / 1 hard.
- **🏷 Topic & level with AI** on a single question (21 Sep 2026) does the same for one question
  instead of the chapter: `POST /api/jobs/<id>/questions/<key>/topic`, which is `fill_topics()` with
  `keys=[key]` and `redo=True` - clicking it is an explicit instruction, so it overwrites what is
  there. The reply carries the chosen values, so the dropdowns update without reloading the page.
  Use it for a question added by hand, or one the batch run could not place. Same cost as one row of
  a batch. Needs a syllabus chapter on the Document tab; without one it answers 400 with what to do.
- **Expand all** in the question list opens every question at once instead of one at a time.

**Mixed PDFs: some pages scanned, some not (fixed 21 Sep 2026, version 7).** Class 9 Maths Ch 15
(Probability) has scanned pages 1-2 and a text layer from page 3 on. The scan is exported as two or three
image *strips*, so the old "one image covering half the page" test missed it and Q1-Q4 were never read.
`_is_scanned()` now adds up all the images on the page (>45% of the page = scanned). That chapter went from
6 questions to 10. Q9 and Q10 are genuinely absent - the book prints "Questions 9 and 10 are activities".
No change to the other chapters (74, 21, 28, 31 questions before and after).

**Bulk upload (added 20 Sep 2026).** `tools/bulk_upload.py <folder>` uploads every PDF in a folder to the
running app; they queue and appear in the web UI. It skips names already uploaded, fills the Document tab
from the folder names (module / subject / chapter), and with `--wait --tag --reread --export` it also tags
topics and levels, AI re-reads every question, and saves each chapter's JSON into `exports/`. `--limit N`
does only a few, `--no-ai` keeps it local-only. The home page now lists every upload
(`GET /api/jobs`) with its question count, live progress for anything running, and links to Results / Review.

**Full run of one chapter, 20 Sep 2026** (Class 10 Science Ch 1, Chemical Reactions and Equations, text PDF):
upload → extract (LaTeX + AI for 5 unsure images, about a minute) → Document fields → topics & levels →
export. 28 questions, all with answers, 0 missing required fields, official NCERT topic list matched
automatically, levels 11 easy / 17 medium. Saved to `exports/`.
Two things learned:
- **Headings behind a bullet image (fixed, version 6).** Three answers were missed because the line read
  `[[eq:bullet.png]] Answer 1:`. Heading matching now looks past leading images, and the image stays in the
  text. After the fix all 28 answers were found and nothing was flagged.
- **pix2tex cannot read chemical equations.** It produced `Fe.O.+2A1`, `3BaC_{2(\infty)}`,
  `2PbO_{i\alpha}`. One **Re-read all with AI** pass (28 calls, 20 s) fixed every one:
  `2PbO_{(s)} + C_{(s)} \rightarrow 2Pb_{(s)} + CO_{2(g)}`, `Fe_2O_3 + 2Al \rightarrow Al_2O_3 + 2Fe`.
  **So for chemistry chapters, run Re-read all with AI as a standard step.**
  Tested on Class 9 Maths Ch 1: 24 of 26 tagged in 8 s, all from the list; the 2 left empty were
  "find rational numbers between 3 and 4", where the list lacked a "Rational Numbers" topic — add it and re-run.

**Image references in the export:** every image has a stable id (kept in `review.json` under `imageIds`).
Records carry `questionImage` + `questionImageId` (never null when the question has any image, falling back to
the answer's first image), `imageIds`, `explanationImageIds`, and `imageCrops` entries with `id`, `file`, `url`,
`page`, `bbox`, `kind`.

- Not produced yet: `options`, `match`, `passageId`/`groupOrder`. The current splitter doesn't parse
  MCQ options or comprehension groups; that belongs to the Resonance extractor (section 6A).

---

## 2c. Scanned / outlined PDFs (added 20 Sep 2026)

8 of the 61 Class 9/10 chapters in `Chapters/` have no text layer: Class 10 Maths Ch 10, and Class 9
Maths Ch 1, 2, 3, 4, 7, 11, 15. Some are real scans (one page-sized image); the others have their text
turned into drawn outlines (hundreds of curves, no image).

- `_is_scanned()` spots such pages: fewer than 25 real words, plus a page-sized image or 200+ drawn shapes.
- `_ocr_tokens()`:
  - RapidOCR (`rapidocr_onnxruntime`, CPU, small) reads the words. Only confident, mostly-letter lines are
    kept as text.
  - Question/solution headings (Q.1, Sol., Answer, EXERCISE …) are always kept, even when maths follows
    them on the same line.
  - "Q.l" is fixed to "Q.1", and full-width characters are normalised.
  - All remaining ink (maths, unsure text, diagrams) is grouped into regions, which are saved as
    `pNNN_ocrNNN.png` images at their exact position.
  - Page frames, "Page | N" headers and top-of-page chapter banners are dropped.
  - Results are cached per page in `images/ocr_pNNN.json`.
- Crops from scanned pages **skip pix2tex and go to the AI**. Tested: pix2tex got ~4 of 12 right on
  the scan's italic font; the AI got ~11 of 14, and all of them after the prompt fix.
- Without AI enabled, these crops stay as images, which is safe.
- **AI prompt v4:** keeps item labels `\text{(ii)}` and leading ⇒ / ∴, which it used to drop.
- **Test, Class 9 Maths Ch 1 (11 pages):** all 6 exercises and 26 questions found (matches the book), every
  solution split out, 284 maths crops converted by AI, 7 figures kept, 0 left to check. Took 2 min 18 s.
- **Limits:**
  - Tiny, faint scan specks can still be misread (e.g. a blurred "0" read as "n").
  - OCR can misread symbols inside trusted prose (e.g. "q ≠ 0" came out as "q ± 0").

---

## 2d. Maths delimiters: `\(...\)` everywhere (21 Sep 2026)

The database wants inline maths as `\(...\)` and a whole line of maths as `\[...\]`. **The whole
project now uses that form** — the recogniser, the AI prompt, `structured.json`, `review.json`, the
review screen and the export. What you see in the edit box is exactly what the database gets.

It was built the other way first (`$...$` inside, converted only on export). That was the smaller
change, but it meant the edit box showed one form and the database held another, so it was switched.

What holds the convention:

| Where | What it does |
|---|---|
| `pdf_to_structured.py` `build_text_latex()` | wraps each recognised formula in `\(...\)` |
| `ai_fallback.py` `REGION_PROMPT` | asks the model for `\(...\)`, never `$` |
| `ai_fallback.py` `RE_MATH` / `maths_spans()` | finds `\(...\)`, `\[...\]` **and** `$...$` — the model still slips into `$` sometimes |
| `review.py` `to_paren_delims()` in `export()` | safety net: normalises any `$` that got through |
| `review.py` `_plain()` | strips all three forms when comparing an AI line with the original |
| `static/review.html` `renderRich()` | renders all three; `\[...\]` in KaTeX display mode |

Reading `$...$` is kept on purpose everywhere, so a stray `$` from the model degrades into
correct rendering rather than visible raw LaTeX.

**Migration.** `tools/migrate_job_delims.py` converted the stored jobs; `tools/convert_delims.py`
does the same for already-exported JSON. Both keep a `.bak` beside each file and both take
`--to dollar` to reverse. Run with the app stopped.

- 665 strings across 28 files in 27 jobs, plus `exports/Class10-Science-Ch01-Chemical-Reactions.json`.
- Verified the maths itself is untouched: 2254 distinct spans before, 2254 after, **identical sets**,
  nothing lost or invented — only the delimiters differ.
- KaTeX failures: 68 before, 68 after, the same ones. They are old pix2tex garbage
  (`\begin{array}{c c}{{\left(f o g\right)...`), not migration damage. Worth a separate clean-up.
- In the browser: all 36 spans of the Probability chapter render, 0 errors, 0 raw fallbacks.

**Two traps, both hit during this change:**

- The Bash tool collapses `\\` to `\` inside heredocs, so regexes written that way came out as
  `(?<!\)` and silently broke. Write files containing `\\` with the editor tools, not a heredoc.
- `\(` inside a **JavaScript template literal** is an unknown escape, and JS drops the backslash —
  the edit-box hint rendered as "between ( and )". Inside a backtick string it must be `\\(`.
  This bites the hint text, not the regexes (those are real regex literals).

---

## 2e. Pushing the export into MongoDB (added 21 Sep 2026)

`tools/push_mongo.py` sends `exports/*.json` to a collection. The export is plain JSON, so three
fields need converting into the types a MongoDB document normally uses; everything else, including
the `\(...\)` LaTeX, goes in unchanged.

| Export | MongoDB |
|---|---|
| `id` — 24-hex string | `_id` — ObjectId (the hex is already in ObjectId layout, so it converts cleanly) |
| `documentId` — 24-hex string | ObjectId |
| `createdAt` / `updatedAt` — ISO strings | BSON date |

```
$env:MONGODB_URI = "mongodb+srv://user:pass@cluster.mongodb.net"   # this terminal only
python tools/push_mongo.py exports --db questionbank --collection questions           # reports
python tools/push_mongo.py exports --db questionbank --collection questions --write   # sends
```

- The connection string is read from `MONGODB_URI` and never written into the project.
- Documents are **upserted by `_id`**, so a second run updates rather than duplicating. The ids come
  from `review.json` and stay the same across exports, which is what makes re-running safe.
- `--string-ids` keeps ids as strings, for a collection that does not use ObjectId.
- The client is opened with `tz_aware=True`, otherwise dates read back naive.

**LaTeX needs no special handling.** In the JSON file a backslash is written `\\`, which is just how
JSON spells one backslash; `json.load` gives back `\(x\)` and BSON stores exactly that. Verified by
encoding all 28 questions of the Chemical Reactions chapter with `bson.encode` and decoding them:
every `stem`, `answer`, `explanation`, `path`, `images`, `imageIds` and `_id` came back identical.
The only field that changes is the timezone marker on the dates (BSON always stores UTC).

It does break if the JSON is pasted into `mongosh` by hand, because the shell re-reads the
backslashes. Use this script, or `mongoimport --jsonArray`, and never paste.

**Images are not uploaded** - the export holds URLs built from `imageBaseUrl` on the Document tab
(default `images/`). Set that to wherever the files will actually be served from *before* exporting,
and upload the `Images (.zip)` contents there separately.

---

## 2f. Adding a question by hand (added 21 Sep 2026)

When the splitter misses a question entirely - an unusual layout, a page the OCR mangled - you can
draw a box round it instead of fighting the parser. **➕ Add question** in the review header turns the
cursor into a crosshair; drag a box round the question (and its answer, if it has one) and the box
goes to the AI exactly like *Re-read with AI* does. What comes back becomes a new question.

Cost is one whole-question read, about 1000 tokens.

**Where an added question lives.** In `review.json` under `addedQuestions`, **not** in
`structured.json`, which stays the pipeline's own output. `ensure_current()` injects them into the
doc as the last step, and `questions()` gives them a key of `add-<id>` instead of the usual
position-based `<exercise>-<index>`. That key is not derived from the PDF, so **a re-split cannot
move or lose them** - which also means the edit-remapping in `ensure_current()` has to carry
`add-*` keys across untouched (it drops anything it cannot match to a heading, and an added
question has no heading to match). That was a real bug during the build: the question survived the
re-split but its topic and edited text did not.

From then on it behaves like any other question: edit the text, crop images into it, re-read it with
AI, give it a topic and level, flag it, skip it, and it exports with its own ObjectId.

- The number is a guess (highest in that exercise + 1), so added questions get a **Question number**
  field to set the real one. `questionNumber` is now in `MANUAL_FIELDS`.
- The exercise is the one owning most questions on that page, so `sectionName` comes out right.
- **🗑 Delete question** removes it and its edits. It only appears on added questions; questions found
  in the PDF are hidden with `skip` instead, never deleted.
- If the AI reports a figure in the box, the `[[FIGURE]]` marker is stripped (there is no extracted
  image behind a box you drew, so the marker would otherwise land in the JSON as literal text) and a
  note says how many figures it saw, so they can be added with ✂ Crop.

`review.add_question()` / `review.remove_added_question()`;
`POST /api/jobs/<id>/questions/add` and `DELETE /api/jobs/<id>/questions/<key>`.

Tested on Chapter 15 - Probability: box drawn on page 1, AI returned the question text, it appeared
as No. 13 with an "added by hand" chip, exported as an 11th record with its own id and
`sourceRegion`, and deleting it put the job back to 10 questions. An added question with an edited
topic also survived a forced re-split.

---

## 2g. Wrong-class topic lists (found and fixed 22 Sep 2026)

**The bug.** A Class 9 Statistics question about bar graphs was tagged "Median of Grouped Data" - a
Class 10 topic. The tagger was working correctly; it had been handed the wrong list.

`guess_syllabus_key()` matched the PDF file name against chapter titles in `topics.json` and took the
best score. Class 9 and Class 10 both have `Chapter 14 - Statistics.pdf`, scoring exactly 1.0, so it
silently picked whichever came first - Class 10. Their topic lists share no entries at all, so every
question in the chapter was then tagged from the wrong syllabus. Same for Circles and Probability:
**3 jobs, 50 questions.**

**Three changes:**

1. **A tie is no longer guessed.** `guess_syllabus_key()` returns `None` when the runner-up scores
   within 0.01 of the winner. The Document tab then asks instead of quietly choosing, and `--tag`
   fails loudly with "pick a syllabus chapter on the Document tab first". A wrong list is worse than
   no list: no list stops you, a wrong one produces confident nonsense.
2. **`detect_class(job_dir)`** reads `(Class - IX)` from the top 16% of the first three pages and
   turns it into `Class 9`. Only the header band is read - question text says things like
   "30 students of Class VIII" and a whole-page scan picks that up instead. Many of these books never
   print the class, so `None` is a normal answer, and then rule 1 applies.
3. **`tools/bulk_upload.py` sends the class from the folder.** It always knew it
   (`Chapters/Class 9/Maths/...`) and was throwing it away. It now sets `syllabusChapter` to the exact
   `topics.json` key, which is the only fully reliable signal, and says so per chapter as it runs.

**Repair.** The three jobs were repointed at their Class 9 chapters and re-tagged
(`fill_topics(fields=("topic",), redo=True)`). Levels were kept - difficulty is judged for the class,
not taken from the list, so it was never wrong. Statistics 22/26, Circles 31/33, Probability 11/11;
the 6 the AI could not place can be done with **🏷 Topic & level with AI** on the question itself.

**Worth knowing:** topics within a chapter are still the model's judgement. Exercise 14.3 Q1 came back
as "Collection and Presentation of Data" where "Bar Graphs" arguably fits better. That is a
refinement to make in the dropdown, not a wrong-list error.

**How to check the rest.** Hash each `jobs/<id>/input.pdf`, look it up among `Chapters/**/*.pdf`, and
compare the folder's class with the job's `syllabusChapter` prefix. That is how these three were
found, and it is worth re-running after a bulk upload.

---

## 2h. Images and JSON together (added 22 Sep 2026)

### Bundles

A bundle is a chapter's questions and its pictures in one folder, instead of a loose JSON and a zip
that have to be paired up by hand:

```
exports/Probability/
    questions.json
    images/manual_006.png ...
```

**Buttons** (added 22 Sep 2026): **📦 Bundle** in the review header does the open chapter;
**📦 Bundle all (N)** above the list on the home page does every finished one, a chapter at a time
with progress. **Command line:** `python tools/export_bundle.py [job_id ...]`.

All three call `review.write_bundle()` through `POST /api/jobs/<id>/bundle`, so there is one
implementation and nothing to drift. It also deletes images that have left the chapter since the last
bundle, so the folder matches the JSON rather than accumulating.

### Buttons that write to MongoDB

**⬆ Push to MongoDB** in the review header, and **⬆ Push all to MongoDB** on the home page. Both
bundle first, then push. They appear **only when the server has `MONGODB_URI`**; without it the home
page says so instead of showing a button that cannot work. `MONGODB_DB` (default `questionbank`),
`MONGODB_COLLECTION` (default `questions`) and `MONGODB_IMAGE_URL` (default `/files/`) set the rest.
`GET /api/mongo` reports the settings without ever revealing the connection string.

The single-chapter button **runs the push twice**: once with `write: false` to find out exactly what
would happen, which fills the confirmation box ("11 questions, 6 images into GridFS - this writes to
your live database"), and again with `write: true` only if you accept. The bulk button confirms once
up front and then runs straight through, one chapter at a time so a failure stops there rather than
half-writing everything.

`push_mongo.push_records()` is shared by the buttons and the CLI, so both behave identically.

### Two ways to store the pictures

**GridFS - the images live in MongoDB.** `push_mongo.py --images gridfs` stores each file under the
id the export already gave it, so `imageIds`, `questionImageId` and `imageCrops[].id` point straight
at the stored file, and a second run re-uses it instead of duplicating.

```bash
python tools/push_mongo.py exports --db questionbank --collection questions \
    --images gridfs --image-url /files/ --write
```

`--image-url` rewrites **every** image reference to that prefix plus the id - the `images` list,
`questionImage`, `imageCrops[].url` and the `![](...)` ones inside the question text - so one route
serves them all. Your app reads a file back with `GridFS(db).get(ObjectId(id))`.

**Object store - the images live elsewhere, MongoDB holds URLs.** Leave `--images` out. Set
`imageBaseUrl` on the Document tab to where the files will be served from *before* exporting, then
upload the bundle's `images/` folder there. Cheaper to serve and CDN-friendly; the database stays
small. This is the usual production choice.

GridFS is worth it when you want one backup, one connection string and no second service to manage.
At 348 KB for a chapter's images, 61 chapters is roughly 20 MB - small either way.

### What was tested

No MongoDB, `mongod` or Docker on this machine, so **the live push has never been run against a real
server.** Do the first one from the command line without `--write`.

What was verified:

- Bundling, for real, through the button, the endpoint and the CLI - Probability: 11 questions,
  6 images, `\(...\)` LaTeX intact.
- Conversion, GridFS upload and URL rewriting against a stub GridFS: 6 files stored, a second pass
  stored 0 and re-used 6 (idempotent), every `_id`, `imageIds`, `questionImageId` and
  `imageCrops[].id` came out as an `ObjectId`, and all six inline `![](...)` references were
  rewritten to ids matching the record's `imageIds`.
- Both failure paths, through the Flask test client: no `MONGODB_URI` gives 400 and the button stays
  hidden; an unreachable server gives 502 with the driver's own message in the toast.
- Button wiring, with `/api/mongo` stubbed to report a configured server: both buttons appear, carry
  the right destination in their tooltip, and have handlers attached.

---

### Deleting a job (added 22 Sep 2026)

Every row in the home page list has a **Delete** link: it removes `jobs/<id>/` - the uploaded PDF,
the extracted images, `structured.json` and `review.json` with every edit. It cannot be undone, so
the confirmation names the file and says exactly what goes, including that anything already pushed to
MongoDB stays where it is.

`DELETE /api/jobs/<id>` refuses with 409 while a job is **queued or running**: the worker holds that
folder open, and half-deleting it underneath would leave a broken result behind. The link is not
rendered for those rows either. A missing job gives 404.

Useful for duplicates - the same chapter uploaded twice becomes two jobs with different ids, and
pushing both would write two copies of every question into MongoDB.

Tested: the throwaway job disappeared from the list and from disk; answering No to the confirmation
left all 27 jobs in place; queued and running jobs were refused and survived.

---

### The .env file (added 22 Sep 2026)

`.env` in the project root holds the connection string and anything else that should not be in the
code. `settings.load()` reads it when `app.py` starts and when a `tools/` script runs.

Copy `.env.example` to `.env` and fill it in:

```
MONGODB_URI="mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority"
MONGODB_DB=questionbank
MONGODB_COLLECTION=questions
MONGODB_IMAGE_URL=/files/
```

Restart `app.py` afterwards; the push buttons appear once `MONGODB_URI` is set.

- **A real environment variable always wins.** `OPENAI_API_KEY` is already set for the Windows user,
  so a stale line in `.env` cannot silently shadow it.
- **Quote a value containing `#`, a space or an `@`** - `p@ss#word` needs quotes or the `#` starts a
  comment. `export ` prefixes, single quotes and blank lines are all handled; a line with no `=` is
  ignored.
- `.env` is in `.gitignore` (written at the same time, along with `jobs/`, `exports/`, `Chapters/`,
  the caches and the model weights). `.env.example` has no values and is safe to share.
- `settings.describe()` masks a value for printing, so the startup line shows
  `mongodb+srv://***@cluster.mongodb.net` rather than the password. It splits on the **last** `@`,
  not the first - an `@` inside the password leaked part of it in the first version.
- `GET /api/mongo` reports only whether a URI is set plus the database and collection names. The
  connection string never reaches the browser.

Tested: quoted values, `export` prefixes, stray spaces, comments, malformed lines, a missing file,
real-environment precedence, and four masking cases with no leak. With a temporary `.env` the app
reported `{"available": true, "db": "envtestbank"}`; the file was then deleted, so only
`.env.example` remains.

---

### Images go to Supabase Storage (22 Sep 2026)

The pictures live in a Supabase bucket and MongoDB holds their public URLs. `supabase_store.py`
talks to the Storage REST API with `requests` - three calls, no extra dependency and no SDK.

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=eyJ...      # service role key; the anon key cannot write to storage
SUPABASE_BUCKET=images
```

- Objects are named **`<chapter>/<image id>.png`** - the same id the export puts in `imageIds`, so the
  name is stable and a chapter's files sit together in the dashboard.
- Uploads go up **without** `x-upsert`, so an object already there answers 409/"Duplicate" and is
  counted as re-used. A second push costs one request per image and no bytes.
- Every reference is rewritten to the public URL: the `images` lists, `questionImage`, `imageCrops`
  and the `![](...)` ones inside the question text.
- `--images` on the CLI: `auto` (the default - Supabase when `SUPABASE_URL` is set, else skip),
  `supabase`, `gridfs`, `skip`. The buttons use `auto`, and the confirmation names the bucket.
- `supabase_store.check()` runs before a CLI push: it catches a missing bucket, a refused key, and a
  **private** bucket, which uploads fine but whose URLs will not open without a signed link.
- `/api/mongo` reports `images` and `bucket` so the page can say where things are going. Neither the
  service key nor the connection string is ever sent to the browser.

GridFS is still there behind `--images gridfs`, for storing the pictures in MongoDB itself.

**Tested against the real project.** `check()` reported the bucket reachable and public. One test
image was uploaded to `_connection-test/`, a second attempt returned "exists" with no re-upload, the
public URL served back the identical 46,596 bytes, and the object was then deleted. A dry-run push of
the Probability chapter reached MongoDB and reported 11 questions and 6 images ready, writing
nothing. **A real push has still not been run** - press the button, or use the CLI without `--write`
first.

**Worth deciding:** `MONGODB_URI` ends in `/banks` but `MONGODB_DB` says `questionbank`, and the
explicit setting wins, so everything goes to `questionbank.questions`. Change `MONGODB_DB` if the
database in the URI was the one you meant.

---

### Matching the live question bank exactly (22 Sep 2026)

The target is **`banks.ingest_extracted_questions`** (6,342 documents), not a new database. What the
export produces is now the same shape, field for field - checked with `count_documents({f: {$exists}})`
for every field we emit, all 30 of which the collection already uses. **Nothing new is introduced.**

Four fields were dropped, because that collection has never had them:

| dropped | where the information went |
|---|---|
| `imageIds`, `questionImageId` | the picture is found by its `imageCrops[].url`; the stored object is named the bank's own way, so no id has to be carried (see below) |
| `explanationImages` | into `images`; the crop is filed as `type: "question"` like any other |
| `explanationImageIds` | as above |

`imageCrops` also changed shape to match: `{url, type, optionIndex, nx, ny, nw, nh}`, not the 0-1 box
we use internally. **nw/nh are the pixel width and height of the stored image file itself** - six of
the live crops were downloaded and measured against their own records, and the ratio was 1.000 every
time - with nx/ny the position in that same pixel space. So the scale is taken from the actual PNG
(`image_sizes()` measures each one) rather than assumed from a DPI; `CROP_DPI = 200` is only the
fallback for a file that cannot be opened. Verified: all six crops of the Probability chapter export
with nw/nh exactly equal to their file dimensions.

`type` is `"question"`, `"option"`, or **`"explanation"`** for a picture that belongs to the worked
solution. That last one is a value the collection has not used before, chosen deliberately: nothing in
the system stores a solution image today - `ingest_extracted_questions` has no field for one, no image
has ever appeared inside a `stem` or `explanation` string (0 of 6,342), and `Question.solution_image`
exists on 328 documents but is `None` on every one. NCERT worked solutions are full of diagrams, so
they had to go somewhere, and the reading app needs to learn this one type value.
`sourceRegion` stays 0-1, because the collection stores it that way.

`questionImage` is now only ever a picture belonging to the question itself. It used to fall back to
the first solution image when the question had none, which showed a working-out diagram as the
question's own picture.

**Image names in the bucket.** The bucket has no single convention - five shapes are in use, from
different code paths in the app: `<id>_ai_<n>_<n>_<ms>` (135), `<id>_question_<n>_<ms>_<ms>` (67),
`all_<id>_question_<n>_<n>_<ms>` (63), `<id>_option_<n>_<ms>` (56), `<id>_question_<n>_<ms>` (25),
plus a `persist_` prefix. What they share: the 24-hex head is the **question `_id`** - true for all
169 live crops, and never the `documentId` - the number after the kind is a position within the
question rather than the question number, and the trailing 13-digit numbers are upload times.

We write the simplest real shape, `<question _id>_question_<index>_<ms>`. The stamp is derived from
the question id's own first four bytes (an ObjectId's creation time in seconds x 1000), not the
clock, so it lands in the same range as the existing names but **never changes**: a second push finds
the object already in the bucket instead of uploading a copy and orphaning the first. Checked stable
across two consecutive exports. The `_<ms>_<ms>`, `all_` and `persist_` variants are not reproduced -
they record how a particular image happened to be saved, and nothing reads the name back, since the
record carries the full public URL.

**Options.** `split_options()` moves `(a) … (b) …` choices out of the stem into
`options: [{label, body, isCorrect}]`, and only for the types that have choices (`single_correct`,
`multiple_correct`, `assertion_reason`, `true_false`); everything else is untouched. It refuses
anything that is not a clean A, B, C… run, so the `(i)`/`(ii)` parts of a multi-part question are
left alone. `mark_correct()` ticks the choice the solution names ("Ans. (c)", "Answer: C",
"the correct option is (d)", "(b) is the correct answer"), and when a choice is ticked the record
follows the bank's convention: `answer` becomes the label and the working moves to `explanation`.
Nothing is ticked when it cannot tell.

**Delimiters.** The export no longer converts anything - the whole pipeline is already `\(...\)`,
which is what the collection holds. The one thing that can still emit `$...$` is the model ignoring
its prompt, so `to_paren_delims()` now runs where an AI answer arrives (`_reread_one`, `add_question`)
instead of on the way out.

Tested: 8 option-splitting cases and 8 correct-answer cases pass, including the ones that must be
refused; a full export of the Probability chapter produced no field the collection lacks; and a
dry-run push through the app reported 11 questions and 6 images with nothing written.

---

### Images in the text: matched now, switchable later (22 Sep 2026)

**How the bank stores pictures** - checked, not assumed:

- Nothing is inline. Across 6,342 documents, **no** `stem` or `explanation` contains a markdown
  image, a bare URL or an `<img>` tag.
- A question image is one URL in `questionImage` with `isQuestionImage: true`, and the stem still
  holds its full text: all 159 such documents have real text, none has an empty stem.
- Option images are a parallel array - `optionImages[i]` belongs to `options[i]` - and the option's
  own `body` is a readable placeholder such as `"[Image of option 1]"`, not a URL.
- So text fields are pure text, and position *within* the text is not represented at all.

Our PDFs are different: a figure often sits mid-question, and worked solutions have diagrams between
steps. `INLINE_IMAGES` in `review.py` is the switch:

- **`False` (default)** - the bank's way. `_strip_images()` removes the `![](...)` references from
  `stem`, `answer` and `explanation`. Every picture is still in `images` and `imageCrops`, with its
  page box, so nothing is lost from the record - only the place in the sentence.
- **`True`** - keeps the markdown where the picture actually appeared.

**Why flipping it later is safe.** The export is derived from `jobs/<id>/review.json`, which keeps the
inline positions for good, and a push upserts by `_id`, which also lives in that file. Verified both
ways: the ids are identical, the image object names are identical, and the image URLs are identical -
so a later re-push rewrites the same documents and re-uses the same uploads rather than creating a
second copy of anything. Turn the flag on when the reading app can render the markdown, re-export,
push again.

---

## 3. Machine setup (what was changed and why)

| Item | State | Why |
|---|---|---|
| NVIDIA driver | **616.92 Studio** (from nvidia.com; Dell's latest was 532.09) | PyTorch cu126 needs CUDA 12.6+. Dell's driver stops at 12.1 and fails with "device busy or unavailable" |
| PyTorch (main Python 3.13) | `2.14.0+cu126` | GPU build. Falls back to CPU automatically if the GPU isn't usable |
| Windows graphics setting | `C:\Python313\python.exe` → High Performance (GTX 1650) | Makes sure Python gets the NVIDIA card |
| OpenAI key | User environment variable `OPENAI_API_KEY` (a `sk-proj…` project key, set before this work) | Read automatically by the OpenAI library. Not stored in any project file |
| AI model | `gpt-5.4-mini` (override with the `OPENAI_LATEX_MODEL` env var) | |
| Node + KaTeX | `node_modules/katex@0.16.11` | Server-side render check |
| Trial Python | `.venv-ocsr/`, **deleted 19 Sep 2026** to free 2.7 GB | Was Python 3.11 + CPU PyTorch + MolScribe, isolated from the main setup. Recreate steps are in section 6C |

### GPU speed findings (samples of 30–120 formulas)

| Setup | Seconds per formula |
|---|---|
| CPU | ~0.50 |
| GPU, one at a time | ~0.35–0.47 |
| **GPU, batched (in use)** | **~0.30** |

Tried and rejected:
- **Padding images to a common width:** changes the output (only 42–81 of 120 matched, against 94 normally).
- **fp16:** crashes the sampler.

pix2tex decodes one token at a time, so the GPU gain is limited. Benchmarks were always run on
samples, never the whole PDF.

---

## 4. The Resonance chemistry PDF (analysed, not built yet)

File: `D:\Content\RESONANCE\CLASS-12\CHEMISTRY\Carbonyl Compounds ... APSP.pdf` (34 pages).

**How it differs from NCERT:**
- Made in **Microsoft Word**, so text is real text. But subscripts come out on the next line (`C H O` then `3 6`).
- **Structures and reaction schemes are vector drawings** (lines and curves), not images. Text
  extraction loses them completely: Q7's options come out as just `(1) (2) (3) (4)`.
- Four Parts: JEE Main test, NSEC, High Level Problems, JEE Advanced test. Sections include single
  correct, multi-correct, integer, match the column, and comprehension paragraphs.
- **Answer keys (p. 23–24) and worked solutions (p. 24–34) are at the end**, numbered per Part.

**Findings:**
- Question numbers are easy to locate: all 139 sit at x = 50 pt, and real questions are bold (which
  filters out numbered instruction lists). Part II has gaps to handle (missing 36–38, repeated 21–23).
- `find_structures.py` locates drawings by clustering nearby lines and curves plus the atom labels
  touching them. It works for most single molecules, but still merges some reaction schemes with text
  and sometimes picks up the header or footer.
- LaTeX and pix2tex are the wrong tools here. Chemistry needs pictures and/or SMILES, plus proper
  subscripts (CH₃).

### Structure-to-SMILES trial (MolScribe)

13 structures cropped from the PDF, scored against hand-written correct SMILES
(`experiments/molscribe_trial/ocsr_compare.png`):

| Type | MolScribe |
|---|---|
| Skeletal drawings (rings, chains) | 4 / 5 |
| Condensed formulas (CH₃–C(=O)–X) | 4 / 7 |
| Messy crop | 0 / 1 |
| **Total** | **8 / 13** in ~1.1 s per image on CPU |

Most misses involve **subscripted text labels**: CH₂ dropped from "CH₂CO₂H", OC₂H₅ read as OCH₃, an
H₃C methyl missed, and CH₃ read as a carbon with missing hydrogens. That last one is fixable by
filling in the missing hydrogens, which would give 9/13.

**DECIMER** (the other open-source option) could not be tested: its model is hosted only on
zenodo.org, which this network cannot reach.

**Conclusion:** MolScribe works as a free first draft, but it isn't trustworthy on its own for this
book. It needs a cross-check.

---

## 5. Lessons from this machine (8 GB RAM)

- **The laptop froze once from low memory.** Windows logged a "low virtual memory" event with
  `node.exe` (~2 GB, very likely the long Claude Code session), `app.py` (~1.8 GB) and a downloader
  (~1 GB) at the top. Stop `app.py` when idle, and start fresh Claude sessions now and then.
- **Kernel non-paged pool was ~930 MB** only 17 minutes after boot (normal is 150–300 MB), which
  suggests a driver memory leak. Suspects: the Intel Wireless-AC 9462 driver and the Dell/Alienware
  services. The Dell update page lists critical Intel Wi-Fi and Bluetooth driver updates (Jul 2024);
  install them.
- **Dell SupportAssist / TechHub use about 1 GB.** Services can be stopped in `services.msc`. Killing
  their processes in Task Manager just restarts them.
- **MolScribe on Windows:** it opens `multiprocessing.Pool(16)`. On Windows each worker re-imports the
  main script and loads its own model copy, which exhausts RAM. The trial replaces `Pool` with a
  one-at-a-time version and uses an `if __name__ == "__main__"` guard. Also:
  - The checkpoint was slimmed from 1.1 GB to 384 MB (`models/molscribe_slim.pth`, training state removed).
  - Loading uses memory-mapping (`mmap=True`).
  - A RAM watchdog stops the run below 350 MB free.
- **Hugging Face downloads** crawl at ~65 KB/s per connection on this network. A 32-connection ranged
  download reached ~2 MB/s. The file's SHA-256 was checked against Hugging Face's.
- **Upgrading to 16 GB RAM** (the G3 3500 has two slots) would remove most of these constraints.

---

## 6. Future scope

### A. Resonance / chemistry extractor (next big piece)

1. **Layout-based question splitting.** Use bold question numbers at the left margin to get each
   question's area. Track Part and Section headings, comprehension paragraphs ("Paragraph for
   Questions 17 to 19") and match-the-column tables.
2. **Save each question and option as a picture** (rendered crop), so nothing is ever lost.
3. **Clean text with real subscripts,** rebuilt from character size and baseline (CH₃, K₂Cr₂O₇).
   Split options (1)–(4) / (A)–(D).
4. **Parse the answer key and solutions** and link them to each question by Part and number.
   Solutions are mostly reaction mechanisms, so save them as pictures.
5. **SMILES for structures,** only as a verified extra:
   - MolScribe draft plus the automatic hydrogen fix;
   - cross-check with `gpt-5.4-mini` on the same picture;
   - agree → accept; disagree → "to check" in the review panel (same pattern as the formulas).
6. **Improve `find_structures.py`:** separate reaction arrows and conditions, drop the header/footer
   and stray neighbouring text, and split multi-molecule clusters. Consider **RxnScribe** for whole
   reaction schemes.
7. **Design for the whole series** if other chapters in `D:\Content\RESONANCE\` share this layout.

### B. NCERT pipeline improvements

- **Send suspicious pix2tex output to the AI too,** not only flagged items (e.g. `f` misread as
  `\int`, words split like `b u t`). One idea: ask the AI to verify a cheap sample, or flag outputs
  containing unusual commands.
- **Re-run old jobs** so they get the symbol-matcher script fix.
- **Speed:** a KV-cache decoder for pix2tex, parallel CPU workers, or a cloud GPU (Kaggle free tier /
  Modal) for bulk runs. Batching already gives ~1.6×.
- **Tables** (e.g. Exercise 1.4 operation tables) come out as rows of numbers. Detect and output real tables.
- **Other publishers:** make the heading regexes (`RE_EXERCISE`, `RE_QUESTION`, `RE_SOLUTION`) configurable per book.
- **Update `pdf-extraction-notes.md`** or point it at this file.

### C. Housekeeping

- `jobs/` keeps every upload and its results (199 MB for the 8 jobs so far). Add a cleanup button or rule.
- **Deleted on 19 Sep 2026:** `.venv-ocsr/` (2.7 GB) and the full MolScribe checkpoint
  `models/swin_base_char_aux_1m.pth` (1.1 GB). Kept: `models/molscribe_slim.pth` (367 MB, weights
  only), which is enough to run MolScribe.
- **To run the MolScribe trial again**, rebuild the environment:
  1. `pip install --user uv`, then `python -m uv venv .venv-ocsr --python 3.11`. If uv's link step
     fails, create the venv with the downloaded `python.exe` under `%APPDATA%\uv\python\`.
  2. Install `torch torchvision` from the CPU index, then
     `MolScribe OpenNMT-py==2.2.0` with `--no-deps`, then
     `timm==0.4.12 albumentations==1.1.0 rdkit SmilesPE pandas matplotlib configargparse`.
  3. Re-apply the two patches inside the venv:
     - replace `onmt/__init__.py` with a stub, so it doesn't import the torchtext-dependent `inputters`;
     - add `mmap=True, weights_only=False` to the `torch.load` call in `molscribe/interface.py`.
  4. Run `experiments/molscribe_trial/ocsr_trial.py`. It already replaces `multiprocessing.Pool`
     and has the RAM watchdog.
- Put the project under git before the chemistry work starts.
