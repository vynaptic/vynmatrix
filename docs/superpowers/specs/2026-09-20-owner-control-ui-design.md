# Owner control UI: switch a strategy on, and keep your own settings

Status: proposed (2026-09-20). Scope: the owner UI stops being read-only. It gains
the ability to bind a released strategy to an account and choose how that binding
trades, and to edit the owner profile and the non-secret configuration of a broker
account. It does not gain the ability to release a strategy, to hold a secret, or
to place an order.

## 1. Goal and non-goals

The three tabs answer questions and change nothing. Everything that turns a
strategy on lives in a terminal, so the page that shows "not bound to an account"
cannot do anything about it. The smallest coherent fix is to let the owner act on
exactly the two things they already see and already own.

| Scenario | Owner wants | Today | After |
| --- | --- | --- | --- |
| A | Start trading a released strategy on my paper account | `vmdev`/curl a 23-field JSON upsert | Pick the account, pick a mode, confirm |
| B | Stop a strategy without losing its history | `DELETE /bindings/{id}`, which is really a deactivation | Set it to **Off**; the row and its P&L stay |
| C | Let a strategy close out but take no new entries | Know that `exits_enabled` without `autopilot` is the legal combination | Pick **Close only** |
| D | Fix my timezone or display currency | `PATCH /owner` with a hand-built expected/changes document | Edit the field, confirm |
| E | Rename an account, or mark it revoked | `PATCH /broker-accounts/{id}` likewise | Edit the field, confirm |

Non-goals, each deliberate: releasing, activating, deprecating or pulling a
strategy version (section 4); any secret in the browser (section 8); creating or
re-designating the deployment owner; deleting anything; multi-user access;
editing a binding's instrument list; a sizing-profile picker (the table is not
readable by the backend role at all — `0052_service_role_rls.py:90`).

## 2. Evidence that shapes the design

| Fact | Source |
| --- | --- |
| `vm_backend` already holds SELECT + INSERT + UPDATE on `user_strategy_bindings`, plus USAGE on its sequence | `0052_service_role_rls.py:48-69`, `:448`; confirmed against the live catalog at head `0108` |
| `vm_backend` already holds column-level UPDATE on `users` (profile) and on `linked_broker_accounts` (`config_key, display_name, status, external_ref, base_ccy, paper_initial_equity, paper_initial_cash`) | `0102_owner_control_plane.py` |
| `vm_backend` holds `DELETE` on nothing at all | `0052_service_role_rls.py:68` (`frozenset()`) |
| `api_audit_logs` is writable by `vm_backend` (SELECT + INSERT) and its CHECK already permits `status='error'` | `risk_audit.py:104-126` |
| Three CHECKs make four independent switches unsafe: `is_active OR (NOT entries AND NOT exits)`; `NOT is_active OR strategy_id present`; `autopilot OR NOT entries` | `control_plane.py:265-276`, added by `0077_execution_safety.py:389-409` |
| `POST /bindings` is a whole-document upsert — it `setattr`s every `BindingIn` field, so a partial save resets the rest | `api.py:709`, `:754`, `:765-771` |
| `POST /bindings` has no expected/changes fence, unlike `PATCH /owner` and `PATCH /broker-accounts/{id}` | `api.py:709` vs `:1031`, `:1065` |
| **`POST /bindings` never checks release.** `_require_strategy_release` is defined at `api.py:440` and called only from `PUT /strategy-configs` at `:847` | `api.py:440`, `:847`, `:709-781` |
| Four pieces of shipped UI copy say the opposite and are therefore false today | `ui/pages/strategies.js:10`, `:25`, `:118`; `ui_queries.py:610` |
| An unreleased binding is nonetheless inert: scoring calls `require_active_strategy_version` before persisting a signal, so nothing is written and nothing trades | `_storage_ops.py:261`, `strategy_authority.py:29-43` |
| Binding write rules live entirely in the route body — nothing in `lib_application` references `UserStrategyBinding` outside the model | `api.py:709-781`; six grep hits, zero service code |
| `GET /owner`, `GET /broker-accounts`, `GET /bindings` already return everything a prefill needs; `BindingOut` is a superset of `BindingIn` | `api.py:1026`, `:1040`, `:703`, `:222-244` |
| The overlap conflict rule only fires when the incoming binding is being activated | `api.py:509-510` |
| `NULL` instruments falls through to `asset_classes_allowed`; both empty matches everything; a class with no catalogued instrument yields a sentinel that matches nothing | `_storage_base.py:498-515`, `tests/test_asset_class_filter.py:37-56` |
| `api.js` can only address `/api/ui/${path}` and supports only `get()` | `ui/api.js:46-47` |
| The 30-second refresh calls `replaceChildren` on all of `#main`; `readerIsBusy()` checks only focus and open `<details>` | `ui/app.js:128`, `:203-206`, `:253-256` |
| Any 401 clears the key and `showLogin()` empties `#main` | `ui/api.js:68-71`, `ui/app.js:83-88` |
| CSP forbids inline script, inline style and native form submission (`form-action 'none'`) | `ui_api.py:33-36`; asserted by `tests/test_ui_api.py:738-742` |
| There is no `<dialog>`, `showModal` or `confirm(` anywhere in `ui/`, and `app.css` styles no `select`, `label`, `fieldset` or checkbox | grep over `apps/backend/backend/ui/` |
| The launcher pins `BACKEND_ADMIN_API_KEY` and hard-sets `BACKEND_ALLOW_ANON=false` for the backend child | `scripts/platform_processes.py:292-309` |
| `guard_account_financial_terms` refuses an edit to `base_ccy`, `external_ref`, `paper_initial_equity` or `paper_initial_cash` once the account has execution activity | `0102_owner_control_plane.py:100-123` |
| `apply_owner_patch` refuses a `base_ccy` change once any account exists; `config_key` is not patchable at all | `owner_onboarding.py:370-379`; `account_onboarding.py:605-608` |
| The two expected/changes contracts disagree: owner is subset, account is exact equality | `owner_onboarding.py:352-355` vs `account_onboarding.py:614-621` |

