# 107Pilot Real 107 Read-only Probe

This package runs a read-only ConfigurationSnapshot probe against the real 107
Slurm REST API from inside a normal Slurm job.

It does not submit workload jobs, cancel jobs, read project files, or persist
tokens. The Slurm job itself is only the carrier used to run this probe on the
real platform.

## Files

```text
probe_real107_snapshot.py
probe_real107_cli_snapshot.py
real107_configuration_snapshot_probe.sbatch
README.md
```

## Submit

On the 107 login environment:

```bash
tar -xzf pilot107-real107-probe-<timestamp>.tar.gz
cd pilot107-real107-probe-<timestamp>
sbatch real107_configuration_snapshot_probe.sbatch
```

The bundled sbatch template follows the observed 107 user-job convention:

```text
-p Students
--qos=qos_stu_medium_2gpu
--gres=gpu:A100:1
```

The probe itself does not use GPU compute. These directives are only used as a
known-valid carrier job profile for the current 107 Slurm policy. If an
administrator provides a CPU-only partition/QoS, replace these directives before
submitting.

By default the sbatch task runs:

```bash
scontrol token lifespan=600
```

inside the job and passes the parsed token only in process memory.

## Optional overrides

```bash
export PILOT107_REAL107_BASE_URL="http://107.ustc.edu.cn:6820"
export PILOT107_REAL107_API_VERSION="v0.0.41"
export PILOT107_REAL107_USERNAME="$USER"
export PILOT107_REAL107_TOKEN_COMMAND="scontrol token lifespan=600"
export PILOT107_REAL107_OUT_DIR="real107-probe-output/manual"
sbatch real107_configuration_snapshot_probe.sbatch
```

If token generation inside a job is not available, run manually with an
environment token and then unset it:

```bash
export PILOT107_REAL107_TOKEN="<short-lived-token>"
python3 probe_real107_snapshot.py --out-dir real107-probe-output/manual
unset PILOT107_REAL107_TOKEN
```

## Outputs

```text
real107-probe-output/<job_id>/configuration_snapshot.json
real107-probe-output/<job_id>/probe_report.json
real107-probe-<job_id>.out
real107-probe-<job_id>.err
```

`configuration_snapshot.json` is the stable compatibility artifact.
`probe_report.json` contains diagnostics such as endpoint status, warnings, and
redacted summaries.

## Optional CLI snapshot

If you have temporary test-only SSH or login-shell access, you can also collect
the official CLI facts without requiring REST token access:

```bash
python3 probe_real107_cli_snapshot.py --out-dir real107-cli-snapshot/manual
```

This script only runs fixed read-only allowlisted commands such as
`hostname`, `pwd`, `python -V`, `scontrol show part`, `scontrol show nodes`,
`sinfo`, and `squeue -u <user>`. It does not accept arbitrary commands.

CLI output is redacted before being written. SSH/login-shell access is treated
as test-only evidence and must not be assumed as a production capability.

## Safety

- The probe only uses HTTP GET.
- The probe does not call `sbatch`, `scancel`, or read user output files.
- Token values and Authorization headers are never written to artifacts.
- Raw responses are summarized and redacted before being written.
- Failures are non-blocking compatibility evidence, not competition deployment failures.
- CLI snapshot collection is allowlisted and read-only; SSH access is not a
  product dependency.
