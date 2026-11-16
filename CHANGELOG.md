# Changelog

What changed in each version of Astra Optimizer. The newest version is at the top.

When a version is tagged on GitHub (for example `v1.2.0`), its section below becomes the release notes.

## v1.8.1 - 2026-11-17

### Changed
- **Fewer false antivirus alarms:** the exe now carries its own version details (product, description, version
  and copyright, shown under Properties > Details), and releases use a launcher compiled from PyInstaller's
  source instead of the ready-made one shared by countless other programs. Some antivirus engines' machine
  learning flagged v1.8.0 for looking like other unsigned PyInstaller apps; nothing in Astra itself changed.

## v1.8.0 - 2026-11-16

### Added
- **What's holding your game back:** the overlay now says whether the game is **GPU-bound**, **CPU-bound** or
  **Capped** (an FPS cap or V-Sync is setting the pace) right now, and each session report and benchmark shows how
  much of the time each one set the pace, for example "Held back by: the GPU 78% of the time, the CPU 15%". When one
  clearly dominates, the report says what would actually raise FPS: lower graphics settings when GPU-bound, Max FPS
  and lighter CPU settings when CPU-bound. It comes from PresentMon's GPU and CPU busy times for every frame, so
  nothing new runs in the game. A very steady frame rate counts as capped, since in-game limiters (like CS2's
  menu cap) wait in a way that otherwise looks like CPU work. The overlay's right-click menu can hide it.

- **Four more switches in Settings > Windows**, each putting back exactly what it replaced when switched off:
  - **Mouse pointer speed 1:1:** the middle notch (6 of 11), where one count from your mouse moves the pointer one
    pixel.
  - **Turn off the Sticky Keys pop-ups:** the same shortcuts Max FPS turns off (Shift five times, holding right
    Shift or Num Lock), as a lasting choice. Switching it here wins over a later Undo of a mode.
  - **Stop sharing updates with other PCs:** Windows stops uploading update files to other PCs over your connection
    (Delivery Optimization), which can raise ping. Updates still download.
  - **Turn off Fast Startup:** shutting down fully restarts Windows and its drivers instead of half-hibernating.
    Booting takes a few seconds longer.

### Changed
- **Appearance moved into Settings**, as a third page next to App and Windows. The side rail has one tab fewer.
- The sliders' knob is a plain glowing dot; the four faint spikes around it are gone.

### Fixed
- After changing a game's video settings, the overlay's FPS went blank for a long while: the game starts drawing to
  a new swap chain, and Astra kept watching the old one. It now follows the new one at once, and the session report
  counts frames from both.
- The DNS page's **Run test** button showed as a black box for a few seconds while Astra read your network adapter
  (and while switching DNS). It now shows the busy look instead.

## v1.7.4 - 2026-11-16

### Added
- **Settings has two pages:** **App** holds Astra's own settings, as before, and the new **Windows** page holds
  Windows settings worth changing for games.
- **Turn off mouse acceleration** (Settings > Windows): switches off Windows' Enhance pointer precision, so the pointer
  moves the same distance however fast you move the mouse. Switching it back on restores your exact previous values.
- **Block the Start menu key in games** (Settings > Windows, off by default): a lone Windows key press won't open
  Start while a game from your list is the window in front, so a stray press can't throw you out mid-match.
  Shortcuts still work: Win+D, Win+G, Win+Shift+S and the rest go through as normal, and Alt+Tab out of the game
  and the key works as usual. Mark games in the Games tab's new **Start key off** column to limit it to those; with
  none marked, it covers every game on your list. It runs in a tiny helper process that only exists while such a
  game is open and adds about 0.03 ms to a key press.
- **Max FPS turns off the Sticky Keys pop-up:** the shortcuts behind it (Shift five times, holding right Shift for
  Filter Keys, holding Num Lock for Toggle Keys) are switched off, so they can't drop you to the desktop in the
  middle of a game. If you actually use Sticky Keys, the feature itself stays on. Undo puts the shortcuts back
  exactly as they were.

### Changed
- The Games list has a third switch column, and the Find games list's Add column moved to the right.

## v1.7.3 - 2026-11-16

### Improved
- **Lighter on your PC:** Astra uses about half the CPU it did while idle or in-game (roughly 1–1.5% of one core
  sitting idle and 1.6% with the FPS overlay showing, down from 2–3%). The UI library it's built on was re-checking
  the Windows light/dark theme 33 times a second, even though Astra is always dark.
- **Smaller download:** the exe is about a fifth smaller, so it also unpacks faster each time it starts. It no longer
  carries support for AVIF images, which Astra never uses.

## v1.7.2 - 2026-11-16

### Added
- **Startup intro:** when Astra opens, its star lights up on the starfield, the orbit is traced around it and the name
  appears, then both glide into the title bar as the app fades in. It takes about three seconds; click or press any key
  to skip it. With Motion on Reduced it's a short fade, and with Motion Off it doesn't play. Appearance > Motion >
  **Startup intro** turns it off. It never plays when Astra starts in the tray at sign-in.

### Fixed
- Astra could open behind another app that was holding the foreground (Medal, for example), so you had to click its
  taskbar button to see it. After the intro it now raises itself on top.

## v1.7.1 - 2026-11-16

### Added
- **Choose your own hotkeys:** Settings > **Hotkeys…** lets you pick the keys for optimize or undo, the FPS overlay
  and benchmarks. Click one and press the new keys: Ctrl or Alt with a letter or number, or an F key on its own.
  Astra checks it straight away, says so if another app already has it (keeping your old key), and won't let two of
  its own hotkeys share one. **Defaults** puts all three back. The tray menu, the overlay's menu and the Games tab
  show the keys you chose, and they're part of settings backups.

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
