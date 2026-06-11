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

1. Process registry с owner/run id и heartbeat поверх существующего supervisor.
2. Общий idempotency contract для side-effecting actions.
3. Contract-repair pass для malformed LLM JSON.
4. Отдельный LLM critic/judge advisory port без completion authority.
5. Plugin Generator как динамический source новых tools.
6. Human approval gate для рискованных mutations.
7. Replay и сравнение loop strategies на одном event log.
