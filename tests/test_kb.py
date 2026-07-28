# -*- coding: utf-8 -*-
"""База знаний: поиск по тексту и по сигнатурам, пересборка индекса."""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase

PAYMENT = 'INC-2026-07-001'
DISK = 'INC-2026-05-003'


class Search(ScriptCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_free_text_finds_expected_record(self):
        hits = self.json_of('kb_search.py',
                            ['таймаут', 'пул', 'соединений', 'payment-api',
                             '--kb', helpers.KB])
        self.assertTrue(hits, 'по свободному тексту ничего не нашлось')
        self.assertEqual(hits[0]['id'], PAYMENT)

    def test_signatures_from_parsed_find_expected_record(self):
        _parsed, path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('single_service.log')])
        hits = self.json_of('kb_search.py', ['--from-parsed', path, '--kb', helpers.KB])
        self.assertTrue(hits, 'по сигнатурам разбора ничего не нашлось')
        self.assertEqual(hits[0]['id'], PAYMENT)
        self.assertTrue(any('сигнатура' in r for r in hits[0]['reasons']),
                        'совпадение объяснено не сигнатурой: %s' % hits[0]['reasons'])

    def test_irrelevant_record_stays_out(self):
        _parsed, path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('single_service.log')])
        hits = self.json_of('kb_search.py', ['--from-parsed', path, '--kb', helpers.KB])
        self.assertNotIn(DISK, [h['id'] for h in hits],
                         'в выдачу попала запись про переполненный диск')

    def test_filter_by_service_is_hard(self):
        hits = self.json_of('kb_search.py',
                            ['таймаут', 'пул', '--service', 'logging-agent',
                             '--kb', helpers.KB])
        self.assertEqual(hits, [], 'фильтр по сервису не сработал как жёсткий')


class Index(ScriptCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kb = os.path.join(self.tmp, 'kb')
        shutil.copytree(helpers.KB, self.kb)

    def _rebuild(self):
        code, out, err = self.run_script('kb_index.py', ['--kb', self.kb])
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.kb, 'index.json'), 'r', encoding='utf-8') as fh:
            return json.load(fh), out

    def test_index_matches_records_on_disk(self):
        index, out = self._rebuild()
        on_disk = sorted(n for n in os.listdir(self.kb) if n.endswith('.md'))
        self.assertEqual(index['count'], len(on_disk))
        self.assertEqual(sorted(e['file'] for e in index['incidents']), on_disk)
        self.assertIn('3 записей', out)
        ids = {e['id'] for e in index['incidents']}
        self.assertIn(PAYMENT, ids)

    def test_search_by_index_matches_search_by_markdown(self):
        """Ответ поиска не должен зависеть от того, читан индекс или markdown."""
        query = ['таймаут', 'пул', 'соединений', '--kb', self.kb]
        before = self.json_of('kb_search.py', query)
        self._rebuild()
        after = self.json_of('kb_search.py', query)
        self.assertEqual([(h['id'], h['score']) for h in before],
                         [(h['id'], h['score']) for h in after])

    def test_stale_index_is_reported(self):
        self._rebuild()
        os.remove(os.path.join(self.kb, 'INC-2026-05-003-disk-full.md'))
        code, _out, err = self.run_script(
            'kb_search.py', ['таймаут', 'пул', '--kb', self.kb, '--format', 'json'])
        self.assertEqual(code, 0, err)
        self.assertIn('индекс базы знаний устарел', err)


if __name__ == '__main__':
    unittest.main()
