#!/usr/bin/env bash
# Watchdog for the browser-llm-api service. Silent when healthy.
#
#   ./healthcheck.sh          the scheduled run (integrity + liveness, deep once a day)
#   ./healthcheck.sh --deep   force the end-to-end model ping too
#   ./healthcheck.sh --quiet  no stdout, only the log and any alert
#
# It watches three things, because they fail independently:
#
#   integrity  can venv/bin/python still import the deps? This is the one that
#              matters most, and the only one that catches the failure early.
#              A deleted venv does NOT take the API down: the running process
#              already holds its imports and keeps serving normally, so the
#              damage stays invisible until the next restart — which once meant
#              ten days of a doomed service and then a crash loop. This check
#              repairs it while it is still latent.
#   liveness   is /api/status answering with ok:true? Catches a dead process,
#              a crash loop, or a port nobody is listening on.
#   auth       once a day, one real completion per provider. A provider session
#              expiring is this project's #1 failure mode, and the server stays
#              perfectly healthy while returning nothing, so no free probe sees it.
#
# Where alerts go (all optional, all off by default — it only logs otherwise):
#   BLM_ALERT_DIR   write a Markdown alert file into this directory. Intended
#                   for a chat/notification bridge that watches a queue folder.
#                   Also how you point the watchdog at a scratch dir to drill it
#                   without raising a real alarm.
#   BLM_ALERT_CMD   run this command per alert, with BLM_ALERT_SEVERITY,
#                   BLM_ALERT_TITLE, BLM_ALERT_BODY and BLM_ALERT_KIND in the
#                   environment. Use it for a webhook, ntfy, mail, anything.
#   notify-send     used when present, so a desktop user sees it with no setup.
#
# Each kind has a cooldown, so a long outage does not become a stream of messages.
set -uo pipefail
cd "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/browser-llm-api"
LOG="$STATE/health.log"
BASE="${BLM_HEALTH_URL:-http://127.0.0.1:8081}"
UNIT="${BLM_HEALTH_UNIT:-browser-llm-api.service}"
mkdir -p "$STATE"

QUIET=0; DEEP=0
for a in "$@"; do
  case "$a" in --quiet) QUIET=1 ;; --deep) DEEP=1 ;; esac
done

log() {
  echo "$(date '+%F %T') [health] $1" >> "$LOG"
  [ "$QUIET" = 1 ] || echo "[health] $1"
}

# alert <kind> <cooldown-seconds> <severity> <title> <body>
alert() {
  local kind=$1 cooldown=$2 sev=$3 title=$4 body=$5
  local stamp="$STATE/alerted-$kind" now; now=$(date +%s)
  if [ -f "$stamp" ] && [ $(( now - $(cat "$stamp") )) -lt "$cooldown" ]; then
    log "alert '$kind' suppressed (inside its ${cooldown}s cooldown)"
    return 0
  fi
  echo "$now" > "$stamp"
  log "ALERT ($sev/$kind): $title"

  if [ -n "${BLM_ALERT_DIR:-}" ] && [ -d "$BLM_ALERT_DIR" ]; then
    printf 'severity: %s\ndepartment: agent-ops\ntitle: %s\n---\n%s\n' \
      "$sev" "$title" "$body" \
      > "$BLM_ALERT_DIR/$(date +%Y%m%d-%H%M)-browser-llm-$kind.md"
  fi
  if [ -n "${BLM_ALERT_CMD:-}" ]; then
    BLM_ALERT_KIND="$kind" BLM_ALERT_SEVERITY="$sev" \
    BLM_ALERT_TITLE="$title" BLM_ALERT_BODY="$body" \
      sh -c "$BLM_ALERT_CMD" >>"$LOG" 2>&1 || log "BLM_ALERT_CMD failed"
  fi
  if command -v notify-send >/dev/null 2>&1; then
    local urgency=normal
    [ "$sev" = critical ] && urgency=critical
    notify-send -a "browser-llm-api" -u "$urgency" "$title" "$body" >/dev/null 2>&1 || true
  fi
}

clear_alert() { rm -f "$STATE/alerted-$1"; }

fail=0

# --- 1. integrity ---------------------------------------------------------
if ./ensure-venv.sh check; then
  clear_alert venv
