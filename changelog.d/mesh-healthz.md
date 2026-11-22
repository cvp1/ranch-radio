### Added

- **`/healthz` says when a Meshtastic node has been away.** The station's health answer was `ok` with nothing degraded whatever its add-ons were doing. A node that has been not connected for two minutes is now a line under `degraded` ("mesh LF not connected for 14 min: ...") and `ok` is false until it is back, so a monitor polling `/healthz` hears of it. A node's USB link here was out for 11 hours overnight and only the page knew. Switching Mesh off is not degraded. Any add-on can say the same of itself (`Addon.degraded`).
