# Архитектура mini-Codex Loop Engine 8

## Цель

Проект `8` исследует loop engineering как отдельную архитектурную дисциплину.
Главной сущностью является не prompt и не агент, а управляемый цикл достижения
цели.

## Базовый поток

```text
LoopDefinition
  -> Planner
  -> Plan
  -> ActionExecutor
  -> ActionResult[]
  -> Verifier
  -> VerificationResult
  -> Judge
  -> complete / continue / replan / stop
```

Все переходы выполняет `LoopEngine`. Адаптеры не могут самостоятельно менять
статус цикла.

## Ответственность ядра

- состояние запуска;
- iteration/action/wall-clock budgets;
- последовательность стадий;
- обработка исключений adapters;
- event log;
- checkpoint после значимых переходов;
- stagnation detection;
- terminal status и stop reason.

## Порты

### Planner

Получает полный `LoopState` и возвращает структурированный `Plan`.

### ActionExecutor

Выполняет одно именованное действие и возвращает `ActionResult`. Исключение
преобразуется ядром в структурированную ошибку действия.

### Verifier

Проверяет evidence, а не формулировку ответа агента. Возвращает passed/failed
criteria и дополнительные данные.

### Judge

Получает state и verification. Возвращает только решение:

- `complete`;
- `continue`;
- `replan`;
- `stop`.

### CheckpointStore

Сохраняет состояние после каждого значимого перехода. Текущая реализация
записывает JSON атомарно через временный файл.

## Phase-aware Recovery

`LoopState.phase` явно фиксирует стадию:

- `ready`;
- `planning`;
- `executing`;
- `verifying`;
- `judging`;
- `terminal`.

Checkpoint дополнительно хранит `next_action_index` и `iteration_results`.
После перезапуска `LoopEngine.resume()`:

- строит plan заново только если сбой произошёл на `planning`;
- продолжает только оставшиеся actions на `executing`;
- не повторяет checkpointed actions перед `verifying`;
- не повторяет verifier перед `judging`;
- сохраняет исходные budgets, event log и observation signatures.

Формат checkpoint имеет `schema_version`. Legacy checkpoint версии `0.2.0`
загружается на консервативной границе новой итерации.

### Граница гарантии

Уже завершённый и записанный в checkpoint action не выполняется повторно.
Однако при аварии непосредственно внутри action до сохранения результата
невозможно определить, успел ли внешний side effect произойти. Такой action
может быть запущен повторно после recovery.

Текущая модель даёт at-least-once для in-flight actions. Для опасных операций
нужны идемпотентные tools, idempotency keys либо transactional adapter.

## Stop Policy

Цикл останавливается при:

- достижении success criteria;
- превышении iteration budget;
- превышении action budget;
- превышении wall-clock budget;
- повторении одинакового observation;
- явном stop от judge;
- ошибке planner/verifier/judge.

Одинаковое verification observation два раза подряд по умолчанию считается
стагнацией. Сигнатура строится из status, passed, failed и evidence.

## Process Supervisor

`BoundedSubprocessTool` является adapter, а не частью kernel.

Каждая команда задаётся неизменяемым `SubprocessSpec` до запуска цикла:

- `argv` нельзя подменить через action arguments;
- `cwd` обязан находиться внутри workspace root;
- используется `shell=False`;
- timeout завершает дерево процессов;
- stdout и stderr непрерывно вычитываются, но сохраняются только до заданного
  лимита;
- exit code, timeout, длительность и признаки truncation возвращаются как
  structured evidence.

На Windows дерево завершается через `taskkill /T /F`, на POSIX — через отдельную
process session и `killpg`.

## Process Registry

В `0.14.0` каждый `BoundedSubprocessTool` автоматически регистрирует дочерний
процесс в общем `ProcessRegistry`:

```text
Popen
  -> register(owner_run_id, pid, identity, command digest)
  -> periodic heartbeat
  -> completed / failed / timed_out
  -> optional terminal-record pruning
```

По умолчанию registry находится в памяти и не создаёт скрытых файлов. Для
наблюдения между запусками ему явно передаётся JSON storage path. Формат
versioned, запись атомарна через временный файл и `os.replace`.

`ProcessRecord` содержит:

- уникальный record id;
- `owner_run_id`;
- PID и process identity;
- SHA-256 команды вместо raw argv;
- cwd, timeout и hostname;
- started/heartbeat/finished timestamps;
- status, exit code и terminal reason.

Raw argv сознательно не сохраняется: аргументы команд могут содержать токены.

Supervisor обновляет heartbeat во время ожидания процесса. Если регистрация или
heartbeat падают, процесс завершается fail-closed, а не остаётся работать без
учёта. Обычный command timeout по-прежнему немедленно завершает process tree и
фиксируется как `timed_out`.

После аварии launcher-а другой runtime может вызвать
`reap_stale_processes(registry, stale_after_seconds=...)`. Reaper:

1. выбирает только records со статусом `running` и просроченным heartbeat;
2. заново получает identity текущего PID;
3. завершает process tree только при точном совпадении identity;
4. при исчезнувшем или переиспользованном PID ставит status `lost`;
5. при завершении по stale heartbeat ставит status `terminated`.

