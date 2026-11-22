### Changed
- The code map in CLAUDE.md now covers every module and folder at the root. It had fallen 20 modules behind, including JS8, the DMR handhelds, the P2P directory and half of the logbook sync. A new test, `tests/code_map_test.py`, fails when a root module or folder is added without a map entry, or when the map names something that is gone.
