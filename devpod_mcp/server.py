"""DevPod MCP server.

Gives agents read/run/stop access to *existing* DevPod workspaces by shelling
out to the local `devpod` CLI. It deliberately does NOT expose any way to
create or start workspaces -- provisioning is a human-only action performed with
the user's dedicated create tool.

When the agent hits something only the human can do (expired AWS auth, or the
need for a brand-new devpod), the tools return clear hand-off instructions
rather than failing opaquely.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Allow overriding the devpod binary / context / provider via env if ever needed.
DEVPOD_BIN = os.environ.get("DEVPOD_BIN", "devpod")
DEVPOD_CONTEXT = os.environ.get("DEVPOD_CONTEXT")  # None -> CLI default
DEVPOD_PROVIDER = os.environ.get("DEVPOD_PROVIDER")  # None -> CLI default

# The exact command the user runs in their terminal to refresh AWS creds.
AWS_AUTH_COMMAND = os.environ.get("DEVPOD_AWS_AUTH_COMMAND", "ne-okta")

# Substrings (case-insensitive) in CLI output that mean "AWS credentials are
# expired / invalid". Confirmed live from devpod + aws CLI output.
_AUTH_ERROR_MARKERS = (
    "expiredtoken",
    "requestexpired",
    "request has expired",
    "security token included in the request is expired",
    "the security token included in the request is invalid",
    "unabletoretrieve",
    "no valid credential sources",
    "could not load credentials",
    "expired or invalid",
    "credentials have expired",
)

AWS_AUTH_HELP = (
    "AWS credentials for DevPod appear to be expired or invalid, so the "
    "devpod/EC2 call failed.\n\n"
    "ACTION NEEDED FROM THE USER (the agent cannot do this itself -- it "
    "requires an interactive Okta browser login):\n"
    f"    Run `{AWS_AUTH_COMMAND}` in your terminal to re-authenticate to AWS, "
    "then ask me to retry.\n\n"
    "Once that command completes, the same devpod operation will work."
)

CREATE_WORKSPACE_HELP = (
    "Creating or starting a NEW devpod is a human-only action and is not "
    "available through this server.\n\n"
    "ACTION NEEDED FROM THE USER:\n"
    "    Please create/start the devpod yourself with your dedicated devpod "
    "creation tool. Once it is up and shows in `list_devpods`, I can run "
    "commands on it, check its status, or stop it."
)

server = MCPServer(
    "devpod",
    instructions=(
        "Interact with the user's EXISTING DevPod workspaces (ephemeral EC2 "
        "instances for heavy analysis). You can list them, check status, run "
        "arbitrary commands inside them, and stop them. You CANNOT create or "
        "start new ones -- that is a human-only action. If a tool reports that "
        "AWS auth has expired, relay its instruction asking the user to run the "
        "auth command, then retry. If the user asks for a devpod that does not "
        "exist, tell them it must be created by hand."
    ),
)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _looks_like_auth_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _AUTH_ERROR_MARKERS)


def _base_cmd() -> list[str]:
    cmd = [DEVPOD_BIN]
    if DEVPOD_CONTEXT:
        cmd += ["--context", DEVPOD_CONTEXT]
    if DEVPOD_PROVIDER:
        cmd += ["--provider", DEVPOD_PROVIDER]
    return cmd


def _run_devpod(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a devpod CLI command. No timeout by default (heavy jobs can run long)."""
    if shutil.which(DEVPOD_BIN) is None and not os.path.isabs(DEVPOD_BIN):
        raise FileNotFoundError(
            f"The `{DEVPOD_BIN}` CLI was not found on PATH. The MCP server must "
            "run in an environment where devpod is installed and on PATH "
            "(launch it from a login shell)."
        )
    return subprocess.run(
        _base_cmd() + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _list_workspaces() -> list[dict[str, Any]]:
    proc = _run_devpod(["list", "--output", "json"])
    if proc.returncode != 0:
        combined = (proc.stdout or "") + (proc.stderr or "")
        if _looks_like_auth_error(combined):
            raise _AuthError(combined)
        raise RuntimeError(f"`devpod list` failed:\n{combined.strip()}")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        raise RuntimeError(f"Could not parse `devpod list` output:\n{proc.stdout}")


def _workspace_names() -> list[str]:
    return [w.get("id", "") for w in _list_workspaces() if w.get("id")]


class _AuthError(Exception):
    """Raised internally when CLI output indicates expired/invalid AWS creds."""


def _unknown_workspace_message(name: str, known: list[str]) -> str:
    known_list = ", ".join(sorted(known)) or "(none found)"
    return (
        f"No existing devpod named '{name}'.\n\n"
        f"Known devpods: {known_list}\n\n" + CREATE_WORKSPACE_HELP
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "List all of the user's existing DevPod workspaces (name, provider, "
        "machine, source folder, last-used time). Use this to discover which "
        "devpods exist before running commands, checking status, or stopping "
        "one. This is the authoritative set of devpods the agent may act on."
    )
)
def list_devpods() -> str:
    try:
        workspaces = _list_workspaces()
    except _AuthError:
        return AWS_AUTH_HELP
    except (RuntimeError, FileNotFoundError) as exc:
        return str(exc)

    if not workspaces:
        return "No devpods exist. " + CREATE_WORKSPACE_HELP

    lines = [f"{len(workspaces)} devpod(s):\n"]
    for w in workspaces:
        lines.append(
            f"- {w.get('id', '?')}"
            f"  [provider={w.get('provider', {}).get('name', '?')},"
            f" machine={w.get('machine', {}).get('machineId', '?')},"
            f" last_used={w.get('lastUsed', '?')}]"
            f"\n    source: {w.get('source', {}).get('localFolder', '?')}"
        )
    return "\n".join(lines)


