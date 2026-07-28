# -*- coding: utf-8 -*-
"""Производные копии оснастки разработки не разъезжаются молча.

`.qwen/` — источник, `.claude/` — производная: скиллы OpenSpec лежат в обоих
деревьях побайтовыми копиями, а команды `/opsx:*` — в разных обёртках (TOML и
markdown с фронтматтером) при одном и том же теле промпта. Правка одной копии
без второй ничем, кроме этой проверки, не ловится, поэтому падение называет
конкретный файл — иначе искать расхождение приходится глазами.

К поставке скилла оснастка отношения не имеет: пользователю едет только
`incident-detective/`.
"""

import os
import re
import unittest

import helpers

QWEN = os.path.join(helpers.REPO, '.qwen')
CLAUDE = os.path.join(helpers.REPO, '.claude')

# шесть скиллов OpenSpec, установленных обеим платформам
SKILLS = [
    'openspec-apply-change',
    'openspec-archive-change',
    'openspec-explore',
    'openspec-propose',
    'openspec-sync-specs',
    'openspec-update-change',
]

# команда → файл у каждой платформы: имена и расширения разные, тело одно
COMMANDS = ['apply', 'archive', 'explore', 'propose', 'sync', 'update']

FRONTMATTER_RE = re.compile(r'\A---\n.*?\n---\n', re.S)
PROMPT_OPEN = 'prompt = """'


def read(path):
    with open(path, 'rb') as fh:
        return fh.read().decode('utf-8')


def relative_files(root):
    """Пути файлов относительно root, отсортированные — для сверки составов."""
    found = []
    for base, _dirs, names in os.walk(root):
        for name in sorted(names):
            full = os.path.join(base, name)
            found.append(os.path.relpath(full, root))
    return sorted(found)


def toml_prompt(text, path):
    """Тело промпта из .qwen/commands/*.toml — между маркерами многострочной строки."""
    if PROMPT_OPEN not in text:
        raise AssertionError('%s: не найден маркер %r' % (path, PROMPT_OPEN))
    return text.split(PROMPT_OPEN, 1)[1].rsplit('"""', 1)[0].strip()


def markdown_body(text):
    """Тело промпта из .claude/commands/opsx/*.md — всё после YAML-фронтматтера."""
    return FRONTMATTER_RE.sub('', text).strip()


class Skills(unittest.TestCase):
    """`.claude/skills` ↔ `.qwen/skills` — побайтовые копии."""

    def test_same_file_lists(self):
        for skill in SKILLS:
            qwen = os.path.join(QWEN, 'skills', skill)
            claude = os.path.join(CLAUDE, 'skills', skill)
            with self.subTest(скилл=skill):
                self.assertTrue(os.path.isdir(qwen), '%s отсутствует' % qwen)
                self.assertTrue(os.path.isdir(claude), '%s отсутствует' % claude)
                self.assertEqual(
                    relative_files(qwen), relative_files(claude),
                    'состав файлов разошёлся: .qwen/skills/%s ↔ .claude/skills/%s'
                    % (skill, skill))

    def test_same_content(self):
        for skill in SKILLS:
            qwen = os.path.join(QWEN, 'skills', skill)
            for rel in relative_files(qwen):
                claude_file = os.path.join(CLAUDE, 'skills', skill, rel)
                with self.subTest(файл=os.path.join(skill, rel)):
                    if not os.path.isfile(claude_file):
                        self.fail('нет копии: .claude/skills/%s/%s' % (skill, rel))
                    self.assertEqual(
                        read(os.path.join(qwen, rel)), read(claude_file),
                        'содержимое разошлось: .qwen/skills/%s/%s ↔ '
                        '.claude/skills/%s/%s (источник — .qwen/)'
                        % (skill, rel, skill, rel))


class Commands(unittest.TestCase):
    """`.claude/commands/opsx/*.md` ↔ `.qwen/commands/opsx-*.toml` — совпадают тела.

    Обёртки разные по устройству платформ, поэтому сверяется только промпт:
    сравнение файлов целиком падало бы всегда.
    """

    def test_both_platforms_have_all_six(self):
        self.assertEqual(
            sorted(name for name in os.listdir(os.path.join(QWEN, 'commands'))
                   if name.endswith('.toml')),
            ['opsx-%s.toml' % name for name in COMMANDS])
        self.assertEqual(
            sorted(name for name in os.listdir(os.path.join(CLAUDE, 'commands', 'opsx'))
                   if name.endswith('.md')),
            sorted('%s.md' % name for name in COMMANDS))

    def test_same_prompt_bodies(self):
        for name in COMMANDS:
            qwen_file = os.path.join(QWEN, 'commands', 'opsx-%s.toml' % name)
            claude_file = os.path.join(CLAUDE, 'commands', 'opsx', '%s.md' % name)
            with self.subTest(команда='/opsx:%s' % name):
                self.assertEqual(
                    toml_prompt(read(qwen_file), qwen_file),
                    markdown_body(read(claude_file)),
                    'тело промпта разошлось: .qwen/commands/opsx-%s.toml ↔ '
                    '.claude/commands/opsx/%s.md (источник — .qwen/)' % (name, name))


if __name__ == '__main__':
    unittest.main()
