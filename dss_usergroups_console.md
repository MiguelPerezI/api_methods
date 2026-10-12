# dss_usergroups_console.py — design & history

Interactive console to edit a DSS user's **group memberships** and **DSS
profile** (`userProfile`) via the public REST API. Every endpoint lives
under `/admin`, so the API key must have **Admin** privileges — this is
the first script in the collection that only makes sense with an admin
key (a 403 gets its own hint message at the banner).

Same connection/auth machinery as every other script (URL chain `--url` >
env `DSS_URL` > hand-edited `CONNECTION DEFAULTS` > prompt; key from env
`DKU_API_KEY` / `--api-key-file` / hidden prompt), and the
edit-in-memory-then-save pattern of
[dss_codeenv_console.md](dss_codeenv_console.md): **nothing is written
until "save changes"** ("the install actions always run on the *saved*
server definition — unsaved edits never reach DSS" applies here the same
way: group/profile picks are staged and reviewable, and `discard` drops
them).

## Usage

```bash
# interactive: list users -> pick one -> stage edits -> save:
DKU_API_KEY=... python3 dss_usergroups_console.py

# straight to one user's edit screen:
DKU_API_KEY=... python3 dss_usergroups_console.py --login jdoe
```

On the user screen:

```
 1) add groups...        multi-pick from the instance's groups the user
                         is NOT in ('1 3-4', '1,3'; empty = cancel);
                         admin groups are marked and staging one prints a
                         warning
 2) remove groups...     multi-pick from the user's effective groups
 3) change DSS profile   known three + every profile seen on the
                         instance's users, or 'm' to type one
 4) save changes         (only when something is staged)
 5) discard ...          drops all staged edits
 b) back to the user picker; q quits anywhere
```

## API facts used

| Endpoint | Method | Privileges | Purpose |
|---|---|---|---|
| `/admin/users/` | GET | Admin | List users (picker; also validates the key) |
| `/admin/users/{login}` | GET | Admin | Fresh user object before every save |
| `/admin/users/{login}` | PUT | Admin | Save groups/profile — body **MUST be a GET-round-trip** |
| `/admin/groups` | GET | Admin | The pool the add-picker offers |

- The User object carries `login`, `sourceType`, `displayName`, `email`,
  `enabled`, `groups` (array of group names), `userProfile`,
  `codeAllowed`, plus undocumented attributes ("you should not modify
  them") — the save PUT sends back exactly what the fresh GET returned,
  with only `groups` (and, if staged, `userProfile`) replaced.
- Documented success for the PUT is `200 {"msg": "Edited user X"}` (the
  mock mirrors that; it also rejects unknown group names with 400, the
  real-DSS behavior).
- The docs don't enumerate `userProfile` values (the create-user example
  shows `"DATA_ANALYST"`), so the profile picker lists the common three
  + every profile observed on the instance + a manual entry; a bogus
  manual value is rejected by DSS at save time — caught, printed, staged
  edits kept.
- Non-LOCAL (LDAP/SAML) users: the API accepts the PUT but a directory
  resync may revert it — `sourceType` is shown in the picker and on the
  user screen so it is never a surprise.

## Design decisions

1. **Staged edits, one save.** Adds/removes/profile are picked freely and
   shown inline (`+ add staged`, `- remove staged`, `staged -> READER`),
   then applied in a **single** GET-round-trip PUT. Removing a group whose
   add was staged just unstages the add (and vice versa) — no needless
   writes.
2. **Save failures stay in the console.** A failed PUT (e.g. DSS
   unreachable mid-session, mock killed in tests) prints
   `save FAILED (nothing applied; staged edits kept)` and returns to the
   menu — interactive tools shouldn't die because one call did.
3. **Admin groups are visible, warned, not blocked.** They carry `** admin
   group **` markers in both the pool and the staged-add lines, and
   staging one prints the what-this-grants note.
4. **Profile discovery over hard-coding.** `KNOWN_PROFILES` is a fallback
   seed; profiles actually in use on the instance extend the picker, and
   `m` types anything (validated as a sane identifier client-side; DSS
   still owns real validation).
5. **Verify after write.** Save re-GETs the user and compares groups and
   profile; a mismatch prints a WARNING with both states instead of
   silently trusting the 200.
6. **Conventions as the rest of the repo**: stdlib only, `.format()` over
   f-strings, no `--api-key` flag, exit codes 0 ok / 1 API failure /
   3 not-found / 2 usage, `q`/EOF/Ctrl+C leave cleanly (no destructive
   gates — nothing here is irreversible — so no `--yes`).

## Verification

Verified 2026-10-09 against `tests/mock_dss_server.py` (mock additions:
`GET /admin/users/`, `GET/PUT /admin/users/{login}`, `GET /admin/groups`,
a five-user seeded world — incl. a disabled service account and an LDAP
user without a `userProfile` key — and a five-group pool incl. an admin
group and an LDAP-sourced one; PUT merges the body over the record and
400s unknown group names).

| Scenario | Result |
|---|---|
| jane.doe: add `administrators`+`data_scientists`, remove `marketing`, profile → READER, save | admin-group warning printed; staged markers correct (`+`/`[-]`/`staged ->`); one PUT; `saved: Edited user jane.doe`; re-read verified; curl shows exactly `["analysts","administrators","data_scientists"]` + READER; picker refreshes (3 grp) |
| bob.smith: empty input at the multi-pick | cancels, back to the menu, nothing staged |
| bob.smith: stage range `2-4` (3 groups), then discard | discard drops all three; mock state unchanged afterwards |
| Mock killed between staging and save | `save FAILED (nothing applied; staged edits kept): cannot reach DSS ...`, menu keeps the edits, quit exit 0 |
| Manual profile `CUSTOM_OPS` via `m` | staged, saved, curl-verified `userProfile: CUSTOM_OPS` |
| Multi-pick ranges/lists | `1 3-4` and `1,3` parse identically; bad tokens (`x`, `9-1`) re-ask |
| `--login bob.smith` | skips the picker, straight to the user screen |
| `--login nosuch` | error + visible logins on stderr, exit 3 |
| Wrong key (401) | "error: HTTP 401 ...", "(the API key was rejected - check DKU_API_KEY)", exit 1 |
| Listing markers | `[disabled]` for svc.etl, `[LDAP]` + profile `?` for lea.extern |
| Guard rails | user with no groups → "(no groups to remove)"; user in all groups → "(already in every group...)"; pool unavailable → its own note |
| Regressions on the updated mock | scenariorun `--list`, codeenv console, bashrc tool (create+payload+run), copy console: all exit 0 |

## History

### v1 — 2026-10-09

- Built for: "modify a user's groups, given a hostname and api key;
  interactive like the other scripts; let the user choose which groups
  to remove or add to a specific user, and DSS profile."
- Driver + `--login`; save = GET-round-trip PUT (the docs' "MUST have
  been previously obtained from a GET user call"), re-read verify.
- Mock grew the users/groups security subset (first `/admin` non-code-env
  routes); nothing existing was changed — regressions pass.
- Found while verifying: an empty staged-save on a dead mock must not
  kill the console — the save call is wrapped so the session survives
  with staged edits intact.
