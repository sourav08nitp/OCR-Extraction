import os
import unittest
from unittest.mock import patch

import ai_fallback


class AIKeyConfigurationTests(unittest.TestCase):
    def test_old_key_alone_does_not_enable_ai(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "old-test-key"}, clear=True):
            self.assertFalse(ai_fallback.available())
            with patch("openai.OpenAI") as constructor:
                with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY2"):
                    ai_fallback.create_client(timeout=90)
                constructor.assert_not_called()

    def test_second_key_is_passed_explicitly(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "old-test-key", "OPENAI_API_KEY2": "new-test-key"
        }, clear=True), patch("openai.OpenAI") as constructor:
            self.assertTrue(ai_fallback.available())
            self.assertIs(ai_fallback.create_client(timeout=120), constructor.return_value)
            constructor.assert_called_once_with(api_key="new-test-key", timeout=120, max_retries=3)
