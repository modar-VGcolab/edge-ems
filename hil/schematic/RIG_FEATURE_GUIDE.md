# Running controller features on the Typhoon rig

Companion to **`RIG_CLOSED_LOOP.md`** (bring-up: bridge + containers + loop
RUN). This guide assumes that's already done and the loop is steady in RUN —
it's the step-by-step for exercising each control feature once you're there:
**self-consumption** (§1, the default active-power loop) and **tariff PFC**
(§2, optional reactive-power control). §3 covers verification/troubleshooting
shared by both.

**Which config files you edit depends on how you run core/controller:**

| You run core/controller as... | Asset config | EMS config |
|---|---|---|
| Containers (`docker compose ... --profile services`) | `configs/asset_config.docker.yaml` | `configs/edge_ems_config.docker.yaml` |
| Host process (`.venv-rig`, `python -m core.main` / `python -m controller.main`) | `configs/asset_config.rig.yaml` | `configs/edge_ems_config.rig.yaml` |

These two pairs are **not** kept in sync automatically — there's no shared
inheritance, no validation that flags drift, and editing the wrong one is a
silent no-op (the running process just keeps using whatever it already loaded,
or a stale value in the *other* file). Confirm which path you're on before
editing anything below. See §3.4 if you're not sure.

---

## 1. Self-consumption (default active-power control)

The controller's baseline behavior: drive PCC active power to a target
(`controller.pcc_setpoint_kw`, default `0.0` = net-zero grid exchange) by
dispatching the battery, then load-shedding/curtailing PV if the battery alone
can't hold it. No feature flag to enable — this runs whenever PFC/droop are
off or idle.

1. Confirm the target. In your EMS config's `controller:` block:

   ```yaml
   controller:
     pcc_setpoint_kw: 0.0      # self-consumption; set nonzero to test import/export tracking
   ```

2. Restart/rebuild whichever process reads that file (controller only — this
   isn't an asset-config change, so the bridge doesn't need touching):

   ```powershell
   # containers:
   docker compose --env-file configs/.env `
     -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
     --profile services up -d --build controller

   # host process (.venv-rig):
   # Ctrl-C the running `python -m controller.main`, then re-run it
   ```

3. Watch it converge. Either:
   - `python -m hil.schematic.live_plot` — PCC power should settle toward the
     setpoint within a few cycles (rig-tuned gains: ~12-15 s, no overshoot).
   - `GET http://localhost:5000/status` — `last_cycle.mode` should be `RUN`,
     `pcc_error_kw` shrinking toward 0.
   - In HIL SCADA: `Power Meter.pcc_01_meter.POWER_P` trending to 0 (or your
     setpoint), `bess-01-UI.Pmeas_kW` moving to compensate.

4. To actually *see* it react instead of just sitting at a settled point, drive
   the flexible load live and watch the battery respond — this needs a one-time
   SCADA slider wired into the model. Full setup (SCADA input + probe, model
   recompile, wiring into `signal_bridge.py`) is in `RIG_CLOSED_LOOP.md` §4b —
   follow that, then come back here for PFC.

**Expected at equilibrium:** PCC active power at (or within a couple kW of) the
setpoint; battery holding whatever power balances load/PV against it; `derate`/
`curtail` factors at `1.0` unless SoC or the feed limit forced a mitigation.

---

## 2. Tariff-driven power-factor control (PFC)

Optional overlay: a time-of-day tariff schedule sets a PF goal at the PCC, and
the controller sizes a battery reactive setpoint for it — after subtracting
whatever PV/flexible-load/fixed-load ("meter" class) reactive is *already*
flowing, so those don't get double-counted. Confirmed on this rig: with a fixed
load contributing ~47-50 kVAr, enabling PFC pulled PCC reactive power down to
~2.4-2.6 kVAr.

### 2.1 Prerequisite: the fixed-load meter must be wired in

PFC's math is only as good as `other_reactive_kvar`, which sums the `pv`,
`flexible_load`, and `meter` aggregates. If your site has a fixed load (a
meter-class asset with no setpoints — `load-02` on this rig, `meter-01` in the
example/SIL configs), confirm it's `state: active` in your asset config:

```yaml
  - id: load-02              # or meter-01
    class: meter
    name: Fixed load meter
    limits: { max_power_kw: 100 }
    comm: { protocol: modbus_tcp, host: ..., port: 5024, unit_id: 1,
            register_map: maps/meter_v1.yaml }
```

If you just added/enabled it, that's an **asset-config** change — restart the
bridge (it only serves ports for assets in the config it was launched with)
*and* rebuild/restart core + controller.

### 2.2 Enable PFC and pick a target

In your EMS config:

```yaml
pfc:
  enabled: true
  q_sign_convention: 1        # confirmed correct on this rig (2026-07-28) -- flip to -1 if Q moves the wrong way
  s_rated_kva: 500             # battery inverter apparent-power rating, for the reactive clamp
  default:
    pf_target: 1.0             # unity outside every window (no reactive command)
    mode: unity
  windows:
    - {start: "08:00", end: "20:00", pf_target: 0.95, mode: lagging}   # peak: absorb VARs (inductive)
    - {start: "20:00", end: "08:00", pf_target: 0.98, mode: leading}   # off-peak: supply VARs (capacitive)
```

