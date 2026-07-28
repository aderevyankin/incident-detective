# Отчёт автономного разбора — состав полей

`report.json` в директории `--out` — единственный машинный выход автономного режима.
Его читает обвязка, а не человек в чате: свободный текст остаётся для человека,
машинный контракт — для обвязки.

Файл собирает `triage.py`; поле `kb_entry` дописывает `kb_add.py --report`.
Отсутствие файла означает **несостоявшийся прогон** — это не то же самое, что вердикт
«данных недостаточно», который лежит внутри файла.

## Поля

| Поле | Тип | Что в нём |
|---|---|---|
| `schema` | строка | версия формата, `incident-detective/report@1` |
| `generated_at` | время | когда собран отчёт (из `INCIDENT_NOW`, шкала UTC) |
| `mode` | `auto` / `interactive` | режим, в котором шёл разбор |
| `incident` | объект | стенд, сервис, окно разбора, симптом и идентификатор алерта; `time_source` — откуда взято время инцидента: `alert` или `now` |
| `verdict` | строка | `подтверждено данными`, `вероятная причина`, `гипотеза` или `данных недостаточно` |
| `confidence` | число / `null` | итог `confidence.py`; `null` — оценивать было нечего |
| `insufficient` | булево | истина, когда вердикт — «данных недостаточно» |
| `missing` | список строк | чего не хватило: непройденные контуры с причинами, поля, которых не было в алерте, названное флагом `--missing` |
| `claim` | строка / `null` | формулировка вывода, которую оценивали (`--claim`) |
| `signature` | строка / `null` | ведущая сигнатура ошибки — «валюта» всех трёх контуров |
| `signatures` | список строк | остальные извлечённые сигнатуры |
| `evidence` | список объектов | доказательства; **у каждого есть `source`** — файл логов, место в коде или id записи базы знаний |
| `contours` | список объектов | `key`, `title`, `passed`, `reason`: что прошло, что пропущено и почему |
| `kb_entry` | объект | `written`, `id`, `path`, `action`, `reason` — что стало с записью базы знаний |
| `next_step` | объект | черновик следующего шага: `kind` (`fix` / `bug` / `task`), `title`, `body`, `why` |
| `environment` | объект | найденная версия `python`, наличие `git`, признак ограниченного режима |
| `artifacts` | объект | директория прогона, пути JSON этапов, телеметрия (`trace_file`) и поток сессии (`session_file`) |
| `stopped_at` | строка / `null` | на каком шаге разбор остановлен — например по потолку прохода |

## Как читает обвязка

```
файла нет                      → прогон не состоялся, смотреть session.json
verdict = данных недостаточно  → честный отказ, в missing перечислено недостающее
verdict = гипотеза             → тикет на доисследование, чинить нечего
verdict = вероятная причина    → баг с текущим уровнем уверенности
verdict = подтверждено данными → есть причина и место в коде, next_step.kind = fix
```

Ни правки, ни тикет автономный разбор не создаёт: `next_step` — черновик, решение
принимает человек или обвязка.

## Пример

```json
{
  "schema": "incident-detective/report@1",
  "generated_at": "2026-07-28 16:35:00",
  "mode": "auto",
  "incident": {
    "stand": "stage",
    "service": "payment-api",
    "since": "2026-07-28 16:05:03",
    "until": "2026-07-28 16:35:03",
    "time_scale": "UTC",
    "symptom": "5xx на оплате выше 10% пять минут подряд",
    "alert_id": "9f2c1a44b7e0",
    "alert_format": "alertmanager",
    "started_at": "2026-07-28 16:20:03",
    "time_source": "alert"
  },
  "verdict": "вероятная причина",
  "confidence": 0.52,
  "insufficient": false,
  "missing": [],
  "signature": "ConnectionTimeoutError",
  "evidence": [
    {
      "contour": "logs",
      "source": "payment.log",
      "detail": "ConnectionTimeoutError: timed out waiting for connection from pool after <n> ms",
      "count": 12,
      "level": "ERROR",
      "first": "2026-07-28 16:20:03",
      "last": "2026-07-28 16:26:41"
    },
    {
      "contour": "kb",
      "source": "INC-2026-07-001",
      "detail": "payment-api отдаёт таймауты: исчерпан пул соединений к базе",
      "score": 51.83
    },
    {
      "contour": "code",
      "source": "src/db/pool.py:42",
      "detail": "raise ConnectionTimeoutError()",
      "commit": "a1b2c3d"
    }
  ],
  "contours": [
    {"key": "logs", "title": "Логи", "passed": true, "reason": null},
    {"key": "kb", "title": "База знаний", "passed": true, "reason": null},
    {"key": "code", "title": "Код", "passed": true, "reason": null},
    {"key": "confidence", "title": "Уверенность", "passed": true, "reason": null}
  ],
  "kb_entry": {
    "written": true,
    "id": "INC-2026-07-002",
    "path": "/srv/incident-kb/INC-2026-07-002-payment-pool.md",
    "action": "дополнена",
    "reason": null
  },
  "next_step": {
    "kind": "bug",
    "title": "Баг: разбор инцидента: payment-api stage",
    "body": "Причина установлена на уровне «вероятная причина» (src/db/pool.py:42).",
    "why": "уровень «вероятная причина» — чинить рано, но баг обоснован"
  },
  "environment": {"python": "3.11.4", "limited": false, "git": true},
  "artifacts": {
    "out_dir": "/srv/incident-runs/9f2c1a44b7e0",
    "report": "/srv/incident-runs/9f2c1a44b7e0/report.json",
    "stages": {"logs": "/srv/incident-runs/9f2c1a44b7e0/parsed.json"},
    "trace_file": "/srv/incident-runs/9f2c1a44b7e0/calls.log",
    "session_file": "/srv/incident-runs/9f2c1a44b7e0/session.json"
  },
  "stopped_at": null
}
```

## Ограниченный режим

Пригодного `python3` нет — `triage.py` не запускается, и отчёт собрать нечем. Тогда
отчёт пишется по этому же составу полей вручную, с двумя обязательными отличиями:
`environment.limited = true`, а `verdict` **не выше `гипотезы`**. Контур, пройденный
чтением глазами, сошедшимся не считается.
