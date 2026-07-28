#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка, что README.md и docs/walkthrough.md не разошлись с репозиторием.

Документация устаревает молча: команда меняет флаг, capability переименовывается,
файл в разделе «Структура» переезжает — и текст остаётся прежним, пока кто-то не
заметит глазами. Эта проверка ловит три конкретных расхождения:

  1. команды из блоков ``` ```bash ``` `` в README.md и docs/walkthrough.md
     действительно запускаются и завершаются нулевым кодом;
  2. перечень capability в таблице README.md совпадает с директориями
     openspec/specs/;
  3. дерево файлов в разделе «Структура» README.md совпадает с тем, что лежит в
     репозитории.

Блок кода пропускается, если непосредственно перед ним есть HTML-комментарий вида
`<!-- check-docs: skip (причина) -->` — команда требует внешнего окружения
(установка в домашнюю директорию, сеть, самоссылающийся прогон всего набора
проверок, время выполнения, зависящее от машины) и не может быть частью
воспроизводимой проверки на голом Python.

  python3 tools/check_docs.py

Только стандартная библиотека, Python 3.8+ — как и весь остальной проект.
"""

import glob
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECS_DIR = os.path.join(REPO, 'openspec', 'specs')
DOC_FILES = ['README.md', os.path.join('docs', 'walkthrough.md')]

SKIP_MARKER = re.compile(r'<!--\s*check-docs:\s*skip')
FENCE_RE = re.compile(r'^```(\w*)\s*$')
CAP_ROW_RE = re.compile(r'^\|\s*`([a-z][a-z0-9-]*)`\s*\|')
TREE_MARKER_RE = re.compile(r'^((?:\u2502   |    )*)(\u251c\u2500\u2500 |\u2514\u2500\u2500 )?(.*)$')


def read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


# ---- 1. команды из блоков кода -------------------------------------------

def extract_blocks(text):
    """Возвращает список (язык, текст_блока, пометка_пропуска_или_None).

    Пометка — HTML-комментарий `<!-- check-docs: skip ... -->`, который стоит
    где-то до блока, с пустыми строками вокруг, но без другого содержимого между
    собой и блоком: любая содержательная строка между ними сбрасывает пометку —
    она относится к одному, ближайшему следующему блоку, а не ко всем последующим.
    """
    lines = text.splitlines()
    blocks = []
    pending = None
    in_comment = False
    buffer = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if in_comment:
            buffer.append(line)
            if '-->' in line:
                in_comment = False
                joined = '\n'.join(buffer)
                if SKIP_MARKER.search(joined):
                    pending = joined
                buffer = []
            i += 1
            continue

        if stripped.startswith('<!--'):
            if '-->' in stripped:
                if SKIP_MARKER.search(stripped):
                    pending = stripped
            else:
                in_comment = True
                buffer = [line]
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        m = FENCE_RE.match(line)
        if m:
            skip = pending
            pending = None
            lang = m.group(1)
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                body.append(lines[i])
                i += 1
            blocks.append((lang, '\n'.join(body), skip))
            i += 1
            continue

        # обычная содержательная строка — отменяет отложенную пометку
        pending = None
        i += 1
    return blocks


def run_command_blocks(doc_path, text, failures):
    for lang, body, skip in extract_blocks(text):
        if lang not in ('bash', 'sh', 'shell'):
            continue
        if skip:
            continue
        if not body.strip():
            continue
        proc = subprocess.run(body, shell=True, cwd=REPO, executable='/bin/bash',
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            failures.append(
                '%s: команда завершилась кодом %d:\n%s\n--- вывод ---\n%s'
                % (doc_path, proc.returncode, body.strip(),
                   proc.stdout.decode('utf-8', 'replace')))


# ---- 2. перечень capability -----------------------------------------------

def check_capabilities(readme_text, failures):
    listed = set(CAP_ROW_RE.match(l).group(1) for l in readme_text.splitlines()
                if CAP_ROW_RE.match(l))
    if not os.path.isdir(SPECS_DIR):
        failures.append('openspec/specs/ не найдена — нечего сверять с README')
        return
    actual = set(n for n in os.listdir(SPECS_DIR)
                if os.path.isdir(os.path.join(SPECS_DIR, n)))
    missing_in_readme = actual - listed
    extra_in_readme = listed - actual
    if missing_in_readme:
        failures.append('README.md: в таблице capability не хватает: %s'
                        % ', '.join(sorted(missing_in_readme)))
    if extra_in_readme:
        failures.append('README.md: в таблице capability есть лишнее (нет в '
                        'openspec/specs/): %s' % ', '.join(sorted(extra_in_readme)))


# ---- 3. дерево файлов в «Структуре» ----------------------------------------

def extract_structure_block(readme_text):
    lines = readme_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == '## \u0421\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u0430':
            j = i + 1
            while j < len(lines) and lines[j].strip() != '```':
                j += 1
            j += 1
            body = []
            while j < len(lines) and lines[j].strip() != '```':
                body.append(lines[j])
                j += 1
            return body
    return None


def resolve_structure_paths(block_lines):
    """Возвращает (пути_для_проверки_существования, glob_шаблоны)."""
    paths = []
    globs = []
    root = ''
    dirs_by_depth = {}
    for line in block_lines:
        if not line.strip():
            continue
        m = TREE_MARKER_RE.match(line)
        prefix, marker, rest = m.group(1), m.group(2), m.group(3)
        rest = rest.strip()
        if not rest:
            continue
        token = rest.split()[0]
        if marker is None:
            root = token
            dirs_by_depth = {}
            if token.endswith('/'):
                continue  # директория верхнего уровня — проверится по вложенным
            paths.append(token)
            continue
        depth = len(prefix) // 4
        parents = ''.join(dirs_by_depth.get(d, '') for d in range(depth))
        full = root + parents + token
        if token.endswith('/'):
            dirs_by_depth[depth] = token
        if '*' in token:
            globs.append(full)
        else:
            paths.append(full)
    return paths, globs


def check_structure(readme_text, failures):
    block = extract_structure_block(readme_text)
    if block is None:
        failures.append('README.md: раздел «Структура» не найден или в нём нет блока кода')
        return
    paths, globs = resolve_structure_paths(block)
    missing = [p for p in paths if not os.path.exists(os.path.join(REPO, p))]
    if missing:
        failures.append('README.md: в разделе «Структура» перечислены отсутствующие '
                        'пути: %s' % ', '.join(sorted(missing)))
    for pattern in globs:
        if not glob.glob(os.path.join(REPO, pattern)):
            failures.append('README.md: в разделе «Структура» шаблон %r ничему не '
                            'соответствует' % pattern)


def main(argv=None):
    failures = []

    readme_path = os.path.join(REPO, 'README.md')
    if not os.path.isfile(readme_path):
        sys.stderr.write('README.md не найден\n')
        return 2
    readme_text = read(readme_path)

    for rel in DOC_FILES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            failures.append('%s: файл не найден' % rel)
            continue
        run_command_blocks(rel, read(path), failures)

    check_capabilities(readme_text, failures)
    check_structure(readme_text, failures)

    if failures:
        sys.stderr.write('Документация разошлась с репозиторием:\n\n')
        for f in failures:
            sys.stderr.write('- %s\n' % f)
        return 1

    print('README.md и docs/walkthrough.md согласованы с репозиторием.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
