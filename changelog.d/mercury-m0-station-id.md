### Added

- Winlink over Mercury HF, the radio server's half (plan winlink-mercury M0; the module,
  installer and pages are M1): `--winlink-vara-modem mercury` puts Mercury behind Pat's
  `varahf`, keying through its own loopback lane (`--winlink-mercury-rigctl-port`, 33 s
  keepalive), which xara's and ARDOP's lanes refuse during a Mercury session and the reverse.
  Mercury sends no station ID, so the radio server sends one (`--winlink-mercury-call`):
  a CW ID at 18 WPM (never above 20), 750 Hz, at the session's Winlink codec level, at the
  end of the session and well before 10 minutes (the gate falls due at 8:20 so even the
  longest call behind Mercury's longest key ends by 9:50), keyed only once no Mercury frame
  may be playing. A Mercury key during the ID releases the ID first, then refuses Mercury:
  the ID stays owed and the session ends; nothing is ever re-sent. The first trial caps a
  Mercury session at 8:10 (Pat aborted first, then the ID). Hosts without Mercury behave as
  before, apart from the two fixes below.

### Fixed

- A Winlink lane (ARDOP, VARA HF, Mercury HF) no longer starts an ATU tune (`G TUNE`):
  the tune carrier went out with no armed-session check. Apps still tune through the main
  rigctl port.
- A key-down still waiting in the CI-V queue is dropped when a transmission is released
  without its owner asking (a Winlink disarm, the owner disconnecting, a keepalive or time
  limit), as STOP TX already did, so it cannot reach the radio after the release.
