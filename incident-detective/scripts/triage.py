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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

import code_hints  # noqa: E402
import confidence  # noqa: E402
import kb_search  # noqa: E402
import parse_logs as pl  # noqa: E402
from kb_common import (  # noqa: E402
    DEFAULT_MAX_LINES, ENV_SESSION, ENV_TRACE, KIND_INCIDENT, LEVELS, MAX_SUMMARY_CHARS,
    REPORT_SCHEMA, VERDICT_INSUFFICIENT, dump_json, git_toplevel, is_auto, kb_dir,
    kind_of, load_incidents_fast, mode, now, report_path, run_script, scrub_text,
    tokenize, write_report,
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
    entries, warning = load_incidents_fast(directory)
    if warning:
        sys.stderr.write('warning: %s\n' % warning)
    # в базе лежат записи двух видов; контур сравнивает разборы, а записи карты
    # источников не разбор — они не ранжируются и не идут в счёт
    incidents = [e for e in entries if kind_of(e['meta']) == KIND_INCIDENT]
    if not incidents:
        if entries:
            return stage.skip('разборов инцидентов в базе нет (записей карты '
                              'источников: %d): %s' % (len(entries), directory))
        return stage.skip('база знаний пуста или не найдена: %s' % directory)

    # запрос и ранжирование — общие с kb_search.py: логику не дублируем, а
    # вызываем те же функции, включая фильтр служебного сервиса `access`
    signatures, query_parts = kb_search.query_from_parsed(parsed)
    if args.query:
        query_parts.extend(args.query)
    if not signatures and not query_parts:
        return stage.skip('из логов не извлеклось ни сигнатур, ни имён сервисов')

    filters = {'stand': [args.stand] if args.stand else None,
               'service': [args.service] if args.service else None,
               'tag': None}
    tokens = tokenize(' '.join(query_parts))
    hits = kb_search.rank(incidents, tokens, signatures, filters, args.min_score)
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

    # 'trace' здесь не считается: оркестратор не строит цепочку запроса — этот
    # контур confidence.combine учитывает как непройденный, ровно как и при
    # вызове score_trace(None), но без бессмысленного вызова с заведомо пустым
    # входом
    scores = {
        'logs': confidence.score_logs(logs),
        'kb': confidence.score_kb(kb_payload, args.stand, args.service),
        'code': confidence.score_code(code),
    }
    total, rows = confidence.combine(scores)
    payload = {'confidence': round(total, 3), 'verdict': confidence.verdict(total),
               'claim': args.claim, 'contours': rows}
    path = write_json(out_dir, 'confidence.json', payload)
    return stage.finish({'total': total, 'rows': rows, 'payload': payload}, path)


# --- машинный отчёт -------------------------------------------------------
#
# Единственный машинный выход автономного режима. Значения в него кладут
# скрипты: числа — `confidence.py`, сигнатуры — `parse_logs.py`, места в коде —
# `code_hints.py`. Модель добавляет к отчёту формулировки в ответе, а не
# значения в полях: контракт, который держится на форматировании модели,
# ломается у потребителя и молча.

# Сколько доказательств каждого вида кладём в отчёт. Отчёт читает обвязка и
# дежурный, а не человек с временем: десять шаблонов подряд — это уже дамп.
MAX_EVIDENCE = 5


def clean(text):
    """Очистка теми же правилами, что запись базы знаний: отчёт уезжает в чат
    обвязки и в тикеты, и сырым строкам логов там не место."""
    result, _counts = scrub_text(str(text or ''))
    return result


def evidence_from_logs(parsed):
    rows = []
    groups = [g for g in parsed.get('groups') or []
              if g.get('level') in ('ERROR', 'FATAL')] or (parsed.get('groups') or [])
    for grp in groups[:MAX_EVIDENCE]:
        origins = list(grp.get('origins') or {})
        rows.append({
            'contour': 'logs',
            'source': ', '.join(origins[:3]) or 'логи',
            'detail': clean(grp.get('template')),
            'count': grp.get('count'),
            'level': grp.get('level'),
            'first': grp.get('first'),
            'last': grp.get('last'),
        })
    return rows


def evidence_from_kb(payload):
    rows = []
    for hit in (payload or [])[:MAX_EVIDENCE]:
        rows.append({
            'contour': 'kb',
            'source': hit.get('id'),
            'detail': clean(hit.get('title')),
            'score': hit.get('score'),
            'path': hit.get('path'),
            'outcome': (hit.get('meta') or {}).get('outcome'),
        })
    return rows


def evidence_from_code(data):
    rows = []
    for frame in (data.get('frames') or [])[:MAX_EVIDENCE]:
        matches = frame.get('matches') or []
        if not matches:
            continue
        blame = frame.get('blame') or {}
        rows.append({
            'contour': 'code',
            'source': '%s:%s' % (matches[0], frame.get('line')),
            'detail': clean((frame.get('context') or [{}])[0].get('text')
                            if isinstance(frame.get('context'), list) else None)
                      or 'кадр стектрейса сопоставлен с файлом проекта',
            'commit': blame.get('hash'),
        })
    # max(0, ...) обязателен: отрицательный срез отрезал бы хвост списка вместо
    # того, чтобы не брать из него ничего
    for hit in (data.get('grep') or [])[:max(0, MAX_EVIDENCE - len(rows))]:
        rows.append({
            'contour': 'code',
            'source': '%s:%s' % (hit.get('path'), hit.get('line')),
            'detail': clean(hit.get('text')) or 'совпадение по тексту ошибки',
        })
    for commit in (data.get('commits') or [])[:2]:
        rows.append({
            'contour': 'code',
            'source': 'коммит %s' % commit.get('hash'),
            'detail': clean(commit.get('subject')),
            'date': commit.get('date'),
        })
    return rows


def next_step_draft(verdict, evidence, args, insufficient):
    """Черновик следующего шага — тот же выбор, что в диалоге, но без вопроса.

    Ведущий вариант выводится из уровня уверенности: чинить по гипотезе нечего,
    а баг с выдуманной причиной хуже честной задачи «разобраться».
    """
    code_places = [e['source'] for e in evidence if e['contour'] == 'code']
    where = ' (%s)' % ', '.join(code_places[:2]) if code_places else ''
    what = args.claim or 'разбор инцидента'
    scope = ' '.join(p for p in [args.service, args.stand] if p)
    head = '%s%s' % (what, ': ' + scope if scope else '')

    if insufficient:
        return {'kind': 'task', 'title': 'Доисследовать: %s' % head,
                'body': 'Данных для вывода не хватило. Нужно добрать перечисленное в '
                        'поле missing и повторить разбор.',
                'why': 'вердикт «данных недостаточно» — тикет с причиной не заводится'}
    if verdict == 'подтверждено данными' and code_places:
        return {'kind': 'fix', 'title': 'Починить: %s' % head,
                'body': 'Причина подтверждена данными, место в коде найдено%s.' % where,
                'why': 'уровень «подтверждено данными» и место в коде известно'}
    if verdict in ('подтверждено данными', 'вероятная причина'):
        return {'kind': 'bug', 'title': 'Баг: %s' % head,
                'body': 'Причина установлена на уровне «%s»%s. В тикете — сигнатура, '
                        'окно времени и доказательства из отчёта.' % (verdict, where),
                'why': 'причина названа, но правки предлагать не на чем' if not code_places
                       else 'уровень «вероятная причина» — чинить рано, но баг обоснован'}
    return {'kind': 'task', 'title': 'Доисследовать: %s' % head,
            'body': 'Уровень вывода — гипотеза. Баг с гипотезой вместо причины не '
                    'заводится: нужно добрать данные.',
            'why': 'гипотеза не тянет ни на правки, ни на баг'}


def build_report(args, stages, out_dir, card):
    """Собирает отчёт разбора. Возвращает (payload, путь) или (payload, None)."""
    logs = stages['logs'].data['parsed'] if stages['logs'].done else None
    kb_payload = stages['kb'].data['payload'] if stages['kb'].done else None
    code = stages['code'].data if stages['code'].done else None
    conf = stages['confidence'].data['payload'] if stages['confidence'].done else None

    evidence = []
    if logs:
        evidence.extend(evidence_from_logs(logs))
    if kb_payload:
        evidence.extend(evidence_from_kb(kb_payload))
    if code:
        evidence.extend(evidence_from_code(code))

    signatures = [s.get('value') for s in (logs or {}).get('signatures') or []
                  if s.get('value')]

    missing = list(args.missing or [])
    for stage in stages.values():
        if not stage.done:
            missing.append('%s: %s' % (stage.title.lower(), stage.reason))
    if card and card.get('missing'):
        missing.extend('карточка инцидента: %s нет в алерте' % part
                       for part in card['missing'])

    passed = [s for s in stages.values() if s.done]
    # ни один контур не сошёлся — вывода нет, и догадка вместо него в автономном
    # режиме дороже: её никто не перечитает
    insufficient = not passed or bool(args.insufficient)
    if card and not card.get('sufficient', True):
        insufficient = True

    verdict = VERDICT_INSUFFICIENT if insufficient else (
        conf.get('verdict') if conf else VERDICT_INSUFFICIENT)
    if not conf and not insufficient:
        insufficient = True
        verdict = VERDICT_INSUFFICIENT

    incident = {
        'stand': args.stand, 'service': args.service,
        'since': args.since.strftime('%Y-%m-%d %H:%M:%S') if args.since else None,
        'until': args.until.strftime('%Y-%m-%d %H:%M:%S') if args.until else None,
        'time_scale': 'UTC',
    }
    if card:
        incident.update({
            'symptom': card.get('symptom'), 'alert_id': card.get('alert_id'),
            'alert_format': card.get('format'),
            'started_at': card.get('started_at'),
            # откуда взято время — из алерта или из INCIDENT_NOW: читатель отчёта
            # не должен это выяснять
            'time_source': card.get('time_source'),
        })

    payload = {
        'schema': REPORT_SCHEMA,
        'generated_at': now().strftime('%Y-%m-%d %H:%M:%S'),
        'mode': mode(),
        'incident': incident,
        'verdict': verdict,
        'confidence': conf.get('confidence') if conf else None,
        'insufficient': insufficient,
        'missing': missing,
        'claim': args.claim,
        'signature': signatures[0] if signatures else None,
        'signatures': signatures[:10],
        'evidence': evidence,
        'contours': [{'key': s.key, 'title': s.title, 'passed': s.done,
                      'reason': s.reason} for s in stages.values()],
        'kb_entry': {'written': False, 'id': None, 'path': None,
                     'reason': 'запись базы знаний делается отдельным запуском '
                               'kb_add.py — он и проставит это поле'},
        'next_step': next_step_draft(verdict, evidence, args, insufficient),
        'environment': {
            'python': '%d.%d.%d' % sys.version_info[:3],
            'limited': False,
            'git': bool(git_toplevel(args.repo) if args.repo else None),
        },
        'artifacts': {
            'out_dir': out_dir,
            'report': report_path(out_dir),
            'stages': {s.key: s.path for s in stages.values() if s.path},
            'trace_file': os.environ.get(ENV_TRACE),
            'session_file': os.environ.get(ENV_SESSION),
        },
        'stopped_at': args.stopped_at,
    }

    path = report_path(out_dir)
    if not write_report(payload, path):
        sys.stderr.write('warning: отчёт не сохранён: %s\n' % path)
        return payload, None
    return payload, path


# --- вывод ----------------------------------------------------------------


def write_json(out_dir, name, payload):
    path = os.path.join(out_dir, name)
    try:
        dump_json(payload, path)
    except OSError as exc:
        sys.stderr.write('warning: не сохранил %s: %s\n' % (path, exc))
        return None
    return path


def render(stages, args, out_dir, out, report=None):
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
    if report:
        w('- **Отчёт разбора**: `%s` — вердикт «%s»\n'
          % (report['path'], report['payload']['verdict']))
    w('\nОни принимаются на вход остальными скриптами скилла: `timeline.py`, '
      '`trace.py`, `kb_add.py`.\n')
    if report:
        w('Отчёт — машинный выход разбора: его читает обвязка. Запись базы знаний '
          'проставляется в него запуском `kb_add.py --report`.\n')
    if not is_auto():
        render_kb_reminder(stages, w)


def render_kb_reminder(stages, w):
    """Напоминание о шаге 8 в хвосте сводки — только в диалоговом разборе.

    Инструкция записать разбор есть в SKILL.md, но решение принимается здесь —
    по прочитанной сводке. Без напоминания в этой точке шаг переступается молча:
    так и произошло на прогоне, где разбор дошёл до вывода и на нём закончился.
    В автономном режиме читателя у напоминания нет: судьбу записи проставляет в
    отчёт сам `kb_add.py --report`.
    """
    parsed = stages['logs'].path if stages['logs'].done else None
    w('\n## Дальше — пополни базу знаний\n\n')
    w('Разбор дошёл до вывода — запиши его, иначе следующий такой же случай '
      'разбирают с нуля:\n\n')
    w('```bash\npython3 kb_add.py --title "..." --root-cause "..." \\\n')
    if parsed:
        w('    --from-parsed %s \\\n' % parsed)
    w('    --file <файл> --outcome unverified\n```\n\n')
    w('Уровень уверенности записи не отменяет: неподтверждённая причина ложится с '
      'исходом «не проверена» и всё равно выводит следующий разбор на это место. '
      'Записи не будет — назови причину в ответе строкой «Запись в базу».\n')


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
    хвост по разделам, а не по символам, и говорим, что урезали.

    Первый раздел целиком не гарантируется: он проходит через ту же укладку,
    что и остальные — раздел логов при большом `--top` мог сам по себе быть
    больше `--max-chars`, и раньше это был единственный способ превысить
    заданный предел.
    """
    if budget <= 0 or len(text) <= budget:
        return text
    parts = text.split('\n## ')
    kept = [parts[0]]
    used = len(parts[0])
    dropped = []
    # резерв под строку «не показаны разделы», которая добавляется в конце —
    # без него сама эта строка могла бы вытолкнуть итог за бюджет
    reserve = 200
    for part in parts[1:]:
        chunk = '\n## ' + part
        if used + len(chunk) > budget - reserve:
            dropped.append(part.split('\n', 1)[0].strip())
            continue
        kept.append(part)
        used += len(chunk)
    result = '\n## '.join(kept)
    if dropped:
        result += ('\n_Не показаны разделы: %s — сводка ограничена по объёму. '
                   'Они есть в машинных результатах._\n' % ', '.join(dropped))
    if len(result) > budget:
        # запасной случай: даже преамбула с перечнем скрытых разделов не
        # уложилась — режем по символам, это надёжнее превышения предела
        result = result[:budget - 1].rstrip() + '…'
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
    ap.add_argument('--level', choices=LEVELS, help='минимальный уровень')
    ap.add_argument('--since', help='начало окна, напр. "2026-07-28 12:00"')
    ap.add_argument('--until', help='конец окна')
    ap.add_argument('--grep', help='regex-фильтр по сырому тексту записи')
    ap.add_argument('--top', type=int, default=10, help='шаблонов в сводке логов (10)')
    ap.add_argument('--top-kb', type=int, default=5, help='записей базы знаний (5)')
    ap.add_argument('--min-score', type=float, default=1.0)
    ap.add_argument('--out', help='директория для JSON этапов и отчёта '
                                  '(по умолчанию временная; в автономном режиме обязателен)')
    ap.add_argument('--incident', help='карточка инцидента от alert_to_incident.py')
    ap.add_argument('--missing', action='append',
                    help='чего не хватило для вывода — попадает в отчёт (можно повторять)')
    ap.add_argument('--insufficient', action='store_true',
                    help='завершить разбор вердиктом «данных недостаточно»: источник '
                         'логов неоднозначен, окружение непригодно и подобное')
    ap.add_argument('--stopped-at',
                    help='на каком шаге разбор остановлен — например по потолку прохода')
    ap.add_argument('--max-chars', type=int, default=MAX_SUMMARY_CHARS,
                    help='предельный объём сводки в символах (%d), 0 — без предела'
                         % MAX_SUMMARY_CHARS)
    ap.add_argument('--max-lines', type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument('--encoding', default='utf-8')
    args = ap.parse_args(argv)

    card = None
    if args.incident:
        try:
            with open(args.incident, 'r', encoding='utf-8') as fh:
                card = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SystemExit('Не прочитал карточку инцидента %s: %s' % (args.incident, exc))
        if not isinstance(card, dict):
            raise SystemExit('Карточка инцидента %s — не объект JSON' % args.incident)
        # аргументы командной строки сильнее карточки: обвязка могла сузить окно
        args.stand = args.stand or card.get('stand')
        args.service = args.service or card.get('service')
        args.since = args.since or card.get('since')
        args.until = args.until or card.get('until')

    args.since = pl.parse_time_arg(args.since) if args.since else None
    args.until = pl.parse_time_arg(args.until) if args.until else None

    if is_auto() and not args.out:
        # отчёт во временной директории, имя которой обвязке неизвестно,
        # бесполезен: за ним никто не придёт
        raise SystemExit(
            'В автономном режиме нужен --out: обвязка задаёт директорию отчёта явно, '
            'иначе report.json ляжет туда, откуда его никто не заберёт.')

    out_dir = args.out or os.path.join(
        os.environ.get('TMPDIR') or '/tmp', 'incident-detective')
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

    payload, path = build_report(args, stages, out_dir, card)
    report = {'payload': payload, 'path': path} if path else None

    buf = io.StringIO()
    render(stages, args, out_dir, buf, report)
    sys.stdout.write(fit_output(buf.getvalue(), args.max_chars))
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
