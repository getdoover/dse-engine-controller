# DSE Engine Controller

<img src="https://images.seeklogo.com/logo-png/27/1/dse-logo-png_seeklogo-271497.png" alt="App Icon" style="max-width: 100px;">

**Monitor and control DSE engines with real-time parameter visualization, fault detection, and start/stop sequencing.**

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/getdoover/dse-engine-controller)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/dse-engine-controller/blob/main/LICENSE)

[Getting Started](#getting-started) | [Configuration](#configuration) | [Developer](https://github.com/getdoover/dse-engine-controller/blob/main/DEVELOPMENT.md) | [Need Help?](#need-help)

<br/>

## Overview

The DSE Engine Controller is a comprehensive engine monitoring and control application designed for DSE (Deep Sea Electronics) engine controllers. It provides real-time visualization of critical engine parameters including RPM, oil pressure, coolant temperature, battery voltage, fuel level, and engine hours, all with color-coded status ranges for quick assessment.

The application implements a full engine state machine that handles start/stop sequences with proper pre-crank priming, configurable crank timing, automatic retry logic, and controlled cooldown periods. When parameters exceed configurable thresholds, the system automatically detects and displays faults, providing operators with clear visual warnings and the ability to acknowledge and reset fault conditions.

Built for the Doover IoT platform, this application runs as a Docker container on edge devices, communicating with DSE engine controllers via Modbus/CAN protocols or through a simulator for testing and development. All engine data is logged to channels for historical analysis and can trigger alerts for critical conditions.

### Features

- **Real-time Engine Monitoring** - Live display of RPM, oil pressure, coolant temperature, battery voltage, fuel level, and engine hours
- **Color-coded Status Ranges** - Visual indicators show normal (green), warning (yellow/orange), and critical (red) conditions at a glance
- **Smart Start/Stop Sequencing** - Automated pre-crank priming, configurable crank attempts with rest periods, and controlled cooldown before shutdown
- **Fault Detection and Handling** - Automatic fault detection with clear visual warnings for low oil pressure, high temperature, low battery, and overspeed conditions
- **Engine Mode Control** - Manual, Auto, and Off modes for flexible operation
- **Emergency Stop** - Immediate shutdown capability with confirmation prompt for safety
- **Data Logging** - Continuous logging of engine parameters to channels for historical analysis
- **Configurable Thresholds** - All alarm thresholds and timing parameters are user-configurable

<br/>

## Getting Started

### Prerequisites

1. A Doover-connected device (gateway or edge controller)
2. DSE engine controller with Modbus or CAN communication capability (or use the simulator for testing)
3. Network connectivity between the Doover device and the engine controller

### Installation

1. Navigate to the Doover Portal and select your device
2. Go to the "Apps" section and click "Add App"
3. Search for "DSE Engine Controller" and click "Install"
4. Configure the application settings (see Configuration section below)
5. The app will automatically start and begin monitoring

### Quick Start

1. After installation, the engine controller UI will appear on your device dashboard
2. Verify engine parameters are displaying correctly (RPM, Oil Pressure, etc.)
3. If using a simulator for testing, configure the `Simulator App Key` in settings
4. Use the "Start Engine" button to initiate a start sequence
5. Monitor the status display and parameter values during operation

<br/>

## Configuration

| Setting | Description | Default |
|---------|-------------|---------|
| **Display Name** | Name shown in the UI | Engine Controller |
| **Auto Start Enabled** | Allow automatic engine starting based on conditions | false |
| **Crank Time (seconds)** | Maximum time to crank before timeout | 10 |
| **Crank Rest Time (seconds)** | Rest time between crank attempts | 5 |
| **Max Crank Attempts** | Maximum number of crank attempts before failure | 3 |
| **Cooldown Time (seconds)** | Engine cooldown period before shutdown | 60 |
| **Low Oil Pressure (PSI)** | Oil pressure threshold for low warning | 15.0 |
| **High Coolant Temp (C)** | Coolant temperature threshold for high warning | 95.0 |
| **Low Battery Voltage (V)** | Battery voltage threshold for low warning | 11.5 |
| **High Battery Voltage (V)** | Battery voltage threshold for high warning | 14.5 |
| **Overspeed RPM** | RPM threshold for overspeed shutdown | 2000 |
| **Underspeed RPM** | RPM threshold for underspeed warning | 1400 |
| **Simulator App Key** | App key for engine data simulator (for testing) | *Required* |

### Example Configuration

```json
{
  "display_name": "Generator #1",
  "auto_start_enabled": false,
  "crank_time_(seconds)": 10,
  "crank_rest_time_(seconds)": 5,
  "max_crank_attempts": 3,
  "cooldown_time_(seconds)": 60,
  "low_oil_pressure_(psi)": 15.0,
  "high_coolant_temp_(c)": 95.0,
  "low_battery_voltage_(v)": 11.5,
  "high_battery_voltage_(v)": 14.5,
  "overspeed_rpm": 2000,
  "underspeed_rpm": 1400,
  "simulator_app_key": "engine_simulator_001"
}
```

<br/>

## UI Elements

This application provides the following UI elements:

**Variables (Display)**

| Element | Description |
|---------|-------------|
| **Engine Status** | Current state of the engine (Stopped, Pre-crank, Cranking, Running, Cooling Down, FAULT) |
| **Engine RPM** | Current engine speed with color-coded ranges (Stopped: grey, Cranking: yellow, Normal: green, Overspeed: red) |
| **Engine Hours** | Total accumulated engine running hours |
| **Oil Pressure** | Current oil pressure in PSI with ranges (Low: red, Normal: green, High: yellow) |
| **Coolant Temperature** | Current coolant temperature in Celsius with ranges (Cold: blue, Warming: yellow, Normal: green, High: red) |
| **Battery Voltage** | Current battery voltage with ranges (Low: red, Normal: green, High: yellow) |
| **Fuel Level** | Current fuel level percentage with ranges (Critical: red, Low: orange, Normal: green) |
| **Last Update** | Timestamp of the most recent data update |

**Warning Indicators**

| Element | Description |
|---------|-------------|
| **Low Oil Pressure** | Appears when oil pressure falls below threshold (engine running) |
| **High Coolant Temperature** | Appears when coolant temperature exceeds threshold |
| **Low Battery Voltage** | Appears when battery voltage falls below threshold |
| **Engine Overspeed** | Appears when RPM exceeds overspeed threshold |

**Actions (Buttons)**

| Element | Description |
|---------|-------------|
| **Start Engine** | Initiates the engine start sequence (pre-crank, cranking) |
| **Stop Engine** | Initiates controlled shutdown with cooldown period |
| **Emergency Stop** | Immediate engine shutdown (requires confirmation) |
| **Reset Fault** | Clears fault state and returns engine to stopped state (only visible during fault) |

**Mode Selector**

| Element | Description |
|---------|-------------|
| **Engine Mode** | Select operating mode: Manual (operator controlled), Auto (automatic start based on conditions), Off (engine disabled) |

<br/>

## How It Works

1. **Initialization** - On startup, the application initializes the UI components, sets up the engine state machine in "stopped" state, and begins reading configuration parameters.

2. **Main Loop (1-second cycle)** - Every second, the application reads engine parameters from the configured data source (simulator or hardware interface), evaluates alarm conditions against configured thresholds, and updates the state machine.

3. **State Machine Processing** - The engine state machine manages transitions between states: stopped, pre-crank (3s fuel priming), cranking (up to configured attempts), crank-rest (pause between attempts), running, cooling-down (controlled shutdown), and fault.

4. **Alarm Evaluation** - Engine parameters are continuously checked against thresholds. When running, low oil pressure, high temperature, and overspeed trigger fault states. Battery voltage is always monitored.

5. **UI Update** - All parameter displays are updated with current values, color-coded ranges reflect operating conditions, and warning indicators appear/hide based on active faults.

6. **Data Logging** - Engine data is published to the "engine_data" channel every loop cycle, including timestamp, state, all parameters, and any active faults for historical analysis.

<br/>

## Integrations

This device application works with:

- **DSE Engine Controllers** - Deep Sea Electronics engine control modules via Modbus or CAN communication
- **Engine Data Simulator** - Built-in simulator app for testing and development without physical hardware
- **Doover Channels** - Engine data is logged to channels for time-series analysis and dashboards
- **Doover Alerts** - Fault conditions and status changes can trigger platform alerts
- **External Monitoring Systems** - Data can be forwarded to SCADA, BMS, or other monitoring platforms via Doover integrations

<br/>

## Need Help?

- Email: support@doover.com
- [Doover Documentation](https://docs.doover.com)
- [App Developer Documentation](https://github.com/getdoover/dse-engine-controller/blob/main/DEVELOPMENT.md)

<br/>

## Version History

### v0.1.0 (Current)
- Initial release
- Real-time monitoring of RPM, oil pressure, coolant temperature, battery voltage, fuel level, and engine hours
- Color-coded parameter ranges for visual status indication
- Engine state machine with start/stop sequencing
- Configurable alarm thresholds for all parameters
- Fault detection with visual warnings and reset capability
- Manual, Auto, and Off operating modes
- Emergency stop with confirmation
- Data logging to Doover channels

<br/>

## License

This app is licensed under the [Apache License 2.0](https://github.com/getdoover/dse-engine-controller/blob/main/LICENSE).
