"""Verify the exact node set collected by the pytest process that executes it."""

from __future__ import annotations

from pathlib import Path

import pytest


NODEID_MANIFEST = Path(__file__).with_name("test_nodeids.txt")


def manifest_difference(actual: set[str]) -> tuple[list[str], list[str]]:
    expected = {
        line.strip()
        for line in NODEID_MANIFEST.read_text().splitlines()
        if line.strip()
    }
    return sorted(expected - actual), sorted(actual - expected)


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

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"collection invariant satisfied: {len(actual)} tests")
