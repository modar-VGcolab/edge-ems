# Known issues

Open issues found while bringing the SIL stack (Gate G2) to green on 2026-06-19.
Both are **controller-code** changes, so per project rule #3 ("no controller code
changes to make SIL/CHIL pass") they were left out of the SIL fix and are tracked
here as follow-ups. The SIL suite passes today by working around them in the test
harness / SIL config; the items below are about the real `services` deployment.

---

## 1. Config `${VAR}` placeholders are never expanded

**Severity:** high (blocks the real `services` profile against a fresh InfluxDB).

**Status: RESOLVED (2026-06-22).** Both loaders now expand environment variables:
`core.main._load` and `controller.config_manager._read_raw` run `yaml.safe_load(os.path.expandvars(text))`, so `token: "${INFLUX_TOKEN}"` resolves
from the environment for the **live** config the services connect with. The controller
keeps the *editable/persisted* document verbatim (`ConfigManager.raw()` reads without
expansion and `update()` rebuilds the live config from disk after writing), so a PUT
round-trip never leaks or persists the resolved secret. Regression tests:
`tests/contract/test_config_env_expansion.py`. The compose `*_CONFIG_PATH` env vars are
now overridable so the `services` profile can use the example EMS config with a real
`INFLUX_TOKEN` (see `configs/.env.example`).

**Symptom.** `core` crash-loops with:

```
influxdb_client.rest.ApiException: (401) Unauthorized
{"code":"unauthorized","message":"unauthorized access"}
```

**Root cause.** `configs/edge_ems_config.example.yaml` carries the secret as a
placeholder:

```yaml
influxdb:
  token: "${INFLUX_TOKEN}"
```

but nothing expands it. Both config loaders just parse the YAML verbatim:

- `packages/core/src/core/main.py` → `_load()` = `yaml.safe_load(Path(path).read_text(...))`
- `packages/controller/src/controller/config_manager.py` → `_read_raw()` = same

and `common.config_models` declares `token: str`. So the controller/core send the
literal 14-character string `${INFLUX_TOKEN}` as the auth token and InfluxDB
rejects it (401). The SIL stack avoids this with `configs/edge_ems_config.sil.yaml`,
which hardcodes the local dev token (`change-me`).

**Fix.** Expand environment variables when loading config, so `${VAR}` works as the
example file already implies. Expand the raw text before parsing, in *both* loaders:

```python
import os

def _load(path):
    raw = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(os.path.expandvars(raw))   # ${INFLUX_TOKEN} -> env value
```

```python
# config_manager.ConfigManager._read_raw
def _read_raw(self, which: str) -> dict:
    raw = self._paths[which].read_text(encoding="utf-8")
    return yaml.safe_load(os.path.expandvars(raw))
```

Notes:
- `os.path.expandvars` leaves an unknown `${VAR}` unchanged; consider failing loudly
  if a required secret is still a `${...}` placeholder after expansion.
- The controller persists config on `PUT` (`config_manager.update`). Decide whether
  the on-disk file should keep the `${VAR}` placeholder (expand only in memory) or
  store the resolved value; expanding only at read time keeps secrets out of the file.

**Regression test.** Add a unit test that loads a config whose `token` is
`${INFLUX_TOKEN}` with the env var set, and asserts the resolved model carries the
expanded value (no infra needed).

**After fixing**, the `services` profile can drop `configs/edge_ems_config.sil.yaml`
and use the example config with a real `INFLUX_TOKEN` in `configs/.env`.

---

## 2. Hot config reload is not applied to the running control loop

**Severity:** medium (operability; the API reports success but nothing changes).

**Partial update (2026-06-30).** A *targeted* live-update path now exists for the
PCC setpoint: `POST /setpoint` calls `LoopRunner.set_pcc_setpoint_kw`, which mutates
the **running** `ControlLoop.pcc_setpoint_kw` between cycles (no restart), and the
applied value is read back in `GET /status.last_cycle.pcc_setpoint_kw`. This is what
the external-EMS gateway (S1, `deploy/ext-ems/`) uses to drive the controller live.
It is the "rebuild-and-swap on the running loop" idea (option 1 below) applied to one
field only — the **general** issue still stands: `PUT /config/ems` / `PUT /config/assets`
changes (e.g. `Kp`, limits, droop) are still not re-read by the running loop.

