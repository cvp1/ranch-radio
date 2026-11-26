### Added

- Winlink over Mercury HF, the radio server's half (plan winlink-mercury M0; the module,
  installer and pages are M1): `--winlink-vara-modem mercury` puts Mercury behind Pat's
  `varahf`, keying through its own loopback lane (`--winlink-mercury-rigctl-port`, 33 s
  keepalive), which xara's and ARDOP's lanes refuse during a Mercury session and the reverse.
  Mercury sends no station ID, so the radio server sends one (`--winlink-mercury-call`):
  a CW ID at 18 WPM (never above 20), 750 Hz, at the session's Winlink codec level, at the
  end of the session and before 10 minutes, keyed only once Mercury's own key is released,
  never into a frame Mercury may still be playing. A Mercury key during the ID cuts it, the
  ID stays owed and the session ends; nothing is ever re-sent. The first trial caps a Mercury
  session at 8:30 (Pat aborted first, then the ID). Hosts without Mercury are unchanged.
