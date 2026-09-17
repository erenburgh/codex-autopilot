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

## Where the prompt ceiling comes from

`MAX_PROMPT_CHARS` is no longer a constant out of thin air. It used to be
`64_000` with no comment and not a single mention in the documentation, and
that number was roughly a sixth of what the model actually accepts.

It is derived as follows:

| quantity | value | source |
| --- | ---: | --- |
| model context window | 258 400 tokens | the `model_context_window` field of a live App Server `turn` event, 14 Sep 2026 |
| share reserved for the prompt | 0.25 | the rest is needed by the worker for reading files, tool output and its own reply |
| characters per token | 3.0 | conservative for mixed Russian-English JSON |
| **ceiling** | **193 800 characters** | the product |

For comparison: on the same run one executor turn consumed 144 368 input
tokens — nine times the whole previous ceiling.

## The original request is not copied into the prompt

`acceptance_gate.original_user_request` is a reference, not the text: its
length, `sha256` and the way to fetch it through Project Memory
(`operation=current`). The user's text is fixed for the whole run and cannot
be narrowed, so a copy in every prompt was pure repetition.

Measured on the real plan of run v1.0 — 23 tasks, a 49 739-character request:

| | before | after |
| --- | ---: | ---: |
| prompt of M1 | 62 635 of 64 000 | 12 214 of 193 800 |
| `acceptance_gate` | 51 475 | 559 |
| task M1 itself | 395 | 395 |

Before the change the task occupied 0.6 % of its own prompt, and 1 365
characters of free space remained: the first task with dependencies would
not have assembled at all.
