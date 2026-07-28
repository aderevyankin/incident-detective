#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор инцидента одной командой: логи → база знаний → код → уверенность.

Скрипты контуров работают секунды, а разбор занимает минуты — время уходит не
внутри скриптов, а между ними: каждый отдельный запуск это ещё один проход
модели. Поиск по базе, разбор кода и оценка уверенности друг от друга не
зависят и ждать очереди не должны.

Оркестратор проходит цепочку в одном процессе и отдаёт одну сводку. Логику он
не дублирует — вызывает те же модули, что работают и по отдельности:
`parse_logs`, `kb_search`, `code_hints`, `confidence`. Правка в любом из них
меняет и поведение оркестратора; расходиться им не с чего.

  python3 triage.py app.log --repo . --stand stage
  python3 triage.py 'logs/**/*.gz' --level ERROR --since "2026-07-28 12:00"
  python3 triage.py app.log --out /tmp/triage      # куда положить JSON этапов

Отдельные скрипты никуда не делись и остаются рабочими: ручной разбор, отладка
одного этапа, нестандартные флаги — всё по-прежнему через них.
"""

import argparse
import io
import os
import json
import sys

# Без f-строк намеренно: на старом интерпретаторе должно печататься сообщение, а не SyntaxError.
if sys.version_info < (3, 8):
    sys.stderr.write('incident-triage: нужен Python 3.8 или новее, запущен %s (%s)\n'
                     % (sys.version.split()[0], sys.executable))
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import code_hints  # noqa: E402
import confidence  # noqa: E402
import kb_search  # noqa: E402
import parse_logs as pl  # noqa: E402
from kb_common import (  # noqa: E402
    MAX_SUMMARY_CHARS, kb_dir, load_incidents_fast, signatures_from_parsed, tokenize,
)


class Stage(object):
    """Этап цепочки: прошёл, не прошёл или пропущен — и почему."""

    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.done = False
        self.reason = None
        self.data = None
        self.path = None

    def skip(self, reason):
        self.reason = reason
        return self

    def finish(self, data, path=None):
        self.done = True
        self.data = data
        self.path = path
        return self


# --- этапы ----------------------------------------------------------------


def stage_logs(args, out_dir):
    """Разбор логов: сводка, шаблоны, сигнатуры."""
    stage = Stage('logs', 'Логи')
    sources = pl.expand_sources(args.sources)
    if not sources:
        return stage.skip('источники не найдены: %s' % ', '.join(args.sources))

    ns = argparse.Namespace(
        level=args.level, since=args.since, until=args.until, grep=args.grep,
        top=args.top, context=None, trace=None, histogram=False,
        show_info=False, format='json', max_chars=0, max_lines=args.max_lines,
        encoding=args.encoding)
    prefilter = pl.PreFilter(level=ns.level, since=ns.since, until=ns.until,
                             grep=ns.grep)
    records = pl.read_records(sources, ns.encoding, ns.max_lines, prefilter)
    result = pl.analyze(records, ns)
    result['prefiltered'] = prefilter.skipped

    buf = io.StringIO()
    pl.render_json(result, ns, buf)
    parsed = json.loads(buf.getvalue())
    path = write_json(out_dir, 'parsed.json', parsed)
    return stage.finish({'result': result, 'parsed': parsed, 'args': ns}, path)


def stage_kb(args, parsed, out_dir):
    """Поиск похожих случаев по сигнатурам из логов."""
    stage = Stage('kb', 'База знаний')
    directory = kb_dir(args.kb)
    incidents, warning = load_incidents_fast(directory)
    if warning:
        sys.stderr.write('warning: %s\n' % warning)
    if not incidents:
        return stage.skip('база знаний пуста или не найдена: %s' % directory)

    signatures = list(signatures_from_parsed(parsed))
    query_parts = []
    for exc in list(parsed.get('stats', {}).get('exceptions', {}))[:5]:
        query_parts.append(exc)
    for svc in list(parsed.get('stats', {}).get('services', {}))[:3]:
        query_parts.append(svc)
    if args.query:
        query_parts.extend(args.query)
    if not signatures and not query_parts:
        return stage.skip('из логов не извлеклось ни сигнатур, ни имён сервисов')

    filters = {'stand': [args.stand] if args.stand else None,
               'service': [args.service] if args.service else None,
               'tag': None}
    tokens = tokenize(' '.join(query_parts))
    hits = []
    for inc in incidents:
        scored = kb_search.score_incident(inc, tokens, signatures, filters)
        if scored and scored['score'] >= args.min_score:
            hits.append(scored)
    hits.sort(key=lambda h: (-h['score'], str(h['inc']['meta'].get('id') or '')))
    hits = hits[:args.top_kb]

    buf = io.StringIO()
    kb_search.render_json(hits, buf)
    payload = json.loads(buf.getvalue())
    path = write_json(out_dir, 'kb.json', payload)
    return stage.finish({'hits': hits, 'payload': payload,
                         'total': len(incidents)}, path)


def stage_code(args, parsed_path, out_dir):
    """Связь ошибок с кодом проекта и git-историей."""
    stage = Stage('code', 'Код')
    repo = os.path.abspath(args.repo) if args.repo else None
    if not repo or not os.path.isdir(repo):
        return stage.skip('репозиторий не указан или не найден — контур кода пропущен')

    ns = argparse.Namespace(from_parsed=parsed_path, log=None, text=None,
                            signature=[], repo=repo, since=None, format='json')
    try:
        data = code_hints.build(ns)
    except SystemExit as exc:
        return stage.skip(str(exc) or 'нечего сопоставлять с кодом')
    path = write_json(out_dir, 'code.json', data)
    return stage.finish(data, path)


def stage_confidence(args, stages, out_dir):
    """Сколько контуров сошлось и что из этого следует."""
    stage = Stage('confidence', 'Уверенность')
    logs = stages['logs'].data['parsed'] if stages['logs'].done else None
    kb_payload = stages['kb'].data['payload'] if stages['kb'].done else None
    code = stages['code'].data if stages['code'].done else None
    if logs is None and kb_payload is None and code is None:
        return stage.skip('нечего оценивать: ни один контур не дал данных')

    scores = {
        'logs': confidence.score_logs(logs),
        'kb': confidence.score_kb(kb_payload, args.stand, args.service),
        'code': confidence.score_code(code),
        'trace': confidence.score_trace(None),
    }
    total, rows = confidence.combine(scores)
    payload = {'confidence': round(total, 3), 'verdict': confidence.verdict(total),
               'claim': args.claim, 'contours': rows}
    path = write_json(out_dir, 'confidence.json', payload)
    return stage.finish({'total': total, 'rows': rows, 'payload': payload}, path)


# --- вывод ----------------------------------------------------------------


def write_json(out_dir, name, payload):
    path = os.path.join(out_dir, name)
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    except OSError as exc:
        sys.stderr.write('warning: не сохранил %s: %s\n' % (path, exc))
        return None
    return path


def render(stages, args, out_dir, out):
    w = out.write
    w('# Разбор инцидента\n\n')

    passed = [s.title for s in stages.values() if s.done]
    skipped = [s for s in stages.values() if not s.done]
    w('- Пройдено контуров: %s\n' % (', '.join(passed) if passed else 'ни одного'))
    for stage in skipped:
        w('- %s — не пройден: %s\n' % (stage.title, stage.reason))
    w('\n')

    if stages['logs'].done:
        data = stages['logs'].data
        sub = io.StringIO()
        ns = data['args']
        ns.format = 'md'
        ns.max_chars = 0
        pl.render_md(data['result'], ns, sub)
        w(indent_section(sub.getvalue()))
        w('\n')

    if stages['kb'].done:
        data = stages['kb'].data
        sub = io.StringIO()
        kb_search.render_md(data['hits'], sub, 'по сигнатурам из логов',
                            data['total'], budget=0)
        w(indent_section(sub.getvalue()))
        w('\n')

    if stages['code'].done:
        sub = io.StringIO()
        code_hints.render_md(stages['code'].data, sub)
        w(indent_section(sub.getvalue()))
        w('\n')

    if stages['confidence'].done:
        data = stages['confidence'].data
        sub = io.StringIO()
        confidence.render_md(data['total'], data['rows'], args.claim, sub)
        w(indent_section(sub.getvalue()))
        w('\n')

    w('## Машинные результаты\n\n')
    for stage in stages.values():
        if stage.path:
            w('- %s: `%s`\n' % (stage.title, stage.path))
    w('\nОни принимаются на вход остальными скриптами скилла: `timeline.py`, '
      '`trace.py`, `kb_add.py`.\n')


def indent_section(text):
    """Сводки скриптов начинаются с `# Заголовок` — внутри общего отчёта они
    должны стать разделами, иначе получается четыре документа подряд."""
    out = []
    for line in text.split('\n'):
        if line.startswith('# '):
            out.append('#' + line)
        elif line.startswith('## '):
            out.append('#' + line)
        else:
            out.append(line)
    return '\n'.join(out)


def fit_output(text, budget):
    """Общая сводка тоже едет в контекст: если она вылезла за бюджет, режем
    хвост по разделам, а не по символам, и говорим, что урезали."""
    if budget <= 0 or len(text) <= budget:
        return text
    parts = text.split('\n## ')
    kept = [parts[0]]
    used = len(parts[0])
    dropped = []
    for part in parts[1:]:
        chunk = '\n## ' + part
        if used + len(chunk) > budget and len(kept) > 1:
            dropped.append(part.split('\n', 1)[0].strip())
            continue
        kept.append(part)
        used += len(chunk)
    result = '\n## '.join(kept)
    if dropped:
        result += ('\n_Не показаны разделы: %s — сводка ограничена по объёму. '
                   'Они есть в машинных результатах._\n' % ', '.join(dropped))
    return result


# --- точка входа ----------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Разбор инцидента одной командой: логи, база знаний, код, уверенность.')
    ap.add_argument('sources', nargs='+',
                    help='файлы логов, glob-маски, директории, архивы или "-" для stdin')
    ap.add_argument('--repo', default='.', help='корень проекта для контура кода (.)')
    ap.add_argument('--kb', help='директория базы знаний')
    ap.add_argument('--stand', help='стенд инцидента')
    ap.add_argument('--service', help='сервис инцидента')
    ap.add_argument('--query', action='append',
                    help='слова симптома для поиска по базе (можно повторять)')
    ap.add_argument('--claim', help='формулировка вывода, которую оцениваем')
    ap.add_argument('--level', choices=pl.LEVELS, help='минимальный уровень')
    ap.add_argument('--since', help='начало окна, напр. "2026-07-28 12:00"')
    ap.add_argument('--until', help='конец окна')
    ap.add_argument('--grep', help='regex-фильтр по сырому тексту записи')
    ap.add_argument('--top', type=int, default=10, help='шаблонов в сводке логов (10)')
    ap.add_argument('--top-kb', type=int, default=5, help='записей базы знаний (5)')
    ap.add_argument('--min-score', type=float, default=1.0)
    ap.add_argument('--out', help='директория для JSON этапов (по умолчанию временная)')
    ap.add_argument('--max-chars', type=int, default=MAX_SUMMARY_CHARS,
                    help='предельный объём сводки в символах (%d), 0 — без предела'
                         % MAX_SUMMARY_CHARS)
    ap.add_argument('--max-lines', type=int, default=2000000)
    ap.add_argument('--encoding', default='utf-8')
    args = ap.parse_args(argv)

    args.since = pl.parse_time_arg(args.since) if args.since else None
    args.until = pl.parse_time_arg(args.until) if args.until else None

    out_dir = args.out or os.path.join(
        os.environ.get('TMPDIR') or '/tmp', 'incident-triage')
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise SystemExit('Не создал директорию для результатов %s: %s' % (out_dir, exc))

    stages = {}
    stages['logs'] = stage_logs(args, out_dir)
    parsed = stages['logs'].data['parsed'] if stages['logs'].done else {}
    parsed_path = stages['logs'].path

    if stages['logs'].done:
        stages['kb'] = stage_kb(args, parsed, out_dir)
        stages['code'] = (stage_code(args, parsed_path, out_dir) if parsed_path
                          else Stage('code', 'Код').skip('разбор логов не сохранён'))
    else:
        stages['kb'] = Stage('kb', 'База знаний').skip('нет разбора логов — искать нечего')
        stages['code'] = Stage('code', 'Код').skip('нет разбора логов — искать нечего')
    stages['confidence'] = stage_confidence(args, stages, out_dir)

    buf = io.StringIO()
    render(stages, args, out_dir, buf)
    sys.stdout.write(fit_output(buf.getvalue(), args.max_chars))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        sys.exit(130)
