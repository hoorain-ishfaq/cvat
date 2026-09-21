# Class Statistics — real-time analytics for CVAT

A task-scoped analytics feature added to CVAT: a REST endpoint that reports how many
images contain each class, a page that charts those numbers, and a WebSocket channel
that refreshes the page the moment annotations change.

Branch `dev-test01`, built in four commits:

| Commit | Scope |
|---|---|
| `5f58fcd87` | Django app `test` — registration, OPA rule, stub endpoint |
| `49ad12947` | `GET /api/test/class-counts` aggregation |
| `f31bc5ea6` | Class Statistics page and chart |
| `4bfb2fded` | WebSocket live updates |

---

## 1. Understanding of CVAT architecture

The parts of CVAT this feature actually touches.

### Process layout

Requests arrive at **traefik** (port 8080), which routes `/api/`, `/static/`, `/admin`
and `/django-rq` to the `cvat_server` container and everything else to `cvat_ui`.
Inside `cvat_server`, **nginx** proxies to **uvicorn**, which runs the Django ASGI
application from `cvat/asgi.py`.

Two facts drove the design:

- uvicorn runs with **`NUMPROCS: 2`** (`docker-compose.yml`), so the server is **two
  independent processes**. Anything shared between connections has to live outside the
  process.
- nginx already forwards WebSocket upgrades — `cvat/nginx.conf` sets
  `proxy_set_header Upgrade $http_upgrade` and `Connection $connection_upgrade`, with the
  matching `map $http_upgrade` block. No proxy configuration had to change.

### Applications

Backend features are Django apps under `cvat/apps/`. A new app has to be registered in
three places, and missing any one of them fails differently:

1. `INSTALLED_APPS` in `cvat/settings/base.py`
2. A URL include in `cvat/urls.py`, guarded by `apps.is_installed(...)`
3. `load_app_iam_rules(self)` in the app's `AppConfig.ready()`

### Authorization: OPA, not Django permissions

CVAT authorizes through **Open Policy Agent**. `REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]`
installs `PolicyEnforcer` globally, which **asserts** that every view exposes an
`iam_permission_class`; a view without one raises `AssertionError` and returns **500**, not 403.

Policy lives in `.rego` files inside each app's `rules/` directory. Django serves them as a
bundle at `/api/auth/rules`, and the `cvat_opa` container polls that endpoint every 5–15
seconds. Adding a rule therefore needs no OPA restart or rebuild — it is picked up on the
next poll.

### Annotation storage

```
Task ──< Segment ──< Job ──< LabeledShape   (label FK, frame)
                        └──< LabeledImage   (label FK, frame)   — image-level tags
                        └──< LabeledTrack   (label FK, no frame)
```

Three details matter for counting:

- `frame` comes from `FrameAnnotationMixin` and is numbered **per job**, so a frame is only
  unique as `(job_id, frame)`.
- `JobType` has **three** values: `ANNOTATION`, `GROUND_TRUTH`, `CONSENSUS_REPLICA`.
  Ground-truth and consensus jobs cover the *same frames* as the jobs they validate.
- A task's labels may belong to its **project** rather than the task. `Task.get_labels()`
  (`cvat/apps/engine/models.py:1019`) resolves that and also excludes skeleton sublabels.

### Where annotations are written

Saves do **not** go through per-row model signals. They funnel through six functions in
`cvat/apps/dataset_manager/task.py`, each wrapped in `@transaction.atomic`:

```
put_job_data    patch_job_data    delete_job_data
put_task_data   patch_task_data   delete_task_data
```

A single save writes one row per annotation inside one transaction, so a `post_save` hook
on `LabeledShape` would fire once per annotation rather than once per save. These six
functions are the correct hook points.

---

## 2. Data flow

### Reading (page load)

```
ClassAnalyticsPage
  └─ core.analytics.classCounts.get(taskId)        cvat-core/src/api.ts
       └─ serverProxy.analytics.classCounts.get    cvat-core/src/server-proxy.ts
            └─ GET /api/test/class-counts?task_id=1
                 └─ traefik → nginx → uvicorn → Django
                      ├─ PolicyEnforcer → TestPermission → OPA  (data.test.allow)
                      ├─ TestPermission → TaskPermission → OPA  (data.tasks.allow)
                      └─ TestViewSet.class_counts → Postgres
```

