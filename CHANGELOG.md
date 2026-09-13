# Changelog

## 0.9.0-beta (unreleased; independent audit blocked)

- Added independent contract regressions for the required v0.9 execution
  default, Desktop-owned start surface, exact thread-title formats, canonical
  project association, and deterministic verification promotion.
- Added separate deterministic AI Studio acceptance shapes for independent
  implementation branches plus integration, a research/analysis/fact-check
  pipeline, and mixed code/Computer Use scheduling.
- Added the required dependency-graph, parallel-execution, roles,
  resource-locks, thread-naming, project-association, and testing documents.
- Recorded release-blocking candidate gaps in
  `docs/RELEASE_VERIFICATION_0.9.0-beta.md`; no release, tag, push, or publish
  was performed.

## 0.8.1-beta

Ревизия после первой успешной приёмки 0.8.0: снято то, что не могло
выполниться, и исправлено то, что обещало невыполнимое.

### Снято как недостижимое

- `orchestrator.py` (1130 строк) и поверхность `headless_app_server`.
  `run`, `resume` и `_dispatch` отказывали при `desktop_owned`, а
  умолчание всех команд было именно `desktop_owned`. Живыми входами
  оставались только `smoke.py` и тесты. Вместе с ними ушли `smoke.py`,
  команды `run`, `_dispatch`, `test desktop`, `restore-app-server` и
  `restore_app_server_transport`.
- Механизм заранее созданных слотов: `add-worker-slot`,
  `append_worker_slot`, `worker_thread_ids`, `worker_slot_cursor`,
  `_validate_worker_slots`, фазы `WAITING_PROJECT_SLOT*`. Он был обходом
  вокруг мнимой невозможности завести видимую задачу через App Server;
  посылка опровергнута - шесть воркеров приёмки 0.8.0 все оказались
  внутри проекта.
- Повторная привязка ветки к проекту после создания. Код строкой выше
  отклоняет создание, если ветка не в нужном проекте, то есть
  `thread/metadata/update` привязывал привязанное. v0.7 его не вызывает.
- Параметр `threadSource` в `start_thread`: значение
  `agent_created_thread` помечало задачу созданной другим приложением.
- Восемь функций, не упомянутых нигде: `system_roles`,
  `build_pipeline_engineer_prompt`, `send_message_payload`, `_transport`,
  `_require_transport_claim`, `_healthcheck_passed`, `_sha256`,
  `_validate_owner_against_state`.

### Исправлено

- Дорожка Pipeline Engineer получила процедуру. Прежде скилл обещал, что
  DevOps починит и перезаведёт, а кода, создающего инженера, не было
  вовсе: `ensure_pipeline_engineer` меняет поле в JSON. Теперь названа
  последовательность из существующих защищённых команд, и отдельно
  сказано, что неизвестный побочный эффект остаётся остановкой.
- Отчёт о запуске больше не выдаётся за видимый. Stop-хук обязан отвечать
  `continue`, иначе инициирующий ход остаётся `interrupted` и диспетчер не
  стартует; значит отчёт не показывается. Инициирующий ход обязан назвать
  фразу `статус`, которая идёт через `UserPromptSubmit` и видима.
- Версия MCP-сервера памяти бралась из прибитой строки и разошлась бы с
  пакетом при любом подъёме версии.
- Умолчание `worker_surface` в конфиге было `headless_app_server`: новый
  прогон получал неработающую поверхность, если её не выбрали явно.

### Покрытие

- `test_model_routing.py` - маршрутизация моделей напрямую, без мёртвого
  оркестратора: таблица маршрутов и отсутствие тихой подмены модели.
- `test_skill_promises.py` - скилл не вправе обещать того, чего рантайм не
  делает; проверяет и то, что процедура не называет несуществующих команд.
- Снято 24 теста мёртвого пути, `test_recovery.py` и
  `test_context_budget.py` целиком: последний мерил рост промпта сборкой,
  которой больше нет, а у живой есть жёсткий предел `MAX_PROMPT_CHARS`.

## 0.8.0-beta

- Added clean-machine preflight for target root, Git, installed runtime, official App Server, `:workspace`, target cwd, built-in Project Memory MCP, SQLite FTS5, and Adaptive model metadata.
- Added an explicit code-77 approval path for official App Server access to its exact `CODEX_HOME`, with no project run-state created on failure.
- Added a short-lived per-user launch registry so an initiating task can safely start Autopilot for a different target repository after `turn/completed`.
- Added project-local evidence-backed Project Memory using SQLite/FTS5 and a bundled stdio MCP server bound to each worker's target cwd. The public MCP surface is one user-approved `memory` tool with 14 strict operations.
- Separated Truth, Decisions, Constraints, Questions, Observations, Evidence, and Conflicts. Truth requires validated non-migration evidence.
- Added bounded retrieval, stable IDs, pagination, audit history, milestone evidence gates, integrity checks, online backups, and recovery from the latest verified milestone backup.
- Made `PROJECT_STATE.md` and `DECISIONS.md` generated views; made `HANDOFF.md` advisory and capped at 8 KiB.
- Added conservative v0.7 migration with a complete backup and zero automatic promotion of old agent prose to Truth.
- Preserved serial visible worker rotation, deterministic Sol/Astra routing, Host Settings omission, rate-limit waiting, and approval fail-closed behavior.
- Added an explicit first-use Project Memory trust probe. The user chooses persistent `Always` trust in Codex; production code never answers that approval.

## 0.7.0-beta

- Added deterministic AUTO routing between GPT-5.6 Sol and GPT-6 Astra, explicit execution modes, model metadata validation, and AUTO-only capability escalation.

## 0.6.0-beta

- Introduced the model-neutral App Server core, trusted lifecycle hooks, serial visible workers, deterministic controls, installer, and clean release package.
