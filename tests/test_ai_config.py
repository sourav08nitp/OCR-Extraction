import os
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_fallback


class AIKeyConfigurationTests(unittest.TestCase):
    def test_old_key_alone_does_not_enable_ai(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "old-test-key"}, clear=True):
            self.assertFalse(ai_fallback.available())
            with patch("openai.OpenAI") as constructor:
                with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY_2"):
                    ai_fallback.create_client(timeout=90)
                constructor.assert_not_called()

    def test_second_key_is_passed_explicitly(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "old-test-key", "OPENAI_API_KEY_2": "new-test-key"
        }, clear=True), patch("openai.OpenAI") as constructor:
            self.assertTrue(ai_fallback.available())
            self.assertIs(ai_fallback.create_client(timeout=120), constructor.return_value)
            constructor.assert_called_once_with(api_key="new-test-key", timeout=120, max_retries=3)

    def test_failed_cache_retries_and_manual_fix_bypasses_success_cache(self):
        image = Path("test-formula.png")
        for cached, force, expected_calls in (
            ({"kind": "formula", "latex": None, "error": "missing brace"}, False, 1),
            ({"kind": "formula", "latex": "old", "error": None}, True, 1),
            ({"kind": "formula", "latex": "old", "error": None}, False, 0),
        ):
            with self.subTest(cached=cached, force=force), \
                    patch.object(ai_fallback, "create_client"), \
                    patch.object(ai_fallback, "_Cache") as cache_class, \
                    patch.object(Path, "read_bytes", return_value=b"test-image"), \
                    patch.object(ai_fallback, "_ask", return_value=("formula", "fresh", "raw")) as ask, \
                    patch.object(ai_fallback, "katex_errors", return_value=[None]):
                cache_class.return_value.get.return_value = cached
                result = ai_fallback.transcribe([(image, "context")], force=force)[image]
                self.assertEqual(ask.call_count, expected_calls)
                self.assertEqual(result["latex"], "fresh" if expected_calls else "old")
                if expected_calls:
                    cache_class.return_value.put.assert_called_once()
