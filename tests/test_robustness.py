# -*- coding: utf-8 -*-
"""Устойчивость ко входу: скрипт объясняется, а не падает трассировкой."""

import os
import unittest

import helpers
from helpers import ScriptCase

EDGE = os.path.join(helpers.LOGS, 'edge')

CASES = [
    ('empty.log', 'пустой файл'),
    ('oneline.log', 'одна строка без времени и уровня'),
    ('no_timestamps.log', 'файл без таймстемпов'),
    ('broken_encoding.log', 'битая кодировка'),
    ('long_line.log', 'строка чрезмерной длины'),
]


class EdgeInputs(ScriptCase):

    def test_parse_survives_every_edge_case(self):
        for name, why in CASES:
            with self.subTest(fixture=name, случай=why):
                code, out, err = self.run_script('parse_logs.py',
                                                 [os.path.join(EDGE, name)])
                self.assertEqual(code, 0, '%s: код возврата %d\n%s' % (name, code, err))
                self.assertNotIn('Traceback', err, '%s: трассировка\n%s' % (name, err))
                self.assertIn('# Сводка логов', out, '%s: сводки нет' % name)

    def test_json_output_survives_every_edge_case(self):
        for name, why in CASES:
            with self.subTest(fixture=name, случай=why):
                parsed = self.json_of('parse_logs.py', [os.path.join(EDGE, name)])
                self.assertIn('stats', parsed)

    def test_long_line_is_cut_not_dropped(self):
        parsed = self.json_of('parse_logs.py', [os.path.join(EDGE, 'long_line.log')])
        self.assertEqual(parsed['stats']['records'], 2)
        for group in parsed['groups']:
            self.assertLessEqual(len(group['template']), 200,
                                 'шаблон не обрезан до читаемого размера')

    def test_missing_source_is_explained(self):
        code, _out, err = self.run_script('parse_logs.py', ['/нет/такого/файла.log'])
        self.assertNotEqual(code, 0)
        self.assertNotIn('Traceback', err)
        self.assertIn('Нечего парсить', err)

    def test_empty_log_says_there_is_nothing(self):
        code, out, _err = self.run_script('parse_logs.py', [os.path.join(EDGE, 'empty.log')])
        self.assertEqual(code, 0)
        self.assertIn('таймстемпы не распознаны', out)


class Orchestrator(ScriptCase):
    """triage.py проходит цепочку целиком и не падает на пропущенных контурах."""

    def setUp(self):
        self.tmp = self.tmpdir()
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)

    def test_full_run_on_known_incident(self):
        code, out, err = self.run_script('triage.py', [
            helpers.log('single_service.log'), '--kb', helpers.KB,
            '--repo', self.repo, '--out', self.tmp, '--stand', 'stage',
            '--service', 'payment-api'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertIn('# Разбор инцидента', out)
        self.assertIn('ConnectionTimeoutError', out)
        self.assertIn('INC-2026-07-001', out)
        for name in ('parsed.json', 'kb.json', 'confidence.json'):
            self.assertTrue(os.path.exists(os.path.join(self.tmp, name)),
                            '%s не сохранён' % name)

    def test_full_run_on_healthy_log_finds_nothing(self):
        code, out, err = self.run_script('triage.py', [
            helpers.log('healthy.log'), '--kb', helpers.KB,
            '--repo', self.repo, '--out', self.tmp])
        self.assertEqual(code, 0, err)
        self.assertNotIn('· ERROR**', out, 'на штатном логе показан шаблон уровня ERROR')
        self.assertIn('гипотеза', out)
        self.assertIn('записей уровня ERROR не найдено', out)


if __name__ == '__main__':
    unittest.main()
