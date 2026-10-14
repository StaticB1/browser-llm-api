#!/usr/bin/env bash
# Apply this repository's GitHub settings: description, topics, private
# vulnerability reporting, branch clean-up, and the unused tabs. Needs an ADMIN
# of the repository, signed in with the GitHub CLI (`gh auth login`); push access
# is not enough (GitHub answers 404). Safe to run again: every step sets a value.
#
#   scripts/github-settings.sh             apply
#   scripts/github-settings.sh --dry-run   print what it would do, change nothing
#
# Not scriptable from here: GitHub Actions has been refused for billing on the
# owning account since 2026-08-18. Clearing that is in the account's Settings >
# Billing and plans; until then CI does not run (see docs/releasing.md).
set -euo pipefail

REPO="${REPO:-StaticB1/browser-llm-api}"
DESCRIPTION="OpenAI-compatible API over your signed-in ChatGPT and Gemini web sessions: chat, vision, image generation, and design tools that make website sections, app screens and brand images. MCP server, dashboard, embeddable widget."
TOPICS=(mcp mcp-server design-tools)

dry=0
[ "${1:-}" = "--dry-run" ] && dry=1

command -v gh >/dev/null 2>&1 || { echo "needs the GitHub CLI: https://cli.github.com" >&2; exit 1; }
who=$(gh api user --jq .login)
admin=$(gh api "repos/$REPO" --jq .permissions.admin)
if [ "$admin" != "true" ] && [ "$dry" = 0 ]; then
  echo "$who is not an admin of $REPO, so GitHub would refuse these changes." >&2
  echo "An admin has to run this (or run it with --dry-run to see the changes)." >&2
  exit 1
fi

run() {
  if [ "$dry" = 1 ]; then printf 'would run:'; printf ' %q' "$@"; echo; else "$@"; fi
}

topic_args=()
for t in "${TOPICS[@]}"; do topic_args+=(--add-topic "$t"); done

# Description and topics for 2.0; delete a pull request's branch once merged;
# hide the Wiki and Projects tabs, which hold nothing (the docs live in docs/).
run gh repo edit "$REPO" \
  --description "$DESCRIPTION" \
  "${topic_args[@]}" \
  --delete-branch-on-merge \
  --enable-wiki=false \
  --enable-projects=false

# SECURITY.md and the issue template send reporters to a private security
# advisory form, which only works with private vulnerability reporting on.
run gh api -X PUT "repos/$REPO/private-vulnerability-reporting" --silent

[ "$dry" = 1 ] && exit 0
echo "Applied as $who. $REPO now has:"
gh api "repos/$REPO" --jq '"  description: \(.description)\n  topics: \(.topics | join(", "))\n  delete branch on merge: \(.delete_branch_on_merge)\n  wiki: \(.has_wiki), projects: \(.has_projects)"'
echo "  private vulnerability reporting: $(gh api "repos/$REPO/private-vulnerability-reporting" --jq .enabled)"