Проверка identity защищает от убийства другого процесса после повторного
использования PID операционной системой.

### Bounded Process Reaper Service

В `0.19.0` добавлен `ProcessReaperService`: явно запускаемый adapter поверх
существующих `ProcessRegistry` и identity-safe reaper.

```text
host runtime
  -> ProcessReaperPolicy
  -> immediate stale sweep
  -> interruptible interval wait
  -> next sweep
  -> max_cycles / stop_requested / error
  -> structured ProcessReaperReport
```

`ProcessReaperPolicy` задаёт три обязательные положительные границы:

- `stale_after_seconds`;
- `interval_seconds`;
- `max_cycles`.

Сервис не создаёт скрытый daemon и не запускает собственный background thread.
Владение thread/process lifecycle остаётся у host runtime. Для штатной остановки
host передаёт `threading.Event`; ожидание между sweep прерывается этим event без
ожидания полного interval.

Первый sweep выполняется сразу. Каждый успешный цикл сохраняет:

- номер и timestamps;
- ids reaped records;
- число `terminated`;
- число `lost`.

Итоговый report имеет status, stop reason, timestamps, cycles и optional error.
Поддерживаются terminal reasons:

```text
max_cycles
stop_requested
error
```

Exception reaper-а и malformed result преобразуются в structured `failed`
report. Lock освобождается и после ошибки, поэтому решение о следующем bounded
запуске остаётся у host. Все service instances одного registry используют общий
lease lock; одновременный второй `run()` для этого registry отклоняется.

Service loop не заменяет command timeout внутри `BoundedSubprocessTool`: timeout
владеет живым launcher lifecycle, reaper обслуживает stale records после потери
launcher heartbeat.

Канонический пример локальной claim admission:

```bash
python -m examples.process_reaper_service_demo
```

### Process Registry Retention

В `0.24.0` service loop получил optional `ProcessRetentionPolicy`:

```text
successful stale sweep
  -> retention cadence due?
  -> prune terminal records older than retain_seconds
  -> remove at most max_pruned_per_cycle
  -> cycle pruning evidence
```

Policy задаёт:

- `retain_seconds >= 0`;
- положительный `prune_every_cycles`;
- положительный `max_pruned_per_cycle`.

Без retention policy сервис ничего не удаляет. Pruning выполняется только после
успешного reaping sweep и под тем же registry-level lease.

`ProcessRegistry.prune_terminal()` теперь принимает optional `max_records`.
Eligible terminal records сортируются по `finished_at`, затем `record_id`, и
удаляются oldest-first. Running records никогда не входят в pruning set.
Прежний вызов без `max_records` сохраняет backward-compatible поведение.

Каждый `ReaperCycleReport` дополнительно содержит:

- `pruning_attempted`;
- `pruned_count`;
- `pruning_error`.

Если pruning завершился ошибкой или вернул невалидный count, service становится
`failed`. Уже выполненный reaping не теряется: cycle report сохраняет ids
reaped records, terminated/lost counts и отдельную pruning error.

Канонический demo удаляет одну старую completed запись, сохраняет свежую
terminated запись и выполняет два bounded cycles:

```bash
python -m examples.process_reaper_service_demo
```

## Первый Coding Profile

Профиль `coding_check` выполняет одну заранее заданную verification command.
Planner создаёт action `run_verification`, verifier оценивает timeout и exit
code, judge завершает цикл только при `exit_code == 0`.

Профиль пока сознательно не редактирует файлы и не пытается чинить ошибку. Он
фиксирует нижний безопасный слой будущего coding loop:

```text
coding goal -> bounded command -> process evidence -> verifier -> judge
```

## Bounded Filesystem

`BoundedFilesystem` регистрирует четыре capability:

- `list_files`;
- `read_text`;
- `search_text`;
- `apply_patch`.

Все пути разрешаются относительно одного workspace root. Выход через `..`,
абсолютный внешний путь или symlink запрещён. `.git`, bytecode, pytest cache и
реальные `.env*` не участвуют в обходе и недоступны для прямого чтения или
изменения. `.env.example` остаётся доступным как безопасный шаблон.

Чтение, число результатов и размер изменяемого файла ограничены. `apply_patch`
не принимает произвольный shell или unified diff. Он выполняет exact-text
замену в одном существующем UTF-8 файле:

```json
{
  "path": "src/example.py",
  "old_text": "return 1",
  "new_text": "return 2",
  "expected_replacements": 1,
  "expected_sha256": "optional precondition"
}
```

Запись атомарна через временный файл и `os.replace`. Если `old_text` уже
отсутствует, а `new_text` присутствует, tool возвращает `already_applied`.
Это делает повтор после сбоя между side effect и checkpoint безопасным для
данного типа patch.

## Scripted Repair Profile

Первый полный coding loop имеет форму:

```text
inspect -> read/search -> apply_patch -> supervised verification
        -> complete / replan with next patch / stop
```

Planner пока deterministic и получает заранее заданную последовательность
patches. Это проверяет orchestration, safety, recovery и verification до
подключения LLM. Генерация patch по описанию задачи ещё не реализована.

## LLM Planning Layer

