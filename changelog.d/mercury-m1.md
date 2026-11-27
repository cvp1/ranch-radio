### Added

- Winlink over Mercury HF on a Mac, as a module: `ranch-radio add mercury`
  sets up `winlink.mercury` (its binary, callsign, sound card, ports 8340/8342
  and its lane 4535). Doctor prints how to build Mercury v1.9.25 with this
  repo's patch, which keeps its ports on this machine only.
- `ranch-radio vara-backend xara|mercury` switches the modem behind Pat's
  VARA HF. It prints the plan; `--apply` runs it on idle radios only: the old
  modem's agent stops and its plist goes to the Trash (dated), the new one
  loads and must be listening, and the radio server restarts. New Winlink
  sessions are refused while it runs, and a radio that doesn't answer counts
  as busy. If Pat's settings don't name the new modem yet, it stops there,
  prints the two edits, and `--apply --finish` restarts the station (with
  Pat) once they are made.
- Before a Mercury session is armed, and again just before it is, the radio
  server checks that one Mercury process listens on 127.0.0.1 only, on exactly
  its ports, and refuses the session otherwise. Mercury's lane keys only for
  that process; a Mercury replaced mid-session ends the session. Doctor reports the same, plus Mercury's version and patch, its
  agent, Pat's `varahf.addr` and whether Pat's `service_codes` list `MERCURY`.
- The page names the modem: "Mercury HF" or "VARA HF (xara)" on a host with
  Mercury set up (other hosts are unchanged). The gateway list for Mercury
  says that a gateway's service code is not known and that any of them may be
  VARA only.
