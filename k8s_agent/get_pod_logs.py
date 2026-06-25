"""
get_pod_logs.py

Tool: get_pod_logs
Fetches container logs for root-cause diagnosis of crash_loop and similar
faults, where the actual reason lives in stdout/stderr rather than in pod
status fields.

Design decision: always fetch both current and previous-container logs by
default, rather than betting on one being sufficient. Verified case: for a
fully deterministic crash (same scripted failure every cycle), current and
--previous logs are identical - but that's a property of THIS test fault,
not a general guarantee. A real crash could differ run to run (e.g. a race
condition, an OOM kill, a flaky dependency), or the current container could
have only just restarted with too little log output yet. Fetching both and
letting the caller/triage layer decide which is more informative is the
safer default.

Verified bug (kubernetes client library, not our code): calling
read_namespaced_pod_log() with its default auto-deserialization returns a
str that has been corrupted - it contains the LITERAL characters b'...'
and a literal backslash-n, rather than decoded text with a real newline.
Confirmed by comparing against _preload_content=False, which returns clean
raw bytes with a real newline. The client's own deserialization step
appears to repr() the bytes instead of decoding them. Workaround: always
request _preload_content=False and decode the raw bytes ourselves.
"""

from dataclasses import dataclass

from kubernetes import client
from kubernetes.client.exceptions import ApiException


@dataclass
class PodLogs:
    pod_name: str
    container_name: str
    current: str | None         # logs from the currently running/most recent container instance
    previous: str | None        # logs from the prior instance, if the container has restarted
    previous_available: bool    # False if there was no prior instance to fetch (e.g. zero restarts)


def get_pod_logs(
    pod_name: str,
    container_name: str,
    namespace: str = "default",
    tail_lines: int = 100,
) -> PodLogs:
    v1 = client.CoreV1Api()

    current = _fetch_logs(v1, pod_name, container_name, namespace, tail_lines, previous=False)

    previous = None
    previous_available = True
    try:
        previous = _fetch_logs(v1, pod_name, container_name, namespace, tail_lines, previous=True)
    except ApiException as e:
        # 400 from the API typically means there is no previous container
        # instance to fetch logs from (e.g. the container has never restarted).
        if e.status == 400:
            previous_available = False
        else:
            raise

    return PodLogs(
        pod_name=pod_name,
        container_name=container_name,
        current=current,
        previous=previous,
        previous_available=previous_available,
    )


def _fetch_logs(v1, pod_name, container_name, namespace, tail_lines, previous) -> str:
    # _preload_content=False bypasses the client's own auto-deserialization,
    # which was verified to corrupt log content (see module docstring).
    # We decode the raw HTTP response body ourselves instead.
    response = v1.read_namespaced_pod_log(
        name=pod_name,
        namespace=namespace,
        container=container_name,
        tail_lines=tail_lines,
        previous=previous,
        _preload_content=False,
    )
    raw_bytes = response.read()
    return raw_bytes.decode("utf-8", errors="replace")


if __name__ == "__main__":
    import sys

    from kubernetes import config

    config.load_kube_config()

    pod_name = sys.argv[1] if len(sys.argv) > 1 else "broken-crash-pod"
    container_name = sys.argv[2] if len(sys.argv) > 2 else "app"

    logs = get_pod_logs(pod_name, container_name)
    print(f"=== current logs ({pod_name}/{container_name}) ===")
    print(logs.current)
    print(f"\n=== previous logs (available={logs.previous_available}) ===")
    print(logs.previous)
