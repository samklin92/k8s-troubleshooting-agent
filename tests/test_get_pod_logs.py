"""
test_get_pod_logs.py

Tests get_pod_logs.py against a real crashing pod. Locks in the fix for
the verified kubernetes client deserialization bug, and the verified
containerd log-retention timing issue - logs must come back as clean
decoded text, and previous_available must accurately reflect whether
usable content was actually retrieved.
"""

import subprocess
import time
from pathlib import Path

import pytest
from kubernetes import config

from get_pod_logs import get_pod_logs

MANIFEST_DIR = Path(__file__).parent.parent / "manifests" / "faults"


@pytest.fixture(scope="session", autouse=True)
def kube_config():
    config.load_kube_config()


@pytest.fixture
def crash_loop_fault():
    subprocess.run(
        ["kubectl", "apply", "-f", str(MANIFEST_DIR / "crashloopbackoff.yaml")],
        check=True,
        capture_output=True,
    )
    time.sleep(20)
    yield "broken-crash-pod"
    subprocess.run(
        ["kubectl", "delete", "-f", str(MANIFEST_DIR / "crashloopbackoff.yaml"), "--ignore-not-found"],
        check=True,
        capture_output=True,
    )


class TestGetPodLogs:
    def test_current_logs_contain_expected_error(self, crash_loop_fault):
        logs = get_pod_logs(crash_loop_fault, "app")
        assert "missing required config file" in logs.current

    def test_logs_are_clean_decoded_text_not_byte_repr(self, crash_loop_fault):
        """
        Regression test for the verified kubernetes client bug: default
        deserialization returned a string containing the literal characters
        b'...' and a literal backslash-n, instead of decoded text with a
        real newline. This must never reappear.
        """
        logs = get_pod_logs(crash_loop_fault, "app")
        assert not logs.current.startswith("b'")
        assert not logs.current.startswith('b"')
        assert "\\n" not in logs.current  # no literal backslash-n
        assert "\n" in logs.current or len(logs.current.splitlines()) >= 1

    def test_previous_logs_consistent_with_availability_flag(self, crash_loop_fault):
        """
        Regression test for the verified containerd log-retention timing
        issue: the --previous API call can succeed but return a runtime
        error message ("unable to retrieve container logs for...") instead
        of real content, if the previous container's logs were already
        garbage collected. This is timing-dependent and not always
        reproducible - the contract we actually guarantee is internal
        consistency, not that previous logs are always available.
        """
        logs = get_pod_logs(crash_loop_fault, "app")

        if logs.previous_available:
            assert logs.previous is not None
            assert not logs.previous.startswith("unable to retrieve container logs for")
        else:
            assert logs.previous is None
