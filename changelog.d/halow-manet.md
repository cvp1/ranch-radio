### Added

- The HaLow tab tracks an OpenMANET HaLow mesh (the halow block's `mesh`: its gate and the nodes behind it).
  Each node's own view is read over SSH, the others through the gate, so every link on the map shows how
  each end hears the other: green a node hears the gate, blue the gate hears it, purple both, labelled
  "it hears / gate hears" in dBm. Nodes further out carry their hops; links between other nodes are grey.
  The gate is this node on the map, and its badge says who hears it and the best dBm. A Mesh table lists
  each node; a node that leaves is noticed and kept on the map (`halow-mesh-seen.json`).
- The mesh takes its own SSH key (`mesh.key`): OpenMANET 1.8's dropbear accepts RSA keys only, and refuses
  the ed25519 key a bridge may use.
