# R&D-отчёт: mini-Codex Loop Engine 8

## Что создано

В `F:\ubuntu\test\8` создан новый проект, который не продолжает структуру
mini-Codex 7, а извлекает из неё общую идею управляемого agent loop.

Реализованы:

- `LoopDefinition` и `LoopState`;
- универсальный `LoopEngine`;
- typed contracts для plan/action/verification/judgement;
- iteration, action и wall-clock budgets;
- stagnation detection по observation signature;
- последовательный event log;
- атомарные JSON checkpoints;
- bounded tool registry;
- bounded subprocess supervisor;
- process-tree termination по timeout;
- ограниченный захват stdout/stderr;
- запрет выхода рабочего каталога за workspace root;
- первый verification-only coding profile;
- versioned checkpoint envelope;
- десериализация полного `LoopState`;
- phase-aware recovery через CLI и API;
- bounded filesystem adapter;
- `list_files`, `read_text`, `search_text`;
- атомарный и идемпотентный exact-text `apply_patch`;
- scripted inspect-edit-verify repair profile;
- CLI-команда `repair`;
- provider-neutral `JSONLLMClient`;
- HTTP adapter к OpenAI-compatible gateway проекта `5`;
- bounded LLM context builder;
- strict JSON plan validator;
- одноразовый bounded contract-repair pass;
- LLM repair profile и CLI-команда `llm-repair`;
- persistent `TaskGraph` и versioned graph store;
- iterative dependency scheduler;
- deterministic atomicity/decomposition;
- capability resolver и acquisition port;
- `LoopEngineLeafExecutor`;
- parent integration verification;
- CLI-команда `task-demo`;
- function adapters для planner и verifier;
- deterministic criteria judge;
- JSON CLI;
- демонстрационный counter loop.

## Проверенные сценарии

1. Успешное достижение цели за несколько итераций.
2. Остановка после одинакового observation.
3. Остановка строго по action budget.
4. Преобразование неизвестного tool в structured error.
5. Запись и чтение итогового checkpoint.
6. Завершение зависшего процесса по timeout.
7. Ограничение большого stdout без блокировки процесса.
8. Запрет `cwd` вне workspace.
9. Успех и ошибка coding verification по реальному exit code.
10. Recovery с фазы выполнения только оставшихся actions.
11. Recovery с verifier без повторения завершённого action.
12. Recovery с judge без повторения verifier.
13. Безопасная загрузка legacy checkpoint.
14. Защита `run_id` от path traversal.
15. CLI resume из сохранённого checkpoint.
16. Запрет filesystem path traversal и symlink traversal.
17. Ограничение чтения и размера patch target.
18. SHA-256 precondition перед изменением.
19. Идемпотентный повтор patch после незаписанного side effect.
20. Одноитерационный inspect-edit-verify repair.
21. Replan на вторую patch-попытку после провала verification.
22. Repair CLI с JSON patch-файлом.
23. Запрет чтения и изменения `.git` и реальных `.env*`.
24. Разрешение `.env.example` как шаблона.
25. Запрет прямого и вложенного symlink path.
26. Отклонение неизвестного LLM tool до выполнения actions.
27. Отклонение traversal paths и лишних runtime arguments.
28. Ограничение LLM context и action count.
29. HTTP request contract `/v1/chat/completions`.
30. Отсутствие API key в state и checkpoint.
31. LLM inspect-edit-verify на deterministic fake client.
32. UTF-8 JSON output в Windows-консоли.
33. Живой repair через gateway проекта `5`.
34. Исправление schema-invalid plan со второй LLM-попытки.
35. Исправление malformed JSON с bounded original response.
36. Блокировка повторно невалидного repair response.
37. Отключение repair через configuration.
38. Запрет repair для transport errors.
39. End-to-end выполнение только валидированных actions после repair.
40. Декомпозиция parent до dependency-ordered atomic leaves.
41. Запрет запуска leaf до завершения dependencies.
42. Propagation failed/blocked в зависимые узлы и parent.
43. Parent integration verification после завершения детей.
44. Capability acquisition и повторный resolve.
45. Блокировка leaf при отсутствующей capability.
46. Ограничения max nodes/depth/leaf executions.
47. Recovery interrupted leaf из `running` в `ready`.
48. Leaf execution через существующий `LoopEngine`.
49. Persistent CLI task graph demo.
50. Structured failure decomposer и integration verifier.
51. Транзакционная проверка child keys/dependencies до изменения graph.
52. Отклонение циклической decomposition.

## Результаты проверок

- `pytest`: 63 passed, 1 symlink test skipped из-за ограничений Windows;
- `compileall`: успешно;
- CLI demo: completed за 3 итерации;
- CLI coding check: completed по exit code 0;
- checkpoint: полный state и event log сохранены.
- живой gateway smoke 11 июня 2026 года: `read_text -> apply_patch ->
  run_verification`, status `completed`, verification exit code `0`;
- wheel `0.7.0` собран и установлен в чистое Python 3.13 окружение;
- установленный `task-demo` успешно выполнил два atomic leaf вне дерева
  исходников;
- для Python ниже 3.11 добавлена явная диагностическая ошибка при импорте.

## Принципиальные решения

### Loop является продуктом

Planner или LLM не владеет циклом. Он предлагает следующий план. Решение о
возможности продолжения ограничивается deterministic policies.

### Verification отделён от Judge

Verifier собирает факты. Judge интерпретирует их относительно цели. Это не
позволяет агенту объявить собственный текст доказательством успеха.

### Core не знает предметную область

Coding, research, content processing и operations используют одинаковый loop,
но разные adapters и profiles.

### Event log важнее финального ответа

Каждое решение можно воспроизвести, анализировать и сравнивать с другой loop
strategy.

## Что сознательно не реализовано

- прямые SDK конкретных LLM-провайдеров;
- parallel actions;
- multi-agent delegation;
- Plugin Generator adapter;
- расширенная coding-specific verification помимо exit code;
- human approval gates.

## Оценка

MVP подтверждает архитектурную гипотезу: полезные механизмы mini-Codex 7 можно
представить как небольшой универсальный kernel, не привязанный к dialog shell.

Проект пока не является автономным coding agent. Это основание, поверх которого
такого агента можно построить без повторного смешивания planner, tools,
verification и stop logic.

Версия `0.7.0` добавляет верхний уровень orchestration: задача доводится до
атомарных листьев, листья выполняются существующим Loop Engine, а результаты
поднимаются через parent integration verification.

Следующая существенная гипотеза — validated LLM decomposition. Модель должна
предлагать children и dependencies, но deterministic runtime обязан проверять
уникальность, depth/node budgets, циклы и наличие исполнимых leaf contracts.

Recovery не обещает exactly-once для action, оборванного внутри внешнего side
effect до записи checkpoint. Такие tools должны быть идемпотентными или
использовать idempotency key.
