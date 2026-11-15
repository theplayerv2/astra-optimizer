# Changelog

What changed in each version of Astra Optimizer. The newest version is at the top.

When a version is tagged on GitHub (for example `v1.2.0`), its section below becomes the release notes.

## v1.7.0 - 2026-11-16

### Added
- **Benchmark mode:** press **Ctrl+Alt+K** while playing (or use the tray menu or the overlay's right-click menu)
  for a fair FPS test. The overlay counts down 3 seconds, measures exactly 60 seconds of the game's own frames, then
  shows the average and 1% low. Each run is saved to the game's history as "Benchmark · Max FPS" (or whichever mode
  was applied), so **Compare** can put a run before a mode next to a run after it. Press Ctrl+Alt+K again to cancel;
  closing the game cancels it too.

### Changed
- A game's history counts sessions and benchmarks separately, and its average FPS comes from real sessions only.

## v1.6.0 - 2026-11-16

### Added
- **Slow background apps:** a new switch under Tools > Games. While a game runs, browsers (Chrome, Edge, Firefox,
  Brave, Opera, Vivaldi), game launchers (Steam's store window, Epic, Battle.net, EA app, Ubisoft Connect, GOG Galaxy)
  and cloud sync (OneDrive, Dropbox, Google Drive) drop to below-normal priority and Windows' efficiency mode, the
  same as Task Manager's. Everything goes back when the last game closes, when you turn the switch off or when Astra
  closes, and if Astra is closed mid-game the next start puts them back. Discord, Spotify, Teams and the Steam client
  itself are never touched, so voice, music and controller input stay smooth. Off by default.

### Changed
- The Games tab's bottom row now holds three switches: Auto-apply my mode, FPS overlay and Slow background apps.

## v1.5.0 - 2026-11-15

### Added
- **AMD driver settings:** on AMD Radeon graphics, Max FPS turns on AMD Anti-Lag and turns off Radeon Chill (which
  caps FPS to save power); Cozy Mode turns on Anti-Lag. They go through AMD's own ADLX library, the same settings AMD
  Software shows, and Undo puts back exactly what you had. If AMD Software has the newer in-driver Anti-Lag selected,
  Astra leaves it alone.
- **Crash catcher:** if something unexpected goes wrong, the full error goes into the log and a dialog offers
  **Report a problem** straight away (once per run). If Astra can't even open, it still logs the error and offers to
  save a report to your desktop.

### Changed
- The mode log has one "Graphics driver" section for NVIDIA and AMD, with a single line when neither is there.

## v1.4.0 - 2026-11-15

### Added
- **In-app updates:** when a new version is out, **Update to vN.N.N** on the About tab downloads it from this
  project's GitHub release, checks it against the SHA-256 checksum in the release notes, and restarts Astra on the new
  version. Settings, games and any applied mode carry over, and the old exe is cleaned up after the restart. A
  download that doesn't match its checksum is thrown away and nothing changes. Choose **Later** to finish the update
  whenever you like.
- **Report a problem:** one button on the About tab (also in the tray menu) saves a zip to your desktop with your PC
  specs, a fresh checkup, your last five game sessions and the app's log and settings, then shows it in Explorer
  ready to send.

### Changed
- The About tab links to the project's GitHub page.

## v1.3.0 - 2026-11-15

### Added
- **FPS measurement:** while a game from your list runs, Astra records its frame rate with Intel PresentMon (bundled,
  MIT license). It reads frame timing from Windows' own event tracing, so nothing is injected into the game and
  anti-cheat isn't involved. Session reports show average FPS, 1% low and stutter spikes.
- **FPS in session history:** a new FPS / 1% low column, and average FPS in the totals line.
- **Compare:** in a game's history, pick any two sessions to see average FPS, 1% low, stutter spikes per hour, GPU
  load, hottest temperature and ping side by side with the change between them. A note says when the sessions are
  too different to compare fairly.
- **Live overlay:** a slim one-line strip with live FPS, a frame-time graph where stutter shows as a spike, 1% low,
  GPU load and temperature, CPU and ping; values that need attention turn yellow. It opens when a game from your list
  starts and closes with it, Ctrl+Alt+F or the tray menu toggles it any time, and it never takes focus from the game.
  Drag it anywhere (it reopens in the same place) and lock it there. Right-click it to choose a Clear, Glass or Solid
  background (real transparency), Small or Large size, and which readings to show. Shows over borderless and windowed games.
- **NVIDIA driver settings:** Max FPS sets power management to "Prefer maximum performance" and Low Latency Mode to
  On; Cozy turns on Low Latency Mode. They're changed through NVIDIA's driver API (the same settings as the NVIDIA
  Control Panel's global profile) and Undo puts back your exact previous values.

### Changed
- The Games page's bottom row holds two switches: apply your mode while a game runs, and show the overlay.

### Fixed
- The taskbar showed a generic icon instead of Astra's; the window now carries its own icon and Windows groups
  and pins it as Astra Optimizer.
- Taskbars that read Windows' cached 256 px icon (such as Seelen UI) could keep showing an old build's icon; Astra
  now asks Windows to refresh its cached icon when it starts.

## v1.2.0 - 2026-11-15

### Added
- **Privacy tab** with six switches for ads and tracking built into Windows: ads and suggested apps in Start and
  Settings, Bing results in Start search, the advertising ID, tips and lock screen fun facts, diagnostic data (down to
  the required minimum) and activity history. Each switch saves the exact values it replaces and puts them back when
  turned off.
- **Five new checkup rows:** graphics chip for games (laptops with two GPUs), graphics card slot speed, gaming
  features (Game Mode, windowed-game optimizations, variable refresh rate, GPU scheduling), Memory Integrity (explained
  and linked, never changed for you), and storage (free space and TRIM). Several have a one-click fix.
- **Checkup score** above the list, for example "8 of 10 look good". The list scrolls with the mouse wheel.
- **Optimizations for windowed games and variable refresh rate** are turned on by Max FPS and Cozy, and undone with
  the rest of the changes.
- **Fast GPU for games on laptops** with two graphics chips: games you add start on the strong GPU. Removing a game
  removes the setting.
- **Session history per game:** click a game on the Games page to see all its sessions with averages, and open any
  of them.
- **Settings backup:** Back up and Restore on the Settings tab move your settings, games, appearance and session
  history to another PC in one file.
- **More cleanup:** Discord's cache, used Windows Update downloads and the Delivery Optimization cache, plus an
  optional weekly cleanup that never runs while a game is open.
- **Power throttling off for games:** listed games are kept out of Windows' power-saving mode (EcoQoS) while they run.
- Releases are now built by GitHub Actions straight from the tagged source, with a SHA-256 checksum in the notes.

### Changed
- The side rail has six tabs; Privacy sits under Tools.
- Settings' last row is now "Your data", with Back up, Restore and Open folder.
- Up to 100 game sessions are kept (was 40).

## v1.1.0 - 2026-11-15

### Added
- **Gaming checkup** on the Tools tab: refresh rate below the monitor's maximum (one-click fix), RAM below its rated
  speed (XMP/EXPO off), an old graphics driver, Wi-Fi or a slow cable link, and a laptop on battery.
- **Game session reports:** CPU and GPU load, the hottest GPU temperature, memory and ping are recorded while a game
  from your list runs, and summarised when it closes.

### Changed
- Tools opens on the Checkup page, and its page switcher was widened to fit seven pages.

## v1.0.1 - 2026-11-15

### Added
- **Update check:** Astra asks GitHub for a newer release at startup and every 12 hours, and the About tab shows a
  Download button when one exists. It only checks; nothing downloads or installs on its own. It can be turned off in
  Settings.

## v1.0.0 - 2026-11-15

First public release.

- Three modes (Max FPS, Cozy Mode, Best Looking) with an exact Undo of every change.
- Tools: ping tester, games (high priority, fullscreen optimizations, automatic mode), startup apps, temp file
  cleanup, DNS speed test and switch, and live monitor graphs.
- Appearance: starfield, black or your own picture as the background, with blur, dim, panel opacity, frost and motion
  settings.
- Tray icon, start with Windows, the Ctrl+Alt+O hotkey and an optional restore point before optimizing.
