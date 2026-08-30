# Frontend scientific journeys and sbatch-first authoring design

- Date: 2026-08-30
- Status: approved
- Scope: user-visible scientific job lifecycle, Run sharing, and template publication
- Deployment target: local web mounted against authoritative `vm-slurm`
- Test boundary: public frontend interactions only

## 1. Problem and observed evidence

107Pilot has working pages for projects, Runs, files, cluster facts, Studio,
market, successful-Run sharing, and template publication. Testing those pages in
isolation does not prove that a scientist can complete a useful computation.
Acceptance must instead begin with a user's scientific goal and follow every
decision, action, Slurm transition, result, and recovery step to completion.

Read-only frontend inspection on 2026-08-30 established this baseline:

- the cluster page reports `CPU-RC` and `qos_cpu_rc`, while a new Studio draft
  still defaults to `Students` and `qos_stu_cpu_long`;
- the same user has completed real Jobs but the cluster page still presents the
  personal Slurm entitlement as unknown;
- Studio treats canonical Contract YAML/JSON as the central source editor and
  an sbatch script as a secondary, not-yet-validated preview;
- Environment and Extensions are raw JSON inputs;
- template publication asks authors for raw Compatibility and Publication
  metadata JSON;
- market, adoption, publication, and sharing details expose internal JSON in
  ordinary user paths;
- file search, type filtering, and manual path entry are now present, but their
  correctness, permission feedback, and handoff into Studio remain unproven;
- successful Runs expose a direct sharing form, but privacy boundaries and the
  visitor experience have not been accepted end to end.

These are workflow and truth-consistency defects, not merely visual issues.

## 2. Goals

1. Prove that a non-programming undergraduate can run, inspect, modify, and
   share a real scientific computation without understanding JSON, shell, or
   Slurm internals.
2. Prove that a graduate researcher can author and monitor a large Slurm array,
   diagnose partial failure, rerun failed work, and trace aggregate results.
3. Make the exact submitted sbatch script the primary job-authoring artifact.
4. Make cluster facts, personal entitlements, editor defaults, preflight, and
   actual submission agree.
5. Distinguish a selectively shared successful Run from a curated, reproducible
   Template Release.
6. Validate every promise through the public frontend, backed by real vm-slurm
   Jobs, files, Evidence, and immutable identifiers.
7. Define the Agent's authority and approval boundary at every entry point.

## 3. Non-goals

- A full visual redesign or new design system.
- Treating browser automation as human usability evidence.
- Supporting every Slurm directive in a structured form.
- Claiming scientific validity solely because a Job exits successfully.
- Replacing vm-slurm with a different scheduler during this slice.
- Using private backend routes to manufacture acceptance state.

## 4. Options considered

### 4.1 Page-by-page coverage

Exercise each button and form independently. This is useful for regression but
cannot prove that required information survives page transitions or that the
user reaches a scientific result. Rejected as the primary method.

### 4.2 Persona journeys only

Run realistic undergraduate and graduate tasks end to end. This reveals user
friction but can miss platform promises such as privacy, provenance, and
reproducibility. Insufficient alone.

### 4.3 Persona journeys crossed with product promises

Use a realistic journey as the execution spine and, at every step, audit the
information source, permitted action, visible feedback, durable fact, recovery,
and evidence. Adopted.

## 5. Truth and acceptance model

Every step records:

| Field | Meaning |
|---|---|
| User goal | What the user is trying to accomplish now |
| Required information | Facts needed before a safe decision |
| Visible source | Where the frontend supplies those facts |
| User action | Click, text entry, selection, confirmation, or edit |
| System response | Immediate human-readable feedback |
| Durable fact | Job, Run, file, Evidence, release, or publication created |
| Recovery | How the user continues after an expected failure |
| Acceptance evidence | Screenshot, Run ID, Job ID, file, or public URL |

The following layers must agree:

```text
vm-slurm facts
  -> platform snapshot and personal entitlement
  -> cluster page and editor defaults
  -> preflight and approval summary
  -> exact submitted sbatch
  -> Job/Run states
  -> files, Evidence, sharing, and release provenance
```

An HTTP success or database record without a corresponding user-visible outcome
does not pass acceptance.

## 6. Persona A: non-programming undergraduate

### 6.1 Scientific task

Lin is an undergraduate comparing two-dimensional heat diffusion under three
boundary temperatures. Lin understands grid size, iteration count, temperature,
and plots but does not understand Python, shell, Slurm, QoS, or JSON.

The expected journey is:

```text
choose a verified experiment -> enter scientific parameters -> verify resources
-> submit -> observe -> inspect plots/data -> modify and rerun -> share the Run
```

### 6.2 Entry and orientation

Actions:

1. Open the workload home as a clean user.
2. Select an entry equivalent to "run an existing experiment".
3. Reach the market without requiring the Agent.

