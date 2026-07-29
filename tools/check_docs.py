#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка, что документация не разошлась с репозиторием.

Документация устаревает молча: команда меняет флаг, capability переименовывается,
файл в разделе «Структура» переезжает, презентация называет число, которого в коде
уже нет — и текст остаётся прежним, пока кто-то не заметит глазами. Эта проверка
ловит расхождения по нескольким направлениям:

  1. команды из блоков ``` ```bash ``` `` в README.md и docs/walkthrough.md
     действительно запускаются и завершаются нулевым кодом;
  2. перечень capability в таблице README.md совпадает с директориями
     openspec/specs/;
  3. дерево файлов в разделе «Структура» README.md совпадает с тем, что лежит в
     репозитории;
  4. презентация (`docs/presentation.html`) не обращается к внешним хостам, её
     числовые утверждения подтверждены исходниками, а упомянутые файлы существуют;
  5. сама проверка не выключилась: командных блоков README исполнено не меньше
     `MIN_README_COMMAND_BLOCKS`.

Блок кода пропускается, если непосредственно перед ним есть HTML-комментарий вида
`<!-- check-docs: skip (причина) -->` — команда требует внешнего окружения
(установка в домашнюю директорию, сеть, самоссылающийся прогон всего набора
проверок, время выполнения, зависящее от машины) и не может быть частью
воспроизводимой проверки на голом Python.

  python3 tools/check_docs.py

Только стандартная библиотека, Python 3.8+ — как и весь остальной проект. Дальнейшие
проверки документации добавляются сюда же отдельными функциями `check_*`, каждая —
список строк с описанием найденных расхождений.
"""

import glob
import html
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECS_DIR = os.path.join(REPO, 'openspec', 'specs')
DOC_FILES = ['README.md', os.path.join('docs', 'walkthrough.md')]
PRESENTATION = os.path.join(REPO, 'docs', 'presentation.html')

# Нижняя граница исполненных командных блоков README. Регрессия в извлечении
# блоков (например, в FENCE_RE) не роняет проверку — она просто перестаёт что-либо
# находить, и прогон остаётся зелёным, ничего не проверив. Число взято с запасом
# вниз от фактического (5 на момент введения), чтобы правка README не роняла
# прогон из-за одного убранного примера.
MIN_README_COMMAND_BLOCKS = 3

SKIP_MARKER = re.compile(r'<!--\s*check-docs:\s*skip')
FENCE_RE = re.compile(r'^```(\w*)\s*$')
CAP_ROW_RE = re.compile(r'^\|\s*`([a-z][a-z0-9-]*)`\s*\|')
TREE_MARKER_RE = re.compile(r'^((?:\u2502   |    )*)(\u251c\u2500\u2500 |\u2514\u2500\u2500 )?(.*)$')

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
CONTOUR_MD_ROW_RE = re.compile(r'^\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$')
CONTOUR_HTML_ROW_RE = re.compile(r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>',
                                 re.DOTALL)
CODE_RE = re.compile(r'`([^`]+)`|<code>(.*?)</code>')

