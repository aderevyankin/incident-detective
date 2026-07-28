#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Алерт системы оповещения → карточка инцидента и аргументы разбора.

Автономный разбор начинается не с сообщения человека, а с payload'а, который
прислала обвязка. Разбор такого payload'а детерминирован: поля лежат по
известным путям, и поручать их извлечение модели незачем — она добавит сюда
только шанс ошибиться.

  python3 alert_to_incident.py < alert.json
  python3 alert_to_incident.py --file alert.json --window 30
  python3 alert_to_incident.py --format args < alert.json    # хвост для triage.py

Известные форматы: Alertmanager (Prometheus), Grafana (legacy и unified),
Sentry. Формат не распознан — ненулевой код и сообщение, а не догадка о полях.
Поле отсутствует — оно отмечено в `missing`, а не заполнено значением по
умолчанию: подставленный «prod» в карточке дороже пустого места.

Коды возврата:
  0  карточка собрана (часть полей может быть отмечена как отсутствующая)
  3  формат payload'а не распознан
  4  ни стенда, ни сервиса из алерта не вышло — разбирать нечего
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

from kb_common import (  # noqa: E402
    dump_json, now, run_script, scrub_text,
)

RC_UNKNOWN_FORMAT = 3
RC_INCOMPLETE = 4

# Окно разбора вокруг времени инцидента — то же ±15 минут, которым сужается
# выгрузка логов в диалоговом разборе.
DEFAULT_WINDOW_MIN = 15

TIME_FMT = '%Y-%m-%d %H:%M:%S'

# Метки, под которыми в алертах ездит стенд. `namespace` и `cluster` сюда не
# входят намеренно: это не стенд, а место запуска, и подстановка одного вместо
# другого — ровно та догадка, которой здесь быть не должно.
STAND_KEYS = ('stand', 'env', 'environment', 'deployment_environment')
SERVICE_KEYS = ('service', 'service_name', 'app', 'application', 'job',
                'component', 'project')

# Написания стендов, приводимые к принятым в разборе. Это нормализация
# известного значения, а не подстановка отсутствующего.
STAND_ALIASES = {
    'production': 'prod', 'prd': 'prod',
    'staging': 'stage', 'stg': 'stage',
    'testing': 'test', 'qa': 'test',
    'development': 'dev', 'devel': 'dev',
}


# --- время ----------------------------------------------------------------