LLM подключается через provider-neutral порт:

```text
JSONLLMClient.complete_json(messages) -> JSON object
```

Текущий HTTP adapter совместим с gateway проекта `5`:

```text
POST http://127.0.0.1:8000/v1/chat/completions
```

Kernel не импортирует код gateway и не знает о провайдерах. URL, model и имя
переменной окружения с API key относятся к adapter metadata. Сам секрет в
checkpoint не записывается.

`ValidatedLLMPlanner` получает bounded context:

- цель и success criteria;
- iteration и оставшийся action budget;
- последнее verification;
- до восьми последних tool results с отдельным лимитом размера.

Ответ LLM считается недоверенным proposal. До executor он проходит
deterministic validation:

- только известные tool names;
- только разрешённые arguments;
- только workspace-relative paths;
- положительные числовые limits;
- непустой `old_text` для patch;
- максимальное число actions;
- ограничение строковых полей.

Известные prompt echo-поля `capabilities` и `rules` отбрасываются. Другие
неизвестные поля блокируют plan. Поддерживается ограниченная оболочка
`{"response": {...}}`, которую некоторые модели возвращали по раннему
варианту контракта.

LLM не может:

- выполнить tool напрямую;
- заменить verification command;
- объявить задачу завершённой;
- увеличить budgets;
- выйти за workspace.

## LLM Repair Profile

```text
goal -> LLM plan -> schema validator -> bounded tools
     -> deterministic verifier -> deterministic judge
     -> complete / replan / stop
```

Инспекция, patch и verification могут занимать разные итерации. Если
`run_verification` ещё не выполнялся, verifier возвращает `incomplete`, а judge
разрешает replan только в пределах iteration/action/wall-clock budgets.

Живой smoke 11 июня 2026 года через модель `auto` gateway проекта `5` прошёл:

```text
read_text -> apply_patch -> run_verification -> completed
```

Весь запуск находился под внешним process-tree supervisor.

## Contract Repair

Ошибка JSON parsing или deterministic plan validation не приводит к немедленному
запуску tools. `ValidatedLLMPlanner` может выполнить ровно одну дополнительную
LLM-попытку:

```text
invalid response
  -> bounded original response + validation error
  -> contract-only repair prompt
  -> full deterministic validation
  -> valid Plan или terminal planner error
```

Repair prompt:

- объявляет исходный ответ недоверенными данными;
- передаёт не более 12 000 символов исходного ответа;
- передаёт validation error с отдельным лимитом;
- требует только `rationale`, `actions`, `expected_evidence`;
- сообщает, что дополнительных repair attempts не осталось.

До успешной повторной валидации actions не выполняются. Если второй ответ снова
нарушает schema, run завершается с `PlanContractError`.

Contract repair не может:

- изменить goal или budgets;
- выполнить tool;
- исправить код;
- интерпретировать verification;
- дать модели completion authority.

HTTP, timeout и другие transport errors не считаются ошибками контракта и не
запускают повторный prompt. CLI позволяет отключить repair:

```text
--contract-repair-attempts 0
```

## Atomic Task Runtime

`LoopEngine` остаётся runtime одного исполнимого листа. Над ним появился
отдельный persistent orchestration layer:

```text
TaskGraph
  -> TaskScheduler
  -> atomicity/decomposition
  -> dependency-ready leaf
  -> capability resolution
  -> capability acquisition при необходимости
  -> LoopEngine leaf execution
  -> leaf evidence
  -> parent integration verification
  -> propagation
```

Scheduler итеративный, а не рекурсивный. После каждого изменения статуса graph
атомарно сохраняется в JSON. Это позволяет пережить перезапуск без
восстановления Python call stack.

### TaskNode

Узел содержит:

- `id`, `parent_id`, `children`;
- локальную `goal`;
- `success_criteria`;
- dependencies;
- required capabilities;
- depth и attempts;
- status;
- result/evidence и error;
- domain metadata.

Статусы:

```text
pending -> ready -> running -> completed
                    |          |
                    v          v
                  failed     parent integration

waiting  - non-atomic parent waiting for children
blocked  - dependency, capability или budget не позволяет продолжить
```

### Atomicity Contract

`TaskDecomposer.assess()` возвращает `AtomicityDecision`.

Лист считается атомарным не по размеру текста prompt, а потому что runtime
может передать его одному bounded executor с локальными criteria и доступными
capabilities. Неатомарный узел обязан вернуть непустой набор детей.

`ScriptedTaskDecomposer` оставлен для deterministic fixtures и replay.

В `0.8.0` добавлен `ValidatedLLMTaskDecomposer`. Модель может вернуть только
один из двух вариантов:

```text
atomic   -> typed leaf contract
decompose -> immediate children + local dependencies
```

Atomic leaf contract содержит уточнённые `goal`, `success_criteria`,
`required_capabilities` и bounded `metadata`. Он применяется scheduler-ом
только после полной deterministic validation.

LLM proposal проверяется до изменения graph:

- строгий набор полей для выбранного decision;
- непустые criteria и capabilities атомарного листа;
- bounded строки, массивы, metadata и число детей;
- стабильный формат child keys и capability names;
- уникальность keys и capabilities;
- существование dependency references;
- отсутствие self-dependency и циклов.

