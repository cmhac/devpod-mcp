# devpod-mcp

An MCP server that lets Kiro agents interact with **existing** [DevPod](https://devpod.sh)
workspaces (ephemeral EC2 instances used for heavy analysis), without the agent
ever opening an SSH connection itself.

## Why this exists

The Kiro agent sandbox blocks SSH on purpose. This server keeps that rule intact:
it runs as a plain local process on *your* machine, in *your* shell environment
(where `devpod` and your AWS creds live), and shells out to the `devpod` CLI on
the agent's behalf. The agent only ever sees MCP tool calls and text results —
it never gets a socket.

## Tools

| Tool | What it does |
|------|--------------|
| `list_devpods` | List all existing devpod workspaces (name, provider, machine, source, last used). This is the authoritative set the agent may act on. |
| `devpod_status` | Show status (Running/Stopped/…) of one existing devpod. |
| `run_command` | Run an **arbitrary** shell command inside a devpod and return stdout/stderr/exit code. **No command filtering, no timeout, no output truncation** — full access. |
| `stop_devpod` | Stop a running devpod (does not delete it). |

### Deliberately NOT provided

There is **no create/start tool**. Spinning up a new devpod is a human-only
action done with your dedicated creation tool. If the agent asks for a devpod
that does not exist, the server returns an instruction telling the user to
create it by hand.

## Human hand-off messages

Two situations require the human, and every tool surfaces a clear instruction
instead of an opaque failure:

1. **Expired/invalid AWS auth.** Detected from devpod/aws error output
   (`RequestExpired`, `ExpiredToken`, etc.). The server tells the user to run
   the auth command in their terminal (default `ne-okta`, an interactive Okta
   browser login the agent cannot perform) and then retry.
2. **Needs a new devpod.** The server points the user at their human-only
   creation tool.

## Requirements

- The `devpod` CLI installed and on `PATH`.
- Valid AWS credentials for the devpod provider (refresh with `ne-okta`).
- Python ≥ 3.12, managed with [uv](https://docs.astral.sh/uv/).

## Run

```bash
uv run devpod-mcp
```

The server speaks MCP over stdio.

## Register with Kiro

Add to `~/.kiro/crew/mcp.json` (`mcpServers`). **Important:** the server must
inherit your interactive shell environment so it can find `devpod` and your AWS
creds — launch it via a login shell:

```json
{
  "mcpServers": {
    "devpod": {
      "command": "/bin/zsh",
      "args": [
        "-l", "-c",
        "uv --directory /Users/hackerc/tools/devpod-mcp run devpod-mcp"
      ]
    }
  }
}
```

## Configuration (env, all optional)

| Var | Default | Purpose |
|-----|---------|---------|
| `DEVPOD_BIN` | `devpod` | Path to the devpod binary. |
| `DEVPOD_CONTEXT` | CLI default | Pass `--context`. |
| `DEVPOD_PROVIDER` | CLI default | Pass `--provider`. |
| `DEVPOD_AWS_AUTH_COMMAND` | `ne-okta` | Command the user is told to run to refresh AWS auth. |
