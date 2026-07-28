#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общее для скриптов скилла: работа со временем, база знаний, токенизация, скоринг."""

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KB = os.path.join(SKILL_DIR, 'kb')

# Куда предлагается положить базу в проекте. Каталог видимый: база в markdown
# ради того, чтобы её читал человек без агента.
PROJECT_KB_RELPATH = os.path.join('memory', 'knowledgebase')

SECTIONS = ['Симптомы', 'Диагностика', 'Причина', 'Решение', 'Проверка', 'Заметки']

# Отдельно от SECTIONS: это единственный необязательный раздел тела записи — его
# не заполняют плейсхолдером «не заполнено», отсутствие признаков не создаёт
# пустого раздела ни в записи, ни в выдаче поиска.
DISTINGUISHERS_SECTION = 'Отличительные признаки'

# Значения поля исхода. Отсутствие поля читается как OUTCOME_UNVERIFIED — это и
# есть правда о записях, созданных до появления этого поля.
OUTCOME_CONFIRMED = 'confirmed'
OUTCOME_REFUTED = 'refuted'
OUTCOME_UNVERIFIED = 'unverified'
OUTCOMES = (OUTCOME_CONFIRMED, OUTCOME_REFUTED, OUTCOME_UNVERIFIED)


def outcome_of(meta):
    """Исход записи: явно заданный или «не проверена» по умолчанию."""
    val = str((meta or {}).get('outcome') or '').strip().lower()
    return val if val in OUTCOMES else OUTCOME_UNVERIFIED

# Вид записи базы. Отсутствие поля читается как KIND_INCIDENT: базы, собранные
# до появления карты источников, состоят из одних разборов, и мигрировать их
# незачем.
KIND_INCIDENT = 'incident'
KIND_SOURCE = 'source'
KINDS = (KIND_INCIDENT, KIND_SOURCE)

# Поля записи карты источников: способ обращения, адрес внутри источника,
# сработавший запрос, соответствие полей, дата подтверждения и перечень
# проверенных и отвергнутых источников.
SOURCE_FIELDS = ('stand', 'services', 'source', 'address', 'query', 'fields',
                 'confirmed', 'checked')

# Значения вердикта при пометке бесполезного источника.
VERDICT_EMPTY = 'empty'
VERDICT_UNAVAILABLE = 'unavailable'
VERDICTS = (VERDICT_EMPTY, VERDICT_UNAVAILABLE)

VERDICT_LABELS = {
    VERDICT_EMPTY: 'пуст для этой пары',
    VERDICT_UNAVAILABLE: 'недоступен',
}

# Через сколько дней запись карты считается требующей проверки. Лечит случай,
# который по имени инструмента не поймать: инструмент тот же, а его
# перенастроили. Устаревшая запись не удаляется — стоимость лишней попытки
# меньше полной инвентаризации.
SOURCE_STALE_DAYS = 30


def kind_of(meta):
    """Вид записи: явно заданный или «разбор инцидента» по умолчанию."""
    val = str((meta or {}).get('kind') or '').strip().lower()
    return val if val in KINDS else KIND_INCIDENT

# Вывод скриптов едет в контекст агента и остаётся там до конца разбора: чем он
# больше, тем медленнее каждый следующий шаг. Предел общий для всех скриптов,
# на машинный вывод (`--format json`) не распространяется — тот пишется в файл.
MAX_SUMMARY_CHARS = 12000

LIST_FIELDS = ('stands', 'services', 'tags', 'signatures', 'related', 'files', 'commits')

# Единый предел количества обрабатываемых строк — по умолчанию «практически без
# предела»: явный флаг у скрипта нужнее магического числа, скопированного трижды.
DEFAULT_MAX_LINES = 2000000

# Уровни логов и их порядок — единственный источник. Разошедшиеся копии этого
# списка означают, что `--level ERROR` в одном скрипте отсекает не то же самое,
# что в другом.
LEVELS = ['TRACE', 'DEBUG', 'INFO', 'NOTICE', 'WARN', 'ERROR', 'FATAL']
LEVEL_ORD = {name: i for i, name in enumerate(LEVELS)}

# Класс исключения/ошибки в сыром тексте: полный вариант, знающий и
# `...Denied`/`...Refused` — без них `AccessDeniedException` считался бы
# сигнатурой, но не находился бы в коде по классу исключения.
EXC_RE = re.compile(
    r'\b((?:[a-z][\w]*\.)*[A-Z][A-Za-z0-9_]*'
    r'(?:Exception|Error|Throwable|Timeout|Failure|Fault|Denied|Refused))\b')


def require_python():
    """Преамбула проверки версии — без f-строк: на старом интерпретаторе должно
    печататься сообщение, а не падать SyntaxError."""
    if sys.version_info < (3, 8):
        sys.stderr.write('incident-detective: нужен Python 3.8 или новее, запущен %s (%s)\n'
                         % (sys.version.split()[0], sys.executable))
        sys.exit(2)