Schema-invalid ответ получает не более одной contract-repair попытки.
Original response рассматривается как недоверенные данные. Transport errors
не запускают repair.

Ограничения дерева:

- `max_nodes`;
- `max_depth`;
- `max_leaf_executions`.

Child keys и dependency references проверяются до изменения graph. Duplicate
keys, неизвестные dependencies и циклы отклоняются как
`decomposition_contract_error`, поэтому невалидная decomposition не оставляет
частично созданных детей.

### Dependencies And Propagation

Leaf становится исполнимым только после завершения всех dependencies.

Если dependency или child получает `failed/blocked`:

- зависимые листья становятся `blocked`;
- parent становится `blocked`;
- независимые незатронутые ветви не обязаны перезапускаться.

После завершения всех детей parent не считается готовым автоматически.
`IntegrationVerifier` обязан проверить родительские criteria и вернуть
structured result.

В `0.13.0` добавлен `BoundedCommandIntegrationVerifier`:

```text
all children completed
  -> external parent command policy
  -> bounded subprocess
  -> exit/timeout/truncation evidence
  -> completed / failed / blocked parent
```

`BoundedIntegrationPolicy` хранит:

- workspace root;
- immutable command для конкретного `node.id` либо явный default;
- workspace-relative cwd;
- timeout и output limit.

Task metadata не участвует в выборе command, cwd или process bounds. До запуска
verifier повторно убеждается, что parent имеет детей и каждый ребёнок находится
в `completed`. Команда запускается с `shell=False`, без stdin и под общим
process-tree supervisor.

Результаты интерпретируются так:

- exit code `0` — parent `completed`;
- ненулевой exit code — parent `failed`;
- timeout — parent `blocked`;
- truncated output — parent `failed`;
- отсутствие admitted command — parent `blocked`.

Evidence команды и evidence всех детей сохраняются в parent result. Успешные
дети не перезапускаются и не меняют свой статус, если общая integration
verification не прошла.

### Integration Policy Composition

В `0.18.0` добавлен `CompositeIntegrationVerifier`, который остаётся обычной
реализацией существующего порта `IntegrationVerifier`:

```text
completed children
  -> external exact-node route
  -> ordered all-of IntegrationPlan
  -> independent verifier snapshots
  -> aggregate every result
  -> failed > blocked > completed
```

`IntegrationCompositionPolicy` определяет:

- exact routes `node.id -> IntegrationPlan`;
- optional default plan;
- порядок обязательных named verifiers.

Маршрут и plan не читаются из `TaskNode.metadata`. LLM или child result не могут
переключить route, убрать gate или изменить порядок проверок.
Созданный routes mapping неизменяем и отделён от исходного mutable словаря.

При создании composite registry проверяются:

- непустые уникальные verifier names в каждом plan;
- существование каждого referenced verifier;
- наличие хотя бы одного exact route или default plan.

Перед запуском composite повторно проверяет, что parent имеет детей и все они
`completed`. Каждый verifier получает собственную deep-copy `TaskNode` и
`TaskGraph`. Изменение snapshot одной проверкой не влияет на следующую проверку
или живой graph scheduler-а.

Composition использует all-of semantics и не short-circuit-ит. Даже если первая
проверка blocked или failed, остальные выполняются для полного evidence.
Результаты агрегируются с приоритетом:

```text
любая failed  -> parent failed
иначе blocked -> parent blocked
иначе         -> parent completed
```

Exception verifier-а преобразуется в failed check. Неверный тип результата и
неизвестный status также преобразуются в fail-closed ошибки.

Parent evidence содержит:

- evidence завершённых детей;
- выбранный route и ordered plan;
- status, summary, error и evidence каждого named check.

Канонический пример:

```bash
python -m examples.integration_composition_demo
```

Scheduler не получил routing или aggregation branches: он по-прежнему вызывает
один `IntegrationVerifier` и применяет один structured result.

### Typed Integration Selectors

В `0.23.0` exact routes дополнены внешними ordered selector routes:

```text
completed parent
  -> exact node.id route
  -> first matching typed selector route
  -> default plan
  -> missing route block
```

`IntegrationRoute` содержит уникальное имя, `IntegrationSelector` и
`IntegrationPlan`. Поддерживаются selector types:

- `node_id_prefix`;
- `depth`;
- `required_capability`.

Selectors читают только structural fields `TaskNode.id`, `depth` и
`required_capabilities`. Metadata не участвует в matching и не может создать,
отключить или переупорядочить route.

Exact route всегда имеет приоритет над selectors. Selector routes проверяются в
явном порядке policy, поэтому пересекающиеся selectors разрешаются
детерминированно через first-match. Default применяется только после miss всех
selectors.

Policy копирует selector routes в immutable tuple, проверяет уникальность route
names и типы selector/plan. Composite registry также проверяет verifier names во
всех selector plans.

Evidence содержит выбранный route и, для selector route, точные `kind` и
`value`. Scheduler по-прежнему не знает о routing types: resolver полностью
остаётся внутри реализации `IntegrationVerifier`.

Канонический demo теперь выбирает root integration plan через selector
`depth=0`:

