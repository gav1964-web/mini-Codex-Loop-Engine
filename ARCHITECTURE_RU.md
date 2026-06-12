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

1. Parent integration verification через bounded commands.
2. Process registry с owner/run id и heartbeat.
3. Parallel execution только независимых ready leaves.
4. OS-level sandbox profile для недоверенных generated plugins.
5. Replay и сравнение decomposition strategies.
