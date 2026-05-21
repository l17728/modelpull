# UI-SP5i — Participating-Executors SSE Stream + Seam Close-on-Disable (Design)

> 9th application of the view-free SSE template (after SP5/SP5b/SP5c/
> SP5d/SP5e/SP5f/SP5g/SP5h). The LAST SP2 sub-resource (Executors tab)
> graduates to SSE — AND, because this is the 4th tab stream (worst-case
> 5 concurrent SSE on TaskDetail), it bundles the seam **close-on-disable**
> change deferred since SP5g.
> Status: design self-approved per Rule #1.
> Branch: `feat/ui-sp5i-executors-sse`.

## 1. Context & Scope

`useParticipatingExecutors(taskId, enabled, terminal)` polls
`/api/v1/tasks/{task_id}/participating-executors` at 2 s (tab-gated).
This is the last of the 4 SP2 sub-resource composables; the other 3
(events SP5f, chunks SP5g, source-alloc SP5h) already stream.

**The connection-cap trigger has arrived.** Once the Executors tab also
streams, a user who visits all 4 TaskDetail tabs would hold **5
concurrent SSE connections** to the controller origin (the always-on
header stream `useTaskDetail` + 4 tab streams). Under HTTP/1.1's
6-per-origin cap that is fragile. SP5g/SP5h recorded the explicit
trigger: *"when the 4th tab stream is added, bundle the seam
close-on-disable change to bound concurrent streams to 2 (header +
active tab)."* SP5i delivers it.

**In scope (additive endpoint + 1 seam evolution, zero migration):**

1. **Backend**: `GET /api/v1/tasks/{task_id}/participating-executors/
   stream` — hand-rolled SSE mirroring `tasks_chunks_stream.py` (SP5g).
   Reuses `_td.executors_for_task(session, task_id, tenant_id)` (a
   `list[ParticipatingExecutor]`), wrapped as `ParticipatingExecutors(
   items=…)`. Pre-stream tenant gate. New file
   `src/dlw/api/tasks_executors_stream.py`. 2 s default tick (env
   `DLW_TASK_EXECUTORS_STREAM_INTERVAL_SECONDS`, clamped `[0.5, 60.0]`).
   Same `?max_ticks=N` hatch.

2. **Seam close-on-disable** (`useLiveResource.ts`): currently the SSE,
   once opened, stays open for the composable's lifetime (the SP5f
   `started` latch is permanent). SP5i replaces the permanent latch
   with an **open/close lifecycle keyed on `streaming.value`**: when
   `streaming` flips false (tab deactivated), abort the SSE; when it
   flips true again (reactivated) and data is present, reopen. This
   bounds the concurrent SSE count to (always-on streams) + (1 per
   active gated tab) = header + active tab = 2 on TaskDetail.

3. **Frontend**: `useParticipatingExecutors` opts in via the seam
   (reactive `streamUrl` over `taskId` + `applyEvent`). Signature/
   return-shape UNCHANGED.

**Out of scope (intentional):**

