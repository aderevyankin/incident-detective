# -*- coding: utf-8 -*-
"""Время: зоны, шкала вывода, расхождение часов.

Порядок событий — то, на чём держится весь разбор: хронология называет вероятной
причиной самое раннее новое событие, цепочка — сервис с самой ранней ошибкой.
Ошибка в разборе времени не видна в выводе: он выглядит как обычно и указывает
не на тот сервис. Поэтому проверяется не «разобралось ли время», а «тот ли
сервис назван первым».
"""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase

ALPHA = helpers.log('clock_skew_alpha.log')
BETA = helpers.log('clock_skew_beta.log')


class Offsets(ScriptCase):
    """Смещение зоны, записанное в самом логе, доходит до порядка событий."""

    def test_records_in_different_zones_are_ordered_by_one_scale(self):
        """По голым числам из строк порядок вышел бы обратным."""
        parsed = self.json_of('parse_logs.py', [helpers.log('timezones.log')])
        firsts = sorted((g['first'], g['template']) for g in parsed['groups'])
        self.assertIn('ConnectionTimeoutError', firsts[0][1],
                      'первым должен быть payment: 18:00:01+03:00 — это 15:00:01 UTC')
        self.assertEqual(firsts[0][0], '2026-07-28 15:00:01')
        self.assertEqual(firsts[-1][0], '2026-07-28 15:00:12')

    def test_zone_suffix_does_not_leak_into_the_template(self):
        parsed = self.json_of('parse_logs.py', [helpers.log('timezones.log')])
        for grp in parsed['groups']:
            self.assertNotIn('+03:00', grp['template'])
            self.assertNotIn('Z ', grp['template'])

    def test_epoch_and_iso_from_one_cluster_agree(self):
        """Эпоха отсчитывается от UTC, а не от зоны машины разбора."""
        parsed = self.json_of('parse_logs.py', [helpers.log('epoch_iso.log')])
        self.assertEqual(parsed['stats']['first_ts'], '2026-07-28 15:00:00')
        self.assertEqual(parsed['stats']['last_ts'], '2026-07-28 15:00:06')
        self.assertEqual(parsed['stats']['tz_unknown'], 0)

    def test_time_scale_is_named(self):
        code, out, err = self.run_script('parse_logs.py', [helpers.log('timezones.log')])
        self.assertEqual(code, 0, err)
        self.assertIn('Шкала времени: UTC', out)