Required information and response:

- explain the three primary choices: use a template, create a job, inspect Runs;
- summarize whether CPU computing is available in human language;
- explain an unknown or stale entitlement and offer a refresh action;
- keep Run, Contract, snapshot, and Evidence terminology out of the novice path
  until it is needed.

Acceptance:

- the market is reachable within three primary clicks;
- no raw JSON is presented;
- the Agent is an optional assistant, not the only navigation mechanism.

### 6.3 Template discovery

Actions:

1. Search for `heat diffusion`, `finite difference`, or an equivalent Chinese
   phrase.
2. Filter by CPU, education, and no-code suitability.
3. Open the template details.

The details must expose:

- the scientific question and method;
- inputs, units, valid ranges, and outputs;
- estimated time, CPU, memory, partition, QoS, and output size;
- current-platform compatibility and the timestamp/source of that conclusion;
- author, version, verification state, and example result;
- the difference between "shared example" and "verified template".

Acceptance:

- search and filters visibly narrow results;
- compatibility is derived from current cluster facts rather than hard-coded;
- raw compatibility JSON is not shown;
- the user can predict what will execute and what will be produced.

### 6.4 Adoption, paths, and parameters

Actions:

1. Adopt the template.
2. search for or manually enter a working directory;
3. enter grid size, iteration count, and boundary temperatures;
4. select or upload required input files.

The frontend must provide:

- typed scientific fields with units, ranges, descriptions, and defaults;
- a searchable file picker and an absolute-path input;
- existence, object type, and write-permission feedback;
- an output-directory preview;
- scientific validation such as "grid size must be an integer from 10 to 1000";
- state preservation when navigating backward.

Acceptance:

- neither Environment JSON nor Extensions JSON is required;
- a missing or unreadable path is never silently accepted;
- a selected file can be handed into the editor without retyping its path.

### 6.5 Job confirmation

The page presents a guided parameter view for novices and the exact sbatch
script behind it. The script is the submission artifact, not a projection from
a user-facing JSON Contract. A valid example is:

```bash
#!/bin/bash
#SBATCH --job-name=heat-study
#SBATCH --partition=CPU-RC
#SBATCH --qos=qos_cpu_rc
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:20:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err

cd /public/home/alice/heat-study
python3 run_heat.py \
  --grid-size 200 \
  --iterations 5000 \
  --temperatures 20,50,80
```

The editor must validate:

- partition/QoS existence and compatibility;
- the current user's entitlement;
- CPU, memory, time, and policy limits;
- work directory, command, inputs, and output write permission;
- unsupported or dangerous directives;
- expected outputs and plausible storage demand.

Acceptance:

- defaults are `CPU-RC` and `qos_cpu_rc` when those are authoritative;
- errors identify the script line and give an executable correction;
- the page explicitly states that the visible script is what will be submitted;
- Contract JSON/YAML is absent from the normal path.

### 6.6 Submission and observation

Actions:

1. Select "validate and submit".
2. Read a natural-language approval summary.
3. Confirm resource, command, and path details.
4. Submit and observe preparation, queueing, running, and completion.

The response must include:

- the real Slurm Job ID;
- partition, QoS, CPU, memory, limit, working directory, and command;
- status timestamp and source;
- incremental stdout/stderr;
- a scoped cancellation action;
- visible progress and timeout explanation for long operations.

Acceptance:

- the Job exists in vm-slurm, not only the platform store;
- Slurm and Run terminal states agree;
- refresh does not lose the Run;
- the page never leaves an empty `"\n\n"` response as the final Agent feedback.

### 6.7 Results and rerun

The successful Run separates logs from scientific results and exposes image
preview, CSV download, generation time, size, parameters, template version, and
completeness. Results must be read from this Job's real working directory.

Rerun actions:

1. Create a rerun from the completed Run.
2. Change one boundary temperature.
3. submit a distinct Job;
4. compare parameter, resource, and result differences.

Acceptance:

- inherited inputs remain editable;
- the original Run and Evidence remain immutable;
- each result traces to its Run, Job, script, and template version.

### 6.8 Direct Run sharing

Actions:

1. Open Share on the successful Run.
2. enter title, description, tags, and visibility;
3. independently select resource summary, result summary, reference-only
   contract, script, evidence preview, and assets;
4. inspect a final visitor preview;
5. explicitly confirm and publish;
6. open the stable URL as a different user;
7. change visibility and withdraw the publication.

Required behavior:

- list exactly what will and will not be disclosed;
- detect usernames, absolute private paths, secrets, host addresses, environment
  variables, and sensitive logs;
- label the artifact as a Run record that is not guaranteed reproducible;
- prevent unselected files from being addressed by the visitor;
- preserve provenance to the source Run without exposing private paths.

