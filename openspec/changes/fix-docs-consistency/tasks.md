## 1. Фактические правки (не ждут остального)

- [ ] 1.1 README: добавить `runtime-preflight` в таблицу capability
- [ ] 1.2 `references/next-step.md`: «уровня уверенности шага 6» → «шага 4»
- [ ] 1.3 README: `kb-entry-example.md` в перечне `templates/`, `kb/README.md` в дереве
- [ ] 1.4 AI_CONTEXT: все шесть команд `/opsx:*`
- [ ] 1.5 README и AI_CONTEXT: один способ вызова `openspec validate`

## 2. Канонические дома

- [ ] 2.1 Порядок пути к базе: полный текст — только в `references/kb-format.md`;
      README, AI_CONTEXT, SKILL.md, `kb/README.md` — ссылка + одна строка сути
- [ ] 2.2 Команда предпроверки: единственная версия в `references/limited-mode.md`
      (с вызовом `kb_search.py` — свежая из SKILL.md); SKILL.md ссылается
- [ ] 2.3 «Границы»: полный текст в SKILL.md; README и AI_CONTEXT — сжатая версия со
      ссылкой
- [ ] 2.4 `references/onboarding.md` и `references/limited-mode.md`: куски,
      пересказывающие SKILL.md, заменить ссылками
- [ ] 2.5 `openspec/config.yaml`: таблицу контуров и принципы сократить до ссылки на
      SKILL.md
- [ ] 2.6 Витринные копии в README пометить ссылкой на канон

## 3. Производные копии

- [ ] 3.1 Проверка `.claude/skills` ↔ `.qwen/skills` в `tests/run.py`; падение называет
      файл
- [ ] 3.2 Сверка тел промптов `.claude/commands/opsx/` ↔ `.qwen/commands/`
- [ ] 3.3 README/AI_CONTEXT: одна фраза, что `.claude/`/`.qwen/` — оснастка разработки,
      а не поставка скилла

## 4. Завершение

- [ ] 4.1 Перечитать README целиком после правок — связность и отсутствие осиротевших
      ссылок
- [ ] 4.2 Прогнать `python3 tests/run.py`
- [ ] 4.3 Прогнать `openspec validate` по изменению (после архивации
      `add-readme-maintenance`)