Two conclusions follow, and they carry the design.

**No migration is required.** Every write in scope is already granted. The scope
was chosen to fit the grants, not the other way round — that is why deletion is
out and soft deactivation is in.

**The false copy must be fixed in this change.** Shipping a bind control beside a
sentence claiming binding is already refused would leave the page contradicting
its own button. The claim is repairable in either direction; section 4 picks one.

## 3. Approaches considered

1. **Controls in place, plus one Settings tab, backed by a binding service
   (chosen).** Strategy control appears on the page that already lists
   strategies. Profile and accounts get the tab they never had. The binding
   rules move out of the route body into `lib_application`, and both the existing
   `POST /bindings` and the new UI route call it. Profile and account writes reuse
   the existing routes untouched.
2. **A "Controls" tab that owns every write.** One place for everything
   changeable. Rejected: it splits strategies across two tabs, so the owner reads
   a strategy's state on one page and changes it on another, and the two lists
   drift. The Settings tab survives from this option because profile and accounts
   genuinely have no existing home.
3. **Mirror every write under `/api/ui/control/*`.** A uniform client with one
   base path. Rejected: it duplicates `PATCH /owner` and `PATCH /broker-accounts`,
   which are already tested and already have the right semantics, purely so the
   browser client can keep a hardcoded prefix. The client is the cheaper thing to
   change.

## 4. The release gate, and the copy that lies about it

`POST /bindings` accepts a binding for an unreleased strategy. The runtime then
refuses to act on it: no signal is ever persisted, so the binding is armed-looking
and silent. That is fail-closed in outcome and misleading in presentation.

This spec closes the gate rather than correcting the copy downward, for three
reasons: the UI already promises it; the runtime already behaves as if it were
true; and a binding that cannot ever fire is not a state worth letting an owner
create. `binding_control` calls `require_active_strategy_version` before any
insert or activation, and both `POST /bindings` and the new route inherit it.

This is a deliberate behaviour change to an existing route — stricter, never
looser. It is listed in section 12 and gets a `CHANGELOG` entry. The UI keeps
saying binding is refused for an unreleased strategy, and it becomes true.

Releasing a strategy stays exactly where it is: no backend-reachable path exists,
and none is added. The Strategies page shows an unreleased strategy with its
control disabled and the reason stated in the owner's language.

## 5. Information architecture

**A fourth tab is the right answer for settings and the wrong answer for strategy
control.**

- **Strategies** grows the control. A row expands to what it already shows, plus
  one mode selector and the account it applies to. The owner reads the state and
  changes it in the same place, which is the only way the two cannot disagree.
- **Settings** (new, fourth) owns the owner profile and the broker accounts —
  everything that is configuration rather than activity. Dashboard answers "is it
  running and how am I doing"; P&L answers "what did it earn"; neither is a home
  for a timezone field.
- **Dashboard** and **P&L** are unchanged.

## 6. Three modes, not four switches

