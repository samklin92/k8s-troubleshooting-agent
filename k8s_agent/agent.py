"""
agent.py

The agentic investigation loop. Claude receives an initial symptom (e.g.
"pod X is unhealthy in namespace Y"), decides which tool to call to gather
more information, evaluates the result, and either calls another tool or
concludes with a root-cause diagnosis and recommended fix.

Tools wired in this version: list_pods, get_pod_logs - both independently
verified against live fault injections (ImagePullBackOff/ErrImagePull
cycling, CrashLoopBackOff, and scheduling failures) before being exposed
to the agent. get_events and describe_resource are deliberately not
included yet; testing showed list_pods + get_pod_logs already provide a
full root cause for all three fault families built so far.

Safety: MAX_ITERATIONS bounds the tool-calling loop. Without this, a
malformed prompt or an unexpected tool-call pattern could loop indefinitely
against a live cluster, burning API calls and Anthropic tokens with no
upper bound. This is a hard cap, not a soft suggestion - the loop stops
and reports inconclusive after MAX_ITERATIONS regardless of whether Claude
asks for more.

Observability: every investigation is instrumented with Prometheus metrics
(outcome, duration, iteration count, tool calls made, fault families seen).
See metrics.py for the metric definitions and the rationale behind each one.
"""

import json
import os
import time

import anthropic
from dotenv import load_dotenv

from get_pod_logs import get_pod_logs
from list_pods import list_pods
from metrics import (
    fault_family_total,
    investigation_duration_seconds,
    investigation_iterations,
    investigations_total,
    tool_calls_total,
)

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 6

SYSTEM_PROMPT = """You are a Kubernetes troubleshooting agent. You investigate pod failures \
by calling tools to gather evidence, then state a root cause and a recommended fix.

Process:
1. Call list_pods first to see overall pod health and any structured failure signals.
2. If a pod shows a crash_loop fault family, call get_pod_logs to find the actual \
error message - the reason field alone ("CrashLoopBackOff" or "Error") never explains \
WHY it crashed.
3. If a pod shows image_pull_failure or a scheduling failure, the message field from \
list_pods is usually sufficient on its own - only call get_pod_logs if you genuinely \
need more detail.
4. Once you have enough evidence, stop calling tools and respond with your diagnosis.

Do not call a tool you don't need. Do not call the same tool with the same arguments \
twice - if you already have the data, use it.

When you conclude, structure your final answer as:
- Root cause: one sentence, specific (not "the pod is unhealthy" - name the actual cause)
- Evidence: what you observed that supports this
- Recommended fix: a concrete, actionable step

Note on remediation commands: `kubectl set image` only works on objects with a pod \
template (Deployment, ReplicaSet, StatefulSet, DaemonSet, Job) - it does NOT work on a \
standalone Pod, since Pods are immutable once created. If you don't know whether the \
pod is managed by a higher-level controller, say so explicitly and give the correct \
command for a standalone pod (delete and recreate with a corrected manifest) as the \
primary suggestion, not an afterthought.
"""

TOOL_DEFINITIONS = [
    {
        "name": "list_pods",
        "description": (
            "Returns structured status for all pods in a namespace: phase, scheduling "
            "status, container state reasons, restart counts, and normalized fault "
            "families. This is the first tool to call when investigating any failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace to query. Defaults to 'default'.",
                },
            },
        },
    },
    {
        "name": "get_pod_logs",
        "description": (
            "Fetches container logs (both current and previous instance, if the "
            "container has restarted) for a specific pod and container. Use this when "
            "a pod's fault_family is 'crash_loop' and you need to know WHY it crashed - "
            "the actual error is in the logs, not in the pod status fields."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string", "description": "Name of the pod"},
                "container_name": {"type": "string", "description": "Name of the container within the pod"},
                "namespace": {"type": "string", "description": "Kubernetes namespace. Defaults to 'default'."},
            },
            "required": ["pod_name", "container_name"],
        },
    },
]


