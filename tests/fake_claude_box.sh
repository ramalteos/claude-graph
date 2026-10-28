#!/usr/bin/env bash
# Stand-in runner: records argv to "$FAKE_ARGV.<sub>", prints the `headless` meta lines, then
# a stream-json tail ending in $FAKE_RESULT (a JSON string); sleeps $FAKE_SLEEP, exits $FAKE_EXIT.
set -u
sub="${1:-}"
{ printf 'ENGINE=%s\n' "${CLAUDE_BOX_ENGINE:-}"
  if read -r -t 1 _; then printf 'STDIN=data\n'; else printf 'STDIN=eof\n'; fi
  printf '%s\n' "$@"; printf '===\n'; } >> "${FAKE_ARGV:?}.$sub"
[ "$sub" = headless ] || exit 0
name="" wt="" branch="" remove=0 prev=""
for a in "$@"; do
  [ "$prev" = --name ] && name="$a"
  [ "$prev" = --worktree ] && wt="$a"
  [ "$prev" = --branch ] && branch="$a"
  [ "$a" = --remove ] && remove=1
  prev="$a"
done
[ "$remove" = 1 ] && exit 0
# the real runner cuts the branch at HEAD; a push-based test needs it to exist
[ -n "$wt" ] && [ -d "${2:-}" ] && git -C "$2" branch "${branch:-claude-box/$wt}" >/dev/null 2>&1
[ -n "$wt" ] && printf 'worktree=/tmp/wt/%s\nbranch=%s\nbase=abc123\n' "$wt" "${branch:-claude-box/$wt}"
printf 'box=claude-box-%s\n' "$name"
case "$name" in *-"${FAKE_FAIL_ROLE:-__none__}"-*) exit 1 ;; esac   # one role fails, the rest work
printf '{"type":"system","subtype":"init","session_id":"s1"}\n'
printf 'claude-box: a note on stderr\n' >&2
[ -n "${FAKE_SLEEP:-}" ] && sleep "$FAKE_SLEEP"
[ -n "${FAKE_RESULT:-}" ] && printf '{"type":"result","subtype":"success","is_error":false,"result":%s,"total_cost_usd":0.0123}\n' "$FAKE_RESULT"
exit "${FAKE_EXIT:-0}"