The component never calls `axios` directly; it goes through the same
`component → cvat-core-wrapper → api.ts → server-proxy.ts` layering CVAT uses everywhere.

### Writing (annotation change → live update)

```
PATCH /api/jobs/1/annotations?action=create
  └─ dm.task.patch_job_data()                      @transaction.atomic
       ├─ rows written
       └─ _notify_class_counts_changed(job_id=1)
            └─ transaction.on_commit(_publish)     ← deferred
                    │
              [ COMMIT ]
                    │
                    └─ publish_class_counts_changed(task_id)
                         └─ Redis PUBLISH cvat:class-counts:task:1
                              └─ ClassCountsConsumer (any uvicorn worker)
                                   └─ ws frame: {"type":"class_counts_changed","task_id":1}
                                        └─ useClassCountsSocket → onChange()
                                             └─ GET /api/test/class-counts   ← numbers come from here
                                                  └─ chart re-renders
```

The socket carries **no counts**. It says only *which task changed*; the browser then
re-reads the REST endpoint.

---

## 3. API design

### `GET /api/test/class-counts?task_id=<id>`

Counts **images**, not annotations: an image holding five cars counts once for `car`.

**Response**

```json
{
  "task_id": 1,
  "total_images": 5,
  "annotated_images": 4,
  "classes": [
    { "label_id": 1, "label_name": "car",    "color": "#ff0000", "image_count": 2 },
    { "label_id": 2, "label_name": "person", "color": "#00ff00", "image_count": 2 },
    { "label_id": 3, "label_name": "dog",    "color": "#0000ff", "image_count": 1 }
  ]
}
```

`color` is included so the chart can reuse each label's colour from the annotation UI
instead of inventing its own palette.

**Status codes**

| Case | Status |
|---|---|
| Valid request | 200 |
| `task_id` missing, non-numeric, or `< 1` | 400 |
| Task does not exist | 404 |
| Caller cannot view the task | 403 |
| Not authenticated | 401 |

`GET /api/test/ping` returns `{"status": "ok", "app": "test"}` and exists to prove the app
is installed and its OPA rules loaded, independently of any aggregation logic.

### Distinct image counting

An image is identified by `(job_id, frame)`, because frame numbers restart in every job.
The query selects `(label_id, job_id, frame)` from both annotation tables and lets the
database `UNION` them:

```python
annotation_jobs = {
    "job__segment__task_id": task_id,
    "job__type": JobType.ANNOTATION.value,
}

shape_rows = LabeledShape.objects.filter(**annotation_jobs).values_list(
    "label_id", "job_id", "frame"
)
tag_rows = LabeledImage.objects.filter(**annotation_jobs).values_list(
    "label_id", "job_id", "frame"
)

distinct_rows = list(shape_rows.union(tag_rows))

counts = Counter(label_id for label_id, _, _ in distinct_rows)
annotated_images = len({(job_id, frame) for _, job_id, frame in distinct_rows})
```

SQL `UNION` deduplicates, which collapses both kinds of duplicate in one step:

- several annotations of one label on one frame → one row
- the same label present as both a shape and a tag on one frame → one row

It also means the work is proportional to the number of **distinct** `(label, image)`
pairs, not to the number of annotations.

Two deliberate decisions:

- **Only `ANNOTATION` jobs.** Ground-truth and consensus-replica jobs annotate the same
  frames, so including them would inflate every count.
- **Zero-count labels are included.** The `classes` list is built by iterating
  `task.get_labels()`, not by iterating the annotations, so a label nobody has used yet
  appears with `image_count: 0` and stays visible on the chart.

**Tracks are not counted.** `LabeledTrack` has no `frame` of its own — it spans an
interpolated range via `TrackedShape` — so deciding which frames a track "occupies" is a
separate design question. The current endpoint counts shapes and tags only.

### Authentication and permissions (REST)

Authentication is CVAT's own: `TokenAuthentication`, `SessionAuthentication` and access
tokens, with `IsAuthenticated` enforced globally.

