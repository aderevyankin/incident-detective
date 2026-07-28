# -*- coding: utf-8 -*-
"""Карта источников: запись, чтение, обновление, старение и отделение от разборов.

Карта отвечает на вопрос «откуда брать логи для этой пары стенда и сервиса», а
не «что тогда сломалось». Отсюда и предмет проверок: адресация не смешивается с
разборами ни в поиске, ни в счётчиках, повторная запись той же пары обновляет
запись вместо дубля, а старое подтверждение видно в выдаче.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

import helpers
from helpers import ScriptCase

SOURCES_KB = os.path.join(helpers.FIXTURES, 'kb_sources')
# записи фикстуры: разбор без поля kind (старая база), разбор с явным kind,
# свежая запись карты и запись с просроченным подтверждением
LEGACY_INCIDENT = 'INC-2026-06-010'   # stands: [stage], поля kind нет
PROD_INCIDENT = 'INC-2026-07-020'     # stands: [prod], kind: incident
FRESH_SOURCE = 'SRC-stage-payment-api'
STALE_SOURCE = 'SRC-dev-payment-api'

BENCH = os.path.join(helpers.REPO, 'tools', 'bench.py')


class SourceKbCase(ScriptCase):
    """Общее для проверок карты: пустая база во временной директории."""

    def setUp(self):
        self.tmp = self.tmpdir()
        self.kb = os.path.join(self.tmp, 'kb')

    def add_source(self, args, now=helpers.NOW):
        code, out, err = self.run_script('kb_add.py',
                                         ['--kind', 'source', '--kb', self.kb] + list(args),
                                         now=now)
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        return out

    def sources(self, args=(), kb=None, now=helpers.NOW):
        return self.json_of('kb_search.py',
                            ['--sources', '--kb', kb or self.kb] + list(args), now=now)

    def record_files(self, prefix='SRC-'):
        return sorted(n for n in os.listdir(self.kb)
                      if n.startswith(prefix) and n.endswith('.md'))

    def read_record(self, name):
        with open(os.path.join(self.kb, name), 'r', encoding='utf-8') as fh:
            return fh.read()


class SourceRecordLifecycle(SourceKbCase):
    """5.1 Запись карты создаётся, читается и обновляется; дубль не появляется."""

    BASE = ['--stand', 'stage', '--service', 'payment-api',
            '--source', 'kind=mcp', '--source', 'server=kibana-mcp',
            '--source', 'tool=search_logs', '--address', 'index=logs-stage-*',
            '--query', 'env:stage AND service:payment-api',
            '--fields', 'time=@timestamp,level=log.level']

    def test_created_record_reads_back_with_all_fields(self):
        out = self.add_source(self.BASE)
        self.assertIn('id: SRC-stage-payment-api', out)
        self.assertEqual(self.record_files(), ['SRC-stage-payment-api.md'])

        found = self.sources(['--stand', 'stage', '--service', 'payment-api'])
        self.assertEqual([e['id'] for e in found], ['SRC-stage-payment-api'])
        meta = found[0]['meta']
        self.assertEqual(meta['kind'], 'source')
        self.assertEqual(meta['stand'], 'stage')
        self.assertEqual(meta['services'], ['payment-api'])
        self.assertEqual(meta['source'],
                         {'kind': 'mcp', 'server': 'kibana-mcp', 'tool': 'search_logs'})
        self.assertEqual(meta['address'], {'index': 'logs-stage-*'})
        self.assertEqual(meta['fields'], {'time': '@timestamp', 'level': 'log.level'})
        self.assertEqual(meta['query'], 'env:stage AND service:payment-api')
        self.assertEqual(meta['confirmed'], helpers.NOW.split()[0])

    def test_same_pair_updates_record_without_duplicate(self):
        self.add_source(self.BASE)
        out = self.add_source(self.BASE + ['--address', 'index=logs-stage-2026*'],
                              now='2026-08-15 10:00:00')
        self.assertIn('Запись обновлена', out)
        self.assertEqual(self.record_files(), ['SRC-stage-payment-api.md'],
                         'повторная запись той же пары завела дубль')

        found = self.sources(now='2026-08-15 10:00:00')
        self.assertEqual(len(found), 1)
        meta = found[0]['meta']
        # дата подтверждения двигается: запись означает состоявшуюся выгрузку
        self.assertEqual(meta['confirmed'], '2026-08-15')
        self.assertEqual(meta['address'], {'index': 'logs-stage-2026*'})
        # прежние поля не потерялись при обновлении
        self.assertEqual(meta['source']['server'], 'kibana-mcp')

    def test_update_by_id_does_not_create_second_record(self):
        self.add_source(self.BASE)
        out = self.add_source(['--update', 'SRC-stage-payment-api',
                               '--query', 'env:stage AND service:payment-api AND level:ERROR'],
                              now='2026-08-01 09:00:00')
        self.assertIn('Запись обновлена', out)
        self.assertEqual(self.record_files(), ['SRC-stage-payment-api.md'])
        meta = self.sources(now='2026-08-01 09:00:00')[0]['meta']
        self.assertIn('level:ERROR', meta['query'])
        self.assertEqual(meta['confirmed'], '2026-08-01')

    def test_another_service_on_same_stand_gets_its_own_record(self):
        self.add_source(self.BASE)
        self.add_source(['--stand', 'stage', '--service', 'gateway',
                         '--source', 'kind=file', '--address', 'path=/var/log/gateway.log'])
        self.assertEqual(self.record_files(),
                         ['SRC-stage-gateway.md', 'SRC-stage-payment-api.md'])
        self.assertEqual(len(self.sources(['--stand', 'stage'])), 2)
        self.assertEqual([e['id'] for e in self.sources(['--service', 'gateway'])],
                         ['SRC-stage-gateway'])

    def test_new_record_requires_stand_service_and_source(self):
        code, _out, err = self.run_script(
            'kb_add.py', ['--kind', 'source', '--stand', 'stage', '--kb', self.kb])
        self.assertEqual(code, 1, err)
        self.assertIn('--service', err)
        self.assertFalse(os.path.isdir(self.kb), 'отказ всё равно завёл базу знаний')

    def test_log_fragment_is_not_stored_in_the_map(self):
        """Карта хранит адресацию, а не выгрузку: строка лога отклоняется."""
        code, _out, err = self.run_script(
            'kb_add.py', ['--kind', 'source', '--stand', 'stage', '--service', 'payment-api',
                          '--source', 'kind=mcp',
                          '--query', '2026-07-28 12:00:01 ERROR connection pool exhausted',
                          '--kb', self.kb])
        self.assertEqual(code, 1, err)
        self.assertIn('Карта хранит', err)


class LegacyBaseWithoutKind(ScriptCase):
    """5.2 База со старыми записями без `kind` читается, записи — инциденты."""

    def test_legacy_record_is_found_by_search(self):
        hits = self.json_of('kb_search.py',
                            ['таймаут', 'пул', 'соединений', '--kb', SOURCES_KB])
        ids = [h['id'] for h in hits]
        self.assertIn(LEGACY_INCIDENT, ids)
        legacy = next(h for h in hits if h['id'] == LEGACY_INCIDENT)
        self.assertNotIn('kind', legacy['meta'],
                         'у фикстуры старой базы поля kind быть не должно')

    def test_legacy_kb_without_kind_reads_and_searches(self):
        """Старая база целиком (tests/fixtures/kb) читается и после ввода видов записей."""
        code, out, err = self.run_script(
            'kb_search.py', ['таймаут', 'пул', '--kb', helpers.KB, '--format', 'json'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertTrue(json.loads(out))

    def test_index_over_legacy_record_marks_it_as_incident(self):
        tmp = self.tmpdir()
        kb = os.path.join(tmp, 'kb')
        shutil.copytree(SOURCES_KB, kb)
        code, _out, err = self.run_script('kb_index.py', ['--kb', kb])
        self.assertEqual(code, 0, err)
        with open(os.path.join(kb, 'index.json'), 'r', encoding='utf-8') as fh:
            index = json.load(fh)
        entries = {e['id']: e for e in index['incidents']}
        self.assertEqual(entries[LEGACY_INCIDENT]['kind'], 'incident')
        self.assertEqual(entries[FRESH_SOURCE]['kind'], 'source')
        # сервис из записи карты — не пережитый инцидент: в счётчики он не идёт
        self.assertEqual(index['services'].get('payment-api'), 2)
        self.assertEqual(index['stands'].get('dev'), None)

    def test_index_without_kind_field_is_still_readable(self):
        """Индекс, собранный до ввода видов записей, читается поиском как есть."""
        tmp = self.tmpdir()
        kb = os.path.join(tmp, 'kb')
        shutil.copytree(SOURCES_KB, kb)
        code, _out, err = self.run_script('kb_index.py', ['--kb', kb])
        self.assertEqual(code, 0, err)
        path = os.path.join(kb, 'index.json')
        with open(path, 'r', encoding='utf-8') as fh:
            index = json.load(fh)
        for entry in index['incidents']:
            entry.pop('kind', None)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(index, fh, ensure_ascii=False)
        hits = self.json_of('kb_search.py', ['таймаут', 'пул', '--kb', kb])
        self.assertIn(LEGACY_INCIDENT, [h['id'] for h in hits])


class KindsDoNotMix(ScriptCase):
    """5.3 Поиск по инцидентам не возвращает карту, запрос карты — не разборы."""

    def test_incident_search_returns_no_source_records(self):
        hits = self.json_of('kb_search.py',
                            ['payment-api', 'таймаут', 'пул', '--kb', SOURCES_KB])
        self.assertTrue(hits)
        for hit in hits:
            self.assertFalse(str(hit['id']).startswith('SRC-'),
                             'в выдачу по симптому попала запись карты: %s' % hit['id'])

    def test_source_terms_do_not_pull_map_into_incident_search(self):
        """Слова из записи карты (kibana, индекс) не делают её результатом поиска."""
        code, text, err = self.run_script(
            'kb_search.py', ['kibana-mcp', 'logs-stage', 'search_logs', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertNotIn('SRC-', text)

    def test_source_listing_returns_no_incidents(self):
        found = self.json_of('kb_search.py', ['--sources', '--kb', SOURCES_KB])
        ids = [e['id'] for e in found]
        self.assertEqual(sorted(ids), sorted([FRESH_SOURCE, STALE_SOURCE]))

    def test_source_listing_filters_by_stand_and_service(self):
        found = self.json_of('kb_search.py',
                             ['--sources', '--stand', 'stage', '--service', 'payment-api',
                              '--kb', SOURCES_KB])
        self.assertEqual([e['id'] for e in found], [FRESH_SOURCE])

    def test_uncovered_pair_is_reported_as_missing_from_the_map(self):
        code, text, err = self.run_script(
            'kb_search.py', ['--sources', '--stand', 'prod', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('картой не покрыта', text)
        # это не «база пуста»: записи карты в базе есть, просто про другие стенды
        self.assertIn('Записей карты в базе: 2', text)


class Freshness(ScriptCase):
    """5.4 Устаревшая запись карты помечается, свежая — нет."""

    def test_stale_record_is_flagged_and_fresh_is_not(self):
        found = {e['id']: e for e in
                 self.json_of('kb_search.py', ['--sources', '--kb', SOURCES_KB])}
        self.assertTrue(found[STALE_SOURCE]['stale'])
        self.assertEqual(found[STALE_SOURCE]['age_days'], 88)
        self.assertFalse(found[FRESH_SOURCE]['stale'])
        self.assertEqual(found[FRESH_SOURCE]['age_days'], 8)

    def test_flag_is_visible_in_the_readable_output(self):
        code, text, err = self.run_script(
            'kb_search.py', ['--sources', '--stand', 'dev', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('требует проверки', text)
        self.assertIn('возраст 88 дн.', text)

        code, text, err = self.run_script(
            'kb_search.py', ['--sources', '--stand', 'stage', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertNotIn('требует проверки', text)

    def test_stale_record_becomes_fresh_after_reconfirmation(self):
        tmp = self.tmpdir()
        kb = os.path.join(tmp, 'kb')
        shutil.copytree(SOURCES_KB, kb)
        code, _out, err = self.run_script(
            'kb_add.py', ['--kind', 'source', '--stand', 'dev', '--service', 'payment-api',
                          '--source', 'kind=file', '--kb', kb])
        self.assertEqual(code, 0, err)
        entry = next(e for e in self.json_of('kb_search.py', ['--sources', '--kb', kb])
                     if e['id'] == STALE_SOURCE)
        self.assertFalse(entry['stale'])
        self.assertEqual(entry['age_days'], 0)


class CheckedMarks(SourceKbCase):
    """5.5 Пометка бесполезного источника пишется, читается и обновляется."""

    def _mark(self, source, verdict, note, now=helpers.NOW):
        code, out, err = self.run_script(
            'kb_add.py', ['--mark-checked', source, '--verdict', verdict, '--note', note,
                          '--stand', 'stage', '--service', 'payment-api', '--kb', self.kb],
            now=now)
        self.assertEqual(code, 0, err)
        return out

    def _marks(self, now=helpers.NOW):
        found = self.sources(now=now)
        self.assertEqual(len(found), 1)
        return found[0]['meta'].get('checked') or []

    def test_mark_is_written_and_read_back(self):
        out = self._mark('loki-mcp', 'empty', 'стенд не пишет в loki')
        self.assertIn('источник loki-mcp помечен как empty', out)
        marks = self._marks()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]['source'], 'loki-mcp')
        self.assertEqual(marks[0]['verdict'], 'empty')
        self.assertEqual(marks[0]['date'], helpers.NOW.split()[0])
        self.assertEqual(marks[0]['note'], 'стенд не пишет в loki')

    def test_repeated_check_replaces_the_previous_mark(self):
        self._mark('loki-mcp', 'empty', 'стенд не пишет в loki')
        self._mark('loki-mcp', 'unavailable', 'сервер не отвечает',
                   now='2026-08-10 12:00:00')
        marks = self._marks(now='2026-08-10 12:00:00')
        self.assertEqual(len(marks), 1, 'повторная проверка легла рядом со старой: %s' % marks)
        self.assertEqual(marks[0]['verdict'], 'unavailable')
        self.assertEqual(marks[0]['date'], '2026-08-10')

    def test_marks_of_different_tools_live_side_by_side(self):
        self._mark('loki-mcp', 'empty', 'стенд не пишет в loki')
        self._mark('grafana-mcp', 'unavailable', 'нет доступа')
        marks = sorted(self._marks(), key=lambda m: m['source'])
        self.assertEqual([m['source'] for m in marks], ['grafana-mcp', 'loki-mcp'])

    def test_mark_does_not_move_confirmation_date(self):
        """Пометка бесполезного источника ничего не подтверждает."""
        self.add_source(['--stand', 'stage', '--service', 'payment-api',
                         '--source', 'kind=mcp', '--source', 'server=kibana-mcp'])
        self._mark('loki-mcp', 'empty', 'пусто за окно', now='2026-08-10 12:00:00')
        meta = self.sources(now='2026-08-10 12:00:00')[0]['meta']
        self.assertEqual(meta['confirmed'], helpers.NOW.split()[0])

    def test_verdict_is_required_and_named(self):
        code, _out, err = self.run_script(
            'kb_add.py', ['--mark-checked', 'loki-mcp', '--stand', 'stage',
                          '--service', 'payment-api', '--kb', self.kb])
        self.assertEqual(code, 2, err)
        self.assertIn('--verdict', err)

    def test_expired_mark_stops_excluding_the_tool(self):
        """Пометка стареет по тому же сроку, что и запись: инструмент могли починить."""
        code, text, err = self.run_script(
            'kb_search.py', ['--sources', '--stand', 'dev', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('пометка устарела', text)
        code, text, err = self.run_script(
            'kb_search.py', ['--sources', '--stand', 'stage', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('отвергнут: loki-mcp', text)
        self.assertNotIn('пометка устарела', text)


class SecretsAreScrubbed(SourceKbCase):
    """5.6 Секрет в запросе и в строке подключения маскируется перед записью."""

    TOKEN = 'abc123secretvalue'
    PASSWORD = 's3cretpass'

    def test_token_in_query_and_password_in_connection_string(self):
        out = self.add_source([
            '--stand', 'stage', '--service', 'payment-api',
            '--source', 'kind=mcp', '--source', 'server=kibana-mcp',
            '--query', 'service:payment-api AND token=%s' % self.TOKEN,
            '--address', 'conn=postgresql://logs:%s@db.internal:5432/logs' % self.PASSWORD,
        ])
        self.assertIn('Очистка сработала', out)

        text = self.read_record('SRC-stage-payment-api.md')
        self.assertNotIn(self.TOKEN, text, 'токен из запроса попал в запись карты')
        self.assertNotIn(self.PASSWORD, text, 'пароль из строки подключения попал в запись')
        self.assertIn('<redacted>', text)
        # адресация вокруг секрета остаётся читаемой — иначе записью не
        # воспользоваться
        self.assertIn('service:payment-api', text)
        self.assertIn('db.internal:5432/logs', text)

    def test_secret_does_not_leak_through_the_note(self):
        self.add_source(['--stand', 'stage', '--service', 'payment-api',
                         '--source', 'kind=mcp'])
        code, _out, err = self.run_script(
            'kb_add.py', ['--mark-checked', 'loki-mcp', '--verdict', 'unavailable',
                          '--note', 'ключ api_key=%s больше не принимается' % self.TOKEN,
                          '--stand', 'stage', '--service', 'payment-api', '--kb', self.kb])
        self.assertEqual(code, 0, err)
        text = self.read_record('SRC-stage-payment-api.md')
        self.assertNotIn(self.TOKEN, text)
        self.assertIn('<redacted>', text)

    def test_dry_run_shows_the_scrubbed_text(self):
        """Предпросмотр не должен показывать то, чего запись не сохранит."""
        code, out, err = self.run_script(
            'kb_add.py', ['--kind', 'source', '--stand', 'stage', '--service', 'payment-api',
                          '--source', 'kind=mcp',
                          '--query', 'token=%s' % self.TOKEN,
                          '--kb', self.kb, '--dry-run'])
        self.assertEqual(code, 0, err)
        self.assertNotIn(self.TOKEN, out)
        self.assertIn('<redacted>', out)
        self.assertFalse(os.path.isdir(self.kb), 'dry-run создал базу знаний')


class CrossStandSearch(ScriptCase):
    """5.7 Cross-stand поиск: записи других стендов и разница пустых выдач."""

    def test_records_of_other_stands_are_returned(self):
        hits = self.json_of('kb_search.py',
                            ['--signature', 'ConnectionTimeoutError',
                             '--exclude-stand', 'stage', '--kb', SOURCES_KB])
        self.assertEqual([h['id'] for h in hits], [PROD_INCIDENT])
        self.assertEqual(hits[0]['meta']['stands'], ['prod'])

    def test_stand_date_and_outcome_are_shown_for_each_hit(self):
        code, text, err = self.run_script(
            'kb_search.py', ['--signature', 'ConnectionTimeoutError',
                             '--exclude-stand', 'stage', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('stands: prod', text)
        self.assertIn('2026-07-20', text)
        self.assertIn('исход: подтверждена', text)

    def test_empty_cross_stand_result_differs_from_empty_base(self):
        code, text, err = self.run_script(
            'kb_search.py', ['--signature', 'ConnectionTimeoutError',
                             '--exclude-stand', 'stage', '--exclude-stand', 'prod',
                             '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('на других стендах не встречалась', text)
        self.assertIn('Исключены стенды: stage, prod', text)
        # выдача пуста, но записи в базе есть — иначе след регрессии теряется
        self.assertIn('Всего записей в базе: 2', text)
        self.assertNotIn('Инцидент, похоже, новый', text)

    def test_no_matches_at_all_is_worded_differently(self):
        code, text, err = self.run_script(
            'kb_search.py', ['--signature', 'СовершенноДругаяОшибка',
                             '--exclude-stand', 'stage', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('на других стендах не встречалась', text)

        code, text, err = self.run_script(
            'kb_search.py', ['--signature', 'СовершенноДругаяОшибка', '--kb', SOURCES_KB])
        self.assertEqual(code, 0, err)
        self.assertIn('совпадений нет', text)
        self.assertIn('Инцидент, похоже, новый', text)

    def test_records_without_stand_stay_in_cross_stand_results(self):
        """Про запись без стенда «не тот стенд» неизвестно — она остаётся в выдаче."""
        tmp = self.tmpdir()
        kb = os.path.join(tmp, 'kb')
        shutil.copytree(SOURCES_KB, kb)
        with open(os.path.join(kb, 'INC-2026-07-021-no-stand.md'), 'w',
                  encoding='utf-8') as fh:
            fh.write('---\nid: INC-2026-07-021\nkind: incident\n'
                     'title: "Таймауты пула, стенд не записан"\ndate: 2026-07-21\n'
                     'services: [payment-api]\nsignatures:\n'
                     '  - "ConnectionTimeoutError"\n---\n\n## Симптомы\n\nТаймауты.\n')
        hits = self.json_of('kb_search.py',
                            ['--signature', 'ConnectionTimeoutError',
                             '--exclude-stand', 'stage', '--exclude-stand', 'prod',
                             '--kb', kb])
        self.assertEqual([h['id'] for h in hits], ['INC-2026-07-021'])


class SearchBudgetWithSourceMap(ScriptCase):
    """5.8 Поиск с записями карты в базе укладывается в бюджет времени.

    Меряет `tools/bench.py` — бюджет и способ замера должны быть одни и те же у
    прогона проверок и у разработчика. Эталонный вход здесь меньше, чем у полного
    бенчмарка: проверяется не абсолютная скорость машины, а то, что записи карты
    не превращают поиск в перебор — база сначала мерится без них, потом с ними.
    """

    LINES = 4000
    RECORDS = 300

    def _bench(self, work_dir):
        proc = subprocess.run(
            [sys.executable, BENCH, '--json', '--only', 'kb_search',
             '--lines', str(self.LINES), '--kb-records', str(self.RECORDS),
             '--keep', work_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err = proc.stderr.decode('utf-8', 'replace')
        out = proc.stdout.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 0,
                         'бюджет поиска нарушен или замер упал:\n%s\n%s' % (out, err))
        row = next(r for r in json.loads(out)['results'] if r['key'] == 'kb_search')
        self.assertEqual(row['status'], 'ok', row)
        return row

    def _add_source_records(self, kb_dir, count):
        template = (
            '---\nid: SRC-bench%(n)03d-svc%(n)03d\nkind: source\n'
            'stand: bench%(n)03d\nservices: [svc%(n)03d]\n'
            'source: {kind: mcp, server: kibana-mcp, tool: search_logs}\n'
            'address: {index: logs-bench%(n)03d-*}\n'
            'query: "env:bench%(n)03d AND service:svc%(n)03d AND level:(ERROR OR FATAL)"\n'
            'fields: {time: @timestamp, level: log.level}\nconfirmed: 2026-07-20\n---\n\n'
            '# Карта источников: bench%(n)03d / svc%(n)03d\n')
        for i in range(count):
            with open(os.path.join(kb_dir, 'SRC-BENCH-%03d.md' % i), 'w',
                      encoding='utf-8') as fh:
                fh.write(template % {'n': i})
        code, _out, err = self.run_script('kb_index.py', ['--kb', kb_dir])
        self.assertEqual(code, 0, err)

    def test_search_stays_within_budget_when_map_records_are_added(self):
        work_dir = os.path.join(self.tmpdir(), 'bench')
        before = self._bench(work_dir)
        # столько же записей карты, сколько разборов: карта в базе может быть
        # соизмерима с разборами, и поиск по симптому не должен её читать
        self._add_source_records(os.path.join(work_dir, 'kb'), self.RECORDS)
        after = self._bench(work_dir)
        self.assertLessEqual(after['elapsed'], after['budget'],
                             'поиск с записями карты вышел за бюджет: %s' % after)
        self.assertIsNotNone(before['elapsed'])


if __name__ == '__main__':
    unittest.main()
