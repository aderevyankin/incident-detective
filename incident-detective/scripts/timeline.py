#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Хронология инцидента из нескольких источников.

Сводит в одну ленту:
  - первое появление каждого нового шаблона ошибки (самое важное — момент,
    когда что-то начало происходить впервые);
  - всплески по гистограмме (резкий рост относительно фона);
  - коммиты git в окне инцидента;
  - вехи, заданные руками (--event): деплой, рестарт, жалоба пользователя.

Смысл: вместо десятков тысяч строк — два десятка событий, по которым видно
последовательность. Каждая строка несёт источник, чтобы вывод можно было
перепроверить.

Времена ленты — в единой шкале (UTC), она называется в выводе. Часы источников
расходятся, и тогда порядок событий врёт: `--check-clocks` оценивает расхождение
той же логикой, что и `trace.py`, но **не применяет** его молча — сдвиг задаётся
руками через `--offset`, чтобы в ленте не появилось подогнанное время.
"""

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

import parse_logs as pl  # noqa: E402
from code_hints import commits_in_window, is_git_repo  # noqa: E402
from kb_common import (  # noqa: E402
    DEFAULT_MAX_LINES, MAX_SUMMARY_CHARS, TAIL_RESERVE, TIME_SCALE, apply_offset,
    dump_json, dump_overflow, estimate_clock_skew, fit_by_render, parse_offset_arg,
    parse_time_arg, render_clock_findings, run_script, sort_key,
)

SPIKE_FACTOR = 3.0
SPIKE_MIN = 5

# Записи лога, а не пометки на ленте: у вехи, заданной руками, и у коммита зона
# не «неизвестна» — их время задал человек, и предупреждать о нём не о чем.
LOG_KINDS = ('первое появление', 'всплеск')

MIXED_ZONES_NOTE = (
    '⚠ Часть источников пишет время с указанной зоной, часть — без неё. Порядок '
    'событий между этими группами может быть неверен, а с ним и вывод о самом '
    'раннем новом событии: сверь зоны источников, прежде чем искать причину '
    'по этой ленте.\n')


class Args(object):
    """Минимальный набор полей, который ждёт parse_logs.analyze."""

    def __init__(self, **kw):
        self.level = None
        self.since = None
        self.until = None
        self.grep = None
        self.top = 200
        for key, val in kw.items():
            setattr(self, key, val)


def parsed_tz_default(parsed):
    """Известна ли зона у разбора в целом.

    Разбор, сделанный прежней версией, полей о зоне не имеет — и читается как
    «зона неизвестна», то есть ровно как вело себя всё до этого изменения.
    Домысливать за старый файл, что зона там была, нельзя: это и есть та самая
    молчаливая подстановка, от которой изменение избавляется.
    """
    stats = parsed.get('stats') or {}
    return bool(stats.get('tz_known')) and not stats.get('tz_unknown')


def events_from_parsed(parsed, label):
    """Достаёт события из JSON-вывода parse_logs.py."""
    events = []
    tz_default = parsed_tz_default(parsed)
    for grp in parsed.get('groups', []):
        if not grp.get('first'):
            continue
        if pl.LEVEL_ORD.get(grp.get('level'), 2) < pl.LEVEL_ORD['WARN']:
            continue
        template = grp.get('template', '')
        events.append({
            'ts': parse_time_arg(grp['first']),
            'kind': 'первое появление',
            'level': grp.get('level'),
            'text': template[:140],
            'detail': 'всего %d×, последнее %s' % (grp.get('count', 0), grp.get('last') or '?'),
            'source': label,
            'tz_known': bool(grp.get('tz_known', tz_default)),
            # общая точка сравнения источников: один и тот же шаблон, впервые
            # увиденный двумя источниками, — это одно и то же событие
            'match': template[:140],
        })
    hist = parsed.get('histogram') or {}
    events.extend(spikes(hist, label, tz_default))
    return events


def spikes(hist, label, tz_known=False):
    """Находит резкие всплески в поминутной гистограмме ошибок."""
    if len(hist) < 3:
        return []
    points = sorted((parse_time_arg(k), v) for k, v in hist.items())
    events = []
    baseline = 0.0
    for i, (ts, val) in enumerate(points):
        if i == 0:
            baseline = val
            continue
        window = [v for _t, v in points[max(0, i - 5):i]]
        baseline = sum(window) / float(len(window)) if window else 0.0
        if val >= SPIKE_MIN and val >= max(baseline * SPIKE_FACTOR, baseline + SPIKE_MIN):
            events.append({
                'ts': ts,
                'kind': 'всплеск',
                'level': 'ERROR',
                'text': '%d ошибок за интервал (фон ~%.1f)' % (val, baseline),
                'detail': '',
                'source': label,
                'tz_known': tz_known,
                'match': None,
            })
    # схлопываем соседние всплески в один
    merged = []
    for ev in events:
        if merged and (ev['ts'] - merged[-1]['ts']) <= timedelta(minutes=2):
            continue
        merged.append(ev)
    return merged[:8]


def events_from_log(path, args, label):
    sources = pl.expand_sources([path])
    records = pl.read_records(sources, args.encoding, args.max_lines)
    result = pl.analyze(records, Args(since=args.since, until=args.until))
    stats = result['stats']
    parsed = {
        'stats': {'tz_known': stats['tz_known'], 'tz_unknown': stats['tz_unknown']},
        'groups': [{
            'level': g.level, 'count': g.count, 'template': g.template,
            'first': pl.fmt_ts(g.first) if g.first else None,
            'last': pl.fmt_ts(g.last) if g.last else None,
            'tz_known': g.tz_known,
        } for g in result['groups'][:100]],
        'histogram': {k.strftime('%Y-%m-%d %H:%M'): v
                      for k, v in sorted(result['err_hist'].items())},
    }
    return events_from_parsed(parsed, label)


def number_events(events):
    """Порядковый номер события — часть ключа порядка при равном времени."""
    for i, ev in enumerate(events):
        ev.setdefault('seq', i)
    return events


def ordered(events):
    """Лента в устойчивом порядке: время, источник, порядковый номер."""
    return sorted(events, key=lambda e: sort_key(e['ts'], e.get('source'), e.get('seq', 0)))


def dedupe(events):
    """Убирает повторы одного и того же шаблона в пределах минуты."""
    seen = {}
    out = []
    for ev in ordered(events):
        key = (ev['kind'], ev['text'][:60], ev['source'])
        prev = seen.get(key)
        if prev and (ev['ts'] - prev) < timedelta(minutes=1):
            continue
        seen[key] = ev['ts']
        out.append(ev)
    return out


def mixed_zones(events):
    """Попали ли в ленту записи логов и с известной зоной, и без неё."""
    logs = [e for e in events if e['kind'] in LOG_KINDS]
    return (any(e.get('tz_known') for e in logs)
            and any(not e.get('tz_known') for e in logs))


def check_clocks(events):
    """Расхождение часов между источниками ленты.

    Общая точка сравнения — один и тот же шаблон сообщения, впервые увиденный
    разными источниками. Сама оценка общая с цепочкой запроса
    (`kb_common.estimate_clock_skew`), поэтому два инструмента не дают разных
    ответов об одних и тех же источниках.
    """
    observations = {}
    for ev in events:
        key = ev.get('match')
        if not key or ev['kind'] not in LOG_KINDS:
            continue
        firsts = observations.setdefault(key, {})
        label = ev['source']
        if label not in firsts or ev['ts'] < firsts[label]:
            firsts[label] = ev['ts']
    # точки, попавшие лишь к одному источнику, сравнивать не с чем
    shared = {k: v for k, v in observations.items() if len(v) > 1}
    return estimate_clock_skew(shared)


KIND_MARK = {
    'первое появление': '🆕',
    'всплеск': '📈',
    'коммит': '⎇ ',
    'веха': '📌',
}


def render_event(ev, write):
    write('**%s** %s %s\n' % (ev['ts'].strftime('%m-%d %H:%M:%S'),
                              KIND_MARK.get(ev['kind'], '·'), ev['kind']))
    write('  %s\n' % ev['text'])
    line = []
    if ev.get('detail'):
        line.append(ev['detail'])
    if ev.get('source'):
        line.append('источник: %s' % ev['source'])
    if line:
        write('  _%s_\n' % ' · '.join(line))
    write('\n')


def overflow_events(events):
    return [{'ts': pl.fmt_ts(ev['ts']), 'kind': ev['kind'], 'level': ev.get('level'),
             'text': ev['text'], 'detail': ev.get('detail'), 'source': ev.get('source')}
            for ev in events]


def render_md(events, out, window, offsets=None, clocks=None, budget=0):
    """Лента в пределах бюджета объёма.

    Вывод собирается в буфер: события укладываются в остаток бюджета от уже
    написанной шапки и от заранее посчитанного хвоста о самом раннем событии.
    И то и другое — часть тех же 12 000 символов, и заменять их константой
    значит промахиваться на разницу.
    """
    buf = io.StringIO()
    _render_md(events, buf, window, offsets, clocks, budget)
    out.write(buf.getvalue())


def _render_md(events, out, window, offsets=None, clocks=None, budget=0):
    w = out.write
    w('# Хронология инцидента\n\n')
    if not events:
        w('Событий не найдено. Проверь окно (`--since`/`--until`) и источники.\n')
        return
    if window[0]:
        w('Окно: %s — %s\n' % (pl.fmt_ts(window[0]), pl.fmt_ts(window[1])))
    w('Времена ленты — в %s.' % TIME_SCALE)
    applied = sorted((label, secs) for label, secs in (offsets or {}).items() if secs)
    if applied:
        w(' Применена заданная поправка: %s — время сдвинуто, в логе его не было.'
          % ', '.join('%s %+.2f с' % (label, secs) for label, secs in applied))
    w('\n\n')

    if mixed_zones(events):
        w(MIXED_ZONES_NOTE + '\n')
    # о расхождении часов лента сообщает до самой ленты: читатель должен узнать о
    # нём раньше, чем прочтёт вывод о том, что случилось первым
    if clocks:
        findings, points = clocks
        systematic = [f for f in findings if f['systematic']]
        if systematic:
            f = systematic[0]
            w('⚠ Часы источников расходятся: %s → %s на %+.2f с по %d общим точкам. '
              'Автоматически время не правится — применить поправку: '
              '`--offset %s=%.2f`.\n\n'
              % (f['from'], f['to'], f['median'], f['traces'], f['to'], -f['median']))

    # ленте нужен предел объёма так же, как сводке логов: события идут в
    # хронологическом порядке, и обрезка хвоста не теряет самое важное — то, что
    # случилось раньше. Хвост о самом раннем событии от укладки не зависит и
    # потому считается заранее — иначе он вылезет за предел уже после неё
    tail = io.StringIO()
    _render_tail(events, tail.write)
    shown, hidden = fit_by_render(events, render_event, budget,
                                  reserve=TAIL_RESERVE,
                                  used=len(out.getvalue()) + len(tail.getvalue()))

    prev = None
    for ev in shown:
        gap = ''
        if prev is not None:
            delta = ev['ts'] - prev
            if delta >= timedelta(minutes=1):
                gap = '  (+%s)' % pl.fmt_span(delta)
        prev = ev['ts']
        mark = KIND_MARK.get(ev['kind'], '·')
        w('**%s** %s %s%s\n' % (ev['ts'].strftime('%m-%d %H:%M:%S'), mark, ev['kind'], gap))
        w('  %s\n' % ev['text'])
        line = []
        if ev.get('detail'):
            line.append(ev['detail'])
        if ev.get('source'):
            line.append('источник: %s' % ev['source'])
        if line:
            w('  _%s_\n' % ' · '.join(line))
        w('\n')

    if hidden:
        full = dump_overflow(overflow_events(events), 'timeline-events.json')
        note = (' Полный список: `%s`.' % full) if full else ''
        w('_Не показано событий: %d — лента ограничена по объёму.%s_\n\n' % (hidden, note))

    w(tail.getvalue())


def _render_tail(events, w):
    """Хвост ленты: он строится по всем событиям, а не по показанным."""
    firsts = [e for e in events if e['kind'] == 'первое появление']
    if not firsts:
        return
    w('---\n\n**Самое раннее новое событие:** %s %s — %s (%s)\n\n'
      % (firsts[0]['ts'].strftime('%m-%d %H:%M:%S'), TIME_SCALE,
         firsts[0]['text'][:100], firsts[0]['source']))
    w('Причину ищи здесь и раньше по времени. Всё, что появилось позже, '
      'скорее всего следствие.\n')
    if mixed_zones(events):
        w('\nПорядок этой ленты недостоверен — см. предупреждение о зонах выше: '
          'вывод о том, что было первым, может смениться после сверки зон.\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description='Единая хронология инцидента из нескольких источников.')
    ap.add_argument('--parsed', action='append', default=[],
                    help='JSON parse_logs.py (можно несколько; формат path или label=path)')
    ap.add_argument('--log', action='append', default=[],
                    help='файл лога напрямую (можно несколько; label=path)')
    ap.add_argument('--event', action='append', default=[],
                    help='веха вручную: "2026-07-28 12:20|деплой payment-api 1.24"')
    ap.add_argument('--repo', help='репозиторий — добавить коммиты в окне')
    ap.add_argument('--since', help='начало окна')
    ap.add_argument('--until', help='конец окна')
    ap.add_argument('--check-clocks', action='store_true',
                    help='оценить расхождение часов между источниками')
    ap.add_argument('--offset', action='append', default=[],
                    help='сдвиг часов источника в секундах: "payment=-2.5"')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    ap.add_argument('--max-chars', type=int, default=MAX_SUMMARY_CHARS,
                    help='предельный объём ленты в символах (%d), 0 — без предела'
                         % MAX_SUMMARY_CHARS)
    ap.add_argument('--encoding', default='utf-8')
    ap.add_argument('--max-lines', type=int, default=DEFAULT_MAX_LINES)
    args = ap.parse_args(argv)

    args.since = parse_time_arg(args.since) if args.since else None
    args.until = parse_time_arg(args.until) if args.until else None
    args.offsets = dict(parse_offset_arg(item) for item in args.offset)

    events = []
    labels = set()
    for item in args.parsed:
        label, _, path = item.partition('=')
        if not path:
            label, path = os.path.basename(item), item
        labels.add(label)
        # без перехвата отсутствующий файл давал сырую трассировку — в разборе
        # инцидента это выглядит как поломка скрипта, а не как опечатка в пути
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                # только разбор JSON: ValueError из `events_from_parsed` — это
                # другая беда, и объяснять её как «файл не JSON» значит уводить
                # от настоящей причины
                payload = json.load(fh)
        except OSError as exc:
            raise SystemExit('Не прочитал разбор %s: %s' % (path, exc))
        except ValueError as exc:
            raise SystemExit('Файл %s — не JSON-вывод parse_logs.py: %s '
                             '(сохрани разбор через `--format json`)' % (path, exc))
        # JSON разобрался — это ещё не значит, что перед нами разбор логов:
        # без проверки список или число уходили в обход полей сырой трассировкой
        if not isinstance(payload, dict):
            raise SystemExit('Файл %s — валидный JSON, но не разбор parse_logs.py: '
                             'ожидался объект с полем `groups`, а получен %s'
                             % (path, type(payload).__name__))
        events.extend(events_from_parsed(payload, label))
    for item in args.log:
        label, _, path = item.partition('=')
        if not path:
            label, path = os.path.basename(item), item
        labels.add(label)
        events.extend(events_from_log(path, args, label))

    unknown = set(args.offsets) - labels
    if unknown:
        raise SystemExit('--offset для неизвестного источника: %s'
                         % ', '.join(sorted(unknown)))
    # поправка применяется только там, где её задали явно: сам скрипт время не
    # правит — подогнанного времени в ленте быть не должно
    for ev in events:
        ev['ts'] = apply_offset(ev['ts'], args.offsets.get(ev['source'], 0.0))

    for raw in args.event:
        when, _, text = raw.partition('|')
        if not text:
            m = re.match(r'^(\S+(?:\s+\S+)?)\s+(.*)$', raw)
            if not m:
                raise SystemExit('Веха задаётся как "ВРЕМЯ|описание": %r' % raw)
            when, text = m.group(1), m.group(2)
        events.append({'ts': parse_time_arg(when), 'kind': 'веха', 'level': None,
                       'text': text.strip(), 'detail': '', 'source': 'указано вручную',
                       'tz_known': True, 'match': None})

    if not events:
        raise SystemExit('Нет источников: укажи --parsed, --log или --event')

    number_events(events)
    clocks = check_clocks(events)

    if args.check_clocks:
        findings, points = clocks
        if args.format == 'json':
            dump_json({'points': points, 'clock_findings': findings,
                      'time_scale': TIME_SCALE, 'mixed_timezones': mixed_zones(events)},
                     sys.stdout)
        else:
            sys.stdout.write('# Проверка часов между источниками\n\n')
            if mixed_zones(events):
                sys.stdout.write(MIXED_ZONES_NOTE + '\n')
            render_clock_findings(findings, points, sys.stdout.write,
                                  unit='общих точек сравнения')
        return 0

    times = [e['ts'] for e in events]
    win_start = args.since or min(times)
    win_end = args.until or max(times)

    if args.repo and is_git_repo(os.path.abspath(args.repo)):
        # commits_in_window отдаёт время уже на шкале ленты (UTC) и понимает
        # границы окна там же — иначе `tz_known: True` ниже был бы неправдой
        for commit in commits_in_window(os.path.abspath(args.repo), win_start, win_end):
            try:
                ts = datetime.strptime(commit['date'], '%Y-%m-%d %H:%M')
            except ValueError:
                continue
            events.append({
                'ts': ts, 'kind': 'коммит', 'level': None,
                'text': '%s — %s' % (commit['hash'], commit['subject'][:110]),
                'detail': commit['author'], 'source': 'git',
                'tz_known': True, 'match': None,
            })
    number_events(events)

    events = [e for e in events
              if (not args.since or e['ts'] >= args.since)
              and (not args.until or e['ts'] <= args.until)]
    events = dedupe(events)

    if args.format == 'json':
        # вывод остаётся списком событий — его читают как список; шкала едет
        # полем события, а не новой обёрткой, которая сломала бы читателей
        dump_json([{k: v for k, v in ev.items() if k != 'match'}
                   for ev in ({**e, 'ts': e['ts'].strftime('%Y-%m-%d %H:%M:%S'),
                               'time_scale': TIME_SCALE} for e in events)],
                  sys.stdout)
    else:
        render_md(events, sys.stdout, (win_start, win_end), args.offsets, clocks,
                  budget=args.max_chars)
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
