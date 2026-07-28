# -*- coding: utf-8 -*-
"""Самопроверка проверки документации.

`tools/check_docs.py` — единственное, что не даёт README и walkthrough разойтись
с репозиторием, и ломается он бесшумно: стоит извлечению блоков перестать что-либо
находить, как проверка выдаёт зелёный прогон, не выполнив ни одной команды.
Поэтому здесь два слоя: юнит-тесты экстрактора на подготовленном тексте и нижняя
граница исполненных блоков живого README.
"""

import os
import sys
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.REPO, 'tools'))
import check_docs  # noqa: E402


class ExtractBlocks(unittest.TestCase):
    """`extract_blocks`: языковые метки, skip-маркеры, границы блоков."""

    def test_language_tag_is_kept(self):
        blocks = check_docs.extract_blocks(
            'текст\n\n```bash\necho ok\n```\n\n```\nбез метки\n```\n')
        self.assertEqual([b[0] for b in blocks], ['bash', ''])
        self.assertEqual(blocks[0][1], 'echo ok')

    def test_multiline_body_is_kept_whole(self):
        blocks = check_docs.extract_blocks('```bash\nls\ncd /\n```\n')
        self.assertEqual(blocks[0][1], 'ls\ncd /')

    def test_skip_marker_marks_the_next_block(self):
        blocks = check_docs.extract_blocks(
            '<!-- check-docs: skip (нужна сеть) -->\n\n```bash\ncurl example\n```\n')
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0][2], 'блок должен быть помечен пропуском')

    def test_skip_marker_does_not_spread_to_later_blocks(self):
        """Пометка относится к ближайшему блоку: содержательная строка её снимает."""
        blocks = check_docs.extract_blocks(
            '<!-- check-docs: skip (нужна сеть) -->\n\n```bash\ncurl example\n```\n\n'
            'обычный абзац\n\n```bash\necho ok\n```\n')
        self.assertEqual([bool(b[2]) for b in blocks], [True, False])

    def test_multiline_comment_marker_is_recognised(self):
        blocks = check_docs.extract_blocks(
            '<!-- check-docs: skip\n     (прогон всего набора проверок)\n-->\n\n'
            '```bash\npython3 tests/run.py\n```\n')
        self.assertTrue(blocks[0][2])

    def test_comment_without_marker_does_not_skip(self):
        blocks = check_docs.extract_blocks('<!-- просто пояснение -->\n\n```bash\nls\n```\n')
        self.assertFalse(blocks[0][2])

    def test_no_blocks_in_plain_text(self):
        self.assertEqual(check_docs.extract_blocks('просто текст\n\nбез кода\n'), [])


class StructurePaths(unittest.TestCase):
    """`resolve_structure_paths`: дерево из README превращается в пути и шаблоны."""

    BLOCK = [
        'incident-detective/',
        '├── SKILL.md',
        '├── scripts/',
        '│   ├── triage.py',
        '│   └── kb_*.py',
        '└── kb/',
    ]

    def test_paths_and_globs_are_separated(self):
        paths, globs = check_docs.resolve_structure_paths(self.BLOCK)
        self.assertIn('incident-detective/SKILL.md', paths)
        self.assertIn('incident-detective/scripts/triage.py', paths)
        self.assertIn('incident-detective/scripts/kb_*.py', globs)

    def test_nested_directory_is_prefixed_to_its_children(self):
        paths, _globs = check_docs.resolve_structure_paths(self.BLOCK)
        self.assertNotIn('incident-detective/triage.py', paths,
                         'вложенность потеряна: файл приписан не той директории')

    def test_empty_block_gives_nothing(self):
        self.assertEqual(check_docs.resolve_structure_paths([]), ([], []))


class SelfCheck(unittest.TestCase):
    """Сломанный экстрактор роняет прогон, а не выдаёт пустой успех."""

    def _readme(self):
        return check_docs.read(os.path.join(helpers.REPO, 'README.md'))

    def _runnable_blocks(self, text):
        return [b for b in check_docs.extract_blocks(text)
                if b[0] in ('bash', 'sh', 'shell') and not b[2] and b[1].strip()]

    def test_readme_still_has_enough_command_blocks(self):
        """Живой README отдаёт экстрактору не меньше блоков, чем требует граница.

        Команды здесь не исполняются — их исполняет сам `check_docs` в прогоне;
        проверяется именно то, что экстрактор их находит.
        """
        found = len(self._runnable_blocks(self._readme()))
        self.assertGreaterEqual(
            found, check_docs.MIN_README_COMMAND_BLOCKS,
            'экстрактор нашёл в README %d исполняемых блоков, граница — %d'
            % (found, check_docs.MIN_README_COMMAND_BLOCKS))

    def test_broken_extractor_is_reported_as_failure(self):
        """Экстрактор, не нашедший ни одного блока, обязан уронить проверку."""
        original = check_docs.extract_blocks
        self.addCleanup(setattr, check_docs, 'extract_blocks', original)
        check_docs.extract_blocks = lambda text: []

        failures = []
        executed = check_docs.run_command_blocks('README.md', self._readme(), failures)
        self.assertEqual(executed, 0)
        self.assertEqual(failures, [], 'нечего исполнять — и ошибок команд быть не должно')

        check_docs.check_command_blocks_minimum(executed, failures)
        self.assertTrue(failures, 'пустое извлечение блоков осталось незамеченным')
        self.assertIn('README.md', failures[0])
        self.assertIn(str(check_docs.MIN_README_COMMAND_BLOCKS), failures[0])

    def test_minimum_passes_when_blocks_are_executed(self):
        failures = []
        check_docs.check_command_blocks_minimum(
            check_docs.MIN_README_COMMAND_BLOCKS, failures)
        self.assertEqual(failures, [])


if __name__ == '__main__':
    unittest.main()
