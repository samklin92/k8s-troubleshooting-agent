"""
list_pods.py

Tool: list_pods
Returns structured status for pods in a namespace - fast, cheap, no log or
event fetching. This is the agent's first move when investigating a failure:
figure out which pod(s) and what kind of failure, before deciding whether
deeper investigation (events, logs, describe) is needed.

Verified field shape: container_statuses[i].state.waiting.reason is the
structured classification (e.g. "ImagePullBackOff", "CrashLoopBackOff").
state.waiting.message gives a short summary but NOT the full root cause -
for that, the agent needs to separately call get_events.

Verified behavior: Kubernetes cycles a failing container through multiple
transient reasons during its retry/backoff loop (e.g. ImagePullBackOff and
ErrImagePull both occur for the same underlying image-pull failure,
alternating depending on which instant you poll). FAULT_FAMILIES normalizes
these into stable categories so detection doesn't depend on polling timing.
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
class PodSummary:
    name: str
    namespace: str
    phase: str                                  # Pending / Running / Succeeded / Failed / Unknown
    containers: list[ContainerStatus] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
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


if __name__ == "__main__":
    from kubernetes import config

    config.load_kube_config()

    for pod in list_pods():
        print(f"{pod.name} [{pod.namespace}] phase={pod.phase} healthy={pod.is_healthy}")
        for c in pod.containers:
            print(
                f"  {c.name}: reason={c.state_reason!r} family={c.fault_family!r} "
                f"message={c.state_message!r} restarts={c.restart_count}"
            )