def dump_json(obj, dest):
    """JSON с единым форматом: `ensure_ascii=False, indent=2, default=str`.

    `dest` — путь к файлу (тогда открывается и закрывается сам) или уже
    открытый поток (stdout, io.StringIO, файловый объект). `default=str`
    избавляет вызывающий код от ручной сериализации дат — она форматируется
    так же, как читалась.
    """
    if isinstance(dest, (str, bytes, os.PathLike)):
        with open(dest, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
            fh.write('\n')
        return
    json.dump(obj, dest, ensure_ascii=False, indent=2, default=str)
    dest.write('\n')


def overflow_dir():
    """Директория для полных результатов, обрезанных в сводке — общая для всех
    скриптов, чтобы агент знал одно место, а не путь, который меняется от
    скрипта к скрипту."""
    return os.path.join(os.environ.get('TMPDIR') or '/tmp', 'incident-detective')


def dump_overflow(payload, name):
    """Сохраняет то, что не поместилось в сводку, и возвращает путь.

    None, если сохранить не удалось — вызывающий код должен сказать об этом в
    выводе, а не притвориться, что путь есть.
    """
    directory = overflow_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        dump_json(payload, path)
    except OSError:
        return None
    return path


def fit_by_render(items, render, budget, reserve=0):
    """Сколько элементов поместится в бюджет объёма.

    `render(item, write)` пишет один элемент через переданную `write`;
    функция суммирует фактический размер вывода, а не оценивает его на глаз —
    так же поступают все существующие реализации, которые эта функция сводит
    в одну. `reserve` — место, зарезервированное под хвост (шапки следующих
    разделов, строка «не показано ещё N»).

    Возвращает (показанные элементы, число скрытых). Бюджет ``<= 0`` —
    предела нет, показаны все элементы.
    """
    if budget is None or budget <= 0:
        items = list(items)
        return items, 0
    room = max(budget - reserve, 0)
    used = 0
    shown = []
    for item in items:
        buf = io.StringIO()
        render(item, buf.write)
        used += len(buf.getvalue())
        if used > room and shown:
            break
        shown.append(item)
    items = list(items)
    return shown, len(items) - len(shown)


# --------------------------------------------------------------------------
# Разбор аргументов времени (--since/--until)
# --------------------------------------------------------------------------

# «1h», «30m», «2d» — окно, отсчитанное назад от текущего момента.
REL_TIME_RE = re.compile(r'^-?(\d+)\s*([smhd])$', re.IGNORECASE)
REL_UNITS = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days'}


def parse_time_arg(value):
    """Разбор `--since`/`--until`: абсолютное время или относительное окно.

    Канонический разбор для всех скриптов скилла — единственное место, где
    он живёт. Поиск таймстемпа в свободном тексте (`find_timestamp`) отложен
    до вызова: `parse_logs` сам импортирует из `kb_common` на уровне модуля, и
    импорт здесь на верхнем уровне закольцевал бы модули друг на друга.
    """
    text = str(value).strip()
    rel = REL_TIME_RE.match(text)
    if rel:
        # «сейчас» берётся из kb_common.now: с заданным INCIDENT_NOW окно
        # получается тем же в любой день запуска
        return now() - timedelta(**{REL_UNITS[rel.group(2).lower()]: int(rel.group(1))})
    from parse_logs import find_timestamp
    dt, _ = find_timestamp(text, limit=len(text) + 1)
    if dt:
        return dt
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%H:%M'):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == '%H:%M':
                today = now()
                dt = dt.replace(year=today.year, month=today.month, day=today.day)
            return dt
        except ValueError:
            continue
    raise SystemExit('Не разобрал время: %r (ожидается «2026-07-28 12:00», '
                     '«12:00» или «1h»)' % value)

# --------------------------------------------------------------------------
# Текущее время и телеметрия вызовов
# --------------------------------------------------------------------------

# «Сейчас» задаётся снаружи: разбор, зависящий от дня запуска, невоспроизводим.
ENV_NOW = 'INCIDENT_NOW'
# Файл, куда скрипты дописывают факт своего запуска. Не задан — не пишут ничего.
ENV_TRACE = 'INCIDENT_TRACE_FILE'
# Поток сессии клиента, сохранённый обвязкой. Скрипты его не пишут — только
# называют в отчёте, чтобы по одному файлу прогона находились остальные.
ENV_SESSION = 'INCIDENT_SESSION_FILE'

NOW_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
               '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d')

_NOW = []


def _parse_now(raw):
    text = str(raw).strip().strip('"\'')
    if text.endswith('Z'):
        text = text[:-1]
    for fmt in NOW_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def now():
    """«Сейчас» для всех скриптов: из INCIDENT_NOW, иначе системные часы.

    Значение фиксируется на весь запуск: разбор, идущий через полночь, не должен
    получать разные ответы в начале и в конце. Неразобранное значение — ошибка, а
    не тихий откат на системное время: откат вернул бы невоспроизводимость,
    замаскировав её.
    """
    if _NOW:
        return _NOW[0]
    raw = os.environ.get(ENV_NOW)
    if raw is None or not str(raw).strip():
        value = datetime.now()
    else:
        value = _parse_now(raw)
        if value is None:
            raise SystemExit(
                'Не разобрал %s=%r — ожидается «2026-07-28 12:00:00». '
                'Системное время подставлять не буду: разбор стал бы '
                'невоспроизводимым молча.' % (ENV_NOW, raw))
    _NOW.append(value)
    return value


