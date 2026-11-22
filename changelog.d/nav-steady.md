### Changed
- Station header: the open section (Radios, Messaging, Mesh, Tools) shows its members in a row under the bar, one click each, so moving from Map to Roster no longer goes through the chevron menu.

### Fixed
- Station header: the bar was redrawn several times a second (every add-on event, the JS8 waterfall among them), so a click could land on a tab that was replaced mid-press and do nothing, and an open menu flickered. The bar, the phone's bottom bar and the section row now redraw only when what they show changes; Mission Control's cards redraw at most three times a second on add-on events.
