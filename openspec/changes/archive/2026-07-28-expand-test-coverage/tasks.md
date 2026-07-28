## 1. Хелперы

- [x] 1.1 `ScriptCase.tmpdir()` — временная директория с очисткой; восемь копий `setUp`
      переводятся на неё
- [x] 1.2 `helpers.json_to_file`; `parsed_to_file` — частный случай; три копии убраны
- [x] 1.3 Убрать дублирование `ScriptCase.run_script`/`json_of` ↔ `helpers.run`/`run_json`
- [x] 1.4 `helpers.make_repo(tmpdir)` — детерминированный git-репозиторий с известным
      классом исключения

## 2. Новые тесты

- [x] 2.1 `test_code_hints.py`: сигнатура находит файл и место; `--repo /nonexistent` и
      директория без `.git` — объяснение, не трассировка
- [x] 2.2 `test_kb_common.py`: round-trip `parse_frontmatter` → `dump_frontmatter`;
      `signature_similarity` на похожих/непохожих парах
- [x] 2.3 Тесты `resolve_kb`: четыре ступени приоритета; ступень проекта — только для
      существующей директории и без создания
- [x] 2.4 Тесты `kb_add`: реальная запись, обратное чтение, индекс, следующий id в том же
      месяце, чужие записи не тронуты

## 3. Чистка набора

- [x] 3.1 Удалить тесты-копии эталонов: `test_year_for_timestamp_without_year`,
      `test_no_dominant_error_template`, `test_no_incident_signatures`,
      `test_confidence_stays_low`
- [x] 3.2 Свести четыре теста детерминизма к одному с subTest; добавить в него
      `kb_search` и `confidence`
- [x] 3.3 `tests/run.py`: печать несовместимости — только в `check_compat`; прогон на
      интерпретаторе ниже 3.8 помечается неполным в stderr

## 4. CI

- [x] 4.1 Матрица `['3.8', '3.12']`
- [x] 4.2 `permissions: contents: read`, `timeout-minutes`, `concurrency` с отменой
      устаревших прогонов

## 5. Завершение

- [x] 5.1 Прогнать `python3 tests/run.py` на 3.8 и на современном интерпретаторе
- [x] 5.2 README: раздел о проверках — если изменился состав, поправить
- [x] 5.3 Прогнать `openspec validate` по изменению
