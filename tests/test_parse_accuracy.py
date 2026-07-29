# -*- coding: utf-8 -*-
"""Точность разбора логов: частоты, границы записей, краевые входы, вывод.

Здесь проверяется не то, что скрипт не падает (это `test_robustness.py`), а то,
что числа и времена в его выводе соответствуют входу: на них стоят остальные
контуры разбора — база знаний, код, уверенность.
"""

import json
import os

from helpers import ScriptCase


def write(path, text):
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return path


class ExceptionCounts(ScriptCase):
    """Класс исключения считается один раз на запись, а не по числу регулярок."""

    def test_python_exception_is_counted_once(self):
        tmp = self.tmpdir()
        path = write(os.path.join(tmp, 'app.log'),
                     '2026-07-28 12:00:00 ERROR svc сбой\n'
                     'ValueError: bad input\n')
        parsed = self.json_of('parse_logs.py', [path])
        self.assertEqual(parsed['stats']['exceptions'], {'ValueError': 1},
                         'класс исключения посчитан дважды')

    def test_repeated_exception_counts_records_not_matches(self):
        tmp = self.tmpdir()
        path = write(os.path.join(tmp, 'app.log'), ''.join(
            '2026-07-28 12:0%d:00 ERROR TimeoutError: gateway timeout\n' % i
            for i in range(3)))
        parsed = self.json_of('parse_logs.py', [path])
        self.assertEqual(parsed['stats']['exceptions'], {'TimeoutError': 3})


class RecordBoundaries(ScriptCase):
    """Строка, отбитая пробелами, — продолжение записи, даже если в ней есть время."""

    def test_indented_line_with_timestamp_does_not_open_a_record(self):
        tmp = self.tmpdir()
        path = write(os.path.join(tmp, 'app.log'),
                     '2026-07-28 12:00:00 ERROR payment сбой оплаты\n'
                     'Traceback (most recent call last):\n'
                     '  File "/app/pay.py", line 3, in charge\n'
                     '    2026-07-28 12:00:01 payload dumped here\n'
                     'ValueError: bad input\n')
        parsed = self.json_of('parse_logs.py', [path])
        self.assertEqual(parsed['stats']['records'], 2,
                         'отбитая пробелами строка с временем разорвала стектрейс')
        templates = [g['template'] for g in parsed['groups']]
        self.assertTrue(any('Traceback' in t and 'ValueError' in t for t in templates),
                        'стектрейс не собрался в одну запись: %s' % templates)

    def test_unindented_record_still_starts_a_new_one(self):
        tmp = self.tmpdir()
        path = write(os.path.join(tmp, 'app.log'),
                     '2026-07-28 12:00:00 ERROR первая\n'
                     '2026-07-28 12:00:01 ERROR вторая\n')
        parsed = self.json_of('parse_logs.py', [path])
        self.assertEqual(parsed['stats']['records'], 2)


class EdgeInputs(ScriptCase):

    def test_broken_xz_is_a_warning_not_a_traceback(self):
        tmp = self.tmpdir()
        path = os.path.join(tmp, 'app.log.xz')
        with open(path, 'wb') as fh:
            # правильная сигнатура .xz, а дальше мусор: файл открывается, но не читается
            fh.write(b'\xfd7zXZ\x00' + b'not an archive at all' * 4)
        code, out, err = self.run_script('parse_logs.py', [path])
        self.assertEqual(code, 0, 'разбор битого .xz завершился кодом %d' % code)
        self.assertNotIn('Traceback', err, 'битый .xz уронил разбор:\n%s' % err)
        self.assertIn('warning', err, 'о битом .xz не предупредили:\n%s' % err)
        self.assertIn('# Сводка логов', out)

    def test_rotated_files_are_picked_up_in_a_directory(self):
        tmp = self.tmpdir()
        logs = os.path.join(tmp, 'logs')
        os.makedirs(logs)
        write(os.path.join(logs, 'app.log'),
              '2026-07-28 12:00:00 ERROR свежая запись\n')
        write(os.path.join(logs, 'app.log.1'),
              '2026-07-28 11:00:00 ERROR вчерашняя ротация\n')
        write(os.path.join(logs, 'messages.0'),
              '2026-07-28 10:00:00 ERROR ротация без расширения\n')
        parsed = self.json_of('parse_logs.py', [logs])
        self.assertEqual(sorted(parsed['stats']['origins']),
                         ['app.log', 'app.log.1', 'messages.0'],
                         'ротированные файлы не подхватились')

    def test_numeric_python_logging_level(self):
        tmp = self.tmpdir()
        path = write(os.path.join(tmp, 'app.json'), '\n'.join([
            '{"time": "2026-07-28T12:00:00Z", "level": 40, "message": "оплата не прошла"}',
            '{"time": "2026-07-28T12:00:01Z", "level": 30, "message": "повтор запроса"}',
            '{"time": "2026-07-28T12:00:02Z", "level": 10, "message": "детали запроса"}',
            '{"time": "2026-07-28T12:00:03Z", "level": 50, "message": "процесс падает"}',
        ]) + '\n')
        parsed = self.json_of('parse_logs.py', [path])
        self.assertEqual(parsed['stats']['levels'],
                         {'ERROR': 1, 'WARN': 1, 'DEBUG': 1, 'FATAL': 1})

    def test_feb_29_without_year_keeps_the_time(self):
        tmp = self.tmpdir()
        # 2026 год невисокосный: подставить его нельзя, ближайший подходящий позади — 2024
        path = write(os.path.join(tmp, 'syslog.log'),
                     'Feb 29 03:15:00 host payment[42]: ConnectionError: pool exhausted\n')
        parsed = self.json_of('parse_logs.py', [path])
        self.assertEqual(parsed['stats']['first_ts'], '2024-02-29 03:15:00',
                         'время записи 29 февраля потеряно')
        notes = ' '.join(parsed['stats']['time_assumptions'])
        self.assertIn('2024', notes, 'подставленный год не назван в сводке')


