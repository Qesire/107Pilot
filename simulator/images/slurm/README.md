# Slurm Simulator Image

This image is for the Phase 0A Docker simulator only.

It provides:

- `slurmctld`
- `slurmd`
- `slurmdbd`
- `slurmrestd`
- local `mariadbd` for the simulator accounting database
- `sbatch`, `squeue`, `sacct`, `scancel`
- `alice`, `bob`, `pilot107`, `slurm` Linux users
- a deterministic simulator-only MUNGE key
- a simulator-only `slurmdbd.conf` with local MariaDB credentials
- `/etc/pilot107/slurm-sim-version-manifest.json`, which records the current
  image version contract

The 25.11 target image is built from the official SchedMD source tarball with:

```bash
bash scripts/build-slurm-sim-25-image.sh
bash scripts/check-slurm-sim-25-image.sh
```

Use `SLURM_SIM_IMAGE=pilot107/slurm-sim:25.11-real107` when starting Compose.
The Ubuntu-package 23.11 image is retained only as a compatibility fallback.

The MUNGE key in this directory is intentionally static because all containers
in the simulator must trust each other. Do not use this image or key outside
local/competition simulation.
