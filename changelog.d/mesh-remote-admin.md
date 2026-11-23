### Added

- Remote admin of another Meshtastic node through the station's node: `mesh_remote_get`, `mesh_remote_set` and `mesh_remote_reboot` (bridge commands `remote_get`, `remote_set`, `remote_reboot`). The far node must list the station node's public key as an admin key; messages are PKI-encrypted, one job runs at a time, and each step times out. Only the settings the Mesh tab already edits can be read or written (not the owner, security, Wi-Fi or keys). A set reads the section fresh first, so fields it does not name are kept, and is reported saved only when the node reads the new value back after it restarts. No Mesh tab controls yet.

### Security

- The MQTT feed no longer publishes admin packets: a remote node's admin answer carries a session passkey and its settings, and they stay between the two nodes.
