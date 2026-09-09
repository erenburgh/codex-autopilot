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
