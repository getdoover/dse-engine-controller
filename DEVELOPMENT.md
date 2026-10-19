# Doover Application Template

This repository serves as a template for creating Doover applications.

It provides a structured layout for application code, deployment configurations, simulators, and tests. The template is
designed to simplify the development and deployment of Doover-compatible applications.

The basic structure of the repository is as follows:

## Getting Started

```
README.md             <-- App description
DEVELOPMENT.md        <-- This file
pyproject.toml        <-- Python project configuration file (including dependencies)
Dockerfile            <-- Dockerfile for building the application image
doover_config.json    <-- Generated config schema consumed by Doover

src/app_template/     <-- Application package
  __init__.py         <-- Entry point: run_app(SampleApplication())
  application.py      <-- Main application code (setup, main_loop, UI handlers)
  app_config.py       <-- Config schema definition
  app_tags.py         <-- Runtime state tag declarations
  app_ui.py           <-- UI definition
  app_state.py        <-- State machine (optional)

simulators/
  app_config.json     <-- Sample configuration injected into the app when running locally
  docker-compose.yml  <-- Docker Compose stack (device agent + simulator + app)
  dse/                <-- DSE E400 GenComm simulator image (model.py + main.py)

tests/
  test_imports.py     <-- Smoke tests for the application
```

The `doover_config.json` file is the Doover configuration file for the application.

It defines all metadata about the application, including name, short and long description,
dependent apps, image name, owner organisation, container registry and more. Two sections —
`config_schema` and `ui_schema` — are generated from Python. Do not edit them by hand.
Regenerate after any change to `app_config.py` or `app_ui.py`:

```bash
uv run export-config   # writes the config_schema block
uv run export-ui       # writes the ui_schema block (required for publishing)
```

The app will fail to publish if `ui_schema` is missing, so make sure both are run and
committed after editing the UI.

### Prerequisites

- Docker and Docker Compose
- Python 3.11 or later (if running locally)
- [uv](https://docs.astral.sh/uv/) for managing Python dependencies
- The Doover CLI (`doover`)

### Running Locally

Run the application alongside the sample simulator:

```bash
doover app run
```

This runs `docker compose up` in `simulators/`.

## Simulators

The `simulators/` directory contains tooling for running the application without real hardware:

- `app_config.json`: Sample configuration injected into the app at startup via `CONFIG_FP`.
- `docker-compose.yml`: Defines the device agent, simulator, and application services.
- `dse/`: A register-accurate DSE E400 simulator. `model.py` is the pure engine/controller model (no pymodbus, so `tests/test_simulator.py` drives it directly); `main.py` serves it over Modbus TCP on port 5020 and exposes `sim_*` control tags through the device agent.

## Testing

Run the test suite:

```bash
uv run pytest tests -v
```

## Publishing

Once your app is ready to publish:

```bash
doover app publish --profile dv2
```

## Customisation

To make this template your own:

1. Rename the `src/app_template/` package and update `pyproject.toml` scripts.
2. Update `app_config.py`, `app_tags.py`, `app_ui.py`, and `application.py` to match your domain.
3. Adjust the simulator under `simulators/dse/` if the module's behaviour needs modelling differently.
4. Regenerate `doover_config.json` with `uv run export-config && uv run export-ui`.
