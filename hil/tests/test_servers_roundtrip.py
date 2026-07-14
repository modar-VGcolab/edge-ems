"""End-to-end Modbus round-trip: the real ModbusTcpAdapter reads/writes a
map-generated HIL server (parity holds on the wire, not just the datastore)."""
import asyncio
from pathlib import Path

from common.data_model import DataModel
from common.register_map import load_register_map
from core.adapters.modbus_tcp import ModbusTcpAdapter

from hil.plant.models import SiteModel
from hil.servers import HilModbusServer

_REPO = Path(__file__).resolve().parents[2]


def test_adapter_reads_and_writes_generated_server():
    async def _run():
        dm = DataModel.load(_REPO / "data_model.yaml")
        rmap = load_register_map(_REPO / "maps" / "custom_bess_v1.yaml", dm)
        site = SiteModel()
        site.battery.soc_pct = 42.0
        srv = HilModbusServer(rmap, site, host="127.0.0.1", port=15555)
        srv.push_inputs()
        task = asyncio.create_task(srv.serve())
        await asyncio.sleep(0.8)
        a = ModbusTcpAdapter("127.0.0.1", rmap, port=15555)
        await a.connect()
        vals = await a.read_points(["soc_pct", "active_power_kw"])
        assert vals["soc_pct"].quality == "GOOD"
        assert abs(vals["soc_pct"].value - 42.0) < 0.2
        # write a setpoint and confirm the plant receives it
        await a.write_points({"active_power_setpoint_kw": -250.0})
        srv.pull_setpoints()
        assert abs(site.battery._p_setpoint - (-250.0)) < 0.2
        await a.disconnect()
        task.cancel()

    asyncio.run(_run())