class Output(ScriptCase):

    def _many_groups(self, tmp, count=15):
        lines = []
        for i in range(count):
            for _ in range(count - i):     # частоты убывают: группа i встречается count-i раз
                lines.append('2026-07-28 12:00:00 ERROR сбой вида %s' % chr(ord('a') + i))
        return write(os.path.join(tmp, 'app.log'), '\n'.join(lines) + '\n')

    def _early_rare_log(self, tmp):
        """Лог, где самый ранний шаблон — самый редкий: по частоте он в хвосте.

        Буквы, а не числа: число в сообщении маскируется как `<n>`, и все строки
        схлопнулись бы в один шаблон.
        """
        lines = ['2026-07-28 09:00:00 ERROR редкий ранний сбой ядра']
        for i in range(8):
            for k in range(5):
                lines.append('2026-07-28 12:%02d:00 ERROR частый поздний сбой вида %s'
                             % (i * 5 + k, chr(ord('a') + i)))
        return write(os.path.join(tmp, 'app.log'), '\n'.join(lines) + '\n')

    def _early_rare_log_with_info(self, tmp):
        """То же, плюс три INFO-шаблона: они уходят в раздел «Прочие сообщения».

        Раздел нумеруется отдельно от проблемного, поэтому именно здесь видно,
        совпадает ли номер в сводке с номером группы для `--context`.
        """
        lines = ['2026-07-28 09:00:00 ERROR редкий ранний сбой ядра']
        for i in range(8):
            for k in range(5):
                lines.append('2026-07-28 12:%02d:00 ERROR частый поздний сбой вида %s'
                             % (i * 5 + k, chr(ord('a') + i)))
        for j, name in enumerate(('альфа', 'бета', 'гамма')):
            for k in range(3 - j):     # частоты убывают, порядок в разделе предсказуем
                lines.append('2026-07-28 12:30:%02d INFO обычное событие %s' % (k, name))
        return write(os.path.join(tmp, 'app.log'), '\n'.join(lines) + '\n')

    def test_json_is_not_cut_by_top(self):
        tmp = self.tmpdir()
        path = self._many_groups(tmp, 15)
        parsed = self.json_of('parse_logs.py', [path, '--top', '10'])
        self.assertEqual(len(parsed['groups']), 15,
                         '`--top` урезал JSON, хотя это правило md-сводки')

    def test_md_is_still_cut_by_top(self):
        tmp = self.tmpdir()
        path = self._many_groups(tmp, 15)
        code, out, err = self.run_script('parse_logs.py', [path, '--top', '5'])
        self.assertEqual(code, 0, err)
        self.assertIn('топ 5 из 15 шаблонов', out)

    def test_early_rare_template_survives_truncation(self):
        tmp = self.tmpdir()
        path = self._early_rare_log(tmp)
        # бюджет заведомо мал: поместится лишь пара шаблонов из десяти
        code, out, err = self.run_script('parse_logs.py', [path, '--max-chars', '2600'])
        self.assertEqual(code, 0, err)
        # именно среди показанных шаблонов, а не в списке сигнатур внизу сводки
        shown = [ln for ln in out.split('\n')
                 if ln.startswith('`') and 'редкий ранний сбой' in ln]
        self.assertTrue(shown, 'самый ранний шаблон вырезан усечением:\n%s' % out)
        self.assertLessEqual(len(out), 2600, 'сводка вылезла за бюджет')

    def test_level_filter_says_the_stats_are_after_it(self):
        tmp = self.tmpdir()
        path = write(os.path.join(tmp, 'app.log'),
                     '2026-07-28 12:00:00 INFO обычная работа\n'
                     '2026-07-28 12:00:01 DEBUG подробности\n'
                     '2026-07-28 12:00:02 ERROR оплата не прошла\n')
        code, out, err = self.run_script('parse_logs.py', [path, '--level', 'ERROR'])
        self.assertEqual(code, 0, err)
        self.assertIn('Отсеяно фильтром до разбора: 2', out)
        self.assertIn('посчитано по оставшимся записям', out,
                      'сводка не оговаривает, что распределение построено после фильтра')

    def test_context_number_points_at_the_shown_group(self):
        """Номер группы в сводке — это номер для `--context`, даже после усечения."""
        tmp = self.tmpdir()
        path = self._early_rare_log(tmp)
        code, out, err = self.run_script('parse_logs.py', [path, '--max-chars', '2600'])
        self.assertEqual(code, 0, err)
        block = [ln for ln in out.split('\n') if 'редкий ранний сбой' in ln and ln.startswith('`')]
        self.assertTrue(block, 'ранний шаблон не показан:\n%s' % out)
        # находим номер, под которым показан ранний шаблон, и просим его сырые записи
        number = None
        current = None
        for line in out.split('\n'):
            if line.startswith('**#'):
                current = int(line[3:line.index(' ')])
            if 'редкий ранний сбой' in line and current is not None:
                number = current
                break
        self.assertIsNotNone(number)
        code, ctx, err = self.run_script('parse_logs.py', [path, '--context', str(number)])
        self.assertEqual(code, 0, err)
        self.assertIn('редкий ранний сбой', ctx,
                      '`--context %d` показал не ту группу:\n%s' % (number, ctx))

    @staticmethod
    def _numbered_templates(out):
        """Пары (номер, шаблон) для всех показанных в сводке групп."""
        pairs = []
        number = None
        for line in out.split('\n'):
            if line.startswith('**#'):
                number = int(line[3:line.index(' ')])
            elif number is not None and line.startswith('`'):
                pairs.append((number, line.strip('`')))
                number = None
        return pairs

    def test_numbers_stay_unique_when_info_section_is_shown(self):
        """Раздел «Прочие сообщения» не переиспользует номера проблемных групп."""
        tmp = self.tmpdir()
        path = self._early_rare_log_with_info(tmp)
        code, out, err = self.run_script(
            'parse_logs.py', [path, '--max-chars', '2900', '--show-info'])
        self.assertEqual(code, 0, err)
        numbers = [n for n, _ in self._numbered_templates(out)]
        self.assertTrue(numbers, 'сводка не показала ни одной группы:\n%s' % out)
        self.assertEqual(len(numbers), len(set(numbers)),
                         'один номер выдан двум группам: %s\n%s' % (numbers, out))

    def test_every_shown_number_points_at_its_own_group(self):
        """Каждый номер из сводки ведёт `--context` ровно к своему шаблону."""
        tmp = self.tmpdir()
        path = self._early_rare_log_with_info(tmp)
        code, out, err = self.run_script(
            'parse_logs.py', [path, '--max-chars', '2900', '--show-info'])
        self.assertEqual(code, 0, err)
        pairs = self._numbered_templates(out)
        self.assertTrue(pairs, 'сводка не показала ни одной группы:\n%s' % out)
        for number, template in pairs:
            code, ctx, err = self.run_script('parse_logs.py', [path, '--context', str(number)])
            self.assertEqual(code, 0, err)
            self.assertIn('`%s`' % template, ctx,
                          '`--context %d` показал не ту группу: в сводке под этим '
                          'номером %r\n%s' % (number, template, ctx))


class JsonRoundTrip(ScriptCase):
    """JSON-разбор остаётся читаемым входом для остальных скриптов."""

    def test_full_json_is_valid_after_top_is_lifted(self):
        tmp = self.tmpdir()
        # буквы, а не числа: число в сообщении маскируется как <n>, и все двадцать
        # строк схлопнулись бы в один шаблон
        lines = ['2026-07-28 12:00:00 ERROR сбой вида %s' % chr(ord('a') + i)
                 for i in range(20)]
        path = write(os.path.join(tmp, 'app.log'), '\n'.join(lines) + '\n')
        code, out, err = self.run_script('parse_logs.py', [path, '--format', 'json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload['groups']), 20)
        self.assertEqual([g['n'] for g in payload['groups']], list(range(1, 21)))
