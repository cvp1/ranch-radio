### Fixed

- Mercury HF's station ID: while a collision's unkey is still on its way to a CI-V radio,
  every refusal on Mercury's lane waits for it (a second connection too, and after the
  session was disarmed), not only the first; a later session is never latched by an
  earlier collision's timeout; and the station binds its end-of-session wait on the first
  armed message of a session, so a later message cannot lengthen it.
