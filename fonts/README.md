# Fonts

These load automatically at startup (any .ttf/.otf in this folder is loaded).
For the built .exe, keep this folder next to the .exe, like the data folder.

Bundled, all under the SIL Open Font License (see the matching *-OFL.txt):

- UnifrakturMaguntia — the "mournyl" wordmark (blackletter)
- IBM Plex Sans — UI text
- IBM Plex Mono — numbers

To swap a font, drop in another .ttf/.otf: a file whose family name contains
"fraktur" replaces the wordmark, "mono" the numbers, "plexsans" the text.
Otherwise the app falls back to fonts already on Windows.
