"""Ranch Radio: every radio, service and add-on on the ranch, shipped from one place.

The runtime is unchanged: server.py runs one radio, station.py puts a Linux
host's radios on one page, add-ons plug into either. What this package adds
is the shipping layer that used to be hand-copied per host:

    hosts/<host>.json  --render-->  launchd plists (macOS) or systemd units
                                    (Linux), plus station.json

One host file holds every fact about that host's radios (device, ports,
sound card, Winlink lane, mesh node). A block present in it is a module
switched on; delete the block and the module is gone from that host.

    ./ranch-radio modules            what exists, where it runs, how it ships
    ./ranch-radio hosts              which host runs which module
    ./ranch-radio render HOST DIR    write HOST's files into DIR (no install)
    ./ranch-radio drift [HOST]       rendered vs installed on this machine
    ./ranch-radio install [HOST]     show what would change; --apply writes it
    ./ranch-radio doctor [HOST]      what its modules need, and whether this machine has it
    ./ranch-radio add MODULE         switch a module on in the host file (--set, --apply)
    ./ranch-radio remove MODULE      switch it off (refused while another module leans on it)

What each module needs (its block, template, dependencies, the binaries and
Python packages it runs) is declared once, in ranch_radio/manifest.py.

Nothing here starts, stops or reloads a service: install --apply writes the
files (old ones kept as .bak-<timestamp>) and prints the reload commands.
"""

__version__ = "0.1.0"