# --------------------------------------------------------------------------
# Режим разбора
# --------------------------------------------------------------------------

# Признак автономного разбора ставит обвязка. Выводить режим из отсутствия
# терминала или из канала запуска нельзя: поведение скилла стало бы зависеть от
# того, как его случайно запустили, — то же правило, по которому «сейчас»
# берётся из INCIDENT_NOW, а не из системных часов.
ENV_MODE = 'INCIDENT_MODE'

MODE_INTERACTIVE = 'interactive'
MODE_AUTO = 'auto'
MODES = (MODE_INTERACTIVE, MODE_AUTO)

_MODE = []


def mode():
    """Режим разбора: из INCIDENT_MODE, иначе диалоговый.

    Неизвестное значение — ошибка запуска, а не повод молча выбрать режим:
    опечатка в переменной обвязки не должна превращать автономный прогон в
    диалоговый, который некому вести.
    """
    if _MODE:
        return _MODE[0]
    raw = os.environ.get(ENV_MODE)
    if raw is None or not str(raw).strip():
        value = MODE_INTERACTIVE
    else:
        value = str(raw).strip().strip('"\'').lower()
        if value not in MODES:
            raise SystemExit(
                'Не разобрал %s=%r — известны значения: %s. Режим по догадке '
                'выбирать не буду: в автономном прогоне некому заметить ошибку.'
                % (ENV_MODE, raw, ', '.join(MODES)))
    _MODE.append(value)
    return value


def is_auto():
    """Идёт ли разбор без человека в контуре."""
    return mode() == MODE_AUTO


# --------------------------------------------------------------------------
# Машинный отчёт разбора
# --------------------------------------------------------------------------

# Единственный машинный выход автономного режима. Имя фиксированное: обвязка
# приходит за файлом по известному пути, а не ищет его перебором.
REPORT_NAME = 'report.json'
REPORT_SCHEMA = 'incident-detective/report@1'

# Исход разбора, а не код возврата: коды принадлежат клиенту, и отличить по ним
# честный отказ от обрыва процесса нельзя. Отсутствие файла отчёта обвязка
# читает как несостоявшийся прогон — это разные события.
VERDICT_INSUFFICIENT = 'данных недостаточно'


def report_path(out_dir):
    return os.path.join(out_dir, REPORT_NAME)


def read_report(path):
    """Отчёт с диска или None, если его нет или он не читается."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_report(payload, path):
    """Сохраняет отчёт. False — не сохранился: вызывающий обязан сказать об этом."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        dump_json(payload, path)
    except OSError:
        return False
    return True


def days_since(date_text, when=None):
    """Сколько дней прошло с даты `YYYY-MM-DD` до «сейчас».

    None — даты нет или она не разобралась: это не ноль дней, и выдавать её за
    свежую нельзя. Отрицательного возраста не бывает: дата из будущего читается
    как сегодняшняя.
    """
    raw = str(date_text or '').strip()
    if not raw:
        return None
    try:
        recorded = datetime.strptime(raw, '%Y-%m-%d')
    except ValueError:
        return None
    return max(0, ((when or now()) - recorded).days)


def source_freshness(meta, when=None):
    """(возраст записи карты в днях, требует ли проверки).

    Возраст считается от даты последнего подтверждения. Записи без даты
    подтверждения доверия не заслуживают — (None, True).
    """
    age = days_since((meta or {}).get('confirmed'), when)
    if age is None:
        return None, True
    return age, age > SOURCE_STALE_DAYS


def mark_freshness(mark, when=None):
    """(возраст пометки бесполезности в днях, перестала ли она исключать источник).

    Пометка стареет по тому же сроку, что и запись карты: инструмент могли
    починить или перенастроить, и вечное исключение из перебора превратилось бы
    во враньё.
    """
    age = days_since((mark or {}).get('date'), when)
    if age is None:
        return None, True
    return age, age > SOURCE_STALE_DAYS


def _flag_names(argv):
    """Только имена флагов: значения аргументов в телеметрию не попадают."""
    names = []
    for item in argv:
        text = str(item)
        if not text.startswith('-'):
            continue
        name = text.split('=', 1)[0]
        if name not in names:
            names.append(name)
    return names


