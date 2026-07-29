# -*- coding: utf-8 -*-
"""Время git-коммитов в хронологии — на той же шкале, что и остальная лента.

Лента объявляет времена в UTC. `git log` по умолчанию печатает время в зоне
машины, а границы `--since/--until` понимает там же, поэтому разбор одного и
того же инцидента на ноутбуке в UTC+3 и на стенде в UTC давал разные ленты.
Тесты гоняют скрипты с подменённой TZ: результат от неё зависеть не должен.
"""

import json
import os
import unittest

import helpers
from helpers import ScriptCase

HAS_GIT = helpers.has_git()

# Зоны выбраны по разные стороны от UTC и с ненулевым смещением: ошибка,
# зависящая от знака смещения, иначе прошла бы незамеченной.
ZONES = ('UTC', 'Asia/Tokyo', 'America/Los_Angeles')


@unittest.skipUnless(HAS_GIT, 'нужен git — тесты времени коммитов пропущены')
class CommitTime(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()
        self.info = helpers.make_repo(self.tmp)
        # веха-якорь: без источников событий лента не строится. Время — «сейчас»
        # тестов, чтобы оба коммита репозитория попали в окно (сутки назад)
        self.event = '2026-07-28 20:00|инцидент замечен'

    def commits(self, zone, extra=()):
        code, out, err = self.run_script(
            'timeline.py', ['--event', self.event, '--repo', self.info['repo'],
                            '--format', 'json'] + list(extra),
            env={'TZ': zone})
        self.assertEqual(code, 0, 'timeline.py упал при TZ=%s:\n%s' % (zone, err))
        self.assertNotIn('Traceback', err)
        return [e for e in json.loads(out) if e['kind'] == 'коммит']

    def test_commit_time_does_not_depend_on_machine_zone(self):
        seen = {}
        for zone in ZONES:
            rows = self.commits(zone)
            self.assertTrue(rows, 'коммитов в ленте нет при TZ=%s' % zone)
            seen[zone] = sorted((r['ts'], r['text']) for r in rows)
        first = seen[ZONES[0]]
        for zone in ZONES[1:]:
            self.assertEqual(seen[zone], first,
                             'при TZ=%s лента коммитов другая: %s vs %s'
                             % (zone, seen[zone], first))

    def test_commit_time_is_utc(self):
        # helpers.make_repo коммитит с явным +0000, второй коммит — за 30 минут до NOW
        rows = self.commits('Asia/Tokyo')
        times = [r['ts'] for r in rows]
        self.assertIn('2026-07-28 19:30:00', times,
                      'время коммита не приведено к UTC: %s' % times)

    def test_window_boundary_is_understood_in_the_same_scale(self):
        # окно кончается через пять минут после коммита: если git поймёт границу
        # в зоне машины, коммит выпадет из ленты
        for zone in ZONES:
            with self.subTest(zone=zone):
                rows = self.commits(zone, extra=['--until', '2026-07-28 19:35'])
                times = [r['ts'] for r in rows]
                self.assertIn('2026-07-28 19:30:00', times,
                              'коммит на границе окна потерян при TZ=%s: %s' % (zone, times))

    def test_code_hints_commits_do_not_depend_on_zone(self):
        seen = {}
        for zone in ZONES:
            data = self.json_of('code_hints.py',
                                ['--repo', self.info['repo'],
                                 '--signature', self.info['exc_name'],
                                 # окно code_hints — сутки до этого момента
                                 '--since', '2026-07-28 20:00'],
                                env={'TZ': zone})
            seen[zone] = [(c['date'], c['subject']) for c in data['commits']]
        first = seen[ZONES[0]]
        self.assertTrue(first, 'коммитов в окне нет вовсе')
        for zone in ZONES[1:]:
            self.assertEqual(seen[zone], first,
                             'при TZ=%s code_hints даёт другие коммиты: %s vs %s'
                             % (zone, seen[zone], first))
        self.assertIn('2026-07-28 19:30', [d for d, _s in first],
                      'время коммита не на шкале UTC: %s' % first)


class MissingInputs(ScriptCase):

    def test_missing_parsed_file_is_explained_not_a_traceback(self):
        code, _out, err = self.run_script(
            'timeline.py', ['--parsed', os.path.join(self.tmpdir(), 'нет.json')])
        self.assertNotEqual(code, 0, 'отсутствующий файл разбора прошёл незамеченным')
        self.assertNotIn('Traceback', err, 'сырая трассировка вместо объяснения:\n%s' % err)
        self.assertIn('нет.json', err)

    def test_parsed_file_that_is_not_json_is_explained(self):
        path = os.path.join(self.tmpdir(), 'raw.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('это вообще не JSON\n')
        code, _out, err = self.run_script('timeline.py', ['--parsed', path])
        self.assertNotEqual(code, 0)
        self.assertNotIn('Traceback', err, 'сырая трассировка вместо объяснения:\n%s' % err)


if __name__ == '__main__':
    unittest.main()
