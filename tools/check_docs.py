#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка согласованности документации с репозиторием.

Пока единственный жилец — презентация проекта (`docs/presentation.html`,
изменение `add-project-presentation`). Файл сам объявляет свои числовые
утверждения и упомянутые файлы репозитория в двух встроенных JSON-блоках
(`<script type="application/json" id="facts">` и `id="referenced-files">`),
а эта проверка сверяет их с реальными источниками — так расхождение между
презентацией и кодом ловится прогоном, а не на показе.

Дальнейшие проверки документации (например, из `add-readme-maintenance`)
предполагается добавлять сюда же отдельными функциями `check_*`, каждая —
список пар (файл, сообщение) о найденных нарушениях.

  python3 tools/check_docs.py
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRESENTATION = os.path.join(REPO, 'docs', 'presentation.html')

# протокол:// или протокол-независимая ссылка (//host/...) — обращение вовне.
# data: URI и якоря (#slide) обращением к внешнему хосту не считаются.
EXTERNAL_HOST_RE = re.compile(
    r'(?:src|href)\s*=\s*["\']\s*(https?:)?//[^"\']+["\']'
    r'|@import\s+["\']?(https?:)?//'
    r'|url\(\s*["\']?(https?:)?//'
    r'|https?://[^\s"\'<>]+',
)

FACTS_BLOCK_RE = re.compile(
    r'<script[^>]*id=["\']facts["\'][^>]*>(.*?)</script>', re.DOTALL)
FILES_BLOCK_RE = re.compile(
    r'<script[^>]*id=["\']referenced-files["\'][^>]*>(.*?)</script>', re.DOTALL)

# как проверить число из facts против исходника: source_key -> регулярка с
# одной захватывающей группой — числовым значением.
SOURCE_PATTERNS = {
    'parse_logs': r"'parse_logs':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'parse_logs_level': r"'parse_logs_level':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'parse_logs_window': r"'parse_logs_window':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'triage': r"'triage':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'MAX_SUMMARY_CHARS': r'MAX_SUMMARY_CHARS\s*=\s*(\d+)',
    'confirmed_threshold': r'подтверждено данными\*\*[^\d]*([\d.]+)',
    'probable_threshold': r'вероятная причина\*\*[^\d]*([\d.]+)',
}


def load_html():
    if not os.path.isfile(PRESENTATION):
        return None
    with open(PRESENTATION, 'r', encoding='utf-8') as fh:
        return fh.read()


def check_no_external_hosts(html):
    """Файл не должен обращаться ни к одному внешнему хосту."""
    failures = []
    for m in EXTERNAL_HOST_RE.finditer(html):
        failures.append((
            'docs/presentation.html',
            'обращение к внешнему хосту: %r' % m.group(0)[:80],
        ))
    return failures


def _load_json_block(html, pattern, label):
    m = pattern.search(html)
    if not m:
        return None, [('docs/presentation.html', 'блок %s не найден' % label)]
    try:
        return json.loads(m.group(1)), []
    except ValueError as exc:
        return None, [('docs/presentation.html', 'блок %s: не разобрался как JSON (%s)' % (label, exc))]


def check_facts(html):
    """Каждое числовое утверждение презентации сверяется с исходником."""
    facts, failures = _load_json_block(html, FACTS_BLOCK_RE, 'facts')
    if facts is None:
        return failures
    for fact in facts:
        claim = fact.get('claim', '?')
        source_file = fact.get('source_file')
        source_key = fact.get('source_key')
        value = fact.get('value')
        src_path = os.path.join(REPO, source_file) if source_file else None
        if not source_file or not os.path.isfile(src_path):
            failures.append(('facts', 'источник не найден для «%s»: %s' % (claim, source_file)))
            continue
        with open(src_path, 'r', encoding='utf-8') as fh:
            source_text = fh.read()

        if source_key == 'пять вызовов инструментов':
            if 'пять вызовов инструментов' not in source_text:
                failures.append((source_file, 'фраза "пять вызовов инструментов" не найдена — '
                                                'утверждение «%s» не подтверждено' % claim))
            continue

        pattern = SOURCE_PATTERNS.get(source_key)
        if pattern is None:
            failures.append((source_file, 'нет способа проверить source_key=%r (claim: %s)'
                              % (source_key, claim)))
            continue
        m = re.search(pattern, source_text)
        if not m:
            failures.append((source_file, 'значение для «%s» не найдено в источнике' % claim))
            continue
        found = float(m.group(1))
        if found != float(value):
            failures.append((source_file, 'расхождение для «%s»: презентация называет %s, '
                              'источник — %s' % (claim, value, found)))
    return failures


def check_referenced_files(html):
    """Все файлы репозитория, упомянутые в презентации, должны существовать."""
    paths, failures = _load_json_block(html, FILES_BLOCK_RE, 'referenced-files')
    if paths is None:
        return failures
    for rel in paths:
        full = os.path.join(REPO, rel)
        if not os.path.exists(full):
            failures.append(('referenced-files', 'файл не найден: %s' % rel))
    return failures


def check_presentation():
    """Все проверки презентации разом. Возвращает список (место, сообщение)."""
    html = load_html()
    if html is None:
        return [('docs/presentation.html', 'файл не найден')]
    failures = []
    failures += check_no_external_hosts(html)
    failures += check_facts(html)
    failures += check_referenced_files(html)
    return failures


def main(argv=None):
    failures = check_presentation()
    if failures:
        sys.stderr.write('Проверка документации не пройдена:\n')
        for where, message in failures:
            sys.stderr.write('  %s: %s\n' % (where, message))
        return 1
    print('Презентация согласована с репозиторием: '
          'внешних обращений нет, числа и файлы подтверждены.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
