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

## Claude Code hooks

Turn the sense on with `pathfinder config set senses '{"claude_code": {}}'` (merge it into
whatever `senses` already holds), then add this block to Claude Code's `settings.json`. Each
hook gets the event's JSON on stdin; the one-liner below reads `cwd`/`message` out of it and
backgrounds the CLI call the same way the shell hook does, so a slow or stopped daemon never
holds the hook up:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 -c \"import json,sys,subprocess; d=json.load(sys.stdin); subprocess.Popen(['pathfinder','signal','claude_code',json.dumps({'event':'Stop','cwd':d.get('cwd',''),'message':d.get('message','')})])\""
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 -c \"import json,sys,subprocess; d=json.load(sys.stdin); subprocess.Popen(['pathfinder','signal','claude_code',json.dumps({'event':'Notification','cwd':d.get('cwd',''),'message':d.get('message','')})])\""
          }
        ]
      }
    ]
  }
}
```

`Stop` becomes `task_done`, `Notification` becomes `needs_input`; both speak only when
`presence` says the user is away from PATHFINDER itself (RULES.md's `away` condition) - otherwise
they are silent, since the user is presumably already looking at Claude Code's own output.

## calendar, reminders, mail

Poll senses, no hook needed - just turn them on:

```bash
pathfinder config set senses '{"calendar": {}, "reminders": {}}'   # needs pyobjc-framework-EventKit
pathfinder config set senses '{"mail": {"host": "imap.example.com", "user": "you@example.com"}}'
```

`calendar`/`reminders` need `pyobjc-framework-EventKit` installed and Calendar/Reminders access
granted to the terminal running the daemon (System Settings > Privacy & Security); without it
they log one `error` event and stay quiet from then on (`pathfinder/senses/_eventkit.py`).
`mail`'s password is never in config - store it once with:

```bash
security add-generic-password -s pathfinder-mail -a you@example.com -w
```

(`-s` must match `keychain_service`, default `pathfinder-mail`; `-a` must match `user`.)
