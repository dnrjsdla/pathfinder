# Senses: hooking the world up

Each snippet below only ever calls `pathfinder signal <sense> '<json>'`, which connects, sends
one line, and exits without waiting - a hook must never block the thing it is hooked to. When
the daemon is not running, or the JSON is bad, the CLI is silent (exit 0): a stopped daemon must
not turn every command into a shell error.

## shell (this partition)

Add to `~/.zshrc` (requires the sense to be turned on: `pathfinder config set senses
'{"shell": {}}'`):

```zsh
_pathfinder_cmd_start=0

preexec() {
  _pathfinder_cmd_start=$EPOCHSECONDS
  _pathfinder_cmd=$1
}

precmd() {
  local exit=$?
  if [[ -n $_pathfinder_cmd ]]; then
    local seconds=$(( EPOCHSECONDS - _pathfinder_cmd_start ))
    local fields=$(printf '{"cmd": %s, "exit": %d, "seconds": %d, "cwd": %s}' \
      "$(printf '%s' "$_pathfinder_cmd" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
      "$exit" "$seconds" \
      "$(printf '%s' "$PWD" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")
    ( pathfinder signal shell "$fields" & ) >/dev/null 2>&1
    _pathfinder_cmd=""
  fi
}
```

`( ... & )` backgrounds the call and disowns it from the shell's job table; `>/dev/null 2>&1`
swallows anything the CLI might print. Neither the command's exit code nor its timing is
affected - `precmd` already ran after the command finished either way.

## Claude Code hooks (later - the senses do not exist yet)

Partition 3's next step adds a `claude_code` sense reading `Stop` and `Notification` hook
payloads from Claude Code's `settings.json` (`pathfinder signal claude_code '{"event": ...,
"cwd": ..., "message": ...}'`), the same fire-and-forget shape as above. Left here as a
placeholder heading so this file does not need restructuring when it lands.
