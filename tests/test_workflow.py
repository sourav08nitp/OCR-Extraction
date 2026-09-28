"""Final sign-off and database tracking, using temporary jobs and a mocked database."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

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
                        patch.object(app, 'LEGACY_JOBS_DIR', self.root / 'legacy'),
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

    def test_existing_local_pdf_jobs_remain_visible_and_readable(self):
        legacy = self.root / 'legacy' / 'old123'
        (legacy / 'out').mkdir(parents=True)
        (legacy / 'input.pdf').write_bytes(b'original local PDF')
        (legacy / 'out' / 'structured.json').write_bytes((self.job / 'out' / 'structured.json').read_bytes())
        (legacy / 'meta.json').write_text(json.dumps({'filename': 'Old chapter.pdf', 'questions': 1}), encoding='utf-8')
        listed = self.client.get('/api/jobs')
        old = next(j for j in listed.json['jobs'] if j['id'] == 'old123')
        self.assertTrue(old['legacy'])
        self.assertEqual(old['filename'], 'Old chapter.pdf')
        result = self.client.get('/api/jobs/old123/result')
        self.assertEqual(result.status_code, 200)
        result.close()
        self.assertEqual(self.client.get('/api/jobs/old123/review').status_code, 200)
        pdf = self.client.get('/api/jobs/old123/pdf')
        self.assertEqual(pdf.data, b'original local PDF')
        pdf.close()
        self.assertEqual((legacy / 'input.pdf').read_bytes(), b'original local PDF')
        with patch('ocr_store.project_tree', return_value={'projects': [], 'sessions': []}):
            tree = self.client.get('/api/ocr/projects').json
        self.assertEqual(tree['localJobs'], [{'id': 'old123', 'label': 'Old chapter.pdf'}])

    def test_import_legacy_pdf_reuses_drive_id_and_preserves_review(self):
        legacy = self.root / 'legacy' / 'old123'
        (legacy / 'out').mkdir(parents=True)
        (legacy / 'input.pdf').write_bytes(b'%PDF-original local source')
        (legacy / 'out' / 'structured.json').write_bytes((self.job / 'out' / 'structured.json').read_bytes())
        (legacy / 'meta.json').write_text(json.dumps({'filename': 'Old chapter.pdf', 'questions': 1}), encoding='utf-8')
        saved = review.load_review(self.job)
        saved['questions']['0-0'] = {'stemOverride': 'My corrected question'}
        review.save_review(legacy, saved)
        original_review = (legacy / 'review.json').read_bytes()
        original_signature = review.review_signature(legacy)
        session = None

        def ensure(session_id, label, drive_file_id):
            nonlocal session
            session = {'_id': session_id, 'label': label, 'driveFileId': drive_file_id, 'projectId': None}
            return session

        with patch('drive_store.configured', return_value=True), \
             patch('drive_store.upload_pdf', return_value='drive-123') as upload, \
             patch('ocr_store.get_session', side_effect=lambda _: session), \
             patch('ocr_store.ingest_drive_file_id', return_value=None), \
             patch('ocr_store.ensure_imported_session', side_effect=ensure) as register:
            first = self.client.post('/api/jobs/old123/import-session')
            second = self.client.post('/api/jobs/old123/import-session')
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(second.status_code, 200, second.json)
        self.assertEqual(first.json['sessionId'], 'old123')
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(register.call_count, 2)
        self.assertEqual((legacy / 'input.pdf').read_bytes(), b'%PDF-original local source')
        self.assertEqual((legacy / 'review.json').read_bytes(), original_review)
        self.assertEqual(review.review_signature(legacy), original_signature)
        self.assertEqual(json.loads((legacy / 'meta.json').read_text())['driveFileId'], 'drive-123')
        with patch('ocr_store.project_tree', return_value={'projects': [], 'sessions': [{'id': 'old123'}]}):
            self.assertEqual(self.client.get('/api/ocr/projects').json['localJobs'], [])

    def test_import_legacy_pdf_uses_existing_ingest_drive_pdf(self):
        legacy = self.root / 'legacy' / 'old123'
        (legacy / 'out').mkdir(parents=True)
        (legacy / 'input.pdf').write_bytes(b'%PDF-original local source')
        (legacy / 'out' / 'structured.json').write_bytes((self.job / 'out' / 'structured.json').read_bytes())
        (legacy / 'meta.json').write_text(json.dumps({'filename': 'Old chapter.pdf'}), encoding='utf-8')
        saved = review.load_review(self.job)
        saved['document']['documentId'] = '0123456789abcdef01234567'
        review.save_review(legacy, saved)
        with patch('drive_store.configured', return_value=True), \
             patch('drive_store.upload_pdf') as upload, \
             patch('ocr_store.get_session', return_value=None), \
             patch('ocr_store.ingest_drive_file_id', return_value='existing-drive') as lookup, \
             patch('ocr_store.ensure_imported_session', return_value={
                 'label': 'Old chapter.pdf', 'driveFileId': 'existing-drive', 'projectId': None}):
            response = self.client.post('/api/jobs/old123/import-session')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['driveFileId'], 'existing-drive')
        lookup.assert_called_once_with('0123456789abcdef01234567')
        upload.assert_not_called()

    def test_import_without_drive_config_leaves_local_job_untouched(self):
        legacy = self.root / 'legacy' / 'old123'
        (legacy / 'out').mkdir(parents=True)
        (legacy / 'input.pdf').write_bytes(b'%PDF-original local source')
        (legacy / 'out' / 'structured.json').write_bytes((self.job / 'out' / 'structured.json').read_bytes())
        (legacy / 'meta.json').write_text(json.dumps({'filename': 'Old chapter.pdf'}), encoding='utf-8')
        with patch('drive_store.configured', return_value=False):
            response = self.client.post('/api/jobs/old123/import-session')
        self.assertEqual(response.status_code, 400)
        self.assertEqual((legacy / 'input.pdf').read_bytes(), b'%PDF-original local source')
        self.assertNotIn('driveFileId', json.loads((legacy / 'meta.json').read_text()))

    def test_duplicate_image_cleanup_preserves_edits_and_source_files(self):
        path = self.job / 'out' / 'structured.json'
        original = path.read_bytes()
        doc = json.loads(original)
        doc['exercises'][0]['questions'][0]['page_start'] = 5
        names = ['p005_eq000.png', 'p005_eq001.png']
        doc['image_boxes'] = {names[0]: {'page': 5, 'bbox': [441.1, 493.6, 470.1, 599.8]},
                              names[1]: {'page': 5, 'bbox': [440.3, 491.9, 469.1, 598.0]}}
        sec = doc['exercises'][0]['questions'][0]['question']
        sec.update(text='Pipe ' + ' '.join(f'[[eq:{n}]]' for n in names), equations=names)
        path.write_text(json.dumps(doc), encoding='utf-8')
        for name in names:
            (self.job / 'out' / 'images' / name).write_bytes(b'source image')
        saved = review.load_review(self.job)
        saved['questions']['0-0'] = {'stemOverride': 'Edited ' + ' '.join(f'![](img:{n})' for n in names),
                                    'solutionOverride': 'My answer', 'topic': 'Arithmetic'}
        review.save_review(self.job, saved)
        review.deduplicate_saved_images(self.job, doc)
        manual = review.load_review(self.job)['questions']['0-0']
        self.assertEqual(manual['stemOverride'].count('![](img:'), 1)
        self.assertIn(names[0], manual['stemOverride'])
        self.assertEqual(manual['solutionOverride'], 'My answer')
        self.assertEqual(manual['topic'], 'Arithmetic')
        self.assertTrue((self.job / 'out' / 'structured.before-image-dedup.json').is_file())
        self.assertTrue((self.job / 'review.before-image-dedup.json').is_file())
        for name in names:
            self.assertEqual((self.job / 'out' / 'images' / name).read_bytes(), b'source image')

    def test_ai_image_text_replaces_only_selected_image_and_keeps_source(self):
        path = self.job / 'out' / 'structured.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['page_sizes'] = {'1': [600, 800]}
        doc['image_boxes'] = {'p001_eq000.png': {'page': 1, 'bbox': [10, 20, 100, 40]}}
        sec = doc['exercises'][0]['questions'][0]['solution']
        text = 'Before [[eq:p001_eq000.png]] after [[eq:p001_eq001.png]]'
        sec.update(text=text, text_latex=text, equations=['p001_eq000.png', 'p001_eq001.png'])
        path.write_text(json.dumps(doc), encoding='utf-8')
        image = self.job / 'out' / 'images' / 'p001_eq000.png'
        image.write_bytes(b'original crop')
        before = path.read_bytes()
        with patch('ai_fallback.available', return_value=True), \
                patch.object(review, 'region_png', return_value=b'selected PDF area') as crop, \
                patch('ai_fallback.transcribe_region', return_value={
                    'question': '', 'solution': r'\(6 \times 5 = 30\)', 'katexErrors': []}) as ai:
            response = self.client.post('/api/jobs/abc123/questions/0-0/image-text',
                                        json={'part': 'sol', 'name': image.name, 'page': 1,
                                              'bbox': [.02, .03, .2, .08]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['text'], r'Before \(6 \times 5 = 30\) after ![](img:p001_eq001.png)')
        ai.assert_called_once_with(b'selected PDF area', part='sol')
        crop.assert_called_once_with(self.job / 'input.pdf',
                                     [{'page': 1, 'bbox': [12.0, 24.0, 120.0, 64.0]}], pad=0)
        saved = review.load_review(self.job)
        self.assertNotIn('stemOverride', saved['questions']['0-0'])
        source = saved['questions']['0-0']['imageReadings'][0]
        self.assertEqual(source['page'], 1)
        self.assertEqual(source['bbox'], [.0167, .025, .1667, .05])
        self.assertEqual(source['selectionBBox'], [.02, .03, .2, .08])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(image.read_bytes(), b'original crop')
        body = {k: saved[k] for k in ('document', 'questions')}
        self.client.put('/api/jobs/abc123/review', json=body)
        self.assertEqual(review.load_review(self.job)['questions']['0-0']['imageReadings'], [source])

    def test_extract_selected_figure_keeps_pdf_crop_and_other_question_part(self):
        path = self.job / 'out' / 'structured.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['page_sizes'] = {'1': [600, 800]}
        path.write_text(json.dumps(doc), encoding='utf-8')
        before = review.load_review(self.job)['document']
        with patch.object(review, 'region_png', return_value=b'full selected PDF crop'), \
             patch('ai_fallback.transcribe_region', return_value={
                 'question': 'Label A\n[[FIGURE]]\nLabel B', 'solution': '', 'katexErrors': []}):
            result = self.client.post('/api/jobs/abc123/questions/0-0/extract',
                                      json={'part': 'stem', 'page': 1, 'bbox': [.1, .1, .8, .5]})
        self.assertEqual(result.status_code, 200, result.json)
        saved = review.load_review(self.job)
        image_name = next(iter(saved['manualImages']))
        self.assertIn(f'![](img:{image_name})', saved['questions']['0-0']['stemOverride'])
        self.assertEqual((self.job / 'out' / 'images' / image_name).read_bytes(), b'full selected PDF crop')
        self.assertNotIn('solutionOverride', saved['questions']['0-0'])
        self.assertEqual(saved['document'], before)

    def test_unreadable_nonblank_selection_is_kept_as_image(self):
        import io
        from PIL import Image, ImageDraw

        path = self.job / 'out' / 'structured.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['page_sizes'] = {'1': [600, 800]}
        path.write_text(json.dumps(doc), encoding='utf-8')
        image = Image.new('RGB', (200, 100), 'white')
        ImageDraw.Draw(image).rectangle((20, 20, 150, 70), fill='black')
        output = io.BytesIO()
        image.save(output, 'PNG')
        with patch.object(review, 'region_png', return_value=output.getvalue()), \
             patch('ai_fallback.transcribe_region', return_value={
                 'question': '', 'solution': '', 'katexErrors': []}):
            response = self.client.post('/api/jobs/abc123/questions/0-0/extract',
                                        json={'part': 'sol', 'page': 1, 'bbox': [.1, .1, .8, .5]})
        self.assertEqual(response.status_code, 200, response.json)
        saved = review.load_review(self.job)
        self.assertIn('![](img:', saved['questions']['0-0']['solutionOverride'])
        self.assertNotIn('stemOverride', saved['questions']['0-0'])

    def test_scanned_page_audit_adds_only_missing_questions_and_is_repeatable(self):
        path = self.job / 'out' / 'structured.json'
        doc = json.loads(path.read_text(encoding='utf-8'))
        doc['page_sizes'] = {'1': [600, 800]}
        doc['scanned_pages'] = [1]
        doc['exercises'][0]['questions'][0].update(label='1', page_start=1)
        doc['exercises'][0]['questions'][0]['regions'] = [{'page': 1, 'bbox': [10, 20, 100, 80]}]
        path.write_text(json.dumps(doc), encoding='utf-8')
        found = [
            {'number': 1, 'question': 'Already extracted', 'solution': '2', 'bbox': [.1, .1, .8, .3]},
            {'number': 2, 'exercise': 'Exercise 1', 'question': 'Describe this.\n[[FIGURE]]',
             'solution': 'Answer: yes', 'bbox': [.2, .3, .9, .8]},
        ]
        with patch.object(review, 'region_png', return_value=b'original PDF region'), \
             patch('ai_fallback.transcribe_page_questions', return_value=found) as ai:
            first = review.ai_recover_missing_questions(self.job)
            second = review.ai_recover_missing_questions(self.job)
        self.assertEqual((first['added'], second['added']), (1, 0))
        self.assertEqual(ai.call_count, 2)
        saved = review.load_review(self.job)
        self.assertEqual(len(saved['addedQuestions']), 1)
        recovered = saved['addedQuestions'][0]
        self.assertEqual(recovered['number'], 2)
        self.assertEqual(saved['questions'][f"add-{recovered['id']}"]['flagged'], True)
        self.assertEqual(len(saved['manualImages']), 1)
        self.assertIn('![](img:', recovered['stem'])
        with patch.object(review, '_reread_one', return_value={
            'stemOverride': 'Read question 1', 'solutionOverride': '2',
            'aiReread': True, 'katexErrors': []}) as reread:
            review.ai_reread_all(self.job, workers=1)
        self.assertEqual(reread.call_count, 1)
        self.assertIn('![](img:', review.load_review(self.job)['addedQuestions'][0]['stem'])

    def test_ai_image_conversion_failure_keeps_question_unchanged(self):
        saved = review.load_review(self.job)
        doc = json.loads((self.job / 'out' / 'structured.json').read_text(encoding='utf-8'))
        doc['page_sizes'] = {'1': [600, 800]}
        doc['image_boxes'] = {'p001_eq000.png': {'page': 1, 'bbox': [10, 20, 100, 40]}}
        (self.job / 'out' / 'structured.json').write_text(json.dumps(doc), encoding='utf-8')
        saved['questions']['0-0'] = {'stemOverride': 'Before ![](img:p001_eq000.png) after'}
        review.save_review(self.job, saved)
        (self.job / 'out' / 'images' / 'p001_eq000.png').write_bytes(b'crop')
        before = (self.job / 'review.json').read_bytes()
        for text, errors in [('[[FIGURE]]', []), ('', []), (r'\(broken\)', ['invalid'])]:
            with patch('ai_fallback.available', return_value=True), \
                    patch.object(review, 'region_png', return_value=b'selected PDF area'), \
                    patch('ai_fallback.transcribe_region', return_value={
                        'question': text, 'solution': '', 'katexErrors': errors}):
                response = self.client.post('/api/jobs/abc123/questions/0-0/image-text',
                                            json={'part': 'stem', 'name': 'p001_eq000.png',
                                                  'page': 1, 'bbox': [.02, .03, .2, .08]})
            self.assertEqual(response.status_code, 400)
            self.assertEqual((self.job / 'review.json').read_bytes(), before)

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

    def test_exam_saved_exported_and_sent_to_ingest(self):
        from tools import push_mongo
        saved = review.load_review(self.job)
        saved['document']['exam'] = ' CBSE '
        response = self.client.put('/api/jobs/abc123/review', json={
            'document': saved['document'], 'questions': saved['questions']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(review.load_review(self.job)['document']['exam'], 'CBSE')
        records = review.export(self.job)
        self.assertEqual(records[0]['exam'], 'CBSE')
        self.assertFalse(records[0]['isPyq'])
        self.assertIsNone(records[0]['pyqExam'])
        mongo_record = push_mongo.to_document(records[0])
        self.assertEqual(mongo_record['exam'], 'CBSE')
        database = MagicMock()
        database['ingest_extracted_questions'].bulk_write.return_value = Mock(upserted_count=1, modified_count=0)
        result = push_mongo.push_records(records, database=database, collection='ingest_extracted_questions',
                                         write=True, images='skip', file_name='Test.pdf')
        self.assertTrue(result['wrote'])
        question_update = database['ingest_extracted_questions'].bulk_write.call_args.args[0][0]
        self.assertEqual(question_update._doc['$set']['exam'], 'CBSE')
        chapter_update = database['ingest_documents'].update_one.call_args.args[1]
        self.assertEqual(chapter_update['$set']['exam'], 'CBSE')
        saved = review.load_review(self.job)
        saved['document']['exam'] = ''
        review.save_review(self.job, saved)
        self.assertIsNone(review.export(self.job)[0]['exam'])

    def test_legacy_board_exam_is_normalized(self):
        from tools import push_mongo
        from exam_names import normalize_exam
        for value in ('Board', 'board', 'Boards', ' boards ', 'BOARDS'):
            self.assertEqual(normalize_exam(value), 'BOARDS')
        saved = review.load_review(self.job)
        saved['document']['exam'] = 'Board'
        review.save_review(self.job, saved)
        self.assertEqual(review.load_review(self.job)['document']['exam'], 'BOARDS')
        record = review.export(self.job)[0]
        self.assertEqual(record['exam'], 'BOARDS')
        record['exam'] = 'Board'  # an older bundle is normalized during a push too
        self.assertEqual(push_mongo.to_document(record)['exam'], 'BOARDS')
        database = MagicMock()
        push_mongo.push_document([push_mongo.to_document(record)], database, file_name='Test.pdf', write=True)
        self.assertEqual(database['ingest_documents'].update_one.call_args.args[1]['$set']['exam'], 'BOARDS')

    def image_fixture(self):
        from PIL import Image
        saved = review.load_review(self.job)
        saved['manualImages'] = {name: {'page': 1, 'bbox': [0, 0, 100, 100]}
                                 for name in ('question.png', 'working.png')}
        saved['questions'] = {'0-0': {'stemOverride': 'Question\n![](img:question.png)',
                                    'solutionOverride': 'Working\n![](img:working.png)'}}
        review.save_review(self.job, saved)
        for name in saved['manualImages']:
            Image.new('RGB', (10, 10), 'white').save(self.job / 'out' / 'images' / name)

    def test_answer_and_explanation_images_follow_text_destination(self):
        self.image_fixture()
        for destination, field, crop_type in (('explanation', 'explanationImages', 'solution'),
                                              ('answer', 'answerImages', 'answer')):
            saved = review.load_review(self.job)
            saved['document']['answerFrom'] = destination
            review.save_review(self.job, saved)
            record = review.export(self.job)[0]
            self.assertEqual(record[field], ['images/working.png'])
            other = 'answerImages' if field == 'explanationImages' else 'explanationImages'
            self.assertEqual(record[other], [])
            self.assertEqual(record['questionImage'], 'images/question.png')
            self.assertEqual({c['url']: c['type'] for c in record['imageCrops']},
                             {'images/question.png': 'question', 'images/working.png': crop_type})
        for heading, field in (('answer', 'answerImages'), ('solution', 'explanationImages')):
            path = self.job / 'out' / 'structured.json'
            doc = json.loads(path.read_text(encoding='utf-8'))
            doc['exercises'][0]['questions'][0]['solution_heading'] = heading
            path.write_text(json.dumps(doc), encoding='utf-8')
            saved = review.load_review(self.job)
            saved['document']['answerFrom'] = 'auto'
            review.save_review(self.job, saved)
            self.assertEqual(review.export(self.job)[0][field], ['images/working.png'])

    def test_supabase_rewrites_solution_images_and_reuses_upload_names(self):
        from tools import push_mongo
        self.image_fixture()
        bundle = review.write_bundle(self.job, name='Test')
        self.assertEqual(bundle['images'], 2)
        records = json.loads(Path(bundle['folder'], 'questions.json').read_text(encoding='utf-8'))
        qid = records[0]['id']
        database = MagicMock()
        database['ingest_extracted_questions'].bulk_write.return_value = Mock(upserted_count=1, modified_count=0)
        with patch('supabase_store.public_url', side_effect=lambda n: 'https://storage.example/images/' + n), \
                patch('supabase_store.upload', return_value='stored') as upload:
            res = push_mongo.push_records(records, Path(bundle['folder'], 'images'), database=database,
                                          collection='ingest_extracted_questions', write=True, images='supabase')
            names = [call.args[1] for call in upload.call_args_list]
            self.assertEqual(res['imagesStored'], 2)
            sent = database['ingest_extracted_questions'].bulk_write.call_args.args[0][0]._doc['$set']
            self.assertTrue(sent['explanationImages'][0].startswith('https://storage.example/'))
            self.assertEqual(sent['answerImages'], [])
            self.assertIn(sent['explanationImages'][0], sent['images'])
            self.assertIn(sent['explanationImages'][0], [c['url'] for c in sent['imageCrops']])
            self.assertEqual(records[0]['explanationImages'], ['images/working.png'])
            upload.reset_mock()
            upload.return_value = 'exists'
            again = push_mongo.push_records(records, Path(bundle['folder'], 'images'), database=database,
                                            collection='ingest_extracted_questions', write=True, images='supabase')
            self.assertEqual(again['imagesReused'], 2)
            self.assertEqual(names, [call.args[1] for call in upload.call_args_list])
            self.assertEqual(str(sent['_id']), qid)
        old = dict(records[0])
        old.pop('answerImages'); old.pop('explanationImages')
        old['imageCrops'] = [dict(c, type='explanation') if c['type'] == 'solution' else dict(c)
                             for c in old['imageCrops']]
        mapped = push_mongo.to_document(old)
        self.assertEqual(mapped['explanationImages'], ['images/working.png'])
        self.assertEqual(mapped['imageCrops'][1]['type'], 'solution')
        self.assertEqual(old['imageCrops'][1]['type'], 'explanation')
        saved = review.load_review(self.job)
        saved['document']['answerFrom'] = 'answer'
        review.save_review(self.job, saved)
        record = review.export(self.job)[0]
        push_mongo.rewrite_urls(record, {'images/working.png': 'https://storage.example/answer.png'})
        self.assertEqual(record['answerImages'], ['https://storage.example/answer.png'])

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
        cfg = {'available': True, 'db': 'test', 'collection': 'questions', 'driveConfigured': True}
        with patch.dict('sys.modules', {'push_mongo': backend}), \
                patch.object(app, '_mongo_settings', return_value=cfg), \
                patch('drive_store.upload_pdf', return_value='drive-file-id'):
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