```bash
python -m examples.integration_composition_demo
```

### Bounded Parallel Leaves

В `0.15.0` `TaskScheduler` может параллельно выполнять независимые ready leaves:

```text
dependency-ready atomic leaves
  -> external parallel-safe capability policy
  -> reserve leaf budget and mark running
  -> bounded thread batch over graph snapshots
  -> stable node-id result application
  -> propagation and integration verification
```

Parallelism выключен по умолчанию: `max_parallel_leaves = 1` полностью сохраняет
прежний последовательный порядок `assess -> execute`. Для включения требуется
`TaskSchedulerPolicy` с:

- положительным `max_parallel_leaves`;
- непустым внешним allowlist `parallel_safe_capabilities`.

Leaf допускается в parallel batch, только если:

- он уже `ready`;
- все dependencies завершены;
- у него есть required capabilities;
- весь capability set входит в parallel-safe allowlist;
- остаётся leaf-execution budget.

Отсутствие dependency edge само по себе не считается достаточным разрешением:
mutation capabilities одного workspace не следует объявлять parallel-safe без
отдельной resource isolation policy.

### Resource Claims

В `0.20.0` `TaskSchedulerPolicy` получил внешний immutable mapping
`node.id -> tuple[ResourceClaim, ...]`.

```text
ready leaves
  -> parallel-safe capability admission
  -> mutation capability requires write claim
  -> deterministic read/write conflict check
  -> bounded non-conflicting batch
```

`ResourceClaim` содержит canonical resource identity и режим:

- `read`;
- `write`.

Два `read` одного ресурса совместимы. Любое пересечение, где хотя бы один claim
имеет режим `write`, является конфликтом.

Для filesystem workspace следует использовать:

```python
ResourceClaim.workspace(path, mode="write")
```

Helper применяет `Path.resolve()` и platform `normcase`, поэтому эквивалентные
написания одного пути получают одинаковую identity.

Policy отдельно задаёт `mutation_capabilities`. Leaf с такой capability может
попасть в parallel batch только при наличии хотя бы одного внешнего write claim.
Без claim он выполняется последовательно. Claim mapping копируется и становится
неизменяемым при создании policy.

Claims выбираются только по exact `node.id`. `TaskNode.metadata` не может
добавить, удалить или подменить ресурс. LLM decomposition поэтому не получает
resource admission authority.

Batch собирается детерминированно в порядке ready nodes. Конфликтующий leaf
пропускается для текущего batch, но не блокирует более поздний независимый leaf.
Например, `write(workspace A)`, второй `write(workspace A)` и
`write(workspace B)` дают первый batch из первого и третьего leaves.

Resource claims являются admission policy, а не sandbox или filesystem lock.
Executor по-прежнему обязан применять bounded workspace policy, atomic writes и
собственные OS-level ограничения.

### Cross-Process Resource Leases

В `0.25.0` к локальной batch admission добавлен optional
`FileResourceLeaseManager`:

```text
capability-admitted leaves
  -> canonical batch claims
  -> atomic acquire in shared JSON registry
  -> mark running and reserve budget
  -> execute workers
  -> release in finally
```

Manager использует один versioned JSON registry и короткоживущий lock directory.
Полный набор claims приобретается одной транзакцией: частичная резервация
невозможна. Семантика конфликтов совпадает с локальным scheduler policy:
`read/read` совместимы, любое пересечение с `write` блокируется.

Acquire имеет bounded timeout и polling interval. При contention scheduler:

- не переводит leaf в `running`;
- не увеличивает `attempts`;
- не расходует `max_leaf_executions`;
- возвращает `resource_lease_unavailable:<resources>`.

Ошибка registry становится `resource_lease_error`, ошибка release -
`resource_lease_release_error`. Workers не получают lease manager и не могут
менять task statuses. Authority остаётся у `TaskScheduler`.

Каждая запись содержит owner id, PID и process identity. Перед acquire manager
удаляет lease только если текущая identity PID больше не совпадает с записанной.
Это позволяет восстановиться после падения scheduler process и защищает от
переиспользования PID.

Граница гарантии: если scheduler-поток погиб внутри всё ещё живого процесса,
его lease не считается orphan. Для такого случая нужен следующий слой -
heartbeat/expiry либо отдельный scheduler process supervisor.

Backend включается явно через `TaskScheduler(resource_lease_manager=...)`.
Без него поведение `0.20.0` полностью сохраняется.

Канонический пример:

```bash
python -m examples.resource_leases_demo
```

Канонический пример:

```bash
python -m examples.resource_claims_demo
```

Capability resolution и acquisition выполняются scheduler-ом последовательно до
запуска threads. Каждый worker получает собственный snapshot `TaskNode` и
`TaskGraph`. Worker не может изменить живые statuses, events или checkpoint.
Общий `LeafExecutor` должен быть thread-safe в рамках явно разрешённых
capabilities.

Scheduler резервирует budget, переводит выбранные узлы в `running` и сохраняет
graph до запуска batch. После завершения всех workers результаты применяются в
стабильном порядке `node.id`, а не completion order. Поэтому event sequence и
persistence остаются детерминированными.

