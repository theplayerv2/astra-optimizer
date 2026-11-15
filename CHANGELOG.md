# Changelog

What changed in each version of Astra Optimizer. The newest version is at the top.

When a version is tagged on GitHub (for example `v1.2.0`), its section below becomes the release notes.

## v1.3.0 - Unreleased

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