- `useSystemHealth` — permanently deferred (SP5e #35).
- After SP5i, ALL 4 SP2 sub-resources + the 5 always-on consumers
  stream. The SP5* SSE conversion is complete.

## 2. The Seam Close-on-Disable Change (centerpiece)

### 2.1 Current seam (post-SP5h)

```ts
if (opts.streamUrl && opts.applyEvent) {
  const ac = new AbortController()          // single, permanent
  let started = false                        // permanent latch
  let stopDataWatch, stopStreamingWatch
  const tryStart = () => {
    if (started) return
    if (!streaming.value) return
    if (q.data.value === undefined) return
    started = true
    stopDataWatch?.(); stopStreamingWatch?.()
    streamSse({ url, signal: ac.signal, … })
      .then(() => { pollingFallback.value = true; q.refetch() })  // giveup
      .catch(() => {})                                            // 401
  }
  stopDataWatch = watch(() => q.data.value, tryStart, { immediate: true })
  stopStreamingWatch = watch(streaming, tryStart)
  onScopeDispose(() => { ac.abort(); stopDataWatch?.(); stopStreamingWatch?.() })
}
```

Once `tryStart` succeeds, `started=true` forever; the SSE never closes
until scope dispose.

### 2.2 New seam (open/close lifecycle)

```ts
if (opts.streamUrl && opts.applyEvent) {
  const qc = useQueryClient()
  const auth = useAuthStore()
  const apply = opts.applyEvent
  let ac: AbortController | null = null   // non-null iff a stream is live
  let gaveUp = false                       // permanent polling fallback

  const openStream = () => {
    if (ac) return                         // already streaming
    if (gaveUp) return                     // gave up earlier → stay polling
    if (!streaming.value) return           // gated off
    if (q.data.value === undefined) return // wait for first useQuery success
    const controller = new AbortController()
    ac = controller
    const url = toValue(opts.streamUrl as MaybeRefOrGetter<string>)
    void streamSse({
      url, token: auth.accessToken, signal: controller.signal,
      onEvent: (ev) => {
        qc.setQueryData(key, apply(qc.getQueryData<T>(key), ev))
      },
      onUnauthorized: () => { auth.logout() },
    }).then(() => {
      // streamSse RESOLVES on BOTH abort and giveup (cf. sse.ts:94,97).
      // Identity guard: if `ac` no longer points at THIS controller, we
      // aborted it ourselves (closeStream) — do nothing. Otherwise it
      // gave up (3 consecutive failures) → fall back to polling.
      if (ac === controller) {
        ac = null
        gaveUp = true
        pollingFallback.value = true
        void q.refetch()
      }
    }).catch(() => {
      // 401 path (streamSse rejects). onUnauthorized already fired.
      if (ac === controller) ac = null
    })
  }

  const closeStream = () => {
    if (ac) { ac.abort(); ac = null }
  }

  watch(
    [streaming, () => q.data.value] as const,
    ([isOn, data]) => {
      if (isOn && data !== undefined) openStream()
      else if (!isOn) closeStream()
    },
    { immediate: true },
  )
  onScopeDispose(closeStream)
}
```

### 2.3 Why the identity guard is mandatory

`streamSse` **resolves** on abort (sse.ts:94 `if (opts.signal.aborted)
return`, and :99) AND on giveup (sse.ts:97). The promise outcome alone
cannot distinguish them. The `if (ac === controller)` guard is the
discriminator: `closeStream()` sets `ac = null` (or a later `openStream`
sets `ac = newController`) **before** the aborted stream's `.then`
microtask runs, so the guard is false → the giveup branch is skipped.
A real giveup leaves `ac === controller` → the branch runs. This also
handles the abort-then-immediate-reopen race (old `.then` sees the new
controller, skips).

### 2.4 Behavior preservation for the 8 existing consumers

- **5 always-on consumers** (SP5/SP5b/SP5c/SP5d/SP5e — no `enabled`
  gate): `streaming.value` is `true` from the first read and never
  flips false. The `{immediate:true}` watch fires with
  `data===undefined` → no-op; when useQuery lands data → `openStream`
  → SSE opens exactly as before. `closeStream` only ever runs on scope
  dispose. **Identical behavior.**
- **3 gated consumers already streaming** (SP5f events, SP5g chunks,
  SP5h source-alloc): previously their SSE stayed open after the first
  tab activation (permanent `started`). Now, deactivating the tab
  closes the SSE and reactivating reopens it. **This is the intended
  improvement** (bounded concurrency); the visible data is unchanged
  because vue-query keeps the last snapshot cached, shown instantly on
  return while the reopened stream refreshes it.

### 2.5 `gaveUp` stays permanent (consistent with prior behavior)

After 3 consecutive stream failures, `gaveUp=true` + `pollingFallback`
— the composable polls for the rest of its life, even across tab
switches. This matches the pre-SP5i semantics (the old
`pollingFallback` latch was also permanent). Resetting `gaveUp` on tab
reactivation would be a behavior change; deferred (not needed for
SP5i's goal).

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/tasks/{task_id}/participating-executors/stream
→ 200 text/event-stream
: open

data: { "items": [ ParticipatingExecutor, ... ] }
…
```

- Reuses `executors_for_task(session, task_id, tenant_id)` (list),
  wrapped `ParticipatingExecutors(items=…)`. Empty list valid (no
  executor-assigned subtasks).
- 2 s default tick; clamp `[0.5, 60.0]`. Pre-stream tenant gate. Fresh
  per-tick session. No auto-terminate on task terminal status.

### 3.2 RBAC / OpenAPI / Config

- RBAC: `require_perm("/api/v1/tasks*", "GET")` reused.
- OpenAPI: `/tasks/{taskId}/participating-executors/stream` (added with
  this spec commit).
- Config: `task_executors_stream_interval_seconds: float =
  Field(default=2.0)` (added with spec commit). Runtime clamp.

### 3.3 Tests (`tests/api/test_task_executors_stream.py`)

Mirror SP5g/SP5h (4 tests): unauth-401, cross-tenant-404,
single-snapshot (`items` is a list — empty is fine, no FileSubTask
seed needed), multi-snapshot.

## 4. Frontend Design

### 4.1 `useParticipatingExecutors` opt-in

Same shape as SP5h: add `streamUrl = computed(() => …/participating-
executors/stream)` + `applyEvent`. Return shape unchanged.

### 4.2 Tests

- `frontend/tests/unit/useParticipatingExecutorsStream.spec.ts` —
  mirror SP5h (3 tests: opt-in fields, reactive streamUrl, applyEvent).
- **Seam regression** — rewrite/extend
  `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts` to cover the
  new lifecycle: (a) enabled=false at mount → no streamSse; (b) flips
  true → streamSse called once; (c) enabled=true at mount → called
  once (always-on path); **(d) NEW: flips true→false → the open
  stream's AbortSignal fires (captured signal `.aborted === true`);
  (e) NEW: flips true→false→true → streamSse called twice (open, close,
  reopen).** Keep cases (a)-(c) so the existing-consumer paths stay
  proven.

### 4.3 View-free proof

- `TaskDetail.vue` + Executors-tab component (`SwimLane.vue` etc.) —
  NOT modified.
- Existing SP2 tests + the 8 SSE-consumer composable specs — NOT
  modified; must pass unchanged (the seam change is backward-compatible
  for always-on consumers and the 3 gated consumers' opt-in specs only
  assert the captured opts, not lifecycle).

### 4.4 Headed smoke

Fresh `:8011` + Vite + headed Playwright: navigate to TaskDetail,
click **Executors** tab (`#tab-executors`), observe
`/participating-executors/stream` SSE request. **Also verify
close-on-disable**: click Files tab (default) → Executors tab (opens
executors stream) → back to Files → assert the executors stream
request count did not grow on a 2nd Executors visit beyond expected
open/close cycles. (Pragmatic: count distinct stream OPEN requests; a
close+reopen yields a 2nd request — that's the proof close-on-disable
re-opens.)

## 5. Milestones

- **M1 Backend**: openapi + config + `tasks_executors_stream.py` + 4
  tests + router include + M1 gate.
- **M2 Seam + frontend cutover**: seam close-on-disable rewrite +
  extended seam regression test (5 cases) + `useParticipatingExecutors`
  opt-in + new composable spec + full frontend gate (all 8 consumer
  specs + both seam specs pass).
- **M3 Smoke + docs**: headed Playwright smoke (incl. close-on-disable
  observation) + append SP5i section to `docs/operator/web-ui.md`
  (note: SP5* SSE conversion complete; concurrency now bounded to 2).

## 6. Risks & Contingencies

- **Seam regression (highest risk)**: the open/close lifecycle replaces
  the permanent `started` latch. Mitigation: the identity-guard design
  (§2.3), backward-compat analysis (§2.4), and the 5-case seam
  regression test (§4.2) covering both always-on and gated paths. All
  8 existing consumer specs must pass unchanged — the regression-proof.
- **Abort/giveup race**: handled by `if (ac === controller)` (§2.3).
- **httpx ASGITransport buffering** — `?max_ticks=N`.
- **Route collision** — N/A (distinct depth).
- **Empty executors list** — valid (no executor-assigned subtasks);
  test seeds none.
- **gaveUp permanence across tab switch** — intentional, documented
  (§2.5).

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: backend mirrors SP5g/SP5h exactly. The seam change
  is the one genuinely new, higher-risk piece — fully specified in §2
  with the abort/giveup discriminator and behavior-preservation
  analysis.
- **Scope**: 1 endpoint + 1 composable opt-in + 1 seam evolution + 4
  backend tests + 1 new composable spec + extended seam spec. Larger
  than SP5g/SP5h (the seam change), but the seam change was a recorded,
  triggered obligation — not scope creep.
- **Ambiguity**: endpoint path, tick/clamp, RBAC reuse, response shape
  (wrap list in `ParticipatingExecutors`), seam open/close lifecycle,
  identity guard, gaveUp permanence, test set — all pinned.
- **Completion note**: SP5i finishes the SP5* SSE conversion (all
  TaskDetail tabs + global consumers stream; concurrency bounded to 2).
