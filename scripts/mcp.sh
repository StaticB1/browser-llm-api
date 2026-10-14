#!/usr/bin/env bash
# MCP stdio entry point. Wraps the MCP server so a missing venv self-repairs
# instead of failing the handshake with ENOENT on venv/bin/python — which is
# exactly what happened on 2026-09-07 and cost the session its browser-llm
# tools for the whole conversation, with no way to reattach short of a restart.
#
# stdout carries JSON-RPC protocol frames ONLY. ensure-venv.sh writes every
# message (pip's included) to stderr for that reason; do not "tidy" that away.
set -uo pipefail
# This script lives in scripts/; everything below runs from the checkout root.
cd "$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
./scripts/ensure-venv.sh >&2 || exit 1
exec venv/bin/python -m browser_llm mcp "$@"
