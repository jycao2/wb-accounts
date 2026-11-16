# wb-accounts · Multi-account manager for WorkBuddy Desktop

> Manage several WorkBuddy desktop accounts on one machine — **register once, skip the QR code forever**.
> Ships with a local dashboard, one-click check-in, and one-click account switching —
> **your tasks come with you, no starting over**.

<p align="center">
  <em>Windows 10/11 · Python 3.10+ · standard library first · binds 127.0.0.1 only</em>
</p>

---

## The problem

WorkBuddy Desktop can only hold one signed-in account at a time. Switching means logging out,
then scanning a QR code / waiting for an SMS code again. If you juggle a main account plus
test accounts, that gets old fast.

`wb-accounts` keeps a **login-state snapshot** and a **token channel** for each account:

- switch with one click: client quits → snapshot written → verified → **relaunched automatically**;
- query status and claim daily credits **without switching** (dedicated token channel);
- your password is never touched; credentials are sealed with Windows DPAPI per user.

## Features

| Capability | Detail |
|---|---|
| **Account registry** | Capture the current client login state as a snapshot (no password) |
| **Token channel** | Auto-renew via `refresh token`, long-lived, DPAPI-sealed |
| **Local dashboard** | Plain HTML on `127.0.0.1:8765` — balances, check-in, switch, renew |
| **Daily check-in** | One click (needs the `workbuddy-daily-claim` skill); idempotent, batchable |
| **One-click switch** | Quit client → write state → verify → relaunch; the current account's tasks can come along |
| **Doctor** | `doctor` tells you exactly what is not configured |

## Tasks come along when you switch

The painful part of switching is not signing in again — it is that **all your previous work stays
behind in the old account**. Tick "copy task data" in the switch dialog (**off by default**, nothing
happens unless you tick it) and the current account's tasks are duplicated under the target account:
the sidebar is populated, tasks open, and you can keep chatting.

- Session entries, full transcripts (including tool calls), containing groups and session
  workspaces are copied;
- **Copy, not move**: the source account is left byte-for-byte untouched, both sides keep a version;
- Pick only what you need: the dialog lists every live task of the current account (all selected by
  default) with search and per-item toggles;
- The whole `workbuddy.db` is backed up to `wb-accounts/backups/` first; each session commits
  individually and a failure is recorded without aborting the switch.

Three boundaries worth knowing up front:

| Not copied | Why |
|---|---|
| Automations | Bound to the runtime and account; a copied job cannot run, rebuild it on the new account |
| In-flight generations | Switching requires fully quitting the client, so running tasks are interrupted |
| Memory | User-level `MEMORY.md` and the cloud profile are stored per account and never copied; only the project-local `.workbuddy/memory/` folder is shared, since it belongs to the directory, not the account |

One more gotcha: the sync is a **snapshot**. `sync-ledger.json` stores the "source session → new
session" mapping, so re-running for the same session is skipped — later messages added in the old
account are **not** topped up. Delete the matching ledger entries to sync that session again.

## Quick start

Get the code first, either way:

```bat
git clone https://github.com/jycao2/wb-accounts.git
cd wb-accounts
```

Or open the repository page → **Code → Download ZIP** and unzip. Every command below runs inside
that directory.

**One-click install (recommended).** Double-click `install.cmd` in the repository root.
It installs `wb-accounts` plus the bundled `vendor/workbuddy-daily-claim` skill into
`%USERPROFILE%\.workbuddy\skills\`, keeps any existing `state\`, locates Python and runs
`doctor`. Running it again acts as an upgrade. Use
`install.ps1 -SkillsRoot "D:\my\skills"` to install elsewhere.

Manual equivalent:

1. Place this repository at `%USERPROFILE%\.workbuddy\skills\wb-accounts\`
   (any location works; set `WB_ACCOUNTS_HOME` to change the data directory).
   Copy `vendor\workbuddy-daily-claim\` to `...\.workbuddy\skills\workbuddy-daily-claim\`.
2. Run `python scripts\wb_accounts.py doctor`.
3. Sign in to the account you want to manage, then:
   `python scripts\wb_accounts.py capture --label main`.
4. Double-click `bin\open-dashboard.vbs` — it locates Python, starts the service and opens
   <http://127.0.0.1:8765>. A local token is generated on first run.

## Common commands

```bat
python scripts\wb_accounts.py current            :: who is logged in
python scripts\wb_accounts.py list               :: all registered accounts
python scripts\wb_accounts.py --json brief       :: one-screen summary (machine readable)
python scripts\wb_accounts.py keepalive --all    :: renew every token
python scripts\wb_accounts.py switch <id>        :: switch client account (quit client first)
python scripts\wb_accounts.py rollback           :: undo last switch
python scripts\wb_accounts.py run-as <id> status :: query as that account
python scripts\wb_accounts.py dashboard          :: run the dashboard in foreground
```

## Requirements

| Item | Requirement | Notes |
|---|---|---|
| OS | Windows 10 / 11 | DPAPI + desktop client are Windows-only |
| Python | 3.10+ | WorkBuddy's bundled runtime works too |
| WorkBuddy Desktop | installed, signed in once | required for capturing state |
| Node.js | 18+ (optional) | only for the web-auth window |
| workbuddy-daily-claim | optional | only for check-in / credits |

## Security

- Dashboard binds `127.0.0.1` only, validates `Host`, requires a token / HttpOnly cookie,
  CSP blocks outbound requests;
- No plaintext tokens in the UI or logs (UID/UIN masked, JWT and secret fields redacted);
- Snapshots and tokens are sealed with Windows DPAPI in the **current user** scope;
- `recover_atrest.py` reads only the **local, running client's own memory**, encrypts
  immediately, never phones home;
- Nothing is injected into the client and no server-side check is bypassed.

See [docs/security.md](docs/security.md) for the threat model.

## FAQ

**Dashboard unreachable?** Double-click `bin\open-dashboard.vbs`; if it still fails, run
`python scripts\wb_accounts.py dashboard` in a console to see the error.

**Token channel not configured?** Run `doctor`. Usually `atrest.bin` is not sealed yet —
the client must be running and signed in when the panel triggers auto-configuration.

**Can I copy `~/.workbuddy/wb-accounts` from another machine?** No — DPAPI is bound to the
Windows user; each account must be registered on the target machine.

## Disclaimer

This project reads the **local** client login state to enable password-free switching and
querying, for personal productivity. Follow WorkBuddy's terms of service; do not use it to
share accounts or bypass risk controls. **Never share your `~/.workbuddy/wb-accounts` folder.**

## License

[MIT](LICENSE)
