# SWL Band Finder

A personal shortwave listening dashboard with band recommendations, an antenna setup calculator, formatted schedule cards, and a map of transmitters and approximate beam directions.

## Run on Windows

1. Install Python 3 if it is not already installed.
2. Double-click `Start-SWL-Band-Finder.bat`. It stops an existing SWL helper on ports 8765 or 9080 before starting the new one.
3. Leave the console window open while using the page. Press `Ctrl+C` in that window to stop the local helper.

The helper serves the page at `http://127.0.0.1:9080/` and opens it in your browser. It uses only Python's standard library. No Python packages need to be installed. On first launch it creates `schedule.sqlite3` beside the app and refreshes the full Shortwave.am and Shortwave.live schedules. The database stays on your computer; it is not included in GitHub.

## What it does

- Estimates useful shortwave bands from local day/night, location, season, solar flux, K-index, and noise inputs, then blends propagation (58%) with a coarse antenna electrical-length/feed proxy (42%) for a separate overall ranking. It also labels the propagation-only and antenna-model picks.
- Shows current local weather from OpenWeather when an API key is configured. Thunderstorms add a modest low-band reception/noise penalty; weather is not treated as an ionospheric propagation or signal-strength forecast.
- Shows wire length in wavelengths by band and rule-of-thumb quarter-/half-wave lengths for the entered wire length. The antenna score is not an impedance, matching, or signal-strength calculation: layout, height, counterpoise/ground, receiver, and local noise matter. A 9:1 UnUn does not make every wire a perfect 50-ohm match.
- Searches your local SQLite schedule database by broadcast band, frequency, station, language, or transmitter site. Matching Short-Wave.Info results are cached locally when available.
- Converts schedule records into readable station cards with frequency, station, language, UTC hours and days, target area, transmitter power, site, country, coordinates, azimuth, distance, and source attribution.
- Shows a flag and transmitter/relay country when the schedule source identifies it. This is the transmitting site’s country and may differ from the broadcaster’s home country.
- Calculates the initial bearing from each transmitter to the listener and compares it with the listed transmit azimuth.
- Filters schedule cards by transmitter country, on-air status, and whether the listed beam azimuth is within the assumed width toward your location. The shortlist is a useful starting point, not a reception guarantee.
- Shows a selectable, adjustable-width directional sector and great-circle path for each transmitter with coordinates; the sector appears only when the source provides a directional azimuth.
- Refreshes the two complete schedule feeds automatically when the cache is missing or older than 12 hours. Use **Update schedule data** to refresh them manually.

## Map and beam interpretation

The light sector on the map is a visualization based on a user-adjustable assumed beam angle and display reach. Short-Wave.Info publishes transmitter coordinates, power, and antenna azimuth for many schedule rows, but generally does not publish the specific antenna pattern or beamwidth. The sector is therefore an aiming aid, not a predicted radio-coverage boundary. Off-axis transmissions can still be received through side lobes, broad antenna patterns, and changing ionospheric paths. Actual reception also depends on frequency, schedule, path geometry, time, noise, receiver, and antenna.

The map uses OpenStreetMap tiles and requires an internet connection. Map tiles are attributed to OpenStreetMap contributors. Shortwave.am and Shortwave.live provide full schedule feeds to the local SQLite cache. Short-Wave.Info has no public full-database export in the inspected interface, so its results are fetched per search, cached locally, and subject to its rate limits. Receiver coordinates are used locally for bearing and distance calculations; they are not sent to any schedule source.

## Privacy and network behavior

The server binds only to `127.0.0.1` (this computer). The SQLite database stores public schedule rows locally and is ignored by Git. It has no login or app telemetry. The browser loads Leaflet from unpkg and OpenStreetMap tiles for map display.


## OpenWeather setup

Copy `.env.example` to `.env`, then set `OPENWEATHER_API_KEY` to your key. The `.env` file is ignored by Git; the browser calls the local Python helper, which keeps the key out of browser code and URLs. This integration uses OpenWeather One Call API 4.0, which requires the matching One Call subscription. Weather refreshes on startup, when receiver coordinates change, and every 15 minutes.

