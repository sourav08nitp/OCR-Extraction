"""Final sign-off and database tracking, using temporary jobs and a mocked database."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import review


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name)
        self.job = self.root / 'abc123'
        (self.job / 'out' / 'images').mkdir(parents=True)
        (self.job / 'input.pdf').write_bytes(b'test PDF placeholder')
        doc = {'structure_version': review.pts.STRUCTURE_VERSION, 'figures_checked': True,
               'chapter': 'Test', 'page_sizes': {}, 'exercises': [{'title': 'Exercise 1', 'questions': [
                   {'number': 1, 'question': {'text': 'What is 1 + 1?', 'equations': []},
                    'solution': {'text': '2', 'equations': []}}]}]}
        (self.job / 'out' / 'structured.json').write_text(json.dumps(doc), encoding='utf-8')
        saved = review.load_review(self.job)
        saved['document'] = {'module': 'Class 9', 'subject': 'Maths', 'topic': 'Arithmetic', 'level': 'easy'}
        review.save_review(self.job, saved)
        self.patches = [patch.object(app, 'JOBS_DIR', self.root),
                        patch.object(review, 'BUNDLES', self.root / 'exports'),
                        patch.dict(app.jobs, {'abc123': {'status': 'done', 'filename': 'Test.pdf'}})]
        for p in self.patches:
            p.start()
        self.client = app.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def finalize(self):
        response = self.client.post('/api/jobs/abc123/finalize')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['workflow']['stage'], 'ready')

    def test_finalization_persists_and_edits_invalidate(self):
        before_export = review.load_review(self.job)['document'].copy()
        self.finalize()
        self.assertEqual(self.client.get('/api/jobs').json['jobs'][0]['workflow']['stage'], 'ready')
        saved = review.load_review(self.job)
        original_id = saved['document']['documentId']
        self.client.put('/api/jobs/abc123/review', json={'document': before_export, 'questions': {}})
        self.assertEqual(review.load_review(self.job)['document']['documentId'], original_id)
        body = {k: saved[k] for k in ('document', 'questions')}
        self.assertEqual(self.client.put('/api/jobs/abc123/review', json=body).json['workflow']['stage'], 'ready')
        self.client.post('/api/jobs/abc123/bundle')
        self.assertEqual(review.workflow_status(self.job)['stage'], 'ready')
        body['questions'] = {'0-0': {'stemOverride': 'Edited question'}}
        self.assertEqual(self.client.put('/api/jobs/abc123/review', json=body).json['workflow']['stage'], 'review')
        self.finalize()
        bundle = review.load_review(self.job)['workflow']['bundle']
        Path(bundle['folder'], 'questions.json').unlink()
        self.assertEqual(review.workflow_status(self.job)['stage'], 'review')

    def test_missing_image_blocks_finalization(self):
        saved = review.load_review(self.job)
        saved['questions'] = {'0-0': {'stemOverride': 'Question ![](img:missing.png)'}}
        saved['manualImages'] = {'missing.png': {'page': 1, 'bbox': [0, 0, 100, 100]}}
        review.save_review(self.job, saved)
        response = self.client.post('/api/jobs/abc123/finalize')
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn('image file(s) missing', response.json['error'])

    def test_preview_edit_updates_only_selected_text(self):
        path = self.job / 'out' / 'structured.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['exercises'][0]['questions'].append({'number': 2, 'question': {'text': 'Another question'},
                                                'solution': {'text': 'Another answer'}})
        path.write_text(json.dumps(doc), encoding='utf-8')
        saved = review.load_review(self.job)
        saved['questions'] = {'0-0': {'level': 'medium'}, '0-1': {'skip': True, 'stemOverride': 'Other edited text'}}
        review.save_review(self.job, saved)
        self.finalize()
        before = review.load_review(self.job)
        route = '/api/jobs/abc123/questions/0-0/text'
        unchanged = self.client.patch(route, json={'stemOverride': 'What is 1 + 1?', 'solutionOverride': '2'})
        self.assertEqual(unchanged.json['workflow']['stage'], 'ready')
        updated = self.client.patch(route, json={'stemOverride': 'Updated question ![](img:diagram.png)',
                                                 'solutionOverride': ''})
        self.assertEqual(updated.status_code, 200, updated.json)
        after = review.load_review(self.job)
        self.assertEqual(after['document'], before['document'])
        self.assertEqual(after['ids'], before['ids'])
        self.assertEqual(after['questions']['0-1'], before['questions']['0-1'])
        self.assertEqual(after['questions']['0-0']['level'], 'medium')
        self.assertEqual(after['questions']['0-0']['solutionOverride'], '')
        self.assertIn('img:diagram.png', after['questions']['0-0']['stemOverride'])
        self.assertEqual(updated.json['workflow']['stage'], 'review')
        self.assertEqual(self.client.patch(route, json={'skip': True}).status_code, 400)
        self.assertEqual(self.client.patch(route, json={'stemOverride': None}).status_code, 400)
        self.assertEqual(self.client.patch('/api/jobs/abc123/questions/unknown/text',
                                           json={'stemOverride': 'Text'}).status_code, 404)

    def test_incomplete_flagged_and_empty_exports_cannot_finalize(self):
        for changes in ({'topic': ''}, {'flagged': True}, {'skip': True}):
            saved = review.load_review(self.job)
            saved['questions'] = {'0-0': changes}
            if 'topic' in changes:
                saved['document']['topic'] = ''
            else:
                saved['document']['topic'] = 'Arithmetic'
            review.save_review(self.job, saved)
            self.assertEqual(self.client.post('/api/jobs/abc123/finalize').status_code, 400)

    def test_push_only_marks_successful_write(self):
        self.finalize()
        backend = Mock()
        backend.connect.return_value = (Mock(), Mock())
        cfg = {'available': True, 'db': 'test', 'collection': 'questions'}
        with patch.dict('sys.modules', {'push_mongo': backend}), patch.object(app, '_mongo_settings', return_value=cfg):
            for write, missing, expected in ((False, [], 'ready'), (True, ['missing.png'], 'ready'), (True, [], 'pushed')):
                backend.push_records.return_value = {'wrote': write, 'missing': missing, 'questions': 1}
                r = self.client.post('/api/jobs/abc123/push', json={'write': write})
                self.assertEqual(r.status_code, 200, r.json)
                self.assertEqual(r.json['workflow']['stage'], expected)
            backend.push_records.side_effect = RuntimeError('database unavailable')
            self.assertEqual(self.client.post('/api/jobs/abc123/push', json={'write': True}).status_code, 400)
            self.assertEqual(review.workflow_status(self.job)['stage'], 'pushed')
        saved = review.load_review(self.job)
        saved['questions'] = {'0-0': {'solutionOverride': 'Changed answer'}}
        review.save_review(self.job, saved)
        self.assertEqual(review.workflow_status(self.job)['stage'], 'review')


if __name__ == '__main__':
    unittest.main()
