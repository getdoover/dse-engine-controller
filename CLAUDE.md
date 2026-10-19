# Doover App Template

A template for building device applications on the Doover IoT platform using pydoover 1.0.

## Commands

```bash
uv run pytest tests -v          # Run tests
uv run export-config             # Write config_schema into doover_config.json
uv run export-ui                 # Write ui_schema into doover_config.json (required to publish)
doover app run                   # Run app + simulator locally via docker-compose
```

## Project Structure

```
src/dse_engine_controller/
  __init__.py        # Entry point — run_app(DseEngineControllerApplication())
  application.py     # Main app class (setup, main_loop, UI handlers, polling, commands)
  gencomm.py         # DSE GenComm register map: pages, sentinels, alarms, control keys, READ_PLAN
  dse_state.py       # DseSnapshot / DseStateStore — decoded view the rest of the app consumes
  app_config.py      # Config schema — class-level declarations
  app_tags.py        # Runtime state tags — bound to UI elements
  app_ui.py          # UI definition — subclasses ui.UI
  app_state.py       # Derived display state
  autostart.py       # Pre-start warning / start / stop state machine (panel-agnostic)
  run_modes.py       # Fixed / Regulate / Maintain Value run modes and transitions
  regulator.py       # PI loop for Regulate modes
  sensors.py, analog.py, outputs.py  # Doover-side sensors and digital outputs
simulators/dse/      # DSE E400 GenComm Modbus TCP simulator (model.py is pure; main.py wires pymodbus + pydoover)
tests/               # pytest suite
```

## pydoover 1.0 Patterns

This app uses the pydoover 1.0 declarative API. Key patterns:

### Application class (application.py)
- Set `config_cls`, `tags_cls`, `ui_cls` as class attributes — framework wires them up automatically
- Override `async def setup()` for init and `async def main_loop()` for the periodic loop
- Use `@ui.handler("element_name")` for UI interaction callbacks (signature: `self, ctx, value`)
- Access config via `self.config.<field>.value`, tags via `self.tags.<name>.set(val)` / `.get()`
- Cross-app tags: `self.get_tag("tag_name", app_key)`
- Messaging: `await self.create_message(channel, {data})`

### Config (app_config.py)
- Subclass `config.Schema` with class-level `config.Boolean`, `config.String`, `config.Application`, etc.
- `export()` is a classmethod: `SampleConfig.export(path, name)`

### Tags (app_tags.py)
- Subclass `Tags` with class-level `Tag("type", default=...)` declarations
- Types: "boolean", "number", "integer", "string", "array", "object"

### UI (app_ui.py)
- Subclass `ui.UI` with class-level element declarations
- Bind variables to tags: `ui.NumericVariable("Label", value=MyTags.field, name="id")`
- Element types: `BooleanVariable`, `NumericVariable`, `TextVariable`, `Button`, `TextInput`, `FloatInput`, `Select`, `Submodule`
- Use explicit `name=` kwarg on interactive elements to match handler names

### State Machine (app_state.py)
- Uses `pydoover.state.StateMachine` (wraps the `transitions` library)
- Define `states` and `transitions` as class attributes, `on_enter_<state>()` callbacks

## Doover Skills

If you have the doover-skills plugin installed, use `/doover` to see all available skills.
Key skills: `/doover-device-apps` for device app development, `/pydoover` for API reference.
