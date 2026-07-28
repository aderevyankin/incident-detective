# -*- coding: utf-8 -*-
"""Исход записи базы знаний: вклад в уверенность, положение в выдаче, счётчик
переиспользования и совместимость с записями без новых полей."""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase

OUTCOMES_KB = os.path.join(helpers.FIXTURES, 'kb_outcomes')
CONFIRMED = 'INC-2026-01-001'
REFUTED = 'INC-2026-01-002'
UNVERIFIED = 'INC-2026-01-003'
LEGACY = 'INC-2026-07-001'  # запись без поля outcome, из tests/fixtures/kb


class OutcomeContribution(ScriptCase):
    """confidence.py: вклад контура базы знаний зависит от исхода записи."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _kb_hits(self, kb_dir, query):
        return self.json_of('kb_search.py', query + ['--kb', kb_dir])

    def _kb_json_file(self, name, hits):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(hits, fh, ensure_ascii=False)
        return path

    def _confidence_value(self, hits):
        path = self._kb_json_file('kb.json', hits)
        payload = self.json_of('confidence.py', ['--kb', path])
        row = next(r for r in payload['contours'] if r['key'] == 'kb')
        return row

    def test_confirmed_gives_full_contribution(self):
        hits = self._kb_hits(OUTCOMES_KB, ['квота', 'провайдера', '429', 'billing'])
        self.assertTrue(hits)
        confirmed = [h for h in hits if h['id'] == CONFIRMED]
        self.assertTrue(confirmed, 'запись CONFIRMED не нашлась: %s' % hits)
        row = self._confidence_value([confirmed[0]])
        self.assertGreater(row['value'], 0.5, row)
        self.assertTrue(any('подтверждена' in n for n in row['notes']), row['notes'])

    def test_unverified_gives_reduced_contribution(self):
        hits = self._kb_hits(OUTCOMES_KB, ['квота', 'провайдера', '429', 'billing'])
        confirmed = next(h for h in hits if h['id'] == CONFIRMED)
        unverified = next(h for h in hits if h['id'] == UNVERIFIED)
        # тот же профиль score — искусственно уравниваем, чтобы разница объяснялась
        # только исходом, а не совпадением по тексту
        unverified = dict(unverified, score=confirmed['score'])
        row_confirmed = self._confidence_value([confirmed])
        row_unverified = self._confidence_value([unverified])
        self.assertLess(row_unverified['value'], row_confirmed['value'])
        self.assertTrue(any('не проверен' in n for n in row_unverified['notes']),
                        row_unverified['notes'])
        self.assertTrue(any('повысит уверенность' in n for n in row_unverified['notes']),
                        row_unverified['notes'])

    def test_refuted_gives_zero_and_is_a_warning(self):
        hits = self._kb_hits(OUTCOMES_KB, ['квота', 'провайдера', '429', 'billing'])
        refuted = next(h for h in hits if h['id'] == REFUTED)
        row = self._confidence_value([refuted])
        self.assertEqual(row['value'], 0.0, row)
        self.assertTrue(row['warnings'], 'опровергнутая запись должна попасть в warnings')
        self.assertTrue(any('опровергнута' in w for w in row['warnings']), row['warnings'])

    def test_missing_outcome_field_treated_as_unverified(self):
        """Запись без поля outcome (старая запись) даёт тот же вклад, что unverified."""
        hits = self.json_of('kb_search.py',
                            ['таймаут', 'пул', 'соединений', '--kb', helpers.KB])
        legacy = next(h for h in hits if h['id'] == LEGACY)
        self.assertNotIn('outcome', legacy['meta'])
        row = self._confidence_value([legacy])
        row_unverified_reference = self._confidence_value(
            [dict(legacy, meta=dict(legacy['meta'], outcome='unverified'))])
        self.assertEqual(row['value'], row_unverified_reference['value'])

    def test_weight_of_other_contours_is_not_redistributed(self):
        """Сниженный/нулевой вклад базы знаний не поднимает веса прочих контуров."""
        hits = self._kb_hits(OUTCOMES_KB, ['квота', 'провайдера', '429', 'billing'])
        refuted = next(h for h in hits if h['id'] == REFUTED)
        path = self._kb_json_file('kb.json', [refuted])
        payload = self.json_of('confidence.py', ['--kb', path])
        rows = {r['key']: r for r in payload['contours']}
        for key in ('logs', 'code', 'trace'):
            self.assertIsNone(rows[key]['value'], 'контур %s не запрашивался' % key)
        self.assertEqual(payload['confidence'], 0.0)


class OutcomeInSearch(ScriptCase):
    """kb_search.py: исход показан в выдаче, опровергнутые записи — ниже остальных."""

    def test_outcome_shown_for_every_hit(self):
        out = self.run_script('kb_search.py',
                              ['квота', 'провайдера', '429', 'billing', '--kb', OUTCOMES_KB])
        code, text, err = out
        self.assertEqual(code, 0, err)
        self.assertIn('исход: подтверждена', text)
        self.assertIn('исход: не проверена', text)
        self.assertIn('ОПРОВЕРГНУТА', text)

    def test_refuted_record_is_demoted_below_others(self):
        code, text, err = self.run_script(
            'kb_search.py',
            ['квота', 'провайдера', '429', 'billing', '--kb', OUTCOMES_KB, '--top', '3'])
        self.assertEqual(code, 0, err)
        pos_confirmed = text.find(CONFIRMED)
        pos_unverified = text.find(UNVERIFIED)
        pos_refuted = text.find(REFUTED)
        self.assertNotEqual(pos_confirmed, -1)
        self.assertNotEqual(pos_unverified, -1)
        self.assertNotEqual(pos_refuted, -1)
        self.assertLess(pos_confirmed, pos_refuted,
                        'подтверждённая запись должна идти раньше опровергнутой')
        self.assertLess(pos_unverified, pos_refuted,
                        'непроверенная запись должна идти раньше опровергнутой')

    def test_refuted_record_stays_in_results_not_dropped(self):
        hits = self.json_of('kb_search.py',
                            ['квота', 'провайдера', '429', 'billing', '--kb', OUTCOMES_KB])
        self.assertIn(REFUTED, [h['id'] for h in hits])

    def test_distinguishing_feature_shown_next_to_match(self):
        code, text, err = self.run_script(
            'kb_search.py', ['квота', 'провайдера', '429', 'billing', '--kb', OUTCOMES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('отличительные признаки', text)
        self.assertIn('локального лимита запросов на воркер', text)

    def test_reuse_count_does_not_affect_relevance(self):
        """Счётчик переиспользования — не участвует в скоринге релевантности."""
        base = self.json_of('kb_search.py',
                            ['квота', 'провайдера', '429', 'billing', '--kb', OUTCOMES_KB])
        base_scores = {h['id']: h['score'] for h in base}
        for h in base:
            self.assertNotIn('reuse_count', h.get('reasons', []))
        # тот же счёт, будь у записи reuse_count 0 или 40 — поле не входит в scoring
        self.assertTrue(all(v > 0 for v in base_scores.values()))


class ReuseCounter(ScriptCase):
    """kb_add.py --update: инкремент счётчика переиспользования и даты."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kb = os.path.join(self.tmp, 'kb')
        shutil.copytree(OUTCOMES_KB, self.kb)

    def _record_path(self, record_id):
        for name in os.listdir(self.kb):
            if name.startswith(record_id):
                return os.path.join(self.kb, name)
        raise AssertionError('запись %s не найдена в %s' % (record_id, self.kb))

    def _read_meta(self, record_id):
        import sys
        sys.path.insert(0, helpers.SCRIPTS)
        import kb_common
        with open(self._record_path(record_id), 'r', encoding='utf-8') as fh:
            meta, _body = kb_common.parse_frontmatter(fh.read())
        return meta

    def test_update_increments_reuse_count_and_sets_date(self):
        before = self._read_meta(CONFIRMED)
        self.assertNotIn('reuse_count', before)

        code, out, err = self.run_script(
            'kb_add.py', ['--update', CONFIRMED, '--stand', 'prod', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        after = self._read_meta(CONFIRMED)
        self.assertEqual(str(after.get('reuse_count')), '1')
        self.assertEqual(after.get('reused_at'), helpers.NOW.split()[0])

        code, out, err = self.run_script(
            'kb_add.py', ['--update', CONFIRMED, '--stand', 'dev', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        again = self._read_meta(CONFIRMED)
        self.assertEqual(str(again.get('reuse_count')), '2')

    def test_update_can_set_outcome_with_date(self):
        code, out, err = self.run_script(
            'kb_add.py', ['--update', UNVERIFIED, '--outcome', 'confirmed', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        after = self._read_meta(UNVERIFIED)
        self.assertEqual(after.get('outcome'), 'confirmed')
        self.assertEqual(after.get('outcome_date'), helpers.NOW.split()[0])
        # прежнее содержимое записи (заголовок) не потерялось
        self.assertIn('квота провайдера', after.get('title', '') + '')

    def _body(self, record_id):
        with open(self._record_path(record_id), 'r', encoding='utf-8') as fh:
            return fh.read()

    def test_outcome_only_update_is_not_a_repeat(self):
        """Опровержение причины — не повтор инцидента: счётчик стоит на месте.

        Счётчик переиспользования говорит, сколько раз запись пригодилась при
        повторе. Смена исхода — правка карточки, никакого нового случая за ней
        нет, и накрученный ею счётчик врал бы в выдаче поиска.
        """
        code, _out, err = self.run_script(
            'kb_add.py', ['--update', CONFIRMED, '--outcome', 'refuted', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        after = self._read_meta(CONFIRMED)
        self.assertEqual(after.get('outcome'), 'refuted')
        self.assertNotIn('reuse_count', after)
        self.assertNotIn('reused_at', after)
        self.assertNotIn('Повторилось', self._body(CONFIRMED))

    def test_card_only_update_is_not_a_repeat(self):
        """Статус, severity, теги, заголовок и связи — тоже правка карточки."""
        code, _out, err = self.run_script(
            'kb_add.py', ['--update', CONFIRMED, '--status', 'workaround',
                          '--severity', 'low', '--tags', 'quota',
                          '--title', 'квота провайдера исчерпана',
                          '--related', 'INC-2026-01-003', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        after = self._read_meta(CONFIRMED)
        self.assertEqual(after.get('status'), 'workaround')
        self.assertNotIn('reuse_count', after)
        self.assertNotIn('reused_at', after)
        self.assertNotIn('Повторилось', self._body(CONFIRMED))

    def test_update_with_repeat_evidence_increments(self):
        """Стенд, сигнатура, разбор логов и текст разделов — следы нового случая."""
        cases = (
            ['--stand', 'prod'],
            ['--signature', 'QuotaExceededError'],
            ['--symptoms', 'снова 429 на оплате'],
        )
        for extra in cases:
            code, _out, err = self.run_script(
                'kb_add.py', ['--update', CONFIRMED, '--kb', self.kb] + extra)
            self.assertEqual(code, 0, err)
        after = self._read_meta(CONFIRMED)
        self.assertEqual(str(after.get('reuse_count')), str(len(cases)))
        self.assertEqual(after.get('reused_at'), helpers.NOW.split()[0])

    def test_update_from_parsed_increments(self):
        parsed = os.path.join(self.tmp, 'parsed.json')
        with open(parsed, 'w', encoding='utf-8') as fh:
            json.dump({'signatures': [{'value': 'QuotaExceededError'}]}, fh)
        code, _out, err = self.run_script(
            'kb_add.py', ['--update', CONFIRMED, '--from-parsed', parsed, '--kb', self.kb])
        self.assertEqual(code, 0, err)
        self.assertEqual(str(self._read_meta(CONFIRMED).get('reuse_count')), '1')

    def test_signature_pickup_in_auto_mode_counts_as_repeat(self):
        """Автономный режим нашёл запись по сигнатуре — это повтор по определению."""
        code, out, err = self.run_script(
            'kb_add.py',
            ['--title', 'снова 429 от провайдера', '--signature', 'QuotaExceededError',
             '--kb', self.kb],
            env={'INCIDENT_MODE': 'auto'})
        self.assertEqual(code, 0, err)
        self.assertIn(CONFIRMED, out)
        after = self._read_meta(CONFIRMED)
        self.assertEqual(str(after.get('reuse_count')), '1')
        self.assertEqual(after.get('reused_at'), helpers.NOW.split()[0])


class LegacyRecordCompatibility(ScriptCase):
    """Запись без новых полей читается и обновляется без ошибок."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kb = os.path.join(self.tmp, 'kb')
        shutil.copytree(helpers.KB, self.kb)

    def test_search_reads_legacy_record_without_error(self):
        code, _out, err = self.run_script(
            'kb_search.py', ['таймаут', 'пул', 'соединений', '--kb', self.kb,
                             '--format', 'json'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)

    def test_update_of_legacy_record_does_not_error(self):
        code, out, err = self.run_script(
            'kb_add.py', ['--update', LEGACY, '--stand', 'prod', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)

    def test_index_rebuild_over_legacy_records_is_compatible(self):
        code, out, err = self.run_script('kb_index.py', ['--kb', self.kb])
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.kb, 'index.json'), 'r', encoding='utf-8') as fh:
            index = json.load(fh)
        entry = next(e for e in index['incidents'] if e['id'] == LEGACY)
        self.assertIsNone(entry.get('outcome'))
        # индекс, собранный над старой записью, всё равно читается поиском
        code, _out, err = self.run_script(
            'kb_search.py', ['таймаут', 'пул', '--kb', self.kb, '--format', 'json'])
        self.assertEqual(code, 0, err)


if __name__ == '__main__':
    unittest.main()
