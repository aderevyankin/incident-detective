#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Третий контур: связывает ошибки из логов с кодом текущего проекта.

Что делает:
  1. Вытаскивает из логов ссылки на код — фреймы стектрейсов (Java, Python,
     JS/TS, Go, C#, PHP, Ruby), имена классов и функций.
  2. Ищет соответствующие файлы в репозитории и показывает контекст строк.
  3. Ищет по литеральным кускам сообщений — место, где текст ошибки порождается.
  4. Сопоставляет время первой ошибки с git-историей: что закоммитили
     непосредственно перед инцидентом.

Ничего не правит — только собирает материал для выводов. Stdlib, Python 3.8+.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

# Без f-строк намеренно: на старом интерпретаторе должно печататься сообщение, а не SyntaxError.
if sys.version_info < (3, 8):
    sys.stderr.write('incident-triage: нужен Python 3.8 или новее, запущен %s (%s)\n'
                     % (sys.version.split()[0], sys.executable))
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb_common import load_parsed  # noqa: E402

SKIP_DIRS = {'.git', 'node_modules', 'venv', '.venv', 'env', '__pycache__',
             'dist', 'build', 'target', '.idea', '.gradle', 'vendor',
             'coverage', '.next', '.tox', '.mypy_cache', '.pytest_cache'}
CODE_EXTS = ('.py', '.java', '.kt', '.scala', '.js', '.jsx', '.ts', '.tsx',
             '.go', '.rb', '.php', '.cs', '.rs', '.c', '.cc', '.cpp', '.h',
             '.hpp', '.sql', '.yaml', '.yml', '.json', '.xml', '.properties',
             '.conf', '.toml', '.ini', '.sh', '.tf')
MAX_FILE_BYTES = 2 * 1024 * 1024

# ---- фреймы стектрейсов -------------------------------------------------

FRAME_PATTERNS = [
    # Java/Kotlin: at com.acme.Svc.pay(Svc.java:142)
    (re.compile(r'at\s+([\w.$]+)\.([\w$<>]+)\(([\w$]+\.(?:java|kt|scala)):(\d+)\)'),
     lambda m: (m.group(3), int(m.group(4)), '%s.%s' % (m.group(1), m.group(2)))),
    # Python: File "/app/svc/pay.py", line 42, in charge
    (re.compile(r'File "([^"]+)", line (\d+)(?:, in (\w+))?'),
     lambda m: (m.group(1), int(m.group(2)), m.group(3) or '')),
    # JS/TS: at charge (/app/src/pay.ts:10:5)  |  at /app/src/pay.js:10:5
    (re.compile(r'at\s+(?:([\w.$<>]+)\s+)?\(?((?:/|\.\/|[A-Za-z]:\\)[^\s():]+'
                r'\.(?:js|jsx|ts|tsx|mjs|cjs)):(\d+)'),
     lambda m: (m.group(2), int(m.group(3)), m.group(1) or '')),
    # Go: /app/internal/pay/handler.go:88 +0x1f
    (re.compile(r'((?:/|\./)[^\s:]+\.go):(\d+)'),
     lambda m: (m.group(1), int(m.group(2)), '')),
    # C#: at Acme.Pay.Charge() in C:\src\Pay.cs:line 55
    (re.compile(r'in\s+(\S+\.cs):line\s+(\d+)'),
     lambda m: (m.group(1), int(m.group(2)), '')),
    # PHP: /var/www/src/Pay.php:88  |  Ruby: /app/lib/pay.rb:42:in `charge'
    (re.compile(r'((?:/|\./)[^\s:]+\.(?:php|rb)):(\d+)'),
     lambda m: (m.group(1), int(m.group(2)), '')),
    # обобщённое: path/to/file.ext:123
    (re.compile(r'\b([\w./\-]+\.(?:py|java|kt|go|rb|php|cs|rs|ts|js)):(\d+)\b'),
     lambda m: (m.group(1), int(m.group(2)), '')),
]

EXC_RE = re.compile(
    r'\b((?:[a-z][\w]*\.)*[A-Z][A-Za-z0-9_]*'
    r'(?:Exception|Error|Throwable|Timeout|Failure|Fault))\b')

# ---- извлечение из шаблона литералов ------------------------------------

PLACEHOLDER_RE = re.compile(r'<\w+>')
NOISE_WORDS = {'error', 'failed', 'failure', 'exception', 'warning', 'timeout',
               'unable', 'cannot', 'could not', 'invalid', 'request', 'response'}


def literal_phrases(template, min_len=12):
    """Куски шаблона без плейсхолдеров — по ним ищем место в коде."""
    text = template[5:] if template.startswith('tmpl:') else template
    out = []
    for chunk in PLACEHOLDER_RE.split(text):
        chunk = chunk.strip(' \t|,;:.-()[]{}"\'')
        chunk = re.sub(r'\s+', ' ', chunk)
        if len(chunk) < min_len:
            continue
        if chunk.lower() in NOISE_WORDS:
            continue
        if not re.search(r'[a-zA-Zа-яА-ЯёЁ]{4}', chunk):
            continue
        out.append(chunk)
    out.sort(key=len, reverse=True)
    return out[:3]


# ---- обход репозитория --------------------------------------------------


def walk_code(repo):
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            if not name.endswith(CODE_EXTS):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def index_by_basename(repo):
    idx = {}
    for path in walk_code(repo):
        idx.setdefault(os.path.basename(path), []).append(path)
    return idx


def read_lines(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            return fh.read().split('\n')
    except OSError:
        return []


def grep_repo(repo, phrases, limit=12):
    """Ищет литеральные фразы по файлам проекта."""
    if not phrases:
        return []
    lowered = [(p, p.lower()) for p in phrases]
    hits = []
    for path in walk_code(repo):
        lines = read_lines(path)
        if not lines:
            continue
        for lineno, line in enumerate(lines, 1):
            low = line.lower()
            for original, needle in lowered:
                if needle in low:
                    hits.append({'path': path, 'line': lineno,
                                 'text': line.strip()[:200], 'phrase': original})
                    break
            if len(hits) >= limit:
                return hits
    return hits


# ---- git ----------------------------------------------------------------


def git(repo, *args):
    try:
        res = subprocess.run(['git', '-C', repo] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.decode('utf-8', 'replace').rstrip('\n')


def is_git_repo(repo):
    return git(repo, 'rev-parse', '--is-inside-work-tree') == 'true'


def commits_in_window(repo, start, end, limit=15):
    if not start:
        return []
    since = (start - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    until = (end or start).strftime('%Y-%m-%d %H:%M:%S')
    out = git(repo, 'log', '--since=%s' % since, '--until=%s' % until,
              '--date=format:%Y-%m-%d %H:%M', '-n', str(limit),
              '--pretty=format:%h|%ad|%an|%s')
    if not out:
        return []
    rows = []
    for line in out.split('\n'):
        parts = line.split('|', 3)
        if len(parts) == 4:
            rows.append({'hash': parts[0], 'date': parts[1],
                         'author': parts[2], 'subject': parts[3]})
    return rows


def file_history(repo, path, limit=3):
    out = git(repo, 'log', '-n', str(limit), '--date=short',
              '--pretty=format:%h|%ad|%an|%s', '--', path)
    if not out:
        return []
    rows = []
    for line in out.split('\n'):
        parts = line.split('|', 3)
        if len(parts) == 4:
            rows.append({'hash': parts[0], 'date': parts[1],
                         'author': parts[2], 'subject': parts[3]})
    return rows


def blame_line(repo, path, lineno):
    out = git(repo, 'blame', '-L', '%d,%d' % (lineno, lineno), '--date=short',
              '-p', '--', path)
    if not out:
        return None
    info = {}
    for line in out.split('\n'):
        if line.startswith('author '):
            info['author'] = line[7:]
        elif line.startswith('author-time '):
            try:
                info['date'] = datetime.fromtimestamp(int(line[12:])).strftime('%Y-%m-%d')
            except ValueError:
                pass
        elif line.startswith('summary '):
            info['subject'] = line[8:]
    first = out.split('\n')[0].split(' ')[0]
    if first and re.match(r'^[0-9a-f]{7,40}$', first):
        info['hash'] = first[:8]
    return info or None


# ---- сбор входных данных -------------------------------------------------


def collect_input(args):
    """Возвращает (тексты для разбора, сигнатуры, first_ts, last_ts)."""
    texts = []
    signatures = []
    first_ts = last_ts = None
    if args.from_parsed:
        parsed = load_parsed(args.from_parsed)
        for grp in parsed.get('groups', []):
            texts.append(grp.get('sample', ''))
            signatures.append('tmpl:' + grp.get('template', ''))
        for sig in parsed.get('signatures', []):
            signatures.append(sig.get('value', ''))
        for exc in parsed.get('stats', {}).get('exceptions', {}):
            signatures.append(exc)
        stats = parsed.get('stats', {})
        for key, dest in (('first_ts', 'first'), ('last_ts', 'last')):
            val = stats.get(key)
            if val:
                try:
                    dt = datetime.strptime(val, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    dt = None
                if dest == 'first':
                    first_ts = dt
                else:
                    last_ts = dt
        # окно ищем по первой ОШИБКЕ, а не по первой записи в логе:
        # иначе в git-окно попадает всё, что было до инцидента
        error_starts = []
        for grp in parsed.get('groups', []):
            if grp.get('first') and grp.get('level') in ('WARN', 'ERROR', 'FATAL'):
                try:
                    error_starts.append(datetime.strptime(grp['first'], '%Y-%m-%d %H:%M:%S'))
                except ValueError:
                    pass
        if error_starts:
            first_ts = min(error_starts)
    if args.log:
        with open(args.log, 'r', encoding='utf-8', errors='replace') as fh:
            texts.append(fh.read()[:200000])
    if args.text:
        texts.append(args.text)
    if not sys.stdin.isatty() and not args.from_parsed and not args.log and not args.text:
        texts.append(sys.stdin.read()[:200000])
    signatures.extend(args.signature or [])
    return texts, [s for s in signatures if s], first_ts, last_ts


def extract_frames(texts, limit=25):
    frames = OrderedDict()
    for text in texts:
        for rx, build in FRAME_PATTERNS:
            for m in rx.finditer(text):
                try:
                    path, lineno, symbol = build(m)
                except (ValueError, IndexError):
                    continue
                key = (os.path.basename(path), lineno)
                if key not in frames:
                    frames[key] = {'raw_path': path, 'basename': os.path.basename(path),
                                   'line': lineno, 'symbol': symbol}
                if len(frames) >= limit:
                    return list(frames.values())
    return list(frames.values())


def resolve_frames(frames, index, repo):
    """Сопоставляет фреймы стектрейсов с файлами репозитория."""
    resolved = []
    for frame in frames:
        candidates = index.get(frame['basename'], [])
        if not candidates and frame['basename'].endswith(('.java', '.kt')):
            stem = os.path.splitext(frame['basename'])[0]
            for name, paths in index.items():
                if os.path.splitext(name)[0] == stem:
                    candidates = paths
                    break
        if len(candidates) > 1:
            # приоритет по совпадению хвоста исходного пути
            raw = frame['raw_path'].replace('\\', '/')
            candidates = sorted(
                candidates,
                key=lambda p: -len(os.path.commonprefix([p.replace('\\', '/')[::-1],
                                                         raw[::-1]])))
        item = dict(frame)
        item['matches'] = candidates[:3]
        if candidates:
            item['context'] = context_lines(candidates[0], frame['line'])
            item['blame'] = blame_line(repo, candidates[0], frame['line'])
            item['history'] = file_history(repo, candidates[0])
        resolved.append(item)
    return resolved


def context_lines(path, lineno, radius=3):
    lines = read_lines(path)
    if not lines or lineno > len(lines):
        return []
    start = max(1, lineno - radius)
    end = min(len(lines), lineno + radius)
    return [{'n': i, 'text': lines[i - 1][:200], 'target': i == lineno}
            for i in range(start, end + 1)]


# ---- вывод ---------------------------------------------------------------


def render_md(data, out):
    w = out.write
    w('# Связь ошибок с кодом\n\n')
    w('- Репозиторий: `%s`%s\n' % (data['repo'], '' if data['git'] else ' (не git)'))
    if data['first_ts']:
        w('- Первая ошибка в логах: %s\n' % data['first_ts'])
    w('\n')

    frames = data['frames']
    if frames:
        w('## Фреймы стектрейсов\n\n')
        for frame in frames:
            head = '`%s:%d`' % (frame['basename'], frame['line'])
            if frame.get('symbol'):
                head += ' — `%s`' % frame['symbol']
            w('**%s**\n' % head)
            if not frame['matches']:
                w('  файл в проекте не найден — вероятно, код внешней библиотеки '
                  'или другого сервиса\n\n')
                continue
            w('  → `%s`\n' % frame['matches'][0])
            if len(frame['matches']) > 1:
                w('  другие кандидаты: %s\n' % ', '.join('`%s`' % p for p in frame['matches'][1:]))
            if not frame.get('context'):
                total = len(read_lines(frame['matches'][0]))
                if total and frame['line'] > total:
                    w('  ⚠ строки %d в файле нет (всего %d) — код в репозитории не той версии, '
                      'что на стенде. Сверь ветку/тег с задеплоенной сборкой.\n\n'
                      % (frame['line'], total))
                    continue
            for line in frame.get('context') or []:
                mark = '>' if line['target'] else ' '
                w('    %s %4d | %s\n' % (mark, line['n'], line['text']))
            blame = frame.get('blame')
            if blame:
                w('  blame: %s · %s · %s · %s\n' % (
                    blame.get('hash', '?'), blame.get('date', '?'),
                    blame.get('author', '?'), blame.get('subject', '')[:70]))
            w('\n')
    else:
        w('## Фреймы стектрейсов\n\nВ логах нет ссылок на файлы кода — '
          'ищем по тексту сообщений.\n\n')

    hits = data['grep']
    if hits:
        w('## Где порождается текст ошибки\n\n')
        for hit in hits:
            w('- `%s:%d` — %s\n' % (os.path.relpath(hit['path'], data['repo']),
                                    hit['line'], hit['text']))
            w('  (по фрагменту: «%s»)\n' % hit['phrase'][:60])
        w('\n')
    elif data['phrases']:
        w('## Где порождается текст ошибки\n\n'
          'Совпадений в проекте нет. Искали: %s.\n'
          'Скорее всего, сообщение приходит из библиотеки, соседнего сервиса '
          'или инфраструктуры.\n\n' % ', '.join('«%s»' % p for p in data['phrases'][:4]))

    if data['exceptions']:
        w('## Классы исключений в коде\n\n')
        for exc, places in data['exceptions'].items():
            if places:
                w('- `%s`: %s\n' % (exc, ', '.join(
                    '%s:%d' % (os.path.relpath(p['path'], data['repo']), p['line'])
                    for p in places[:3])))
            else:
                w('- `%s`: в проекте не встречается (внешний код)\n' % exc)
        w('\n')

    commits = data['commits']
    if commits:
        w('## Коммиты в окне инцидента (сутки до первой ошибки)\n\n')
        for c in commits:
            w('- `%s` %s · %s · %s\n' % (c['hash'], c['date'], c['author'], c['subject'][:80]))
        w('\nСовпадение по времени — повод посмотреть, а не доказательство. '
          'Сверь, трогает ли коммит найденные выше файлы.\n\n')
    elif data['git'] and data['first_ts']:
        w('## Коммиты в окне инцидента\n\nЗа сутки до первой ошибки коммитов нет — '
          'причина, скорее всего, не в свежем коде (конфиг, данные, соседний сервис, '
          'нагрузка).\n\n')

    w('## Что дальше\n\n')
    if frames and any(f['matches'] for f in frames):
        w('- Проверь верхний фрейм — он ближе всего к месту падения.\n')
    if commits:
        w('- Сопоставь список коммитов с затронутыми файлами.\n')
    w('- Правки предлагай только после того, как причина подтверждена логами.\n')
    w('- Разобрался — запиши в базу знаний с `--file` и `--commit`, '
      'чтобы в следующий раз выйти на это место сразу.\n')


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Связывает ошибки из логов с кодом проекта и git-историей.')
    ap.add_argument('--from-parsed', help='JSON-вывод parse_logs.py')
    ap.add_argument('--log', help='файл лога/стектрейса напрямую')
    ap.add_argument('--text', help='текст ошибки или стектрейс строкой')
    ap.add_argument('--signature', action='append', default=[], help='сигнатура')
    ap.add_argument('--repo', default='.', help='корень проекта (по умолчанию текущая директория)')
    ap.add_argument('--since', help='начало окна для git log (YYYY-MM-DD HH:MM)')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        raise SystemExit('Нет такой директории: %s' % repo)

    texts, signatures, first_ts, last_ts = collect_input(args)
    if not texts and not signatures:
        raise SystemExit('Нечего анализировать: укажи --from-parsed, --log, --text '
                         'или подай стектрейс в stdin')

    if args.since:
        try:
            first_ts = datetime.strptime(args.since, '%Y-%m-%d %H:%M')
        except ValueError:
            first_ts = datetime.strptime(args.since, '%Y-%m-%d')

    blob = '\n'.join(texts)
    index = index_by_basename(repo)

    frames = extract_frames(texts + signatures)
    frames = resolve_frames(frames, index, repo)

    phrases = []
    for sig in signatures:
        phrases.extend(literal_phrases(sig))
    if not phrases:
        for line in blob.split('\n')[:50]:
            phrases.extend(literal_phrases(line))
    seen = set()
    phrases = [p for p in phrases if not (p.lower() in seen or seen.add(p.lower()))][:6]
    hits = grep_repo(repo, phrases)

    exceptions = OrderedDict()
    for exc in list(OrderedDict.fromkeys(EXC_RE.findall(blob)))[:6]:
        short = exc.split('.')[-1]
        found = grep_repo(repo, [short], limit=3)
        exceptions[exc] = found

    git_ok = is_git_repo(repo)
    commits = commits_in_window(repo, first_ts, last_ts) if git_ok else []

    data = {
        'repo': repo,
        'git': git_ok,
        'first_ts': first_ts.strftime('%Y-%m-%d %H:%M:%S') if first_ts else None,
        'frames': frames,
        'grep': hits,
        'phrases': phrases,
        'exceptions': exceptions,
        'commits': commits,
    }

    if args.format == 'json':
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write('\n')
    else:
        render_md(data, sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
