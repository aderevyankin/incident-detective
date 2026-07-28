## 1. Общие функции в kb_common

- [ ] 1.1 Перенести `parse_time_arg` (с относительными окнами) в `kb_common`; реэкспорт из
      `parse_logs` для внутренних вызовов
- [ ] 1.2 `fit_by_render(items, render, budget, reserve)` — общая укладка вывода в бюджет
- [ ] 1.3 `dump_json` с `default=str` и едиными параметрами
- [ ] 1.4 `require_python()` — преамбула проверки версии
- [ ] 1.5 Перенести `EXC_RE` (полный вариант), `LEVELS`, `LEVEL_ORD`, `DEFAULT_MAX_LINES`

## 2. Переход скриптов

- [ ] 2.1 `timeline.py`: `parse_dt` → общий разбор времени; `--since 1h` работает
- [ ] 2.2 `code_hints.py`: инлайновый `strptime` → общий разбор; трейсбека нет
- [ ] 2.3 `trace.py` и `timeline.py`: бюджет `MAX_SUMMARY_CHARS` с файлом полного
      результата; `chain_to_json` удалить в пользу `dump_json`
- [ ] 2.4 `triage.py`: `fit_output` без гарантии «первый раздел целиком» — предел
      `--max-chars` соблюдается всегда
- [ ] 2.5 `code_hints.limit_frames`: без мутации `data`; `frames_hidden` есть и в JSON
- [ ] 2.6 Выделить в `kb_search` функции `query_from_parsed` / `rank`; `triage.py`
      вызывает их; фильтр `access` — только в `query_from_parsed`
- [ ] 2.7 `confidence.py`, `triage.py`: константы из `kb_common`, без импорта всего
      `parse_logs`
- [ ] 2.8 Преамбула девяти скриптов → две строки
- [ ] 2.9 Единый формат `warning:` в stderr

## 3. Префильтр

- [ ] 3.1 Отключить отсев по уровню для строк, начинающихся с `{`
- [ ] 3.2 Фикстура: JSON-строки со словом `debug` в тексте и `"level":"error"` в поле;
      `--level ERROR` их не теряет
- [ ] 3.3 Прогнать `tools/bench.py` — бюджет скорости не просел

## 4. Мёртвый код

- [ ] 4.1 Удалить `is_record_start` (`parse_logs`), `index_path` (`kb_common`),
      неиспользуемые импорты `defaultdict`
- [ ] 4.2 Удалить бессмысленный вызов `score_trace(None)` и мёртвую ветку dict в
      `confidence`
- [ ] 4.3 Удалить поле `symptoms` из индекса (никто не читает)
- [ ] 4.4 `bench.py`: удалить `ORDER` и ветку «triage.py ещё не реализован»

## 5. bench.py

- [ ] 5.1 Изоляция окружения по образцу `tests/helpers.script_env`
- [ ] 5.2 Замер `code_hints` на сгенерированном репозитории вместо живого
- [ ] 5.3 Валидация ключей `--only` по `BUDGETS`
- [ ] 5.4 Свести три копии «посчитай, если нет файла» к `ensure(path, cmd)`

## 6. Проверки и документация

- [ ] 6.1 Тест: `--since 1h` во всех скриптах с этим флагом даёт одинаковое окно
- [ ] 6.2 Тест: вывод `trace`/`timeline` на большом входе укладывается в бюджет
- [ ] 6.3 Тест: `triage --max-chars` не превышается при огромном разделе логов
- [ ] 6.4 Прогнать `python3 tests/run.py` и `tools/bench.py`
- [ ] 6.5 README/AI_CONTEXT: если изменились команды или соглашения — поправить
- [ ] 6.6 Прогнать `openspec validate` по изменению
