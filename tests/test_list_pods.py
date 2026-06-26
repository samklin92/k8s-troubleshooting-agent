"""
test_list_pods.py

Tests list_pods.py against a real kind cluster with real fault-injection
manifests - the same verification we did manually during development,
now codified so it runs automatically on every push.

These tests require a running kind cluster (kubeconfig already configured)
and apply/delete real fault manifests as part of each test. They are
integration tests, not unit tests with mocks - the whole point of this
suite is to catch the kind of real-world surprises (fault-state cycling,
phase=Running during CrashLoopBackOff) that mocks would hide.
"""

import subprocess
import time
from pathlib import Path

import pytest
from kubernetes import config

from list_pods import list_pods

MANIFEST_DIR = Path(__file__).parent.parent / "manifests" / "faults"


def _apply(manifest_name: str):
    subprocess.run(
        ["kubectl", "apply", "-f", str(MANIFEST_DIR / manifest_name)],
        check=True,
        capture_output=True,
    )


def _delete(manifest_name: str):
    subprocess.run(
        ["kubectl", "delete", "-f", str(MANIFEST_DIR / manifest_name), "--ignore-not-found"],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session", autouse=True)
def kube_config():
    config.load_kube_config()


@pytest.fixture
def image_pull_fault():
    _apply("imagepullbackoff.yaml")
    time.sleep(8)  # let the pod reach a stable failure state
    yield "broken-image-pod"
    _delete("imagepullbackoff.yaml")


@pytest.fixture
def crash_loop_fault():
    _apply("crashloopbackoff.yaml")
    time.sleep(20)  # let it crash and restart at least once
    yield "broken-crash-pod"
    _delete("crashloopbackoff.yaml")


@pytest.fixture
def scheduling_fault():
    _apply("pending-unschedulable.yaml")
    time.sleep(5)
    yield "unschedulable-pod"
    _delete("pending-unschedulable.yaml")


def _find_pod(pods, name):
    matches = [p for p in pods if p.name == name]
    assert matches, f"Pod {name!r} not found in list_pods() output"
    return matches[0]


class TestImagePullFailure:
    def test_detected_as_unhealthy(self, image_pull_fault):
        pod = _find_pod(list_pods(), image_pull_fault)
        assert not pod.is_healthy

    def test_fault_family_normalizes_across_cycling(self, image_pull_fault):
        """
        Verified manually: the same fault alternates between ImagePullBackOff
        and ErrImagePull depending on poll timing. Regardless of which raw
        reason is showing at any given instant, fault_family must be stable.
        """
        for _ in range(5):
            pod = _find_pod(list_pods(), image_pull_fault)
            app_container = next(c for c in pod.containers if c.name == "app")
            assert app_container.fault_family == "image_pull_failure", (
                f"Expected stable fault_family despite reason cycling, got "
                f"reason={app_container.state_reason!r} family={app_container.fault_family!r}"
            )
            time.sleep(2)


class TestCrashLoop:
    def test_detected_as_unhealthy(self, crash_loop_fault):
        pod = _find_pod(list_pods(), crash_loop_fault)
        assert not pod.is_healthy

    def test_phase_is_running_not_pending_or_failed(self, crash_loop_fault):
        """
        Verified manually: Kubernetes reports phase=Running during the brief
        windows between crashes, even though the pod is clearly unhealthy.
        This locks in that is_healthy must not rely on phase alone.
        """
        pod = _find_pod(list_pods(), crash_loop_fault)
        assert pod.phase == "Running"
        assert not pod.is_healthy  # must still be False despite phase=Running

    def test_fault_family_is_crash_loop(self, crash_loop_fault):
        pod = _find_pod(list_pods(), crash_loop_fault)
        app_container = next(c for c in pod.containers if c.name == "app")
        assert app_container.fault_family == "crash_loop"

    def test_restart_count_increases(self, crash_loop_fault):
        pod = _find_pod(list_pods(), crash_loop_fault)
        app_container = next(c for c in pod.containers if c.name == "app")
        assert app_container.restart_count >= 1


class TestSchedulingFailure:
    def test_detected_as_unhealthy(self, scheduling_fault):
        pod = _find_pod(list_pods(), scheduling_fault)
        assert not pod.is_healthy

    def test_node_name_is_none(self, scheduling_fault):
        pod = _find_pod(list_pods(), scheduling_fault)
        assert pod.node_name is None

    def test_scheduling_message_contains_root_cause(self, scheduling_fault):
        """
        Verified manually: the PodScheduled condition's own message field
        already contains the full root cause (e.g. "Insufficient memory"),
        no separate get_events call needed for this fault family.
        """
        pod = _find_pod(list_pods(), scheduling_fault)
        assert pod.scheduling is not None
        assert pod.scheduling.scheduled is False
        assert "insufficient" in pod.scheduling.message.lower()