Acceptance:

- publication is impossible without explicit confirmation;
- preview and visitor view agree;
- withdrawal or visibility changes take effect;
- direct sharing never receives a "verified template" badge.

## 7. Persona B: graduate researcher running a large experiment

### 7.1 Scientific task

Zhou is running a two-dimensional Ising temperature sweep with 40 temperature
points and five repetitions, for 200 array tasks. The experiment needs bounded
parallelism, deterministic random seeds, environment provenance, partial-failure
diagnosis, selective rerun, and aggregate output.

### 7.2 Resource and entitlement discovery

Before editing, the frontend must expose:

- nodes and CPU topology;
- partitions, QoS, maximum time, and maximum array size;
- the user's allowed partitions and QoS;
- queue load and data age;
- available storage;
- whether each fact is live Slurm data, configured policy, or inference.

Acceptance:

- cluster, editor, preflight, and submission use the same snapshot;
- an unknown entitlement is explained rather than contradicted by an allowed
  submission;
- stale facts have a visible refresh path.

### 7.3 File preparation

Zhou searches for the project, manually opens an absolute path, previews
`simulate.py` and `temperatures.csv`, and selects a result directory.

Acceptance:

- name and path-fragment search work;
- type filters are correct;
- large files are bounded or paged rather than fully loaded;
- permission failures are actionable;
- selections transfer into the job editor.

### 7.4 Array authoring

The primary editor accepts and preserves:

```bash
#!/bin/bash
#SBATCH --job-name=ising-sweep
#SBATCH --partition=CPU-RC
#SBATCH --qos=qos_cpu_rc
#SBATCH --array=0-199%8
#SBATCH --cpus-per-task=2
#SBATCH --mem=6G
#SBATCH --time=02:00:00
#SBATCH --output=logs/%A_%a.out
#SBATCH --error=logs/%A_%a.err

set -euo pipefail
cd /public/home/alice/ising
python3 simulate.py \
  --task-id "$SLURM_ARRAY_TASK_ID" \
  --parameter-file temperatures.csv \
  --seed "$SLURM_ARRAY_TASK_ID" \
  --output-dir results
```

Required behavior:

- syntax highlighting and line diagnostics;
- array expression and concurrency-limit explanation;
- support for modules, conda, containers, environment variables, dependencies,
  comments, quoting, and unknown legal directives;
- maximum-concurrent resource estimation rather than naïvely multiplying all
  200 tasks;
- semantic preservation across save, reload, adoption, and rerun.

Acceptance:

- unknown legal directives are never dropped;
- directive order, comments, shell quotes, and array syntax survive round trips;
- actual submitted script is available for byte-level comparison.

### 7.5 Large-run monitoring and recovery

The Run view must provide a parent-array summary, child status counts, task-ID
and status filters, individual logs, common failure grouping, first failure, and
a "rerun failed tasks" action.

Acceptance:

- the user does not need to open 200 pages;
- 197/200 success is represented as partial completion, not silent success;
- selective rerun creates a new Run linked to its source;
- cancellation states whether it targets a child or the full array.

### 7.6 Result traceability

The experiment retains the sbatch script, parameter-file digest, source/template
version, environment summary, random seed per task, Slurm resources and exits,
and the source-task set for every aggregate artifact.

Acceptance:

- every plot can be traced to contributing tasks;
- missing tasks are visible in aggregate results;
- scheduler success is not described as proof of scientific correctness.

## 8. Sbatch-first authoring contract

### 8.1 Source of truth

The user's sbatch text is the source of truth. Internal Contract objects may be
derived for validation and orchestration but are not a competing editable
source. The submission receipt includes the exact script digest and downloadable
script.

### 8.2 Guided and expert views

- novice view: scientific parameters and common resource controls with an
  expandable exact script;
- expert view: direct script editor with structured help for recognized fields;
- a structured edit updates the script;
- a script edit updates recognized fields;
- unrecognized legal content is retained verbatim;
- a lossy transformation is blocked and explained.

### 8.3 Diagnostics

Diagnostics bind to a line and distinguish:

- syntax error;
- cluster incompatibility;
- personal entitlement failure;
- missing path or command;
- policy warning;
- portable-science warning.

Each diagnostic identifies its fact source and offers an automatic correction
only when the correction is deterministic and reviewable.

### 8.4 No ordinary JSON authoring

Environment, compatibility, extensions, publication metadata, and adoption
plans use structured controls or read-only human summaries. Raw JSON may exist in
a deliberately opened developer diagnostic view, never as a prerequisite for a
scientist.

## 9. Formal Template Release journey

A Template Release is stricter than direct Run sharing.

### 9.1 Start from a successful Run

