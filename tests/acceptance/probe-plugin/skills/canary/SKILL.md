---
description: "Isolation probe for the SkillFlow acceptance suite (SF-49 A3). Reports whether a canary token is visible in its forked context."
context: fork
background: false
user-invocable: false
---

# probe:canary

You are an isolation probe: your only job is to report whether a canary
token is visible in your context. Do exactly this and nothing else.

Your context may contain a canary token of the form `CANARY-<hex>`.

- If you can see such a token, reply with ONLY the token.
- Otherwise reply with ONLY the exact string `NO_CANARY`.

Do not invoke any tool. Do not start, dispatch, or invoke another step.
