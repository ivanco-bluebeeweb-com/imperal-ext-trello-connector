# Scenario Tests (PST) — Trello Connector

Метод: `Docs/session-notes/SCENARIO_TESTING_STANDARD.md`.

---

## Прогон 2026-08-20 — Часть D (Deploy Verification / Idempotency / Security-SSRF / Regression grep)

**D1 (Deploy Verification):** не применялось — код приложения не менялся (только тесты), деплой не требуется.

**D2 (Idempotency):** добавлен 1 тест. `delete_card` резолвит карту по имени через `shared.resolve_card` перед вызовом DELETE. Подтверждено: повторный вызов на уже удалённой карте не находит её при повторном сканировании доски и получает чистую ошибку — никогда не падает и не сообщает о повторном успешном удалении (Trello-удаление необратимо, так что ложный второй успех был бы особенно вводящим в заблуждение).

**D3 (Security/SSRF):** подтверждено на двух уровнях. (1) `add_attachment`'s `url` уходит как query-параметр В Trello API (`api.trello.com/.../attachments?url=...`) — сам этот коннектор никогда не дереференсит его; добавлен тест, что этот URL не появляется среди адресов собственных HTTP-вызовов приложения. (2) Единственный настоящий `ctx.http.get` в исходниках (`accounts.py`, проверка живости ключа) целится в фиксированную константу `trello.com/1/authorize` с интерполированным ключом — не произвольный домен. Добавлен исходный regression-тест, требующий, чтобы каждый файл с `ctx.http.*`-вызовом содержал `trello.com`/`TRELLO_API` где-то в файле.

**D4 (Regression grep):** нет новых находок специфичных для этого приложения сверх `Docs/known-bug-patterns.md`.

**Итог:** 237/237 тестов зелёные (было 226). Реальных багов не найдено.

---

## Прогон 2026-08-19

**Существующее покрытие до PST:** 7 файлов тестов, 2600+ строк —
accounts, shared-хелперы резолва, HTTP-клиент, форма объектов, панели,
и большинство read/write инструментов. Аудит по точному имени функции
нашёл **11 функций, никогда не тестировавшихся напрямую через свой
хендлер**:

`archive_list`, `create_board`, `create_checklist`, `create_workspace`,
`list_activity`, `list_attachments`, `list_custom_fields`,
`list_notifications`, `list_stickers`, `list_workspaces`,
`set_check_item`.

**Новый файл:** `tests/test_pst_scenarios.py` — 23 сценария (happy,
error, adversarial) по всем 11 функциям.

### Реальный баг найден и исправлен (код приложения)

Шесть write/read-функций, ни одна из которых работает с доской
(`create_workspace`, `update_workspace`, `set_workspace_member`,
`delete_workspace`, `list_workspaces`, `list_notifications`),
использовали `shared.resolve(ctx, "")` — то есть **полный резолв
доски** — вместо `shared.any_credentials(ctx)`, который сам докстринг
`any_credentials()` называет правильным выбором именно для вызовов
уровня `/members/me` (не привязанных к доске). Последствие:

1. Аккаунт с рабочим токеном, но без единой открытой доски, получал
   ложную ошибку "no open Trello boards" при попытке создать/изменить
   воркспейс или прочитать уведомления — хотя эти операции с досками
   вообще не связаны.
2. Проверка `if err and not creds: return err` была мёртвым кодом:
   `creds` при ошибке `resolve()` — это `("", "")`, непустой tuple,
   поэтому `not creds` всегда `False` в Python. Ошибка резолва board
   (если бы она вообще случилась) тихо проглатывалась бы, и код
   продолжал выполняться с пустыми credentials.

Обнаружено это тестами на `create_workspace`/`list_notifications`/
`list_workspaces`, где не было ни одной доски в очереди HTTP-ответов —
ожидаемо (эти операции accounts не board-scoped), но хендлеры падали
с "нет доступных досок".

**Исправление:** все 6 мест переведены на `shared.any_credentials(ctx)`
(main.py-эквивалент — `handlers_write.py`/`handlers_read.py`), мёртвая
проверка убрана. Три существующих write-теста
(`test_removing_a_workspace_member_says_boards_are_unchanged`,
`test_removing_a_workspace_member_is_not_a_deactivation`,
`test_workspace_description_can_be_cleared`) содержали лишние
`member_payload()`/`board_payload()` HTTP-заглушки, компенсировавшие
баг — приведены в соответствие с исправленным (корректным) поведением.

### Результат

234/234 тестов зелёные (211 существующих + 23 новых). Публикуется по
правилу двойной публикации (код приложения изменился): git commit +
push, затем `developer.deploy_app`.