Authorization runs **twice** per request, by design:

1. `TestPermission` → `data.test.allow` in `rules/test.rego` — may this user use the
   analytics app at all?
2. `TaskPermission.create_scope_view(request, task)` → `data.tasks.allow` — may this user
   view *this task*?

Delegating the second check to the engine's own `TaskPermission` means sandbox and
organization rules stay identical to the rest of CVAT rather than being reimplemented.

The task is resolved **inside the permission layer** so a missing task answers **404**.
Handing a bare id to `TaskPermission` makes it raise `ValidationError` → 400, which would
have been inconsistent with CVAT's other task-scoped endpoints.

---

## 4. WebSocket implementation

### Why raw ASGI instead of Django Channels

CVAT does not depend on `channels`, and adding it would mean new requirements and a server
image rebuild. Two things were already installed:

- **uvicorn with `websockets`** — the server can already speak the protocol
- **`redis.asyncio`** (redis-py 8.1.0) — an async client for pub/sub

So the endpoint is implemented directly against the ASGI interface. The cost is the
protocol handling in `consumers.py` (241 lines) that Channels would otherwise provide; the
benefit is **no new dependency and no rebuild**.

`cvat/asgi.py` routes by protocol and leaves HTTP completely alone:

```python
_http_application = application

async def application(scope, receive, send):  # noqa: F811
    if scope["type"] == "websocket":
        from cvat.apps.test.consumers import websocket_application
        await websocket_application(scope, receive, send)
        return
    await _http_application(scope, receive, send)
```

Any WebSocket path other than `/api/test/ws/class-counts` is closed with `4404`, so the
socket handler is confined to this feature.

### Endpoint

```
ws://<host>/api/test/ws/class-counts?task_id=<id>
```

It sits under `/api/` so traefik's existing rule and the webpack dev proxy both route it
without new configuration.

### Authentication and task authorization

The CVAT UI authenticates with **session cookies** (`Axios.defaults.withCredentials = true`),
and browsers attach cookies to same-origin WebSocket handshakes. The consumer therefore
reads the session directly from the handshake headers:

```
Cookie header → parse_cookie → SessionStore(session_key) → _auth_user_id → User
```

No token travels in the query string, where it would land in access logs.

Authorization reuses the engine's `TaskPermission` with scope `VIEW` — the same OPA rule
the REST endpoint calls. There is no DRF request in a WebSocket scope, so instead of faking
one, the IAM context is assembled explicitly from the user's highest IAM group and the
task's organization membership.

| Condition | Close code |
|---|---|
| `task_id` missing or malformed | `4400` |
| Not authenticated, or session invalid | `4401` |
| Authenticated but cannot view the task | `4403` |
| Unknown WebSocket path | `4404` |
| Unhandled server error | `4500` |

**These codes are sent after `websocket.accept`, deliberately.** Closing *before* accepting
makes the handshake fail with a plain HTTP 403 and the application code never reaches the
browser, which sees an indistinguishable `1006`. Accepting first is what lets the client
tell "not allowed, stop retrying" apart from "connection dropped, retry". Nothing is ever
sent on a rejected socket.

### Redis pub/sub flow

One channel per task:

```
cvat:class-counts:task:<task_id>
```

A subscriber only ever joins its own task's channel, so task scoping is enforced by the
channel name, not by filtering on the client.

Redis is not an optimisation here — it is **required**. With `NUMPROCS: 2`, the save that
triggers an event and the socket that must deliver it routinely live in different uvicorn
processes. An in-process event bus would deliver an event only when the save and the
socket happened to land in the same worker, and silently drop it otherwise.

**Debounce.** A bulk save writes many rows in one transaction, and both workers may publish.
The publisher takes an atomic Redis lock before sending:

```python
if not client.set(f"{_DEBOUNCE_KEY_PREFIX}{task_id}", 1, nx=True, ex=_DEBOUNCE_SECONDS):
    return
```

`set(nx=True, ex=1)` succeeds for only one caller per second per task; the key expires on
its own, so nothing needs cleaning up.

### `transaction.on_commit()`