**Symptom.** `PUT /config/assets` and `PUT /config/ems` return `{"applied": true}`
and update the in-memory `ConfigManager`, but the live control loop keeps running
with the configuration it had at start-up. The `config_reload` SIL scenario
therefore only proves the loop *stays in RUN*, not that a reload is actually applied.

**Root cause.** `packages/controller/src/controller/main.py` → `build_runner()`
builds the `ControlLoop` once and captures the config-derived objects
(`EdgeController` limits, `pcc_base_kw`, `max_feed_kw`, `DroopController`,
`ModeController`) at that moment. `ConfigManager.update()` swaps `self._assets` /
`self._ems`, but no one rebuilds or re-reads them into the running `LoopRunner`.
There is no subscription/callback wiring config changes to the loop.

**Fix.** Make the loop consume config changes at a cycle boundary. Two options:

1. **Rebuild-and-swap (simplest).** Give `LoopRunner` a thread-safe
   `apply_config(cm)` that rebuilds the config-derived pieces and swaps them in
   between cycles (preserving rolling `ControlState`: integral, last setpoint, mode).
   Have the `PUT` handlers call it after `cm.update(...)`.

2. **Pull on each cycle.** Have `ControlLoop.run_once` read its tunables/limits from
   the `ConfigManager` (or a shared, atomically-replaced config snapshot) at the top
   of each cycle, instead of from values captured in `__init__`.

Either way, decide the state-carry policy on reload (keep the PI integrator and mode,
or reset them) and document it; a structural change already requires the loop stopped
(`http_api._structural_change` → 409).

**Regression test (per rule #3, at the lowest level).** Unit-test the chosen
mechanism without infra: build a loop, apply a config that changes a limit (e.g.
`max_feed_kw` or `Kp`), and assert the next `run_once` reflects the new value while
mode/state carry across as intended.

**After fixing**, strengthen the `config_reload` SIL scenario to assert the change
actually took effect (e.g. reload a config with a different `Kp`/feed limit and check
the `control` series responds), not just that the loop stayed in RUN.

---

## 3. SunSpec maps not yet reconciled against as-built firmware (G3 blocker)

**Severity:** high (blocks Gate G3 rig sign-off against real devices).

**Status:** OPEN — reconciliation **tooling delivered and tested**; awaiting a
real device or saved firmware dump to reconcile against.

**Symptom.** `maps/*.yaml` load, pass the contract tests, and `hil.parity_check`
is green, but their addresses, types and scale factors were *chosen to be
internally consistent* (`SF_VALUES` in `maps/sunspec/generate.py`), not measured
on hardware. Because the HIL servers and the controller read the same map files,
a wrong address/type/scale is **invisible in SIL/CHIL** and only breaks on the
real inverter.

**Tooling (done).** Walk a live device or a saved raw register dump, record the
device's real `sunssf` values + model placement, and diff against each map:

```bash
python scripts/read_real_inverter.py discover --host <ip> --map custom_bess_v1.yaml \
    --out reconciliation/<asset>.dump.json --diff-json reconciliation/<asset>.diff.json
```

- `scripts/sunspec_discovery.py` — SunSpec walker (live / raw-dump / self-dump).
- `scripts/diff_dump_vs_map.py` — dump-vs-map discrepancy report.
- `tests/tools/test_sunspec_discovery.py` — walker + diff tests on synthetic dumps.
- `reconciliation/` — usage notes and (synthetic) sample artifacts.

**To close.** For each asset class: capture a real dump, run the diff, reconcile
any discrepancy by editing **`maps/sunspec/generate.py`** (real SF in `SF_VALUES`,
real model placement/lengths — never hand-edit the emitted YAML) and regenerate,
re-run `hil.parity_check` + `pytest tests hil/tests -q`, then record a real
encode→write→read→decode round-trip. Commit the real dump artifacts under
`reconciliation/` for traceability and flip this entry + the hil/README blocker
to closed.
