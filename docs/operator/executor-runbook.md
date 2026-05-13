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
