# -*- coding: utf-8 -*-
"""Контур кода: связь ошибок из логов с файлами и историей git-репозитория.

Возражение «нужен git-репозиторий» слабое: репозиторий на два коммита
собирается в setUp за доли секунды (helpers.make_repo). Без git тесты этого
класса пропускаются с пометкой, как и проверка совместимости.
"""

import json
import os
import unittest

import helpers
from helpers import ScriptCase

HAS_GIT = helpers.has_git()


@unittest.skipUnless(HAS_GIT, 'нужен git — тесты контура кода пропущены')
class Repo(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()
        self.info = helpers.make_repo(self.tmp)

    def test_signature_finds_file_and_place(self):
        data = self.json_of('code_hints.py',
                            ['--repo', self.info['repo'],
                             '--signature', self.info['exc_name']])
        self.assertTrue(data['git'], 'репозиторий должен опознаться как git')
        hits = [h for h in data['grep'] if h['line'] == self.info['lineno']]
        self.assertTrue(hits, 'место определения класса исключения не найдено:\n%s'
                        % json.dumps(data['grep'], ensure_ascii=False, indent=2))
        self.assertTrue(hits[0]['path'].endswith(self.info['file'].replace(os.sep, '/')),
                        'найден не тот файл: %s' % hits[0]['path'])

    def test_nonexistent_repo_is_explained_not_a_traceback(self):
        code, _out, err = self.run_script(
            'code_hints.py', ['--repo', '/нет/такой/директории', '--text', 'ошибка'])
        self.assertNotEqual(code, 0)
        self.assertNotIn('Traceback', err)
        self.assertIn('Нет такой директории', err)

    def test_directory_without_git_is_explained_not_a_traceback(self):
        plain = os.path.join(self.tmp, 'plain')
        os.makedirs(plain)
        with open(os.path.join(plain, 'a.py'), 'w', encoding='utf-8') as fh:
            fh.write('x = 1\n')
        code, out, err = self.run_script(
            'code_hints.py', ['--repo', plain, '--text', 'что-то сломалось'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('Traceback', err)
        self.assertIn('не git', out)

        data = self.json_of('code_hints.py', ['--repo', plain, '--text', 'что-то сломалось'])
        self.assertFalse(data['git'])
        self.assertEqual(data['commits'], [])


if __name__ == '__main__':
    unittest.main()