def parse_alert_time(value):
    """Время из алерта → naive UTC. None, если разобрать нечего.

    Алерты приходят с зоной (`...Z`, `+03:00`) или числом секунд эпохи. Шкала
    вывода одна — UTC, как у всего остального разбора.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.utcfromtimestamp(float(value))
    text = str(value).strip()
    if not text:
        return None
    # Alertmanager так пишет «времени нет»
    if text.startswith('0001-01-01'):
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    # доли секунды длиннее шести знаков fromisoformat в 3.8 не берёт
    if '.' in text:
        head, _, tail = text.partition('.')
        digits = ''
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        rest = tail[len(digits):]
        text = head + ('.' + digits[:6] if digits else '') + rest
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


# --- извлечение полей -----------------------------------------------------


def pick(mapping, keys):
    """Первое непустое значение по перечню ключей, регистр не важен."""
    if not isinstance(mapping, dict):
        return None
    lower = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        val = lower.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def norm_stand(value):
    if not value:
        return None
    text = str(value).strip().lower()
    return STAND_ALIASES.get(text, text)


def sentry_tags(event):
    """Теги Sentry приходят и списком пар, и словарём — приводим к словарю."""
    tags = event.get('tags')
    if isinstance(tags, dict):
        return dict(tags)
    out = {}
    for item in tags or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out[str(item[0])] = item[1]
    return out


def detect(payload):
    """Имя формата или None. Порядок проверок — от частного к общему."""
    if not isinstance(payload, dict):
        return None
    if 'evalMatches' in payload or 'ruleName' in payload or 'ruleUrl' in payload:
        return 'grafana'
    if isinstance(payload.get('alerts'), list) and payload['alerts']:
        # unified alerting Grafana шлёт тот же конверт, что Alertmanager, и
        # отличается только своими полями — по ним и различаем
        if 'orgId' in payload or 'ruleUrl' in payload:
            return 'grafana'
        return 'alertmanager'
    if 'culprit' in payload or 'event_id' in payload:
        return 'sentry'
    data = payload.get('data')
    if isinstance(data, dict) and isinstance(data.get('event'), dict):
        return 'sentry'
    if isinstance(payload.get('event'), dict) and (
            'event_id' in payload['event'] or 'tags' in payload['event']):
        return 'sentry'
    return None


def from_alertmanager(payload):
    alerts = [a for a in payload.get('alerts') or [] if isinstance(a, dict)]
    firing = [a for a in alerts if str(a.get('status') or 'firing').lower() == 'firing']
    alert = (firing or alerts)[0]
    labels = dict(payload.get('commonLabels') or {})
    labels.update({k: v for k, v in (alert.get('labels') or {}).items()})
    ann = dict(payload.get('commonAnnotations') or {})
    ann.update({k: v for k, v in (alert.get('annotations') or {}).items()})
    return {
        'stand': pick(labels, STAND_KEYS),
        'service': pick(labels, SERVICE_KEYS),
        'started_at': parse_alert_time(alert.get('startsAt')),
        'signature': pick(labels, ('alertname',)),
        'severity': pick(labels, ('severity',)),
        'symptom': pick(ann, ('summary', 'description', 'message')),
        'alert_id': (str(alert.get('fingerprint') or '').strip()
                     or str(payload.get('groupKey') or '').strip() or None),
        'alerts_total': len(alerts),
    }


def from_grafana(payload):
    if isinstance(payload.get('alerts'), list) and payload['alerts']:
        # unified alerting — конверт Alertmanager, разбирается им же
        card = from_alertmanager(payload)
        card['signature'] = card['signature'] or str(payload.get('title') or '').strip() or None
        return card
    tags = dict(payload.get('tags') or {})
    for match in payload.get('evalMatches') or []:
        if isinstance(match, dict) and isinstance(match.get('tags'), dict):
            for key, val in match['tags'].items():
                tags.setdefault(key, val)
    return {
        'stand': pick(tags, STAND_KEYS),
        'service': pick(tags, SERVICE_KEYS),
        # legacy-вебхук Grafana времени не присылает вовсе — это не ошибка
        # формата, а его свойство: окно строится от INCIDENT_NOW
        'started_at': parse_alert_time(payload.get('startsAt') or payload.get('time')),
        'signature': (str(payload.get('ruleName') or '').strip()
                      or str(payload.get('title') or '').strip() or None),
        'severity': pick(tags, ('severity',)),
        'symptom': (str(payload.get('message') or '').strip()
                    or str(payload.get('title') or '').strip() or None),
        'alert_id': (str(payload.get('ruleId') or '').strip()
                     or str(payload.get('ruleName') or '').strip() or None),
        'alerts_total': 1,
    }


def from_sentry(payload):
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    event = data.get('event') or payload.get('event') or payload
    if not isinstance(event, dict):
        event = {}
    tags = sentry_tags(event)
    meta = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    stand = pick(tags, STAND_KEYS) or pick(event, ('environment',))
    service = (pick(tags, SERVICE_KEYS)
               or str(payload.get('project') or '').strip() or None
               or pick(event, ('project',)))
    signature = (str(meta.get('type') or '').strip() or None)
    if not signature:
        exc = event.get('exception')
        values = exc.get('values') if isinstance(exc, dict) else None
        if isinstance(values, list) and values and isinstance(values[0], dict):
            signature = str(values[0].get('type') or '').strip() or None
    symptom = (str(event.get('title') or '').strip()
               or str(meta.get('value') or '').strip()
               or str(payload.get('message') or '').strip()
               or str(event.get('culprit') or payload.get('culprit') or '').strip() or None)
    return {
        'stand': stand,
        'service': service,
        'started_at': parse_alert_time(event.get('timestamp') or event.get('datetime')
                                       or payload.get('datetime')),
        'signature': signature,
        'severity': (str(event.get('level') or payload.get('level') or '').strip() or None),
        'symptom': symptom,
        'alert_id': (str(event.get('event_id') or payload.get('id') or '').strip() or None),
        'alerts_total': 1,
    }


READERS = {
    'alertmanager': from_alertmanager,
    'grafana': from_grafana,
    'sentry': from_sentry,
}


# --- карточка -------------------------------------------------------------


class UnknownFormat(Exception):
    """Payload не похож ни на один известный формат."""


def build_card(payload, window_min):
    fmt = detect(payload)
    if fmt is None:
        raise UnknownFormat()
    raw = READERS[fmt](payload)

    started = raw.get('started_at')
    if started is None:
        # времени в алерте нет — окно строится вокруг «сейчас», и в карточке
        # написано, откуда оно взято: читатель отчёта не должен гадать
        started = now()
        time_source = 'now'
    else:
        time_source = 'alert'

    delta = timedelta(minutes=window_min)
    stand = norm_stand(raw.get('stand'))
    service = raw.get('service')
    symptom, _ = scrub_text(raw.get('symptom') or '')

    missing = []
    if not stand:
        missing.append('стенд')
    if not service:
        missing.append('сервис')
    if time_source == 'now':
        missing.append('время инцидента')

    card = {
        'format': fmt,
        'alert_id': raw.get('alert_id'),
        'alerts_total': raw.get('alerts_total'),
        'stand': stand,
        'service': service,
        'symptom': symptom or None,
        'signature': raw.get('signature'),
        'severity': raw.get('severity'),
        'started_at': started.strftime(TIME_FMT),
        'time_source': time_source,
        'time_scale': 'UTC',
        'since': (started - delta).strftime(TIME_FMT),
        'until': (started + delta).strftime(TIME_FMT),
        'window_minutes': window_min,
        'missing': missing,
        'sufficient': bool(stand or service),
    }
    card['triage_args'] = triage_args(card)
    return card


def triage_args(card):
    """Хвост команды `triage.py` — только то, что действительно известно."""
    args = []
    if card.get('stand'):
        args.extend(['--stand', card['stand']])
    if card.get('service'):
        args.extend(['--service', card['service']])
    args.extend(['--since', card['since'], '--until', card['until']])
    if card.get('signature'):
        args.extend(['--query', card['signature']])
    return args


def shell_quote(text):
    if text and all(ch.isalnum() or ch in '-_./:=' for ch in text):
        return text
    return "'%s'" % str(text).replace("'", "'\\''")


def render_md(card, out):
    w = out.write
    w('# Карточка инцидента из алерта (%s)\n\n' % card['format'])
    rows = [
        ('стенд', card['stand']), ('сервис', card['service']),
        ('симптом', card['symptom']), ('сигнатура', card['signature']),
        ('уровень', card['severity']),
        ('время инцидента', '%s %s (источник: %s)'
         % (card['started_at'], card['time_scale'],
            'алерт' if card['time_source'] == 'alert' else 'INCIDENT_NOW')),
        ('окно разбора', '%s — %s' % (card['since'], card['until'])),
        ('идентификатор алерта', card['alert_id']),
    ]
    for name, value in rows:
        w('- **%s**: %s\n' % (name, value if value else '_нет в алерте_'))
    if card['missing']:
        w('\nНет в алерте: %s. Значения по умолчанию не подставлены.\n'
          % ', '.join(card['missing']))
    w('\n```\n%s\n```\n' % ' '.join(shell_quote(a) for a in card['triage_args']))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Payload системы оповещения → карточка инцидента и аргументы разбора.')
    ap.add_argument('--file', help='файл с payload (по умолчанию — стандартный ввод)')
    ap.add_argument('--window', type=int, default=DEFAULT_WINDOW_MIN,
                    help='половина окна разбора в минутах (%d)' % DEFAULT_WINDOW_MIN)
    ap.add_argument('--format', choices=['json', 'md', 'args'], default='json')
    args = ap.parse_args(argv)

    if args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as fh:
                text = fh.read()
        except OSError as exc:
            raise SystemExit('Не прочитал %s: %s' % (args.file, exc))
    else:
        text = sys.stdin.read()

    if not text.strip():
        sys.stderr.write('На вход не пришло ничего: ожидается JSON алерта.\n')
        return RC_UNKNOWN_FORMAT
    try:
        payload = json.loads(text)
    except ValueError as exc:
        sys.stderr.write('Payload не разобрался как JSON (%s). Формат не распознан — '
                         'поля угадывать не буду.\n' % exc)
        return RC_UNKNOWN_FORMAT

    try:
        card = build_card(payload, args.window)
    except UnknownFormat:
        sys.stderr.write(
            'Формат payload не распознан: известны Alertmanager, Grafana и Sentry. '
            'Поля карточки угадывать не буду — передай разбор в диалоговом режиме '
            'или добавь формат в alert_to_incident.py.\n')
        return RC_UNKNOWN_FORMAT

    if args.format == 'json':
        dump_json(card, sys.stdout)
    elif args.format == 'args':
        sys.stdout.write('%s\n' % ' '.join(shell_quote(a) for a in card['triage_args']))
    else:
        render_md(card, sys.stdout)

    if not card['sufficient']:
        sys.stderr.write(
            'Из алерта не вышло ни стенда, ни сервиса — карточка неполная. '
            'В автономном режиме это вердикт «данных недостаточно», а не повод '
            'подставить значения.\n')
        return RC_INCOMPLETE
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
