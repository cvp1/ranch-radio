### Fixed

- The radio no longer snaps back when you turn the knob while only a listening
  app is connected. The hold (a change at the radio is put back while an app
  like WSJT-X or JTDX is connected) now applies only while an app that actually
  set the frequency, mode or DATA is still connected. An app that only listens,
  such as the JS8 engine, never pins the radio, and a change made on the page
  alone never starts a hold. The hold ends when the app that set it leaves.

### Added

- A HELD badge on the radio page while an app holds the radio. Click it to
  release: the radio stays where you put it until an app sets it again.