@server.tool(
    description=(
        "Show the status of one existing devpod workspace (e.g. Running, "
        "Stopped). Pass the exact workspace name from list_devpods."
    )
)
def devpod_status(name: str) -> str:
    try:
        known = _workspace_names()
    except _AuthError:
        return AWS_AUTH_HELP
    except (RuntimeError, FileNotFoundError) as exc:
        return str(exc)

    if name not in known:
        return _unknown_workspace_message(name, known)

    proc = _run_devpod(["status", name, "--output", "json"])
    combined = (proc.stdout or "") + (proc.stderr or "")
    if _looks_like_auth_error(combined):
        return AWS_AUTH_HELP
    if proc.returncode != 0:
        return f"`devpod status {name}` failed:\n{combined.strip()}"

    # devpod status --output json prints a JSON object; surface it readably.
    try:
        data = json.loads(proc.stdout)
        return json.dumps(data, indent=2)
    except json.JSONDecodeError:
        return proc.stdout.strip() or combined.strip()


@server.tool(
    description=(
        "Run an arbitrary shell command inside an existing devpod workspace "
        "(over the devpod SSH tunnel) and return its stdout, stderr, and exit "
        "code. Full access -- no command filtering, no timeout, no output "
        "truncation. Pass the exact workspace name from list_devpods and the "
        "command string to execute. The devpod must already be running; if it "
        "is stopped, start it first via the human-only creation tool."
    )
)
def run_command(name: str, command: str) -> str:
    try:
        known = _workspace_names()
    except _AuthError:
        return AWS_AUTH_HELP
    except (RuntimeError, FileNotFoundError) as exc:
        return str(exc)

    if name not in known:
        return _unknown_workspace_message(name, known)

    proc = _run_devpod(["ssh", name, "--command", command])
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and _looks_like_auth_error(combined):
        return AWS_AUTH_HELP

    parts = [f"exit_code: {proc.returncode}"]
    parts.append("--- stdout ---\n" + (proc.stdout or ""))
    if proc.stderr:
        parts.append("--- stderr ---\n" + proc.stderr)
    return "\n".join(parts)


@server.tool(
    description=(
        "Stop an existing, running devpod workspace (tears down the ephemeral "
        "instance's running state; it can be started again later by the human "
        "creation tool). Pass the exact workspace name from list_devpods. This "
        "does NOT delete the workspace."
    )
)
def stop_devpod(name: str) -> str:
    try:
        known = _workspace_names()
    except _AuthError:
        return AWS_AUTH_HELP
    except (RuntimeError, FileNotFoundError) as exc:
        return str(exc)

    if name not in known:
        return _unknown_workspace_message(name, known)

    proc = _run_devpod(["stop", name])
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and _looks_like_auth_error(combined):
        return AWS_AUTH_HELP
    if proc.returncode != 0:
        return f"`devpod stop {name}` failed:\n{combined.strip()}"
    return f"Stopped devpod '{name}'.\n{combined.strip()}"


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
