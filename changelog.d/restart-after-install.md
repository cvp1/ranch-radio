### Fixed

- `ranch-radio install --apply` then `ranch-radio update --apply`, as install
  says to, now restarts a running station (or radio, or packet hub) whose
  files install changed. Before, update found every file already matching and
  restarted nothing, so an add-on added to the host file stayed unloaded.
- The commands install prints for macOS wait for launchd to let go of the
  old instance before starting the new one; run straight after the stop, the
  start failed with "Input/output error" and left the station down.
