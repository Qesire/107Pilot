## Browser Automation

All 107Pilot browser operations must go through:

    pilot-browser ...

Do not call `agent-browser` directly for this project.

Reason: `agent-browser` uses a daemon and Unix domain socket for cross-command
browser control. Codex command sandboxing can block that IPC path, so direct
calls can split `open`, `snapshot`, and actions across different execution
boundaries. The `pilot-browser` wrapper pins one session, namespace, socket
directory, launch configuration, and localhost-only domain policy.

For `@eN` refs:

1. Run `pilot-browser snapshot -i`.
2. Use the returned ref immediately in the same browser session.
3. Re-run snapshot after navigation, refresh, submit, polling updates, or DOM
   replacement.
4. Prefer role/name semantic locators when they are available.
