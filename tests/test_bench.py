# -*- coding: utf-8 -*-
"""Бенчмарк бюджетов: вердикт, множитель щадящего порога, негативные сценарии.

Сами числа бюджетов здесь не проверяются — они зависят от машины, и строгий
прогон `tools/bench.py` остаётся ручным. Проверяется инструмент: что нарушение
он замечает и называет, что неизвестный ключ не превращается в пустой успешный
прогон и что множитель ослабляет порог ровно так, как объявлено.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers

sys.path.insert(0, os.path.join(helpers.REPO, 'tools'))
import bench  # noqa: E402


class Threshold(unittest.TestCase):
    """Множитель ослабляет порог по смыслу замера: время — вверх, скорость — вниз."""

    def test_time_budget_is_multiplied(self):
        self.assertEqual(bench.threshold('confidence'), 1.0)
        self.assertEqual(bench.threshold('confidence', 5), 5.0)

    def test_rate_budget_is_divided(self):
        self.assertEqual(bench.threshold('parse_logs'), 50000)
        self.assertEqual(bench.threshold('parse_logs', 5), 10000)

    def test_verdict_follows_the_scaled_threshold(self):
        # 2 с при бюджете ≤ 1 с: строго — нарушение, с множителем 5 — в пределах
        self.assertEqual(bench.verdict('confidence', 2.0, None)[0], 'НАРУШЕН')
        self.assertEqual(bench.verdict('confidence', 2.0, None, 5)[0], 'ok')
        # 20 тыс. строк/с при бюджете ≥ 50 тыс. — то же в обратную сторону
        self.assertEqual(bench.verdict('parse_logs', 1.0, 20000)[0], 'НАРУШЕН')
        self.assertEqual(bench.verdict('parse_logs', 1.0, 20000, 5)[0], 'ok')

    def test_without_scale_verdict_is_unchanged(self):
        for key in bench.BUDGETS:
            with self.subTest(замер=key):
                self.assertEqual(bench.threshold(key, 1.0), bench.BUDGETS[key]['limit'])

    def test_skipped_measurement_stays_skipped(self):
        self.assertEqual(bench.verdict('confidence', None, None, 5)[0], 'пропущен')


class Negative(unittest.TestCase):
    """`--only` с неизвестным ключом и `--budget-scale` меньше единицы."""

    def _run(self, args):
        proc = subprocess.run(
            [sys.executable, os.path.join(helpers.REPO, 'tools', 'bench.py')] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return (proc.returncode, proc.stdout.decode('utf-8', 'replace'),
                proc.stderr.decode('utf-8', 'replace'))

    def test_unknown_only_key_lists_the_known_ones(self):
        code, out, err = self._run(['--only', 'parse_log'])
        self.assertNotEqual(code, 0, 'неизвестный ключ не должен давать успешный прогон')
        self.assertIn('parse_log', err)
        for key in bench.BUDGETS:
            self.assertIn(key, err, 'в сообщении не перечислен известный ключ %s' % key)
        self.assertEqual(out.strip(), '', 'при ошибке замеры выводиться не должны')

    def test_scale_below_one_is_rejected(self):
        """Множитель ослабляет бюджеты; ужесточение через него — опечатка, а не режим."""
        code, _out, err = self._run(['--budget-scale', '0.5'])
        self.assertNotEqual(code, 0)
        self.assertIn('--budget-scale', err)


class ViolatedBudget(unittest.TestCase):
    """Нарушенный бюджет: ненулевой код и обе величины в выводе.

    Замеряется самый дешёвый скрипт на урезанном входе — проверяется реакция на
    нарушение, а не скорость. Бюджет подменяется заведомо недостижимым: замедлять
    скрипт ради проверки инструмента незачем. Эталонный вход генерируется один раз
    и переиспользуется обоими прогонами (`--keep`).
    """

    ARGS = ['--lines', '2000', '--kb-records', '5', '--only', 'confidence']

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix='triage-bench-tests-')
        self.addCleanup(shutil.rmtree, self.work, True)
        original = bench.BUDGETS['confidence']['limit']
        self.addCleanup(bench.BUDGETS['confidence'].__setitem__, 'limit', original)
        # 1 мкс не достижимы даже пустым процессом — нарушение гарантировано
        bench.BUDGETS['confidence']['limit'] = 0.000001

    def _bench(self, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bench.main(self.ARGS + ['--keep', self.work] + list(extra))
        return code, buf.getvalue()

    def test_strict_run_fails_and_names_both_values(self):
        code, out = self._bench()
        self.assertEqual(code, 1, 'нарушенный бюджет обязан дать ненулевой код')
        self.assertIn('confidence.py', out, 'не назван скрипт')
        self.assertIn('НАРУШЕН', out)
        self.assertIn('Нарушено бюджетов: 1', out)
        # замер и бюджет — обе величины в одной строке таблицы
        row = [l for l in out.splitlines() if l.startswith('confidence.py')][0]
        self.assertIn(' с', row)
        self.assertIn('≤ 0.0 с', row)

    def test_scale_relaxes_the_same_run_to_green(self):
        """Тот же замер: строго — нарушение, с множителем — в пределах порога."""
        strict_code, _out = self._bench()
        self.assertEqual(strict_code, 1)
        # порог 0.000001 × 1 000 000 = 1 с — замер confidence укладывается
        scaled_code, out = self._bench(['--budget-scale', '1000000'])
        self.assertEqual(scaled_code, 0, 'множитель не ослабил порог:\n%s' % out)
        self.assertNotIn('НАРУШЕН', out)
        self.assertIn('множитель', out, 'множитель не назван в выводе')
        row = [l for l in out.splitlines() if l.startswith('confidence.py')][0]
        self.assertIn('≤ 0.0 с', row, 'исходный бюджет пропал из вывода')
        self.assertIn('≤ 1.0 с', row, 'действующий порог не показан')


if __name__ == '__main__':
    unittest.main()
