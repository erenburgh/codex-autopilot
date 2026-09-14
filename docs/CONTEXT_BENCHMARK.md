# Context benchmark

This deterministic synthetic benchmark adds five Observations and five Constraints per milestone. The v0.8 column measures the real worker prompt builder. The v0.7 column is a labeled synthetic baseline that prepends all accumulated prose; it is not a measurement from a live v0.7 model run.

| Milestone | v0.8 prompt chars | Approx. tokens | Memory records | MCP calls sampled | MCP payload chars | Records returned | Synthetic v0.7 full-history chars |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| M1 | 3576 | 894 | 10 | 1 | 4434 | 5 | 5079 |
| M5 | 3821 | 956 | 50 | 1 | 5319 | 6 | 15414 |
| M10 | 3828 | 957 | 100 | 1 | 5330 | 6 | 28344 |
| M20 | 3828 | 957 | 200 | 1 | 5330 | 6 | 54244 |

Measured v0.8 initial-prompt growth from M1 to M20: **252 characters**.
Synthetic full-history growth over the same fixture: **49165 characters**.

The MCP sample is one bounded FTS query with limit 8 at each checkpoint. Real workers may make more calls depending on the milestone; the server caps each page at 20 records.

## Откуда взялся потолок промпта

`MAX_PROMPT_CHARS` больше не константа из воздуха. Прежде здесь стояло
`64_000` без комментария и без единого упоминания в документации, и это
число составляло примерно шестую часть того, что модель фактически
принимает.

Выводится так:

| величина | значение | откуда |
| --- | ---: | --- |
| окно контекста модели | 258 400 токенов | поле `model_context_window` живого события App Server `turn`, 14.09.2026 |
| доля, отводимая промпту | 0.25 | остальное нужно воркеру на чтение файлов, вывод инструментов и собственный ответ |
| символов на токен | 3.0 | консервативно для смешанного русско-английского JSON |
| **потолок** | **193 800 символов** | произведение |

Для сравнения: на том же прогоне один ход исполнителя израсходовал
144 368 входных токенов — вдевятеро больше прежнего потолка целиком.

## Исходный запрос не копируется в промпт

`acceptance_gate.original_user_request` — ссылка, а не текст: длина,
`sha256` и способ получения через Project Memory (`operation=current`).
Текст пользователя неизменен на весь прогон и сузить его нельзя, поэтому
копия в каждом промпте была чистым повтором.

Замер на реальном плане прогона v1.0 — 23 задачи, запрос 49 739 символов:

| | до | после |
| --- | ---: | ---: |
| промпт M1 | 62 635 из 64 000 | 12 214 из 193 800 |
| `acceptance_gate` | 51 475 | 559 |
| сама задача M1 | 395 | 395 |

До правки задача занимала 0.6 % собственного промпта, а свободного места
оставалось 1 365 символов: первая же задача с зависимостями не собралась
бы вовсе.
