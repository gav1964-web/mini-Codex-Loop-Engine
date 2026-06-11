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
- validated LLM atomicity/decomposition;
- typed atomic leaf contract;
- одноразовый bounded repair невалидного decomposition contract;
- coding leaf policy и executor;
- отображение atomic repair/verify contracts на реальные Loop Engine profiles;
- запрет execution authority в LLM metadata;
- restricted read-only LLM planner;
- addressable bounded evidence catalogue;
- strict criterion-by-criterion evidence verifier;
- bounded Plugin Generator acquisition adapter;
- persistent generated capability registry;
- artifact hash validation и tamper-driven reacquisition;
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
53. Применение validated atomic leaf contract до выполнения leaf.
54. LLM decomposition parent в dependency-ordered children.
55. Запрет пустых criteria и capabilities для LLM atomic leaf.
56. Проверка child keys, dependency references и циклов в LLM proposal.
57. Одноразовый repair schema-invalid decomposition.
58. Блокировка повторно невалидного decomposition response.
59. Запрет contract repair для transport errors.
60. Ограничение decomposition context до вызова LLM.
61. Compatibility unwrap для одиночных `response/atomic/decompose` wrappers.
62. Отклонение противоречивых `atomic + children` и `non-atomic + leaf`.
63. Выполнение atomic verify leaf через immutable external command.
64. Выполнение atomic repair leaf через validated LLM repair loop.
65. Dependency-ordered task tree `repair -> verify`.
66. Запрет patch leaf без `process.verify`.
67. Запрет workspace/command override через task metadata.
68. Structured block repair leaf без LLM client.
69. Structured block read-only leaf до появления evidence verifier.
70. Выполнение read-only leaf через list/read/search-only profile.
71. Запрет mutation plan до action execution.
72. Проверка каждого success criterion ровно один раз.
73. Запрет неизвестных evidence refs.
74. Запрет satisfied criterion без evidence refs.
75. Одноразовый repair evidence contract.
76. Replan по missing evidence и завершение после дополнительного чтения.
77. Read-only leaf без фиктивной verification command.
78. Structured block verify/repair leaf без внешней verification command.
79. Запрет evidence contract repair для transport errors.
80. Физическое отсутствие `apply_patch` в read-only executor registry.
81. Registry содержит только capability-declared read tools.
82. Явный пустой `allowed_tools` отклоняется, а не расширяется до defaults.
83. Scheduler cycle `missing -> acquire -> resolve again -> execute`.
84. External allowlist `capability -> plugin family`.
85. Bounded JSON CLI invocation standalone проекта `4`.
86. Проверка materialized root и обязательного file set.
87. Проверка manifest family, entrypoint и requested capability.
88. Persistent versioned capability registry.
89. Идемпотентный повтор acquisition.
90. Reacquisition после изменения generated artifact.
91. Structured block для unmapped capability и corrupt manifest.
92. Registry artifact root и запрет внешних descriptor paths.

## Результаты проверок

- `pytest`: 100 passed, 1 symlink test skipped из-за ограничений Windows;
- `compileall`: успешно;
- CLI demo: completed за 3 итерации;
- CLI coding check: completed по exit code 0;
- checkpoint: полный state и event log сохранены.
- живой gateway smoke 11 июня 2026 года: `read_text -> apply_patch ->
  run_verification`, status `completed`, verification exit code `0`;
- wheel `0.7.0` был собран и установлен в чистое Python 3.13 окружение;
- живой decomposition smoke 11 июня 2026 года: модель предложила и validator
  принял DAG `locate_failing_test -> diagnose_failure -> apply_fix ->
  verify_fix`;
- wheel `0.8.0` успешно собран;
- живой coding leaf smoke 11 июня 2026 года: TaskGraph передал atomic repair
  contract в `CodingLeafExecutor`, LLM repair изменил `value = 1` на
  `value = 2`, immutable verification command завершилась с exit code `0`;
- wheel `0.9.0` успешно собран;
- живой read-only evidence smoke 11 июня 2026 года: leaf за две итерации
  обнаружил `RetryPolicy.max_attempts = 3`, завершил criterion со ссылкой
  `evidence:1` и не имел mutation/process capabilities;
- wheel `0.10.0` успешно собран;
- живой Plugin Generator smoke 11 июня 2026 года: scheduler приобрёл
  `project.loc_report` через публичный CLI проекта `4`, проверил bundle
  `plugin.py/manifest.json/README.md`, записал registry и завершил leaf после
  повторного resolve;
- wheel `0.11.0` успешно собран;
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

Версия `0.11.0` добавляет acquisition отсутствующих reusable capabilities.
Family выбирается внешней policy, standalone Plugin Generator запускается
bounded subprocess-ом, а созданный bundle проходит независимую проверку
manifest, paths и hashes до регистрации.

Следующий существенный шаг — bounded invocation admitted generated plugins.
Acquisition и execution сознательно разделены: наличие корректного bundle ещё
не означает право исполнять его код или доверять его output.

Recovery не обещает exactly-once для action, оборванного внутри внешнего side
effect до записи checkpoint. Такие tools должны быть идемпотентными или
использовать idempotency key.
