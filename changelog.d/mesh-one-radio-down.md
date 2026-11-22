### Fixed

- **Mesh: one node down no longer reads as Mesh being down.** On a station with more than one Meshtastic node, any node's "not connected" set the whole tab's state: a node whose USB cable was out overnight said so each minute its bridge retried, and the bar dropped the Mesh tab and Mission Control listed "Offline: Mesh" while the other node carried on. Mesh is now up while any of its nodes is; with one down, the tab's dot and the card turn amber and name it ("LF not connected"), and "Offline" lists that node ("Mesh · LF").
