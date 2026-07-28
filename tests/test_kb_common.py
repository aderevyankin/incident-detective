# -*- coding: utf-8 -*-
"""kb_common как библиотека: разбор frontmatter и сходство сигнатур.

Единственный модуль скилла, который имеет смысл проверять импортом, а не
запуском процесса: это общий код без побочных эффектов, вызываемый из каждого
скрипта, и его баг рискует незаметно проехать во все контуры сразу.
"""

import sys
import unittest

import helpers

sys.path.insert(0, helpers.SCRIPTS)
import kb_common  # noqa: E402


class FrontmatterRoundTrip(unittest.TestCase):

    def test_simple_fields_round_trip(self):
        meta = {'id': 'INC-2026-07-001', 'title': 'Пул исчерпан', 'date': '2026-07-28',
                'status': 'resolved'}
        text = kb_common.dump_frontmatter(meta)
        back, body = kb_common.parse_frontmatter(text + '\n\nтело записи')
        self.assertEqual(back['id'], meta['id'])
        self.assertEqual(back['title'], meta['title'])
        self.assertEqual(back['date'], meta['date'])
        self.assertEqual(back['status'], meta['status'])
        self.assertEqual(body.strip(), 'тело записи')

    def test_list_fields_round_trip(self):
        meta = {'id': 'INC-2026-07-002', 'title': 'X', 'date': '2026-07-28',
                'services': ['payment-api', 'gateway'], 'tags': ['таймаут']}
        text = kb_common.dump_frontmatter(meta)
        back, _body = kb_common.parse_frontmatter(text)
        self.assertEqual(back['services'], meta['services'])
        self.assertEqual(back['tags'], meta['tags'])

    def test_long_signatures_round_trip_as_block_list(self):
        """Длинные строки (с запятой/двоеточием) идут блоком `- "..."`, не инлайн-списком."""
        sigs = ['ConnectionTimeoutError: pool exhausted, retrying',
                'HTTP 502 from payment-gateway: upstream closed']
        meta = {'id': 'INC-2026-07-003', 'title': 'X', 'date': '2026-07-28',
                'signatures': sigs}
        text = kb_common.dump_frontmatter(meta)
        back, _body = kb_common.parse_frontmatter(text)
        self.assertEqual(back['signatures'], sigs)

    def test_field_with_special_characters_round_trips(self):
        meta = {'id': 'INC-2026-07-004', 'title': 'Ошибка: превышен лимит [503]',
                'date': '2026-07-28'}
        text = kb_common.dump_frontmatter(meta)
        back, _body = kb_common.parse_frontmatter(text)
        self.assertEqual(back['title'], meta['title'])


class SignatureSimilarity(unittest.TestCase):

    def test_identical_signatures_are_maximally_similar(self):
        self.assertEqual(kb_common.signature_similarity(
            'ConnectionTimeoutError', 'ConnectionTimeoutError'), 1.0)

    def test_one_contained_in_the_other_is_close(self):
        score = kb_common.signature_similarity(
            'ConnectionTimeoutError', 'payment: ConnectionTimeoutError: pool exhausted')
        self.assertGreaterEqual(score, 0.85)

    def test_overlapping_words_score_between_zero_and_one(self):
        score = kb_common.signature_similarity(
            'connection pool exhausted for payment-api',
            'connection pool exhausted for orders-api')
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_unrelated_signatures_score_zero(self):
        score = kb_common.signature_similarity(
            'ConnectionTimeoutError: pool exhausted',
            'disk usage at 97 percent on /var/log')
        self.assertEqual(score, 0.0)

    def test_empty_signature_scores_zero(self):
        self.assertEqual(kb_common.signature_similarity('', 'ConnectionTimeoutError'), 0.0)
        self.assertEqual(kb_common.signature_similarity('ConnectionTimeoutError', ''), 0.0)


if __name__ == '__main__':
    unittest.main()
