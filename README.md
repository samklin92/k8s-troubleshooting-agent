# Kubernetes Troubleshooting Agent
<img width="1536" height="1024" alt="image" src="https://github.com/user-attachments/assets/96da98c1-72ff-4a9b-85e4-ad0c83dff1da" />


An agentic Kubernetes diagnostic tool that investigates unhealthy pods by gathering evidence from a live cluster, reasoning through the failure state, and producing a specific root cause with actionable remediation guidance.

Instead of returning a generic status like:

> "Pod is unhealthy"

the agent performs the same multi-step investigation an engineer would during a real incident:

* Inspect pod state
* Correlate restart behavior
* Analyze container logs
* Classify the fault family
* Identify the underlying cause
* Recommend a concrete fix

Built and tested against a real local `kind` cluster with intentionally injected Kubernetes failures.

---

# Why this exists

Kubernetes exposes symptoms very well.

It does **not** always explain causes.

For example:

```bash
kubectl get pods
```

may show:

```text
CrashLoopBackOff
```

But that alone does not explain:

* why the container crashed,
* what triggered the restart loop,
* or how to fix it.

Finding the actual cause typically requires manually correlating:

* container state,
* restart history,
* scheduling conditions,
* and application logs.

This project automates that investigative workflow using an LLM-driven agentic loop.

---

# Technical highlights

* Agentic troubleshooting workflow using Claude tool-calling
* Real Kubernetes fault injection with reproducible manifests
* Structured fault-family normalization for unstable Kubernetes states
* Tool-first verification before agent integration
* Root-cause analysis driven by evidence correlation
* Explicit iteration and API safety limits
* Tested against real cluster behavior rather than mocked responses

---

# Architecture

```text
User symptom
(e.g. "A pod seems unhealthy")
            |
            v
+----------------------------------+
| Claude Agentic Reasoning Loop    |
|  - Observe                       |
|  - Reason                        |
|  - Decide next action            |
+----------------------------------+
            |
            +----------------------+
            |                      |
            v                      v

+-------------------+    +-------------------+
| list_pods         |    | get_pod_logs      |
|                   |    |                   |
| - pod phase       |    | - current logs    |
| - scheduling      |    | - previous logs   |
| - restart counts  |    | - crash evidence  |
| - container state |    |                   |
+-------------------+    +-------------------+
            |
            v
Root cause + evidence + remediation
```

---

# How the agent reasons

The system follows an iterative observe → reason → act workflow.

1. Gather initial cluster evidence using `list_pods`
2. Classify the likely fault family
3. Decide whether additional evidence is required
4. Call targeted tools only when necessary
5. Produce a root-cause diagnosis and remediation plan

The agent does not blindly call every tool.

It selectively gathers additional evidence based on what it already knows, similar to how a human engineer investigates incidents.

---

# Design principle

## Tools were verified independently before agent integration

Each tool was built and tested directly against a real Kubernetes fault before being exposed to the agent.

This prevented ambiguous failures where it becomes unclear whether:

* the tool is wrong,
* the cluster state is misleading,
* or the LLM reasoning failed.

Verifying tools independently uncovered several real Kubernetes and client edge cases before they became agent failures.

---

# Fault families currently supported

| Fault family                                              | Detection source | Root cause source        |
| --------------------------------------------------------- | ---------------- | ------------------------ |
| `image_pull_failure` (`ImagePullBackOff`, `ErrImagePull`) | `list_pods`      | Pod/container status     |
| `crash_loop` (`CrashLoopBackOff`)                         | `list_pods`      | `get_pod_logs`           |
| `scheduling_failure` (`Pending`, unschedulable)           | `list_pods`      | Pod scheduling condition |

The current toolset intentionally excludes:

* `kubectl describe`-style resource inspection
* Kubernetes Events analysis

Testing showed the existing tools already provide complete root-cause coverage for the supported fault families.

Those capabilities would become useful for future fault categories such as:

* Service/networking failures
* PersistentVolume issues
* ResourceQuota enforcement
* Ingress misconfiguration

---

# Real bugs discovered during verification

## 1. Fault-state cycling during image pull failures