else
  log "venv missing or broken — rebuilding before it can take the service down"
  if ./ensure-venv.sh >>"$LOG" 2>&1; then
    alert venv 43200 notice "browser-llm venv was missing — rebuilt automatically" \
"venv/bin/python could not import the server's dependencies, so healthcheck.sh rebuilt it.

Nothing is down: the running process already holds its imports and keeps serving, which is exactly why this kind of damage goes unnoticed until the next restart. But something on this machine is deleting or breaking that directory, and it is worth finding.

Log: $LOG"
  else
    fail=1
    alert venv-broken 7200 critical "browser-llm venv is broken and will not rebuild" \
"venv/bin/python cannot import the server's dependencies, and the rebuild failed — most likely no network, or the python3-venv package is missing.

The API is still answering if the old process is alive, but it dies at the next restart. Run ./ensure-venv.sh in the repo to see the real error.

Log: $LOG"
  fi
fi

# --- 2. liveness ----------------------------------------------------------
status=$(curl -s -m 15 "$BASE/api/status" 2>/dev/null) || status=""
alive=$(printf '%s' "$status" | python3 -c \
  'import sys, json
try: print("yes" if json.load(sys.stdin).get("ok") else "no")
except Exception: print("no")' 2>/dev/null)

if [ "$alive" = yes ]; then
  clear_alert down
  log "up — /api/status ok"
else
  log "DOWN — /api/status did not answer ok; trying one restart"
  systemctl --user restart "$UNIT" >>"$LOG" 2>&1 || true
  sleep 20
  status=$(curl -s -m 15 "$BASE/api/status" 2>/dev/null) || status=""
  if printf '%s' "$status" | grep -q '"ok": *true'; then
    log "restart recovered it"
    clear_alert down
  else
    fail=1
    alert down 7200 critical "browser-llm API is down and a restart did not fix it" \
"$BASE/api/status is not answering, and restarting $UNIT did not bring it back.

Everything depending on this instance is failing right now: image generation, the MCP tools, the web dashboard, and any client on the network.

Check: journalctl --user -u $UNIT -n 50
Log: $LOG"
  fi
fi

# --- 3. auth (once a day) -------------------------------------------------
today=$(date +%F)
if [ "$fail" = 0 ] && { [ "$DEEP" = 1 ] || [ "$(cat "$STATE/deep-last" 2>/dev/null)" != "$today" ]; }; then
  for model in ${BLM_HEALTH_MODELS:-chatgpt-browser gemini-browser}; do
    body=$(curl -s -m 180 "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}],\"ephemeral\":true}" \
      2>/dev/null)
    reply=$(printf '%s' "$body" | python3 -c \
      'import sys, json
try: print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip()[:40])
except Exception: print("")' 2>/dev/null)
    if [ -n "$reply" ]; then
      log "deep ok — $model answered '$reply'"
      clear_alert "auth-$model"
    else
      # The server names the failure in "detail" (a hidden page, an input box it
      # could not find, a page that never started an answer). Log that rather
      # than guessing: "returned nothing" read as "expired login" for eight days
      # in September 2026 while the real cause was a hidden window.
      why=$(printf '%s' "$body" | python3 -c \
        'import sys, json
try: print(str(json.load(sys.stdin).get("detail") or "")[:400])
except Exception: print("")' 2>/dev/null)
      [ -z "$body" ] && why="no response within 180s"
      [ -z "$why" ] && why="empty answer"
      log "DEEP FAIL — $model: $why"
      alert "auth-$model" 21600 high "browser-llm: $model is returning empty answers" \
"A one-word test completion to $model failed: $why

The server itself is up. Causes seen so far, most recent first:
- The chat page is hidden (its Chrome window off-screen or covered on the agent display), so the
  site takes the prompt and never renders an answer. Check the page before anything else:
  curl -s http://127.0.0.1:<the provider's CDP port>/json/list, then read document.visibilityState.
- An expired login or a 'verify you are human' wall. Re-auth needs a real display:
    systemctl --user stop $UNIT
    DISPLAY=:1 ./venv/bin/python login.py ${model%%-*}
    systemctl --user start $UNIT

Log: $LOG"
    fi
  done
  echo "$today" > "$STATE/deep-last"
fi

exit "$fail"
