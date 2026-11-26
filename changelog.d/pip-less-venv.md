### Fixed

- `ranch-radio update` and `doctor --fix` install into a venv that has no
  pip (one made by `uv venv`) through uv, instead of failing with "No module
  named pip" the first time a requirement changes. With neither pip nor uv
  they say so, and how to add either.