A broken image pull alternates between:

* `ErrImagePull`
* `ImagePullBackOff`

depending on the exact polling moment.

Classification based purely on raw reason strings produced non-deterministic detection.

### Fix

Implemented a `FAULT_FAMILIES` normalization layer mapping both states to a single fault category.

---

## 2. `phase=Running` during `CrashLoopBackOff`

Kubernetes may report a crashing pod as:

```text
phase = Running
```

because the container briefly enters a running state between crashes.

A health check based only on pod phase would incorrectly classify the pod as healthy.

### Fix

Health evaluation was updated to inspect container state reasons and restart behavior rather than relying on pod phase alone.

---

## 3. Kubernetes Python client log deserialization issue

The Kubernetes Python client returned malformed log output when using default auto-deserialization.

Instead of properly decoded text:

```text
line1
line2
```

the client returned:

```text
b'line1\nline2'
```

with literal byte-string formatting and escaped newlines.

### Fix

Switched to:

```python
_preload_content=False
```

and manually decoded raw bytes.

This produced clean, reliable log output for agent analysis.

---

## 4. Incorrect remediation guidance

An early version of the agent recommended:

```bash
kubectl set image
```

against a standalone Pod.

That command only works for mutable controllers such as:

* Deployments
* ReplicaSets
* StatefulSets

Standalone Pods are immutable after creation.

### Fix

The system prompt was updated with explicit Kubernetes immutability constraints.

The corrected remediation behavior generalized successfully across unrelated fault types.

---

## 5. Containerd log-retention timing during automated testing

Even when the Kubernetes API call for `--previous` container logs succeeded without error, the actual content returned was sometimes a containerd-level runtime message (`unable to retrieve container logs for containerd://...`) rather than real log content - if the previous container's logs had already been garbage collected.

This was caught specifically by writing an automated pytest suite and running it in CI, under different timing than manual verification. Manual testing happened to run within the log-retention window every time; an automated run did not.

### Fix

`get_pod_logs.py` now detects this specific runtime message and reports it as `previous_available=False`, since "logs unavailable" is not usable diagnostic content even though the API call itself didn't raise an error. A regression test locks in the correct behavior going forward.

---

# Testing & CI

Manual verification against a live cluster found the first four bugs above. The fifth was found only after that verification process was converted into an automated pytest suite - proof that codifying manual checks catches real issues manual testing alone can miss.

## Test suite

Integration tests run against a real `kind` cluster with the same fault-injection manifests used during development - not mocks. Each test applies a real fault, asserts on the tool's actual output, then tears the fault down.

```bash
pytest tests/ -v
```

## Continuous integration

Two GitHub Actions jobs, intentionally separated by cost and reliability profile:

- **`test-tools`** - runs on every push and pull request. Spins up a fresh `kind` cluster in the runner, runs the full pytest suite against `list_pods.py` and `get_pod_logs.py`. Free, deterministic, fast. This is the job that gates merges.
- **`test-agent-smoke`** - runs only on direct pushes to `main`, after `test-tools` passes. Injects a real fault, runs the full Claude agentic loop against it, and verifies the diagnosis actually names the root cause. Costs real Anthropic API tokens and depends on an external service, so it's an integration smoke test, not a merge gate.

---

# Why use an agent instead of static monitoring?

Traditional monitoring systems detect symptoms.

This project investigates causes.

Example:

| Traditional monitoring     | This agent                          |
| -------------------------- | ----------------------------------- |
| Detects `CrashLoopBackOff` | Explains why the container crashed  |
| Raises an alert            | Correlates restart history and logs |
| Shows unhealthy state      | Produces remediation guidance       |

The goal is not replacing monitoring.

The goal is reducing time-to-root-cause during incidents.

---

# Example investigation

## Inject a fault

```bash
kubectl apply -f manifests/faults/crashloopbackoff.yaml
```

Wait for the container to fail:

```bash
sleep 15
```

Run the agent:

```bash
python k8s_agent/agent.py "A pod is failing" default
```

## Example output

