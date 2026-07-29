# -*- coding: utf-8 -*-
"""База знаний: поиск по тексту и по сигнатурам, пересборка индекса."""

import json
import os
import shutil
import unittest

import helpers
from helpers import ScriptCase

PAYMENT = 'INC-2026-07-001'
DISK = 'INC-2026-05-003'


class Search(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()

    def test_free_text_finds_expected_record(self):
        hits = self.json_of('kb_search.py',
                            ['таймаут', 'пул', 'соединений', 'payment-api',
                             '--kb', helpers.KB])
        self.assertTrue(hits, 'по свободному тексту ничего не нашлось')
        self.assertEqual(hits[0]['id'], PAYMENT)

    def test_signatures_from_parsed_find_expected_record(self):
        _parsed, path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('single_service.log')])
        hits = self.json_of('kb_search.py', ['--from-parsed', path, '--kb', helpers.KB])
        self.assertTrue(hits, 'по сигнатурам разбора ничего не нашлось')
        self.assertEqual(hits[0]['id'], PAYMENT)
        self.assertTrue(any('сигнатура' in r for r in hits[0]['reasons']),
                        'совпадение объяснено не сигнатурой: %s' % hits[0]['reasons'])

    def test_irrelevant_record_stays_out(self):
        _parsed, path = helpers.parsed_to_file(
            self, self.tmp, 'parsed.json', [helpers.log('single_service.log')])
        hits = self.json_of('kb_search.py', ['--from-parsed', path, '--kb', helpers.KB])
        self.assertNotIn(DISK, [h['id'] for h in hits],
                         'в выдачу попала запись про переполненный диск')

    def test_filter_by_service_is_hard(self):
        hits = self.json_of('kb_search.py',
                            ['таймаут', 'пул', '--service', 'logging-agent',
                             '--kb', helpers.KB])
        self.assertEqual(hits, [], 'фильтр по сервису не сработал как жёсткий')


class Index(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()
        self.kb = os.path.join(self.tmp, 'kb')
        shutil.copytree(helpers.KB, self.kb)

    def _rebuild(self):
        code, out, err = self.run_script('kb_index.py', ['--kb', self.kb])
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.kb, 'index.json'), 'r', encoding='utf-8') as fh:
            return json.load(fh), out

    def test_index_matches_records_on_disk(self):
        index, out = self._rebuild()
        on_disk = sorted(n for n in os.listdir(self.kb) if n.endswith('.md'))
        self.assertEqual(index['count'], len(on_disk))
        self.assertEqual(sorted(e['file'] for e in index['incidents']), on_disk)
        self.assertIn('3 записей', out)
        ids = {e['id'] for e in index['incidents']}
        self.assertIn(PAYMENT, ids)

    def test_search_by_index_matches_search_by_markdown(self):
        """Ответ поиска не должен зависеть от того, читан индекс или markdown."""
        query = ['таймаут', 'пул', 'соединений', '--kb', self.kb]
        before = self.json_of('kb_search.py', query)
        self._rebuild()
        after = self.json_of('kb_search.py', query)
        self.assertEqual([(h['id'], h['score']) for h in before],
                         [(h['id'], h['score']) for h in after])

    def test_stale_index_is_reported(self):
        self._rebuild()
        os.remove(os.path.join(self.kb, 'INC-2026-05-003-disk-full.md'))
        code, _out, err = self.run_script(
            'kb_search.py', ['таймаут', 'пул', '--kb', self.kb, '--format', 'json'])
        self.assertEqual(code, 0, err)
        self.assertIn('индекс базы знаний устарел', err)


class Add(ScriptCase):
    """kb_add.py по результату: реальная запись, а не только --dry-run."""

    def setUp(self):
        self.tmp = self.tmpdir()
        self.kb = os.path.join(self.tmp, 'kb')

    def _add(self, title, **kw):
        args = ['--title', title, '--kb', self.kb]
        for flag, value in kw.items():
            args += ['--' + flag.replace('_', '-'), value]
        code, out, err = self.run_script('kb_add.py', args)
        self.assertEqual(code, 0, err)
        return out

    def _record_path(self, out):
        for line in out.split('\n'):
            if line.startswith('Запись '):
                return line.split(': ', 1)[1].strip()
        raise AssertionError('строка "Запись ..." не найдена в выводе:\n%s' % out)

    def test_written_record_reads_back(self):
        out = self._add('Пул соединений исчерпан', service='payment-api',
                        root_cause='утечка соединений')
        path = self._record_path(out)
        self.assertTrue(os.path.exists(path), 'файл записи не создан: %s' % path)
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('title: Пул соединений исчерпан', text)
        self.assertIn('date: 2026-07-28', text)
        self.assertIn('services: [payment-api]', text)
        self.assertIn('утечка соединений', text)

        # обратное чтение через kb_search.py — не только на диске, но и разбирается
        self.assertIn('id: INC-2026-07-001', out)
        hits = self.json_of('kb_search.py', ['утечка', 'соединений', '--kb', self.kb])
        self.assertTrue(hits, 'записанная запись не находится поиском')
        self.assertEqual(hits[0]['id'], 'INC-2026-07-001')

    def test_record_without_stand_and_tags_keeps_mandatory_fields(self):
        """Запись без `--stand` и `--tags` всё равно содержит эти поля пустыми."""
        out = self._add('Запись без стендов и тегов')
        with open(self._record_path(out), 'r', encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('stands: []', text)
        self.assertIn('tags: []', text)
        # необязательные списки пустыми в запись не лезут
        self.assertNotIn('files:', text)
        self.assertNotIn('related:', text)

    def test_index_is_updated_after_write(self):
        self._add('Первая запись')
        with open(os.path.join(self.kb, 'index.json'), 'r', encoding='utf-8') as fh:
            index = json.load(fh)
        self.assertEqual(index['count'], 1)
        self.assertEqual(index['incidents'][0]['id'], 'INC-2026-07-001')

    def test_second_record_same_month_gets_next_id(self):
        first = self._add('Первая запись')
        second = self._add('Вторая запись')
        self.assertIn('INC-2026-07-001', first)
        self.assertIn('INC-2026-07-002', second)
        with open(os.path.join(self.kb, 'index.json'), 'r', encoding='utf-8') as fh:
            index = json.load(fh)
        self.assertEqual(index['count'], 2)

    def test_other_records_are_left_untouched(self):
        os.makedirs(self.kb)
        other = os.path.join(self.kb, 'INC-2026-05-003-disk-full.md')
        shutil.copy(os.path.join(helpers.KB, 'INC-2026-05-003-disk-full.md'), other)
        with open(other, 'rb') as fh:
            before = fh.read()
        self._add('Новая запись')
        with open(other, 'rb') as fh:
            after = fh.read()
        self.assertEqual(before, after, 'чужая запись изменилась при добавлении новой')


if __name__ == '__main__':
    unittest.main()
