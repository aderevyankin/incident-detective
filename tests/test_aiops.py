# -*- coding: utf-8 -*-
"""Автономный разбор: нормализация алерта, машинный отчёт, база знаний без вопросов.

Проверяется то, что в этом режиме проверить больше нечем: человека в контуре нет,
и неверное поведение обнаружится не репликой в чате, а молча — неправильным
файлом отчёта или записью в базе знаний, которую никто не перечитает.
"""

import json
import os
import unittest

import helpers
from helpers import ScriptCase

ALERTS = os.path.join(helpers.FIXTURES, 'alerts')

AUTO = {'INCIDENT_MODE': 'auto'}


def alert(name):
    return os.path.join(ALERTS, '%s.json' % name)


class AlertNormalization(ScriptCase):
    """Разбор payload'а детерминирован и модели не поручается."""

    def card(self, name, extra_args=(), env=None):
        code, out, err = self.run_script(
            'alert_to_incident.py', ['--file', alert(name)] + list(extra_args), env=env)
        self.assertEqual(code, 0, 'alert_to_incident.py: rc=%d\n%s' % (code, err))
        return json.loads(out)

    def test_alertmanager(self):
        card = self.card('alertmanager')
        self.assertEqual(card['format'], 'alertmanager')
        self.assertEqual(card['stand'], 'stage')      # staging приведён к принятому
        self.assertEqual(card['service'], 'payment-api')
        self.assertEqual(card['started_at'], '2026-07-28 16:20:03')
        self.assertEqual(card['time_source'], 'alert')
        self.assertEqual(card['signature'], 'PaymentApiErrors')
        self.assertEqual(card['missing'], [])

    def test_grafana(self):
        card = self.card('grafana-legacy')
        self.assertEqual(card['format'], 'grafana')
        self.assertEqual(card['stand'], 'stage')
        self.assertEqual(card['service'], 'payment-api')
        self.assertEqual(card['signature'], 'payment-api error rate')

    def test_sentry(self):
        card = self.card('sentry')
        self.assertEqual(card['format'], 'sentry')
        self.assertEqual(card['stand'], 'stage')
        self.assertEqual(card['service'], 'payment-api')
        self.assertEqual(card['started_at'], '2026-07-28 16:20:03')
        self.assertEqual(card['signature'], 'ConnectionTimeoutError')

    def test_window_wraps_incident_time(self):
        card = self.card('alertmanager', ['--window', '30'])
        self.assertEqual(card['since'], '2026-07-28 15:50:03')
        self.assertEqual(card['until'], '2026-07-28 16:50:03')
        self.assertIn('--since', card['triage_args'])

    def test_window_is_reproducible_with_incident_now(self):
        """Времени в legacy-вебхуке Grafana нет — окно строится от INCIDENT_NOW.

        Два прогона одного алерта с одним значением переменной дают одно окно:
        иначе повторный разбор смотрел бы в другой отрезок времени.
        """
        first = self.card('grafana-legacy')
        second = self.card('grafana-legacy')
        self.assertEqual(first['since'], second['since'])
        self.assertEqual(first['until'], second['until'])
        self.assertEqual(first['time_source'], 'now')
        self.assertIn('время инцидента', first['missing'])

        other = self.card('grafana-legacy', env={'INCIDENT_NOW': '2026-07-29 03:00:00'})
        self.assertNotEqual(other['since'], first['since'])

    def test_unknown_format_fails(self):
        code, _out, err = self.run_script('alert_to_incident.py', ['--file', alert('unknown')])
        self.assertEqual(code, 3)
        self.assertIn('не распознан', err)

    def test_garbage_input_fails(self):
        path = os.path.join(self.tmpdir(), 'garbage.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('не json вовсе\n')
        code, _out, err = self.run_script('alert_to_incident.py', ['--file', path])
        self.assertEqual(code, 3)
        self.assertIn('JSON', err)

    def test_missing_fields_are_marked_not_guessed(self):
        code, out, err = self.run_script('alert_to_incident.py',
                                         ['--file', alert('no-fields')])
        self.assertEqual(code, 4, err)
        card = json.loads(out)
        self.assertIsNone(card['stand'])
        self.assertIsNone(card['service'])
        self.assertEqual(sorted(card['missing']), ['сервис', 'стенд'])
        self.assertFalse(card['sufficient'])
        # значения по умолчанию не подставлены — их нет и в аргументах разбора
        self.assertNotIn('--stand', card['triage_args'])
        self.assertNotIn('--service', card['triage_args'])


class Mode(ScriptCase):
    """Режим берётся из переменной и только из неё."""

    def test_unknown_value_is_an_error(self):
        code, _out, err = self.run_script(
            'kb_search.py', ['--list'], env={'INCIDENT_MODE': 'полуавтомат'})
        self.assertNotEqual(code, 0)
        self.assertIn('INCIDENT_MODE', err)

    def test_unset_keeps_dialog_behaviour(self):
        """Переменной нет — прежнее поведение: расположение базы спрашивается."""
        code, _out, err = self.run_script(
            'kb_add.py', ['--title', 'без базы'], env={'INCIDENT_KB_DIR': None},
            cwd=self.tmpdir())
        self.assertEqual(code, 3, err)
        self.assertIn('не выбрано', err)


class Report(ScriptCase):
    """Отчёт собирает скрипт, а не формулировка модели."""

    def triage(self, args, env=None, out_dir=None):
        out_dir = out_dir or self.tmpdir()
        env = dict(env or {})
        args = list(args)
        if '--repo' not in args:
            # умолчание `--repo .` от рабочей директории прогона — это корень
            # файловой системы: контур кода уполз бы индексировать её целиком
            args += ['--repo', self.tmpdir()]
        code, out, err = self.run_script(
            'triage.py', args + ['--out', out_dir], env=env)
        return code, out, err, out_dir

    def report_of(self, args, env=None, out_dir=None):
        code, _out, err, out_dir = self.triage(args, env, out_dir)
        self.assertEqual(code, 0, 'triage.py: rc=%d\n%s' % (code, err))
        path = os.path.join(out_dir, 'report.json')
        self.assertTrue(os.path.isfile(path), 'отчёт не создан: %s' % path)
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh), out_dir

    def test_full_triage_report(self):
        report, out_dir = self.report_of(
            [helpers.log('payment.log'), '--kb', helpers.KB,
             '--repo', helpers.make_repo(self.tmpdir())['repo'],
             '--stand', 'stage', '--service', 'payment-api'], env=AUTO)
        self.assertEqual(report['mode'], 'auto')
        self.assertFalse(report['insufficient'])
        self.assertIn(report['verdict'],
                      ('подтверждено данными', 'вероятная причина', 'гипотеза'))
        self.assertIsInstance(report['confidence'], float)
        self.assertTrue(report['signature'])
        self.assertTrue(report['evidence'], 'доказательств нет вовсе')
        for item in report['evidence']:
            self.assertTrue(item.get('source'),
                            'доказательство без источника: %r' % item)
        self.assertEqual(report['artifacts']['out_dir'], out_dir)
        self.assertTrue(report['next_step']['title'])

    def test_report_carries_alert_card(self):
        out_dir = self.tmpdir()
        card_path = os.path.join(out_dir, 'incident.json')
        code, out, err = self.run_script('alert_to_incident.py',
                                         ['--file', alert('alertmanager')])
        self.assertEqual(code, 0, err)
        with open(card_path, 'w', encoding='utf-8') as fh:
            fh.write(out)

        report, _ = self.report_of(
            [helpers.log('payment.log'), '--kb', helpers.KB,
             '--incident', card_path], env=AUTO, out_dir=out_dir)
        # стенд и сервис не передавались флагами — они пришли из алерта
        self.assertEqual(report['incident']['stand'], 'stage')
        self.assertEqual(report['incident']['service'], 'payment-api')
        self.assertEqual(report['incident']['time_source'], 'alert')
        self.assertEqual(report['incident']['alert_id'], '9f2c1a44b7e0')

    def test_report_without_logs_says_insufficient(self):
        """Логов нет — вердикт, а не догадка."""
        missing_log = os.path.join(self.tmpdir(), 'нет-такого.log')
        report, _ = self.report_of([missing_log, '--kb', helpers.KB], env=AUTO)
        self.assertTrue(report['insufficient'])
        self.assertEqual(report['verdict'], 'данных недостаточно')
        self.assertTrue(report['missing'], 'не сказано, чего не хватило')
        self.assertTrue(any(not c['passed'] and c['reason'] for c in report['contours']))

    def test_contours_and_reasons_are_listed(self):
        """Пропущенный контур назван вместе с причиной — читателя в чате нет."""
        report, _ = self.report_of(
            [helpers.log('payment.log'), '--kb', helpers.KB,
             '--repo', os.path.join(self.tmpdir(), 'нет-репозитория')], env=AUTO)
        code = [c for c in report['contours'] if c['key'] == 'code'][0]
        self.assertFalse(code['passed'])
        self.assertIn('репозиторий', code['reason'])
        self.assertTrue(any('код' in m for m in report['missing']))

    def test_explicit_insufficient_verdict(self):
        """Источник неоднозначен или окружение непригодно — разбор завершается вердиктом."""
        report, _ = self.report_of(
            [helpers.log('payment.log'), '--kb', helpers.KB, '--insufficient',
             '--missing', 'источник логов неоднозначен: kibana-mcp, /var/log/payment'],
            env=AUTO)
        self.assertEqual(report['verdict'], 'данных недостаточно')
        self.assertIn('источник логов неоднозначен: kibana-mcp, /var/log/payment',
                      report['missing'])
        self.assertEqual(report['next_step']['kind'], 'task')

    def test_evidence_is_scrubbed(self):
        """К доказательствам применяется та же очистка, что к записи базы знаний."""
        tmp = self.tmpdir()
        path = os.path.join(tmp, 'secrets.log')
        with open(path, 'w', encoding='utf-8') as fh:
            for i in range(3):
                fh.write('2026-07-28 16:20:0%d ERROR payment-api AuthTokenError: '
                         'refused token=sk-livedeadbeef0123456789 for user %d\n' % (i, i))
        report, _ = self.report_of([path, '--kb', helpers.KB], env=AUTO)
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('sk-livedeadbeef0123456789', blob)
        self.assertIn('token=<redacted>', blob)

    def test_auto_mode_requires_explicit_out(self):
        """Отчёт во временной директории обвязке бесполезен — за ним никто не придёт."""
        code, _out, err = self.run_script(
            'triage.py', [helpers.log('payment.log'), '--kb', helpers.KB], env=AUTO)
        self.assertNotEqual(code, 0)
        self.assertIn('--out', err)

    def test_stopped_at_is_recorded(self):
        report, _ = self.report_of(
            [helpers.log('payment.log'), '--kb', helpers.KB,
             '--stopped-at', 'потолок прохода: пять вызовов до вывода причины'],
            env=AUTO)
        self.assertIn('потолок прохода', report['stopped_at'])


