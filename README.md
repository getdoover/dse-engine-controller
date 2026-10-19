# DSE Engine Controller

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="App Icon" style="max-width: 300px;">

**Monitor and control DSE E-series engine controllers (E400, E800) over Modbus RTU / GenComm**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/dse-engine-controller/blob/main/LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/getdoover/dse-engine-controller?quickstart=1)

[Getting Started](#-getting-started) • [Configuration](#configuration) • [Commissioning](#-commissioning-checks) • [Developer](https://github.com/getdoover/dse-engine-controller/blob/main/DEVELOPMENT.md) • [Need Help?](#need-help)

<br/>

## 📖 Overview

This app connects a [Doover](https://doover.com) device to a **Deep Sea Electronics E-series engine controller** (the DSEE400 today, the E800 as a config choice) over its RS485 Modbus RTU port, giving full remote visibility and (optionally) remote control of the engine or pump set it drives.

It speaks DSE's **GenComm** register protocol. Each poll reads the module's control mode, alarm flags, engine speed and basic instrumentation, its named-alarm table and its speed-control block; every ten seconds or so it also reads the slow-moving pages — run hours, fuel, the full input-alarm table, the module's own flexible sensor readings. Everything lands on the Doover dashboard: engine telemetry, every active alarm by name and severity, the panel's mode and operating state, and what its speed control is holding.

With **Remote Control** enabled, the app also runs a safety-first auto-start sequence on top of the module's own engine logic:

- If the start gives up — the panel never became ready, or the engine never ran — the speaker says so, so a failed start doesn't just sound like the horn stopping.
- Every start runs a **pre-start warning** first — beacon (and optional horn) outputs are driven before the engine is allowed to crank. The rule is that they sound for as long as a start is being attempted: the configured countdown is a *floor*, the warning is then held open until the panel can actually take a start command, and the horn keeps going through the cranking attempt itself. There is never a window where the app is trying to start an engine and the horn is off. A single **Start Timeout Seconds** budget (default 90 s) covers the whole attempt from the press — countdown, panel prep and cranking — so that is also the longest a failed start can sound the horn. There is no path to a running engine that skips the warning.
- The panel is automatically prepared for remote operation *during* the pre-start warning, not after it: a latched shutdown alarm is reset, then the module is put into **Auto**, the only mode in which a DSE accepts a remote start. On a DSE this takes seconds rather than a reboot, so the panel is normally ready well before the countdown ends. Only the START command itself waits for the warning, since that is what the warning is warning about.
- The start is GenComm's **Telemetry Start** key, and a stop withdraws it so the module runs its own return delay and cool-down and stays in Auto for the next start. An engine the module is running for some other reason — a physical remote-start input, or a start made at the fascia in Manual — ignores that, so a stop that has not taken effect within a few minutes, or on a panel that is not in Auto, escalates to the **Stop mode** key, which stops the engine from any mode.
- An optional **panel power output** switches the module itself on, so a panel that is only powered for a run can still be started remotely. It is held on from the moment a run is wanted — through the boot, the warning, the run and the cooldown — and released once the pump is idle. It is never held *off*: wired in parallel with the panel's own power switch, the module can be powered up and run by hand at any time, and an engine found running that way keeps the power on rather than having it switched out from under it.

Starts and stops can be triggered three ways:

| Mode | Behaviour |
|------|-----------|
| **Manual** | Operator presses Start Now / Stop Now on the dashboard (always available) |
| **Sensor** | Fires when a chosen sensor crosses a setpoint (above/below configurable) |
| **Timer** | Start / stop at a scheduled date and time picked on the dashboard |

Engine speed is driven by a **run mode**. A mode is one of three things:

| Control | What drives engine speed | Where the sensor lives |
|---|---|---|
| **Fixed** | This app writes the module's **target speed** register and holds it there | — |
| **Regulate** | This app's PI loop adjusts the target speed to hold a process variable at a setpoint, with an invert option for inverse-acting processes and a live plot of setpoint / measured / error / commanded RPM | A Doover analog input or a remote tag |
| **Maintain Value** | The module's **own Maintain Value Control loop** holds a sensor at a set point; this app only moves the set point, and the same plot shows what the module's loop is doing | A flexible sensor input on the DSE, configured in DSE Configuration Suite |

Maintain Value is the better fit for most pump sets: the loop keeps holding pressure even if the Doover link drops, and the operator's set point slider maps straight onto the module's register, clamped to the Set Point Min / Max in the module's own configuration. Fixed modes still work alongside it for warm-up and line-fill steps, and Regulate is there for a sensor that can't be wired to the module.

Names are matched forgivingly: `Line Fill`, `line fill`, `linefill` and `LINE-FILL` all refer to the same mode, and the same goes for sensor names. Config can't offer a picker — only the dashboard can — so a typed name has to survive the ways a person will reasonably write it. An exact match always wins, so a site that deliberately keeps two similar names keeps them apart, and a name matching nothing is still reported rather than silently ignored.

Modes are *named and listed in config*, and a second list says how the pump moves between them, so a site can shape its own run sequence without any of it being built into this app:

| | |
|---|---|
| **Run Modes** | `Warm Up` — Fixed 900 rpm · `Line Fill` — Fixed 1300 rpm · `Run` — Maintain Value 4.5 bar |
| **Transitions** | Warm Up → Line Fill when the panel's Warming Up state clears · Line Fill → Run when discharge pressure is above 200 kPa for 10 s · Run → `Shutdown` if suction drops below 20 kPa for 30 s |

A rule's *number* can be handed to the operator with **Adjustable On Dashboard**: a Timer's period or a Sensor's trigger level appears as a slider with the controls of the mode the rule applies in — which is when moving it means anything — starting at the configured value. What the rule *is* stays in config; only the threshold moves, and the status line quotes whatever is in force so it never contradicts the slider beside it. (A Panel Flag rule has no number to offer.)

The **Mode Status** line counts a wait out rather than only describing it: `Warm Up → Line Fill when Discharge Pressure above 200 for 10s` becomes `… above 200 — met for 6s of 10s` once the reading is there, and a timer reads `after 60s (18s so far)`. A condition that reads the same whether it has been true for two minutes or never seen at all is exactly the one an operator wants to interrogate. With the pump stopped nothing is being watched, so each rule simply states what it looks for.

Where a site has more than one mode, the app tile names the one running — `Booster 1: Running (Warm Up)`. With a single mode there is nothing to distinguish.

Every mode change is logged to `activity_logs` with the reason that caused it — `run mode Line Fill (after 60s)`, `run mode Warm Up (operator)` — so a pump that changes behaviour on its own says why.

Transitions are checked in order and the first one whose condition is met wins; a condition can be a timer, a sensor crossing a setpoint (optionally held true for a while first), or a state the module itself reports — **Warming Up**, **Cooling Down**, **Common Alarm**, **Warning**. These come from the DSE's engine operating state and alarm flags; a Kensho-style *Line Fill* flag does not exist on a DSE, so a programme that used one should move on a timer or a pressure instead. A transition with a blank **From Mode** applies everywhere, which is how you write a fall-back rule.

A transition can also point at the reserved destination **`Shutdown`**, which stops the engine rather than changing mode. It goes through the same run latch the Stop button uses, so it gets the ordinary cooldown, timeout and path back to idle. Because it is a destination rather than a fourth kind of rule, *any* condition can stop the pump — after twenty minutes, on a discharge pressure that has collapsed, on a panel state.

**Start In Mode** says where every start begins. Leave it blank and the pump resumes whatever mode it was last left in, including one an operator picked before shutting down. It is deliberately not tied to the order of the mode list: that order is how the picker reads, and letting it decide what every start does would turn re-ordering into a behaviour change.

Conditions are level-triggered — they ask "is this true now", not "did this just become true" — so each mode has a **Minimum Seconds** floor on how long it runs before anything may move on from it. Without one, a rule whose condition happens to be true the moment a mode is entered fires immediately, which bites in two places: a mode an operator has just picked by hand gets abandoned on the next poll, and `Warming Up / Off` fires before the module has reported warming up at all. Conditions keep being watched during the floor, so a rule already satisfied fires as soon as it is up.

Picking a mode on the dashboard and a transition firing are the same operation, so an operator can jump between modes at any time — and a rule whose condition is currently true will move the pump straight back out again. A mode meant to be held is simply a mode with no transitions out of it. What each mode *is* lives in config; the numbers worth moving while the pump runs — a Fixed mode's target RPM, a Regulate mode's setpoint and sensitivity, a Maintain Value mode's set point — are controls on the dashboard, one set per mode.

A plain **Fixed RPM** mode is offered alongside whatever you configure, so an operator always has somewhere to take manual control of engine speed regardless of the programme. Nothing transitions into or out of it — it is reached by hand and stays until it is left by hand. **Include Default Fixed Run Mode** turns that off if you want the picker restricted to the configured modes; a site that names a mode `Fixed RPM` itself keeps its own and gets no duplicate.

Configure nothing at all and that Fixed RPM mode is the only one.

Sensors come from either source, and the app treats them identically for display, start/stop triggers and RPM regulation:

- **Native analog inputs** — a 4-20 mA or 0-10 V sensor wired straight to the device's own analog inputs, configured as a list, so an install shows exactly the inputs it has. No separate 4-20 mA app in the middle: pick the pin, give it a range in engineering units, and the app scales, smooths and health-checks the loop itself. Because it reads the raw signal it can tell a *broken* loop (open circuit / over range) from a genuinely low reading, and either hold the last commanded RPM or shut the engine down — your choice, per install.
- **Three remote tags** — a value published by another app on the device.

The module's own **flexible analogue inputs** are read and shown too (see *What the module reports* below), and one of them is what a Maintain Value mode holds; they are not currently selectable as the variable of a Regulate mode or a sensor trigger.

**Digital outputs** are configured the same way: a list of pin, name, and two resting states — **Default Off State** for when the engine isn't turning, **Default Running State** for when it is. Each one's state is shown on the dashboard below the sensors, and a run mode names only the outputs it wants *differently* from the running default. So a clutch engaged for most of a run is configured once rather than repeated in every mode, and a mode that says nothing about an output isn't inheriting whatever the last mode left — it is taking the default.

Outputs follow the engine rather than the sequence. Everything before it turns — the pre-start warning, the cranking — sits at the off state, where run modes have no say. Cooldown counts as running, because the engine is.

That is how a clutch or a discharge valve is driven: it is a Digital Output the modes happen to switch. There is no built-in clutch and no engage/disengage delays — a delay is expressed the way every other timing here is, as a mode with a **Minimum Seconds** and a transition out of it. So "open the valve 15 s after running" becomes a Warm Up mode with the valve closed, a 15 s minimum, and a timer transition into a mode that leaves it at its open running default.

There is no Reset Fault button: an auto-start fault clears by pressing **Start Now** (which is the operator saying "I know, go anyway"), or on its own when the engine turns out to be running again. The last fault stays on the dashboard as a warning until the pump next starts or runs, so it isn't lost. The module's own latched alarms are a separate thing — they are shown by name, and the next start's panel prep resets them before asking for Auto.

When an auto-start gives up — the panel never became ready, or the engine never ran — the speaker says so (**Start Failed Announcement**, "*{name} startup failed.*"). From any distance a start that failed sounds exactly like one that worked: the horn stops either way. Faults that aren't failed starts stay quiet, because an engine going silent is its own announcement.

That distinction runs through the rest of the app too. A start that gave up leaves an engine that definitively never ran, so the app releases the panel's power output (the operator's run demand outlives the fault — it is what a later Start press clears — and holding power would flatten the battery in front of a pump that isn't going anywhere) and the tile reads **Power Off** rather than `no comms`. A fault out of a *run* is treated the opposite way: the engine may still be turning, so the power stays on and the tile keeps saying `no comms`.

The warning carries no countdown figure, spoken or otherwise, because it no longer has a knowable length — it ends when the panel is ready, not when a timer expires. Instead the pre-start announcement **repeats every 20 seconds** for as long as the warning runs, which is also what someone walking up part-way through needs. A saved template still containing `{seconds}` is reworded to "shortly" automatically, and flagged in the log at startup.

With a horn fitted, announcements are spoken **into the horn's silences**. The horn sounds for 3 s in every 13 s and is deliberately phased to blast the instant the warning begins — which is exactly when the opening announcement is due — so each announcement is queued and goes out at the next gap wide enough to hold it. If no gap comes up before the phrase stops being worth saying, it is spoken over the horn rather than dropped. With no horn pin configured there is nothing to dodge and announcements go out immediately. Beacon and horn timing is coordinated across every pump on the device through shared tags, so several pumps warning at once pulse in unison rather than fighting over a shared output.

A start does not wait for the panel. The pre-start warning is entirely app-side, so pressing Start on a module that is switched off runs the beacon/horn countdown immediately — covering the panel's boot where a **Panel Power Output Pin** is switching it on — and the panel only has to answer before the start timeout expires. If it never does, the warning runs the whole way and the attempt aborts to a visible fault (`Auto Start Failed`, with the reason in the log) rather than sitting silently in `Ready`. Nothing is ever written to a panel that has not answered: a START fired blind at a bus with nobody on it achieves nothing, and a panel that does turn up is read on the next poll and started properly.

The auto-start machine is tolerant of the panel disappearing and coming back. A module that has been power-cycled boots reporting *stopped, out of Auto*, which is indistinguishable from an engine that has tripped — so a not-running reading arriving just after a comms outage, from a panel no longer in Auto, is waited out (up to ~150 s, shown as "Running (waiting for the panel)") instead of faulting a healthy pump. And if a fault has already latched, the app resumes control once the engine is confirmed running again with healthy comms — but only for faults a running engine disproves. A failed start or a panel alarm still waits for a human.

If the app itself hits an unexpected error it lets the exception go, so pydoover logs it, marks the app unhealthy and exits for the container to restart. Recovery is clean: the active run mode and its clock survive in `run_mode` and `run_mode_entered_at`, and the auto-start machine re-adopts an already-running engine on its first poll — so a restart mid-sequence resumes where it left off rather than dropping a hot pump back to warm-up RPM. A *deliberate* start resets to **Start In Mode** where one is configured, because that path runs through the pre-start warning and adoption doesn't.

The regulate loop only integrates time it could actually observe: steps skipped because a sensor faulted or comms dropped are capped, and a gap of a few poll periods re-seeds the loop from the engine's current speed, so RPM resumes from where the engine is instead of catching up on a backlog. Target speed and Maintain Value set point writes go to the module only when they differ from what it echoes back, with an unchanged value re-asserted at most every ten seconds, which keeps within GenComm's guidance on write rates and keeps a module in Manual — where it ignores both — from being written to every poll.

Each sensor slot gets a label, units, and setpoint bounds. **Allow RPM Control** is off by default on both kinds: a sensor can be displayed and used as a start/stop trigger without being named by a Regulate run mode, since driving engine speed from a reading is a deliberate choice per sensor. Other apps can also demand a run via the `run_request` tag (gated behind an operator-controlled automation toggle, off by default).

### What the module reports

| Group | Readings |
|---|---|
| **Engine** | RPM, load, oil pressure, oil and coolant temperature, coolant and oil level, fuel level, fuel rate and fuel used, boost pressure, intake and exhaust temperature, alternator and battery volts, run hours, number of starts |
| **Alarms** | Every active named alarm (58 on the E-series: low oil pressure, over speed, fail to start, ECU warnings, …) and every active input alarm (flexible sensor high/low/fault, PLC functions, maintenance due), each with its condition — warning, shutdown, controlled shutdown, electrical trip, indication. The worst one is the fault the tile and the auto-start act on; the whole list is one line on the dashboard |
| **Panel** | Control mode (Stop / Auto / Manual), engine operating state (pre-start, warming up, running, cooling down), panel lock, manual run time remaining, serial and model number, which control keys the module advertises |
| **Speed control** | The target speed the module is holding; the Maintain Value loop's set point, limits, measured value, units, source and PID gains |
| **Panel sensors** | The module's flexible analogue inputs A–G (A–L on an E800), in the units the module was configured with; hidden until the module says an input is in use |

<br/>

## 🚀 Getting Started

### How to Use

#### Quick Start Guide

Click the **Open in GitHub Codespaces** badge above to launch a ready-to-go development environment with:
- Python 3.13, uv, and all project dependencies
- Doover CLI (`doover`) pre-installed — you'll be prompted to log in on first open
- Claude Code with [doover-skills](https://github.com/getdoover/doover-skills) pre-configured

> **Claude Code:** You'll be prompted for your `ANTHROPIC_API_KEY` when creating a Codespace.
> Get a key at [console.anthropic.com](https://console.anthropic.com/settings/keys).
> To skip this prompt in future, save it as a permanent secret at
> [github.com/settings/codespaces](https://github.com/settings/codespaces).

This Doover App can be managed via the Doover CLI, and installed quickly onto devices through the Doover platform.

A register-accurate DSE E400 simulator ships in [`simulators/`](simulators/) for local development — see [DEVELOPMENT.md](DEVELOPMENT.md).

### Wiring

- RS485 A/B from the module's terminals C2 (A, −) and C8 (B, +) to the device's RS485 port, screened 120 Ω cable, with a **120 Ω termination resistor across A and B at each end** of the link. DSE's manual advises against USB-to-RS485 adaptors; a doovit's native RS485 port is the first choice.
- Factory serial settings on the module: **19200 baud, 8 data bits, no parity, 1 stop bit, server address 10**. Set the Modbus bus config to match.
- In DSE Configuration Suite, set the module's **client inactivity timeout** to at least twice the app's poll period (so 5 s or more at the default 2 s poll), or the module will decide the link is dead between polls.
- For remote starts the module must be able to enter Auto and accept the GenComm *Telemetry Start* key — the app logs which keys the module advertises at startup, and warns if the ones it needs are missing.
- For a Maintain Value run mode, enable **Maintain Value Control** on the module, pick the flexible sensor it holds, and set its Set Point Min / Max in the front panel editor. The app reads all of that back and clamps to it.

### Installation

1. In the Doover portal, open the device and add the **DSE Engine Controller** app.
2. Configure the Modbus bus (serial, RTU, 19200 8N1) and the module's server ID.
3. Leave Enable Remote Control off for a first look: the dashboard will fill with telemetry and alarms. Turn it on once the pre-start warning outputs are wired.

### Configuration

Configuration fields are declared in [`src/dse_engine_controller/app_config.py`](src/dse_engine_controller/app_config.py). Key settings:

| Setting | Description | Default |
|---------|-------------|---------|
| **DSE Model** | E400 or E800 — decides how many flexible sensors and alarm registers are read | `E400` |
| **Modbus Server ID** | The module's Modbus address | `10` |
| **Poll Period Seconds** | How often the live registers are read. Set the module's client inactivity timeout to at least twice this | `2.0` |
| **Slow Poll Period Seconds** | How often run hours, fuel, the input-alarm table and the flexible sensors are read | `10.0` |
| **Power Off Message** | What the app tile says when the panel can't be reached and nothing suggests a run is under way — usually it is simply switched off. During a start it still reads `Starting`, and mid-run (or while the app is holding the panel's power on) `no comms`. Blank restores the original `connecting…` / `no comms` wording | `Power Off` |
| **Enable Remote Control** | Show Start/Stop/RPM controls; disable for monitor-only | `true` |
| **Pre-Start Warning Seconds** | *Minimum* beacon/horn warning time. The warning then runs on until the panel can take a start, within the Start Timeout Seconds budget | `30` |
| **Start Timeout Seconds** | One safety budget for the whole start attempt, measured from the press. Also the longest the horn can sound. Needs to clear the *longer* of the countdown or the panel handshake — they run concurrently, so it isn't their sum | `90` |
| **Stop Timeout Seconds** | Safety cap — abort to fault if the engine doesn't stop | `120` |
| **Beacon / Horn Output Pin** | Optional digital-output pins; leave empty to disable each | *(empty)* |
| **Panel Power Output Pin** | Optional digital-output pin that powers the module while a run is wanted; leave empty if the panel is always powered | *(empty)* |
| **Target RPM Min / Max** | Bounds for the Fixed-mode RPM sliders and the Regulate loop's output — they can't be dragged outside them. Keep inside the module's configured minimum and maximum speeds | `900` / `2000` |
| **Enable Spoken Announcements** | Voice the pre-start warning and the start over a speaker app | `false` |
| **Announcement Speaker** | The speaker app that plays the announcements | *(empty)* |
| **Start Failed Announcement** | Spoken when an auto-start gives up; `{name}` is substituted. Blank to disable | `{name} startup failed.` |
| **Pre-Start / Start Announcement** | Spoken text; `{name}` is substituted. The pre-start phrase repeats every 20 s while the warning runs, spoken into the gaps between horn blasts. No countdown figure — a legacy `{seconds}` is reworded to "shortly" | *(see config)* |
| **Digital Outputs** | A list — one entry per output: pin, name, and its resting state with the engine stopped and with it running | *(empty)* |
| **Analog Inputs** | A list — one entry per sensor: AI pin, signal type, engineering range, label, units, invert flag, setpoint bounds, gauge display ranges, and whether it may control RPM. Always shown | *(empty)* |
| **Enable Remote Tags** | Show the remote tag slots below | `false` |
| **Remote Sensor 1–3** | Tag from another app + label, units, invert flag, setpoint bounds, and whether it may control RPM. Collapsed by default | *(disabled)* |
| **Sensor Fault Action** | What to do when a sensor the app is acting on fails: `Hold` the last RPM, or `Stop Engine` | `Hold` |
| **Sensor Fault Grace Seconds** | How long a sensor must stay faulted before that action is taken | `30` |
| **Start / Stop Trigger Direction** | Fire sensor-triggered starts/stops when above or below the setpoint | `Below` / `Above` |
| **Run Modes** | A list — one entry per mode: name, Fixed / Regulate / Maintain Value, its numbers (target RPM — required on a Fixed mode — the sensor it holds plus setpoint and sensitivity, or the Maintain Value set point and its slider bounds), the outputs it drives, and a Minimum Seconds floor. Empty means a single Fixed mode | *(empty)* |
| **Start In Mode** | Mode every start begins in. Blank resumes whatever mode the pump was last left in | *(blank)* |
| **Include Default Fixed Run Mode** | Offer a plain Fixed RPM mode alongside the configured ones, as a manual escape hatch | `true` |
| **Run Mode Transitions** | A list of rules for moving between modes: From (blank = any) / To (or `Shutdown`), and a Timer, Sensor or Panel Flag condition with an optional hold time. Checked in order; first match wins. **Adjustable On Dashboard** puts the rule's threshold on a slider | *(empty)* |
| **Regulate Sensitivity Min / Max** | Bounds for the RPM Regulate sensitivity slider. The knob is geometric and anchored at the top: **10 is the fastest loop these pumps need**, and every 6 steps below it divides the gains by ten. The slider bottoms out at `0.1`, about a 45th of the top — the gentlest anyone has wanted. New installs start at 2 | `0.1` / `10.0` |

#### Wiring an analog sensor

On a doovit, **AI 0-1 are the 0-10 V inputs and AI 2-5 are the 4-20 mA current
loops** (slave IO adds more channels to the same numbering). Set **Signal Type**
to match the sensor — the app checks the pin against the device's real IO layout
at startup and flags a mismatch on the dashboard rather than quietly reading
nonsense. **Value At Minimum / Maximum Signal** are the engineering readings at
4 mA / 20 mA (or 0 V / 10 V); set the minimum higher than the maximum for an
inverse-scaled sensor such as an ullage probe. That range is also what the
dashboard gauge spans — the setpoint bounds only size the trigger / regulate
sliders. Each input's raw mA / V reading is published under Details →
Diagnostics for commissioning.

**Display Ranges** colour the gauge, the same way the built-in engine gauges are
banded. Add one entry per band with a From / To in the sensor's units, a colour,
and an optional label — e.g. `0–200 red "Low"`, `200–1400 green "Normal"`,
`1400–1600 yellow "High"`. Order doesn't matter (bands are sorted by where they
start, and back-to-front entries are corrected), and leaving the list empty gives
a single plain band across the whole range.

Open-circuit detection applies to 4-20 mA only (0 V is a legitimate voltage
reading). A faulted sensor never fires a start/stop trigger or a run mode
transition, and the RPM Regulate loop holds its last commanded speed instead of
chasing a dead reading.

A faulted input keeps reporting its last good value for about 15 seconds (or two
poll periods, whichever is longer) and then reads as no value at all, so its gauge
blanks rather than flat-lining on a stale number that looks healthy. Recovery is
immediate on the next good reading, and both the drop-out and the recovery are
logged so the plot shows the gap.

Analog inputs need no enabling switch — the list is a single row when empty. Only
the remote tag slots are gated, because each is a form rather than a list row, and
they stay a fixed set: pydoover resolves a config `TagRef` only at the top level of
the schema, so one inside a list is silently never bound.

The form only shows fields that can do something: each block sits behind an
`Enable …` boolean (`Enable Remote Control`, `Enable Spoken Announcements`,
`Enable Remote Tags`) and is hidden — and inactive at runtime — while that
boolean is off. This uses pydoover's `show_if=` with `config.equal(...)`;
conditions can only test equality against one non-conditional sibling field,
which is why each block is fronted by an explicit boolean rather than keying off
"a pin/app has been chosen".

After changing config fields, regenerate `doover_config.json` with `uv run export-config`.

<br/>

## 🔗 Integrations

### Tags

All panel readings and control state are published as tags from [`src/dse_engine_controller/app_tags.py`](src/dse_engine_controller/app_tags.py). The ones most useful to other apps:

| Tag | Description |
|-----|-------------|
| **status_string** | The app tile text: `<name>: Running (Warm Up)`, `<name>: Fault: Low oil pressure`, `<name>: Power Off`… |
| **state** | Derived panel state: `stopped_auto`, `stopped_manual`, `pre_start`, `warming_up`, `running`, `cooling_down`, `fault`, `no_comms` |
| **is_running** / **is_auto** / **is_faulted** / **has_warning** / **has_common_alarm** | Panel state flags. `is_faulted` means the module is holding the engine off on a shutdown, controlled shutdown or electrical trip; a warning alone does not set it |
| **control_mode** / **engine_state** | The module's mode (`Stop` / `Auto` / `Manual`) and engine operating state, as text |
| **fault_string** / **alarm_summary** / **alarm_count** | The worst active alarm by name, every active alarm worst-first on one line, and how many there are. `alarm_summary` is logged on change, so the history shows when each alarm came and went |
| **engine_rpm**, **coolant_temp_c**, **oil_pressure_kpa**, **battery_voltage**, **fuel_level_pct**, **total_engine_hours**, … | Engine telemetry. A reading the module doesn't implement is `null`, never zero |
| **target_speed_rpm** | The target speed the module reports holding — the echo the app checks its own writes against |
| **maintain_setpoint** / **maintain_value** / **maintain_units** / **maintain_setpoint_min** / **maintain_setpoint_max** | The module's Maintain Value loop: what it is holding at, what it reads, and the limits it clamps to |
| **flex_a_value** … **flex_g_value** (… **flex_l_value** on an E800) | The module's flexible sensor readings, in the units the module was configured with |
| **supported_keys** | The GenComm control keys the module advertises — the first thing to check if a start does nothing |
| **panel_locked** | The module's Panel Lock input is active, so mode changes and alarm resets sent to it will be ignored |
| **auto_start_state** | Auto-start orchestration state (`Ready`, warning countdown, starting, …) |
| **auto_start_fault** | Last auto-start fault, shown on the dashboard as a warning until the pump is started or running normally. The indicator also goes quiet once the fault is 24 hours old, and a restart drops it entirely — the machine comes up with no memory of it, so the app can't vouch for it. The event stays in the activity log either way. This is the app's fault, not the module's |
| **pump_power_on** | Whether the app is holding the panel's power output on. False when no Panel Power Output Pin is configured, and false for a panel powered by hand |
| **manual_mode_hidden** / **rpm_status** | Whether the panel is running in Manual — where it owns engine speed and ignores the target speed and set point writes — and the message shown in place of the RPM controls, which are hidden while it is |
| **run_request** / **run_request_reason** | Set `run_request` true from another app to demand a run; the reason is shown to operators. Only honoured when the operator has enabled automation. |
| **remote_sensor_0–2** | The configured remote sensors, republished locally |
| **analog_value_pin&lt;n&gt;** | A native analog input, scaled to engineering units. Logged when the reading moves 0.5% of its configured span (plus a 5-minute heartbeat), so an idling sensor doesn't fill the history. Named after the AI pin, so reordering the list never moves a sensor's history |
| **analog_raw_pin&lt;n&gt;** / **analog_fault_pin&lt;n&gt;** | Raw mA / V for that input, and its health flag |
| **analog_fault_string** / **sensor_fault_active** | Summary line and flag for any analog input fault, plus any startup pin-config problem. Published for other apps and alerts — deliberately not shown on the dashboard |
| **run_mode_active** / **run_mode_status** | The name of the mode actually running, and what would move it on (or the config problems stopping it working — observations that aren't problems, like a mode you only ever pick by hand, stay in the log) |
| **run_mode_variable** | The sensor the active mode is regulating, by sanitised name; `maintain_value` in a Maintain Value mode; blank in a Fixed mode. Which sensor a Regulate mode holds lives in config, per mode, so nothing else published says it — a display has to know in order to show the setpoint against the right reading rather than against engine speed |
| **regulate_setpoint_active / measured / error / output_rpm** | Loop telemetry, logged on change for plotting — from this app's PI loop in a Regulate mode, or from the module's own loop in a Maintain Value mode. `_active` is the setpoint the loop is holding — kept separate from the operator's input so the loop can't overwrite an edit made mid-iteration |

<br/>

## 🔬 Commissioning Checks

The protocol layer follows DSE's GenComm standard (v2.228) and the DSEE400 operator manual, and the app is tested end to end against the register-accurate simulator in `simulators/dse/`. When commissioning a module, the following are worth confirming against the unit's own display and configuration, since they depend on module firmware and configuration rather than on this app:

1. **Alarm reporting.** Raise a test alarm on the module and check it appears on the dashboard by name with the right severity. The app reads both the status word and the alarm pages, so a fault never depends on a single bit.
2. **Speed control precedence.** On a site that uses both Fixed and Maintain Value run modes, confirm which register the module honours while Maintain Value Control is enabled. The simulator models the loop as owning speed while it is enabled.
3. **Flexible-sensor values.** Compare `flex_*_value` and `maintain_value` with the module's display for any sensor configured with a scaling multiplier.
4. **Link settings.** Set the module's client inactivity timeout to at least twice the poll period, and fit 120 Ω termination at both ends of the RS485 link.

DSE issues the register table for a specific firmware on request (`support@deepseaelectronics.com`, quoting the module's serial number). The pages this app uses are 3, 4, 5, 7, 16, 137, 153, 154 and 178, as listed in `src/dse_engine_controller/gencomm.py`.

<br/>

## 🛠 Development

```bash
uv run pytest tests -v       # unit tests, simulator model tests, and the app driven end to end against the simulator
uv run export-config         # regenerate config_schema in doover_config.json
uv run export-ui             # regenerate ui_schema (required to publish)
doover app run               # device agent + DSE simulator + this app, via docker compose
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for the layout and [CLAUDE.md](CLAUDE.md) for the pydoover patterns in use.

<br/>

### Need Help?

- 📧 Email: support@doover.com
- 📖 [Doover Documentation](https://docs.doover.com)
- 👨‍💻 [App Developer Documentation](https://github.com/getdoover/dse-engine-controller/blob/main/DEVELOPMENT.md)

<br/>

## 📄 License

This app is licensed under the [Apache License 2.0](https://github.com/getdoover/dse-engine-controller/blob/main/LICENSE).
