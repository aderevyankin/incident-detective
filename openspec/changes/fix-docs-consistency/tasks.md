## 1. Фактические правки (не ждут остального)

- [x] 1.1 README: `runtime-preflight` в таблице capability — уже добавлен ранее
- [x] 1.2 `references/next-step.md`: «уровня уверенности шага 6» → «шага 4»
- [x] 1.3 README: `kb-entry-example.md` в перечне `templates/`, `kb/README.md` в дереве
- [x] 1.4 AI_CONTEXT: все шесть команд `/opsx:*`
- [x] 1.5 README и AI_CONTEXT: один способ вызова `openspec validate`

## 2. Канонические дома

- [x] 2.1 Порядок пути к базе: полный текст — только в `references/kb-format.md`;
      README, AI_CONTEXT, SKILL.md, `kb/README.md` — ссылка + одна строка сути
- [x] 2.2 Команда предпроверки: единственная версия в `references/limited-mode.md`
      (с вызовом `kb_search.py` — свежая из SKILL.md); SKILL.md ссылается
- [x] 2.3 «Границы»: полный текст в SKILL.md; README и AI_CONTEXT — сжатая версия со
      ссылкой
- [x] 2.4 `references/onboarding.md` и `references/limited-mode.md`: куски,
      пересказывающие SKILL.md, заменить ссылками
- [x] 2.5 `openspec/config.yaml`: таблицу контуров и принципы сократить до ссылки на
      SKILL.md
- [x] 2.6 Витринные копии в README пометить ссылкой на канон

## 3. Производные копии

- [x] 3.1 Проверка `.claude/skills` ↔ `.qwen/skills` в `tests/run.py`; падение называет
      файл
- [x] 3.2 Сверка тел промптов `.claude/commands/opsx/` ↔ `.qwen/commands/`
- [x] 3.3 README/AI_CONTEXT: одна фраза, что `.claude/`/`.qwen/` — оснастка разработки,
      а не поставка скилла

## 4. Завершение

- [x] 4.1 Перечитать README целиком после правок — связность и отсутствие осиротевших
      ссылок
- [x] 4.2 Прогнать `python3 tests/run.py`
- [x] 4.3 Прогнать `openspec validate fix-docs-consistency --strict`
