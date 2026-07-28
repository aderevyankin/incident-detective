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
import json
import os
import sys
from collections import Counter, OrderedDict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parse_logs as pl  # noqa: E402

# ниже этого не считаем расхождение часов подозрительным: сетевые задержки
# и разное время записи в лог дают до секунды сами по себе
CLOCK_MIN_SHIFT = 2.0
# сколько общих запросов нужно, чтобы вывод о часах вообще имел смысл
CLOCK_MIN_TRACES = 3


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


def parse_offset_arg(item):
    label, sep, val = item.partition('=')
    if not sep:
        raise SystemExit('Сдвиг задаётся как "сервис=секунды": %r' % item)
    try:
        return label, float(val)
    except ValueError:
        raise SystemExit('Не разобрал секунды в %r' % item)


def read_service(label, path, args):
    """Читает один источник, возвращает записи с меткой сервиса."""
    sources = pl.expand_sources([path])
    if not sources:
        print('warning: источник не найден: %s' % path, file=sys.stderr)
        return []
    offset = args.offsets.get(label, 0.0)
    shift = timedelta(seconds=offset) if offset else None
    out = []
    for rec in pl.read_records(sources, args.encoding, args.max_lines):
        if shift and rec.ts:
            rec.ts = rec.ts + shift
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
    items.sort(key=lambda it: (-it['errors'], -len(it['services']), -it['records']))
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
    ordered = sorted(hops.values(), key=lambda h: (h.first or datetime.max))
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
                errors.append((rec.ts or datetime.max, rec.line_no, hop.service, rec))
    errors.sort(key=lambda e: (e[0], e[1]))
    first_error = {'service': errors[0][2], 'rec': errors[0][3]} if errors else None

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


def median(values):
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def check_clocks(pairs):
    """Оценивает расхождение часов между сервисами по общим запросам.

    Для пары сервисов берём разницу первых отметок одного и того же запроса.
    Если разница по всем запросам держится около одного значения (разброс мал),
    это похоже на систематический сдвиг часов, а не на живую задержку: реальная
    задержка гуляет от запроса к запросу.
    """
    per_trace = {}
    for label, rec in pairs:
        key = trace_key(rec)
        if not key or not rec.ts:
            continue
        firsts = per_trace.setdefault(key, {})
        if label not in firsts or rec.ts < firsts[label]:
            firsts[label] = rec.ts

    deltas = {}
    for firsts in per_trace.values():
        labels = sorted(firsts)
        for i, a in enumerate(labels):
            for b in labels[i + 1:]:
                deltas.setdefault((a, b), []).append((firsts[b] - firsts[a]).total_seconds())

    findings = []
    for (a, b), vals in sorted(deltas.items()):
        if len(vals) < CLOCK_MIN_TRACES:
            continue
        med = median(vals)
        spread = median([abs(v - med) for v in vals])
        if abs(med) < CLOCK_MIN_SHIFT:
            continue
        # разброс мал относительно самого сдвига — значит он постоянный
        systematic = spread <= max(0.25 * abs(med), 0.5)
        findings.append({
            'from': a, 'to': b, 'median': round(med, 2), 'spread': round(spread, 2),
            'traces': len(vals), 'systematic': systematic,
        })
    return findings, len(per_trace)


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
    w('Источники: %s · всего запросов с id: %d\n\n' % (', '.join(sources), total))
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
    w('\n\n')

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
        w('## Записи (%d из %d)\n\n' % (min(limit, len(matched)), len(matched)))
        rows = sorted(matched, key=lambda p: (p[1].ts or datetime.max, p[1].line_no))
        for label, rec in rows[:limit]:
            w('%s  %-5s  **%s**  _%s:%d_\n' % (pl.fmt_ts(rec.ts), rec.level or '-',
                                               label, rec.origin, rec.line_no))
            w('```\n%s\n```\n\n' % rec.raw_text[:1200])


def render_clocks(findings, traces, out):
    w = out.write
    w('# Проверка часов между сервисами\n\n')
    if traces < CLOCK_MIN_TRACES:
        w('Общих запросов слишком мало (%d) — сравнивать нечего.\n' % traces)
        return
    if not findings:
        w('Расхождений больше %.0f с не видно (%d общих запросов). '
          'Время сервисов можно считать сопоставимым.\n' % (CLOCK_MIN_SHIFT, traces))
        return
    w('Общих запросов: %d\n\n' % traces)
    for f in findings:
        kind = ('похоже на сдвиг часов' if f['systematic']
                else 'разброс большой — скорее реальная задержка, чем часы')
        w('- **%s → %s**: медиана %+.2f с, разброс ±%.2f с по %d запросам — %s\n'
          % (f['from'], f['to'], f['median'], f['spread'], f['traces'], kind))
    systematic = [f for f in findings if f['systematic']]
    if systematic:
        f = systematic[0]
        w('\nПоправка применяется вручную, чтобы в цепочке не появилось '
          'подогнанное время:\n\n```\n--offset %s=%.2f\n```\n' % (f['to'], -f['median']))
    w('\nОценка косвенная: сдвиг часов и стабильно одинаковая задержка выглядят '
      'одинаково. Сверься с ntp/системным временем стендов, прежде чем опираться '
      'на неё в выводе.\n')


def chain_to_json(trace_id, hops, matched):
    first_error, slowest = chain_findings(hops)
    return {
        'id': trace_id,
        'records': len(matched),
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
    ap.add_argument('--encoding', default='utf-8')
    ap.add_argument('--max-lines', type=int, default=2000000)
    args = ap.parse_args(argv)

    args.since = pl.parse_time_arg(args.since) if args.since else None
    args.until = pl.parse_time_arg(args.until) if args.until else None
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
        if args.format == 'json':
            json.dump({'traces': traces, 'clock_findings': findings}, out,
                      ensure_ascii=False, indent=2)
            out.write('\n')
        else:
            render_clocks(findings, traces, out)
        return 0

    if args.id:
        hops, matched = build_chain(pairs, args.id)
        if args.format == 'json':
            json.dump(chain_to_json(args.id, hops, matched), out,
                      ensure_ascii=False, indent=2)
            out.write('\n')
        else:
            render_chain(args.id, hops, matched, out, args)
        return 0 if hops else 1

    stats = collect_candidates(pairs)
    items = rank_candidates(stats, args.top or 10)
    if args.format == 'json':
        json.dump({
            'total_traces': len(stats),
            'candidates': [{
                'id': it['id'], 'records': it['records'], 'errors': it['errors'],
                'services': sorted(it['services']),
                'first': pl.fmt_ts(it['first']) if it['first'] else None,
                'last': pl.fmt_ts(it['last']) if it['last'] else None,
            } for it in items],
        }, out, ensure_ascii=False, indent=2)
        out.write('\n')
    else:
        render_candidates(items, out, len(stats), [label for label, _ in sources])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BrokenPipeError:
        pass
    except KeyboardInterrupt:
        sys.exit(130)