All six save functions are `@transaction.atomic`. Publishing inline would create a race:
the event could reach the browser, the browser could re-read `/api/test/class-counts`, and
the transaction could still be uncommitted — returning the **old** numbers and leaving a
stale chart with no further event coming.

`_notify_class_counts_changed()` therefore registers the publish with
`transaction.on_commit()`, which Django runs only after a successful commit. If the
transaction rolls back, the callback never runs and no event is sent, which is correct: a
rolled-back save changed nothing.

### Failure isolation

Analytics must never be able to break an annotation save. Four layers:

1. `apps.is_installed("cvat.apps.test")` — if the app is removed, CVAT is unaffected
2. `transaction.on_commit()` registration wrapped in `try/except`
3. The publish callback body wrapped in `try/except`
4. `publish_class_counts_changed()` swallows and logs every exception, with a 2-second
   Redis socket timeout so an unreachable Redis cannot stall a request

Verified by test: saves succeeded both with the publisher raising `RuntimeError` and with
Redis pointed at an unroutable address. The worst case is a client showing stale numbers
until its next refresh.

---

## 5. Frontend live updates

`useClassCountsSocket(taskId, { onChange })` owns the connection.

**Reconnect.** Exponential backoff from **1s**, doubling to a **30s** ceiling, plus up to
250 ms of jitter so many tabs do not all reconnect on the same tick after a restart. On
close codes `4400`, `4401`, `4403`, `4404` and `1008` it stops retrying — those mean the
server will never accept this client, so retrying would loop forever.

**No duplicate connections or listeners.** The effect depends on `[taskId]` alone; the
`onChange` callback lives in a ref so a re-render never resubscribes. A `closedByUsRef`
guard short-circuits both `connect()` and `scheduleRetry()` after unmount, `clearTimer()`
runs before every new timer, and cleanup nulls `onclose` before closing so a teardown cannot
schedule a retry.

**Resync after an outage.** Events published while the socket was down were never delivered
and are not replayed. On every reconnect after the first, the hook calls `onChange()` once,
which re-reads the REST endpoint and picks up whatever was missed.

**Live refreshes are silent.** A socket-triggered refetch passes `{ silent: true }` so the
chart stays on screen instead of being replaced by a spinner on every annotation change.

A badge shows `Live`, `Connecting`, `Reconnecting` or `Offline`, so the connection state is
visible rather than guessed.

### Why REST remains the source of truth

The socket never carries counts — only `{"type": "class_counts_changed", "task_id": N}`.
Four reasons:

- **One source of truth.** Numbers come from one code path, so the chart cannot drift from
  the API.
- **Permissions stay in one place.** The REST endpoint checks authorization on every read.
  A socket that pushed data would need those checks duplicated at publish time.
- **Missed events are self-healing.** Because every update is a re-read, a dropped event
  costs nothing once the next one arrives or the socket reconnects.
- **The page works without a socket.** If the WebSocket never connects, the page still
  loads and renders correctly; it just will not refresh by itself.

---

## 6. Challenges faced and solutions

### Docker would not start at all

`docker compose up` failed before any code was written. Docker Desktop's WSL 2 backend
could not create its VM:

```
Wsl/Service/RegisterDistro/CreateVm/HCS/0x80070001
```

`wsl --set-version Debian 2` failed with the same code, which proved the fault was in WSL,
not Docker. Ruled out one at a time: Windows features were enabled (and cycled off/on with
reboots), Hyper-V services were running, the hypervisor was live in the event log, WSL was
current, Memory Integrity was already off, and the component store was undamaged. A direct
test showed Hyper-V itself was healthy — `New-VHD`, `Mount-VHD`, and a Gen-2 VM with a disk
attached all worked — so the fault was specific to the Host Compute Service path that WSL 2
uses.

**Solution:** reinstall Docker Desktop as an **all-users** installation with the **Hyper-V
backend** instead of WSL 2. The backend choice is only offered during an all-users install,
which is why switching engines afterwards had not been possible.

### A memory limit broke ClickHouse and crash-looped the server