Ошибка executor преобразуется в structured failed result. Ошибка построения
snapshot также завершает выбранные leaves как failed, не оставляя их навсегда в
`running`. Recovery сохраняет прежнюю семантику: checkpointed `running` leaves
после перезапуска переходят в `ready`.

### Decomposition Replay And Comparison

В `0.17.0` добавлен отдельный experiment layer над портом `TaskDecomposer`:

```text
live/scripted decomposer
  -> RecordingTaskDecomposer
  -> versioned decision trace
  -> fresh TaskGraph
  -> RecordedTaskDecomposer
  -> strict context replay
```

`RecordingTaskDecomposer` сохраняет уже полученный `AtomicityDecision`. Он не
обходит deterministic validation LLM decomposer-а и не вмешивается в scheduler.

Каждая trace entry содержит `node_id`, SHA-256 deterministic node context и
serialized atomic/decompose decision. Context fingerprint включает goal,
criteria, capabilities, dependencies, metadata, ancestors, depth, task budget,
текущее число nodes и leaf executions.

Replay разрешён только при точном совпадении context. Изменение цели, бюджета,
порядка исполнения или структуры предков даёт
`decomposition replay context mismatch`, а не молчаливое применение старого
решения к новой задаче.

Trace имеет `schema_version`, сохраняется атомарно и проверяет:

- уникальность node ids;
- формат context SHA-256;
- полный decision shape;
- непротиворечивость atomic/children/leaf.

`DecompositionStrategyRunner` запускает несколько decomposer factories на
свежих graphs одного `ReplayTaskCase`. Scheduler, capability resolver, leaf
executor и integration verifier задаются общей factory, поэтому сравнивается
именно decomposition strategy.

Для каждого запуска считаются root status, node/leaf count, maximum depth,
dependency edges, leaf executions, events, failed/blocked count, topology
SHA-256 и outcome SHA-256. Fingerprints не включают timestamps и graph id.

Comparison группирует стратегии по topology и outcome fingerprints и сообщает
`topology_diverged`/`outcome_diverged`. Слой намеренно не выбирает победителя:
стоимость, качество, latency и риск требуют отдельной явной judge policy.

### Decomposition Strategy Judge

В `0.22.0` добавлен независимый `LexicographicStrategyJudge`:

```text
StrategyComparison
  -> external StrategyJudgePolicy
  -> eligibility filter
  -> ordered objective tuple
  -> stable lexicographic ranking
  -> versioned StrategyRanking report
```

`StrategyJudgePolicy` явно задаёт допустимые terminal `root_status` и
упорядоченный набор `StrategyObjective` с направлением `min` или `max`.
Поддерживаются только уже измеренные numeric metrics: node/leaf count, max
depth, dependency edges, leaf executions, event count, failed/blocked count.

Judge не обращается к scheduler, decomposer или LLM и не запускает стратегии
повторно. Он работает только с результатом `StrategyComparison`.

Ranking является lexicographic: первый objective имеет наивысший приоритет,
следующий используется только при равенстве предыдущих. Общий неявный weighted
score отсутствует. Metric values и полный порядок policy сохраняются в report.

Стратегии с root status вне policy остаются видимыми как `eligible=false` и
`rank=null`. Равные objective tuples получают одинаковый rank. Имя стратегии
используется только для стабильного порядка отображения и не разрывает tie.

Judge fail-closed отклоняет пустой comparison, duplicate/empty strategy names,
run другого case, неизвестные или повторяющиеся objectives и пустой eligibility
set.

Канонический пример:

```bash
python -m examples.decomposition_strategy_compare
```

Нейтральный report сохраняется в `build/decomposition_comparison.json`, ranking
report — в `build/decomposition_ranking.json`. Оба остаются execution artifacts.
Выбранный reference baseline следует переносить в human-maintained документ, а
не коммитить как полный run output.

### Capability Acquisition

```text
required capabilities
  -> CapabilityResolver
  -> missing?
       no  -> execute leaf
       yes -> CapabilityAcquirer
              -> resolve again
              -> execute / block
```

Plugin Generator должен реализовать `CapabilityAcquirer`. Scheduler не знает,
как создаётся plugin, и не импортирует generator. Он только фиксирует запрос,
повторно вызывает resolver и блокирует leaf, если capability не появилась.

В `0.11.0` это реализовано через два независимых компонента.

`PluginGeneratorAcquirer`:

- вызывает standalone проект `4` только через публичный JSON CLI;
- запускает процесс через bounded process-tree supervisor;
- принудительно использует UTF-8 stdout на Windows;
- берёт family из внешнего allowlist `capability -> family`;
- не принимает family, interpreter, output root или constraints из task metadata;
- проверяет exit code, timeout и output truncation;
- проверяет JSON status, plugin id, family и materialized root;
- разрешает только `plugin.py`, `manifest.json`, `README.md`;
- проверяет manifest entrypoint и requested capability.

`PersistentCapabilityRegistry`:

- реализует `CapabilityResolver`;
- атомарно сохраняет versioned JSON state;
- имеет обязательный artifact root и отклоняет descriptors за его пределами;
- хранит family, plugin id, paths и SHA-256 всех обязательных файлов;
- при каждом resolve повторно проверяет наличие и hashes;
- считает tampered/stale artifact отсутствующей capability.

