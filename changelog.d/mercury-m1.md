### Added

- Winlink over Mercury HF on a Mac, as a module: `ranch-radio add mercury`
  sets up `winlink.mercury` (its binary, callsign, sound card, ports 8340/8342
  and its lane 4535). Doctor prints how to build Mercury v1.9.25 with this
  repo's patch, which keeps its ports on this machine only.
- `ranch-radio vara-backend xara|mercury` switches the modem behind Pat's
  VARA HF. It prints the plan; `--apply` runs it on idle radios only: the old
  modem's agent stops and its plist goes to the Trash (dated), the new one
  loads, the radio server and the station (with Pat) restart, and it prints
  the two Pat settings to change by hand.
- Before a Mercury session is armed, the radio server checks that Mercury
  listens on 127.0.0.1 only, on exactly its ports, and refuses the session
  otherwise. Doctor reports the same, plus Mercury's version and patch, its
  agent, Pat's `varahf.addr` and whether Pat's `service_codes` list `MERCURY`.
- The page names the modem: "Mercury HF" or "VARA HF (xara)" on a host with
  Mercury set up (other hosts are unchanged). The gateway list for Mercury
  says that a gateway's service code is not known and that any of them may be
  VARA only.
