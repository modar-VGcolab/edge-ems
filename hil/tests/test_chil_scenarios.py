"""The seven scenarios + three fault injections, plant-in-the-loop, with the
exact tests/sil/scenarios.py checks as oracle (tuned gains from configs)."""
import pytest

from hil import orchestrate_chil as orch


@pytest.mark.parametrize("result", orch.run_all(), ids=lambda r: r.name)
def test_scenario_passes(result):
    assert result.passed, result.error


@pytest.mark.parametrize("fault", orch.run_faults(), ids=lambda f: f.name)
def test_fault_injection_passes(fault):
    assert fault.passed, fault.detail
