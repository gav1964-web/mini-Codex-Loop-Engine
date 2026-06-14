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
- policy-driven invocation admitted generated plugins;
- isolated hash-verifying plugin worker;
- timeout/process-tree/output bounds для generated plugin execution;
- strict JSON output validation generated plugins;
- bounded command integration verifier;
- external immutable parent verification policy;
- global owner/run-aware process registry;
- periodic subprocess heartbeat и terminal outcomes;
- identity-safe stale process reaper;
- bounded periodic process-reaper service;
- persistent provider-neutral service-run reports;
- persistent release history и bounded regression trends;
- bounded terminal-record retention policy;
- command digest persistence без raw argv;
- bounded parallel execution independent ready leaves;
- external parallel-safe capability allowlist;
- immutable read/write resource claims;
- canonical workspace claim identities;
- snapshot-isolated leaf workers;
- deterministic parallel result application;
- fail-closed generated plugin sandbox contract;
- WSL bubblewrap sandbox launcher;
- strict automated sandbox release gate;
- explicit read-only data и writable output mounts;
- no-fallback policy для strict plugin invocation;
- context-bound decomposition decision replay;
- versioned atomic replay traces;
- deterministic decomposition strategy metrics;
- topology/outcome fingerprint grouping;
- explicit lexicographic strategy judge policy;
- external parent integration routing;
- typed structural integration selectors;
- ordered all-of integration plans;
- snapshot-isolated composite verifier execution;
- fail-closed integration result aggregation;
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
93. End-to-end `missing -> acquire -> admit -> invoke -> validate`.
94. Запуск generated plugin только по external invocation allowlist.
95. Запрет task metadata override для plugin payload.
96. Повторная SHA-256 проверка исполняемых байтов в child worker.
97. Timeout generated plugin с завершением process tree.
98. Structured failure для non-object plugin output.
99. Structured block для capability без invocation admission.
100. Подавление произвольного plugin stdout до JSON envelope.
101. Parent завершается только после bounded integration command.
102. Ненулевой integration exit code завершает parent как failed.
103. Timeout integration command завершает parent как blocked.
104. Task metadata не может подменить integration command или bounds.
105. Parent без admitted integration command блокируется.
106. Integration cwd за пределами workspace отклоняется policy.
107. Каждый bounded subprocess получает owner/run process record.
108. Heartbeat обновляется во время выполнения процесса.
109. Completed/failed/timed_out фиксируются как terminal outcomes.
110. Persistent process registry переживает повторную загрузку.
111. Stale process завершается только при совпадении PID identity.
112. Переиспользованный или исчезнувший PID помечается lost.
113. Raw argv и секретные аргументы не записываются в registry.
114. Старые terminal records удаляются bounded pruning.
115. Независимые admitted leaves выполняются с bounded parallelism.
116. Dependency leaf не запускается до завершения predecessor.
117. Не admitted capability остаётся последовательной.
118. Leaf budget резервируется до запуска parallel batch.
119. Parallel results применяются в стабильном node-id order.
120. Worker mutation не меняет живой TaskGraph.
121. Parallel policy требует явного safe capability allowlist.
122. Default policy сохраняет прежний assess/execute order.
123. Snapshot failure становится structured leaf failure.
124. Strict plugin без sandbox configuration блокируется.
125. Unavailable sandbox не даёт fallback в direct process.
126. Strict plugin запускается только через configured launcher.
127. Bubblewrap command использует unshare-all и clearenv.
128. Plugin bundle и trusted worker монтируются read-only.
129. Writable mounts разрешены только под `/output`.
130. Sandbox mount targets обязаны быть уникальными.
131. Production WSL probe фиксирует отсутствующий bubblewrap как unavailable.
132. После установки bubblewrap production probe возвращает available.
133. Реальный strict plugin читает read-only `/data`.
134. Реальная запись в `/data` блокируется.
135. Немонтированный Windows workspace не виден sandbox.
136. Network connection из sandbox блокируется.
137. Запись в explicit writable `/output` проходит.
138. Канонический smoke runner воспроизводит весь isolation gate.
139. Recorded decisions воспроизводятся на fresh graph.
140. Изменённый node context блокирует replay.
141. Отсутствующий trace node даёт structured decomposer failure.
142. Duplicate trace nodes и invalid SHA-256 отклоняются.
143. Strategy runner обнаруживает topology divergence.
144. Strategy runner обнаруживает outcome divergence.
145. Эквивалентные стратегии получают стабильные fingerprints.
146. Comparison сохраняется как versioned JSON.
147. Пустой набор strategies отклоняется.
148. Exact parent route запускает ordered all-of checks.
149. Все integration checks выполняются даже после failed/blocked.
150. Failed check имеет приоритет над blocked.
151. Exact route переопределяет default plan.
152. Task metadata не может подменить route или plan.
153. Каждый verifier получает независимый graph snapshot.
154. Exception verifier-а становится failed check.
155. Missing route блокирует parent.
156. Unknown/duplicate verifier names отклоняются policy.
157. Unknown verifier status завершается fail-closed.
158. Неверный тип verifier result завершается fail-closed.
159. Созданные exact routes нельзя изменить через исходный или policy mapping.
160. Reaper service выполняет первый stale sweep немедленно.
161. Reaper service завершается по обязательному `max_cycles`.
162. Stop event прерывает interval wait.
163. Pre-requested stop не запускает sweep.
164. Exception reaper-а становится structured failed report.
165. Malformed reaper output завершается fail-closed.
166. Service lock освобождается после failed run.
167. Одновременный второй service run одного registry отклоняется.
168. Cycle report считает terminated/lost и сохраняет record ids.
169. Policy отклоняет неположительные stale/interval/cycle bounds.
170. Write claims разных workspaces допускают parallel mutation.
171. Пересекающиеся write claims не попадают в один batch.
172. Конфликтующий leaf не блокирует следующий независимый leaf.
173. Mutation capability без write claim остаётся последовательной.
174. Task metadata не может подменить external claims.
175. Shared read claims совместимы, read/write конфликтуют.
176. Claim mapping копируется и становится immutable.
177. Прямой dataclass constructor не обходит validation.
178. Workspace helper канонизирует эквивалентные paths.
179. Release gate запускает canonical smoke через bounded supervisor.
180. Status passed требует всех восьми isolation checks.
181. Unavailable backend блокирует release по умолчанию.
182. Explicit degraded mode никогда не возвращает passed.
183. Missing/false check завершается fail-closed.
184. Malformed JSON и timeout завершаются fail-closed.
185. Output truncation и неожиданный outcome завершаются fail-closed.
186. Launch failure создаёт structured failed report.
187. Report path ограничен workspace и записывается атомарно.
188. Прямой policy constructor не обходит validation.
189. Degraded mode принимает только точную unavailable ошибку backend-а.
190. Judge ранжирует только по ordered external objectives.
191. Objective поддерживает явное направление min/max.
192. Равные objective tuples сохраняют общий rank.
193. Ineligible root statuses остаются видимыми без rank.
194. Eligibility set задаётся external policy.
195. Ranking report versioned и записывается атомарно.
196. Empty comparison и duplicate names отклоняются.
197. Run другого case отклоняется fail-closed.
198. Unknown/duplicate objectives отклоняются.
199. Eligibility policy отклоняет неизвестные task statuses.
200. Exact integration route имеет приоритет над typed selectors.
201. Ordered selectors используют deterministic first-match.
202. Node-id prefix selector выбирает reusable route.
203. Depth selector выбирает reusable route.
204. Required-capability selector выбирает reusable route.
205. Default plan применяется только после selector miss.
206. Metadata не может создать или подменить selector match.
207. Selector routes копируются в immutable tuple.
208. Duplicate selector route names отклоняются.
209. Unknown verifier в selector plan отклоняется.
210. Evidence сохраняет selector kind и value.
211. Terminal pruning поддерживает explicit per-cycle limit.
212. Terminal records удаляются oldest-first.
213. Running records никогда не удаляются retention policy.
214. Retention запускается только по заданной cycle cadence.
215. Retention отключена без explicit policy.
216. Cycle report сохраняет pruning attempt и count.
217. Pruning error не стирает completed reaping evidence.
218. Invalid pruner count завершается fail-closed.
219. Retention bounds валидируются при создании policy.
220. Reaper policy требует typed retention contract.
221. Два manager instances координируют write lease через общий registry.
222. Shared read leases совместимы, writer блокируется.
223. Набор claims приобретается атомарно без частичной резервации.
224. Lease умершего process очищается только по PID identity mismatch.
225. Contention не расходует leaf attempts и execution budget.
226. Lease освобождается после executor failure.
227. Lease policy и versioned registry fail closed на невалидном контракте.
228. Stale lock без timestamp восстанавливается по filesystem mtime.
229. Malformed lease-manager response блокируется как contract error.
230. Отдельный spawned process удерживает write lease от второго process.
231. Composite gate требует pytest, wheel smoke и strict sandbox.
232. Failed stage не отменяет запуск последующих release stages.
233. Несколько failed stages сохраняются в стабильном порядке.
234. Exact unavailable sandbox допускает только visible degraded status.
235. Unexpected sandbox block остаётся failed при degraded-ok.
236. Timeout и output truncation каждого stage завершаются fail-closed.
237. Malformed sandbox JSON блокирует composite release.
238. Launch failure одного stage не стирает evidence других.
239. Composite report имеет canonical JSON shape и atomic write.
240. Expired lease очищается при живом owner process.
241. Renew продлевает TTL и сохраняет exclusion.
242. Heartbeat удерживает lease дольше исходного TTL.
243. Renewal failure переводит completed worker result в failed.
244. Невалидный heartbeat contract блокирует operation и освобождает lease.
245. Runner измеряет elapsed milliseconds через injected monotonic clock.
246. External usage provider добавляет token и cost evidence.
247. Без provider usage metrics остаются явно неизмеренными.
248. Невалидный usage contract отклоняется fail-closed.
249. Judge ранжирует по measured latency и comparable cost.
250. Missing usage objective и mixed cost basis блокируют ranking.
251. Direct metrics constructor проверяет measurement shape.
252. Usage provider не может переписать уже captured topology/outcome metrics.
253. Monotonic clock regression отклоняется вместо подстановки нулевой latency.