# как проверить число из facts против исходника: source_key -> регулярка с
# одной захватывающей группой — числовым значением.
SOURCE_PATTERNS = {
    'parse_logs': r"'parse_logs':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'parse_logs_level': r"'parse_logs_level':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'parse_logs_window': r"'parse_logs_window':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'triage': r"'triage':\s*\{'kind':\s*'\w+',\s*'limit':\s*([\d.]+)",
    'MAX_SUMMARY_CHARS': r'MAX_SUMMARY_CHARS\s*=\s*(\d+)',
    'DEFAULT_WINDOW_MIN': r'DEFAULT_WINDOW_MIN\s*=\s*(\d+)',
    'confirmed_threshold': r'подтверждено данными\*\*[^\d]*([\d.]+)',
    'probable_threshold': r'вероятная причина\*\*[^\d]*([\d.]+)',
}


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
    """Исполняет командные блоки документа. Возвращает число исполненных.

    Число нужно вызывающему: «ни одной ошибки» и «ни одной команды» выглядят
    одинаково, и без счётчика сломанное извлечение блоков выдаёт себя за успех.
    """
    executed = 0
    for lang, body, skip in extract_blocks(text):
        if lang not in ('bash', 'sh', 'shell'):
            continue
        if skip:
            continue
        if not body.strip():
            continue
        executed += 1
        proc = subprocess.run(body, shell=True, cwd=REPO, executable='/bin/bash',
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            failures.append(
                '%s: команда завершилась кодом %d:\n%s\n--- вывод ---\n%s'
                % (doc_path, proc.returncode, body.strip(),
                   proc.stdout.decode('utf-8', 'replace')))
    return executed


def check_command_blocks_minimum(executed, failures, minimum=None):
    """Проверка проверяет сама себя: исполнено не меньше заданного числа блоков."""
    minimum = MIN_README_COMMAND_BLOCKS if minimum is None else minimum
    if executed < minimum:
        failures.append(
            'README.md: исполнено командных блоков %d, ожидалось не менее %d — '
            'похоже, извлечение блоков сломалось и проверка документации ничего '
            'не проверяет' % (executed, minimum))


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


# ---- 4. презентация ---------------------------------------------------------

def load_presentation():
    if not os.path.isfile(PRESENTATION):
        return None
    return read(PRESENTATION)


def _load_json_block(html, pattern, label):
    m = pattern.search(html)
    if not m:
        return None, ['docs/presentation.html: блок %s не найден' % label]
    try:
        return json.loads(m.group(1)), []
    except ValueError as exc:
        return None, ['docs/presentation.html: блок %s: не разобрался как JSON (%s)'
                      % (label, exc)]


def check_presentation_external_hosts(html, failures):
    """Презентация не должна обращаться ни к одному внешнему хосту."""
    for m in EXTERNAL_HOST_RE.finditer(html):
        failures.append('docs/presentation.html: обращение к внешнему хосту: %r'
                        % m.group(0)[:80])


def check_presentation_facts(html, failures):
    """Каждое числовое утверждение презентации сверяется с исходником."""
    facts, block_failures = _load_json_block(html, FACTS_BLOCK_RE, 'facts')
    failures.extend(block_failures)
    if facts is None:
        return
    for fact in facts:
        claim = fact.get('claim', '?')
        source_file = fact.get('source_file')
        source_key = fact.get('source_key')
        value = fact.get('value')
        src_path = os.path.join(REPO, source_file) if source_file else None
        if not source_file or not os.path.isfile(src_path):
            failures.append('facts: источник не найден для «%s»: %s' % (claim, source_file))
            continue
        source_text = read(src_path)

        if source_key == 'пять вызовов инструментов':
            if 'пять вызовов инструментов' not in source_text:
                failures.append('%s: фраза "пять вызовов инструментов" не найдена — '
                                'утверждение «%s» не подтверждено' % (source_file, claim))
            continue

        pattern = SOURCE_PATTERNS.get(source_key)
        if pattern is None:
            failures.append('%s: нет способа проверить source_key=%r (claim: %s)'
                            % (source_file, source_key, claim))
            continue
        m = re.search(pattern, source_text)
        if not m:
            failures.append('%s: значение для «%s» не найдено в источнике'
                            % (source_file, claim))
            continue
        found = float(m.group(1))
        if found != float(value):
            failures.append('%s: расхождение для «%s»: презентация называет %s, '
                            'источник — %s' % (source_file, claim, value, found))


def check_presentation_referenced_files(html, failures):
    """Все файлы репозитория, упомянутые в презентации, должны существовать."""
    paths, block_failures = _load_json_block(html, FILES_BLOCK_RE, 'referenced-files')
    failures.extend(block_failures)
    if paths is None:
        return
    for rel in paths:
        if not os.path.exists(os.path.join(REPO, rel)):
            failures.append('referenced-files: файл не найден: %s' % rel)


def check_presentation(failures):
    html = load_presentation()
    if html is None:
        failures.append('docs/presentation.html: файл не найден')
        return
    check_presentation_external_hosts(html, failures)
    check_presentation_facts(html, failures)
    check_presentation_referenced_files(html, failures)


# ---- 5. таблица трёх контуров --------------------------------------------

def _strip_markup(text):
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[*_`]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _contour_name(text):
    text = _strip_markup(text)
    text = re.sub(r'^\d+\.\s*', '', text)
    return text


def _scripts_from_cell(cell):
    scripts = []
    for match in CODE_RE.finditer(cell):
        scripts.append((match.group(1) or match.group(2) or '').strip())
    if scripts:
        return scripts
    return re.findall(r'\b[\w_]+\.py\b', cell)


def _extract_md_contours(text):
    rows = []
    in_table = False
    for line in text.splitlines():
        if line.strip().startswith('|') and 'Контур' in line and (
                'Скрипты' in line or 'Инструмент' in line):
            in_table = True
            continue
        if not in_table:
            continue
        if line.strip().startswith('|---'):
            continue
        if not line.strip().startswith('|'):
            if rows:
                break
            continue
        m = CONTOUR_MD_ROW_RE.match(line)
        if not m:
            continue
        name = _contour_name(m.group(1))
        if not name or name == 'Контур':
            continue
        rows.append((name, _strip_markup(m.group(2)), _scripts_from_cell(m.group(3))))
        if len(rows) == 3:
            break
    return rows


def _extract_html_contours(text):
    rows = []
    if 'Три контура' not in text:
        return rows
    for m in CONTOUR_HTML_ROW_RE.finditer(text):
        name = _contour_name(m.group(1))
        if name in ('База знаний', 'Логи', 'Код'):
            rows.append((name, _strip_markup(m.group(2)), _scripts_from_cell(m.group(3))))
            if len(rows) == 3:
                break
    return rows


def check_contour_table_sync(failures):
    canon_path = os.path.join(REPO, 'incident-detective', 'SKILL.md')
    canon = _extract_md_contours(read(canon_path))
    if len(canon) != 3:
        failures.append('incident-detective/SKILL.md: не разобрана каноническая таблица трёх контуров')
        return
    expected = {name: scripts for name, _question, scripts in canon}
    for rel, extractor in (
            ('README.md', _extract_md_contours),
            ('AI_CONTEXT.md', _extract_md_contours),
            (os.path.join('docs', 'presentation.html'), _extract_html_contours)):
        rows = extractor(read(os.path.join(REPO, rel)))
        if len(rows) != 3:
            failures.append('%s: не разобрана витринная таблица трёх контуров' % rel)
            continue
        actual = {name: scripts for name, _question, scripts in rows}
        for name in sorted(expected):
            if name not in actual:
                failures.append('%s: в таблице нет контура %s' % (rel, name))
                continue
            if actual[name] != expected[name]:
                failures.append('%s: контур %s: скрипты %s, канон %s'
                                % (rel, name, ', '.join(actual[name]) or '—',
                                   ', '.join(expected[name]) or '—'))


def _skill_section(text, title):
    """Текст раздела SKILL.md от его заголовка до следующего заголовка того же уровня."""
    start = text.find('\n## ' + title)
    if start < 0:
        return None
    rest = text[start + 1:]
    end = rest.find('\n## ', 1)
    return rest if end < 0 else rest[:end]


def check_closing_order(failures):
    """Порядок завершения разбора и повод включиться — инварианты формулировок.

    Прогон не проверяет поведение модели, но проверяет, что инструкции, которые
    это поведение задают, из канона не исчезли: шаг 7 перестал отсылать в шаг 9
    в обход шага 8 ровно потому, что такая отсылка там однажды была.
    """
    rel = os.path.join('incident-detective', 'SKILL.md')
    text = read(os.path.join(REPO, rel))

    trigger = _skill_section(text, 'Когда включаться')
    if trigger is None:
        failures.append('%s: не найден раздел «Когда включаться»' % rel)
    else:
        if 'прямая просьба' not in trigger.lower():
            failures.append('%s: в поводах включиться нет прямой просьбы использовать скилл' % rel)
        if 'parse_logs.py' not in trigger or 'triage.py' not in trigger:
            failures.append('%s: не сказано, что логи открываются только через скрипты' % rel)

    step7 = _skill_section(text, 'Шаг 7.')
    if step7 is None:
        failures.append('%s: не найден шаг 7' % rel)
    else:
        if 'Запись в базу' not in step7:
            failures.append('%s: в перечне пунктов вывода нет строки «Запись в базу»' % rel)
        if re.search(r'разбор идёт в шаг 9', step7):
            failures.append('%s: шаг 7 отсылает в шаг 9 в обход шага 8' % rel)

    step9 = _skill_section(text, 'Шаг 9.')
    if step9 is None:
        failures.append('%s: не найден шаг 9' % rel)
    elif 'шага 8' not in step9 and 'шаг 8' not in step9:
        failures.append('%s: шаг 9 не называет условие входа — пройденный шаг 8' % rel)

    walk = read(os.path.join(REPO, 'docs', 'walkthrough.md'))
    kb_at = walk.find('\n## Запись в базу знаний')
    next_at = walk.find('\n## Предложение следующего шага')
    if kb_at < 0 or next_at < 0:
        failures.append('docs/walkthrough.md: не найдены разделы записи в базу и следующего шага')
    elif kb_at > next_at:
        failures.append('docs/walkthrough.md: следующий шаг показан раньше записи в базу')


def main(argv=None):
    failures = []

    readme_path = os.path.join(REPO, 'README.md')
    if not os.path.isfile(readme_path):
        sys.stderr.write('README.md не найден\n')
        return 2
    readme_text = read(readme_path)

    executed = {}
    for rel in DOC_FILES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            failures.append('%s: файл не найден' % rel)
            continue
        executed[rel] = run_command_blocks(rel, read(path), failures)

    check_command_blocks_minimum(executed.get('README.md', 0), failures)
    check_capabilities(readme_text, failures)
    check_structure(readme_text, failures)
    check_presentation(failures)
    check_contour_table_sync(failures)
    check_closing_order(failures)

    if failures:
        sys.stderr.write('Документация разошлась с репозиторием:\n\n')
        for f in failures:
            sys.stderr.write('- %s\n' % f)
        return 1

    print('README.md, docs/walkthrough.md и docs/presentation.html согласованы с репозиторием.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
