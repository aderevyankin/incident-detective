# -*- coding: utf-8 -*-
"""Штатная работа не выдаётся за инцидент.

Скилл самозапускающийся: ложная тревога обходится дороже пропущенной находки,
поэтому этот класс проверок не менее обязателен, чем успешный разбор.
"""

import os
import unittest

import helpers
from helpers import ScriptCase


class HealthyLog(ScriptCase):
    """Отсутствие инцидента на healthy.log подтверждено фикстурой tests/expected/healthy.json
    (records, error_groups=0, signature_kinds_absent) — здесь только то, что она не
    накрывает: markdown-сводка и честная низкая уверенность."""

    def setUp(self):
        self.tmp = self.tmpdir()
        self.parsed, self.parsed_path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('healthy.log')])

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
        _hits, kb_path = helpers.json_to_file(
            self, self.tmp, 'kb.json', 'kb_search.py',
            ['--from-parsed', self.parsed_path, '--kb', helpers.KB])
        payload = self.json_of('confidence.py',
                               ['--parsed', self.parsed_path, '--kb', kb_path])
        self.assertEqual(payload['verdict'], 'гипотеза')


if __name__ == '__main__':
    unittest.main()
