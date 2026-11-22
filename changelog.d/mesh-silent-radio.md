### Changed
- `/healthz` now lists a Meshtastic node that is connected but has passed on no packet for half an hour (`MESH_SILENT_S`, default 1800) as degraded, as it already did for a node that is not connected. A Bluetooth link can connect, load the node's settings and then deliver nothing: on 2026-11-22 one did so for five hours while `/healthz` said ok.