## Результаты проверок

- `pytest`: 246 passed, 1 symlink test skipped из-за ограничений Windows;
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
- bounded generated plugin runtime targeted tests: 13 passed;
- wheel `0.12.0` успешно собран;
- bounded parent integration targeted tests: 18 passed;
- wheel `0.13.0` успешно собран;
- process registry integration tests: 22 passed;
- wheel `0.14.0` успешно собран;
- parallel scheduler targeted tests: 21 passed;
- wheel `0.15.0` успешно собран;
- generated plugin sandbox targeted tests: 20 passed;
- wheel `0.16.0` успешно собран;
- real WSL bubblewrap isolation smoke: passed;
- canonical `python -m examples.plugin_sandbox_smoke`: 8/8 checks passed;
- wheel `0.16.1` успешно собран;
- canonical decomposition comparison: atomic 1 node против staged 4 nodes;
- replay/decomposition targeted tests: 32 passed;
- wheel `0.17.0` успешно собран;
- canonical integration composition demo: 2/2 required checks passed;
- integration composition targeted tests: 30 passed;
- wheel `0.18.0` успешно собран, установлен в чистый каталог и проверен через
  публичные экспорты composition contracts;
- process reaper service targeted tests: 20 passed;
- canonical process reaper service demo: 1 stale record terminated за 2 cycles;
- wheel `0.19.0` успешно собран и проверен через публичные service exports;
- resource claim + parallel scheduler targeted tests: 18 passed;
- canonical resource claims demo: 2 independent workspace writes overlapped;
- wheel `0.20.0` успешно собран и проверен через публичные claim exports;
- sandbox release targeted tests: 22 passed;
- strict `python -m tools.sandbox_release_gate`: passed, 8/8 checks;
- wheel `0.21.0` успешно собран и проверен через release-gate contracts;
- strategy judge + replay targeted tests: 17 passed;
- canonical comparison demo создал neutral comparison и explicit ranking;
- wheel `0.22.0` успешно собран и проверен через public judge exports;
- typed integration selector targeted tests: 28 passed;
- canonical integration demo выбрал plan через selector `depth=0`;
- wheel `0.23.0` успешно собран и проверен через public selector exports;
- process retention/reaper targeted tests: 33 passed;
- canonical reaper demo удалил old terminal record и сохранил fresh reaped record;
- wheel `0.24.0` успешно собран и проверен через public retention exports;
- cross-process lease targeted tests: 28 passed вместе с claims/parallel contour;
- canonical resource lease demo: contention до release, acquire после release;
- wheel `0.25.0` успешно собран и проверен через public lease exports;
- composite release-gate targeted tests: 19 passed вместе с sandbox contour;
- wheel helper собрал, установил и импортировал wheel `0.26.0`;
- canonical composite gate сохранил pytest, wheel и sandbox stage evidence;
- strict `python -m tools.release_gate`: passed, все 3 stages releasable;
- composite sandbox stage: `wsl_bubblewrap`, 8/8 checks passed;
- heartbeat/lease targeted contour: 33 passed;
- canonical lease demo удержал write lease после initial TTL и освободил его;
- wheel `0.27.0` проверен через canonical composite release gate;
- strategy measurement + judge targeted contour: 25 passed;
- measured comparison demo записал schema v2 usage/latency metrics;
- wheel `0.28.0` проверен через canonical composite release gate;
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
- расширенная coding-specific verification помимо exit code;
- human approval gates.

