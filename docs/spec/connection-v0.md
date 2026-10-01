# Connection spec v0

One JSON object per MCP-server candidate: the shape every step of the tool path (`catalog`,
`installer`, the approval box, `state`) reads and writes, and the shape a future teammate's
verification server would receive (D14). v1 has no such server - `catalog` reads the official
MCP registry directly, `installer` runs every check locally, and the `outcome` field is recorded
in `state.db` and never sent anywhere. Fields marked "the installer" below name
`pathfinder/installer.py`'s contract (partition 6's `check()`/`install()`); if that file has not
landed yet, the field is still `not_checked` in every candidate.

Every check the code did not run is the literal string `"not_checked"`. The field list is
`pathfinder.catalog.SPEC_FIELDS` verbatim, in order; a candidate has no key outside this list.

## name

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `name` | string | `catalog.candidate` (the registry entry's own name) | never - the registry always names an entry |

## version

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `version` | string | `catalog.candidate` | never - empty string `""` if the registry omits it, not `"not_checked"` |

## source_registry

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `url` | string | `catalog.candidate` (`catalog.REGISTRY`) | never |
| `entry_id` | string | `catalog.candidate` (the registry entry's `name`) | never |
| `updated_at` | string (RFC3339) | `catalog.candidate` (registry metadata) | the registry gives neither `updatedAt` nor `publishedAt` |

## description

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `description` | string | `catalog.candidate` | never - empty string if the registry omits it |

## package

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `registry_type` | `npm\|pypi\|remote` | `catalog.candidate` | never |
| `identifier` | string | `catalog.candidate` (package identifier, or the first remote's URL) | never |
| `version` | string | `catalog.candidate` | never |
| `runtime` | `npx\|uvx\|not_checked` | `catalog.candidate` | the entry has no npm/pypi package, only a remote |
| `args` | array | `catalog.candidate` | never (empty array, not `"not_checked"`, when there are none). The runtime's own flags, then the pinned package spec `<identifier>@<version>`, then the package's arguments - `[runtime, *args]` is the exact command the trial and the installed server run |
| `env[].name/secret/required` | string/bool/bool | `catalog.candidate` | never |
| `remotes[]` | array | `catalog.candidate` | never (empty array when there are none) |

## verification

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `license.spdx` | string | the installer (`check()`), stub from `catalog.candidate` | before `check()` runs |
| `license.source` | `npm\|pypi\|unknown` | the installer, stub from `catalog.candidate` | before `check()` runs |
| `license.verdict` | `ok\|excluded\|unknown` | the installer, stub from `catalog.candidate` | its own no-info value is `"unknown"`, not the literal string |
| `malicious_scan` | string | nobody | always - v1 never scans; this is the whole field's permanent value |
| `sandbox_trial` | `"not_checked"` or object | the installer (`check()`'s trial step) | before `check()` reaches the trial |
| `sandbox_trial.ran_at/started` | string/bool | the installer | (only present once the trial ran) |
| `sandbox_trial.tools[].name/description_hash/schema_hash/read_only_hint` | string x4 | the installer | (only present once the trial ran) |
| `sandbox_trial.file_writes_outside_tmp` | bool | the installer | (only present once the trial ran) |
| `sandbox_trial.network` | string | the installer | always - the Seatbelt profile allows network; observing it is later work |
| `sandbox_trial.tools[].params` | array of argument names | the installer (the pins' evidence) | (only present once the trial ran) |
| `sandbox_trial.skipped[]` | array of tool names | the installer (a tool whose qualified name does not fit the provider naming rule is listed here, never granted) | (only present once the trial ran) |
| `sandbox_trial.sandboxed` | bool | the installer (`false` when this machine has no `sandbox-exec`) | (only present once the trial ran) |
| `sandbox_trial.verdict` | `ok` | the installer | (only present once the trial ran; a trial that fails leaves `sandbox_trial` at `"not_checked"` and sets `failed_step`/`reason` below) |
| `failed_step` | `installed\|runtime\|version\|license\|trial` | the installer (`check()`, on the first hard failure) | absent on success |
| `reason` | string | the installer, with `failed_step` | absent on success |
| `maintenance.last_publish_at` | string | `catalog.candidate` (registry metadata) | the registry gives neither `publishedAt` nor `updatedAt` |
| `maintenance.namespace_verified` | bool | `catalog.candidate` | never - `true` for every active registry entry |
| `description_hash` | string | the installer (over the trial's tool list, after a successful trial) | before a successful trial |
| `trust_tier` | `unverified\|checked` | `catalog.candidate` (`"unverified"`), the installer (`"checked"`) | its own no-info value is `"unverified"`, not the literal string; v1 never produces anything but `"checked"` on success |

## install

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `install` | `"not_checked"` or object | the installer (`check()` opens it with `id` and `needs`; the rest lands after a successful trial) | before `check()` reaches the secrets step |
| `id` | string | the installer (`check()`: the server id, decided once - the registry name's last segment sanitized to the config's id rule) | (only present once checked) |
| `needs[]` | array of env names | the installer (`check()`: secrets not yet in the Keychain, required values not set - the row prints them) | (only present once checked) |
| `runtime/pinned_version/integrity` | string x3 | the installer (after a successful trial) | (only present once the trial ran) |
| `env_from_keychain[]` | array of names | the installer (every `secret` env the package declares, found or not - the config references each as `keychain:<NAME>` when required or `keychain?:<NAME>` when optional, with `=<default>` appended when the registry gives one; `mcp.servers._resolve` reads it for the check and the start alike, U34) | (only present once the trial ran) |
| `args[]` | array | the installer (`[runtime, *args]` is the installed command) | (only present once the trial ran) |
| `permissions[]` | array of `{tool, grant}` | the installer (D31: `allow` for every read-only-hinted tool the trial saw; every other tool asks) | (only present once installed) |

## billing

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `billing` | `unknown\|free\|paid` | `catalog.candidate` | never - its own no-info value is `"unknown"`, not the literal string |

## outcome

| field | type | who fills it | not_checked when |
|---|---|---|---|
| `outcome` | `proposed\|approved\|rejected\|installed\|install_failed\|uninstalled` | `catalog.candidate` (initial `"proposed"`), then the daemon's approval box and the installer | never - recorded locally; v1 sends this to nobody |

## What v1 never does

- Send `outcome`, or any other field, to a verification server (D14: there is none yet).
- OAuth for a remote MCP server - a `remotes[]` entry needing a bearer token installs with the
  token read from the Keychain, nothing more.
- Install an mcpb/dxt bundle - only `npm` and `pypi` packages, plus bare remotes.
- Update a server automatically - a newer version is a new candidate, proposed like any other.
- Scan for malicious code. `verification.malicious_scan` is always `"not_checked"` in v1.

## Fixtures

`docs/spec/fixtures/`, all synthetic, `example` namespaces only, all covered by
`scripts/scan_personal.py`:

- `proposed.json` - a checked candidate exactly as `pathfinder approvals` would hold it: license
  and sandbox trial both filled and `ok`, three tools of which two are read-only, `outcome:
  "proposed"`, `install` still `"not_checked"` (nothing has been installed yet).
- `installed.json` - the same candidate after `install()`: `outcome: "installed"`,
  `install.permissions` listing the two `allow`s the trial's read-only tools earned (D31).
- `install_failed.json` - a different candidate whose sandbox trial timed out:
  `verification.failed_step: "trial"` with a `reason` (`sandbox_trial` stays `"not_checked"`), `outcome: "install_failed"`,
  and nothing the check computes after the trial (`description_hash`, `trust_tier`, `install`)
  filled in.