`mode: lagging` commands the battery to absorb reactive power (inductive);
`leading` supplies it (capacitive). Whichever window covers the current
wall-clock time is what's active — check that before you go looking for an
effect that won't happen for hours.

This is an **EMS-config-only** change (no asset added/removed), so you only
need to restart/rebuild **controller** — the bridge doesn't need touching:

```powershell
# containers:
docker compose --env-file configs/.env `
  -f deploy/docker-compose.yml -f deploy/docker-compose.rig.yml `
  --profile services up -d --build controller

# host process:
# Ctrl-C `python -m controller.main`, then re-run it
```

### 2.3 Verify it

Give it a few control cycles to settle, then check:

- **The fixed load's telemetry is actually flowing** (do this first — if it
  isn't, PFC will silently compute against 0 and look like it's doing
  nothing):

  ```flux
  from(bucket:"edge_ems")
    |> range(start: -2m)
    |> filter(fn: (r) => r._measurement == "meter" and r._field == "reactive_power_kvar")
  ```

  Should show a nonzero, fresh (`GOOD`) value close to what you'd read
  straight off the model. Empty or stale means the meter asset isn't active,
  or core/controller haven't been restarted since it was added.

- **PCC reactive power is dropping toward the residual**, not sitting at the
  pre-PFC level:

  ```flux
  from(bucket:"edge_ems")
    |> range(start: -2m)
    |> filter(fn: (r) => r._measurement == "pcc" and
                          (r._field == "active_power_kw" or r._field == "reactive_power_kvar"))
  ```

  Or read the Grid Interface / PCC panel directly in SCADA (`P_Grid`/`Q_Grid`,
  `Active Power`/`Reactive Power`).

**A caveat on reading "PF" directly:** `PF = P/√(P²+Q²)`. At a self-consumption
site, PCC active power is regulated toward ~0 by design — and at `P≈0`, PF
reads near 0 for *any* nonzero Q, no matter how well-compensated the reactive
side is. Don't take a low PF display as PFC failing; look at whether Q itself
dropped instead. If you specifically want to see PF converge on the tariff
target number, you need nonzero PCC active power for the ratio to mean
anything — temporarily set `pcc_setpoint_kw` to something like `-50` (import)
so `P` dominates, then check PF against the window's target.

### 2.4 If Q moves the wrong way

Flip `q_sign_convention` between `1` and `-1` — this calibrates the direction
between the controller's math and the battery's actual VarSet convention on
your particular model/inverter, the same class of open sign flag as
`BESS_P_SIGN`/`PCC_P_SIGN` in `signal_bridge.py`. Confirmed `1` on this rig
(2026-07-28); re-verify if you change plant models or the battery block.

---

## 3. Shared verification / troubleshooting

- **`GET http://localhost:5000/status`** — `last_cycle.mode` (must be `RUN`
  for either feature to compute anything at all; `HOLD`/`SAFE` freeze or zero
  the reactive setpoint), `pcc_error_kw`. Note: the commanded
  `reactive_setpoint_kvar` itself isn't currently surfaced here or in the
  InfluxDB `control` measurement — you validate PFC by its effect on measured
  PCC/battery reactive power, not by reading the setpoint directly.
- **`python -m hil.schematic.live_plot`** — rolling PCC power / battery
  power+SoC from InfluxDB, no Docker needed on the viewing side.
- **Config-file confusion is the most common failure mode.** If you change a
  config and see zero effect, the first thing to check is whether the process
  that's actually running was pointed at the file you edited (see the table in
  the intro) — that part is still read once at startup, so pointing at the
  wrong file is still a silent no-op regardless of the note below. A container
  needs `--build` if the code changed, not just the config.
- **EMS tuning (`Kp`/`Ki`, `droop`, `pfc`, `pcc_setpoint_kw`) no longer strictly
  needs a controller restart** (KNOWN_ISSUES #2, fixed): `PUT /config/ems`
  applies to the running loop immediately via `LoopRunner.apply_config`,
  carrying the PI integral and current mode across untouched. Restarting is
  still the simplest path if you're editing the YAML file by hand (this guide
  assumes that workflow throughout), but if the controller API is gated
  (`CONTROLLER_API_TOKEN` set), a live edit looks like:
  ```powershell
  curl.exe -H "X-API-Key: <token>" http://localhost:5000/config/ems
  # edit the returned JSON (e.g. bump pfc.default.pf_target), then:
  curl.exe -X PUT -H "X-API-Key: <token>" -H "Content-Type: application/json" `
    -d "<edited JSON>" http://localhost:5000/config/ems
  ```
  Asset-config changes (adding/removing/toggling an asset, e.g. `load-02`)
  still require restarting the bridge and core/controller — only non-structural
  EMS tuning is live.
- **Restart matrix:**

  | What changed | Bridge | core | controller |
  |---|---|---|---|
  | Asset added/removed/toggled (`asset_config.*.yaml`) | restart | restart | restart |
  | EMS tuning only (`edge_ems_config.*.yaml`: `pfc`, `Kp`/`Ki`, `droop`, `pcc_setpoint_kw`) | — | — | restart, **or** live via `PUT /config/ems` |
  | Code change (`.py` files) | restart | rebuild+restart | rebuild+restart |
