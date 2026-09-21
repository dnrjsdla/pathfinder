# HISTORY

What reality changed after the structure was agreed. The blueprint is not edited after
2026-09-22; this file is.

## 2026-09-22 — Partition 0 started

- Repository initialized (private first, noreply commit identity, pre-commit personal-data scan).
- Free-plan probe on the owner's Ollama account, one native tool call per model through the
  OpenAI-compatible endpoint: succeeded on `gpt-oss:20b-cloud`, `gpt-oss:120b-cloud`,
  `gemma4:31b-cloud`, `nemotron-3-nano:30b-cloud`, `nemotron-3-super:cloud`; HTTP 402
  ("not included in your free usage") on `glm-5.1`, `glm-5.3`, `deepseek-v4-flash`, `kimi-k3`,
  `qwen3.5`, `minimax-m2.7`. Three free families exist, so m1 can run as designed.
- Finding: the free plan is starter models + a small undisclosed monthly credit + 1 concurrent
  request. m6 was therefore changed from "run a synthetic day three times" to "sample each turn
  type, extrapolate to a day, price it" so the measurement does not burn the month's credit.
