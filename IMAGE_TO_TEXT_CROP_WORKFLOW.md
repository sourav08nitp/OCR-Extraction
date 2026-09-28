# PDF image to AI text: crop workflow

This guide describes the **Select PDF area → AI text** feature in Boards Extractor so it can be adapted in another project. The user selects one image in a question or answer, draws a rectangle over the corresponding PDF content, and gets editable text or rendered maths in that image's place.

## User flow

1. Show each embedded image inside its question or answer with a **Select PDF area → AI text** button.
2. On click, save any pending edits. Remember the question ID, image name, and destination (`stem` or `sol`). Navigate the PDF viewer to the source image's page and highlight its original position. Enable drag selection. **Escape** cancels without changing the question.
3. The user drags a box around exactly the desired text or formula on the PDF. Reject a tiny selection. Convert the rectangle to normalized page coordinates `[x0, y0, x1, y1]`, each between 0 and 1.
4. On mouse release, show **Reading selected area…** and send the question ID, image name, destination, page number, and normalized box to the server. Keep the UI responsive while the request runs. This is an asynchronous browser request; this implementation does **not** use a durable server job queue for a single image.
5. The server renders only the selected PDF rectangle to a PNG, asks the vision model to transcribe it, validates the resulting maths, and replaces only that image reference in the selected question part.
6. Show the returned text in Review. Keep a small original-image reference beside the converted text so the user can compare it with the PDF. The source PNG stays on disk. On any failure, leave the image and question text unchanged and show the error.

## Data needed

- A stable `questionKey` and `imageName`, such as `0-2` and `p015_ocr003.png`.
- The question part: `stem` (question) or `sol` (answer/solution).
- A saved source image and its PDF page and box. Automatic crops come from `structured.json` → `image_boxes`; user crops come from `review.json` → `manualImages`.
- PDF page dimensions from `structured.json` → `page_sizes`.
- Question text with an exact image token, for example `![](img:p015_ocr003.png)`.
- Saved manual overrides and source-image metadata. In this app these are `stemOverride`, `solutionOverride`, and `imageReadings` in `review.json`.

The coordinate systems differ:

| Coordinate | Meaning |
| --- | --- |
| Browser box | Pixel rectangle relative to the rendered PDF page |
| API `bbox` | Browser rectangle divided by rendered page width and height; values from 0 to 1 |
| PDF box | API coordinates multiplied by the PDF page width and height |

Use the PDF page element's rectangle for normalization, not the browser window or scroll container. Preserve the page number with the box.

## API contract

`POST /api/jobs/{jobId}/questions/{questionKey}/image-text`

Request:

```json
{
  "part": "sol",
  "name": "p015_ocr003.png",
  "page": 15,
  "bbox": [0.24, 0.42, 0.73, 0.49]
}
```

Success response:

```json
{
  "field": "solutionOverride",
  "text": "Volume = \\(6 \\times 5 \\times 4.5\\) m³\nHence, ...",
  "imageReadings": [
    {
      "name": "p015_ocr003.png",
      "part": "solution",
      "text": "\\(6 \\times 5 \\times 4.5\\) m³",
      "page": 15,
      "bbox": [0.24, 0.42, 0.73, 0.49],
      "selectionBBox": [0.24, 0.42, 0.73, 0.49]
    }
  ]
}
```

Here `bbox` in an actual response is the **original image** position. `selectionBBox` is the **user-drawn** region; they may differ. The numbers and text above are illustrative.

On failure, return an error such as `{"error":"AI returned invalid maths; the image was kept"}` and do not save an override.

## Server sequence

1. Check that the extraction job is complete and AI is configured.
2. Validate `part`, image filename, question key, page, and box. Accept only an image belonging to this question part. Reject path traversal and boxes outside the normalized page.
3. Confirm that the selected PDF page is the image's source page and that the exact image token is still present in the current text.
4. Convert normalized coordinates to PDF points, render that region to PNG, and call the vision transcription routine for question or solution text as appropriate.
5. Reject an empty response, an unconverted `[[FIGURE]]` marker, or maths that does not render in KaTeX. The model may retry a bad maths response once, but it must not replace the image with invalid output.
6. Reload the saved question after the AI call. If its text changed while the request ran, reject the stale result and ask the user to retry.
7. Replace **one occurrence** of `![](img:IMAGE_NAME)` in only `stemOverride` or `solutionOverride`. Save the AI text and an `imageReadings` entry containing original image position and selected box. Return the updated text and metadata.

Minimal pseudocode:

```python
def convert_selected_image(job, question_key, part, image_name, page, normalized_box):
    question, saved = load_question_and_review(job, question_key)
    current_text = saved.override(part) or question.automatic_text(part)
    token = f"![](img:{image_name})"
    require(token in current_text)
    require(page == source_page(image_name))
    pdf_box = normalized_to_pdf_box(normalized_box, page_size(page))
    crop_png = render_pdf_region(job.pdf, page, pdf_box)
    reading = vision_transcribe(crop_png, part=part)
    require(reading.text and not reading.contains_figure and not reading.katex_errors)
    require(load_current_text(job, question_key, part) == current_text)
    save_override(part, current_text.replace(token, reading.text, 1))
    save_image_reading(image_name, reading.text, original_box(image_name), normalized_box)
    return updated_override_and_readings()
```

## Browser sequence

```text
image button click
  → save pending edits
  → locate image source page and highlight its box
  → set crop state { questionKey, part, imageName }
PDF mousedown → create selection rectangle
PDF mousemove → resize rectangle
PDF mouseup
  → normalize rectangle to page coordinates
  → clear crop state
  → show reading status and POST /image-text
  → on success update only the returned field and source-reference metadata
  → on failure show error and keep the old image
Escape before mouseup → clear crop state with no request
```

Do not call the AI when the button is clicked. Call it only after the drag is complete. Disable another conversion for the same image while the request is pending. After success, render the text and the saved original crop reference; clicking the crop reference should navigate back to its PDF position. Manual text editing and reset should still work.

## Source files in this project

- [`static/review.html`](static/review.html): image button, PDF drag interaction, progress state, and Review rendering.
- [`app.py`](app.py): `/image-text` endpoint.
- [`review.py`](review.py): `transcribe_question_image`, PDF region rendering, validation, saved overrides, and source positions.
- [`ai_fallback.py`](ai_fallback.py): vision transcription and KaTeX checks.
- [`tests/test_workflow.py`](tests/test_workflow.py): tests for selected-region input, exact image replacement, source preservation, and unchanged state on failure.

The project uses `OPENAI_API_KEY_2` for AI. Keep the key in the server environment; never send it to the browser or put it in this document.
