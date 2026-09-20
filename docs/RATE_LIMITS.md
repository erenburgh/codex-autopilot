# Rate limits

Sol and Astra share the account allowance. Codex Autopilot models one account-wide limit and never switches models to evade it.

Structured App Server errors and `account/rateLimits/read` drive retry. If windows report 100% use, the dispatcher waits for the latest reset among those exhausted windows plus five seconds. It ignores longer windows that remain below 100%. If App Server reports only that a bucket was reached, it conservatively uses the latest reset in that bucket. Without a timestamp it uses exponential backoff from 30 seconds to 15 minutes. The default budget is 5 attempts per failure signature (`retry.maximum_attempts`); reaching it opens a ticket for the on-call engineer rather than stopping the run.

Waiting uses a local timer and sends no model request. A failed turn is retired; retry creates a fresh thread for the same milestone using the same deterministic model route. Pause remains available while waiting.

The state machine and reset selection are deterministic-tested. v0.6 live testing observed a real five-hour exhaustion and exact primary reset selection. Waiting through that real reset and weekly exhaustion remain unverified.
