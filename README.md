# DSE Engine Controller

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="App Icon" style="max-width: 300px;">

**Monitor and control DSE E-series engine controllers (E400, E800) over Modbus RTU / GenComm**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/dse-engine-controller/blob/main/LICENSE)

[Getting Started](#-getting-started) • [Configuration](#configuration) • [Commissioning](#-commissioning-checks) • [Developer](DEVELOPMENT.md) • [Need Help?](#need-help)

<br/>

## 📖 Overview

This app connects a [Doover](https://doover.com) device to a **Deep Sea Electronics E-series engine controller** (the DSEE400 today, the E800 as a config choice) over its RS485 Modbus RTU port, giving full remote visibility and (optionally) remote control of the engine or pump set it drives.

It speaks DSE's **GenComm** register protocol. Each poll reads the module's control mode, alarm flags, engine speed and basic instrumentation, its named-alarm table and its speed-control block; every ten seconds or so it also reads the slow-moving pages — run hours, fuel, the full input-alarm table, the module's own flexible sensor readings. Everything lands on the Doover dashboard: engine telemetry, every active alarm by name and severity, the panel's mode and operating state, and what its speed control is holding.

With **Remote Control** enabled, the app runs the same safety-first auto-start sequence as the Kensho Controller app, on top of the module's own engine logic:

- Every start runs a **pre-start warning** first — beacon (and optional horn) outputs are driven before the engine is allowed to crank, and they sound for as long as a start is being attempted. There is no path to a running engine that skips the warning.
- The panel is prepared for remote operation *during* the warning: a latched shutdown alarm is reset, then the module is put into **Auto** (the only mode in which a DSE accepts a remote start). On a DSE this takes seconds, not the Kensho's thirty-second reboot.
- The start itself is GenComm's **Telemetry Start** key; a stop withdraws it and lets the module run its own return delay and cool-down. An engine the module is running for some other reason — a physical remote-start input, or a fascia start in Manual — ignores that, so a stop that hasn't taken effect escalates to **Stop mode**.
- If the start gives up — the panel never became ready, or the engine never ran — the dashboard and the speaker say so.
- An optional **panel power output** switches the module itself on, so a panel that is only powered for a run can still be started remotely.

Starts and stops can be triggered three ways:

| Mode | Behaviour |
|------|-----------|
| **Manual** | Operator presses Start Now / Stop Now on the dashboard (always available) |
| **Sensor** | Fires when a chosen sensor crosses a setpoint (above/below configurable) |
| **Timer** | Start / stop at a scheduled date and time |

Engine speed is driven by a **run mode**. A mode is one of three things:

| Control | What drives engine speed | Where the sensor lives |
|---|---|---|
| **Fixed** | This app writes the module's **target speed** register and holds it there | — |
| **Regulate** | This app's PI loop adjusts the target speed to hold a sensor at a setpoint | A Doover analog input or a remote tag |
| **Maintain Value** | The module's **own Maintain Value Control loop** holds a sensor at a set point; this app only moves the set point | A flexible sensor input on the DSE, configured in DSE Configuration Suite |

Maintain Value is the better fit for most pump sets: the loop keeps holding pressure even if the Doover link drops, and the operator's set point slider maps straight onto the module's register. Fixed modes still work alongside it for warm-up and line-fill steps, and Regulate is there for a sensor that can't be wired to the module.

Modes are named and listed in config, with a second list saying how the pump moves between them — on a timer, on a sensor crossing a setpoint, or on a panel state (**Warming Up**, **Cooling Down**, **Common Alarm**, **Warning**). A transition can also point at **Shutdown** to stop the engine. Every mode change is logged with its reason. A plain **Fixed RPM** mode is offered alongside whatever you configure, so an operator always has somewhere to take manual control.

Native analog inputs, remote tags, and digital outputs (clutches, valves) are configured exactly as in the Kensho app — see the configuration table below. The run-mode sequencer, the pre-start warning, announcements, sensor-fault handling and cross-pump horn coordination are shared code and behave identically.

### What the module reports

| Group | Readings |
|---|---|
| **Engine** | RPM, load, oil pressure, oil and coolant temperature, coolant and oil level, fuel level, fuel rate and fuel used, boost pressure, intake and exhaust temperature, alternator and battery volts, run hours, number of starts |
| **Alarms** | Every active named alarm (58 on the E-series: low oil pressure, over speed, fail to start, ECU warnings, …) and every active input alarm (flexible sensor high/low/fault, PLC functions, maintenance due), each with its condition — warning, shutdown, controlled shutdown, electrical trip, indication |
| **Panel** | Control mode (Stop / Auto / Manual), engine operating state (pre-start, warming up, running, cooling down), panel lock, manual run time remaining, serial and model number, which control keys the module advertises |
| **Speed control** | The target speed the module is holding; the Maintain Value loop's set point, limits, measured value, units, source and PID gains |
| **Panel sensors** | The module's flexible analogue inputs A–G (A–L on an E800), in the units the module was configured with; hidden until the module says an input is in use |

<br/>

## 🚀 Getting Started

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

A register-accurate DSE E400 simulator ships in [`simulators/`](simulators/) for local development — see [DEVELOPMENT.md](DEVELOPMENT.md).

### Configuration

Configuration fields are declared in [`src/dse_engine_controller/app_config.py`](src/dse_engine_controller/app_config.py). Key settings:

| Setting | Description | Default |
|---------|-------------|---------|
| **DSE Model** | E400 or E800 — decides how many flexible sensors and alarm registers are read | `E400` |
| **Modbus Server ID** | The module's Modbus address | `10` |
| **Poll Period Seconds** | How often the live registers are read. Set the module's client inactivity timeout to at least twice this | `2.0` |
| **Slow Poll Period Seconds** | How often run hours, fuel, the input-alarm table and the flexible sensors are read | `10.0` |
| **Power Off Message** | What the app tile says when the panel can't be reached and nothing suggests a run is under way | `Power Off` |
| **Enable Remote Control** | Show Start/Stop/RPM controls; disable for monitor-only | `true` |
| **Pre-Start Warning Seconds** | *Minimum* beacon/horn warning time. The warning then runs on until the panel can take a start, within the Start Timeout budget | `30` |
| **Start Timeout Seconds** | One safety budget for the whole start attempt, measured from the press. Also the longest the horn can sound | `90` |
| **Stop Timeout Seconds** | Safety cap — abort to fault if the engine doesn't stop | `120` |
| **Beacon / Horn Output Pin** | Optional digital-output pins; leave empty to disable each | *(empty)* |
| **Panel Power Output Pin** | Optional digital-output pin that powers the module while a run is wanted; leave empty if it is always powered | *(empty)* |
| **Target RPM Min / Max** | Bounds for the Fixed-mode RPM sliders and the Regulate loop's output. Keep inside the module's configured speed limits | `900` / `2000` |
| **Run Modes** | A list — one entry per mode: name, control (Fixed / Regulate / Maintain Value), its starting target RPM, setpoint or Maintain Value set point, the outputs it drives, and its minimum time | *(empty = one Fixed RPM mode)* |
| **Run Mode Transitions** | A list of rules for moving between modes: from, to (or `Shutdown`), and when — timer, sensor crossing, or panel state | *(empty)* |
| **Start In Mode** | Mode every start begins in; blank resumes the last mode | *(empty)* |
| **Digital Outputs** | A list — one entry per output: pin, name, and its resting state with the engine stopped and running | *(empty)* |
| **Analog Inputs** | A list — one entry per sensor: AI pin, signal type, engineering range, label, units, invert flag, setpoint bounds, gauge bands, and whether it may control RPM | *(empty)* |
| **Enable Remote Tags** / **Remote Sensor 1–3** | Sensors published by other apps on the device | `false` |
| **Sensor Fault Action** / **Grace Seconds** | What to do when a sensor the app is acting on fails: hold the last RPM, or stop the engine, after a grace period | `Hold` / `30` |
| **Enable Spoken Announcements** | Voice the pre-start warning and the start over a speaker app | `false` |

<br/>

## 🔌 Integrations

### Tags

The tags other apps are most likely to want. Everything shown on the dashboard is also a tag.

| Tag | Description |
|-----|-------------|
| `status_string` | The app tile text: `<name>: Running (Warm Up)`, `Fault: Low oil pressure`, `Power Off`… |
| `state` | Derived panel state: `stopped_auto`, `stopped_manual`, `pre_start`, `warming_up`, `running`, `cooling_down`, `fault`, `no_comms` |
| `is_running`, `is_auto`, `is_faulted`, `has_warning`, `has_common_alarm` | Panel state flags |
| `control_mode`, `engine_state` | The module's mode and operating state as text |
| `fault_string`, `alarm_summary`, `alarm_count` | The worst active alarm, every active alarm worst-first, and how many |
| `engine_rpm`, `oil_pressure_kpa`, `coolant_temp_c`, `battery_voltage`, `fuel_level_pct`, `total_engine_hours`, … | Engine telemetry |
| `target_speed_rpm`, `maintain_setpoint`, `maintain_value`, `maintain_units` | What the module's speed control is holding |
| `flex_a_value` … `flex_g_value` | The module's flexible sensor readings, in its units |
| `auto_start_state`, `auto_start_fault` | The app's own start sequence and its last fault |
| `run_mode_active`, `run_mode_status` | The run mode in force and what would move it on |
| `run_request`, `run_request_reason` | Set by another app to demand a run (gated behind the operator's automation toggle) |
| `supported_keys` | The GenComm control keys the module advertises |

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

## Need Help?

Contact the Doover team at [support@doover.com](mailto:support@doover.com), or raise an issue on this repository.
