#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Парсер разнородных неструктурированных логов.

Превращает произвольную кучу строк в сжатую сводку: шаблоны сообщений с
частотами, временное окно, гистограмму, exception-классы, HTTP-статусы и
набор сигнатур для поиска по базе знаний.

Только stdlib, Python 3.8+.
"""

import argparse
import bz2
import gzip
import io
import json
import lzma
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

from kb_common import (  # noqa: E402
    DEFAULT_MAX_LINES, EXC_RE, LEVELS, LEVEL_ORD, MAX_SUMMARY_CHARS, TIME_SCALE,
    dump_json, now, run_script,
)
from kb_common import parse_time_arg as _kb_parse_time_arg  # noqa: E402

MAX_LINE = 8192
LOG_EXTS = ('.log', '.txt', '.json', '.jsonl', '.ndjson', '.out', '.err')
ARCHIVE_EXTS = ('.gz', '.bz2', '.xz', '.lzma', '.zip')

# Хвост ротации: `app.log.1`, `messages.0`. Логи ротируются везде, и без этого
# правила обход каталога брал только сегодняшний файл — то есть ровно не тот,
# в котором инцидент, если разбор начался наутро.
ROTATED_RE = re.compile(r'\.\d+$')

LEVEL_ALIASES = {
    'TRACE': 'TRACE', 'TRC': 'TRACE', 'FINEST': 'TRACE', 'VERBOSE': 'TRACE',
    'DEBUG': 'DEBUG', 'DBG': 'DEBUG', 'FINE': 'DEBUG', 'D': 'DEBUG',
    'INFO': 'INFO', 'INF': 'INFO', 'INFORMATION': 'INFO', 'I': 'INFO',
    'NOTICE': 'NOTICE',
    'WARN': 'WARN', 'WARNING': 'WARN', 'WRN': 'WARN', 'W': 'WARN',
    'ERROR': 'ERROR', 'ERR': 'ERROR', 'ERRO': 'ERROR', 'SEVERE': 'ERROR', 'E': 'ERROR',
    'FATAL': 'FATAL', 'CRIT': 'FATAL', 'CRITICAL': 'FATAL', 'PANIC': 'FATAL',
    'EMERG': 'FATAL', 'ALERT': 'FATAL',
}

# Числовые уровни python logging (DEBUG..CRITICAL). С syslog-severity 0..7 не
# пересекаются, поэтому обе шкалы уживаются в одной проверке.
PY_LOG_LEVELS = {10: 'DEBUG', 20: 'INFO', 30: 'WARN', 40: 'ERROR', 50: 'FATAL'}

LEVEL_RE = re.compile(
    r'(?<![A-Za-z])('
    r'TRACE|DEBUG|VERBOSE|INFO|NOTICE|WARNING|WARN|ERROR|ERRO|SEVERE|FATAL|'
    r'CRITICAL|CRIT|PANIC|EMERG|ALERT|ERR|DBG|INF|WRN'
    r')(?![A-Za-z])', re.IGNORECASE)

MONTHS = {m: i + 1 for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}

# --------------------------------------------------------------------------
# Таймстемпы
# --------------------------------------------------------------------------


# Внутренняя шкала одна — TIME_SCALE из kb_common. Время с явно указанным
# смещением приводится к ней при разборе; время без указания зоны остаётся как
# записано и помечается признаком «зона неизвестна». Подставлять зону неоткуда: и
# зона машины разбора, и зона соседних записей — домысел, который сдвинет события
# молча.

# Годы, подставленные записям syslog (в самой записи года нет). Собираются при
# разборе и называются в сводке как допущение: подставленный год виден в выводе,
# а не остаётся решением, о котором читатель не знает. Множество, а не счётчик:
# поиск таймстемпа по одной строке делается несколько раз, и счётчик врал бы.
YEAR_ASSUMED = set()

# Суффикс зоны: `Z`, `+03:00`, `+0300`, `-05`. Пробел перед суффиксом допускается
# только для форм, которые ни с чем не спутать (access-лог пишет `... +0300]`).
# Голое `±HH` принимается лишь вплотную к времени: отделённое пробелом `-15` в
# тексте сообщения — обычное число, а не смещение зоны.
TZ_RE = r'(\s?Z|\s?[+-]\d{2}:\d{2}|\s?[+-]\d{4}|[+-]\d{2}(?![\d:]))?'


def _frac(s):
    if not s:
        return 0
    s = (s + '000000')[:6]
    return int(s)


def _tzinfo(raw):
    """Смещение из суффикса таймстемпа. None — зона в записи не указана."""
    if not raw:
        return None
    text = raw.strip()
    if text in ('Z', 'z'):
        return timezone.utc
    sign = -1 if text[0] == '-' else 1
    body = text[1:].replace(':', '')
    if not body.isdigit():
        return None
    hours = int(body[:2])
    minutes = int(body[2:4]) if len(body) >= 4 else 0
    if hours > 23 or minutes > 59:
        return None
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _ts_iso(m):
    g = m.groups()
    return datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]),
                    int(g[5]), _frac(g[6]), _tzinfo(g[7]))


def _ts_clf(m):
    # 28/Jul/2026:12:33:44 +0300
    g = m.groups()
    mon = MONTHS.get(g[1][:3].lower())
    if not mon:
        return None
    return datetime(int(g[2]), mon, int(g[0]), int(g[3]), int(g[4]), int(g[5]),
                    tzinfo=_tzinfo(g[6]))


def _ts_dmy(m):
    g = m.groups()
    return datetime(int(g[2]), int(g[1]), int(g[0]), int(g[3]), int(g[4]), int(g[5]),
                    tzinfo=_tzinfo(g[6]))


def _syslog_year(month, day):
    """Год для записи без года: такой, чтобы событие не оказалось в будущем.

    Месяц и день позже сегодняшних означают, что запись — из прошлого года:
    декабрьский инцидент, разбираемый в январе, иначе уезжает на одиннадцать
    месяцев вперёд, и хронология рассыпается ровно там, где её труднее всего
    перепроверить. Правило ошибается только на логах давностью больше года — там
    и подстановка текущего года бесполезна.
    """
    ref = now()
    year = ref.year
    if (month, day) > (ref.month, ref.day):
        year -= 1
    return year


def _ts_syslog(m):
    # Jul 28 12:33:44 — года в записи нет, зоны тоже
    g = m.groups()
    mon = MONTHS.get(g[0][:3].lower())
    if not mon:
        return None
    day = int(g[1])
    year = _syslog_year(mon, day)
    try:
        dt = datetime(year, mon, day, int(g[2]), int(g[3]), int(g[4]))
    except ValueError:
        # 29 февраля, а подставленный год невисокосный. Подставлять год «сейчас»
        # бессмысленно — он невисокосный ровно так же, и время терялось совсем.
        # Отступаем назад до ближайшего года, в котором такой день был: это
        # заведомо прошлое, то есть та же гарантия «событие не в будущем».
        dt = None
        for back in range(1, 9):
            try:
                dt = datetime(year - back, mon, day, int(g[2]), int(g[3]), int(g[4]))
            except ValueError:
                continue
            year -= back
            break
        if dt is None:
            return None
    YEAR_ASSUMED.add(year)
    return dt


def _ts_epoch(m):
    # секунды эпохи отсчитываются от UTC по определению — зона тут известна
    val = float(m.group(1))
    if val > 1e12:          # миллисекунды
        val /= 1000.0
    try:
        return datetime.fromtimestamp(val, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


TS_SPECS = [
    (re.compile(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?'
                + TZ_RE), _ts_iso),
    (re.compile(r'(\d{4})/(\d{2})/(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?'
                + TZ_RE), _ts_iso),
    (re.compile(r'(\d{1,2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})' + TZ_RE), _ts_clf),
    (re.compile(r'(\d{1,2})\.(\d{2})\.(\d{4})[T ](\d{2}):(\d{2}):(\d{2})' + TZ_RE), _ts_dmy),
    (re.compile(r'([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})'), _ts_syslog),
    (re.compile(r'^\W{0,2}(\d{10}(?:\.\d{1,6})?)\b'), _ts_epoch),
]


def to_scale(dt):
    """(время на шкале UTC, известна ли зона).

    Время с известным смещением приводится к UTC и дальше живёт наивным: смешение
    aware и naive в одном сравнении бросает TypeError в самом неожиданном месте —
    например при вычитании пауз между хопами. Признак зоны едет отдельным полем и
    доходит до вывода, поэтому допущение сделано в одном месте и названо.
    """
    if dt is None:
        return None, False
    if dt.tzinfo is None:
        return dt, False
    return dt.astimezone(timezone.utc).replace(tzinfo=None), True


def find_timestamp_full(text, limit=64):
    """(datetime|None, end_pos, start_pos, зона известна) для первого таймстемпа."""
    head = text[:limit]
    best = None
    for rx, fn in TS_SPECS:
        m = rx.search(head)
        if not m:
            continue
        if best is not None and m.start() >= best[2]:
            continue
        try:
            dt = fn(m)
        except (ValueError, TypeError):
            dt = None
        if dt is not None:
            dt, tz_known = to_scale(dt)
            best = (dt, m.end(), m.start(), tz_known)
            # раньше нулевой позиции совпадения быть не может, а таймстемп в
            # начале строки — обычный случай: остальные шаблоны проверять незачем
            if m.start() == 0:
                break
    if best:
        return best
    return None, 0, 0, False


def find_timestamp(text, limit=64):
    """Возвращает (datetime|None, end_pos) для первого таймстемпа в начале строки."""
    dt, end, _start, _tz = find_timestamp_full(text, limit)
    return dt, end


def find_timestamp_tz(text, limit=64):
    """То же, но с признаком того, была ли зона указана в самой записи."""
    dt, end, _start, tz_known = find_timestamp_full(text, limit)
    return dt, end, tz_known


# Реэкспорт: канонический разбор `--since/--until` живёт в kb_common (его
# нужен find_timestamp — этот модуль), внутренние вызовы parse_logs используют
# короткое имя, как и раньше.
parse_time_arg = _kb_parse_time_arg


# --------------------------------------------------------------------------
# Чтение источников
# --------------------------------------------------------------------------


def _open_text(path, encoding):
    if path.endswith('.gz'):
        return io.TextIOWrapper(gzip.open(path, 'rb'), encoding=encoding, errors='replace')
    if path.endswith('.bz2'):
        return io.TextIOWrapper(bz2.open(path, 'rb'), encoding=encoding, errors='replace')
    if path.endswith('.xz') or path.endswith('.lzma'):
        return io.TextIOWrapper(lzma.open(path, 'rb'), encoding=encoding, errors='replace')
    return open(path, 'r', encoding=encoding, errors='replace')


def looks_like_log(fname):
    """Брать ли файл при обходе каталога.

    Явно указанный путь берётся всегда — правило нужно только для каталога, где
    рядом с логами лежит что угодно. Ротация распознаётся по числовому хвосту:
    `app.log.1` — тот же лог, `messages.0` — тоже (у syslog-файлов расширения
    нет вовсе, поэтому имя без точек засчитывается, только если хвост ротации был).
    """
    if fname.endswith(LOG_EXTS) or fname.endswith(ARCHIVE_EXTS):
        return True
    base = ROTATED_RE.sub('', fname)
    if base == fname:
        return False
    return base.endswith(LOG_EXTS) or base.endswith(ARCHIVE_EXTS) or '.' not in base


def expand_sources(patterns):
    """Разворачивает пути, glob-маски и директории в список файлов."""
    import glob as globmod
    out = []
    for pat in patterns:
        if pat == '-':
            out.append('-')
            continue
        matches = globmod.glob(pat, recursive=True) or ([pat] if os.path.exists(pat) else [])
        if not matches:
            print('warning: ничего не найдено по %r' % pat, file=sys.stderr)
        for path in sorted(matches):
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for fname in sorted(files):
                        if looks_like_log(fname):
                            out.append(os.path.join(root, fname))
            else:
                out.append(path)
    return out


def iter_lines(sources, encoding, max_lines):
    """Отдаёт (origin, line_no, text) по всем источникам."""
    count = 0
    for path in sources:
        if count >= max_lines:
            return
        try:
            if path == '-':
                stream = io.TextIOWrapper(sys.stdin.buffer, encoding=encoding, errors='replace')
                for i, line in enumerate(stream, 1):
                    yield '<stdin>', i, line.rstrip('\n\r')[:MAX_LINE]
                    count += 1
                    if count >= max_lines:
                        return
            elif path.endswith('.zip'):
                with zipfile.ZipFile(path) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        origin = '%s!%s' % (os.path.basename(path), info.filename)
                        with zf.open(info) as raw:
                            stream = io.TextIOWrapper(raw, encoding=encoding, errors='replace')
                            for i, line in enumerate(stream, 1):
                                yield origin, i, line.rstrip('\n\r')[:MAX_LINE]
                                count += 1
                                if count >= max_lines:
                                    return
            else:
                with _open_text(path, encoding) as fh:
                    origin = os.path.basename(path)
                    for i, line in enumerate(fh, 1):
                        yield origin, i, line.rstrip('\n\r')[:MAX_LINE]
                        count += 1
                        if count >= max_lines:
                            return
        # lzma.LZMAError — не наследник OSError, в отличие от ошибок gzip и bz2:
        # без него битый .xz валил разбор трассировкой вместо предупреждения
        except (OSError, lzma.LZMAError, zipfile.BadZipFile, EOFError) as exc:
            print('warning: не прочитал %s: %s' % (path, exc), file=sys.stderr)


# --------------------------------------------------------------------------
# Разбор записей
# --------------------------------------------------------------------------

JSON_TS_KEYS = ('timestamp', 'time', '@timestamp', 'ts', 'date', 'datetime',
                'eventTime', 'event_time', 'asctime')
JSON_LEVEL_KEYS = ('level', 'severity', 'lvl', 'loglevel', 'log_level',
                   'levelname', 'levelName', 'status')
JSON_MSG_KEYS = ('message', 'msg', 'log', 'short_message', 'event', 'text',
                 'error', 'err', 'exception', 'body')
JSON_SRC_KEYS = ('logger', 'logger_name', 'loggerName', 'source', 'caller',
                 'component', 'service', 'app', 'application', 'container',
                 'kubernetes.container_name', 'name')
TRACE_KEYS = ('trace_id', 'traceId', 'traceID', 'request_id', 'requestId',
              'requestID', 'correlation_id', 'correlationId', 'x-request-id',
              'rquid', 'rqUID', 'span_id', 'spanId', 'req_id')

LOGFMT_RE = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s]*)')
NGINX_RE = re.compile(
    r'^(\S+)\s+\S+\s+(\S+)\s+\[([^\]]+)\]\s+"([A-Z]+)\s+(\S+)[^"]*"\s+(\d{3})\s+(\d+|-)')
TRACE_TEXT_RE = re.compile(
    r'(?:trace[_-]?id|request[_-]?id|correlation[_-]?id|rq[_-]?uid|x-request-id)'
    r'\s*[=:]\s*["\']?([\w\-]{6,})', re.IGNORECASE)
PY_EXC_RE = re.compile(r'^([A-Z][\w.]*(?:Error|Exception|Warning)):', re.MULTILINE)


def _flat_get(obj, keys):
    for key in keys:
        if key in obj and obj[key] not in (None, ''):
            return obj[key]
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val not in (None, ''):
            return val
    return None


def norm_level(raw):
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in LEVEL_ALIASES:
        return LEVEL_ALIASES[text]
    if text.isdigit():                       # syslog severity / http status
        num = int(text)
        # уровни python logging: `logging.ERROR` в JSON-логе приезжает числом 40,
        # и без этого соответствия запись проваливалась в INFO — то есть ошибка
        # переставала быть ошибкой ровно там, где её ищут
        if num in PY_LOG_LEVELS:
            return PY_LOG_LEVELS[num]
        if 100 <= num <= 599:
            return 'ERROR' if num >= 500 else ('WARN' if num >= 400 else 'INFO')
        return {0: 'FATAL', 1: 'FATAL', 2: 'FATAL', 3: 'ERROR',
                4: 'WARN', 5: 'NOTICE', 6: 'INFO', 7: 'DEBUG'}.get(num)
    m = LEVEL_RE.search(text)
    if m:
        return LEVEL_ALIASES.get(m.group(1).upper())
    return None


def _stringify(val):
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)[:MAX_LINE]
    return str(val)


class Record(object):
    __slots__ = ('ts', 'tz_known', 'level', 'message', 'source', 'origin', 'line_no',
                 'fmt', 'trace', 'status', 'raw', 'ts_hint', '_text')

    def __init__(self, origin, line_no, raw, ts_hint=None):
        self.origin = origin
        self.line_no = line_no
        self.raw = [raw]
        self._text = raw
        # (datetime, end_pos, зона известна) от проверки «начало ли это записи»:
        # тот же поиск таймстемпа, что нужен разбору, — второй раз его делать незачем
        self.ts_hint = ts_hint
        self.ts = None
        self.tz_known = False
        self.level = None
        self.message = raw
        self.source = None
        self.fmt = 'plain'
        self.trace = None
        self.status = None

    @property
    def raw_text(self):
        # к сырому тексту обращаются несколько раз за запись — при разборе, при
        # поиске классов исключений, при сохранении примера; склеивать список
        # заново каждый раз незачем, а однострочной записи склейка не нужна вовсе
        if self._text is None:
            self._text = self.raw[0] if len(self.raw) == 1 else '\n'.join(self.raw)
        return self._text


def parse_json_line(rec, text):
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(obj, dict):
        return False
    rec.fmt = 'json'
    raw_ts = _flat_get(obj, JSON_TS_KEYS)
    if raw_ts is not None:
        if isinstance(raw_ts, (int, float)):
            val = float(raw_ts)
            if val > 1e12:
                val /= 1000.0
            try:
                rec.ts, rec.tz_known = to_scale(datetime.fromtimestamp(val, timezone.utc))
            except (ValueError, OSError, OverflowError):
                rec.ts = None
        else:
            text = str(raw_ts)
            rec.ts, _, rec.tz_known = find_timestamp_tz(text, limit=len(text) + 1)
    rec.level = norm_level(_flat_get(obj, JSON_LEVEL_KEYS))
    msg = _flat_get(obj, JSON_MSG_KEYS)
    src = _flat_get(obj, JSON_SRC_KEYS)
    rec.source = _stringify(src)[:60] if src else None
    trace = _flat_get(obj, TRACE_KEYS)
    rec.trace = _stringify(trace)[:64] if trace else None
    status = _flat_get(obj, ('status_code', 'statusCode', 'http_status', 'response_code'))
    if isinstance(status, (int, str)) and str(status).isdigit() and 100 <= int(status) <= 599:
        rec.status = int(status)
    if msg is None:
        # нет явного поля сообщения — собираем из остатка полей
        skip = set(JSON_TS_KEYS + JSON_LEVEL_KEYS + JSON_SRC_KEYS)
        parts = ['%s=%s' % (k, _stringify(v)) for k, v in obj.items()
                 if k not in skip and v not in (None, '')]
        msg = ' '.join(parts)
    else:
        msg = _stringify(msg)
        extra = obj.get('stack_trace') or obj.get('stacktrace') or obj.get('exception')
        if extra and _stringify(extra) not in msg:
            msg = msg + ' | ' + _stringify(extra)[:2000]
    rec.message = msg
    if rec.level is None:
        rec.level = norm_level(msg[:200])
    return True


def parse_nginx_line(rec, text):
    m = NGINX_RE.match(text)
    if not m:
        return False
    rec.fmt = 'access'
    rec.ts, _, rec.tz_known = find_timestamp_tz(m.group(3), limit=len(m.group(3)) + 1)
    rec.status = int(m.group(6))
    rec.level = 'ERROR' if rec.status >= 500 else ('WARN' if rec.status >= 400 else 'INFO')
    rec.source = 'access'
    # класс статуса вместо точного кода: 500 и 503 попадут в одну группу,
    # точные коды остаются в разделе HTTP-статусов
    rec.message = '%s %s -> HTTP %dxx' % (m.group(4), m.group(5), rec.status // 100)
    return True


def parse_logfmt_line(rec, text):
    pairs = LOGFMT_RE.findall(text)
    if len(pairs) < 2:
        return False
    covered = sum(len(k) + len(v) + 1 for k, v in pairs)
    keys = {k.lower() for k, _ in pairs}
    strong = bool(keys & {'level', 'msg', 'message', 'severity', 'lvl', 'time', 'ts'})
    if not strong and covered < len(text) * 0.5:
        return False
    obj = {}
    for key, val in pairs:
        if len(val) >= 2 and val[0] in '"\'' and val[-1] == val[0]:
            val = val[1:-1]
        obj[key] = val
    rec.fmt = 'logfmt'
    raw_ts = _flat_get(obj, JSON_TS_KEYS)
    if raw_ts:
        val = str(raw_ts)
        rec.ts, _, rec.tz_known = find_timestamp_tz(val, limit=len(val) + 1)
    if rec.ts is None:
        rec.ts, _, rec.tz_known = find_timestamp_tz(text)
    rec.level = norm_level(_flat_get(obj, JSON_LEVEL_KEYS))
    src = _flat_get(obj, JSON_SRC_KEYS)
    rec.source = str(src)[:60] if src else None
    trace = _flat_get(obj, TRACE_KEYS)
    rec.trace = str(trace)[:64] if trace else None
    msg = _flat_get(obj, JSON_MSG_KEYS)
    skip = set(JSON_TS_KEYS + JSON_LEVEL_KEYS + JSON_MSG_KEYS)
    extras = ['%s=%s' % (k, v) for k, v in obj.items() if k not in skip]
    rec.message = ((str(msg) + ' ') if msg else '') + ' '.join(extras)
    return True


BRACKET_SRC_RE = re.compile(r'\[([\w.\-]{2,60})\]')


def parse_plain_line(rec, text):
    if rec.ts_hint is not None and rec.raw[0] == text:
        ts, end, tz_known = rec.ts_hint
    else:
        ts, end, tz_known = find_timestamp_tz(text)
    rec.ts = ts
    rec.tz_known = tz_known
    rest = text[end:] if ts else text
    m = LEVEL_RE.search(rest[:120])
    if m:
        rec.level = LEVEL_ALIASES.get(m.group(1).upper())
        rest = rest[:m.start()] + rest[m.end():]
    # источник вида [payment-api] — вырезаем его из сообщения, чтобы не мусорил в шаблоне
    src = BRACKET_SRC_RE.search(rest[:120])
    if src and not src.group(1).isdigit():
        rec.source = src.group(1)
        rest = rest[:src.start()] + ' ' + rest[src.end():]
    rec.message = rest.strip(' \t|:-[]')
    return True


def classify_line(text):
    """Возвращает (начало ли записи, ts_hint).

    Таймстемп ищется здесь не более одного раза и передаётся дальше в `Record`:
    разбору он нужен снова, а на больших логах повторный поиск — заметная доля
    всей работы. Дешёвые признаки проверяются раньше поиска, чтобы строки
    продолжения стектрейса не оплачивали его вовсе.
    """
    stripped = text.strip()
    if not stripped:
        return False, None
    if stripped[0] == '{' and stripped[-1] == '}':
        return True, None
    # отступ проверяется по исходному тексту: после strip() строка не начинается
    # с пробела никогда, и проверка не срабатывала — а отбитая пробелами строка
    # стектрейса с таймстемпом внутри (дамп payload, вложенный лог) рвала запись
    # надвое. Заодно продолжение перестаёт оплачивать поиск таймстемпа.
    if text[0] in ' \t':
        return False, None
    dt, end, _start, tz_known = find_timestamp_full(text, limit=64)
    hint = (dt, end, tz_known)
    # прежняя проверка искала в первых 48 символах: совпадение считается
    # найденным, только если оно целиком помещается в этот отрезок
    if dt is not None and end <= 48:
        return True, hint
    if NGINX_RE.match(text):
        return True, hint
    # явные маркеры начала ошибки без таймстемпа
    if stripped.startswith(('Traceback (most recent call last)', 'panic:', 'fatal error:')):
        return True, hint
    # logfmt без таймстемпа: level=error msg="..."
    if re.match(r'^[A-Za-z_][\w.\-]*=', stripped) and \
            re.search(r'\b(level|severity|lvl|msg|message|time|ts)=', stripped[:200]):
        return True, hint
    # продолжения стектрейсов
    if re.match(r'^\s*(at |Caused by:|\.{3} \d+ more|File ")', text):
        return False, hint
    return False, hint


def parse_record(rec):
    text = rec.raw[0].strip()
    if not text:
        return rec
    if text[0] == '{' and parse_json_line(rec, text):
        pass
    elif parse_nginx_line(rec, text):
        pass
    elif parse_logfmt_line(rec, text):
        pass
    else:
        parse_plain_line(rec, text)
    if len(rec.raw) > 1:
        tail = '\n'.join(rec.raw[1:])
        rec.message = (rec.message + ' | ' + tail.strip())[:4000]
        if rec.level is None and LEVEL_RE.search(tail[:200]):
            rec.level = norm_level(tail[:200])
    if rec.trace is None:
        m = TRACE_TEXT_RE.search(rec.raw_text[:1000])
        if m:
            rec.trace = m.group(1)
    if rec.level is None:
        low = rec.raw_text[:400]
        if EXC_RE.search(low) or 'Traceback (most recent call last)' in low:
            rec.level = 'ERROR'
        else:
            rec.level = 'INFO'
    return rec


class PreFilter(object):
    """Отсев записей до полного разбора.

    Фильтры в `analyze` применяются к разобранной записи: формат определён, поля
    извлечены, шаблон построен — и только потом запись выбрасывается. На больших
    логах именно эта работа и составляет основное время: при `--level ERROR`
    девять записей из десяти разбираются, чтобы быть отброшенными.

    Здесь то же решение принимается по сырому тексту уже собранной записи, до
    разбора. Запись собрана целиком, вместе с продолжениями, поэтому стектрейс не
    разваливается: строка `at com.acme...` живёт или умирает вместе со своей
    первой строкой, а не сама по себе.

    Отсев консервативный. Отбрасывается только то, про что по сырому тексту можно
    сказать наверняка: уровень распознан и он ниже требуемого, время распознано и
    лежит вне окна. Всё неоднозначное уходит в разбор и фильтруется как раньше —
    потерянная запись в разборе инцидента дороже сэкономленных секунд.
    """

    __slots__ = ('min_ord', 'since', 'until', 'grep_rx', 'skipped')

    def __init__(self, level=None, since=None, until=None, grep=None):
        self.min_ord = LEVEL_ORD[level] if level else None
        self.since = since
        self.until = until
        self.grep_rx = re.compile(grep, re.IGNORECASE) if grep else None
        self.skipped = 0

    def active(self):
        return (self.min_ord is not None or self.since is not None
                or self.until is not None or self.grep_rx is not None)

    def skip(self, rec):
        head = rec.raw[0]
        # JSON-строка: уровень достаётся из поля после разбора, а не из первого
        # слова сырого текста — иначе `{"msg":"debug retry failed","level":"error"}`
        # теряется при `--level ERROR` из-за слова «debug» в тексте сообщения
        is_json = head.lstrip()[:1] == '{'
        if self.min_ord is not None and not is_json:
            m = LEVEL_RE.search(head[:200])
            if m:
                level = LEVEL_ALIASES.get(m.group(1).upper())
                if level is not None and LEVEL_ORD.get(level, 2) < self.min_ord:
                    self.skipped += 1
                    return True
        if self.since is not None or self.until is not None:
            ts = rec.ts_hint[0] if rec.ts_hint else find_timestamp(head)[0]
            if ts is not None:
                if self.since is not None and ts < self.since:
                    self.skipped += 1
                    return True
                if self.until is not None and ts > self.until:
                    self.skipped += 1
                    return True
        if self.grep_rx is not None and not self.grep_rx.search(rec.raw_text):
            self.skipped += 1
            return True
        return False


def read_records(sources, encoding, max_lines, prefilter=None):
    keep = prefilter.skip if (prefilter is not None and prefilter.active()) else None
    current = None
    for origin, line_no, text in iter_lines(sources, encoding, max_lines):
        if not text.strip():
            continue
        if current is None:
            current = Record(origin, line_no, text, classify_line(text)[1])
            continue
        start, hint = classify_line(text)
        if start or origin != current.origin:
            if keep is None or not keep(current):
                yield parse_record(current)
            current = Record(origin, line_no, text, hint)
        else:
            if len(current.raw) < 60:
                current.raw.append(text)
                current._text = None
    if current is not None:
        if keep is None or not keep(current):
            yield parse_record(current)


# --------------------------------------------------------------------------
# Шаблонизация
# --------------------------------------------------------------------------

# Третий элемент — подстрока, без которой маска заведомо не сработает: проверка
# `in` обходится в разы дешевле запуска регулярки, а сообщение обычно содержит
# лишь пару из этих признаков. None — маску надо пробовать всегда.
MASKS = [
    (re.compile(r'\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s"\'<>]+'), '<url>', '://'),
    (re.compile(r'\b[\w.\-+]+@[\w.\-]+\.\w{2,}\b'), '<email>', '@'),
    (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<uuid>', '-'),
    (re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+\-]\d{2}:?\d{2})?'),
     '<ts>', '-'),
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b'), '<ip>', '.'),
    (re.compile(r'\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b'), '<time>', ':'),
    (re.compile(r'(?<![\w.])(?:/[\w.\-@]+){2,}/?'), '<path>', '/'),
    (re.compile(r'\b(?:0x)?[0-9a-fA-F]{12,}\b'), '<hex>', None),
    (re.compile(r'"[^"\n]{0,200}"'), '<str>', '"'),
    (re.compile(r"'[^'\n]{0,200}'"), '<str>', "'"),
    (re.compile(r'\b[\w\-]*\d[\w\-]*\b'), '<n>', None),
]

WS_RE = re.compile(r'\s+')


SQUEEZE_N_RE = re.compile(r'(<n>[ ,.]*){2,}')


def templatize(message, limit=180):
    # Обрезать сообщение до замен нельзя, хотя это и выглядит выгодным: маски
    # сжимают текст в десятки раз («…200 символов…» → <str>), поэтому отрезанный
    # хвост влияет на итоговый шаблон. Проверено на логах с длинными сообщениями:
    # обрезка до замен разбивает 7 групп на 10 и портит частоты.
    text = message.replace('\n', ' ⏎ ')
    for rx, repl, needle in MASKS:
        if needle is None or needle in text:
            text = rx.sub(repl, text)
    if '<n>' in text:
        text = SQUEEZE_N_RE.sub('<n> ', text)
    text = WS_RE.sub(' ', text).strip(' \t|,;:-')
    if len(text) > limit:
        text = text[:limit] + '…'
    return text or '<empty>'


def bucket_key(dt, minutes):
    if minutes >= 1440:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if minutes >= 60:
        step = minutes // 60
        return dt.replace(hour=(dt.hour // step) * step, minute=0, second=0, microsecond=0)
    return dt.replace(minute=(dt.minute // minutes) * minutes, second=0, microsecond=0)


# --------------------------------------------------------------------------
# Анализ
# --------------------------------------------------------------------------


class Group(object):
    def __init__(self, level, template):
        self.level = level
        self.template = template
        self.count = 0
        self.first = None
        self.last = None
        # зона известна у всех записей группы — иначе время её первого появления
        # сопоставимо с другими источниками только при допущении о зоне
        self.tz_known = True
        self.sample = None
        self.origins = Counter()
        self.sources = Counter()
        self.traces = set()
        self.records = []

    def add(self, rec):
        self.count += 1
        if rec.ts:
            if not rec.tz_known:
                self.tz_known = False
            if self.first is None or rec.ts < self.first:
                self.first = rec.ts
            if self.last is None or rec.ts > self.last:
                self.last = rec.ts
        if self.sample is None:
            self.sample = rec.raw_text
        self.origins[rec.origin] += 1
        if rec.source:
            self.sources[rec.source] += 1
        if rec.trace and len(self.traces) < 50:
            self.traces.add(rec.trace)
        if len(self.records) < 5:
            self.records.append(rec)


def analyze(records, args):
    groups = {}
    stats = {
        'records': 0, 'filtered': 0, 'levels': Counter(), 'formats': Counter(),
        'origins': Counter(), 'statuses': Counter(), 'exceptions': Counter(),
        'services': Counter(),
        # считаются по всем записям с распознанным временем, а не по прошедшим
        # фильтры: смешение зон — свойство ленты, а не выборки из неё
        'tz_known': 0, 'tz_unknown': 0,
    }
    hist = Counter()
    err_hist = Counter()
    trace_errors = Counter()
    first_ts = last_ts = None
    grep_rx = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    min_ord = LEVEL_ORD[args.level] if args.level else None

    for rec in records:
        stats['records'] += 1
        stats['levels'][rec.level] += 1
        stats['formats'][rec.fmt] += 1
        stats['origins'][rec.origin] += 1
        if rec.source:
            stats['services'][rec.source] += 1
        if rec.status:
            stats['statuses'][rec.status] += 1
        if rec.ts:
            stats['tz_known' if rec.tz_known else 'tz_unknown'] += 1
            if first_ts is None or rec.ts < first_ts:
                first_ts = rec.ts
            if last_ts is None or rec.ts > last_ts:
                last_ts = rec.ts

        if args.since and (rec.ts is None or rec.ts < args.since):
            continue
        if args.until and (rec.ts is None or rec.ts > args.until):
            continue
        if grep_rx and not grep_rx.search(rec.raw_text):
            continue
        if min_ord is not None and LEVEL_ORD.get(rec.level, 2) < min_ord:
            continue

        stats['filtered'] += 1
        # находки обеих регулярок объединяются до инкремента: PY_EXC_RE отличается
        # от EXC_RE лишь суффиксом `Warning` и привязкой к началу строки, поэтому
        # `ValueError: bad input` находится обеими — и класс считался бы дважды
        # sorted, а не голое множество: при равных частотах порядок в выводе
        # задаётся порядком первого инкремента, а обход множества строк зависит
        # от хеша процесса — два прогона на одном логе давали разные сводки
        head = rec.raw_text[:4000]
        for exc in sorted(set(EXC_RE.findall(head)) | set(PY_EXC_RE.findall(head))):
            stats['exceptions'][exc] += 1
        if rec.ts:
            hist[rec.ts.replace(second=0, microsecond=0)] += 1
            if LEVEL_ORD.get(rec.level, 2) >= LEVEL_ORD['WARN']:
                err_hist[rec.ts.replace(second=0, microsecond=0)] += 1
        if rec.trace and LEVEL_ORD.get(rec.level, 2) >= LEVEL_ORD['ERROR']:
            trace_errors[rec.trace] += 1

        tmpl = templatize(rec.message)
        key = (rec.level, tmpl)
        grp = groups.get(key)
        if grp is None:
            grp = groups[key] = Group(rec.level, tmpl)
        grp.add(rec)

    ordered = sorted(groups.values(),
                     key=lambda g: (-LEVEL_ORD.get(g.level, 2), -g.count))
    ordered.sort(key=lambda g: (-(LEVEL_ORD.get(g.level, 2) >= LEVEL_ORD['WARN']), -g.count))
    return {
        'groups': ordered, 'stats': stats, 'hist': hist, 'err_hist': err_hist,
        'trace_errors': trace_errors, 'first_ts': first_ts, 'last_ts': last_ts,
        'prefiltered': 0,
    }


def time_notes(stats):
    """Допущения, сделанные при разборе времени. Пустой список — их не было.

    Названы вместе с последствием: пометка «зона неизвестна» сама по себе читается
    как поломка, а нужно, чтобы читатель понял, чему именно нельзя верить.
    """
    notes = []
    known = stats.get('tz_known', 0)
    unknown = stats.get('tz_unknown', 0)
    total = known + unknown
    if unknown and total:
        notes.append('Зона не указана у %d из %d записей с временем (%d%%) — '
                     'они показаны как записаны, зона не домысливается ни из '
                     'окружения, ни из соседних записей.'
                     % (unknown, total, round(100.0 * unknown / total)))
    if known and unknown:
        notes.append('В ленте есть и записи с указанной зоной, и записи без неё: '
                     'порядок событий между этими группами может быть неверен, а с '
                     'ним и вывод о том, что случилось раньше.')
    if YEAR_ASSUMED:
        notes.append('Года в записях syslog нет — подставлен %s, выбран так, чтобы '
                     'событие не оказалось в будущем.'
                     % ', '.join(str(y) for y in sorted(YEAR_ASSUMED)))
    return notes


def short_template(template, limit=100):
    """Укорачивает шаблон до сигнатуры: первый смысловой кусок без хвоста трейса."""
    head = template.split(' | ')[0].split(' ⏎ ')[0].strip()
    if len(head) < 15 and ' | ' in template:
        head = template.split(' ⏎ ')[0].strip()
    if len(head) > limit:
        head = head[:limit].rstrip() + '…'
    return head


def build_signatures(result, limit=12):
    sigs = []
    for exc, cnt in result['stats']['exceptions'].most_common(6):
        sigs.append({'kind': 'exception', 'value': exc, 'count': cnt})
    for status, cnt in sorted(result['stats']['statuses'].items()):
        if status >= 500:
            sigs.append({'kind': 'http', 'value': 'http_status=%d' % status, 'count': cnt})
    for grp in result['groups']:
        if len(sigs) >= limit:
            break
        if LEVEL_ORD.get(grp.level, 2) < LEVEL_ORD['WARN']:
            continue
        sigs.append({'kind': 'template', 'value': 'tmpl:' + short_template(grp.template),
                     'count': grp.count, 'level': grp.level})
    return sigs[:limit]


# --------------------------------------------------------------------------
# Вывод
# --------------------------------------------------------------------------


def fmt_ts(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S') if dt else '?'


def fmt_short(dt):
    return dt.strftime('%H:%M:%S') if dt else '?'


def render_md(result, args, out):
    """Сводка в пределах бюджета объёма.

    Сводка едет в контекст агента и остаётся там до конца разбора, поэтому её
    объём ограничен. Режется не текст, а число показанных шаблонов: обрубленная
    на полуслове сводка хуже короткой. Хвостовые разделы — исключения, статусы,
    гистограмма, сигнатуры — считаются заранее и место под них резервируется:
    сигнатуры нужны следующим шагам разбора, терять их нельзя.
    """
    budget = getattr(args, 'max_chars', 0) or 0
    if budget <= 0:
        return _render_md(result, args, out, None)
    head = io.StringIO()
    _render_md(result, args, head, None, head_only=True)
    tail = io.StringIO()
    _render_tail(result, args, tail)
    # 320 символов — заголовок секции шаблонов и строка о том, что часть скрыта:
    # они пишутся уже после подгонки, и без резерва сводка вылезает за бюджет
    room = budget - len(head.getvalue()) - len(tail.getvalue()) - 320
    _render_md(result, args, out, room)


def _render_md(result, args, out, room, head_only=False):
    stats = result['stats']
    w = out.write
    w('# Сводка логов\n\n')
    origins = ', '.join('%s (%d)' % (o, c) for o, c in stats['origins'].most_common(8))
    w('- Источники: %s\n' % (origins or 'нет данных'))
    prefiltered = result.get('prefiltered', 0)
    w('- Записей: %d' % (stats['records'] + prefiltered))
    if stats['filtered'] != stats['records'] + prefiltered:
        w(' (после фильтров: %d)' % stats['filtered'])
    w('\n')
    if prefiltered:
        w('- Отсеяно фильтром до разбора: %d — распределение по уровням, форматам и\n'
          '  компонентам ниже посчитано по оставшимся записям\n' % prefiltered)
    if result['first_ts']:
        span = result['last_ts'] - result['first_ts']
        w('- Период: %s — %s (%s)\n' % (fmt_ts(result['first_ts']),
                                        fmt_ts(result['last_ts']), fmt_span(span)))
        # шкала называется всегда, когда в сводке есть времена: без неё их не с чем
        # сверить — ни с Kibana, ни с алертом
        w('- Шкала времени: %s\n' % TIME_SCALE)
    else:
        w('- Период: таймстемпы не распознаны\n')
    levels = ' '.join('%s=%d' % (lv, stats['levels'][lv])
                      for lv in LEVELS if stats['levels'].get(lv))
    w('- Уровни: %s\n' % (levels or '—'))
    w('- Форматы: %s\n' % ' '.join('%s=%d' % (k, v) for k, v in stats['formats'].most_common()))
    if stats['services']:
        w('- Компоненты: %s\n' % ', '.join(
            '%s (%d)' % (s, c) for s, c in stats['services'].most_common(6)))
    notes = time_notes(stats)
    if notes:
        w('\n**Допущения о времени**\n\n')
        for note in notes:
            w('- %s\n' % note)
    w('\n')

    if head_only:
        return

    problems = [g for g in result['groups'] if LEVEL_ORD.get(g.level, 2) >= LEVEL_ORD['WARN']]
    others = [g for g in result['groups'] if LEVEL_ORD.get(g.level, 2) < LEVEL_ORD['WARN']]

    if problems:
        wanted = problems[:args.top]
        picked = (fit_groups(wanted, room) if room is not None
                  else list(range(len(wanted))))
        shown = len(picked)
        w('## Ошибки и предупреждения (топ %d из %d шаблонов)\n\n'
          % (shown, len(problems)))
        # номер группы — её место в общем порядке, а не порядковый номер строки:
        # по нему пользователь просит сырые записи через `--context`, и после
        # усечения показанные группы идут не подряд
        render_groups([wanted[i] for i in picked], w, numbers=[i + 1 for i in picked])
        hidden = len(problems) - shown
        if hidden > 0:
            # причины разные, и путать их не надо: `--top` пользователь задал сам,
            # а бюджет объёма — решение скрипта, о котором он и должен сказать
            why = ('сводка ограничена по объёму' if shown < len(wanted)
                   else 'показан топ %d, задайте `--top` больше' % args.top)
            w('_Не показано шаблонов: %d — %s. '
              'Полный разбор: `--format json > файл`, отдельная группа: `--context N`._\n\n'
              % (hidden, why))
    else:
        shown = 0
        w('## Ошибок и предупреждений не найдено\n\n'
          'Если инцидент точно был — сбой мог не логироваться как ошибка '
          '(проверь обрыв лога, OOM, kill) или окно выбрано мимо.\n\n')

    if others and args.show_info:
        w('## Прочие сообщения (топ 5)\n\n')
        render_groups(others[:5], w, start=shown + 1)

    _render_tail(result, args, out)


def group_size(grp):
    """Сколько символов займёт группа в сводке — считаем по факту рендера."""
    buf = io.StringIO()
    render_groups([grp], buf.write, start=1)
    return len(buf.getvalue())


EARLY_KEPT = 2


def fit_groups(groups, room, early=EARLY_KEPT):
    """Номера групп (индексы в `groups`), попадающих в отведённый объём.

    Группы отсортированы по частоте, поэтому усечение «первые N» режет хвост — а
    в хвосте живут редкие, но самые ранние шаблоны. Именно они обычно называют
    причину: частое приходит следом и является следствием. Поэтому место
    резервируется под `early` самых ранних шаблонов, и они остаются в сводке,
    даже если по частоте не дотянули.

    Порядок отбора: сначала самый частый шаблон (ради него сводку и открывают),
    затем ранние, затем остальные по частоте. Минимум одна группа показывается
    всегда: сводка без единого шаблона не отвечает на вопрос, ради которого её
    читают.
    """
    if not groups:
        return []
    sizes = [group_size(g) for g in groups]
    earliest = sorted((i for i, g in enumerate(groups) if g.first is not None),
                      key=lambda i: (groups[i].first, i))[:early]
    order = [0]
    order += [i for i in earliest if i not in order]
    order += [i for i in range(len(groups)) if i not in order]

    chosen = []
    used = 0
    for i in order:
        if used + sizes[i] > room and chosen:
            continue
        chosen.append(i)
        used += sizes[i]
    return sorted(chosen)


def _render_tail(result, args, out):
    stats = result['stats']
    w = out.write
    if stats['exceptions']:
        w('## Exception-классы\n\n')
        for exc, cnt in stats['exceptions'].most_common(10):
            w('- %d× `%s`\n' % (cnt, exc))
        w('\n')

    if stats['statuses']:
        w('## HTTP-статусы\n\n')
        for status, cnt in sorted(stats['statuses'].items()):
            mark = ' ⚠' if status >= 500 else ''
            w('- %s: %d%s\n' % (status, cnt, mark))
        w('\n')

    hist = result['err_hist'] or result['hist']
    if hist and (args.histogram or result['err_hist']):
        w('## Гистограмма %s\n\n```\n' % ('ошибок' if result['err_hist'] else 'записей'))
        render_hist(hist, w)
        w('```\n\n')

    if result['trace_errors']:
        w('## Trace/request id с ошибками\n\n')
        for trace, cnt in result['trace_errors'].most_common(5):
            w('- `%s` — %d ошибок (`--trace %s` покажет цепочку)\n' % (trace, cnt, trace))
        w('\n')

    sigs = build_signatures(result)
    if sigs:
        w('## Сигнатуры для базы знаний\n\n')
        for sig in sigs:
            w('- `%s` (%d×)\n' % (sig['value'], sig['count']))
        w('\nПоиск похожих инцидентов: '
          '`kb_search.py --from-parsed <файл>` (сохрани разбор через `--format json`).\n')


def render_groups(groups, w, start=1, numbers=None):
    # номера задаются списком, когда показанные группы идут не подряд: номер
    # должен указывать на группу, а не на строку сводки — по нему запрашивают
    # `--context`
    seq = numbers if numbers is not None else range(start, start + len(groups))
    for i, grp in zip(seq, groups):
        w('**#%d · %d× · %s**\n' % (i, grp.count, grp.level))
        w('`%s`\n' % grp.template)
        detail = []
        if grp.first:
            detail.append('first %s' % fmt_ts(grp.first))
        if grp.last and grp.last != grp.first:
            detail.append('last %s' % fmt_ts(grp.last))
        if grp.sources:
            detail.append('компонент: %s' % ', '.join(list(grp.sources)[:3]))
        if grp.origins:
            detail.append('файл: %s' % ', '.join(list(grp.origins)[:3]))
        if detail:
            w('  %s\n' % ' · '.join(detail))
        sample = (grp.sample or '').strip()
        if sample:
            lines = sample.split('\n')
            shown = ' ⏎ '.join(lines[:3])
            w('  пример: %s%s\n' % (shown[:300], '…' if len(shown) > 300 else ''))
        w('  сырые записи: `--context %d`\n\n' % i)


def render_hist(hist, w, width=40):
    if not hist:
        return
    keys = sorted(hist)
    span = (keys[-1] - keys[0]).total_seconds() / 60 if len(keys) > 1 else 1
    minutes = 1
    for candidate in (1, 5, 15, 30, 60, 180, 720, 1440):
        if span / candidate <= 30:
            minutes = candidate
            break
    else:
        minutes = 1440
    buckets = Counter()
    for key, val in hist.items():
        buckets[bucket_key(key, minutes)] += val
    peak = max(buckets.values())
    for key in sorted(buckets):
        val = buckets[key]
        bar = '█' * max(1, int(val / peak * width))
        mark = '  ← пик' if val == peak else ''
        w('%s  %-*s %d%s\n' % (key.strftime('%m-%d %H:%M'), width, bar, val, mark))


def fmt_span(delta):
    total = int(delta.total_seconds())
    if total < 60:
        return '%d с' % total
    if total < 3600:
        return '%d мин' % (total // 60)
    if total < 86400:
        return '%d ч %d мин' % (total // 3600, (total % 3600) // 60)
    return '%d дн %d ч' % (total // 86400, (total % 86400) // 3600)


def render_json(result, args, out):
    stats = result['stats']
    payload = {
        'stats': {
            'records': stats['records'] + result.get('prefiltered', 0),
            'parsed': stats['records'],
            'prefiltered': result.get('prefiltered', 0),
            'filtered': stats['filtered'],
            'levels': dict(stats['levels']),
            'formats': dict(stats['formats']),
            'origins': dict(stats['origins']),
            'services': dict(stats['services'].most_common(20)),
            'statuses': {str(k): v for k, v in stats['statuses'].items()},
            'exceptions': dict(stats['exceptions'].most_common(20)),
            'first_ts': fmt_ts(result['first_ts']) if result['first_ts'] else None,
            'last_ts': fmt_ts(result['last_ts']) if result['last_ts'] else None,
            # смысл существующих полей времени не меняется: это та же шкала, теперь
            # названная. Разбор, сделанный прежней версией, этих полей не имеет —
            # читающий его считает зону неизвестной, как оно и было
            'time_scale': TIME_SCALE,
            'tz_known': stats['tz_known'],
            'tz_unknown': stats['tz_unknown'],
            'mixed_timezones': bool(stats['tz_known'] and stats['tz_unknown']),
            'time_assumptions': time_notes(stats),
        },
        # `--top` сюда не применяется: это правило md-сводки, которая едет в
        # контекст агента. JSON пишется в файл и служит как раз способом увидеть
        # то, что в сводку не поместилось, — усекать его нечем и незачем
        'groups': [{
            'n': i,
            'level': g.level,
            'count': g.count,
            'template': g.template,
            'first': fmt_ts(g.first) if g.first else None,
            'last': fmt_ts(g.last) if g.last else None,
            'tz_known': g.tz_known,
            'sample': (g.sample or '')[:600],
            'origins': dict(g.origins),
            'sources': dict(g.sources),
        } for i, g in enumerate(result['groups'], 1)],
        'signatures': build_signatures(result),
        'trace_errors': dict(result['trace_errors'].most_common(10)),
        'histogram': {k.strftime('%Y-%m-%d %H:%M'): v
                      for k, v in sorted(result['err_hist'].items())},
    }
    dump_json(payload, out)


def render_context(result, args, out):
    idx = args.context
    groups = result['groups']
    if idx < 1 or idx > len(groups):
        out.write('Нет группы #%d (всего %d)\n' % (idx, len(groups)))
        return
    grp = groups[idx - 1]
    out.write('# Группа #%d — %d× %s\n`%s`\n\nВремена в %s.\n\n'
              % (idx, grp.count, grp.level, grp.template, TIME_SCALE))
    for rec in grp.records:
        out.write('--- %s:%d  %s\n%s\n\n' % (rec.origin, rec.line_no,
                                             fmt_ts(rec.ts), rec.raw_text[:4000]))


def render_trace(records, trace_id, out):
    hits = [r for r in records if (r.trace and trace_id in r.trace)
            or trace_id in r.raw_text]
    if not hits:
        out.write('Записей с id %r не найдено\n' % trace_id)
        return
    hits.sort(key=lambda r: (r.ts or datetime.min, r.origin, r.line_no))
    out.write('# Цепочка %s — %d записей\n\nВремена в %s.\n\n'
              % (trace_id, len(hits), TIME_SCALE))
    for rec in hits:
        out.write('%s  %-5s  %s\n%s\n\n' % (fmt_ts(rec.ts), rec.level,
                                            rec.source or rec.origin,
                                            rec.raw_text[:2000]))


# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Парсер неструктурированных логов: сводка, шаблоны, сигнатуры.')
    ap.add_argument('sources', nargs='+',
                    help='файлы, glob-маски, директории, архивы или "-" для stdin')
    ap.add_argument('--level', choices=LEVELS, help='минимальный уровень для анализа')
    ap.add_argument('--since', help='начало окна, напр. "2026-07-28 12:00"')
    ap.add_argument('--until', help='конец окна')
    ap.add_argument('--grep', help='regex-фильтр по сырому тексту записи')
    ap.add_argument('--top', type=int, default=10, help='сколько шаблонов показать (10)')
    ap.add_argument('--context', type=int, help='показать сырые записи группы №N')
    ap.add_argument('--trace', help='показать все записи с указанным trace/request id')
    ap.add_argument('--histogram', action='store_true', help='всегда показывать гистограмму')
    ap.add_argument('--show-info', action='store_true', help='включить не-ошибочные шаблоны')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    ap.add_argument('--max-chars', type=int, default=MAX_SUMMARY_CHARS,
                    help='предельный объём сводки в символах (%d), 0 — без предела'
                         % MAX_SUMMARY_CHARS)
    ap.add_argument('--max-lines', type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument('--encoding', default='utf-8')
    args = ap.parse_args(argv)

    args.since = parse_time_arg(args.since) if args.since else None
    args.until = parse_time_arg(args.until) if args.until else None

    sources = expand_sources(args.sources)
    if not sources:
        raise SystemExit('Нечего парсить: источники не найдены')

    # при --trace и --context нужны все записи: цепочку запроса и сырые строки
    # группы нельзя собрать из того, что отсеяно до разбора
    keep_records = bool(args.trace)
    prefilter = None
    if not args.trace and not args.context:
        prefilter = PreFilter(level=args.level, since=args.since,
                              until=args.until, grep=args.grep)
    records = []
    stream = read_records(sources, args.encoding, args.max_lines, prefilter)
    if keep_records:
        records = list(stream)
        stream = records

    result = analyze(stream, args)
    result['prefiltered'] = prefilter.skipped if prefilter is not None else 0
    out = sys.stdout

    if args.trace:
        render_trace(records, args.trace, out)
        return 0
    if args.context:
        render_context(result, args, out)
        return 0
    if args.format == 'json':
        render_json(result, args, out)
    else:
        render_md(result, args, out)
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
