# Executor Runbook

## `.parts/` staging area (Phase 2 W2b1+)

Executors that handle files ≥ 100 MiB stage downloads into
`${DLW_EXECUTOR_PARTS_DIR_PATH}/` (default `./parts`) before uploading
to S3. In production:

- Mount a writable PV at the configured path; sized to at least the
  largest expected single file + 20% headroom.
- Operator must `chown` the dir to the user running the executor
  process.
- Controller's `sweep_paused_disk_full` recovers subtasks back to
  `pending` if disk frees up. No manual intervention needed for
  transient ENOSPC.

## Task cancellation latency (Phase 2 W2b2+)

`POST /api/v1/tasks/{task_id}/cancel` flips the task to `cancelling`. The
scheduler stops handing out new subtasks for that task immediately.
In-flight subtasks finish naturally:

- Small files (< 100 MiB, W4 `HfS3StreamDownloader`): typically seconds.
- Large files (>= 100 MiB, W2b1 `DirectOffsetDownloader`): can take **up to
  several minutes** depending on file size and bandwidth.

The task stays in `cancelling` until the last in-flight subtask reaches a
terminal state, then transitions to `cancelled`. Paused subtasks
(`paused_disk_full` / `paused_external`) at the moment of `/cancel` are
force-terminated synchronously inside the cancel transaction.

If a task stays in `cancelling` for unexpectedly long (e.g. > 30 minutes
on a fast network), check executor logs for stuck downloads. Operator
escalation: re-issue `/cancel` — it is idempotent and will re-force-terminate
any paused subtasks that appeared after the original cancel.

A future Phase 2 W3 release will add heartbeat-carried cancellation
signals so executors abort in-flight downloads on chunk boundaries,
reducing latency to sub-minute.
