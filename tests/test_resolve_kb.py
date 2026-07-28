# -*- coding: utf-8 -*-
"""Приоритет расположения базы знаний: `--kb` → `INCIDENT_KB_DIR` → база в
корне проекта (если уже есть) → база внутри скилла.

Проверяется через `kb_search.py --list`: на пустой директории он честно
называет её путь («База знаний пуста или не найдена: <путь>»), и по этому пути
видно, какая ступень сработала — без нужды заглядывать внутрь resolve_kb.
"""

import os
import subprocess
import unittest

import helpers
from helpers import ScriptCase

DEFAULT_KB = os.path.join(os.path.dirname(helpers.SCRIPTS), 'kb')
HAS_GIT = helpers.has_git()


def _resolved_dir(out):
    for line in out.split('\n'):
        if line.startswith('База знаний пуста или не найдена: '):
            return line[len('База знаний пуста или не найдена: '):].strip()
    raise AssertionError('строка с путём базы не найдена в выводе:\n%s' % out)


class Priority(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()
        self.plain_cwd = os.path.join(self.tmp, 'not-a-repo')
        os.makedirs(self.plain_cwd)

    def test_flag_wins_over_everything(self):
        flag_kb = os.path.join(self.tmp, 'flag-kb')
        os.makedirs(flag_kb)
        env_kb = os.path.join(self.tmp, 'env-kb')
        os.makedirs(env_kb)
        code, out, err = self.run_script(
            'kb_search.py', ['--list', '--kb', flag_kb],
            env={'INCIDENT_KB_DIR': env_kb}, cwd=self.plain_cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(os.path.realpath(_resolved_dir(out)), os.path.realpath(flag_kb))

    def test_env_var_used_without_flag(self):
        env_kb = os.path.join(self.tmp, 'env-kb')
        os.makedirs(env_kb)
        code, out, err = self.run_script(
            'kb_search.py', ['--list'],
            env={'INCIDENT_KB_DIR': env_kb}, cwd=self.plain_cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(os.path.realpath(_resolved_dir(out)), os.path.realpath(env_kb))

    @unittest.skipUnless(HAS_GIT, 'нужен git — ступень проекта не проверяется без него')
    def test_project_root_kb_used_when_it_already_exists(self):
        project = os.path.join(self.tmp, 'project')
        kb = os.path.join(project, 'memory', 'knowledgebase')
        os.makedirs(kb)
        subprocess.run(['git', 'init', '-q'], cwd=project, check=True)
        code, out, err = self.run_script('kb_search.py', ['--list'], cwd=project)
        self.assertEqual(code, 0, err)
        self.assertEqual(os.path.realpath(_resolved_dir(out)), os.path.realpath(kb))

    @unittest.skipUnless(HAS_GIT, 'нужен git — ступень проекта не проверяется без него')
    def test_project_root_kb_not_created_and_not_used_when_missing(self):
        """Ступень проекта — чтение уже сделанного выбора, а не создание директории."""
        project = os.path.join(self.tmp, 'project')
        os.makedirs(project)
        subprocess.run(['git', 'init', '-q'], cwd=project, check=True)
        kb = os.path.join(project, 'memory', 'knowledgebase')
        code, out, err = self.run_script('kb_search.py', ['--list'], cwd=project)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(kb), 'resolve_kb не должен создавать базу проекта')
        self.assertEqual(os.path.realpath(_resolved_dir(out)), os.path.realpath(DEFAULT_KB))

    def test_default_is_the_skill_kb(self):
        code, out, err = self.run_script('kb_search.py', ['--list'], cwd=self.plain_cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(os.path.realpath(_resolved_dir(out)), os.path.realpath(DEFAULT_KB))


if __name__ == '__main__':
    unittest.main()
