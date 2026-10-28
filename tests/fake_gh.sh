#!/usr/bin/env bash
# Stand-in `gh`: records argv to $FAKE_GH_STATE/argv.log, keeps one fake PR per branch in
# $FAKE_GH_STATE/pr-<branch>.json, answers `pr checks --json` from $FAKE_GH_CHECKS (or []).
set -u
S="${FAKE_GH_STATE:?FAKE_GH_STATE must be set}"; mkdir -p "$S"
printf '%s\n' "$*" >> "$S/argv.log"
val() { local prev="" a; for a in "$@"; do [ "$prev" = "$1" ] && { printf '%s' "$a"; return; }; prev="$a"; done; }
case "$1 $2" in
  "pr list")   b="$(val --head "$@")"; [ -f "$S/pr-$b.json" ] && cat "$S/pr-$b.json" || echo '[]' ;;
  "pr create") b="$(val --head "$@")"; n="${FAKE_GH_PR_NUMBER:-7}"; u="https://github.com/o/r/pull/$n"
               printf '[{"url":"%s","number":%s}]' "$u" "$n" > "$S/pr-$b.json"; echo "$u" ;;
  "pr checks") if [ "$3" = "--watch" ] || [ "${4:-}" = "--watch" ]; then
                 [ -n "${FAKE_GH_SLEEP:-}" ] && sleep "$FAKE_GH_SLEEP"; exit "${FAKE_GH_WATCH_EXIT:-0}"
               fi
               [ -n "${FAKE_GH_CHECKS:-}" ] && [ -f "$FAKE_GH_CHECKS" ] && cat "$FAKE_GH_CHECKS" || echo '[]' ;;
  "pr ready")  exit 0 ;;
  "label list") printf '%s' "${FAKE_GH_LABELS:-[]}" ;;
  "api --paginate") case "$3" in
                      */reviews)  printf '%s' "${FAKE_GH_REVIEWS:-[]}" ;;
                      */comments) printf '%s' "${FAKE_GH_COMMENTS:-[]}" ;;
                    esac ;;
  "label create") exit 0 ;;
  "pr edit")   exit 0 ;;
  *)           exit 0 ;;
esac
