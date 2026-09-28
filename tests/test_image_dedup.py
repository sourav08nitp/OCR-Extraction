import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pdf_to_structured as pts


class ImageDedupTests(unittest.TestCase):
    def setUp(self):
        self.boxes = {
            "p005_eq000.png": {"page": 5, "bbox": [441.1, 493.6, 470.1, 599.8]},
            "p005_eq001.png": {"page": 5, "bbox": [440.3, 491.9, 469.1, 598.0]},
            "p005_eq002.png": {"page": 5, "bbox": [480, 493.6, 509, 599.8]},
            "p006_eq000.png": {"page": 6, "bbox": [441.1, 493.6, 470.1, 599.8]},
            "p005_eq003.png": {"page": 5, "bbox": [444, 510, 465, 580]},
        }

    def test_only_nearly_identical_same_page_boxes_merge(self):
        self.assertEqual(pts.overlapping_image_aliases(self.boxes),
                         {"p005_eq001.png": "p005_eq000.png"})

    def test_existing_result_removes_one_reference_and_is_idempotent(self):
        names = list(self.boxes)
        sec = {"text": "Before " + " ".join(f"[[eq:{n}]]" for n in names) + " After",
               "equations": names, "equations_latex": {}}
        doc = {"image_boxes": self.boxes, "exercises": [{"questions": [
            {"question": sec, "solution": {"text": "Answer", "equations": []}}]}]}
        aliases = pts.deduplicate_images(doc)
        self.assertEqual(aliases, {"p005_eq001.png": "p005_eq000.png"})
        self.assertNotIn("p005_eq001.png", sec["equations"])
        self.assertNotIn("p005_eq001.png", sec["text_latex"])
        self.assertIn("p005_eq000.png", sec["text_latex"])
        before = copy.deepcopy(doc)
        self.assertEqual(pts.deduplicate_images(doc), {})
        self.assertEqual(doc, before)

    def test_new_extraction_skips_duplicate_before_cropping(self):
        page = Mock()
        page.extract_words.return_value = []
        page.images = [{"x0": b["bbox"][0], "top": b["bbox"][1],
                        "x1": b["bbox"][2], "bottom": b["bbox"][3]}
                       for b in list(self.boxes.values())[:2]]
        with patch.object(pts, "_is_scanned", return_value=False), \
                patch.object(pts, "_tokens_to_lines", side_effect=lambda tokens, *args: tokens):
            boxes = {}
            tokens = pts.page_to_lines(page, Mock(), 5, Path("unused"), crop=False, boxes=boxes)
        self.assertEqual(list(boxes), ["p005_eq000.png"])
        self.assertEqual(len(tokens), 1)

    def test_scanned_page_is_marked_even_when_ocr_has_no_image_crops(self):
        page = Mock()
        page.extract_words.return_value = []
        scanned = []
        with patch.object(pts, '_is_scanned', return_value=True), \
             patch.object(pts, '_ocr_tokens', return_value=[]), \
             patch.object(pts, '_tokens_to_lines', return_value=[]):
            self.assertEqual(pts.page_to_lines(page, Mock(), 2, Path('unused'),
                                               scanned_pages=scanned), [])
        self.assertEqual(scanned, [2])

    def test_initial_pass_uses_ai_if_local_ocr_crashes(self):
        from PIL import Image

        page = Mock(width=600, height=800)
        pdf = Mock()
        pdf.pages = [page]
        rendered = Mock()
        rendered.to_pil.return_value = Image.new('RGB', (600, 800), 'white')
        pdfium_page = Mock()
        pdfium_page.render.return_value = rendered
        pdfium_doc = Mock()
        pdfium_doc.__getitem__ = Mock(return_value=pdfium_page)
        pdfium_doc.__enter__ = Mock(return_value=pdfium_doc)
        pdfium_doc.__exit__ = Mock(return_value=None)
        with patch.object(pts.pdfplumber, 'open') as opened, \
             patch.object(pts.pdfium, 'PdfDocument', return_value=pdfium_doc), \
             patch.object(pts, 'page_to_lines', side_effect=RuntimeError('OCR failed')), \
             patch('ai_fallback.transcribe_page_lines', return_value=[
                 'EXERCISE 1.1', 'Q.1. What is 2 + 2?', 'Sol. 4']) as ai:
            opened.return_value.__enter__.return_value = pdf
            doc = pts._read_and_structure('fake.pdf', Path('unused'), crop=False, ai_ocr=True)
        self.assertEqual(doc['ai_ocr_pages'], [1])
        self.assertEqual(doc['scanned_pages'], [1])
        self.assertEqual(doc['exercises'][0]['questions'][0]['question']['text'], 'What is 2 + 2?')
        self.assertEqual(doc['exercises'][0]['questions'][0]['solution']['text'], '4')
        ai.assert_called_once()
