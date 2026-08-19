# Post-Audit Log — Trello Connector

Формат и правила ведения: см. `/Users/vladivanco/Documents/Imperal OS/POST_AUDIT_LOG_STANDARD.md`.
Новые записи добавляются СВЕРХУ.

---

## 2026-08-19 — Сквозной пост-аудит + исправление double-prompt бага в delete_board

**Что проверялось:** py_compile всех 10 модулей; количество `@chat.function`
(62, совпадает с манифестом); наличие поля `pricing` (уже присутствует,
не тронуто); классификация `action_type` каждой из 6 `delete_*` функций
(`delete_card`, `delete_attachment`, `delete_comment`, `delete_check_item`,
`delete_checklist`, `delete_label`, `delete_board`, `delete_custom_field`,
`delete_workspace`) на предмет соответствия реальной обратимости операции;
double-prompt антипаттерн (ручное поле `confirm*` рядом с уже корректным
`action_type="destructive"`); полный прогон тестового набора (7 файлов,
211 тестов, .venv/bin/pytest).

**Метод:** распечатала полный список `name -> action_type` + описание
каждой `delete_*` функции из `imperal.json`; сравнила язык описаний
("cannot be undone" / "Trello offers no undo") между функциями с разным
`action_type`, чтобы найти несоответствия; прочитала код каждой найденной
подозрительной функции в `handlers_write.py` и её схему в `models.py`.

### Находки

1. **Реальный баг: `delete_board` был `action_type="write"` с ручным полем
   `confirm` — классический double-prompt/misclassification.** Его
   описание в манифесте буквально идентично по серьёзности описанию
   `delete_card` ("Permanently delete... Cannot be undone" /
   "Unlike archiving, this cannot be undone"), а `delete_card` уже
   правильно `destructive`. Собственный docstring `delete_board` прямо
   говорил "Gated on an explicit confirm, unlike delete_card" — то есть
   автор кода СОЗНАТЕЛЬНО выбрал ручной гейт вместо платформенного
   `destructive`, что и есть тот самый антипаттерн, задокументированный
   Imperal: "If you add `if user_confirms()` in your handler, you
   double-prompt and break the guarantee". Удаление борда уничтожает
   каждый лист, карту, комментарий и вложение на нём безвозвратно —
   объективно destructive-уровня риск.
2. Пять остальных `delete_*`-функций с языком "no undo"
   (`delete_attachment`, `delete_comment`, `delete_checklist`) —
   корректно `write`, без ручного `confirm` поля; более лёгкие по
   масштабу потери (один комментарий/один чеклист на одной карте, не
   целый борд) — решено не трогать: это решение о серьёзности риска, не
   баг схемы, соответствующая доктрине граница есть в `delete_card` vs
   `delete_attachment` уже до этого аудита.
3. `delete_custom_field` и `delete_workspace` — уже корректно
   `destructive`, без ручного `confirm`, без замечаний.

### Что сделано

1. `models.py`: удалено поле `confirm: bool` из `DeleteBoardParams`.
2. `handlers_write.py`: `delete_board` — `action_type` изменён с `write` на
   `destructive`; убран ручной блок `if not params.confirm: return _error(...)`;
   docstring переписан по образцу уже существующего образцового
   `delete_message` в Slack Connector (объясняет ПОЧЕМУ `destructive`, а не
   просто меняет тег).
3. `imperal.json`: синхронизирован программно — `action_type` ->
   `destructive`, поле `confirm` удалено из `params_schema.properties` (там
   не было в `required`).
4. `tests/test_write_tools.py`: два стухших тест-сайта, использовавших
   `confirm=True`/`confirm=False`, переписаны — `test_delete_board_requires_confirmation`
   переименован в `test_delete_board_calls_delete_and_drops_cache` и теперь
   мокает HTTP (member+board lookup+delete) и проверяет УСПЕШНОЕ удаление
   без ручного подтверждения (первая версия правки забыла добавить моки
   HTTP-ответов — тест упал с `TRELLO_UNREACHABLE`; исправлено добавлением
   трёх `http.push(...)` по образцу соседнего `test_delete_label_warns_it_leaves_every_card`);
   второй сайт (`dropped`/cache test) — убран `confirm=True` kwarg.
5. Верификация: `python3 -m py_compile` чисто; `imperal.json` валиден;
   полный прогон — `test_write_tools.py` 78/78, остальные 6 файлов 133/133
   (итого 211/211).

**Статус: FIXED.** Один реальный баг найден и устранён.
