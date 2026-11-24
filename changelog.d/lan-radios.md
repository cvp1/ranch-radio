### Added

- A station can use the radios on other Ranch Radio stations on the LAN. List them under `station.peers` in the host file (each with its certificate pinned; `ranch-radio peer-pin URL` prints the pin). Their radios join the rig bar and radio list as `<radio>@<station>`, with their full page, audio and controls passed through that station. Their own station still keys them, and STOP TX all HF reaches them. They are never this station's shared rigctl port, Winlink or JS8 radio.