The binding's four booleans are constrained by three CHECKs, so most of the
sixteen combinations are illegal. Four checkboxes would be a minefield where the
database rejects the owner's third click. One selector is offered instead, and
every option it can produce satisfies all three constraints by construction:

| Mode | `is_active` | `entries_enabled` | `exits_enabled` | `autopilot` | Owner-facing meaning |
| --- | --- | --- | --- | --- | --- |
| Off | false | false | false | false | Does not trade. History is kept. |
| Close only | true | false | true | false | May close what it holds. Opens nothing new. |
| Trading | true | true | true | true | Opens and closes positions on its own. |

Verification against the constraints: `is_active OR (NOT entries AND NOT exits)`
holds for Off by the second clause and for the other two by the first;
`NOT is_active OR strategy_id present` holds for Off by the first clause and
otherwise because the UI only ever binds a picked strategy;
`autopilot OR NOT entries` holds for Off and Close only by the second clause and
for Trading by the first.

The numeric parameters (`asset_score_threshold`, `max_position_pct`,
`max_total_exposure_pct`, `max_daily_loss_pct`, `max_open_positions`) are editable
in an **Advanced** disclosure, each with its range shown and validated client-side
against the CHECK it must satisfy. The asymmetry is real and is surfaced:
`max_position_pct` and `max_total_exposure_pct` reject zero, `max_daily_loss_pct`
and `asset_score_threshold` accept it. Thresholds are magnitude-based —
`abs(score) >= threshold` — and the help text says so, because a reader who
assumes a signed comparison will set a threshold that silently drops shorts.

`instruments_allowed`, `sectors_allowed`, `sizing_profile_id`,
`execution_modes_allowed` and `preferred_mode` are not editable here. The form
shows the stored instrument scope read-only. When it is empty, it says which
asset classes the binding will therefore match, and warns when that expansion is
empty — the one case where a blank scope matches nothing.

## 7. The write API

| Route | Method | Body | Backed by |
| --- | --- | --- | --- |
| `/api/ui/control` | GET | — | new read model: owner profile, accounts with credential status, full binding rows, strategies with release state, reference data |
| `/api/ui/bindings/{binding_id}` | POST | `{expected, changes}` | `binding_control.patch_binding` |
| `/api/ui/bindings` | POST | `{strategy_id, broker_account_id, mode}` | `binding_control.create_binding` |
| `/owner` | PATCH | `{expected, changes}` | existing route, unchanged |
| `/broker-accounts/{account_id}` | PATCH | `{expected, changes}` | existing route, unchanged |

Reads consolidate into one call so a page renders from one round trip; writes
reuse the existing routes rather than being mirrored. `GET /owner`,
`GET /broker-accounts` and `GET /bindings` stay as they are and keep serving the
CLI — `/api/ui/control` joins the same data with the names a page needs.
`api.js` gains a `send()` that takes an absolute path, so the client can address
the two PATCH routes; that is a smaller change than duplicating two tested routes
under a new prefix.

Binding writes are a **partial patch**, not the whole-document upsert
`POST /bindings` performs. Only the keys in `changes` are written. This is what
makes a mode change safe: it cannot reset a threshold the form never showed.

`GET /api/ui/control` widens the read surface in one deliberate way. The
2026-09-19 spec listed `config_key` and `external_ref` as never returned. Both
PATCH routes require the caller to supply the current value in `expected`, so the
owner cannot edit what they cannot see. They are identifiers the owner chose, not
secrets, and they are returned only on this route. Credential material remains
unreturnable and unwritable from the browser; the account card shows only
`status`, `expires_at` and whether an active credential exists.

## 8. Database authority

**No migration.** Every write is inside the existing grants:

| Write | Granted by | Note |
| --- | --- | --- |
| Insert a binding | `0052:48-69` (INSERT) + `:448` (sequence USAGE) | verified against the live catalog |
| Update a binding | `0052:67` (UPDATE) | table-level |
| Update the owner profile | `0102` column-level UPDATE on `users` | `status` and `user_id` not granted, so not editable |
| Update an account | `0102` column-level UPDATE on `linked_broker_accounts` | grant is a superset of `patch_account`'s six fields |
| Append an audit row | `0052` INSERT on `api_audit_logs` | |

Deletion is impossible by design: `vm_backend` holds `DELETE` on nothing. "Remove
this binding" is therefore not offered; **Off** is the honest control, and the
page says the history is kept rather than implying the row is gone.

Every write is additionally gated on `public.vm_deployment_owner_id()` returning
exactly one active owner. When it does not, the control surfaces are unreadable
and unwritable together, and the page says so rather than rendering empty forms.

