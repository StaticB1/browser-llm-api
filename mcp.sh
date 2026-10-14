#!/usr/bin/env bash
# Kept at the top level because things outside this repo run it by this path
# (a systemd unit installed before 2.0, Claude's MCP config, other repos).
# The script itself is scripts/mcp.sh.
exec "$(dirname "$(readlink -f "$0")")/scripts/mcp.sh" "$@"