The publication draft inherits the exact script, parameter schema, inputs,
outputs, resource envelope, environment, and evidence. The author supplies
structured fields for compatibility, overridable resources, parameter ranges,
output rules, environment requirements, visibility, audience, and tags.

### 9.2 Sanitization

The platform scans usernames, private absolute paths, secrets, host addresses,
private datasets, non-portable environments, temporary files, and author-only
Slurm configuration.

Acceptance:

- changes are shown as a reviewable diff;
- no silent rewrite is allowed;
- high-risk findings block publication;
- every public asset is explicitly selected.

### 9.3 Compatibility

The platform validates script parsing, current partitions/QoS, satisfiable
resources, complete inputs, rebuildable environment, and output declarations.
The conclusion carries a fact source and capture time, and separates "compatible
here" from speculative portability elsewhere.

### 9.4 Independent reproduction

The sanitized template runs in a new workspace through the ordinary frontend
submission path. It may not read hidden output from the source Run. Exit state,
key outputs, completeness, and declared invariants are compared. Failure returns
logs and the draft to editing. Only a successful independent Run earns a verified
state.

### 9.5 Review and versioned publication

Reviewers see the scientific description, sanitization diff, exact sbatch,
compatibility evidence, reproduction Run, input/output contract, and risks.
Rejection is reasoned, history is retained, and an update creates a new immutable
version rather than rewriting a released version.

### 9.6 Adoption by another user

A different user sees purpose, conditions, parameters, live compatibility,
verification time, example results, author, and version. Adoption copies the
release into that user's workspace, substitutes private paths, revalidates
personal entitlement, and produces an editable personal sbatch. Later publisher
updates do not mutate the adopted version.

Acceptance requires a new user to complete an independent vm-slurm Run without
access to the publisher's private workspace.

## 10. Agent authority by entry point

The Agent always pairs structured approval content with a natural-language
description. It may not hide the exact sbatch or invent unknown cluster facts.

| Entry point | Default authority | Approval boundary |
|---|---|---|
| Cluster | Read-only explanation and diagnosis | Any mutation is out of scope |
| Files | Read and suggest paths | write, overwrite, and delete require approval |
| Studio | Edit the current draft | submission requires approval |
| Run | Diagnose and prepare rerun/cancel action | cancel and rerun require approval |
| Direct share | Prepare disclosure draft | publication/withdrawal require approval |
| Template release | Prepare, sanitize, and validate draft | formal publication requires approval |

Tool budgets are safety ceilings, not small normal-operation quotas. The UI must
show streaming natural-language progress, fold tool details by default, and end
with a useful response or a specific recoverable timeout rather than blank
deltas.

## 11. Browser acceptance protocol

All acceptance actions use the public local frontend. Direct API calls may be
used after a failure only as read-only diagnosis and cannot count as a passing
step.

For each step, capture:

- role and clean starting state;
- visible information before the action;
- clicks, input, and confirmation;
- immediate feedback;
- Run ID, Job ID, script digest, file, Evidence ID, release ID, or publication
  URL produced;
- refresh/relogin behavior;
- recovery from one representative invalid input;
- screenshots for decision, approval, terminal state, and visitor view.

Defect severity:

- **Blocker:** real Job cannot run, result is wrong, unauthorized access, or
  sensitive disclosure.
- **Severe:** truth contradiction, displayed/submitted script mismatch, false
  sharing or reproducibility promise.
- **Normal:** journey completes only through an avoidable workaround.
- **Experience:** terminology or feedback is unclear without changing outcome.

Automated browser results prove repeatability and integration, not novice
usability. Human novice evidence remains a separate release criterion.

## 12. Release gates

The slice passes only when:

1. both personas complete their scientific journeys through the frontend;
2. every submission enters vm-slurm and returns a real Job ID;
3. cluster facts, entitlement, defaults, preflight, and submission agree;
4. exact sbatch is primary, durable, round-trip safe, and downloadable;
5. no normal journey requires raw JSON/YAML;
6. results trace to Jobs, scripts, inputs, and template versions;
7. direct Run sharing passes cross-user privacy and withdrawal tests;
8. a formal template passes sanitization, compatibility, independent
   reproduction, review, versioning, and cross-user adoption;
9. Agent actions observe the declared authority and approval boundaries;
10. blockers and severe defects are closed, with automated regression evidence.

## 13. Delivery sequence

1. Preserve the current pages as a recorded baseline.
2. Repair authoritative fact/default/entitlement consistency.
3. Implement the sbatch-first editor and lossless round-trip tests;
4. replace ordinary raw JSON publication and adoption fields;
5. close novice file/parameter/submission/result/rerun journey;
6. close array monitoring/partial-failure/selective-rerun journey;
7. accept direct Run sharing across users;
8. accept Template Release sanitization, reproduction, review, and adoption;
9. rerun the complete browser matrix and publish the evidence report.
