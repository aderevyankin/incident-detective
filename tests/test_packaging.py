# -*- coding: utf-8 -*-
"""Границы набора проверок: он не едет в скилл и не содержит настоящих данных."""

import os
import re
import subprocess
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


class Delivery(ScriptCase):
    """Чистота поставки — свойство содержимого репозитория, а не шага установки.

    Установщика нет: скилл разворачивается копированием выгрузки репозитория,
    поэтому единственное место, где артефакт разработки может доехать до
    пользователя, — версионный контроль.
    """

    def test_no_development_artifacts_under_version_control(self):
        try:
            proc = subprocess.run(
                ['git', 'ls-files', '--', 'incident-detective'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=helpers.REPO)
        except OSError:
            self.skipTest('git недоступен — список отслеживаемых файлов не получить')
        if proc.returncode != 0:
            self.skipTest('не git-репозиторий — список отслеживаемых файлов не получить')
        tracked = proc.stdout.decode('utf-8', 'replace').splitlines()
        junk = [path for path in tracked
                if path.endswith('.pyc') or '__pycache__' in path.split('/')]
        self.assertEqual(junk, [],
                         'артефакты разработки под версионным контролем: %s' % junk)


if __name__ == '__main__':
    unittest.main()
