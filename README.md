# Kubernetes Troubleshooting Agent

An agentic Kubernetes diagnostic tool. Claude investigates pod failures by
calling tools to gather evidence (pod status, container logs), reasons about
what it's found, and produces a specific root cause and an actionable fix -
rather than a generic "the pod is unhealthy" response.

Built and tested against a local `kind` cluster with deliberately injected
faults, verifying each tool independently against real cluster behavior
before wiring anything into the agent loop.

## Why this exists

`kubectl get pods` tells you a pod is in `CrashLoopBackOff`. It doesn't tell
you *why* - that requires correlating container state, restart history, and
log output, which is exactly the kind of multi-step investigation an
engineer does manually during an incident. This tool automates that
investigation: it decides which signals to check based on what it's already
found, the same way a human would.

## Architecture
Symptom (free text)

|

v

agent.py (Claude agentic loop, MAX_ITERATIONS=6 safety cap)

|

+--> list_pods    (pod phase, scheduling status, container state,

|                  fault-family classification)

|

+--> get_pod_logs (current + previous container logs, called only

when list_pods alone doesn't explain the cause)

|

v

Root cause + evidence + recommended fix
### Design principle: tools verified independently before being given to the agent

Each tool was built and tested against a real fault on a real cluster
*before* being exposed to Claude as a callable tool. This caught several
real bugs that would otherwise have surfaced as confusing agent behavior
with no clear cause - see below.

## Fault families covered

| Fault family | Detected by | Root cause source |
|---|---|---|
| `image_pull_failure` (`ImagePullBackOff` / `ErrImagePull`) | `list_pods` | `list_pods` message field is usually sufficient |
| `crash_loop` (`CrashLoopBackOff`) | `list_pods` (restart count + state reason) | `get_pod_logs` - required, root cause only in container output |
| `scheduling_failure` (unschedulable / `Pending`) | `list_pods` (`PodScheduled` condition) | `list_pods` - the condition's own `message` field already has full detail |

`get_events` and `describe_resource` were deliberately not built - testing
showed `list_pods` + `get_pod_logs` already provide a complete root cause
for all three fault families above. They would be the natural next addition
if a fault type requiring them is identified (e.g. networking/Service
misconfiguration, where the relevant signal lives in Events history rather
than current pod state).

## Real bugs found during verification

1. **Fault-state cycling** - a failing image pull alternates between
   `ImagePullBackOff` and `ErrImagePull` depending on the exact instant you
   poll. Classifying on the raw reason string alone would make detection
   non-deterministic. Fixed with a `FAULT_FAMILIES` normalization map that
   treats both as the same underlying fault.
2. **`phase=Running` during CrashLoopBackOff** - Kubernetes reports a
   crashing pod's phase as `Running`, not `Pending` or `Failed`, because
   there are brief windows between crashes where the container genuinely is
   running. Health checks based on `phase` alone would miss this fault
   entirely; `is_healthy` checks container state reasons instead.
3. **Kubernetes Python client log deserialization bug** - calling
   `read_namespaced_pod_log()` with default auto-deserialization returned a
   `str` containing the literal characters `b'...'` and a literal
   backslash-n, rather than decoded text with a real newline. Confirmed by
   comparing against `_preload_content=False`, which returns clean raw
   bytes. Worked around by always requesting raw bytes and decoding
   manually rather than relying on the client's built-in deserialization.
4. **Incorrect remediation guidance** - the agent's first version
   recommended `kubectl set image` for a standalone Pod, which doesn't work
   (that command only applies to Deployments/ReplicaSets/etc - Pods are
   immutable once created). Fixed via an explicit system prompt instruction;
   verified the corrected guidance generalized correctly to a second,
   unrelated fault type without being re-prompted.

All four were caught by testing against a real local cluster with real
injected faults, not by reasoning about the code in the abstract.

## Setup

```bash
pip install kubernetes anthropic python-dotenv
```

Create a local `kind` cluster (requires Docker):

```bash
kind create cluster --name devops-agent-cluster
```

Create a `.env` file in the project root (never commit this - it's already
in `.gitignore`):
ANTHROPIC_API_KEY=your-key-here
## Usage

```bash
python k8s_agent/agent.py "There's a pod that seems unhealthy" default
```

Inject a test fault first to see it in action:

```bash
kubectl apply -f manifests/faults/crashloopbackoff.yaml
sleep 15  # let it actually crash at least once
python k8s_agent/agent.py "A pod is failing" default
kubectl delete -f manifests/faults/crashloopbackoff.yaml  # clean up
```

## Project structure
k8s_agent/

|-- list_pods.py       # Structured pod status: phase, scheduling, container state, fault family

|-- get_pod_logs.py     # Current + previous container logs

`-- agent.py             # Claude agentic loop wiring the above as tools
manifests/faults/

|-- imagepullbackoff.yaml       # Bad image tag

|-- crashloopbackoff.yaml        # Container exits immediately with an error

`-- pending-unschedulable.yaml    # Memory request exceeding cluster capacity
## Safety

The agent loop is capped at `MAX_ITERATIONS = 6` tool calls. This is a hard
limit, not a soft suggestion - an unbounded loop against a live cluster
risks runaway API calls and token spend if a fault type confuses the
agent's decision logic. In production, the agent's tool-using service
account should also be scoped to read-only RBAC permissions, since
diagnosis never requires write access to the cluster.

## Limitations

- Only three fault families are covered. Networking/Service misconfiguration,
  PersistentVolume issues, and resource-quota rejections are not yet handled
  and would likely need `get_events` and/or `describe_resource` tools.
- Tested against a single-node local `kind` cluster. Multi-node scheduling
  scenarios (e.g. node affinity, taints/tolerations conflicts) are not
  covered by current fault fixtures.
