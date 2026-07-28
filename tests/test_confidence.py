# -*- coding: utf-8 -*-
"""Честное «не знаю»: несошедшиеся контуры не поднимают уверенность.

Единственный автоматически проверяемый способ поймать ложную уверенность до
того, как её увидит пользователь.
"""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase


class Contours(ScriptCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.parsed, self.parsed_path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('single_service.log')])

        # база знаний без похожего случая: одна запись про переполненный диск
        self.lonely_kb = os.path.join(self.tmp, 'kb-empty')
        os.makedirs(self.lonely_kb)
        shutil.copy(os.path.join(helpers.KB, 'INC-2026-05-003-disk-full.md'),
                    self.lonely_kb)

    def _kb_json(self, kb_dir, name):
        hits = self.json_of('kb_search.py',
                            ['--from-parsed', self.parsed_path, '--kb', kb_dir])
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(hits, fh, ensure_ascii=False)
        return hits, path

    def test_no_match_no_code_stays_hypothesis(self):
        hits, kb_path = self._kb_json(self.lonely_kb, 'kb-none.json')
        self.assertEqual(hits, [], 'в базе не должно быть похожего случая')
        payload = self.json_of('confidence.py',
                               ['--parsed', self.parsed_path, '--kb', kb_path])
        self.assertEqual(payload['verdict'], 'гипотеза')
        self.assertLess(payload['confidence'], 0.40)

    def test_missing_contours_are_named(self):
        _hits, kb_path = self._kb_json(self.lonely_kb, 'kb-none.json')
        code, out, err = self.run_script(
            'confidence.py', ['--parsed', self.parsed_path, '--kb', kb_path])
        self.assertEqual(code, 0, err)
        self.assertIn('Что поднимет уверенность', out)
        self.assertIn('Код', out)
        self.assertIn('Цепочка запроса', out)
        self.assertIn('контур не проверялся', out)
        self.assertIn('Это гипотеза', out)

    def test_contours_that_converge_raise_confidence(self):
        """Обратный случай: совпадение в базе поднимает вывод выше гипотезы."""
        hits, kb_path = self._kb_json(helpers.KB, 'kb-hit.json')
        self.assertTrue(hits, 'в фикстурной базе есть похожий случай')
        self.assertEqual(hits[0]['id'], 'INC-2026-07-001')
        payload = self.json_of('confidence.py',
                               ['--parsed', self.parsed_path, '--kb', kb_path])
        self.assertNotEqual(payload['verdict'], 'гипотеза')
        self.assertGreaterEqual(payload['confidence'], 0.40)

    def test_json_names_every_contour(self):
        payload = self.json_of('confidence.py', ['--parsed', self.parsed_path])
        keys = [row['key'] for row in payload['contours']]
        self.assertEqual(keys, ['logs', 'code', 'kb', 'trace'])
        unchecked = [row['key'] for row in payload['contours'] if row['value'] is None]
        self.assertEqual(sorted(unchecked), ['code', 'kb', 'trace'])


if __name__ == '__main__':
    unittest.main()
