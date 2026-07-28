# -*- coding: utf-8 -*-
"""Штатная работа не выдаётся за инцидент.

Скилл самозапускающийся: ложная тревога обходится дороже пропущенной находки,
поэтому этот класс проверок не менее обязателен, чем успешный разбор.
"""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase


class HealthyLog(ScriptCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.parsed, self.parsed_path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('healthy.log')])

    def test_no_dominant_error_template(self):
        errors = [g for g in self.parsed['groups'] if g['level'] in ('ERROR', 'FATAL')]
        self.assertEqual(errors, [], 'на логе без инцидента выделены ошибочные шаблоны')

    def test_no_incident_signatures(self):
        kinds = {s['kind'] for s in self.parsed['signatures']}
        self.assertNotIn('exception', kinds)
        self.assertNotIn('http', kinds)

    def test_summary_shows_warnings_but_no_errors(self):
        """Предупреждения показать надо, ошибки — взять неоткуда.

        Ни один шаблон не доминирует: три предупреждения по одному разу каждое.
        """
        code, out, err = self.run_script('parse_logs.py', [helpers.log('healthy.log')])
        self.assertEqual(code, 0, err)
        self.assertNotIn('· ERROR**', out, 'на штатном логе показан шаблон уровня ERROR')
        self.assertIn('топ 3 из 3 шаблонов', out)
        for group in self.parsed['groups']:
            if group['level'] == 'WARN':
                self.assertEqual(group['count'], 1,
                                 'предупреждение повторяется — это уже не штатная работа')

    def test_confidence_stays_low(self):
        payload = self.json_of('confidence.py', ['--parsed', self.parsed_path])
        self.assertEqual(payload['verdict'], 'гипотеза',
                         'на штатном логе уверенность поднялась выше гипотезы')
        self.assertLess(payload['confidence'], 0.40)

    def test_knowledge_base_match_is_not_by_signature(self):
        """Совпадение по базе, если и находится, то слабое — не по сигнатуре.

        Токенизатор режет `orders-api` на `orders` и `api`, и общее слово `api`
        вытягивает запись про `payment-api`. Совпадение при этом объясняется
        полями title/services, а не сигнатурой, и уверенность не поднимает
        (см. test_confidence_stays_low_with_knowledge_base). Точность поиска по
        общим словам — отдельный вопрос, к ложной тревоге разбора он не ведёт.
        """
        hits = self.json_of('kb_search.py',
                            ['--from-parsed', self.parsed_path, '--kb', helpers.KB])
        for hit in hits:
            self.assertFalse(any('сигнатура' in r for r in hit['reasons']),
                             'штатный лог совпал по сигнатуре: %s' % hit['reasons'])

    def test_confidence_stays_low_with_knowledge_base(self):
        hits = self.json_of('kb_search.py',
                            ['--from-parsed', self.parsed_path, '--kb', helpers.KB])
        kb_path = os.path.join(self.tmp, 'kb.json')
        with open(kb_path, 'w', encoding='utf-8') as fh:
            json.dump(hits, fh, ensure_ascii=False)
        payload = self.json_of('confidence.py',
                               ['--parsed', self.parsed_path, '--kb', kb_path])
        self.assertEqual(payload['verdict'], 'гипотеза')


if __name__ == '__main__':
    unittest.main()