class MixedZones(ScriptCase):
    """Смешение записей с зоной и без неё называется, а не чинится молча."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_json(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def test_summary_warns_with_the_consequence(self):
        code, out, err = self.run_script('parse_logs.py', [helpers.log('mixed_zones.log')])
        self.assertEqual(code, 0, err)
        self.assertIn('Допущения о времени', out)
        self.assertIn('порядок событий', out.lower())

    def test_json_carries_the_mixing_flag(self):
        parsed = self.json_of('parse_logs.py', [helpers.log('mixed_zones.log')])
        self.assertTrue(parsed['stats']['mixed_timezones'])
        self.assertEqual(parsed['stats']['tz_known'], 1)
        self.assertEqual(parsed['stats']['tz_unknown'], 2)
        self.assertTrue(parsed['stats']['time_assumptions'])

    def test_timeline_repeats_the_warning(self):
        code, out, err = self.run_script(
            'timeline.py', ['--log', 'm=' + helpers.log('mixed_zones.log')])
        self.assertEqual(code, 0, err)
        self.assertIn('без неё', out)
        self.assertIn('может быть неверен', out)

    def test_chain_repeats_the_warning(self):
        code, out, err = self.run_script(
            'trace.py', ['--log', 'm=' + helpers.log('mixed_zones.log'),
                         '--id', 'req-mz01'])
        self.assertEqual(code, 0, err)
        self.assertIn('может быть неверен', out)

    def test_parse_made_by_the_previous_version_reads_as_unknown_zone(self):
        """Файл разбора без полей о зоне читается без ошибки и без домыслов."""
        old = {
            'stats': {'first_ts': '2026-07-28 15:00:00', 'last_ts': '2026-07-28 15:00:09'},
            'groups': [{'level': 'ERROR', 'count': 2, 'template': 'OldError: boom',
                        'first': '2026-07-28 15:00:00', 'last': '2026-07-28 15:00:09'}],
            'histogram': {},
        }
        path = self.write_json('old_parse.json', old)
        code, out, err = self.run_script('timeline.py', ['--parsed', 'old=' + path,
                                                         '--format', 'json'])
        self.assertEqual(code, 0, err)
        events = json.loads(out)
        self.assertTrue(events)
        self.assertFalse(events[0]['tz_known'],
                         'у старого разбора зона неизвестна — домысливать её нельзя')


class Clocks(ScriptCase):
    """Расхождение часов: одна логика на хронологию и на цепочку."""

    SOURCES_TRACE = ['--log', 'alpha=' + ALPHA, '--log', 'beta=' + BETA]

    def test_both_tools_give_the_same_estimate(self):
        chain = self.json_of('trace.py', self.SOURCES_TRACE + ['--check-clocks'])
        feed = self.json_of('timeline.py', self.SOURCES_TRACE + ['--check-clocks'])
        self.assertEqual(chain['clock_findings'], feed['clock_findings'],
                         'два инструмента дали разный ответ об одних источниках')
        self.assertTrue(chain['clock_findings'][0]['systematic'])
        self.assertEqual(chain['clock_findings'][0]['median'], 5.0)

    def test_timeline_does_not_correct_time_by_itself(self):
        before = self.json_of('timeline.py', self.SOURCES_TRACE)
        beta = [e for e in before if e['source'] == 'beta']
        self.assertTrue(beta)
        self.assertEqual(beta[0]['ts'], '2026-07-28 16:00:05',
                         'время сдвинуто без явно заданной поправки')

    def test_timeline_applies_an_explicit_offset(self):
        after = self.json_of('timeline.py', self.SOURCES_TRACE + ['--offset', 'beta=-5'])
        beta = [e for e in after if e['source'] == 'beta']
        self.assertEqual(beta[0]['ts'], '2026-07-28 16:00:00')

    def test_offset_for_unknown_source_is_a_startup_error(self):
        code, _out, err = self.run_script(
            'timeline.py', self.SOURCES_TRACE + ['--offset', 'gamma=-5'])
        self.assertNotEqual(code, 0)
        self.assertIn('gamma', err)
        self.assertNotIn('Traceback', err)

    def test_applied_offset_is_named_in_the_output(self):
        code, out, err = self.run_script(
            'timeline.py', self.SOURCES_TRACE + ['--offset', 'beta=-5'])
        self.assertEqual(code, 0, err)
        self.assertIn('поправка', out.lower())
        self.assertIn('в логе его не было', out)


class StableOrder(ScriptCase):
    """Повторный запуск на тех же данных даёт тот же порядок."""

    def test_timeline_order_is_the_same_twice(self):
        args = ['--log', 'alpha=' + ALPHA, '--log', 'beta=' + BETA]
        first = self.json_of('timeline.py', args)
        second = self.json_of('timeline.py', args)
        self.assertEqual([(e['ts'], e['source'], e['text']) for e in first],
                         [(e['ts'], e['source'], e['text']) for e in second])

    def test_equal_times_keep_their_order(self):
        """Два источника с совпадающим временем не меняются местами между запусками."""
        args = ['--log', 'alpha=' + ALPHA, '--log', 'copy=' + ALPHA]
        runs = [self.json_of('timeline.py', args) for _ in range(3)]
        keys = [[(e['ts'], e['source']) for e in run] for run in runs]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[1], keys[2])


if __name__ == '__main__':
    unittest.main()
