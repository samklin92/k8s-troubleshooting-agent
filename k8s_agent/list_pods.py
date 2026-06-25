"""
list_pods.py

Tool: list_pods
Returns structured status for pods in a namespace - fast, cheap, no log
fetching required. This is the agent's first move when investigating a
failure: figure out which pod(s) and what kind of failure, before deciding
whether deeper investigation (logs, describe) is needed.

Verified field shape: container_statuses[i].state.waiting.reason is the
structured classification (e.g. "ImagePullBackOff", "CrashLoopBackOff").
state.waiting.message gives a short summary but not always the full root
cause - for image pull failures specifically, get_pod_logs/get_events may
still be needed for full detail.

Verified behavior: Kubernetes cycles a failing container through multiple
transient reasons during its retry/backoff loop (e.g. ImagePullBackOff and
ErrImagePull both occur for the same underlying image-pull failure,
alternating depending on which instant you poll). FAULT_FAMILIES normalizes
these into stable categories so detection doesn't depend on polling timing.

Verified behavior: a pod stuck in CrashLoopBackOff reports phase=Running,
not Pending or Failed - Kubernetes considers the pod "running" during the
brief windows between crashes. is_healthy therefore checks container state
reasons, not phase alone.

Verified behavior: for scheduling failures (pod never assigned a node), the
PodScheduled condition's own `message` field already contains the full
root cause (e.g. "0/1 nodes are available: 1 Insufficient memory") - the
same text that appears in the FailedScheduling event. This means scheduling
failures can be fully diagnosed from this one structured field, without a
separate get_events call, unlike image-pull failures where the condition/
state message is often a short summary needing get_events for full detail.
"""

from dataclasses import dataclass, field

from kubernetes import client


# Kubernetes cycles a failing container through multiple transient reasons
# during its retry/backoff loop. Treating these as separate fault types
# would make detection non-deterministic - the same underlying problem
# could be classified differently depending on which instant you poll.
# This map normalizes each known reason to a stable fault family.
FAULT_FAMILIES = {
    "ImagePullBackOff": "image_pull_failure",
    "ErrImagePull": "image_pull_failure",
    "CrashLoopBackOff": "crash_loop",
    "Error": "crash_loop",
}


def fault_family(state_reason: str | None) -> str | None:
    """Normalize a raw container state reason into a stable fault family."""
    if state_reason is None:
        return None
    return FAULT_FAMILIES.get(state_reason, "unknown")


@dataclass
class ContainerStatus:
    name: str
    ready: bool
    restart_count: int
    state_reason: str | None         # e.g. "Running", "ImagePullBackOff", "CrashLoopBackOff"
    state_message: str | None        # short message if waiting/terminated, None if running
    fault_family: str | None = None  # normalized fault category, e.g. "image_pull_failure"


@dataclass
class SchedulingStatus:
    scheduled: bool
    reason: str | None      # e.g. "Unschedulable", None if scheduled successfully
    message: str | None     # full root cause, e.g. "0/1 nodes are available: 1 Insufficient memory..."


@dataclass
class PodSummary:
    name: str
    namespace: str
    phase: str                                   # Pending / Running / Succeeded / Failed / Unknown
    node_name: str | None                          # None if never scheduled to a node
    scheduling: SchedulingStatus | None = None
    containers: list[ContainerStatus] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        if self.scheduling is not None and not self.scheduling.scheduled:
            return False
        if self.phase not in ("Running", "Succeeded"):
            return False
        return all(c.state_reason in (None, "Running") for c in self.containers)


def list_pods(namespace: str = "default", label_selector: str | None = None) -> list[PodSummary]:
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

    summaries = []
    for pod in pods.items:
        containers = []
        for cs in pod.status.container_statuses or []:
            reason, message = _extract_state(cs.state)
            containers.append(
                ContainerStatus(
                    name=cs.name,
                    ready=cs.ready,
                    restart_count=cs.restart_count,
                    state_reason=reason,
                    state_message=message,
                    fault_family=fault_family(reason),
                )
            )

        summaries.append(
            PodSummary(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                phase=pod.status.phase,
                node_name=pod.spec.node_name,
                scheduling=_extract_scheduling(pod.status.conditions),
                containers=containers,
            )
        )

    return summaries


def _extract_state(state) -> tuple[str | None, str | None]:
    """
    A container's state is exactly one of running/waiting/terminated.
    Running has no reason (it's healthy); waiting/terminated both carry
    a `reason` (e.g. ImagePullBackOff, CrashLoopBackOff, Completed, Error)
    and an optional `message`.
    """
    if state.running is not None:
        return "Running", None
    if state.waiting is not None:
        return state.waiting.reason, state.waiting.message
    if state.terminated is not None:
        return state.terminated.reason, state.terminated.message
    return None, None


def _extract_scheduling(conditions) -> SchedulingStatus | None:
    """
    Finds the PodScheduled condition and reports whether scheduling
    succeeded. When it fails (status="False"), the condition's own
    `message` field carries the full root cause - verified against a
    real Insufficient-memory scheduling failure, where this message
    matched the FailedScheduling event text exactly.
    """
    if not conditions:
        return None

    for c in conditions:
        if c.type == "PodScheduled":
            scheduled = c.status == "True"
            return SchedulingStatus(
                scheduled=scheduled,
                reason=c.reason,
                message=c.message,
            )
    return None


if __name__ == "__main__":
    from kubernetes import config

    config.load_kube_config()

    for pod in list_pods():
        print(f"{pod.name} [{pod.namespace}] phase={pod.phase} node={pod.node_name} healthy={pod.is_healthy}")
        if pod.scheduling and not pod.scheduling.scheduled:
            print(f"  SCHEDULING FAILED: reason={pod.scheduling.reason!r}")
            print(f"    message={pod.scheduling.message!r}")
        for c in pod.containers:
            print(
                f"  {c.name}: reason={c.state_reason!r} family={c.fault_family!r} "
                f"message={c.state_message!r} restarts={c.restart_count}"
            )
