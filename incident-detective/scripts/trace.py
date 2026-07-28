#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная цепочка одного запроса по correlation id через несколько сервисов.

`parse_logs.py --trace` показывает записи одного id внутри одного разбора.
Здесь другое: логи нескольких сервисов сводятся в одну цепочку хопов —
кто кого звал, сколько ждал, на ком оборвалось.

  python3 trace.py --log gateway=gw.log --log payment=pay.log --top
  python3 trace.py --log gateway=gw.log --log payment=pay.log --id 7f3a-9c
  python3 trace.py --log ... --id 7f3a-9c --offset payment=-2.5

Часы на стендах расходятся, и тогда хоп выглядит уехавшим назад во времени.
`--check-clocks` оценивает расхождение по общим запросам, но **не применяет**
его молча: сдвиг задаётся руками через `--offset`, чтобы в выводе не появилось
подогнанное время, которого не было в логах.
"""

import argparse
import os
import sys
from collections import Counter, OrderedDict
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

import parse_logs as pl  # noqa: E402
from kb_common import (  # noqa: E402
    DEFAULT_MAX_LINES, MAX_SUMMARY_CHARS, TIME_SCALE, apply_offset, dump_json,
    dump_overflow, estimate_clock_skew, fit_by_render, parse_offset_arg,
    parse_time_arg, render_clock_findings, run_script, sort_key,
)


# Смешение записей с зоной и без неё чинить нечем: узнать зону наивных записей
# неоткуда. Значит, о нём говорится прямо — и с последствием, а не только с фактом.
MIXED_ZONES_NOTE = (
    '⚠ Часть источников пишет время с указанной зоной, часть — без неё. Порядок '
    'хопов между этими группами может быть неверен, а с ним и вывод о точке '
    'обрыва: прежде чем заводить баг, сверь зоны источников.\n')


class Hop(object):
    """Отрезок цепочки внутри одного сервиса."""

    __slots__ = ('service', 'first', 'last', 'records', 'errors', 'levels', 'statuses')

    def __init__(self, service):
        self.service = service
        self.first = None
        self.last = None
        self.records = []
        self.errors = 0
        self.levels = Counter()
        self.statuses = Counter()

    def add(self, rec):
        self.records.append(rec)
        if rec.ts:
            if self.first is None or rec.ts < self.first:
                self.first = rec.ts
            if self.last is None or rec.ts > self.last:
                self.last = rec.ts
        level = rec.level or 'INFO'
        self.levels[level] += 1
        if pl.LEVEL_ORD.get(level, 2) >= pl.LEVEL_ORD['ERROR']:
            self.errors += 1
        if rec.status:
            self.statuses[rec.status] += 1

    @property
    def worst_level(self):
        if not self.levels:
            return None
        return max(self.levels, key=lambda lv: pl.LEVEL_ORD.get(lv, 2))


# --------------------------------------------------------------------------
# Чтение источников
# --------------------------------------------------------------------------


def parse_source_arg(item):
    """'payment=logs/pay.log' -> ('payment', 'logs/pay.log')."""
    label, sep, path = item.partition('=')
    if not sep or not path:
        path = item
        label = os.path.basename(item.rstrip('/')) or item
        for suffix in ('.log', '.txt', '.json', '.gz'):
            if label.endswith(suffix):
                label = label[:-len(suffix)]
    return label, path


def read_service(label, path, args):
    """Читает один источник, возвращает записи с меткой сервиса."""
    sources = pl.expand_sources([path])
    if not sources:
        print('warning: источник не найден: %s' % path, file=sys.stderr)
        return []
    offset = args.offsets.get(label, 0.0)
    out = []
    for rec in pl.read_records(sources, args.encoding, args.max_lines):
        if offset and rec.ts:
            rec.ts = apply_offset(rec.ts, offset)
        if args.since and rec.ts and rec.ts < args.since:
            continue
        if args.until and rec.ts and rec.ts > args.until:
            continue
        out.append((label, rec))
    return out


def trace_key(rec):
    """id запроса из записи; None — если его нет."""
    if rec.trace:
        return rec.trace.strip().strip('"\',;')
    m = pl.TRACE_TEXT_RE.search(rec.raw_text[:400])
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Сборка цепочек
# --------------------------------------------------------------------------


def collect_candidates(pairs):
    """Сводка по всем id: сколько записей, ошибок, сервисов."""
    stats = OrderedDict()
    for label, rec in pairs:
        key = trace_key(rec)
        if not key:
            continue
        item = stats.get(key)
        if item is None:
            item = stats[key] = {'id': key, 'records': 0, 'errors': 0,
                                 'services': set(), 'first': None, 'last': None}
        item['records'] += 1
        item['services'].add(label)
        if pl.LEVEL_ORD.get(rec.level or 'INFO', 2) >= pl.LEVEL_ORD['ERROR']:
            item['errors'] += 1
        if rec.ts:
            if item['first'] is None or rec.ts < item['first']:
                item['first'] = rec.ts
            if item['last'] is None or rec.ts > item['last']:
                item['last'] = rec.ts
    return stats


def rank_candidates(stats, limit=10):
    """Интереснее тот запрос, где больше ошибок и больше задетых сервисов."""
    items = list(stats.values())
    items.sort(key=lambda it: (-it['errors'], -len(it['services']), -it['records'], it['id']))
    return items[:limit]


def build_chain(pairs, trace_id):
    """Хопы по сервисам для одного id, в порядке времени."""
    hops = OrderedDict()
    matched = []
    needle = trace_id.lower()
    for label, rec in pairs:
        key = trace_key(rec)
        hit = (key and needle in key.lower()) or needle in rec.raw_text.lower()
        if not hit:
            continue
        matched.append((label, rec))
        hop = hops.get(label)
        if hop is None:
            hop = hops[label] = Hop(label)
        hop.add(rec)
    ordered = sorted(hops.values(), key=lambda h: sort_key(h.first, h.service, 0))
    return ordered, matched


def chain_findings(hops):
    """Точка обрыва и самая долгая пауза между сервисами.

    Обрыв ищем по самой ранней ошибке во всей цепочке, а не по первому
    сервису в порядке хопов: вызывающий сервис часто пишет свою ошибку
    (таймаут, 5xx) позже того, кто на самом деле упал первым.
    """
    errors = []
    for hop in hops:
        for rec in hop.records:
            if pl.LEVEL_ORD.get(rec.level or 'INFO', 2) >= pl.LEVEL_ORD['ERROR']:
                errors.append((rec.ts, hop.service, rec.line_no, rec))
    errors.sort(key=lambda e: sort_key(e[0], e[1], e[2]))
    first_error = {'service': errors[0][1], 'rec': errors[0][3]} if errors else None

    slowest = None
    for prev, cur in zip(hops, hops[1:]):
        if not prev.first or not cur.first:
            continue
        gap = cur.first - prev.first
        if slowest is None or gap > slowest['gap']:
            slowest = {'from': prev.service, 'to': cur.service, 'gap': gap}
    return first_error, slowest


# --------------------------------------------------------------------------
# Проверка часов
# --------------------------------------------------------------------------


def check_clocks(pairs):
    """Расхождение часов между сервисами по общим запросам.

    Общая точка сравнения — id запроса: берём самую раннюю отметку каждого
    сервиса по этому запросу. Сама оценка — общая с хронологией
    (`kb_common.estimate_clock_skew`): два инструмента не должны давать разных
    ответов об одних и тех же источниках.
    """
    per_trace = {}
    for label, rec in pairs:
        key = trace_key(rec)
        if not key or not rec.ts:
            continue
        firsts = per_trace.setdefault(key, {})
        if label not in firsts or rec.ts < firsts[label]:
            firsts[label] = rec.ts
    return estimate_clock_skew(per_trace)


def mixed_zones(pairs):
    """Попали ли в цепочку записи и с известной зоной, и без неё."""
    known = any(rec.tz_known for _label, rec in pairs if rec.ts)
    unknown = any(not rec.tz_known for _label, rec in pairs if rec.ts)
    return known and unknown


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------


def fmt_gap(delta):
    secs = delta.total_seconds()
    if abs(secs) < 1:
        return '%d мс' % round(secs * 1000)
    return pl.fmt_span(abs(delta)) if abs(secs) >= 60 else '%.1f с' % secs


def render_candidates(items, out, total, sources):
    w = out.write
    w('# Запросы в логах — %d id (сначала те, где есть ошибки)\n\n' % len(items))
    if not items:
        w('Ни в одной записи не нашёлся correlation/request id.\n\n'
          'Проверь, что логи содержат `trace_id`, `request_id`, `correlationId` '
          'или `rqUID`. Без сквозного id цепочку между сервисами не собрать — '
          'разбирай по времени через `timeline.py`.\n')
        return
    w('Источники: %s · всего запросов с id: %d · времена в %s\n\n'
      % (', '.join(sources), total, TIME_SCALE))
    w('| id | ошибок | записей | сервисы | длительность |\n')
    w('|---|---|---|---|---|\n')
    for it in items:
        span = ''
        if it['first'] and it['last']:
            span = fmt_gap(it['last'] - it['first'])
        w('| `%s` | %d | %d | %s | %s |\n' % (
            it['id'][:48], it['errors'], it['records'],
            ', '.join(sorted(it['services'])), span))
    w('\nЦепочка одного запроса: `--id %s`\n' % items[0]['id'])


def render_chain(trace_id, hops, matched, out, args):
    w = out.write
    if not hops:
        w('Запрос `%s` не найден ни в одном источнике.\n' % trace_id)
        return
    first_error, slowest = chain_findings(hops)
    start = hops[0].first
    end = max([h.last for h in hops if h.last] or [None])

    w('# Цепочка `%s`\n\n' % trace_id)
    w('записей: %d · сервисов: %d' % (len(matched), len(hops)))
    if start and end:
        w(' · %s → %s (%s)' % (pl.fmt_ts(start), pl.fmt_ts(end), fmt_gap(end - start)))
    w('\n\nВремена хопов и паузы между ними — в %s.' % TIME_SCALE)
    applied = sorted((label, secs) for label, secs in args.offsets.items() if secs)
    if applied:
        w(' Применена заданная поправка: %s — время сдвинуто, в логе его не было.'
          % ', '.join('%s %+.2f с' % (label, secs) for label, secs in applied))
    w('\n\n')
    if mixed_zones(matched):
        w(MIXED_ZONES_NOTE + '\n')

    w('| # | сервис | первая запись | внутри | пауза от предыдущего | уровень | ошибок | статусы |\n')
    w('|---|---|---|---|---|---|---|---|\n')
    prev = None
    for i, hop in enumerate(hops, 1):
        inside = fmt_gap(hop.last - hop.first) if hop.first and hop.last else '—'
        gap = '—'
        if prev is not None and prev.first and hop.first:
            delta = hop.first - prev.first
            gap = fmt_gap(delta)
            if delta.total_seconds() < 0:
                gap += ' ⚠ назад'
        statuses = ', '.join('%d×%d' % (cnt, code)
                             for code, cnt in hop.statuses.most_common(3)) or '—'
        w('| %d | **%s** | %s | %s | %s | %s | %d | %s |\n' % (
            i, hop.service, pl.fmt_ts(hop.first) if hop.first else '?',
            inside, gap, hop.worst_level or '—', hop.errors, statuses))
        prev = hop

    w('\n')
    if first_error:
        rec = first_error['rec']
        w('**Первая ошибка — `%s`, %s**\n\n' % (first_error['service'], pl.fmt_ts(rec.ts)))
        w('```\n%s\n```\n\n' % rec.raw_text[:800])
        w('Раньше неё в цепочке ошибок нет: причину ищи в `%s` и в том, что он звал '
          'дальше. Ошибки соседних сервисов после этого момента — обычно уже '
          'реакция на неё (таймаут, 5xx), а не отдельная поломка.\n\n'
          % first_error['service'])
    else:
        w('Ошибок в цепочке нет: запрос прошёл целиком. '
          'Если пользователь всё же видел сбой — id не тот либо логи неполные.\n\n')

    if slowest and slowest['gap'] >= timedelta(seconds=1):
        w('**Самая долгая пауза:** %s → %s, %s.\n\n'
          % (slowest['from'], slowest['to'], fmt_gap(slowest['gap'])))

    backward = [i for i in range(1, len(hops))
                if hops[i].first and hops[i - 1].first
                and hops[i].first < hops[i - 1].first]
    if backward:
        w('⚠ Часть хопов идёт назад во времени — вероятен рассинхрон часов. '
          'Проверь: `--check-clocks`, поправь: `--offset сервис=секунды`.\n\n')

    limit = args.records
    if limit:
        rows = sorted(matched, key=lambda p: sort_key(p[1].ts, p[0], p[1].line_no))[:limit]

        def render_row(pair, write):
            label, rec = pair
            write('%s  %-5s  **%s**  _%s:%d_\n' % (pl.fmt_ts(rec.ts), rec.level or '-',
                                                    label, rec.origin, rec.line_no))
            write('```\n%s\n```\n\n' % rec.raw_text[:1200])

        # без бюджета `--records 500` уводил в контекст сотни килобайт сырых
        # записей; полный список остаётся доступен через файл, если что-то не
        # поместилось
        shown, hidden = fit_by_render(rows, render_row, args.max_chars, reserve=260)
        w('## Записи (%d из %d)\n\n' % (len(shown), len(matched)))
        for pair in shown:
            render_row(pair, w)
        if hidden:
            full = dump_overflow([{
                'ts': pl.fmt_ts(rec.ts), 'level': rec.level, 'service': label,
                'origin': rec.origin, 'line_no': rec.line_no, 'text': rec.raw_text,
            } for label, rec in rows], 'trace-records.json')
            note = (' Полный список: `%s`.' % full) if full else ''
            w('_Не показано записей: %d — вывод ограничен по объёму.%s_\n\n' % (hidden, note))


def render_clocks(findings, traces, out, mixed=False):
    w = out.write
    w('# Проверка часов между сервисами\n\n')
    if mixed:
        w(MIXED_ZONES_NOTE + '\n')
    render_clock_findings(findings, traces, w, unit='общих запросов')


def chain_to_json(trace_id, hops, matched, offsets=None):
    first_error, slowest = chain_findings(hops)
    return {
        'id': trace_id,
        'records': len(matched),
        'time_scale': TIME_SCALE,
        'mixed_timezones': mixed_zones(matched),
        'offsets_applied': {k: v for k, v in (offsets or {}).items() if v},
        'hops': [{
            'service': h.service,
            'first': pl.fmt_ts(h.first) if h.first else None,
            'last': pl.fmt_ts(h.last) if h.last else None,
            'errors': h.errors,
            'level': h.worst_level,
            'statuses': {str(k): v for k, v in h.statuses.items()},
            'count': len(h.records),
        } for h in hops],
        'first_error': ({
            'service': first_error['service'],
            'ts': pl.fmt_ts(first_error['rec'].ts),
            'level': first_error['rec'].level,
            'text': first_error['rec'].raw_text[:800],
        } if first_error else None),
        'slowest_gap': ({
            'from': slowest['from'], 'to': slowest['to'],
            'seconds': round(slowest['gap'].total_seconds(), 3),
        } if slowest else None),
    }


# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Сквозная цепочка запроса по correlation id через несколько сервисов.')
    ap.add_argument('--log', action='append', default=[], required=True,
                    help='источник логов: "сервис=путь" (можно повторять)')
    ap.add_argument('--id', help='correlation/request/trace id — построить цепочку')
    ap.add_argument('--top', type=int, nargs='?', const=10, default=None,
                    help='показать запросы с наибольшим числом ошибок (по умолчанию 10)')
    ap.add_argument('--check-clocks', action='store_true',
                    help='оценить расхождение часов между сервисами')
    ap.add_argument('--offset', action='append', default=[],
                    help='сдвиг часов сервиса в секундах: "payment=-2.5"')
    ap.add_argument('--records', type=int, default=0,
                    help='сколько сырых записей цепочки показать (0 — не показывать)')
    ap.add_argument('--since', help='начало окна')
    ap.add_argument('--until', help='конец окна')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    ap.add_argument('--max-chars', type=int, default=MAX_SUMMARY_CHARS,
                    help='предельный объём раздела "Записи" в символах (%d), '
                         '0 — без предела' % MAX_SUMMARY_CHARS)
    ap.add_argument('--encoding', default='utf-8')
    ap.add_argument('--max-lines', type=int, default=DEFAULT_MAX_LINES)
    args = ap.parse_args(argv)

    args.since = parse_time_arg(args.since) if args.since else None
    args.until = parse_time_arg(args.until) if args.until else None
    args.offsets = dict(parse_offset_arg(item) for item in args.offset)

    sources = [parse_source_arg(item) for item in args.log]
    unknown = set(args.offsets) - {label for label, _ in sources}
    if unknown:
        raise SystemExit('--offset для неизвестного сервиса: %s' % ', '.join(sorted(unknown)))

    pairs = []
    for label, path in sources:
        pairs.extend(read_service(label, path, args))
    if not pairs:
        raise SystemExit('Записей не найдено — проверь пути и окно (--since/--until)')

    out = sys.stdout

    if args.check_clocks:
        findings, traces = check_clocks(pairs)
        mixed = mixed_zones(pairs)
        if args.format == 'json':
            dump_json({'traces': traces, 'clock_findings': findings,
                      'time_scale': TIME_SCALE, 'mixed_timezones': mixed}, out)
        else:
            render_clocks(findings, traces, out, mixed)
        return 0

    if args.id:
        hops, matched = build_chain(pairs, args.id)
        if args.format == 'json':
            dump_json(chain_to_json(args.id, hops, matched, args.offsets), out)
        else:
            render_chain(args.id, hops, matched, out, args)
        return 0 if hops else 1

    stats = collect_candidates(pairs)
    items = rank_candidates(stats, args.top or 10)
    if args.format == 'json':
        dump_json({
            'total_traces': len(stats),
            'time_scale': TIME_SCALE,
            'mixed_timezones': mixed_zones(pairs),
            'candidates': [{
                'id': it['id'], 'records': it['records'], 'errors': it['errors'],
                'services': sorted(it['services']),
                'first': pl.fmt_ts(it['first']) if it['first'] else None,
                'last': pl.fmt_ts(it['last']) if it['last'] else None,
            } for it in items],
        }, out)
    else:
        render_candidates(items, out, len(stats), [label for label, _ in sources])
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