Последовательность:

```text
missing capability
  -> external family admission policy
  -> bounded Plugin Generator CLI
  -> bundle/manifest/hash admission
  -> persistent registry
  -> resolve again
  -> leaf execute / block
```

### Generated Plugin Runtime

В `0.12.0` admission и execution остаются разными полномочиями:

```text
hash-verified registry descriptor
  -> external invocation allowlist
  -> fixed JSON payload
  -> isolated Python worker
  -> bounded subprocess supervisor
  -> strict JSON envelope
  -> output contract validation
  -> LeafExecutionResult
```

`PluginInvocationPolicy` определяет вне task graph:

- какие generated capabilities разрешено запускать;
- payload для каждой capability;
- обязательные поля результата и допустимые success statuses;
- interpreter, timeout, output limit и payload limit.

`TaskNode.metadata` не может подменить payload, interpreter или process bounds.
Один generated leaf исполняет ровно одну capability. Смешанные capability sets
блокируются до появления отдельной composition policy.

`GeneratedPluginLeafExecutor` перед каждым запуском получает descriptor только
через `PersistentCapabilityRegistry`, поэтому stale или tampered artifact
считается отсутствующей capability. Worker дополнительно:

- запускается с `python -I`;
- повторно проверяет SHA-256 байтов `plugin.py`;
- компилирует и исполняет именно прочитанные проверенные байты;
- не использует shell и не имеет stdin;
- подавляет произвольный stdout/stderr плагина;
- принимает и возвращает только JSON objects;
- возвращает bounded error envelope без traceback.

Timeout завершает всё дерево процессов через общий `BoundedSubprocessTool`.
Truncated, malformed, non-object или unsuccessful output не может завершить
leaf успешно.

Это process isolation, но не полноценная security sandbox. Generated code всё
ещё наследует файловые и сетевые права пользователя процесса. Поэтому runtime
не запускает любую зарегистрированную capability автоматически: для неё нужна
отдельная invocation policy. Следующий уровень изоляции потребует OS-level
restrictions или контейнерного runner-а.

### OS Sandbox For Generated Plugins

В `0.16.0` invocation contract различает два режима:

```text
trusted admitted plugin
  -> existing isolated Python worker

requires_os_sandbox = true
  -> configured sandbox probe
  -> unavailable? blocked, no fallback
  -> sandbox command
  -> bounded process supervisor
  -> existing worker/hash/output validation
```

Trust classification задаётся только внешним `PluginInvocationSpec`.
`TaskNode.metadata` не может отключить sandbox, изменить backend, добавить
mounts или включить сеть.

Production backend `WslBubblewrapSandbox` предназначен для Windows + WSL2 +
Linux `bubblewrap`. Он строит команду:

- `wsl.exe --distribution <policy distro> --exec /usr/bin/bwrap`;
- `--die-with-parent` и `--new-session`;
- `--unshare-all`, включая отдельный network namespace;
- `--clearenv`;
- read-only `/usr`, `/lib`, `/lib64`;
- отдельные `/proc`, `/dev` и tmpfs `/tmp`;
- read-only plugin bundle в `/plugin`;
- read-only trusted worker в `/runtime/plugin_worker.py`;
- только явно разрешённые data/output mounts;
- isolated Linux Python `-I`.

Sandbox не видит Windows workspace целиком. `SandboxMount` допускает:

- read-only mounts только под `/data/...`;
- read-only или writable mounts под `/output/...`;
- существующие host paths;
- уникальные нормализованные sandbox targets.

Payload должен ссылаться на sandbox paths, например `/data/project`, а не на
Windows path. Writable доступ требует отдельного explicit mount под `/output`.

Backend probe также выполняется через `BoundedSubprocessTool`, поэтому имеет
timeout, process registry и structured terminal outcome. Если WSL, distro,
`bwrap` или executable недоступны, strict invocation получает:

```text
generated_plugin_os_sandbox_unavailable:wsl_bubblewrap
```

и generated code не запускается. Direct-process fallback запрещён.

На текущей Windows-машине 12 июня 2026 года доступны WSL2 Ubuntu 22.04,
`/usr/bin/python3` и установленный `bubblewrap 0.6.1`. Из-за неработающей
исходящей сети WSL официальный Ubuntu package был скачан Windows-транспортом и
установлен через `dpkg`; SHA-256 `.deb`:

```text
F75C835D6871D1B36370E12EE82940334B2A9F94EFC7B959B5B236447E89743D
```

Production probe возвращает `available=True`. Канонический real smoke:

```bash
python -m examples.plugin_sandbox_smoke
```

Он проверяет end-to-end:

- strict invocation через `GeneratedPluginLeafExecutor`;
- read-only чтение `/data`;
- запрет записи в `/data`;
- отсутствие немонтированного Windows workspace;
- отсутствие общей сети;
- успешную запись только в explicit `/output`.

12 июня 2026 года все проверки smoke завершились успешно.

### Sandbox Release Gate

В `0.21.0` канонический real smoke подключён к автоматическому release gate:

