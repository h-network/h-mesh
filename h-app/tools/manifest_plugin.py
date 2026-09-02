"""Verify the exact node set collected by the pytest process that executes it."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest


NODEID_MANIFEST = Path(__file__).with_name("test_nodeids.txt")
_terminal_nodeids: set[str] = set()
_skipped_nodeids: dict[str, str] = {}


def manifest_difference(actual: set[str]) -> tuple[list[str], list[str]]:
    expected = {
        line.strip()
        for line in NODEID_MANIFEST.read_text().splitlines()
        if line.strip()
    }
    return sorted(expected - actual), sorted(actual - expected)


def pytest_configure(config: pytest.Config) -> None:
    if not os.environ.get("H_MESH_TEST_ATTESTATION_PATH") or not os.environ.get(
        "H_MESH_TEST_ATTESTATION_NONCE"
    ):
        raise pytest.UsageError(
            "manifest plugin requires a runner-created attestation path and nonce"
        )
    zero_runtest_modes = [
        name
        for name in ("collectonly", "setuponly", "setupplan")
        if getattr(config.option, name, False)
    ]
    if zero_runtest_modes:
        raise pytest.UsageError(
            "complete-suite execution refuses zero-runtest mode(s): "
            + ", ".join(zero_runtest_modes)
        )


def pytest_sessionstart() -> None:
    _terminal_nodeids.clear()
    _skipped_nodeids.clear()


def pytest_collection_finish(session: pytest.Session) -> None:
    actual = {item.nodeid for item in session.items}
    missing, added = manifest_difference(actual)
    if missing or added:
        details = ["collected tests differ from the reviewed node-id manifest"]
        details.extend(f"  missing: {nodeid}" for nodeid in missing)
        details.extend(f"  added: {nodeid}" for nodeid in added)
        details.append(
            "Missing entries mean reviewed tests did not execute; added entries "
            "mean new or renamed tests need review. Do not regenerate the manifest "
            "until each difference is explained. See docs/ci.md."
        )
        pytest.exit("\n".join(details), returncode=1)



def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        if report.skipped:
            # report.wasxfail identifies expected-failure call skips if a future
            # concrete use case gives them deliberately different semantics.
            # Today every skipped call is non-certifying.
            longrepr = report.longrepr
            reason = str(longrepr[2] if isinstance(longrepr, tuple) else longrepr)
            _skipped_nodeids[report.nodeid] = reason
        else:
            _terminal_nodeids.add(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus != 0:
        return

    expected = {item.nodeid for item in session.items}
    incomplete = sorted(expected - _terminal_nodeids)
    skipped = sorted(expected & _skipped_nodeids.keys())
    no_call_report = sorted(set(incomplete) - set(skipped))
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if incomplete:
        session.exitstatus = 1
        if reporter is not None:
            reporter.write_sep("=", "suite execution invariant failed", red=True)
            reporter.write_line("suite cannot certify these nodes:", red=True)
            for nodeid in skipped:
                reporter.write_line(
                    f"  not executed: skipped: {nodeid} ({_skipped_nodeids[nodeid]})",
                    red=True,
                )
            for nodeid in no_call_report:
                reporter.write_line(
                    f"  not executed: no call-phase report: {nodeid}", red=True
                )
        return

    attestation_path = Path(os.environ["H_MESH_TEST_ATTESTATION_PATH"])
    attestation = {
        "version": 1,
        "nonce": os.environ["H_MESH_TEST_ATTESTATION_NONCE"],
        "manifest_sha256": hashlib.sha256(NODEID_MANIFEST.read_bytes()).hexdigest(),
        "terminal_count": len(_terminal_nodeids),
    }
    temporary_path = attestation_path.with_name(
        f".{attestation_path.name}.{os.getpid()}.tmp"
    )
    temporary_path.write_text(json.dumps(attestation, sort_keys=True) + "\n")
    os.replace(temporary_path, attestation_path)
