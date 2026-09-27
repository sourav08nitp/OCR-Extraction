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
