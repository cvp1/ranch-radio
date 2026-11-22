### Fixed

- `ranch-radio init` (and so `install.sh`) on a Mac no longer stops with a `UnicodeDecodeError` when some USB device reports a name that is not UTF-8: the port search read `ioreg`'s whole listing as strict UTF-8, and one stray byte from any device ended it before it had looked at a port.