On a 7.8 GB host the Docker VM was small, so per-container limits were added. ClickHouse was
capped at 512 MB. Its native client still worked, but its **HTTP API on 8123 reset every
connection**, and `cvat_server` crash-looped **19 times** — not an OOM kill
(`OOMKilled=false`), just ClickHouse misbehaving under a tight cgroup limit.

**Solution:** raise ClickHouse to 1 GB. Traefik hit the same problem later at 64 MB and was
raised to 192 MB. The limits live in `docker-compose.override.yml`, which is **gitignored**,
so they do not travel with the repository.

### A missing task returned 400 instead of 404

Testing edge cases showed `?task_id=9999` returning **400**, while CVAT's own
`/api/quality/reports?task_id=9999` returns **404**. The cause was `TaskPermission`
raising `ValidationError` for a missing task inside the permission layer, before the view ran.

**Solution:** resolve the task in `TestPermission.create()` and raise `NotFound` explicitly,
matching the pattern in `cvat/apps/consensus/views.py`. Only the happy path had been checked
before this; the edge cases caught it.

### WebSocket close codes never reached the browser

The first consumer closed rejected connections *before* accepting them. Per the ASGI spec
that makes the handshake fail with HTTP 403, so `4401` and `4403` never arrived — the browser
saw `1006` and could not tell a permission denial from a dropped connection, which would
have meant retrying forever against a socket that would never be accepted.

**Solution:** accept the handshake, then close with the application code. Nothing is ever
sent on those sockets.

### Two of six save paths were never hooked

The final code review found that `delete_job_data` and `delete_task_data` — reachable from
the REST API as the "delete all annotations" endpoints — had no notification hook. Clearing
a job's annotations would have zeroed every count with **no live update**.

**Solution:** hook both. All six mutation entry points now notify.

### Bulk saves are not per-row saves

`post_save` on `LabeledShape` looked like the obvious hook until the save path turned out to
run through `dataset_manager`, writing many rows inside one transaction. A model signal
would have fired once per annotation row instead of once per save.

**Solution:** hook the six `dataset_manager` functions instead, and debounce to one event
per second per task.

### Code is not baked into the running image

`docker-compose.dev.yml` adds a build context but **no bind mount**, so the container runs
the code inside the image. Rebuilding for each change would have cost 30–50 minutes per
iteration.

**Solution during development:** verify the image matched the checkout (same version,
identical file checksums), then `docker cp` changed files in and restart the container —
which survives `docker restart` but not a container recreate. For the frontend, run the
webpack dev server on port 3000 proxying the API to 8080, which hot-reloads.

> **Before deployment, the server image must be rebuilt** so the backend changes are baked
> in rather than living in the container's writable layer:
>
> ```
> docker compose -f docker-compose.yml -f docker-compose.dev.yml build cvat_server
> ```

### The dev proxy did not forward WebSocket upgrades

The page worked on port 8080 but the socket failed on the dev server. `cvat-ui/webpack.config.js`
proxied `/api/` without `ws: true`, so the dev server answered the upgrade itself.

**Solution:** add `ws: true` to the proxy entry.

---

## 7. Verification

Counting correctness was checked against a hand-built task — 5 images, 6 shapes, with
**two cars on one frame** to exercise deduplication, and one frame deliberately left empty:

| Class | Annotations | Expected images | API |
|---|---|---|---|
| car | 3 | 2 | 2 |
| person | 2 | 2 | 2 |
| dog | 1 | 1 | 1 |

Edge cases: missing / non-numeric / zero `task_id` → 400, unknown task → 404, no auth → 401,
zero-count label listed with `0`, a tag on an already-counted frame does not increase the
count, and a tag on an otherwise empty frame does.

WebSocket suite, 13 checks: connection, anonymous and invalid-session rejection, task
authorization, malformed input, unknown path, pub/sub delivery, cross-task isolation (an
event for another task does not reach the socket), live delivery on a real save, and
reconnect.

End to end: with the page open and untouched, adding one dog box to the empty frame moved
the page from `dog: 1 / annotated: 4` to `dog: 2 / annotated: 5`, and deleting it moved it
back — both without a refresh. The database fingerprint after the test was identical to the
baseline.
