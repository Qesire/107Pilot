# pilot107-worker

Phase 0A worker responsibilities:

- reconcile submitted Runs with Slurm state;
- collect stdout, stderr, accounting and environment evidence;
- mark partial evidence explicitly instead of hiding collection failures;
- package Capsules only from authorized Run-scoped files;
- recover from service restart by reading persisted Run metadata.

The worker must not run as root in Docker Compose.
