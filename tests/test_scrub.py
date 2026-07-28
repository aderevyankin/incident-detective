# -*- coding: utf-8 -*-
"""Очистка от секретов и персональных данных: kb_common.scrub_text.

Фикстуры здесь — заведомо поддельные значения (номер карты, придуманные
СНИЛС и телефон, тестовый JWT): проверка на форму, а не на настоящие данные.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, 'incident-detective', 'scripts')
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import kb_common  # noqa: E402
import helpers  # noqa: E402
from helpers import ScriptCase  # noqa: E402

# Карта, проходящая контрольную сумму Луна (тестовый номер Visa).
FAKE_CARD = '4111111111111111'
# Тот же по длине идентификатор, контрольную сумму НЕ проходящий.
FAKE_NON_CARD_16 = '1234567890123456'


class ScrubKinds(unittest.TestCase):

    def test_email(self):
        clean, counts = kb_common.scrub_text('user ivan.petrov@bank.ru failed')
        self.assertNotIn('ivan.petrov@bank.ru', clean)
        self.assertIn('<email>', clean)
        self.assertEqual(counts.get('email'), 1)

    def test_phone(self):
        clean, counts = kb_common.scrub_text('звонил клиент +7 926 123-45-67 по оплате')
        self.assertNotIn('926 123-45-67', clean)
        self.assertIn('<phone>', clean)
        self.assertEqual(counts.get('phone'), 1)

    def test_card_with_valid_checksum_keeps_last_four(self):
        clean, counts = kb_common.scrub_text('card %s declined' % FAKE_CARD)
        self.assertNotIn(FAKE_CARD, clean)
        self.assertIn('<card:…1111>', clean)
        self.assertEqual(counts.get('card'), 1)

    def test_account_number(self):
        clean, counts = kb_common.scrub_text('перевод на счёт 40817810099910004312 завис')
        self.assertNotIn('40817810099910004312', clean)
        self.assertIn('<account>', clean)
        self.assertEqual(counts.get('account'), 1)

    def test_snils(self):
        clean, counts = kb_common.scrub_text('снилс клиента 123-456-789 00 в заявке')
        self.assertNotIn('123-456-789 00', clean)
        self.assertIn('<person_id>', clean)
        self.assertEqual(counts.get('person_id'), 1)

    def test_passport(self):
        clean, counts = kb_common.scrub_text('паспорт 45 12 678901 приложен к тикету')
        self.assertNotIn('45 12 678901', clean)
        self.assertIn('<person_id>', clean)
        self.assertEqual(counts.get('person_id'), 1)

    def test_secret_named_field(self):
        clean, counts = kb_common.scrub_text('token=abc123secret')
        self.assertNotIn('abc123secret', clean)
        self.assertIn('token=<redacted>', clean)
        self.assertEqual(counts.get('secret'), 1)

    def test_password_field(self):
        clean, counts = kb_common.scrub_text('password: hunter2plus')
        self.assertNotIn('hunter2plus', clean)
        self.assertIn('password=<redacted>', clean)
        self.assertEqual(counts.get('secret'), 1)

    def test_token_shaped_jwt(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.fakefakefakefake'
        clean, counts = kb_common.scrub_text('в заголовке пришёл %s без подписи поля' % jwt)
        self.assertNotIn(jwt, clean)
        self.assertIn('<token>', clean)
        self.assertEqual(counts.get('token'), 1)

    def test_url_credentials(self):
        clean, counts = kb_common.scrub_text('источник https://svc:s3cr3t@internal.example/api')
        self.assertNotIn('svc:s3cr3t', clean)
        self.assertIn('<redacted>@internal.example', clean)
        self.assertEqual(counts.get('urlcred'), 1)


class ScrubRegression(unittest.TestCase):

    def test_bearer_token_is_masked_not_the_word_bearer(self):
        """Подтверждённый дефект: раньше маскировалось слово Bearer, а не токен."""
        token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart'
        clean, counts = kb_common.scrub_text('Authorization: Bearer %s' % token)
        self.assertNotIn(token, clean, 'токен пережил очистку')
        self.assertNotIn('Bearer', clean, 'в тексте осталось служебное слово Bearer')
        self.assertEqual(counts.get('secret'), 1)


class ScrubNonCard(unittest.TestCase):

    def test_sixteen_digits_without_valid_checksum_is_left_alone(self):
        clean, counts = kb_common.scrub_text('order id %s not found' % FAKE_NON_CARD_16)
        self.assertIn(FAKE_NON_CARD_16, clean, 'идентификатор без верной контрольной суммы не должен маскироваться как карта')
        self.assertNotIn('card', counts)


class ScrubStability(unittest.TestCase):

    def test_same_value_gets_same_label_within_one_text(self):
        text = 'ivan@example.com писал дважды, снова ivan@example.com не отвечает'
        clean, counts = kb_common.scrub_text(text)
        self.assertEqual(clean.count('<email>'), 2)
        self.assertEqual(counts.get('email'), 2)


class ScrubNonGoals(unittest.TestCase):
    """Перечень немаскируемого: диагностические данные, без которых разбор бессмыслен."""

    def _unchanged(self, text):
        clean, counts = kb_common.scrub_text(text)
        self.assertEqual(clean, text, 'текст изменился, хотя маскировать было нечего: %r' % counts)
        self.assertEqual(counts, {})

    def test_request_id_and_trace_id(self):
        self._unchanged('request_id=7f3a9c21-4b2e-4c1a-9f21-abcdef123456 обработан')

    def test_order_and_transaction_numbers(self):
        self._unchanged('заказ order-2026-0007123 транзакция txn_88213')

    def test_service_and_host_names(self):
        self._unchanged('сервис payment-api-3.stage.internal ответил медленно')

    def test_version(self):
        self._unchanged('после релиза 1.24.3 выросла latency')

    def test_code_path(self):
        self._unchanged('падение в src/db/pool.py:41')

    def test_ip_address(self):
        self._unchanged('источник запроса 10.0.0.5')


class ScrubKeepsSignatures(unittest.TestCase):
    """Записи остаются пригодными для поиска после очистки (задача 3.3)."""

    def test_signature_survives_scrub_alongside_pii(self):
        text = ('java.sql.SQLTimeoutException для пользователя ivan@example.com, '
                'http_status=504, request_id=7f3a9c21-4b2e-4c1a')
        clean, _counts = kb_common.scrub_text(text)
        self.assertIn('java.sql.SQLTimeoutException', clean)
        self.assertIn('http_status=504', clean)
        self.assertIn('request_id=7f3a9c21-4b2e-4c1a', clean)
        self.assertNotIn('ivan@example.com', clean)


class ScrubMatchesAcrossPaths(ScriptCase):
    """Один и тот же текст даёт одинаковый результат независимо от пути.

    Путь в базу знаний — `kb_add.py`, использующий `kb_common.scrub_text` через
    свою обёртку `scrub()`. Путь в текст тикета — работа модели по инструкции
    (SKILL.md, references/next-step.md), формулирующей то же правило и ту же
    форму меток. Единственный код, который можно прогнать напрямую, — путь в
    базу знаний; этот тест фиксирует, что он не имеет собственной логики поверх
    общей функции, то есть результат `kb_add.py` и результат прямого вызова
    `kb_common.scrub_text` (которым обязана руководствоваться и модель на пути
    тикета) совпадают дословно.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_kb_add_output_matches_direct_scrub(self):
        symptoms = 'клиент ivan@example.com жаловался на списание с card %s' % FAKE_CARD
        expected_clean, _counts = kb_common.scrub_text(symptoms)

        kb_dir = os.path.join(self.tmp, 'kb')
        code, out, err = self.run_script('kb_add.py', [
            '--title', 'проверка совпадения путей очистки',
            '--symptoms', symptoms,
            '--kb', kb_dir,
        ])
        self.assertEqual(code, 0, err)
        self.assertIn('id: INC-2026-07-001', out)

        names = [n for n in os.listdir(kb_dir) if n.startswith('INC-2026-07-001')]
        self.assertEqual(len(names), 1, 'ожидалась ровно одна новая запись: %s' % names)
        with open(os.path.join(kb_dir, names[0]), 'r', encoding='utf-8') as fh:
            saved = fh.read()
        self.assertIn(expected_clean, saved)
        self.assertNotIn('ivan@example.com', saved)
        self.assertNotIn(FAKE_CARD, saved)


if __name__ == '__main__':
    unittest.main()