## 9. Concurrency, confirmation and audit

**Concurrency.** Bindings adopt the account contract — exact equality between
`expected` and `changes` keys — because it is the stricter of the two and it fits
a form that shows what it is changing. A mismatch is a 409 and the UI re-reads,
shows what changed underneath, and asks again. It never retries silently.

**Confirmation.** Every state-changing action confirms first, in an overlay built
on the login overlay's existing markup pattern (the only modal precedent in the
codebase, and already CSP-clean). The confirmation states the current value, the
new value, and the consequence in the owner's language — "this strategy will
start opening positions on Swing canary local paper (EUR)". Nothing is confirmed
by a checkbox toggling under the pointer.

**Audit.** Every write appends an `api_audit_logs` row inside the same
transaction, which the existing `_append_control_audit` helper already does for
success. Two extensions are required and are new code, not reuse:

- **Refusals are audited.** Today `_append_control_audit` runs only on the success
  path and hard-codes `status="ok"`, so a rejected write leaves no trace. The
  helper gains a `status` argument and refusals write `status="error"` with the
  reason code. The CHECK already permits it (`risk_audit.py:126`).
- **One payload convention.** `req` carries metadata only —
  `{"fields": sorted(changed), "mode": "<mode>"}` — following `owner.patch`
  rather than the binding route's habit of storing the entire field dict. Values
  do not go in the audit row.

`account_id` is populated for binding and account writes. It is `NULL` today for
every binding audit row, which makes "which account did this touch" recoverable
only by parsing `req`.

A read-only **Activity** section on the Settings page renders the owner's recent
audit rows. `vm_backend` already holds SELECT on the table; only a read model is
new.

## 10. UI mechanics

Four shell-level problems must be solved before any form is safe.

**The refresh destroys forms.** `readerIsBusy()` checks focus and open
`<details>`; a form whose field is blurred — the normal state while a confirm
overlay has focus — is replaced mid-edit. The shell gains an explicit
`state.editing` flag. While an editor or confirmation is open, the timed refresh
does not run at all. This is a dirty-state concept, which the shell has none of
today.

**A 401 destroys the form too.** Any 401 clears the key and empties `#main`. A
write that 401s must not silently discard the owner's input: the pending change is
held in memory, the unlock prompt is shown, and on success the editor reopens with
the same values. The key is still never logged, never in a URL, and still cleared
by Lock.

**No form-control vocabulary exists.** `app.css` styles `button` and `input`
generically and nothing else — no `select`, `label`, `fieldset`, or checkbox.
`h()` sets attributes only, so it cannot express live control state (an input's
`value` after creation, a checkbox's `checked`, a select's selection). Both gaps
are filled: `format.js` gains `field()`, `choice()` and `numberField()` helpers
that set properties as well as attributes, and `app.css` gains the matching
rules. Every control is built with function-valued `on*` props and an explicit
`preventDefault`, because `form-action 'none'` forbids native submission.

**Errors must reach the owner as words.** The server returns `{"detail": str}`
only for the two registered onboarding handlers; pydantic 422 bodies and
re-raised database errors escape that shape, and `api.py` registers no
`IntegrityError` handler at all, so an unknown `strategy_id` becomes a 500. The
UI maps status codes to sentences (409 → "someone changed this while you were
editing"; 422 → the returned detail, or a generic "that value was not accepted";
503 → "no deployment owner is set up yet") and never shows a raw body. Error text
lives beside the control that produced it, not in the shared banner, which the
next successful render clears.

**Keyboard and assistive use.** Every control is reachable and operable by
keyboard; the confirmation overlay traps focus and returns it to the control that
opened it, exactly as the login overlay already does with `inert`; each field has
a real `<label>`; validation messages are associated with their field and
announced via `role="alert"`, as the login error already is.

## 11. Empty and first-run states

Each says what is missing and what would make it appear — the rule the read-only
pages already follow.

| State | Message |
| --- | --- |
| No broker account | "No account yet. `vmdev deploy` creates a local paper account on a fresh install; `vmdev user account` adds another." The bind control is hidden, not disabled-with-no-reason. |
| No released strategy | "Nothing is released for trading yet. Strategies ship with the installation and are released by maintenance, not from this page." |
| Released, not bound | The mode selector, defaulted to Off, with the account picker. |
| Bound, Off | "Not trading. Its history is kept." |
| Unreleased | Control disabled, with "Cannot be bound until it is released for trading" — a sentence that becomes true with section 4. |
| Account revoked | Bind control disabled; the account picker offers only `status='connected'` accounts, because anything else fails at the API. |
| No deployment owner | The whole control surface is replaced by the existing 503 message. |

