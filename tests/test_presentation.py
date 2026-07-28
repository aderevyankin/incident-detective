# -*- coding: utf-8 -*-
"""Презентация проекта (`docs/presentation.html`) остаётся согласованной с репозиторием.

Проверка сама по себе живёт в `tools/check_docs.py` — здесь только включение в общий
прогон, как того требует `add-project-presentation`.
"""

import os
import sys
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.REPO, 'tools'))

import check_docs


class Presentation(unittest.TestCase):

    def test_file_exists(self):
        self.assertTrue(
            os.path.isfile(os.path.join(helpers.REPO, 'docs', 'presentation.html')),
            'docs/presentation.html отсутствует')

    def test_no_external_hosts_facts_and_files_check_out(self):
        failures = []
        check_docs.check_presentation(failures)
        self.assertEqual(failures, [], 'check_docs.py нашёл расхождения: %r' % (failures,))

    def test_main_returns_zero(self):
        self.assertEqual(check_docs.main([]), 0)


if __name__ == '__main__':
    unittest.main()
