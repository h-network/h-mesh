"""Verify the exact node set collected by the pytest process that executes it."""

from __future__ import annotations

from pathlib import Path

import pytest


NODEID_MANIFEST = Path(__file__).with_name("test_nodeids.txt")
_terminal_nodeids: set[str] = set()


def manifest_difference(actual: set[str]) -> tuple[list[str], list[str]]:
    expected = {
        line.strip()
        for line in NODEID_MANIFEST.read_text().splitlines()
        if line.strip()
    }
    return sorted(expected - actual), sorted(actual - expected)


def pytest_configure(config: pytest.Config) -> None:
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
        _terminal_nodeids.add(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus != 0:
        return

    expected = {item.nodeid for item in session.items}
    incomplete = sorted(expected - _terminal_nodeids)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if incomplete:
        session.exitstatus = 1
        if reporter is not None:
            reporter.write_sep("=", "suite execution invariant failed", red=True)
            reporter.write_line(
                "successful pytest session lacked terminal outcomes for:", red=True
            )
            for nodeid in incomplete:
                reporter.write_line(f"  not executed: {nodeid}", red=True)
        return

    if reporter is not None:
        reporter.write_line(
            f"suite execution invariant satisfied: "
            f"{len(_terminal_nodeids)} terminal outcomes"
        )