## Оценка

MVP подтверждает архитектурную гипотезу: полезные механизмы mini-Codex 7 можно
представить как небольшой универсальный kernel, не привязанный к dialog shell.

Проект пока не является автономным coding agent. Это основание, поверх которого
такого агента можно построить без повторного смешивания planner, tools,
verification и stop logic.

Версия `0.25.0` добавляет optional cross-process resource lease backend.
Scheduler атомарно резервирует canonical claims всего batch до изменения
attempts и execution budget. Contention и registry errors становятся
structured blocked outcomes.

Lease records связаны с PID identity. Падение process допускает безопасное
удаление orphan records.

Версия `0.26.0` добавляет canonical composite release gate. Полный pytest,
чистый wheel build/install/import и strict real sandbox выполняются как три
независимых bounded stages без short-circuit.

Production status `passed` возможен только при успехе всех stages. Exact
unavailable sandbox может дать только явно запрошенный non-production status
`degraded`; остальные failures остаются блокирующими.

Версия `0.27.0` добавляет heartbeat/expiry lease lifecycle. Active operation
продлевает ownership до release; зависший scheduler ownership истекает даже
при живом process. Renewal failure делает task result failed.

TTL не заменяет fencing: внешний side effect, уже выполняющийся после потери
lease, требует отдельного fencing token или cancellable process boundary.

