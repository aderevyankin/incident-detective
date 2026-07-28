# -*- coding: utf-8 -*-
"""Честное «не знаю»: несошедшиеся контуры не поднимают уверенность.

Единственный автоматически проверяемый способ поймать ложную уверенность до
того, как её увидит пользователь.
"""

import json
import os
import shutil
import unittest

import helpers
from helpers import ScriptCase


class Contours(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()
        self.parsed, self.parsed_path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('single_service.log')])

        # база знаний без похожего случая: одна запись про переполненный диск
        self.lonely_kb = os.path.join(self.tmp, 'kb-empty')
        os.makedirs(self.lonely_kb)
        shutil.copy(os.path.join(helpers.KB, 'INC-2026-05-003-disk-full.md'),
                    self.lonely_kb)

    def _kb_json(self, kb_dir, name):
        return helpers.json_to_file(
            self, self.tmp, name, 'kb_search.py',
            ['--from-parsed', self.parsed_path, '--kb', kb_dir])

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


class WeakKbHint(ScriptCase):
    """Подсказка слабого контура базы знаний называет действительную причину.

    «Похожего случая в базе нет — запиши этот» при найденной записи — прямая
    неправда: запись есть, ослаблен вклад, и агент по такой подсказке заведёт
    дубль вместо того, чтобы подтвердить исход или сверить условия.
    """

    OUTCOMES_KB = os.path.join(helpers.FIXTURES, 'kb_outcomes')
    UNVERIFIED = 'INC-2026-01-003'
    QUERY = ['квота', 'провайдера', '429', 'billing']

    def setUp(self):
        self.tmp = self.tmpdir()

    def _kb_file(self, name, hits):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(hits, fh, ensure_ascii=False)
        return path

    def _hit(self, record_id):
        hits = self.json_of('kb_search.py', self.QUERY + ['--kb', self.OUTCOMES_KB])
        return next(h for h in hits if h['id'] == record_id)

    def _kb_row(self, hits, extra=()):
        path = self._kb_file('kb.json', hits)
        payload = self.json_of('confidence.py', ['--kb', path] + list(extra))
        return next(r for r in payload['contours'] if r['key'] == 'kb')

    def test_found_record_is_not_reported_as_empty_kb(self):
        """Непроверенный исход плюс чужой стенд: вклад слаб, но запись найдена."""
        path = self._kb_file('kb.json', [self._hit(self.UNVERIFIED)])
        code, out, err = self.run_script(
            'confidence.py', ['--kb', path, '--stand', 'prod'])
        self.assertEqual(code, 0, err)
        self.assertIn('Что поднимет уверенность', out)
        self.assertNotIn('похожего случая в базе нет', out)
        self.assertIn(self.UNVERIFIED, out)

    def test_empty_kb_keeps_the_write_this_case_hint(self):
        row = self._kb_row([])
        self.assertEqual(row['value'], 0.0)
        code, out, err = self.run_script(
            'confidence.py', ['--kb', self._kb_file('kb.json', [])])
        self.assertEqual(code, 0, err)
        self.assertIn('похожего случая в базе нет', out)

    def test_unverified_outcome_hint_names_the_record(self):
        row = self._kb_row([self._hit(self.UNVERIFIED)])
        self.assertIn('исход', row['hint'])
        self.assertIn(self.UNVERIFIED, row['hint'])

    def test_foreign_stand_hint_asks_to_compare_conditions(self):
        row = self._kb_row([self._hit(self.UNVERIFIED)], ['--stand', 'prod'])
        self.assertLess(row['value'], 0.5, row)
        self.assertIn('сверь условия', row['hint'])

    def test_refuted_record_hint_does_not_ask_to_write_the_same_case(self):
        """Опровергнутая запись обнуляет вклад — но она найдена, и это не «базы нет»."""
        row = self._kb_row([self._hit('INC-2026-01-002')])
        self.assertEqual(row['value'], 0.0)
        self.assertIn('INC-2026-01-002', row['hint'])
        self.assertIn('опроверг', row['hint'])

    def test_hint_is_an_added_field_of_the_json_row(self):
        """JSON расширен совместимо: ключ добавлен, прежние поля на месте."""
        row = self._kb_row([self._hit(self.UNVERIFIED)])
        for key in ('key', 'label', 'weight', 'value', 'contribution', 'notes',
                    'warnings'):
            self.assertIn(key, row)
        self.assertIn('hint', row)
        payload = self.json_of('confidence.py',
                               ['--kb', self._kb_file('empty.json', [])])
        rows = {r['key']: r for r in payload['contours']}
        # у непроверенного контура подсказки нет: там своя, «контур не проверялся»
        self.assertIsNone(rows['code']['hint'])


if __name__ == '__main__':
    unittest.main()