## 12. Testing

- **Unit (SQLite).** Every mode maps to a flag combination that satisfies all
  three CHECKs — a table-driven test over all three modes asserting each
  predicate directly. Partial patch writes only the keys in `changes`. The
  expected/changes fence: subset rejected, mismatch 409, idempotent repeat
  accepted. Release refusal on create and on activation. Refusals write an
  `status="error"` audit row; successes write `status="ok"` with metadata only.
  Account picker excludes non-connected accounts.
- **Contract.** `/api/ui/control` returns no credential material and no secret
  field. The CSP assertion still passes with the new markup. The grants guard is
  extended to cover write statements, not just SELECT, so a write touching an
  ungranted column fails the build.
- **PostgreSQL integration.** RLS refuses a write when `app.current_tenant` is not
  the deployment owner. The audit row commits in the same transaction as the
  change and is absent when the change rolls back. The `guard_account_financial_terms`
  trigger surfaces as a 409, not a 500, when an account with activity is edited.
  The three CHECK constraints reject the illegal flag combinations the UI can
  never produce — proving the modes are the safe subset, not merely the chosen one.
- **Behaviour change.** A test asserting `POST /bindings` now refuses an
  unreleased strategy, and one asserting the E2E canary ordering (activate, then
  bind) still succeeds.
- **Acceptance.** `vmdev format --check`, `vmdev lint`, `vmdev audit --strict`,
  affected suites, `vmdev test all`, then each write path exercised against the
  live stack through the API — never by typing the admin key into a browser.

## 13. Risks

**Tightening `POST /bindings` changes an existing contract.** Any caller binding
an unreleased strategy starts getting a 409. That is the intent, and the runtime
already refused to act on such a binding, but it is a behaviour change to a route
the CLI and the canary use. The canary activates before it binds, so it should be
unaffected; the test in section 12 proves it rather than assuming it.

**`/docs`, `/openapi.json` and `/metrics` are unauthenticated on the same
published port.** Compose publishes 8081 directly with no proxy, so adding write
routes makes the public OpenAPI document describe them. The routes themselves stay
behind `X-Admin-Key`; this exposes their shape, not their use. Recommended, and
deferred to a decision: serve the schema only in non-production, or drop the
control router from it.

**Header-only auth has no CSRF token.** `X-Admin-Key` is not a cookie, so a
cross-site form cannot forge it and no CSRF class exists — but there is also no
`Origin` check, no CORS policy and no rate limit, and new write routes inherit
all three absences. Acceptable for a loopback-bound single-owner tool; it would
not be for a published one.

**Re-pointing a binding creates a second row.** The upsert keys on
`(user_id, strategy_id, broker_account_id)`, and nothing can delete. Moving a
strategy to a different account leaves the old binding behind, Off, forever. The
list will accumulate. Mitigated by grouping Off bindings under a collapsed "Not
trading" section rather than by pretending they can be removed.

**A stale stored instrument can 422 an unrelated activation.**
`_binding_scopes_overlap` re-resolves the *existing* row's instruments, so if a
stored symbol has since left the catalogue, activating a different binding fails
with a message naming an instrument the owner never typed. The UI cannot prevent
this; it surfaces the server's message verbatim and points at the binding that
holds the stale symbol.

**`Strategy.is_active` is nullable** and `bool(row.is_active)` renders NULL
identically to false. A three-state truth is being shown as two. Left as-is —
correcting it is a catalogue concern, not a UI one — but the release-state read
model distinguishes them so the UI can say "not released" rather than "disabled".

**Float round-trip on `Numeric(10,4)`.** `BindingOut` returns `float()` for five
decimal columns. The partial patch means a mode change no longer rewrites them at
all, which removes the exposure rather than managing it.

## 14. Documentation

`apps/backend/README.md` owns the new routes. Three pieces of copy stop being
true with this change and are corrected in it: the release claims in
`ui/pages/strategies.js` and `ui_queries.py` (section 4); the `ui_api.py` module
docstring, which says the surface "never writes"; and the shell's rail note,
"Read-only view of your own deployment. Nothing here can place or change an
order." Its second sentence stays exactly as it is and stays true — a binding
authorizes, it does not place an order — and only the words "Read-only view" go.
`SETUP.md` gains one line saying the dashboard can now switch a strategy on.
`CHANGELOG.md` gains an `Added` entry and a `Changed` entry for the release
tightening. No new top-level document.