def run_investigation(symptom: str, namespace: str = "default") -> str:
    """
    Runs a full investigation and records duration as a metric. Outcome,
    iteration count, and fault-family metrics are recorded inside _run_loop
    itself, at the point each is actually known (the diagnosed-return or
    inconclusive-return paths) - not here, so a failure partway through a
    multi-metric update can't leave metrics in a half-recorded state.
    """
    start_time = time.monotonic()
    seen_fault_families = set()

    try:
        result, _ = _run_loop(symptom, namespace, seen_fault_families)
        return result
    finally:
        duration = time.monotonic() - start_time
        investigation_duration_seconds.observe(duration)


def _run_loop(symptom: str, namespace: str, seen_fault_families: set) -> tuple[str, int]:
    client_ = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [{"role": "user", "content": f"{symptom} (namespace: {namespace})"}]

    for iteration in range(MAX_ITERATIONS):
        response = client_.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            result_text = _extract_text(response)
            investigations_total.labels(outcome="diagnosed").inc()
            investigation_iterations.observe(iteration + 1)
            for family in seen_fault_families:
                fault_family_total.labels(fault_family=family).inc()
            return result_text, iteration + 1

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_calls_total.labels(tool_name=block.name).inc()
            result = _execute_tool(block.name, block.input)

            if block.name == "list_pods":
                for pod in result:
                    for c in pod.get("containers", []):
                        if c.get("fault_family"):
                            seen_fault_families.add(c["fault_family"])

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    investigations_total.labels(outcome="inconclusive").inc()
    investigation_iterations.observe(MAX_ITERATIONS)
    for family in seen_fault_families:
        fault_family_total.labels(fault_family=family).inc()

    inconclusive_message = (
        f"INCONCLUSIVE: investigation did not reach a diagnosis within {MAX_ITERATIONS} "
        f"tool-call iterations. This is a safety cap, not a sign the issue is unsolvable - "
        f"it likely means the agent needs an additional tool (e.g. get_events) for this "
        f"fault type, or got stuck re-requesting the same information."
    )
    return inconclusive_message, MAX_ITERATIONS


def _execute_tool(name: str, input_data: dict):
    if name == "list_pods":
        namespace = input_data.get("namespace", "default")
        pods = list_pods(namespace=namespace)
        return [_pod_to_dict(p) for p in pods]

    if name == "get_pod_logs":
        return _logs_to_dict(
            get_pod_logs(
                pod_name=input_data["pod_name"],
                container_name=input_data["container_name"],
                namespace=input_data.get("namespace", "default"),
            )
        )

    raise ValueError(f"Unknown tool requested by agent: {name}")


def _pod_to_dict(pod) -> dict:
    return {
        "name": pod.name,
        "namespace": pod.namespace,
        "phase": pod.phase,
        "node_name": pod.node_name,
        "is_healthy": pod.is_healthy,
        "scheduling": (
            {
                "scheduled": pod.scheduling.scheduled,
                "reason": pod.scheduling.reason,
                "message": pod.scheduling.message,
            }
            if pod.scheduling
            else None
        ),
        "containers": [
            {
                "name": c.name,
                "ready": c.ready,
                "restart_count": c.restart_count,
                "state_reason": c.state_reason,
                "state_message": c.state_message,
                "fault_family": c.fault_family,
            }
            for c in pod.containers
        ],
    }


def _logs_to_dict(logs) -> dict:
    return {
        "pod_name": logs.pod_name,
        "container_name": logs.container_name,
        "current": logs.current,
        "previous": logs.previous,
        "previous_available": logs.previous_available,
    }


def _extract_text(response) -> str:
    return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    import sys

    from kubernetes import config

    config.load_kube_config()

    symptom = sys.argv[1] if len(sys.argv) > 1 else "Pods in the cluster appear unhealthy. Investigate."
    namespace = sys.argv[2] if len(sys.argv) > 2 else "default"

    print(f"=== Investigating: {symptom} ===\n")
    result = run_investigation(symptom, namespace)
    print(result)
