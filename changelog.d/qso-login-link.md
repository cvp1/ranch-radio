### Fixed

- QSO tab: the "QRZ login" link did nothing. While the station has no working QRZ login the form is already open, so the link is now hidden then; once logged in it reads "Change QRZ login" and opens the form with the cursor in the password box. Enter in the password box saves it.
- QSO tab: a correct QRZ password was reported as refused. QRZ reads credentials only from a form POST and answered every GET login with "Username / password required"; logins and lookups are now POSTs (which also keeps the password out of any URL). When QRZ refuses a login, the tab shows QRZ's own words.
