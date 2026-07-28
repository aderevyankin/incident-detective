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
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parse_logs as pl  # noqa: E402
from code_hints import commits_in_window, is_git_repo  # noqa: E402

SPIKE_FACTOR = 3.0
SPIKE_MIN = 5


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


def parse_dt(text):
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    dt, _ = pl.find_timestamp(text, limit=len(text) + 1)
    if dt:
        return dt
    raise SystemExit('Не разобрал время: %r' % text)


def events_from_parsed(parsed, label):
    """Достаёт события из JSON-вывода parse_logs.py."""
    events = []
    for grp in parsed.get('groups', []):
        if not grp.get('first'):
            continue
        if pl.LEVEL_ORD.get(grp.get('level'), 2) < pl.LEVEL_ORD['WARN']:
            continue
        events.append({
            'ts': parse_dt(grp['first']),
            'kind': 'первое появление',
            'level': grp.get('level'),
            'text': grp.get('template', '')[:140],
            'detail': 'всего %d×, последнее %s' % (grp.get('count', 0), grp.get('last') or '?'),
            'source': label,
        })
    hist = parsed.get('histogram') or {}
    events.extend(spikes(hist, label))
    return events


def spikes(hist, label):
    """Находит резкие всплески в поминутной гистограмме ошибок."""
    if len(hist) < 3:
        return []
    points = sorted((parse_dt(k), v) for k, v in hist.items())
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
    parsed = {
        'groups': [{
            'level': g.level, 'count': g.count, 'template': g.template,
            'first': pl.fmt_ts(g.first) if g.first else None,
            'last': pl.fmt_ts(g.last) if g.last else None,
        } for g in result['groups'][:100]],
        'histogram': {k.strftime('%Y-%m-%d %H:%M'): v
                      for k, v in sorted(result['err_hist'].items())},
    }
    return events_from_parsed(parsed, label)


def dedupe(events):
    """Убирает повторы одного и того же шаблона в пределах минуты."""
    seen = {}
    out = []
    for ev in sorted(events, key=lambda e: e['ts']):
        key = (ev['kind'], ev['text'][:60], ev['source'])
        prev = seen.get(key)
        if prev and (ev['ts'] - prev) < timedelta(minutes=1):
            continue
        seen[key] = ev['ts']
        out.append(ev)
    return out


KIND_MARK = {
    'первое появление': '🆕',
    'всплеск': '📈',
    'коммит': '⎇ ',
    'веха': '📌',
}


def render_md(events, out, window):
    w = out.write
    w('# Хронология инцидента\n\n')
    if not events:
        w('Событий не найдено. Проверь окно (`--since`/`--until`) и источники.\n')
        return
    if window[0]:
        w('Окно: %s — %s\n\n' % (pl.fmt_ts(window[0]), pl.fmt_ts(window[1])))
    prev = None
    for ev in events:
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

    firsts = [e for e in events if e['kind'] == 'первое появление']
    if firsts:
        w('---\n\n**Самое раннее новое событие:** %s — %s (%s)\n\n'
          % (firsts[0]['ts'].strftime('%m-%d %H:%M:%S'), firsts[0]['text'][:100],
             firsts[0]['source']))
        w('Причину ищи здесь и раньше по времени. Всё, что появилось позже, '
          'скорее всего следствие.\n')


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
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    ap.add_argument('--encoding', default='utf-8')
    ap.add_argument('--max-lines', type=int, default=2000000)
    args = ap.parse_args(argv)

    args.since = parse_dt(args.since) if args.since else None
    args.until = parse_dt(args.until) if args.until else None

    events = []
    for item in args.parsed:
        label, _, path = item.partition('=')
        if not path:
            label, path = os.path.basename(item), item
        with open(path, 'r', encoding='utf-8') as fh:
            events.extend(events_from_parsed(json.load(fh), label))
    for item in args.log:
        label, _, path = item.partition('=')
        if not path:
            label, path = os.path.basename(item), item
        events.extend(events_from_log(path, args, label))

    for raw in args.event:
        when, _, text = raw.partition('|')
        if not text:
            m = re.match(r'^(\S+(?:\s+\S+)?)\s+(.*)$', raw)
            if not m:
                raise SystemExit('Веха задаётся как "ВРЕМЯ|описание": %r' % raw)
            when, text = m.group(1), m.group(2)
        events.append({'ts': parse_dt(when), 'kind': 'веха', 'level': None,
                       'text': text.strip(), 'detail': '', 'source': 'указано вручную'})

    if not events:
        raise SystemExit('Нет источников: укажи --parsed, --log или --event')

    times = [e['ts'] for e in events]
    win_start = args.since or min(times)
    win_end = args.until or max(times)

    if args.repo and is_git_repo(os.path.abspath(args.repo)):
        for commit in commits_in_window(os.path.abspath(args.repo), win_start, win_end):
            try:
                ts = datetime.strptime(commit['date'], '%Y-%m-%d %H:%M')
            except ValueError:
                continue
            events.append({
                'ts': ts, 'kind': 'коммит', 'level': None,
                'text': '%s — %s' % (commit['hash'], commit['subject'][:110]),
                'detail': commit['author'], 'source': 'git',
            })

    events = [e for e in events
              if (not args.since or e['ts'] >= args.since)
              and (not args.until or e['ts'] <= args.until)]
    events = dedupe(events)

    if args.format == 'json':
        json.dump([{**e, 'ts': e['ts'].strftime('%Y-%m-%d %H:%M:%S')} for e in events],
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    else:
        render_md(events, sys.stdout, (win_start, win_end))
    return 0


if __name__ == '__main__':
    sys.exit(main())