```text
Detected fault family: crash_loop

Evidence:
- Restart count: 6
- Container state: CrashLoopBackOff
- Previous container logs:
  Error: DATABASE_URL environment variable missing

Root cause:
The application exits immediately because DATABASE_URL is not configured.

Recommended fix:
Add DATABASE_URL to the Deployment environment variables or Kubernetes Secret.
```

Clean up:

```bash
kubectl delete -f manifests/faults/crashloopbackoff.yaml
```

---

# Setup

Install dependencies:

```bash
pip install kubernetes anthropic python-dotenv fastapi uvicorn prometheus-client
```

Create a local cluster using `kind`:

```bash
kind create cluster --name devops-agent-cluster
```

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=your-api-key
```

---

# Usage

```bash
python k8s_agent/agent.py "There's a pod that seems unhealthy" default
```

## Running as a service

The agent can also run as an HTTP service rather than a one-shot CLI script, with Prometheus metrics exposed for scraping:

```bash
cd k8s_agent
uvicorn main:app --port 8000
```

Endpoints:

- `POST /investigate` - run a real investigation. Body: `{"symptom": "...", "namespace": "default"}`
- `GET /metrics` - Prometheus scrape endpoint
- `GET /healthz` - liveness check, does not touch the cluster or Claude

```bash
curl -X POST http://localhost:8000/investigate \
  -H "Content-Type: application/json" \
  -d '{"symptom": "A pod looks broken", "namespace": "default"}'
```

### What the metrics actually show

Verified against two different live fault types, the metrics confirm the agent varies its tool usage based on what it finds - not just calling every available tool every time:

| Fault type | Tool calls made | Iterations | Why |
|---|---|---|---|
| `crash_loop` | `list_pods`, `get_pod_logs` | 3 | Root cause only visible in container logs |
| `image_pull_failure` | `list_pods` only | 2 | `list_pods`'s message field was already sufficient |

Metrics exposed: investigation outcome (diagnosed/inconclusive), tool calls by name, investigation duration, iteration count, and fault families observed - see `k8s_agent/metrics.py` for full definitions and rationale.

---

# Project structure

```text
k8s_agent/
├── list_pods.py
├── get_pod_logs.py
└── agent.py

manifests/faults/
├── imagepullbackoff.yaml
├── crashloopbackoff.yaml
└── pending-unschedulable.yaml
```

## Components

### `list_pods.py`

Provides structured Kubernetes pod diagnostics including:

* pod phase
* scheduling conditions
* restart counts
* container state
* fault-family classification

### `get_pod_logs.py`

Retrieves:

* current container logs
* previous crash logs

Used only when pod state alone is insufficient for root-cause determination.

### `agent.py`

Implements the Claude-powered reasoning loop coordinating tool selection and diagnosis.

---

# Safety

The agent loop is protected by:

```python
MAX_ITERATIONS = 6
```

This is a hard limit preventing:

* runaway tool invocation,
* excessive Kubernetes API usage,
* and uncontrolled token consumption.

In production environments, the troubleshooting agent should also run under read-only RBAC permissions since diagnosis does not require write access to cluster resources.

---

# Limitations

Current coverage is intentionally narrow and focused.

Not yet supported:

* Service/networking failures
* PersistentVolume issues
* ResourceQuota enforcement
* Ingress misconfiguration
* Multi-node scheduling conflicts
* Taints/tolerations analysis
* Node affinity conflicts

The project has only been validated against a single-node local `kind` cluster.

---

# Future improvements

Planned extensions include:

* Kubernetes Events analysis
* `kubectl describe` parity tooling
* Service and networking diagnostics
* PersistentVolume troubleshooting
* Multi-node scheduling analysis
* Prometheus integration
* Slack/Teams incident-response integration
* Automatic remediation suggestion ranking
* Historical incident memory and comparison

---

# Key takeaway

This project is not a generic “AI + Kubernetes” demo.

It is an investigation-oriented troubleshooting system built around:

* real Kubernetes behavior,
* real operational edge cases,
* deterministic tool validation,
* and evidence-driven reasoning.

The focus is not simply identifying unhealthy resources.

The focus is identifying *why* they failed.

