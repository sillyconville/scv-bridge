# -*- coding: utf-8 -*-
"""The environment variable names that actually showed up (⛔ not the values) in the two agent-CLI families'
sessions — the fixture for Task 15c. ⛔ Do not hand-edit: it is exported from the real environment by 15c Phase A's
export script (`export_agent_env.py`, not in the repository; to update it, rerun it inside a fresh agent session).
⭐ This file holds only the facts (which names showed up); which ones should be stripped is the explicit decision
written in `tests/test_20_argv.py::ChildEnv` (lead's 15c ⑦1 ruling) — the two are kept apart, ⛔ never derived
backwards from scv.
  · `CLAUDE_CODE_SESSION`: inside a Claude Code 2.1.282 session (a shell started by the Bash tool), names that land
    in either CLI family's namespace (CLAUDE*/CODEX*/ANTHROPIC_*/OPENAI_*) or under AI_AGENT/TRACEPARENT — when the
    agent runs `scv start` on the user's behalf, this is the set the bridge inherits;
  · `CODEX_SHELL_ADDED`: names the codex-cli 0.155.0-alpha.16 shell tool adds for the child process (measured on
    Phase A's 8th run; names only, including the one the player's own `shell_environment_policy.set` configures)."""
CLAUDE_CODE_SESSION = ("AI_AGENT", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_USE_POWERSHELL_TOOL",
    "CLAUDE_EFFORT", "CLAUDE_PID",)
CODEX_SHELL_ADDED = ("CLAUDE_CODE_USE_POWERSHELL_TOOL", "CODEX_CI", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
    "CODEX_VERSION", "COLORTERM", "GH_PAGER", "GIT_PAGER", "LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR",
    "PAGER",)
