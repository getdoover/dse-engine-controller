"""DSE E400 engine controller Modbus TCP simulator (GenComm).

Serves the GenComm register map the dse-engine-controller app reads and
writes (``src/dse_engine_controller/gencomm.py`` is the contract; the
register constants are duplicated in ``model.py`` because this image doesn't
contain the app package) so the app can be exercised without a real module.

The engine/controller behaviour lives in ``model.py`` (``E400Model``), which
has no pymodbus or pydoover dependency so the app's tests can drive it with a
fake clock. This file wires it to:
  * a pymodbus 3.9.2 async TCP server, through a slave context that delegates
    holding-register reads/writes straight to the model, and
  * the doover device agent, through a pydoover Application whose main loop
    ticks the model and exposes sim-control tags.

Addressing: the app reads GenComm page P offset O at holding-register PDU
address ``P * 256 + O`` (e.g. 1029 = page 4 offset 5). pymodbus 3.9.2's stock
``ModbusSlaveContext`` adds 1 to every address before hitting its data block;
the context here is built on ``ModbusBaseSlaveContext`` instead and passes the
PDU address through unchanged, so there's no off-by-one to compensate for.

Sim-control via doover tags (set from the device agent):
  * ``sim_command`` -- one of
      ``start``               remote start input active (acts in Auto mode)
      ``stop``                remove all start requests (Manual: stop now)
      ``shortcut_to_running`` jump straight to running in Auto
      ``inject_alarm``        raise ``sim_alarm_name`` with ``sim_alarm_condition``
      ``clear_alarm``         clear ``sim_alarm_name``'s condition (all if empty);
                              lockouts stay latched until a reset key / Stop mode
      ``set_mode``            select ``sim_mode`` (stop / auto / manual)
  * ``sim_alarm_name`` -- any page 154 / page 153 alarm name from gencomm.py,
    e.g. "Low oil pressure", "Over speed", "Fail to start".
  * ``sim_alarm_condition`` -- warning / shutdown / controlled shutdown /
    electrical trip.
  * ``sim_mode`` -- target for ``set_mode``; also republished each loop as a
    readback of the current mode, so set it together with ``sim_command``.
  * ``sim_maintain_enabled`` -- True: in Auto the Maintain Value PID drives
    the engine; False: the engine tracks the target speed register.
  * ``sim_flex_a_kpa`` / ``sim_flex_b_c`` / ``sim_flex_c_pct`` -- override
    flexible sensors A/B/C (null = derived from the model).
Readback tags published each loop: ``sim_state``, ``sim_rpm``, ``sim_mode``,
``sim_alarms``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from pydoover.docker import Application, run_app
from pydoover.tags import Tag, Tags
from pymodbus.datastore import ModbusServerContext
from pymodbus.datastore.context import ModbusBaseSlaveContext
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.pdu import ExceptionResponse
from pymodbus.server import StartAsyncTcpServer

from model import E400Model, Mode

log = logging.getLogger(__name__)

HOLDING_FCS = (3, 6, 16, 23)
INPUT_FCS = (4,)


class GenCommContext(ModbusBaseSlaveContext):
    """A slave context whose holding registers are an ``E400Model``.

    Reads of anything the model doesn't implement come back as 0xFFFF
    (GenComm's unimplemented sentinel), never as an exception. A write the
    model rejects (bad control-key complement, unsupported key, read-only
    register, out-of-range target speed) answers ILLEGAL_VALUE.
    """

    def __init__(self, model: E400Model):
        self.model = model

    def reset(self):
        pass

    def getValues(self, fc_as_hex, address, count=1):
        if fc_as_hex in HOLDING_FCS or fc_as_hex in INPUT_FCS:
            return self.model.read(address, count)
        return ExceptionResponse.ILLEGAL_ADDRESS

    def setValues(self, fc_as_hex, address, values):
        if fc_as_hex not in HOLDING_FCS:
            return ExceptionResponse.ILLEGAL_ADDRESS
        ok = self.model.write(address, list(values))
        return None if ok else ExceptionResponse.ILLEGAL_VALUE


def build_server_context(model: E400Model) -> ModbusServerContext:
    # single=True: answer whatever unit id the client uses (the app is
    # configured with MODBUS_SLAVE_ID, default 10, like a real E400).
    return ModbusServerContext(slaves=GenCommContext(model), single=True)


def build_identity() -> ModbusDeviceIdentification:
    return ModbusDeviceIdentification(
        info_name={
            "VendorName": "Doover",
            "ProductCode": "DSESIM",
            "VendorUrl": "https://doover.com",
            "ProductName": "DSE E400 Simulator",
            "ModelName": "E400 Simulator",
            "MajorMinorRevision": "1.0.0",
        }
    )


async def serve(model: E400Model, host: str, port: int):
    log.info("Starting DSE E400 modbus TCP simulator on %s:%s", host, port)
    return await StartAsyncTcpServer(
        context=build_server_context(model),
        identity=build_identity(),
        address=(host, port),
        framer="socket",
    )


class DseSimulatorTags(Tags):
    sim_command = Tag("string", default=None)
    sim_alarm_name = Tag("string", default=None)
    sim_alarm_condition = Tag("string", default="shutdown")
    sim_mode = Tag("string", default="stop")
    sim_maintain_enabled = Tag("boolean", default=True)
    sim_flex_a_kpa = Tag("number", default=None)
    sim_flex_b_c = Tag("number", default=None)
    sim_flex_c_pct = Tag("number", default=None)
    sim_state = Tag("string", default="")
    sim_rpm = Tag("number", default=0)
    sim_alarms = Tag("string", default="")


class DseSimulator(Application):
    """A simulated DSE E400 controller that exposes itself over modbus TCP."""

    tags_cls = DseSimulatorTags

    async def setup(self):
        self.host = os.environ.get("MODBUS_HOST", "0.0.0.0")
        self.port = int(os.environ.get("MODBUS_PORT", "5020"))
        self.slave_id = int(os.environ.get("MODBUS_SLAVE_ID", "10"))

        self.model = E400Model(clock=time.monotonic)
        self.modbus_task = asyncio.create_task(serve(self.model, self.host, self.port))
        self.loop_target_period = 0.5

    # ------------------------------------------------------------------
    # Sim controls (via doover tags)
    # ------------------------------------------------------------------

    async def _process_sim_tags(self) -> None:
        self.model.maintain_enabled = bool(
            True if self.tags.sim_maintain_enabled.get() is None else self.tags.sim_maintain_enabled.get()
        )
        self.model.flex_a_kpa = self.tags.sim_flex_a_kpa.get()
        self.model.flex_b_c = self.tags.sim_flex_b_c.get()
        self.model.flex_c_pct = self.tags.sim_flex_c_pct.get()

        cmd = self.tags.sim_command.get()
        if not cmd:
            return
        try:
            log.info("Sim command: %s", cmd)
            if cmd == "start":
                self.model.sim_start()
            elif cmd == "stop":
                self.model.sim_stop()
            elif cmd == "shortcut_to_running":
                self.model.shortcut_to_running()
            elif cmd == "inject_alarm":
                name = self.tags.sim_alarm_name.get() or "Low oil pressure"
                condition = self.tags.sim_alarm_condition.get() or "shutdown"
                self.model.inject_alarm(name, condition)
            elif cmd == "clear_alarm":
                self.model.clear_alarm(self.tags.sim_alarm_name.get() or None)
            elif cmd == "set_mode":
                self.model.set_mode(self.tags.sim_mode.get() or "stop")
            else:
                log.warning("Unknown sim command: %s", cmd)
        except (ValueError, KeyError) as exc:
            log.warning("Sim command %s failed: %s", cmd, exc)
        finally:
            await self.tags.sim_command.set(None)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def main_loop(self):
        if self.modbus_task.done():
            raise RuntimeError("Modbus server task exited unexpectedly")

        await self._process_sim_tags()
        self.model.tick()

        await self.tags.sim_state.set(self.model.phase.value)
        await self.tags.sim_rpm.set(round(self.model.rpm, 1))
        await self.tags.sim_mode.set(Mode(self.model.mode).name.lower())
        await self.tags.sim_alarms.set(self.model.alarm_summary())


def main():
    run_app(DseSimulator())


if __name__ == "__main__":
    main()