class KnowledgeBaseWithoutQuestions(ScriptCase):
    """Пополнение базы знаний в автономном режиме — без вопроса о расположении."""

    def report_stub(self, tmp):
        path = os.path.join(tmp, 'report.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'schema': 'incident-detective/report@1', 'kb_entry': {}}, fh)
        return path

    def test_no_path_no_record_but_no_error(self):
        tmp = self.tmpdir()
        report = self.report_stub(tmp)
        code, out, err = self.run_script(
            'kb_add.py', ['--title', 'ночной разбор', '--report', report],
            env=dict(AUTO, INCIDENT_KB_DIR=None), cwd=tmp)
        self.assertEqual(code, 0, err)
        self.assertIn('не сделана', out)
        with open(report, 'r', encoding='utf-8') as fh:
            entry = json.load(fh)['kb_entry']
        self.assertFalse(entry['written'])
        self.assertIn('INCIDENT_KB_DIR', entry['reason'])
        # директория базы молча не заводится нигде
        self.assertFalse(os.path.isdir(os.path.join(tmp, 'memory')))

    def test_repeated_signature_updates_entry(self):
        tmp = self.tmpdir()
        kb = os.path.join(tmp, 'kb')
        os.makedirs(kb)
        env = dict(AUTO, INCIDENT_KB_DIR=kb)
        args = ['--signature', 'FlappingError: upstream refused connection',
                '--stand', 'stage']

        code, _out, err = self.run_script(
            'kb_add.py', ['--title', 'первый разбор'] + args, env=env)
        self.assertEqual(code, 0, err)
        first = sorted(n for n in os.listdir(kb) if n.startswith('INC-'))
        self.assertEqual(len(first), 1, first)

        code, out, err = self.run_script(
            'kb_add.py', ['--title', 'тот же алерт снова'] + args, env=env)
        self.assertEqual(code, 0, err)
        self.assertIn('дубля не завожу', out)
        second = sorted(n for n in os.listdir(kb) if n.startswith('INC-'))
        self.assertEqual(first, second, 'повтор сигнатуры завёл вторую запись')

        with open(os.path.join(kb, first[0]), 'r', encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('reuse_count: 1', text)

    def test_written_record_is_named_in_report(self):
        tmp = self.tmpdir()
        kb = os.path.join(tmp, 'kb')
        os.makedirs(kb)
        report = self.report_stub(tmp)
        code, _out, err = self.run_script(
            'kb_add.py', ['--title', 'разбор с базой', '--report', report],
            env=dict(AUTO, INCIDENT_KB_DIR=kb))
        self.assertEqual(code, 0, err)
        with open(report, 'r', encoding='utf-8') as fh:
            entry = json.load(fh)['kb_entry']
        self.assertTrue(entry['written'])
        self.assertTrue(entry['id'].startswith('INC-'))
        self.assertTrue(os.path.isfile(entry['path']))


class RunDiagnostics(ScriptCase):
    """По следам прогона видно, что запускалось и чем кончилось."""

    def test_telemetry_tells_skipped_from_failed(self):
        tmp = self.tmpdir()
        trace = os.path.join(tmp, 'calls.log')
        out_dir = os.path.join(tmp, 'run')
        os.makedirs(out_dir)
        env = dict(AUTO, INCIDENT_TRACE_FILE=trace)

        # контур кода пропущен: репозитория нет, но сам разбор состоялся
        code, _out, err = self.run_script(
            'triage.py', [helpers.log('payment.log'), '--kb', helpers.KB,
                          '--repo', os.path.join(tmp, 'нет-репозитория'),
                          '--out', out_dir], env=env)
        self.assertEqual(code, 0, err)
        # а этот запуск падает — на несуществующей записи базы знаний
        self.run_script('kb_add.py', ['--update', 'INC-9999-99-999', '--kb', helpers.KB],
                        env=env)

        with open(trace, 'r', encoding='utf-8') as fh:
            lines = [l for l in fh.read().split('\n') if l.strip()]
        codes = {l.split('\t')[1]: l.split('\t')[-1] for l in lines}
        self.assertEqual(codes['triage.py'], 'rc=0')
        self.assertNotEqual(codes['kb_add.py'], 'rc=0',
                            'упавший запуск неотличим от прошедшего')

        with open(os.path.join(out_dir, 'report.json'), 'r', encoding='utf-8') as fh:
            report = json.load(fh)
        skipped = [c for c in report['contours'] if not c['passed']]
        self.assertTrue(skipped, 'пропущенный контур в отчёте не отмечен')
        self.assertEqual(report['artifacts']['trace_file'], trace)

    def test_flag_values_never_reach_telemetry(self):
        """Правило действует независимо от режима: в файл идут имена флагов."""
        tmp = self.tmpdir()
        trace = os.path.join(tmp, 'calls.log')
        self.run_script('kb_search.py', ['--list'],
                        env=dict(AUTO, INCIDENT_TRACE_FILE=trace))
        with open(trace, 'r', encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('--list', text)
        self.assertNotIn(helpers.KB, text)


if __name__ == '__main__':
    unittest.main()
