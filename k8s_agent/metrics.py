"""
metrics.py

Prometheus metrics for the Kubernetes troubleshooting agent. Kept in a
separate module from agent.py so the metric definitions can be imported
and inspected independently (e.g. in tests) without needing to run a full
investigation.

Metric design rationale:
- investigations_total: outcome breakdown (diagnosed vs inconclusive) is
  the single most important signal for "is this tool actually working" -
  a rising inconclusive rate means a fault type has appeared that the
  agent's current tools/prompt can't handle.
- tool_calls_total: labeled by tool name, this proves (or disproves) the
  "agent calls only the tools it needs" design claim with real numbers,
  not just anecdote from a few manual test runs.
- investigation_duration_seconds: a histogram, not a gauge, because we
  care about the distribution (p50/p95), not just the latest value -
  useful for noticing if a particular fault family is consistently slow
  to diagnose.
- fault_family_total: labeled by fault_family, shows which fault types
  are actually being seen in practice, which is useful operational
  intelligence even independent of the AI layer.
"""

from prometheus_client import Counter, Histogram

investigations_total = Counter(
    "k8s_agent_investigations_total",
    "Total investigations run, labeled by outcome",
    ["outcome"],  # "diagnosed" or "inconclusive"
)

tool_calls_total = Counter(
    "k8s_agent_tool_calls_total",
    "Total tool calls made by the agent during investigations, labeled by tool name",
    ["tool_name"],
)

investigation_duration_seconds = Histogram(
    "k8s_agent_investigation_duration_seconds",
    "Wall-clock time for a full investigation, from symptom to diagnosis or inconclusive result",
)

investigation_iterations = Histogram(
    "k8s_agent_investigation_iterations",
    "Number of tool-calling loop iterations used per investigation",
    buckets=[1, 2, 3, 4, 5, 6],
)

fault_family_total = Counter(
    "k8s_agent_fault_family_total",
    "Fault families observed during investigations, labeled by fault_family",
    ["fault_family"],
)
