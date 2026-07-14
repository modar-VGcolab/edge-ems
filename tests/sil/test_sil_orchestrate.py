"""Unit tests for SIL action planning (run everywhere, no infra)."""

from orchestrate import FakeExecutor, build_actions, run_scenario
from scenarios import SCENARIOS, by_name


def _verbs(scenario):
    return [a[0] for a in build_actions(scenario)]


def test_every_scenario_starts_loop_and_sleeps():
    for s in SCENARIOS:
        verbs = _verbs(s)
        assert "loop_start" in verbs
        assert "sleep" in verbs
        assert verbs[0] == "ems_droop"  # droop state set before starting


def test_droop_scenario_enables_droop():
    actions = build_actions(by_name("droop"))
    assert ("ems_droop", True) in actions


def test_non_droop_scenarios_disable_droop():
    for name in ("tracking", "curtailment", "stale_data"):
        assert ("ems_droop", False) in build_actions(by_name(name))


def test_stale_data_pauses_and_resumes_core():
    verbs = _verbs(by_name("stale_data"))
    assert "pause" in verbs and "unpause" in verbs
    actions = build_actions(by_name("stale_data"))
    assert ("pause", "core") in actions
    assert ("unpause", "core") in actions
    # pause must come before unpause
    assert verbs.index("pause") < verbs.index("unpause")


def test_config_reload_issues_reload_between_sleeps():
    actions = build_actions(by_name("config_reload"))
    verbs = [a[0] for a in actions]
    assert "reload" in verbs
    assert verbs.count("sleep") >= 2  # run, reload, keep running
    reload_idx = verbs.index("reload")
    assert "sleep" in verbs[:reload_idx] and "sleep" in verbs[reload_idx + 1 :]


def test_total_sleep_matches_duration_for_simple_scenario():
    s = by_name("tracking")
    total = sum(a[1] for a in build_actions(s) if a[0] == "sleep")
    assert total == s.duration_s


def test_fake_executor_records_actions():
    ex = FakeExecutor()
    run_scenario(by_name("tracking"), ex)
    assert ex.verbs[0] == "ems_droop"
    assert "loop_start" in ex.verbs


def test_reload_path_is_carried_through():
    actions = build_actions(by_name("config_reload"))
    reload = next(a for a in actions if a[0] == "reload")
    assert reload[1].endswith(".yaml")


def test_config_reload_targets_sil_site_not_real_ips():
    actions = build_actions(by_name("config_reload"))
    reload = next(a for a in actions if a[0] == "reload")
    assert reload[1] == "configs/asset_config.sil.yaml"


def test_sim_profile_scenarios_recreate_sims_before_loop():
    for name in ("tracking", "curtailment", "droop"):
        verbs = [a[0] for a in build_actions(by_name(name))]
        assert "sim_profiles" in verbs and "await_steady" in verbs
        # ems_droop first, then stimulus, then the loop starts
        assert verbs.index("ems_droop") < verbs.index("sim_profiles") < verbs.index("loop_start")


def test_curtailment_drives_grid_and_bess():
    actions = build_actions(by_name("curtailment"))
    profiles = next(a for a in actions if a[0] == "sim_profiles")[1]
    assert profiles == {"sim-grid": "export_over_feed.csv", "sim-bess": "high_soc.csv"}


def test_await_steady_excluded_from_window_sleep():
    # the settle after recreating sims must not count toward the scenario window
    s = by_name("tracking")
    total = sum(a[1] for a in build_actions(s) if a[0] == "sleep")
    assert total == s.duration_s