Версия `0.28.0` добавляет measured strategy evidence. Runner измеряет latency,
а token/cost данные принимает только через внешний typed provider. Judge может
использовать их как explicit objectives и fail-closed отклоняет missing evidence
или несовместимые cost bases.

Версия `0.29.0` добавляет compound typed selectors с bounded `all`/`any`
composition. Selector algebra отделена от integration verifier composition,
группы immutable и ограничены по глубине и общему числу узлов.

Route precedence не изменился: exact, ordered first-match selectors, default.
Metadata не получила routing authority, а evidence сохраняет нормализованное
дерево выбранного selector expression.

Полная регрессия также выявила Windows timing race в stale directory-lock
fallback: mtime каталога теперь фиксируется до чтения отсутствующего
`created_at`, поэтому recovery не зависит от побочных filesystem lookup.

Версия `0.30.0` добавляет общий `ServiceRunReport` и typed sink boundary.
Process reaper сохраняет versioned atomic operational reports с run identity,
timestamps, cycle/reaping/pruning metrics и structured cycle details.

JSON store проверяет path-safe identifiers, поддерживает bounded newest-first
listing и возвращает immutable snapshots. Если явно настроенная persistence
недоступна, service run завершается видимой ошибкой, а не теряет observability
молча.

Версия `0.31.0` архивирует каждый canonical composite release report и строит
deterministic trends по bounded rolling window. Status downgrade считается
регрессией безусловно; duration regression требует одновременно относительного
и абсолютного превышения rolling median.

Trend CLI работает analyze-only по умолчанию, поэтому не дублирует snapshot,
уже сохранённый release gate. Внешний report импортируется только явным
`--record-report`.

Recovery не обещает exactly-once для action, оборванного внутри внешнего side
effect до записи checkpoint. Такие tools должны быть идемпотентными или
использовать idempotency key.
