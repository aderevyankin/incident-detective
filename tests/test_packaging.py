# -*- coding: utf-8 -*-
"""Границы набора проверок: он не едет в скилл и не содержит настоящих данных."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import helpers
from helpers import ScriptCase

# то, чего в фикстурах быть не должно: набор лежит в публичном репозитории и сам
# служит примером того, что скилл разрешает сохранять
FORBIDDEN = [
    (re.compile(r'(?i)(password|passwd|secret|api[_-]?key|bearer|authorization)\s*[=:]'),
     'секрет'),
    (re.compile(r'[\w.\-+]+@[\w.\-]+\.\w{2,}'), 'адрес почты'),
    (re.compile(r'\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.[\d.]+'), 'внутренний адрес'),
    (re.compile(r'(?i)\.(?:local|internal|corp|intranet)\b'), 'внутреннее имя хоста'),
]


class Fixtures(ScriptCase):

    def _files(self):
        for root, _dirs, names in os.walk(helpers.FIXTURES):
            for name in sorted(names):
                yield os.path.join(root, name)

    def test_no_real_data_in_fixtures(self):
        for path in self._files():
            with open(path, 'rb') as fh:
                text = fh.read().decode('utf-8', 'replace')
            for pattern, what in FORBIDDEN:
                match = pattern.search(text)
                with self.subTest(fixture=os.path.relpath(path, helpers.REPO), что=what):
                    self.assertIsNone(match, 'похоже на %s: %r' % (
                        what, match.group(0) if match else ''))


class Packaging(ScriptCase):

    def test_tests_live_outside_the_skill(self):
        """Устанавливается директория скилла — проверки в неё не входят."""
        self.assertFalse(
            os.path.abspath(helpers.HERE).startswith(
                os.path.join(helpers.REPO, 'incident-detective') + os.sep),
            'набор проверок лежит внутри устанавливаемого скилла')


class Installer(ScriptCase):
    """Установщик проверяется по результату установки, а не по тексту скрипта.

    Текст проверять бессмысленно: любой рефакторинг валит такую проверку без
    изменения поведения, а настоящий дефект — байткод в поставке — проходит
    незамеченным.
    """

    INSTALLER = os.path.join(helpers.REPO, 'install.sh')
    SRC = os.path.join(helpers.REPO, 'incident-detective')
    INSTALLED = os.path.join('.qwen', 'skills', 'incident-detective')

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='incident-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dest = os.path.join(self.home, self.INSTALLED)

    def install(self, *args):
        """Прогон установщика в изолированном HOME и без терминала на входе."""
        env = os.environ.copy()
        env['HOME'] = self.home
        # свой TMPDIR: резервная папка с записями базы не должна утекать в
        # системный /tmp, если установщик её не убрал
        env['TMPDIR'] = self.home
        with open(os.devnull, 'rb') as devnull:
            proc = subprocess.run(
                ['bash', self.INSTALLER, *args],
                stdin=devnull, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=helpers.REPO, env=env)
        return (proc.returncode,
                proc.stdout.decode('utf-8', 'replace'),
                proc.stderr.decode('utf-8', 'replace'))

    def _plant_bytecode(self):
        """Байткод в источнике: без него проверка чистой поставки проходит впустую."""
        cache = os.path.join(self.SRC, 'scripts', '__pycache__')
        existed = os.path.isdir(cache)
        os.makedirs(cache, exist_ok=True)
        probe = os.path.join(cache, 'installer_probe.cpython-38.pyc')
        with open(probe, 'wb') as fh:
            fh.write(b'not really bytecode')
        self.addCleanup(os.remove, probe)
        if not existed:
            self.addCleanup(os.rmdir, cache)

    def _plant_record(self):
        """Запись базы знаний в установленной копии — как будто ей пользовались."""
        source = os.path.join(helpers.KB, 'INC-2026-05-003-disk-full.md')
        target = os.path.join(self.dest, 'kb', os.path.basename(source))
        shutil.copyfile(source, target)
        return target

    def test_installs_the_skill_without_development_artifacts(self):
        self._plant_bytecode()
        code, out, err = self.install()
        self.assertEqual(code, 0, 'установщик завершился с кодом %d\n%s' % (code, err))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'SKILL.md')),
                        'в установленной директории нет SKILL.md\n%s' % out)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'scripts', 'parse_logs.py')),
                        'в установленной директории нет скриптов скилла')

        junk = []
        for root, dirs, names in os.walk(self.dest):
            if '__pycache__' in dirs:
                junk.append(os.path.relpath(os.path.join(root, '__pycache__'), self.dest))
            junk.extend(os.path.relpath(os.path.join(root, n), self.dest)
                        for n in names if n.endswith('.pyc'))
        self.assertEqual(junk, [], 'артефакты разработки поехали в установку: %s' % junk)

    def test_reinstall_keeps_knowledge_base_records(self):
        self.assertEqual(self.install()[0], 0)
        record = self._plant_record()

        code, out, err = self.install('--yes')
        self.assertEqual(code, 0, 'переустановка завершилась с кодом %d\n%s' % (code, err))
        self.assertTrue(os.path.isfile(record),
                        'запись базы знаний не пережила переустановку\n%s' % out)

        index_path = os.path.join(self.dest, 'kb', 'index.json')
        self.assertTrue(os.path.isfile(index_path), 'индекс базы не собран')
        with open(index_path, 'r', encoding='utf-8') as fh:
            index = json.load(fh)
        self.assertEqual(index.get('count'), 1,
                         'индекс не видит сохранённую запись: %r' % index.get('count'))

        # путь резервной копии напечатан — иначе при сбое записи «сохранены»
        # туда, где их никто не найдёт
        self.assertIn('Записи базы сохранены:', out)
        # ...и при успешном возврате папка убрана
        leftovers = [n for n in os.listdir(self.home) if n.startswith('tmp')]
        self.assertEqual(leftovers, [],
                         'резервная папка осталась после успешной переустановки: %s' % leftovers)

    def test_reinstall_without_tty_and_without_yes_explains_itself(self):
        self.assertEqual(self.install()[0], 0)

        code, _out, err = self.install()
        self.assertNotEqual(code, 0, 'переустановка без подтверждения прошла молча')
        self.assertIn('--yes', err, 'в объяснении нет указания на флаг:\n%s' % err)
        self.assertNotIn('read:', err, 'скрипт упал на чтении ввода:\n%s' % err)

    def test_unknown_argument_is_refused(self):
        code, _out, err = self.install('--projekt')
        self.assertNotEqual(code, 0, 'неизвестный аргумент не остановил установку')
        self.assertIn('--projekt', err)
        self.assertFalse(os.path.exists(self.dest),
                         'установка выполнилась несмотря на неизвестный аргумент')


if __name__ == '__main__':
    unittest.main()
