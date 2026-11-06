# SWL Band Finder

A personal shortwave listening dashboard with band recommendations, an antenna setup calculator, formatted schedule cards, and a map of transmitters and approximate beam directions.

## Run on Windows

1. Install Python 3 if it is not already installed.
2. Double-click `Start-SWL-Band-Finder.bat`.
3. Leave the console window open while using the page. Press `Ctrl+C` in that window to stop the local helper.

The helper serves the page at `http://127.0.0.1:8765/` and opens it in your browser. It uses only Python's standard library. No Python packages need to be installed.

## What it does

- Estimates useful shortwave bands from local day/night, location, season, solar flux, K-index, and noise inputs.
- Shows antenna electrical length and an approximate resonance for your wire length, counterpoise, and feed choice (direct high-Z, direct 50 ohm, or 9:1 UnUn).
- Searches Short-Wave.Info by broadcast band, frequency, station, language, or transmitter site.
- Converts the schedule response into readable station cards with frequency, station, language, UTC hours, transmitter power, site, country flag, coordinates, azimuth, and distance.
- Calculates the initial bearing from each transmitter to the listener and compares it with the listed transmit azimuth.
- Filters schedule cards by transmitter country, on-air status, and whether the listed beam azimuth is within the assumed width toward your location. The shortlist is a useful starting point, not a reception guarantee.
- Shows a selectable, adjustable-width directional sector and great-circle path for each transmitter that has coordinates and a directional azimuth.

## Map and beam interpretation

The light sector on the map is a visualization based on a user-adjustable assumed beam angle and display reach. Short-Wave.Info publishes transmitter coordinates, power, and antenna azimuth for many schedule rows, but generally does not publish the specific antenna pattern or beamwidth. The sector is therefore an aiming aid, not a predicted radio-coverage boundary. Off-axis transmissions can still be received through side lobes, broad antenna patterns, and changing ionospheric paths. Actual reception also depends on frequency, schedule, path geometry, time, noise, receiver, and antenna.

The map uses OpenStreetMap tiles and requires an internet connection. Map tiles are attributed to OpenStreetMap contributors. Schedule lookups go to Short-Wave.Info. The receiver coordinates are used by the local page for bearing and distance calculations; the helper does not forward those coordinates to Short-Wave.Info.

## Privacy and network behavior

The server binds only to `127.0.0.1` (this computer). It has no login, telemetry, or data storage. It fetches public schedule pages from `www.short-wave.info` and extracts only schedule table fields. The browser loads Leaflet from unpkg and OpenStreetMap tiles for map display.