def record_run(script, argv, code):
    """Дописывает строку о запуске скрипта, если задан INCIDENT_TRACE_FILE.

    Отвечает на вопрос, который прогоном фикстур не проверяется и по тексту
    ответа не виден: какие контуры разбора реально прогнали. Значения аргументов
    не пишутся — на вход скриптам идут фрагменты логов и слова пользователя, и
    файл отладки не должен стать ещё одним местом, где они оседают.
    """
    path = os.environ.get(ENV_TRACE)
    if not path:
        return
    try:
        stamp = (_NOW[0] if _NOW else datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
        line = '%s\t%s\t%s\trc=%s\n' % (stamp, script,
                                        ' '.join(_flag_names(argv)) or '-', code)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(line)
    except Exception:                                       # noqa: BLE001
        # отладка не имеет права ломать разбор: недоступный файл — не повод
        # прерывать работу и не повод писать что-то в стандартный вывод
        pass


def run_script(main, path, argv=None):
    """Точка входа скрипта: единая обработка выходов плюс телеметрия.

    Возвращает код возврата — вызывающий передаёт его в sys.exit.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    code = 0
    try:
        now()          # неразобранное INCIDENT_NOW — ошибка до начала работы
        mode()         # и неизвестный INCIDENT_MODE тоже: молча не выбираем
        code = main() or 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            code = 0
        elif not isinstance(code, int):
            sys.stderr.write('%s\n' % code)
            code = 1
    except BrokenPipeError:
        code = 0
    except KeyboardInterrupt:
        code = 130
    record_run(os.path.basename(path), argv, code)
    return code


# --------------------------------------------------------------------------
# Работа с уже разобранным временем
# --------------------------------------------------------------------------

# Одна шкала на весь вывод: время, показанное без указания шкалы, не с чем
# сверить — ни с Kibana, ни с текстом алерта. Разбор смещений живёт в
# parse_logs.py (он запускается первым и над сырыми строками), здесь — только то,
# что работает над уже разобранным временем.
TIME_SCALE = 'UTC'

# ниже этого не считаем расхождение часов подозрительным: сетевые задержки
# и разное время записи в лог дают до секунды сами по себе
CLOCK_MIN_SHIFT = 2.0
# сколько общих точек нужно, чтобы вывод о часах вообще имел смысл
CLOCK_MIN_POINTS = 3


def median(values):
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def sort_key(ts, source, seq):
    """Устойчивый ключ порядка событий.

    При равном времени порядок определяется источником и порядковым номером
    записи, а не порядком обхода словарей и файлов. Вывод «первым был X» не
    должен меняться от запуска к запуску: его читают как факт.
    """
    return (ts if ts is not None else datetime.max, str(source or ''), seq)


def apply_offset(ts, seconds):
    """Сдвиг времени на явно заданную величину. Ноль и None ничего не меняют."""
    if ts is None or not seconds:
        return ts
    return ts + timedelta(seconds=seconds)


def parse_offset_arg(item):
    """'payment=-2.5' -> ('payment', -2.5)."""
    label, sep, val = item.partition('=')
    if not sep:
        raise SystemExit('Сдвиг задаётся как "источник=секунды": %r' % item)
    try:
        return label, float(val)
    except ValueError:
        raise SystemExit('Не разобрал секунды в %r' % item)


def estimate_clock_skew(observations):
    """Оценивает расхождение часов между источниками по общим точкам.

    `observations` — словарь «общая точка → {источник: самое раннее время}». Для
    цепочки запроса общая точка — id запроса, для хронологии — один и тот же
    шаблон сообщения. Математика одна и та же, и живёт она здесь, чтобы два
    инструмента не давали разных ответов об одних и тех же источниках.

    Если разница по всем точкам держится около одного значения (разброс мал), это
    похоже на систематический сдвиг часов, а не на живую задержку: реальная
    задержка гуляет от точки к точке.

    Возвращает (список находок, число общих точек).
    """
    deltas = {}
    for firsts in observations.values():
        labels = sorted(firsts)
        for i, a in enumerate(labels):
            for b in labels[i + 1:]:
                if firsts[a] is None or firsts[b] is None:
                    continue
                deltas.setdefault((a, b), []).append((firsts[b] - firsts[a]).total_seconds())

    findings = []
    for (a, b), vals in sorted(deltas.items()):
        if len(vals) < CLOCK_MIN_POINTS:
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
    return findings, len(observations)


def render_clock_findings(findings, points, write, unit='общих запросов'):
    """Вывод оценки расхождения часов — общий для цепочки и хронологии."""
    if points < CLOCK_MIN_POINTS:
        write('%s слишком мало (%d) — сравнивать нечего.\n'
              % (unit.capitalize(), points))
        return
    if not findings:
        write('Расхождений больше %.0f с не видно (%d %s). '
              'Время источников можно считать сопоставимым.\n'
              % (CLOCK_MIN_SHIFT, points, unit))
        return
    write('%s: %d\n\n' % (unit.capitalize(), points))
    for f in findings:
        kind = ('похоже на сдвиг часов' if f['systematic']
                else 'разброс большой — скорее реальная задержка, чем часы')
        write('- **%s → %s**: медиана %+.2f с, разброс ±%.2f с по %d точкам — %s\n'
              % (f['from'], f['to'], f['median'], f['spread'], f['traces'], kind))
    systematic = [f for f in findings if f['systematic']]
    if systematic:
        f = systematic[0]
        write('\nПоправка применяется вручную, чтобы в выводе не появилось '
              'подогнанное время:\n\n```\n--offset %s=%.2f\n```\n' % (f['to'], -f['median']))
    write('\nОценка косвенная: сдвиг часов и стабильно одинаковая задержка выглядят '
          'одинаково. Сверься с ntp/системным временем стендов, прежде чем опираться '
          'на неё в выводе.\n')


# --------------------------------------------------------------------------
# Расположение базы знаний
# --------------------------------------------------------------------------

ENV_KB = 'INCIDENT_KB_DIR'

# Каким шагом разрешён путь. Источник нужен не скриптам, а агенту: по нему он
# понимает, выбирал ли пользователь расположение вообще.
KB_FLAG = 'flag'          # --kb
KB_ENV = 'env'            # INCIDENT_KB_DIR
KB_PROJECT = 'project'    # база в корне текущего репозитория — выбор уже сделан
KB_DEFAULT = 'default'    # директория внутри скилла — выбора не было

_GIT_TOP = {}


def git_toplevel(start=None):
    """Корень git-репозитория для директории или None, если репозитория нет."""
    start = os.path.abspath(start or os.getcwd())
    if start in _GIT_TOP:
        return _GIT_TOP[start]
    top = None
    try:
        proc = subprocess.run(['git', '-C', start, 'rev-parse', '--show-toplevel'],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=5)
        if proc.returncode == 0:
            text = proc.stdout.decode('utf-8', 'replace').strip()
            if text:
                top = os.path.abspath(text)
    except (OSError, subprocess.SubprocessError):
        # git может отсутствовать — это не ошибка, просто нет шага «база проекта»
        top = None
    _GIT_TOP[start] = top
    return top


def project_root(start=None):
    """Корень проекта: корень репозитория, иначе текущая директория."""
    return git_toplevel(start) or os.path.abspath(start or os.getcwd())


def project_kb_dir(start=None):
    """Путь, который предлагается как «база в корне проекта»."""
    return os.path.join(project_root(start), PROJECT_KB_RELPATH)


def kb_is_empty(directory):
    """Нет ли в директории записей.

    Признак — наличие `INC-*.md` или `SRC-*.md`, а не самой директории: пустая
    директория приезжает вместе со скиллом и выбором расположения не является.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return True
    for name in names:
        if name.upper().startswith(('INC-', 'SRC-')) and name.lower().endswith('.md'):
            return False
    return True


def resolve_kb(explicit=None, start=None):
    """(путь, источник) — разрешение пути к базе знаний.

    Порядок: `--kb` → `INCIDENT_KB_DIR` → база в корне текущего репозитория,
    **если она там уже существует** → директория внутри скилла. Третий шаг —
    не поиск базы по кандидатам, а чтение обратно уже сделанного выбора:
    директория появляется только после явного ответа пользователя. Поэтому
    здесь ничего не создаётся.
    """
    if explicit:
        return os.path.abspath(explicit), KB_FLAG
    from_env = os.environ.get(ENV_KB)
    if from_env:
        return os.path.abspath(from_env), KB_ENV
    top = git_toplevel(start)
    if top:
        candidate = os.path.join(top, PROJECT_KB_RELPATH)
        if os.path.isdir(candidate):
            return os.path.abspath(candidate), KB_PROJECT
    return os.path.abspath(DEFAULT_KB), KB_DEFAULT


def kb_dir(explicit=None):
    return resolve_kb(explicit)[0]


# --------------------------------------------------------------------------
# Очистка от секретов и персональных данных
# --------------------------------------------------------------------------
#
# Общая для всех путей, по которым текст разбора покидает разбор: запись в
# базу знаний, текст тикета, постмортем. Один список правил — единственный
# источник истины; путь, забывший её вызвать, считается дефектом (см.
# openspec/changes/add-pii-redaction).
#
# Что НЕ маскируется — сознательно, а не по недосмотру: идентификаторы
# запросов и trace id, номера заказов и транзакций, имена сервисов и хостов,
# версии, пути в коде, IP-адреса. Без них разбор бессмысленен, персональными
# данными они не являются.

# Секрет, подписанный именем поля: password=..., Authorization: Bearer ...
_SECRET_KEY = (r'(?:password|passwd|secret|token|api[_-]?key|authorization|'
              r'bearer|private[_-]?key|access[_-]?key)')
# Служебное слово, которое иногда стоит между именем поля и значением
# (`Authorization: Bearer <токен>`) — само по себе не значение, и маскировать
# его вместо токена — тот самый подтверждённый дефект.
_SECRET_CONNECTOR = r'(?:bearer|basic|token|key)'

# Токены, узнаваемые по форме значения, а не по имени поля: JWT и
# распространённые префиксы ключей API.
_TOKEN_SHAPE = (r'(?:eyJ[\w-]+\.[\w-]+\.[\w-]+|sk-[A-Za-z0-9]{16,}|'
               r'gh[oprsu]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|'
               r'xox[baprs]-[A-Za-z0-9-]{10,})')

# Порядок альтернатив — это порядок приоритета: движок берёт первую
# сработавшую на данной позиции, поэтому специфичные по форме шаблоны стоят
# раньше общих числовых. Секрет по имени поля — первым: если у значения есть
# подпись поля, она сильнее любой другой эвристики.
_PII_RE = re.compile(
    r'\b(?P<secret_field>%s)\b\s*[:=]\s*(?:%s\s+)?(?P<secret_val>\S+)'
    r'|\b(?P<urlcred_scheme>[a-zA-Z][\w+.-]*://)(?P<urlcred_user>[^\s/:@]+):(?P<urlcred_pass>[^\s/@]+)@'
    r'|(?P<token>%s)'
    r'|(?P<email>[\w.+-]+@[\w-]+\.[\w.-]+)'
    r'|(?P<snils>\d{3}-\d{3}-\d{3}[ ]\d{2})'
    r'|(?P<passport>\d{2}[ ]\d{2}[ ]\d{6})'
    r'|(?P<account>\b\d{20}\b)'
    r'|(?P<card>(?:\d{4}[ -]){3}\d{4}\b|\b\d{13,19}\b)'
    # Телефон распознаётся по характерной форме записи (код страны или
    # скобки вокруг кода города), а не по произвольному числу цифр с
    # разделителями — иначе под шаблон попадали бы номера заказов вида
    # `order-2026-0007123`.
    r'|(?P<phone>(?<!\d)(?:\+7|8)[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)'
    r'|(?<!\d)\+?1[\s.-]\d{3}[\s.-]\d{3}[\s.-]\d{4}(?!\d)'
    r'|(?<!\d)\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}(?!\d))'
    % (_SECRET_KEY, _SECRET_CONNECTOR, _TOKEN_SHAPE), re.IGNORECASE)

# Что называть пользователю в сводке срабатываний — без исходных значений.
PII_KIND_LABELS = {
    'secret': 'секрет',
    'urlcred': 'учётные данные в URL',
    'token': 'токен',
    'email': 'email',
    'card': 'карта',
    'account': 'счёт',
    'person_id': 'ид. физлица',
    'phone': 'телефон',
}


def _luhn_ok(digits):
    """Контрольная сумма Луна: отсекает числа, похожие на карту, но не карты."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scrub_text(text):
    """Маскирует секреты и персональные данные в тексте.

    Возвращает (очищенный текст, {вид: количество}). Сопоставление метки с
    исходным значением нигде не сохраняется — не обратимо. Одинаковое
    значение внутри текста маскируется одинаково: метка зависит только от
    вида данных, а не от места совпадения.
    """
    if not text:
        return text, {}
    counts = {}

    def repl(m):
        if m.group('secret_field') is not None:
            counts['secret'] = counts.get('secret', 0) + 1
            return '%s=<redacted>' % m.group('secret_field')
        if m.group('urlcred_scheme') is not None:
            counts['urlcred'] = counts.get('urlcred', 0) + 1
            return '%s<redacted>@' % m.group('urlcred_scheme')
        kind = m.lastgroup
        value = m.group(0)
        if kind == 'card':
            digits = re.sub(r'\D', '', value)
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                counts['card'] = counts.get('card', 0) + 1
                return '<card:…%s>' % digits[-4:]
            # не прошло контрольную сумму — не карта, оставляем как есть
            return value
        if kind in ('snils', 'passport'):
            counts['person_id'] = counts.get('person_id', 0) + 1
            return '<person_id>'
        counts[kind] = counts.get(kind, 0) + 1
        return '<%s>' % kind

    new_text = _PII_RE.sub(repl, text)
    return new_text, counts


def merge_scrub_counts(target, extra):
    for kind, n in (extra or {}).items():
        target[kind] = target.get(kind, 0) + n
    return target


def render_scrub_summary(counts):
    """Строка для пользователя: виды и количество, без исходных значений."""
    if not counts:
        return ''
    parts = ['%s ×%d' % (PII_KIND_LABELS.get(k, k), n)
            for k, n in sorted(counts.items())]
    return 'Очистка сработала: ' + ', '.join(parts)


# --------------------------------------------------------------------------
# Мини-парсер frontmatter (подмножество YAML, без внешних зависимостей)
# --------------------------------------------------------------------------

_KV_RE = re.compile(r'^([A-Za-z_][\w\-]*)\s*:\s*(.*)$')


def _unquote(val):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
        return val[1:-1]
    return val


def _split_top_level(text):
    """Разбивает по запятым верхнего уровня: кавычки и вложенные скобки целы.

    Нужно из-за записи карты: `source: {kind: mcp, server: kibana-mcp}` и список
    пометок `checked: [{...}, {...}]` наивным `split(',')` рвутся посередине.
    """
    parts = []
    buf = []
    depth = 0
    quote = ''
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ''
            continue
        if ch in '"\'':
            quote = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        elif ch == ',' and depth <= 0:
            parts.append(''.join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append(''.join(buf))
    return [p.strip() for p in parts if p.strip()]


def _parse_value(val):
    """Значение фронтматтера: вложенный словарь, список или скаляр."""
    val = val.strip()
    if val.startswith('{') and val.endswith('}'):
        return _parse_inline_map(val)
    if val.startswith('[') and val.endswith(']'):
        return _parse_inline_list(val)
    return _unquote(val)


def _parse_inline_map(val):
    out = {}
    for item in _split_top_level(val.strip()[1:-1]):
        key, sep, inner = item.partition(':')
        if not sep:
            continue
        out[key.strip()] = _parse_value(inner)
    return out


def _parse_inline_list(val):
    inner = val.strip()[1:-1].strip()
    if not inner:
        return []
    return [_parse_value(p) for p in _split_top_level(inner)]


def parse_frontmatter(text):
    """Возвращает (meta: dict, body: str)."""
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    head = text[3:end].strip('\n')
    body = text[end + 4:].lstrip('\n')
    meta = {}
    key = None
    for line in head.split('\n'):
        if not line.strip() or line.strip().startswith('#'):
            continue
        if line.lstrip().startswith('- ') and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(_unquote(line.lstrip()[2:]))
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == '':
            meta[key] = []
        else:
            meta[key] = _parse_value(val)
    for field in LIST_FIELDS:
        if field in meta and not isinstance(meta[field], (list, dict)):
            raw = str(meta[field])
            meta[field] = [p.strip() for p in raw.split(',') if p.strip()]
    return meta, body


def _fmt_scalar(text):
    text = str(text)
    if text == '' or any(ch in text for ch in ':#[]{},') or text != text.strip():
        return '"%s"' % text.replace('"', "'")
    return text


def _fmt_value(val):
    """Инлайн-запись значения: словарь, список или скаляр."""
    if isinstance(val, dict):
        return '{%s}' % ', '.join('%s: %s' % (k, _fmt_value(v)) for k, v in val.items())
    if isinstance(val, list):
        return '[%s]' % ', '.join(_fmt_value(v) for v in val)
    return _fmt_scalar(val)


# Списочные поля, которые не выпадают из фронтматтера пустыми: формат записи
# числит их обязательными (канон — references/kb-format.md, таблица полей), а
# отсутствие ключа неотличимо от «поле забыли». Прочие списки пустыми
# опускаются: пустой ключ ничего не сообщает, а запись читают глазами.
MANDATORY_LIST_FIELDS = ('stands', 'tags')


def dump_frontmatter(meta):
    lines = ['---']
    order = ['id', 'kind', 'title', 'date', 'stand', 'stands', 'services', 'tags',
             'severity', 'status', 'outcome', 'outcome_date', 'reuse_count', 'reused_at',
             'files', 'commits', 'related', 'signatures',
             'source', 'address', 'query', 'fields', 'confirmed', 'checked']
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    for key in keys:
        val = meta[key]
        if isinstance(val, dict):
            lines.append('%s: %s' % (key, _fmt_value(val)))
        elif isinstance(val, list):
            if not val and key not in MANDATORY_LIST_FIELDS:
                continue
            if any(isinstance(v, (dict, list)) for v in val):
                # список пометок карты: разбивать его на блок незачем — он
                # читается целиком и правится целиком
                lines.append('%s: %s' % (key, _fmt_value(val)))
            elif key == 'signatures' or any(len(str(v)) > 40 or ',' in str(v) for v in val):
                lines.append('%s:' % key)
                for item in val:
                    lines.append('  - "%s"' % str(item).replace('"', "'"))
            else:
                lines.append('%s: [%s]' % (key, ', '.join(str(v) for v in val)))
        else:
            lines.append('%s: %s' % (key, _fmt_scalar(val)))
    lines.append('---')
    return '\n'.join(lines)


def split_sections(body):
    """Возвращает {заголовок: текст} по разделам '## '."""
    out = {}
    current = None
    buf = []
    for line in body.split('\n'):
        if line.startswith('## '):
            if current:
                out[current] = '\n'.join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current:
        out[current] = '\n'.join(buf).strip()
    return out


# --------------------------------------------------------------------------
# Загрузка записей
# --------------------------------------------------------------------------


def load_incidents(directory=None):
    directory = kb_dir(directory)
    items = []
    if not os.path.isdir(directory):
        return items
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.md') or name.upper().startswith('README'):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                text = fh.read()
        except OSError as exc:
            print('warning: %s: %s' % (path, exc), file=sys.stderr)
            continue
        meta, body = parse_frontmatter(text)
        if not meta.get('id'):
            meta['id'] = os.path.splitext(name)[0]
        if not meta.get('title'):
            head = re.search(r'^#\s+(.+)$', body, re.MULTILINE)
            meta['title'] = head.group(1).strip() if head else meta['id']
        items.append({'meta': meta, 'body': body, 'path': path,
                      'sections': split_sections(body)})
    return items


def index_is_stale(directory=None):
    """(устарел ли индекс, причина).

    Индекс — производная от markdown-файлов, и правят их руками: добавить запись,
    поправить причину, удалить дубль. Поэтому перед тем как верить индексу, надо
    убедиться, что он описывает те же файлы и не старше их.
    """
    directory = kb_dir(directory)
    path = os.path.join(directory, 'index.json')
    if not os.path.exists(path):
        return True, 'индекса нет'
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return True, 'индекс не читается'
    listed = {e.get('file') for e in index.get('incidents') or []}
    on_disk = set()
    newest = 0.0
    try:
        names = os.listdir(directory)
    except OSError:
        return True, 'база знаний не читается'
    for name in names:
        if not name.endswith('.md') or name.upper().startswith('README'):
            continue
        on_disk.add(name)
        try:
            newest = max(newest, os.path.getmtime(os.path.join(directory, name)))
        except OSError:
            pass
    if listed != on_disk:
        return True, 'состав записей изменился'
    source_mtime = index.get('source_mtime')
    if not isinstance(source_mtime, (int, float)):
        return True, 'индекс собран старой версией kb_index.py'
    if newest > source_mtime:
        return True, 'запись правили после сборки индекса'
    return False, None


def load_from_index(directory=None):
    """Записи базы знаний из индекса — без чтения markdown-файлов.

    Возвращает None, если индекса нет или он устарел: тогда вызывающий код
    читает markdown, а пользователю говорится, что индекс стоит пересобрать.
    """
    directory = kb_dir(directory)
    stale, _ = index_is_stale(directory)
    if stale:
        return None
    try:
        with open(os.path.join(directory, 'index.json'), 'r', encoding='utf-8') as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return None
    items = []
    for entry in index.get('incidents') or []:
        sections = entry.get('sections')
        if sections is None:      # индекс собран старой версией kb_index.py
            return None
        meta = {k: v for k, v in entry.items() if k not in ('sections', 'file')}
        items.append({'meta': meta, 'body': '', 'sections': sections,
                      'path': os.path.join(directory, entry.get('file') or '')})
    return items


def load_incidents_fast(directory=None):
    """(записи, предупреждение) — через индекс, если он актуален."""
    items = load_from_index(directory)
    if items is not None:
        return items, None
    stale, reason = index_is_stale(directory)
    warning = None
    if stale and reason != 'индекса нет':
        warning = 'индекс базы знаний устарел (%s) — поиск идёт по markdown; ' \
                  'пересобрать: kb_index.py' % reason
    return load_incidents(directory), warning


def next_id(incidents, when=None):
    when = when or now()
    prefix = 'INC-%04d-%02d' % (when.year, when.month)
    used = []
    for inc in incidents:
        m = re.match(r'INC-(\d{4})-(\d{2})-(\d+)', str(inc['meta'].get('id', '')))
        if m and '%s-%s' % (m.group(1), m.group(2)) == '%04d-%02d' % (when.year, when.month):
            used.append(int(m.group(3)))
    return '%s-%03d' % (prefix, (max(used) + 1) if used else 1)


def source_entry_id(stand, service):
    """Идентификатор записи карты по паре «стенд + сервис».

    Он вычислимый, а не порядковый: одна и та же пара всегда даёт один id, и
    повторная запись обновляет существующую запись вместо создания дубля.
    """
    return 'SRC-%s-%s' % (slugify(stand, 24), slugify(service, 24))


def slugify(text, limit=40):
    translit = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
        'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    out = []
    for ch in text.lower():
        if ch in translit:
            out.append(translit[ch])
        elif ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        else:
            out.append('-')
    slug = re.sub(r'-{2,}', '-', ''.join(out)).strip('-')
    return slug[:limit].strip('-') or 'incident'


# --------------------------------------------------------------------------
# Токенизация и скоринг
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r'[0-9a-zA-Zа-яёА-ЯЁ_]{2,}', re.UNICODE)

STOP = {
    'the', 'and', 'for', 'not', 'was', 'are', 'this', 'that', 'with', 'from',
    'при', 'для', 'что', 'как', 'это', 'все', 'или', 'его', 'она', 'они',
    'был', 'была', 'было', 'быть', 'есть', 'нет', 'там', 'тут', 'наш', 'мы',
    'на', 'по', 'из', 'до', 'за', 'об', 'же', 'ли', 'но', 'то', 'бы',
}

# окончания, которые режем для грубой нормализации русских словоформ
RU_SUFFIXES = ('ами', 'ями', 'ого', 'ему', 'ому', 'ыми', 'ими', 'ах', 'ях',
               'ов', 'ев', 'ый', 'ий', 'ая', 'яя', 'ое', 'ее', 'ые', 'ие',
               'ам', 'ям', 'ом', 'ем', 'ой', 'ей', 'ую', 'юю', 'ть', 'ла',
               'ли', 'ло', 'на', 'а', 'я', 'ы', 'и', 'у', 'ю', 'е', 'о', 'ь')


def stem(word):
    word = word.lower()
    if len(word) <= 4:
        return word
    if re.search(r'[а-яё]', word):
        for suf in RU_SUFFIXES:
            if word.endswith(suf) and len(word) - len(suf) >= 4:
                return word[:-len(suf)]
        return word
    if len(word) > 6:
        for suf in ('ing', 'tion', 'ed', 'es', 's'):
            if word.endswith(suf) and len(word) - len(suf) >= 4:
                return word[:-len(suf)]
    return word


def tokenize(text, keep_stop=False):
    out = []
    for word in TOKEN_RE.findall(str(text or '')):
        low = word.lower()
        if not keep_stop and (low in STOP or len(low) < 3):
            continue
        out.append(stem(low))
    return out


def norm_signature(sig):
    """Приводит сигнатуру к сравнимому виду."""
    text = str(sig or '').strip().lower()
    if text.startswith('tmpl:'):
        text = text[5:]
    text = re.sub(r'<\w+>', '<v>', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' .,:;|')


def signature_similarity(a, b):
    """0..1 — насколько похожи две сигнатуры."""
    na, nb = norm_signature(a), norm_signature(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    ta, tb = set(tokenize(na, keep_stop=True)), set(tokenize(nb, keep_stop=True))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    return inter / float(len(ta | tb))


def load_parsed(path):
    """Читает JSON-вывод parse_logs.py."""
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def signatures_from_parsed(parsed):
    return [s.get('value') for s in parsed.get('signatures', []) if s.get('value')]
