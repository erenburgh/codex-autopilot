# Project Memory

Project Memory is a local evidence store for facts that must survive fresh Codex workers. It reduces two separate risks: growing transcripts degrade context, while copied summaries can turn assumptions into apparent facts.

## Record model

| Entity | Meaning | Required basis |
| --- | --- | --- |
| Truth (`FACT-*`) | Verified current project fact | At least one validated, non-migration Evidence ID |
| Decision (`DEC-*`) | Desired choice with explicit origin and status | May be proposed; agent decisions cannot begin as accepted |
| Constraint (`CON-*`) | Boundary or requirement with origin | Explicit status and provenance |
| Question (`Q-*`) | Open or resolved uncertainty | Lifecycle status |
| Observation (`OBS-*`) | Hypothesis or unverified note | Always unverified; never silently becomes Truth |
| Evidence (`EVID-*`) | File, Git, test, build, tool, artifact, screenshot, user instruction, or environment result | Structured provenance fields |
| Verification (`VERIFY-*`) | Deterministic or fresh-verifier PASS/REVISE audit outcome; never Truth | Existing non-migration Evidence plus task/check/thread/turn identity |
| Conflict (`CONFLICT-*`) | Contradiction requiring resolution | Preserves both sides and resolution history |

The core invariant is **NO EVIDENCE → NO TRUTH**. Migration evidence may preserve an old completion marker but cannot support Truth. File evidence is resolved within the project, rejects path and symlink escapes, validates optional line ranges, and stores the actual file SHA-256. Evidence and records retain the creator, provider, provider thread, and timestamps where supplied.

User correction creates an accepted user-origin decision, supersedes conflicting agent decisions, and opens a conflict when it disagrees with existing Truth. It does not rewrite observed repository state. Contradictions dispute records and preserve an audit trail; resolution is explicit.

## Retrieval and context budget

Records are indexed with SQLite FTS5 using the Unicode tokenizer. Search supports type/status filters, stable ID lookup, a maximum page size of 20, and opaque cursor pagination. Results are concise. Initial worker prompts receive only bounded constraints and relevant IDs; workers retrieve record bodies and evidence when needed.

No embeddings, vector service, external database, background model, or network service is required. The database is `.codex-autopilot/memory.sqlite3` inside the target Git repository.

## Lifecycle and recovery

The database uses foreign keys, WAL journaling, full synchronous commits, explicit read snapshots and `BEGIN IMMEDIATE` writes, transactional sequence IDs, bounded busy handling, and an audit log. A project-local advisory lock serializes writers and file-producing operations across MCP processes; a bounded process-local guard provides the corresponding thread safety. A process crash releases the advisory lock and SQLite rolls back the incomplete transaction.

Rendering, verified-milestone backup, and recovery take the same writer exclusion. Generated views and integrity-checked backup/restore files are fsynced and atomically renamed, so another worker cannot observe a partially written file or write through a restore. At verified milestone completion, Python's SQLite online backup API writes `.codex-autopilot/memory-backups/latest.sqlite3`. Startup checks SQLite integrity and semantic invariants, including that every Truth and verification result has valid non-migration supporting evidence. A broken database is quarantined before restore.

## Design reference

The design study reviewed [Agent Memory Engine](https://github.com/uudam42/agent-memory-engine) at commit `146044dfae3143c1028c0a6b78e193ccfee64802` under its MIT license. The v0.8 implementation is clean-room and shares no source code, schema, package, or runtime dependency with that project.

| Studied concept | v0.8 decision |
| --- | --- |
| Local-first durable memory | Used with a new stdlib SQLite design |
| Full-text search | Used with SQLite FTS5 |
| Evidence and provenance | Used with explicit entities and links |
| Candidate before promotion | Simplified into Observation/Decision/Truth boundaries |
| Conflict history | Used with an explicit conflict ledger |
| Bounded retrieval | Used for fresh-worker context |
| Project path isolation | Used and enforced server-side |
| stdio MCP access | Used with a new allowlisted server |
| FastAPI, SQLAlchemy, Pydantic | Not used |
| Vector embeddings and reranking | Not used |
| Docker, Ollama, external services | Not used |
| Generic document ingestion and branch memory | Not used |

Because no third-party code was copied or distributed, this release does not add a third-party source notice. The upstream design reference remains documented here for transparency.

## Limits

Evidence provenance makes a claim's basis inspectable; it is not a cryptographic guarantee that a worker described a test or tool result correctly. The server itself hashes file evidence. Declared user-instruction provenance is retained but is not independently signed. Semantic conflicts are explicit and ID-linked; v0.8 does not claim to infer every contradiction automatically.
