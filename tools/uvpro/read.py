"""Read-only dump of a UV-PRO over BLE (benlink). No set_* calls anywhere."""
import asyncio
import json
import sys

from benlink.controller import RadioController


def plain(m):
    d = m.model_dump() if hasattr(m, "model_dump") else dict(m)
    return {k: (v if isinstance(v, (bool, int, float, str)) or v is None else str(v)) for k, v in d.items()}


async def main(addr):
    r = RadioController.new_rfcomm(addr, int(sys.argv[2])) if len(sys.argv) > 2 else RadioController.new_ble(addr)
    await asyncio.wait_for(r.connect(), 60)
    out = {"device": plain(r.device_info), "settings": plain(r.settings), "beacon": plain(r.beacon_settings),
           "status": plain(r.status), "channels": [plain(c) for c in r.channels]}
    for k, f in (("battery_pct", r.battery_level_as_percentage), ("battery_v", r.battery_voltage)):
        try:
            out[k] = await asyncio.wait_for(f(), 5)
        except Exception as e:
            out[k] = f"err {type(e).__name__}"
    await r.disconnect()
    print(json.dumps(out))


asyncio.run(main(sys.argv[1]))