```text
release validation
  -> bounded subprocess supervisor
  -> python -m examples.plugin_sandbox_smoke
  -> strict JSON parse
  -> require 8/8 checks
  -> atomic release report
  -> passed / blocked / degraded / failed
```

Production-команда:

```bash
python -m tools.sandbox_release_gate
```

Gate не дублирует sandbox fixtures и проверки. Он запускает единственный
канонический smoke как child process и строго интерпретирует его результат.
Команда immutable, имеет timeout и output bounds, а lifecycle фиксируется общим
`ProcessRegistry`.

Статус `passed` возможен только когда smoke завершился с exit code `0`, status
`completed` и все восемь checks равны `true`:

- completed;
- backend;
- data write blocked;
- host hidden;
- network blocked;
- output written;
- read-only data unchanged;
- output materialized.

Unavailable backend с exit code `2` даёт `blocked` и запрещает release.
Явный `--degraded-ok` предназначен только для non-production проверки: report
получает status `degraded` и никогда не маскируется под `passed`.

Fail-closed результат формируется при timeout, truncation, malformed JSON,
отсутствующем check, неожиданном outcome или ошибке запуска executable.
Даже launch failure создаёт атомарный versioned JSON report. Report path обязан
оставаться внутри workspace и по умолчанию равен:

```text
build/sandbox_release_gate/report.json
```

Real strict gate 12 июня 2026 года завершился `passed`: `8/8`, backend
`wsl_bubblewrap`, release decision `releasable=true`.

### Leaf Execution

`LoopEngineLeafExecutor` строит task-specific `LoopEngine + LoopDefinition`,
запускает leaf и преобразует terminal state в `LeafExecutionResult`.

В evidence сохраняются:

- loop run id;
- loop status;
- iteration/action counts;
- verifier evidence;
- stop reason.

### Coding Leaf Policy

В `0.9.0` добавлен `CodingLeafExecutor`. Он переводит validated atomic contract
в один из существующих profiles:

```text
process.verify
  -> deterministic coding check

filesystem.patch + process.verify
  -> validated LLM repair loop
```

Read/list/search capabilities могут сопровождать repair leaf. Наличие
`filesystem.patch` без `process.verify` блокируется: изменение кода не считается
завершённым без объективной проверки.

Execution authority находится во внешнем `CodingLeafPolicy`:

- workspace root;
- immutable verification command для repair/verify leaves;
- subprocess timeout и output limit;
- LLM iteration/action budgets;
- checkpoint root.

Task metadata не может переопределить workspace, command, gateway, model или
credentials. Reserved execution fields приводят к structured `blocked`.
Read-only evidence policy не требует фиктивной verification command.

### Read-Only Evidence Leaf

В `0.10.0` read-only leaf исполняется отдельным profile:

```text
restricted LLM planner
  -> list_files / read_text / search_text
  -> bounded evidence catalogue
  -> strict LLM evidence verifier
  -> deterministic reference validation
  -> judge complete / replan / stop
```

Planner получает только tools, соответствующие capabilities конкретного leaf.
`apply_patch` и `run_verification` отсутствуют и в prompt contract, и в
разрешённом validator set. Даже если модель предложит mutation, action не
попадёт в executor.

Каждый успешный tool result получает стабильный id `evidence:N`. Evidence
verifier обязан:

- вернуть каждый success criterion ровно один раз и без изменения текста;
- явно указать `satisfied`;
- для satisfied criterion сослаться минимум на один существующий evidence id;
- перечислить недостающие факты для следующей итерации.

Неизвестные refs, пропущенные criteria и голословный `satisfied=true`
отклоняются. Допускается одна bounded contract-repair попытка.

Семантическая оценка достаточности остаётся LLM-решением, но она привязана к
реальному bounded catalogue. Deterministic runtime контролирует полноту
контракта и существование ссылок, а не подменяет смысл phrase rules.

### Recovery

Task graph имеет versioned JSON envelope. Узел, сохранённый как `running`,
при загрузке переводится в `ready` с диагностикой
`recovered_after_interrupted_leaf_execution`.

Это at-least-once semantics для незавершённого leaf. Side-effecting leaf
по-прежнему обязан опираться на идемпотентные tools и checkpoints собственного
`LoopEngine`.

## Отличие от mini-Codex 7

В `7` agent loop был частью dialog runtime и знал о coding workflow. В `8`:

- нет dialog shell;
- нет semantic phrase routing;
- нет встроенного LLM;
- нет встроенных файловых инструментов;
- нет встроенного plugin generator;
- нет специальных repair branches.

Все перечисленное подключается через adapters.

## Целевая интеграция

```text
Loop Engine
  + LLM Planner Adapter
  + Tool/Process Supervisor Adapter
  + Plugin Generator Adapter
  + Coding Verifier Adapter
  + LLM/Deterministic Judge
  = Codex-like autonomous runtime
```

## Следующий этап

1. Composite release gate для pytest, wheel smoke и real sandbox.
2. Lease heartbeat/expiry для зависшего scheduler внутри живого процесса.
3. Measured latency/cost metrics для richer strategy judge policies.
4. Compound typed selectors с явным all/any composition.
5. Persistent service-run reports и operational observability.
