# -*- coding: utf-8 -*-
"""Задаваемое текущее время: прогон в другой день даёт тот же результат."""

import json
import os
import shutil
import tempfile
import unittest

import helpers
from helpers import ScriptCase

SYSLOG = helpers.log('syslog_no_year.log')
KNOWN = helpers.log('single_service.log')


class GivenNow(ScriptCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='triage-tests-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_year_for_timestamp_without_year(self):
        parsed = self.json_of('parse_logs.py', [SYSLOG], now='2026-07-28 20:00:00')
        self.assertEqual(parsed['stats']['first_ts'], '2026-07-28 12:33:44')

    def test_year_is_chosen_so_the_event_is_not_in_the_future(self):
        """Разбор в январе: июльская запись — из прошлого года, а не из будущего.

        Прежнее поведение подставляло год «сейчас» безусловно и отправляло
        декабрьский инцидент, разбираемый в январе, на одиннадцать месяцев вперёд.
        """
        parsed = self.json_of('parse_logs.py', [SYSLOG], now='2031-01-05 08:00:00')
        self.assertEqual(parsed['stats']['first_ts'], '2030-07-28 12:33:44')

    def test_substituted_year_is_named_as_an_assumption(self):
        parsed = self.json_of('parse_logs.py', [SYSLOG], now='2031-01-05 08:00:00')
        notes = ' '.join(parsed['stats'].get('time_assumptions') or [])
        self.assertIn('2030', notes, 'подставленный год не назван в допущениях')

    def test_window_without_date_counts_from_given_now(self):
        parsed = self.json_of('parse_logs.py', [KNOWN, '--since', '12:34'])
        self.assertEqual(parsed['stats']['filtered'], 15,
                         'окно «12:34» отсчиталось не от заданного дня')

    def test_relative_window_counts_from_given_now(self):
        wide = self.json_of('parse_logs.py', [KNOWN, '--since', '8h'])
        narrow = self.json_of('parse_logs.py', [KNOWN, '--since', '6h'])
        self.assertEqual(wide['stats']['filtered'], 23)
        self.assertEqual(narrow['stats']['filtered'], 0)

    def test_knowledge_base_record_gets_given_date(self):
        kb = os.path.join(self.tmp, 'kb')
        os.makedirs(kb)
        code, out, err = self.run_script(
            'kb_add.py', ['--title', 'Проверочная запись', '--kb', kb, '--dry-run'],
            now='2026-09-03 11:00:00')
        self.assertEqual(code, 0, err)
        self.assertIn('date: 2026-09-03', out)
        self.assertIn('id: INC-2026-09-001', out)
        self.assertEqual(os.listdir(kb), [], '--dry-run не должен ничего писать')

    def test_run_on_another_day_gives_the_same_result(self):
        """Фикстуры с явными датами не зависят от дня прогона вовсе."""
        today = self.json_of('parse_logs.py', [KNOWN], now='2026-07-28 20:00:00')
        next_year = self.json_of('parse_logs.py', [KNOWN], now='2027-02-11 03:00:00')
        self.assertEqual(json.dumps(today, sort_keys=True, ensure_ascii=False),
                         json.dumps(next_year, sort_keys=True, ensure_ascii=False))

    def test_without_variable_behaviour_is_unchanged(self):
        with_var = self.json_of('parse_logs.py', [KNOWN], now='2026-07-28 20:00:00')
        without = self.json_of('parse_logs.py', [KNOWN], now=None)
        self.assertEqual(with_var, without)

    def test_unparsed_value_is_an_error_not_a_fallback(self):
        code, out, err = self.run_script('parse_logs.py', [KNOWN], now='вчера вечером')
        self.assertNotEqual(code, 0, 'неразобранное значение должно быть ошибкой')
        self.assertIn('INCIDENT_NOW', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(out.strip(), '',
                         'при ошибке разбор не должен выдавать сводку на системном времени')

    def test_unparsed_value_stops_every_script(self):
        for name, args in (('kb_search.py', ['таймаут', '--kb', helpers.KB]),
                           ('kb_index.py', ['--kb', self.tmp]),
                           ('timeline.py', ['--log', 'a=' + KNOWN])):
            with self.subTest(script=name):
                code, _out, err = self.run_script(name, args, now='никогда')
                self.assertNotEqual(code, 0)
                self.assertIn('INCIDENT_NOW', err)


if __name__ == '__main__':
    unittest.main()
