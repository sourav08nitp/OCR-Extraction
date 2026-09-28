import os
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

import ai_fallback


class AIKeyConfigurationTests(unittest.TestCase):
    @staticmethod
    def _png():
        output = io.BytesIO()
        Image.new('RGB', (300, 180), 'white').save(output, 'PNG')
        return output.getvalue()

    def test_selected_crop_uses_complete_content_prompt_and_original_image_detail(self):
        reply = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
            content=json.dumps({'question': 'All three subparts', 'solution': ''})))], usage=None)
        client = Mock()
        client.chat.completions.create.return_value = reply
        with patch.object(ai_fallback, 'create_client', return_value=client), \
             patch.object(ai_fallback, 'note_usage'), \
             patch.object(ai_fallback, 'model_name', return_value='gpt-5.4-mini'):
            result = ai_fallback.transcribe_region(self._png(), part='stem')
        self.assertEqual(result['question'], 'All three subparts')
        content = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('EVERYTHING inside the rectangle', content[0]['text'])
        self.assertIn('question field', content[0]['text'])
        self.assertEqual(content[1]['image_url']['detail'], 'original')

    def test_crop_transcription_retries_control_character_before_saving(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                content=json.dumps({'question': '2Cu \x1f Heat', 'solution': ''})))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                content=json.dumps({'question': r'\(2\mathrm{Cu}\xrightarrow{\text{Heat}}2\mathrm{CuO}\)',
                                    'solution': ''})))], usage=None),
        ]
        with patch.object(ai_fallback, 'create_client', return_value=client), \
             patch.object(ai_fallback, 'katex_errors', return_value=[None]), \
             patch.object(ai_fallback, 'note_usage'):
            result = ai_fallback.transcribe_region(self._png(), part='stem')
        self.assertNotIn('\x1f', result['question'])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_truncated_page_audit_cannot_save_partial_questions(self):
        reply = SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(
            content=json.dumps({'questions': [{'number': 1}]})))], usage=None)
        client = Mock()
        client.chat.completions.create.return_value = reply
        with patch.object(ai_fallback, 'create_client', return_value=client), \
             patch.object(ai_fallback, 'note_usage'):
            with self.assertRaisesRegex(ValueError, 'cut off'):
                ai_fallback.transcribe_page_questions(self._png())

    def test_latex_repair_uses_source_image_and_rejects_broken_symbols(self):
        before = 'Copper reacts: 2Cu + O_2 \x1f Heat \x1e 2CuO\n![](img:source.png)'
        fixed = (r'Copper reacts: \(2\mathrm{Cu} + \mathrm{O}_2 '
                 r'\xrightarrow{\text{Heat}} 2\mathrm{CuO}\)' + '\n![](img:source.png)')
        bad = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
            content=json.dumps({'text': before})))], usage=None)
        good = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
            content=json.dumps({'text': fixed})))], usage=None)
        client = Mock()
        client.chat.completions.create.side_effect = [bad, good]
        with patch.object(ai_fallback, 'create_client', return_value=client), \
             patch.object(ai_fallback, 'katex_errors', return_value=[None]), \
             patch.object(ai_fallback, 'note_usage'), \
             patch.object(ai_fallback, 'model_name', return_value='gpt-5.4-mini'):
            result = ai_fallback.repair_text_latex(self._png(), before, 'answer')
        self.assertEqual(result['text'], fixed)
        content = client.chat.completions.create.call_args_list[0].kwargs['messages'][0]['content']
        self.assertIn('selected answer text', content[0]['text'])
        self.assertEqual(content[1]['image_url']['detail'], 'original')
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_latex_repair_preserves_image_reference_or_leaves_original(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                content=json.dumps({'text': r'Changed formula \(x+1\)' })))], usage=None)
        with patch.object(ai_fallback, 'create_client', return_value=client), \
             patch.object(ai_fallback, 'note_usage'):
            with self.assertRaisesRegex(ValueError, 'image reference'):
                ai_fallback.repair_text_latex(self._png(), 'Original ![](img:source.png)', 'answer')

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
