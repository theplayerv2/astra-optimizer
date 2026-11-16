"""
Astra Optimizer - Windows gaming performance dashboard.

Single-file application that provides:
  * live CPU / GPU / RAM / power-plan telemetry (psutil, NVML or PDH, powrprof; every 1.5 s)
  * three optimisation profiles (Max FPS, Cozy Mode, Best Looking) with snapshot-based undo
  * a full "Restore Defaults" path back to stock Windows settings
  * a ping tester, a system tray icon, start with Windows and a global hotkey
  * automatic UAC elevation and per-call error handling / logging

All system work runs on a background thread; the Tk main loop only drains a queue.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
from collections import deque
import json
import math
import os
import queue
import random
import re
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from xml.sax.saxutils import escape as xml_escape

if sys.platform != "win32":
    raise SystemExit("Astra Optimizer only runs on Windows.")

# --------------------------------------------------------------------------------------
# Start menu key guard: a helper process started as  Astra --winkey-guard <event> <astra pid> <game pids>
# It runs on its own (no other Python threads it could wait behind), so a key spends ~0.04 ms in it.
# --------------------------------------------------------------------------------------

WH_KEYBOARD_LL = 13
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80
VK_LWIN, VK_RWIN = 0x5B, 0x5C
WINKEY_GUARD_ARG = "--winkey-guard"
WINKEY_HOOK_FAILED = 2  # the helper's exit code when Windows refuses the hook


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class WinKeyFilter:
    """Decides, key by key, what reaches Windows while a marked game is in front. A Windows key press is held
    back: pressed together with another key it's handed on just before that key, so Win+D and every other
    shortcut work; released on its own it's dropped, so the Start menu doesn't open. A Windows key that Windows
    saw go down always gets its release too, so it can't stick."""

    def __init__(self, pids, foreground, replay):
        self.pids = frozenset(pids)
        self.foreground = foreground  # () -> pid of the window in front
        self.replay = replay          # (windows keys, vk, scan, flags): send them, then that key, in order
        self.held: set[int] = set()     # held back; Windows hasn't seen them yet
        self.handed: set[int] = set()   # held back, then handed on for a shortcut
        self.passed: set[int] = set()   # went down normally (no game in front)
        self.blocked = 0

    def swallow(self, vk: int, scan: int, flags: int) -> bool:
        if flags & LLKHF_INJECTED:
            return False  # ours (a handed-on shortcut) or another app's
        up = flags & LLKHF_UP
        if vk == VK_LWIN or vk == VK_RWIN:
            if up:
                if vk in self.held:
                    self.held.discard(vk)
                    self.blocked += 1
                    return True  # a lone tap: Start stays shut
                self.handed.discard(vk)
                self.passed.discard(vk)
                return False
            if vk in self.held:
                return True  # key repeat while held
            if vk in self.handed or vk in self.passed:
                return False
            if self.foreground() in self.pids:
                self.held.add(vk)
                return True
            self.passed.add(vk)
            return False
        if self.held and not up:
            keys = sorted(self.held)
            self.held.clear()
            self.handed.update(keys)
            self.replay(keys, vk, scan, flags)
            return True  # sent again right after the Windows key, so the order is kept
        return False


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]  # as large as MOUSEINPUT


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


_send_input = ctypes.WinDLL("user32", use_last_error=True).SendInput
_send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_send_input.restype = wintypes.UINT


def winkey_replay(win_keys, vk: int, scan: int, flags: int) -> int:
    """Press the held-back Windows key(s), then the key that came with them, as one uninterrupted batch."""
    extended = 0x0001  # KEYEVENTF_EXTENDEDKEY
    events = [INPUT(1, INPUTUNION(ki=KEYBDINPUT(w, w, extended, 0, 0))) for w in win_keys]
    events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(vk, scan, extended if flags & LLKHF_EXTENDED else 0, 0, 0))))
    return int(_send_input(len(events), (INPUT * len(events))(*events), ctypes.sizeof(INPUT)))


def winkey_guard_main(args: list[str]) -> int:
    """Hold a low-level keyboard hook until Astra closes, every listed game has closed or Astra sets the
    stop event. Only the Windows keys are ever looked at; every other key goes straight on."""
    try:
        event_name, astra_pid = args[0], int(args[1])
        game_pids = [int(p) for p in args[2].split(",") if p]
    except (IndexError, ValueError):
        return 1
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    hookproc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    u32.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc, wintypes.HINSTANCE, wintypes.DWORD]
    u32.SetWindowsHookExW.restype = wintypes.HHOOK
    u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u32.CallNextHookEx.restype = ctypes.c_ssize_t
    u32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    u32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                              wintypes.DWORD, wintypes.DWORD]
    u32.MsgWaitForMultipleObjects.restype = wintypes.DWORD
    u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                 wintypes.UINT]
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


    synchronize, infinite, qs_allinput, pm_remove, wm_quit = 0x00100000, 0xFFFFFFFF, 0x04FF, 0x0001, 0x0012
    stop = k32.OpenEventW(synchronize, False, event_name)
    astra = k32.OpenProcess(synchronize, False, astra_pid)
    games = [h for h in (k32.OpenProcess(synchronize, False, pid) for pid in game_pids) if h]
    if not (stop and astra and games):
        return 1
    k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)  # HIGH: a busy game can't make keys wait on us

    def foreground() -> int:
        hwnd = u32.GetForegroundWindow()
        pid = wintypes.DWORD(0)
        if hwnd:
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    keys = WinKeyFilter(game_pids, foreground, winkey_replay)

    def on_key(code, wparam, lparam):
        if code == 0 and lparam:
            try:
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                if keys.swallow(kb.vkCode, kb.scanCode, kb.flags):
                    return 1
            except Exception:
                pass
        return u32.CallNextHookEx(None, code, wparam, lparam)

    proc = hookproc(on_key)
    hook = u32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, k32.GetModuleHandleW(None), 0)
    if not hook:
        return WINKEY_HOOK_FAILED
    msg = wintypes.MSG()
    try:
        while games:
            waits = (wintypes.HANDLE * (2 + len(games)))(stop, astra, *games)
            result = u32.MsgWaitForMultipleObjects(len(waits), waits, False, infinite, qs_allinput)
            if result in (0, 1):  # told to stop, or Astra closed
                break
            if 2 <= result < len(waits):
                k32.CloseHandle(games.pop(result - 2))
                continue
            if result != len(waits):
                break  # WAIT_FAILED
            while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, pm_remove):
                if msg.message == wm_quit:
                    games = []
    finally:
        u32.UnhookWindowsHookEx(hook)
    return 0


if __name__ == "__main__" and sys.argv[1:2] == [WINKEY_GUARD_ARG]:
    sys.exit(winkey_guard_main(sys.argv[2:]))

import tkinter as tk
import tkinter.font as tkfont
import winreg
from tkinter import filedialog, messagebox

import customtkinter as ctk
import psutil
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageTk

try:  # optional: without it the app simply runs without a tray icon
    import pystray
except Exception:  # pragma: no cover - depends on the build
    pystray = None

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

APP_NAME = "Astra Optimizer"
LEGACY_APP_NAME = "Optimizer"  # pre-1.12 name: data folder and startup task are carried over
APP_VERSION = "1.8.0"
DISPLAY_VERSION = "v1.8.0"
CREDIT_NAME = "theplayerv2"
UPDATE_REPO = "theplayerv2/astra-optimizer"
CREDIT_URL = f"https://github.com/{UPDATE_REPO}"
RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases"
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_INTERVAL_MS = 12 * 3600 * 1000  # re-check while it stays open in the tray

# Monochrome palette - brightness carries all meaning (no hue accents)
VOID = "#000000"          # true black space
SURFACE = "#0F0F0F"       # graphite surfaces (glass borders: white at 6% alpha)
CONSOLE_CORE = "#050505"  # terminal core
STARLIGHT = "#FFFFFF"     # primary light: selection, glow, primary action
SILVER = "#BDBDBD"        # secondary light: gradient ends, supporting text
ASH = "#6B6B6B"           # tertiary: disabled, info
TEXT_HI = "#F2F2F2"
TEXT_MID = "#9A9A9A"
SEGMENT_OFF = "#1E1E1E"
ERROR_RED = "#FF4D4F"     # hue reserved for errors
WARN_YELLOW = "#FFD60A"   # hue reserved for warnings

SYSTEM_ROOT = os.environ.get("SystemRoot") or r"C:\Windows"
SYSTEM32 = os.path.join(SYSTEM_ROOT, "System32")
POWERCFG = os.path.join(SYSTEM32, "powercfg.exe")
SC_EXE = os.path.join(SYSTEM32, "sc.exe")
IPCONFIG = os.path.join(SYSTEM32, "ipconfig.exe")
SCHTASKS = os.path.join(SYSTEM32, "schtasks.exe")

def resource_path(*parts: str) -> str:
    """A file shipped next to the script, or inside the packaged exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def _resolve_state_dir() -> str:
    """ProgramData/Astra Optimizer. The packaged app moves an existing pre-rename ProgramData/Optimizer
    folder there once, so the undo snapshot and settings survive the rename. Until that succeeds
    (or when running from source) the old folder is simply used where it is."""
    root = os.environ.get("ProgramData") or r"C:\ProgramData"
    current, legacy = os.path.join(root, APP_NAME), os.path.join(root, LEGACY_APP_NAME)
    if os.path.isdir(current) or not os.path.isdir(os.path.join(legacy)):
        return current
    if getattr(sys, "frozen", False):
        try:
            os.replace(legacy, current)
            return current
        except OSError:
            pass
    return legacy


STATE_DIR = _resolve_state_dir()
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = os.path.join(STATE_DIR, "optimizer.log")
LOG_FILE_MAX_BYTES = 1_048_576

CREATE_NO_WINDOW = 0x08000000
TELEMETRY_INTERVAL = 1.5
MB = 1024 * 1024

# Power scheme GUIDs (stock Windows)
GUID_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
GUID_HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GUID_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
GUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
SCHEME_LINE_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*\(([^)]*)\)\s*(\*)?"
)

# Registry locations
REG_VFX = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects"
REG_EXPLORER_ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
REG_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
REG_DWM = r"Software\Microsoft\Windows\DWM"
REG_GAMECONFIG = r"System\GameConfigStore"
REG_GAMEDVR = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
REG_GAMEBAR = r"Software\Microsoft\GameBar"
REG_GAMEDVR_POLICY = r"SOFTWARE\Policies\Microsoft\Windows\GameDVR"
REG_GRAPHICS = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"

DELETE = object()  # sentinel: stock state is "value absent"

# (hive, path, value name, value when disabled, human label)
GAME_DVR_OFF = [
    ("HKCU", REG_GAMECONFIG, "GameDVR_Enabled", 0, "Game DVR capture"),
    ("HKCU", REG_GAMEDVR, "AppCaptureEnabled", 0, "Game Bar app capture"),
    ("HKCU", REG_GAMEDVR, "HistoricalCaptureEnabled", 0, "Background recording"),
    ("HKCU", REG_GAMEBAR, "UseNexusForGameBarEnabled", 0, "Controller Game Bar button"),
    ("HKCU", REG_GAMEBAR, "ShowStartupPanel", 0, "Game Bar startup tips"),
    ("HKLM", REG_GAMEDVR_POLICY, "AllowGameDVR", 0, "Game DVR machine policy"),
]
# Stock Windows values for the same keys.
GAME_DVR_STOCK = [
    ("HKCU", REG_GAMECONFIG, "GameDVR_Enabled", 1, "Game DVR capture"),
    ("HKCU", REG_GAMEDVR, "AppCaptureEnabled", DELETE, "Game Bar app capture"),
    ("HKCU", REG_GAMEDVR, "HistoricalCaptureEnabled", DELETE, "Background recording"),
    ("HKCU", REG_GAMEBAR, "UseNexusForGameBarEnabled", DELETE, "Controller Game Bar button"),
    ("HKCU", REG_GAMEBAR, "ShowStartupPanel", DELETE, "Game Bar startup tips"),
    ("HKLM", REG_GAMEDVR_POLICY, "AllowGameDVR", DELETE, "Game DVR machine policy"),
]
GAME_MODE_ON = [
    ("HKCU", REG_GAMEBAR, "AutoGameModeEnabled", 1, "Game Mode"),
    ("HKCU", REG_GAMEBAR, "AllowAutoGameMode", 1, "Game Mode (allow)"),
]
GAME_MODE_STOCK = [
    ("HKCU", REG_GAMEBAR, "AutoGameModeEnabled", DELETE, "Game Mode"),
    ("HKCU", REG_GAMEBAR, "AllowAutoGameMode", DELETE, "Game Mode (allow)"),
]

# Non-essential services that Max FPS suspends (stopped for this session only).
SUSPEND_SERVICES = [
    ("SysMain", "SysMain / Superfetch"),
    ("DiagTrack", "Connected User Experiences & Telemetry"),
    ("WSearch", "Windows Search indexer"),
    ("MapsBroker", "Downloaded Maps Manager"),
    ("WerSvc", "Windows Error Reporting"),
    ("dmwappushservice", "WAP Push Message Routing"),
    ("RetailDemo", "Retail Demo"),
    ("Fax", "Fax"),
]
# Stock Windows startup types (sc.exe syntax).
SERVICE_STOCK_START = {
    "SysMain": "auto",
    "DiagTrack": "auto",
    "WSearch": "delayed-auto",
    "MapsBroker": "delayed-auto",
    "WerSvc": "demand",
    "dmwappushservice": "demand",
    "RetailDemo": "demand",
    "Fax": "demand",
}
SERVICE_SHORT = {
    "SysMain": "SysMain", "DiagTrack": "Telemetry", "WSearch": "Search indexer", "MapsBroker": "Maps",
    "WerSvc": "Error Reporting", "dmwappushservice": "WAP Push", "RetailDemo": "Retail Demo", "Fax": "Fax",
}
QUALITY_RESTART_SERVICES = [("SysMain", "SysMain / Superfetch"), ("WSearch", "Windows Search indexer")]

# Services that must never be touched (system, audio, network, security, anti-cheat).
PROTECTED_SERVICES = {
    "audiosrv", "audioendpointbuilder", "dhcp", "dnscache", "nlasvc", "nsi", "netprofm",
    "wlansvc", "wwansvc", "lanmanworkstation", "lanmanserver", "bfe", "mpssvc", "windefend",
    "wdnissvc", "securityhealthservice", "rpcss", "rpceptmapper", "dcomlaunch", "eventlog",
    "plugplay", "power", "profsvc", "schedule", "samss", "winmgmt", "cryptsvc", "bits",
    "vgc", "vgk", "easyanticheat", "easyanticheat_eos", "beservice", "faceit", "faceitservice",
    "eaanticheatservice", "xigncode", "npggsvc", "gameguard",
}

# Processes never trimmed (system-critical, audio, GPU, network, security, anti-cheat).
PROTECTED_PROCESSES = {
    # Windows core
    "system", "registry", "idle", "system idle process", "secure system", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "lsaiso.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "sihost.exe", "ctfmon.exe",
    "spoolsv.exe", "wudfhost.exe", "dashost.exe", "lsm.exe", "audiodg.exe",
    # Audio stacks
    "rtkauduservice64.exe", "rtkaudioservice64.exe", "ravbg64.exe", "nahimicsvc64.exe",
    "nahimicservice.exe", "voicemeeter.exe", "voicemeeterpro.exe", "voicemeeter8.exe",
    "voicemeeter8x64.exe", "wavessvc64.exe", "dtsapo4service.exe",
    # GPU drivers
    "nvdisplay.container.exe", "nvcontainer.exe", "amdrsserv.exe", "atiesrxx.exe",
    "atieclxx.exe", "igfxem.exe", "igfxcuiservice.exe", "intelgraphicssoftware.exe",
    # Network
    "wlanext.exe", "networkuxbroker.exe",
    # Security
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
    "mpdefendercoreservice.exe", "smartscreen.exe",
    # Anti-cheat (Vanguard, Easy Anti-Cheat, BattlEye, FACEIT, EA, others)
    "vgc.exe", "vgtray.exe", "vgm.exe",
    "easyanticheat.exe", "easyanticheat_eos.exe", "easyanticheat_launcher.exe",
    "eac_launcher.exe", "start_protected_game.exe",
    "beservice.exe", "beservice_x64.exe", "battleye launcher.exe", "belauncher.exe",
    "faceit.exe", "faceitclient.exe", "faceitservice.exe",
    "eaanticheat.gameservice.exe", "eaanticheat.installer.exe",
    "gameguard.exe", "gamemon.des", "gamemon64.des", "xigncode.exe", "zakynthos.exe",
    "ricochet.exe", "atvi-randgrid_sr.exe",
}

# Apps Cozy Mode never touches (voice chat, music, browsers).
COZY_SPARED_PROCESSES = {
    "discord.exe", "discordptb.exe", "discordcanary.exe", "spotify.exe",
    "chrome.exe", "msedge.exe", "msedgewebview2.exe", "firefox.exe", "opera.exe",
    "opera_gx.exe", "brave.exe", "vivaldi.exe", "arc.exe", "librewolf.exe", "waterfox.exe",
    "zen.exe", "thorium.exe", "yandex.exe",
}

# Apps never trimmed in ANY profile (matched by process name or by install folder).
EXCLUDED_PROCESSES = {
    # Medal.tv clipping (UI, recorder/encoder, Steam integration, notifications)
    "medal.exe", "medalencoder.exe", "medalsteamutils.exe", "medaltoast.exe",
}
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA") or ""
EXCLUDED_INSTALL_DIRS = [os.path.join(_LOCAL_APPDATA, "Medal")] if _LOCAL_APPDATA else []
EXCLUDED_INSTALL_PREFIXES = tuple(os.path.normcase(os.path.abspath(path)) + os.sep for path in EXCLUDED_INSTALL_DIRS)

# --------------------------------------------------------------------------------------
# Win32 bindings
# --------------------------------------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
powrprof = ctypes.WinDLL("powrprof")
gdi32 = ctypes.WinDLL("gdi32")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300

SystemMemoryListInformation = 80
MemoryPurgeStandbyList = 4
MemoryPurgeLowPriorityStandbyList = 5

SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
SPI_SETDRAGFULLWINDOWS = 0x0025
SPI_SETANIMATION = 0x0049
SPI_SETFONTSMOOTHING = 0x004B
SPI_SETMENUANIMATION = 0x1003
SPI_SETCOMBOBOXANIMATION = 0x1005
SPI_SETLISTBOXSMOOTHSCROLLING = 0x1007
SPI_SETMENUFADE = 0x1013
SPI_SETSELECTIONFADE = 0x1015
SPI_SETTOOLTIPANIMATION = 0x1017
SPI_SETTOOLTIPFADE = 0x1019
SPI_SETCURSORSHADOW = 0x101B
SPI_SETDROPSHADOW = 0x1025
SPI_SETUIEFFECTS = 0x103F
SPI_SETCLIENTAREAANIMATION = 0x1043
SPI_SETFONTSMOOTHINGTYPE = 0x200B
SPI_GETANIMATION = 0x0048
# Accessibility shortcuts (Shift five times, hold right Shift, hold Num Lock)
SPI_GETFILTERKEYS, SPI_SETFILTERKEYS = 0x0032, 0x0033
SPI_GETTOGGLEKEYS, SPI_SETTOGGLEKEYS = 0x0034, 0x0035
SPI_GETSTICKYKEYS, SPI_SETSTICKYKEYS = 0x003A, 0x003B
ACCESS_HOTKEY_ON = 0x00000004  # SKF_/FKF_/TKF_HOTKEYACTIVE: the same bit in all three
SPI_GETMOUSE, SPI_SETMOUSE = 0x0003, 0x0004
SPI_GETMOUSESPEED, SPI_SETMOUSESPEED = 0x0070, 0x0071
POINTER_SPEED_1TO1 = 10  # the middle notch (6 of 11): one mouse count moves the pointer one pixel
MOUSE_ACCEL_DEFAULT = (6, 10, 1)  # threshold 1, threshold 2, acceleration: Enhance pointer precision as Windows ships
# SET action -> matching GET action (used to snapshot the current value before changing it)
SPI_GET_FOR_SET = {
    SPI_SETDRAGFULLWINDOWS: 0x0026,
    SPI_SETFONTSMOOTHING: 0x004A,
    SPI_SETMENUANIMATION: 0x1002,
    SPI_SETCOMBOBOXANIMATION: 0x1004,
    SPI_SETLISTBOXSMOOTHSCROLLING: 0x1006,
    SPI_SETMENUFADE: 0x1012,
    SPI_SETSELECTIONFADE: 0x1014,
    SPI_SETTOOLTIPANIMATION: 0x1016,
    SPI_SETTOOLTIPFADE: 0x1018,
    SPI_SETCURSORSHADOW: 0x101A,
    SPI_SETDROPSHADOW: 0x1024,
    SPI_SETUIEFFECTS: 0x103E,
    SPI_SETCLIENTAREAANIMATION: 0x1042,
    SPI_SETFONTSMOOTHINGTYPE: 0x200A,
}
FE_FONTSMOOTHINGCLEARTYPE = 0x0002

HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class ANIMATIONINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("iMinAnimate", ctypes.c_int)]


class SYSTEM_INFO(ctypes.Structure):
    _fields_ = [
        ("wProcessorArchitecture", wintypes.WORD),
        ("wReserved", wintypes.WORD),
        ("dwPageSize", wintypes.DWORD),
        ("lpMinimumApplicationAddress", wintypes.LPVOID),
        ("lpMaximumApplicationAddress", wintypes.LPVOID),
        ("dwActiveProcessorMask", ctypes.c_size_t),
        ("dwNumberOfProcessors", wintypes.DWORD),
        ("dwProcessorType", wintypes.DWORD),
        ("dwAllocationGranularity", wintypes.DWORD),
        ("wProcessorLevel", wintypes.WORD),
        ("wProcessorRevision", wintypes.WORD),
    ]


class SYSTEM_MEMORY_LIST_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ZeroPageCount", ctypes.c_size_t),
        ("FreePageCount", ctypes.c_size_t),
        ("ModifiedPageCount", ctypes.c_size_t),
        ("ModifiedNoWritePageCount", ctypes.c_size_t),
        ("BadPageCount", ctypes.c_size_t),
        ("PageCountByPriority", ctypes.c_size_t * 8),
        ("RepurposedPagesByPriority", ctypes.c_size_t * 8),
        ("ModifiedPageCountPageFile", ctypes.c_size_t),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.GetOEMCP.argtypes = []
kernel32.GetOEMCP.restype = wintypes.UINT
kernel32.GetSystemInfo.argtypes = [ctypes.POINTER(SYSTEM_INFO)]
kernel32.GetSystemInfo.restype = None

psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
psapi.EmptyWorkingSet.restype = wintypes.BOOL

advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
ntdll.NtSetSystemInformation.restype = ctypes.c_long
ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long

user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_wchar_p,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = wintypes.LPARAM
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_MINIMIZEBOX = 0x00020000
WS_SYSMENU = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_MINIMIZE = 6
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2

user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
try:
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
except OSError:
    dwmapi = None

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)
]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
PW_RENDERFULLCONTENT = 0x00000002
GWLP_HWNDPARENT = -8  # a top-level window's owner


_icon_api = ctypes.WinDLL("user32")  # private function objects for the taskbar icon
_icon_api.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_icon_api.LoadImageW.restype = wintypes.HANDLE
_icon_api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_icon_api.SendMessageW.restype = ctypes.c_ssize_t
WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
APP_USER_MODEL_ID = "theplayerv2.AstraOptimizer"


def set_window_icons(hwnd: int, ico_path: str, scale: float) -> list:
    """Give a window its own big (taskbar, Alt+Tab) and small icons from an .ico file. Returns the
    icon handles, which must stay alive as long as the window does."""
    handles = []
    if not os.path.isfile(ico_path):
        return handles
    for kind, size in ((ICON_BIG, max(32, round(32 * scale))), (ICON_SMALL, max(16, round(16 * scale)))):
        handle = _icon_api.LoadImageW(None, ico_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if handle:
            _icon_api.SendMessageW(hwnd, WM_SETICON, kind, handle)
            handles.append(handle)
    return handles


def capture_window(hwnd: int):
    """Exactly what a window shows right now (text and child widgets included), as an RGB image."""
    from PIL import Image as _Image
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    try:
        previous = gdi32.SelectObject(mdc, bmp)
        printed = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        gdi32.SelectObject(mdc, previous)
        if not printed:
            return None
        buffer = ctypes.create_string_buffer(w * h * 4)
        header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buffer, ctypes.byref(header), 0):
            return None
        return _Image.frombuffer("RGB", (w, h), buffer, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)


class _IpOptionInformation(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                ("Options", _IpOptionInformation)]


class _MibIpForwardRow(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in (
        "dest", "mask", "policy", "next_hop", "if_index", "type", "proto", "age", "next_hop_as",
        "metric1", "metric2", "metric3", "metric4", "metric5")]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


try:
    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.IcmpCreateFile.argtypes = []
    iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
    iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
    iphlpapi.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
    iphlpapi.GetBestRoute.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_MibIpForwardRow)]
    iphlpapi.GetBestRoute.restype = wintypes.DWORD
except (OSError, AttributeError):
    iphlpapi = None

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = [
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int
]
shell32.ShellExecuteW.restype = ctypes.c_void_p

powrprof.PowerGetActiveScheme.argtypes = [wintypes.HKEY, ctypes.POINTER(ctypes.POINTER(GUID))]
powrprof.PowerGetActiveScheme.restype = wintypes.DWORD
powrprof.PowerReadFriendlyName.argtypes = [
    wintypes.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID), ctypes.POINTER(GUID),
    ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
]
powrprof.PowerReadFriendlyName.restype = wintypes.DWORD


def _page_size() -> int:
    try:
        info = SYSTEM_INFO()
        kernel32.GetSystemInfo(ctypes.byref(info))
        return int(info.dwPageSize) or 4096
    except Exception:
        return 4096


PAGE_SIZE = _page_size()

try:
    OEM_CODEPAGE = f"cp{kernel32.GetOEMCP()}"
except Exception:
    OEM_CODEPAGE = "cp437"

# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Re-launch this program elevated via the UAC 'runas' verb. True if the new process started."""
    if getattr(sys, "frozen", False):
        executable = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.isfile(pythonw):
            executable = pythonw
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    try:
        result = shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        return int(result or 0) > 32
    except Exception:
        return False


def decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in (OEM_CODEPAGE, "utf-8", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def run_command(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """Run a console tool hidden. Returns (return code, combined output)."""
    try:
        completed = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            shell=False,
        )
        return completed.returncode, decode_output(completed.stdout).strip()
    except FileNotFoundError:
        return -1, f"executable not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return -2, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return -3, str(exc)


def first_line(text: str, limit: int = 90) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return "no output"


def enable_privilege(name: str) -> bool:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
    ):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(privileges), ctypes.sizeof(privileges), None, None
        ):
            return False
        return ctypes.get_last_error() != ERROR_NOT_ALL_ASSIGNED
    finally:
        kernel32.CloseHandle(token)


def get_active_power_plan() -> tuple[str, str]:
    """Return (guid, friendly name) of the active power scheme."""
    scheme = ctypes.POINTER(GUID)()
    try:
        if powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme)) == 0 and scheme:
            try:
                g = scheme.contents
                guid = "%08x-%04x-%04x-%s-%s" % (
                    g.Data1, g.Data2, g.Data3,
                    "".join("%02x" % b for b in g.Data4[:2]),
                    "".join("%02x" % b for b in g.Data4[2:]),
                )
                size = wintypes.DWORD(0)
                powrprof.PowerReadFriendlyName(None, scheme, None, None, None, ctypes.byref(size))
                name = ""
                if size.value:
                    buffer = ctypes.create_string_buffer(size.value + 2)
                    if powrprof.PowerReadFriendlyName(
                        None, scheme, None, None, buffer, ctypes.byref(size)
                    ) == 0:
                        name = ctypes.wstring_at(ctypes.addressof(buffer))
                return guid, name or "Custom plan"
            finally:
                kernel32.LocalFree(ctypes.cast(scheme, ctypes.c_void_p))
    except Exception:
        pass
    rc, out = run_command([POWERCFG, "/getactivescheme"], timeout=10)
    if rc == 0:
        match = SCHEME_LINE_RE.search(out)
        if match:
            return match.group(1).lower(), match.group(2).strip()
    return "", "Unknown"


class STICKYKEYS(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD)]


class TOGGLEKEYS(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD)]


class FILTERKEYS(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD), ("iWaitMSec", wintypes.DWORD),
                ("iDelayMSec", wintypes.DWORD), ("iRepeatMSec", wintypes.DWORD), ("iBounceMSec", wintypes.DWORD)]


# which, label, struct, get, set
ACCESS_SHORTCUTS = [
    ("sticky", "Sticky Keys shortcut (Shift five times)", STICKYKEYS, SPI_GETSTICKYKEYS, SPI_SETSTICKYKEYS),
    ("filter", "Filter Keys shortcut (hold right Shift)", FILTERKEYS, SPI_GETFILTERKEYS, SPI_SETFILTERKEYS),
    ("toggle", "Toggle Keys shortcut (hold Num Lock)", TOGGLEKEYS, SPI_GETTOGGLEKEYS, SPI_SETTOGGLEKEYS),
]
ACCESS_BY_KEY = {spec[0]: spec for spec in ACCESS_SHORTCUTS}


def access_shortcut_get(which: str):
    """The current STICKYKEYS/FILTERKEYS/TOGGLEKEYS settings, or None when Windows won't say."""
    _which, _label, struct, get_action, _set = ACCESS_BY_KEY[which]
    info = struct()
    info.cbSize = ctypes.sizeof(struct)
    ok = user32.SystemParametersInfoW(get_action, ctypes.sizeof(struct), ctypes.byref(info), 0)
    return info if ok else None


def mouse_accel_get() -> tuple[int, int, int] | None:
    """(threshold 1, threshold 2, acceleration); acceleration 0 means Enhance pointer precision is off."""
    values = (ctypes.c_int * 3)()
    if not user32.SystemParametersInfoW(SPI_GETMOUSE, 0, ctypes.byref(values), 0):
        return None
    return tuple(int(v) for v in values)


def mouse_accel_set(values) -> bool:
    data = (ctypes.c_int * 3)(*values)
    return bool(user32.SystemParametersInfoW(SPI_SETMOUSE, 0, ctypes.byref(data), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE))


def pointer_speed_get() -> int | None:
    value = ctypes.c_int(0)
    if not user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(value), 0):
        return None
    return int(value.value)


def pointer_notch(speed: int) -> int:
    """Where Windows' 11-notch pointer speed slider sits for a speed of 1-20 (notch 6 is 10)."""
    return 1 if speed <= 1 else min(11, speed // 2 + 1)


def pointer_speed_set(speed: int) -> bool:
    return bool(user32.SystemParametersInfoW(SPI_SETMOUSESPEED, 0, ctypes.c_void_p(int(speed)),
                                             SPIF_UPDATEINIFILE | SPIF_SENDCHANGE))


# Settings > Windows switches that are one registry value: key -> (hive, path, name, value when on,
# value to fall back to when switched off with nothing saved (None: remove it, as Windows ships))
WINSET_REG = {
    # Settings > Windows Update > Delivery Optimization > "Allow downloads from other PCs" (0 = off)
    "deliveryopt": ("HKU", r"S-1-5-20\Software\Microsoft\Windows\CurrentVersion\DeliveryOptimization\Settings",
                    "DownloadMode", 0, None),
    # Control Panel > Power Options > "Turn on fast startup"
    "faststartup": ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Power", "HiberbootEnabled", 0, 1),
}
WINSET_MESSAGES = {  # (switched on, switched off)
    "deliveryopt": ("Windows no longer shares update files with other PCs",
                    "Windows shares update files with other PCs again, as it did before"),
    "faststartup": ("Fast Startup is off: from your next shut down, Windows and its drivers start fresh",
                    "Fast Startup is back on, as Windows had it"),
}


def access_shortcuts_on() -> list[str]:
    """Which of the three accessibility shortcuts are switched on right now."""
    on = []
    for which, *_rest in ACCESS_SHORTCUTS:
        info = access_shortcut_get(which)
        if info is not None and info.dwFlags & ACCESS_HOTKEY_ON:
            on.append(which)
    return on


def foreground_pid() -> int:
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def has_battery() -> bool:
    try:
        return psutil.sensors_battery() is not None
    except Exception:
        return False


# --------------------------------------------------------------------------------------
# Persistent state (created power plans, HAGS original value)
# --------------------------------------------------------------------------------------


class StateStore:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._data = data
        except (OSError, ValueError):
            self._data = {}
        if not isinstance(self._data.get("snapshot"), dict):
            self._data["snapshot"] = {}
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """Fold the v1.0 'hags_original' record into the undo snapshot."""
        legacy = self._data.pop("hags_original", None)
        if not isinstance(legacy, dict):
            return
        key = f"reg:HKLM\\{REG_GRAPHICS}\\HwSchMode"
        if key not in self._data["snapshot"]:
            entry = {
                "kind": "reg", "hive": "HKLM", "path": REG_GRAPHICS, "name": "HwSchMode",
                "label": "Hardware-accelerated GPU scheduling", "present": bool(legacy.get("present")),
            }
            if entry["present"] and isinstance(legacy.get("value"), int):
                entry["value"] = legacy["value"]
                entry["type"] = winreg.REG_DWORD
            else:
                entry["present"] = False
            self._data["snapshot"][key] = entry
        with self._lock:
            self._save_locked()

    # ---- undo snapshot: the value each setting had BEFORE Optimizer first changed it ----

    def snapshot_has(self, key: str) -> bool:
        with self._lock:
            return key in self._data["snapshot"]

    def snapshot_add(self, key: str, entry: dict) -> None:
        """Record an original value. The first recording wins, so repeated runs never overwrite it."""
        with self._lock:
            if key not in self._data["snapshot"]:
                self._data["snapshot"][key] = entry
                self._save_locked()

    def snapshot_items(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [(k, dict(v)) for k, v in self._data["snapshot"].items()]

    def snapshot_discard(self, key: str) -> None:
        with self._lock:
            if self._data["snapshot"].pop(key, None) is not None:
                self._save_locked()

    def snapshot_count(self) -> int:
        with self._lock:
            return len(self._data["snapshot"])

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def remove(self, key: str) -> None:
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._save_locked()

    def _save_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# Run log (counts outcomes, forwards lines to the UI sink)
# --------------------------------------------------------------------------------------


class RunLog:
    def __init__(self, sink):
        self._sink = sink
        self.ok_count = 0
        self.warn_count = 0
        self.fail_count = 0

    def head(self, msg: str) -> None:
        self._sink("head", msg)

    def info(self, msg: str) -> None:
        self._sink("info", msg)

    def ok(self, msg: str) -> None:
        self.ok_count += 1
        self._sink("ok", msg)

    def warn(self, msg: str) -> None:
        self.warn_count += 1
        self._sink("warn", msg)

    def fail(self, msg: str) -> None:
        self.fail_count += 1
        self._sink("fail", msg)


# --------------------------------------------------------------------------------------
# System tweaks engine
# --------------------------------------------------------------------------------------

HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKU": winreg.HKEY_USERS}

# (label, SPI action or "animation", parameter style, category)
VISUAL_SPI_EFFECTS = [
    ("Animate windows when minimizing/maximizing", "animation", "struct", "anim"),
    ("Animate controls inside windows", SPI_SETCLIENTAREAANIMATION, "pv", "anim"),
    ("Fade/slide menus into view", SPI_SETMENUANIMATION, "pv", "anim"),
    ("Menu fade effect", SPI_SETMENUFADE, "pv", "anim"),
    ("Fade out menu items after clicking", SPI_SETSELECTIONFADE, "pv", "anim"),
    ("Slide open combo boxes", SPI_SETCOMBOBOXANIMATION, "pv", "anim"),
    ("Smooth-scroll list boxes", SPI_SETLISTBOXSMOOTHSCROLLING, "pv", "anim"),
    ("Fade/slide tooltips into view", SPI_SETTOOLTIPANIMATION, "pv", "anim"),
    ("Tooltip fade effect", SPI_SETTOOLTIPFADE, "pv", "anim"),
    ("Shadows under mouse pointer", SPI_SETCURSORSHADOW, "pv", "shadow"),
    ("Shadows under windows", SPI_SETDROPSHADOW, "pv", "shadow"),
    ("Show window contents while dragging", SPI_SETDRAGFULLWINDOWS, "ui", "other"),
]
# (label, hive, path, value name, category)
VISUAL_REG_EFFECTS = [
    ("Taskbar animations", "HKCU", REG_EXPLORER_ADV, "TaskbarAnimations", "anim"),
    ("Drop shadows for desktop icon labels", "HKCU", REG_EXPLORER_ADV, "ListviewShadow", "shadow"),
    ("Translucent selection rectangle", "HKCU", REG_EXPLORER_ADV, "ListviewAlphaSelect", "other"),
    ("Aero Peek", "HKCU", REG_DWM, "EnableAeroPeek", "other"),
]
# mode -> (VisualFXSetting, {category: enabled}, transparency, description)
VISUAL_MODES = {
    "performance": (2, {"anim": False, "shadow": False, "other": False}, False, "Best performance"),
    "smooth": (3, {"anim": True, "shadow": False, "other": True}, True, "Smooth (animations on, shadows trimmed)"),
    "quality": (1, {"anim": True, "shadow": True, "other": True}, True, "Best appearance"),
    "default": (0, {"anim": True, "shadow": True, "other": True}, True, "Let Windows choose (stock)"),
}


# --------------------------------------------------------------------------------------
# NVIDIA driver settings (NVAPI "DRS", the same store the NVIDIA Control Panel edits).
# IDs and layouts are from NVIDIA's public headers (github.com/NVIDIA/nvapi): nvapi_interface.h,
# NvApiDriverSettings.h and nvapi.h. Only the global (base) profile is touched.
# --------------------------------------------------------------------------------------

NVAPI_IDS = {"Initialize": 0x0150E828, "DRS_CreateSession": 0x0694D52E, "DRS_DestroySession": 0xDAD9CFF8,
             "DRS_LoadSettings": 0x375DBD6B, "DRS_SaveSettings": 0xFCBC7E14, "DRS_GetBaseProfile": 0xDA8466A0,
             "DRS_GetSetting": 0x73BF8338, "DRS_SetSetting": 0x577DD202, "DRS_DeleteProfileSetting": 0xE4A26362}
NV_PREFERRED_PSTATE = 0x1057EB71   # "Power management mode": 1 = Prefer maximum performance
NV_PRERENDER_LIMIT = 0x007BA09E    # "Maximum pre-rendered frames": 1 = Low Latency Mode "On"
NV_SETTING_NOT_FOUND, NV_INVALID_USER_PRIVILEGE, NV_DEVICE_NOT_FOUND = -160, -137, -6
NVIDIA_TWEAKS = {
    "max": [(NV_PREFERRED_PSTATE, 1, "NVIDIA power management", "Prefer maximum performance"),
            (NV_PRERENDER_LIMIT, 1, "NVIDIA Low Latency Mode", "On")],
    "cozy": [(NV_PRERENDER_LIMIT, 1, "NVIDIA Low Latency Mode", "On")],
}


class _NvdrsBinary(ctypes.Structure):
    _fields_ = [("valueLength", ctypes.c_uint32), ("valueData", ctypes.c_uint8 * 4096)]


class _NvdrsValue(ctypes.Union):
    # The classic layout the driver accepts (NVDRS_SETTING_VER1 = 0x13020). Newer nvapi.h adds 64-bit
    # members to this union, which would make the struct 12328 bytes; current drivers reject that
    # with NVAPI_INCOMPATIBLE_STRUCT_VERSION (-9), so they're left out on purpose.
    _fields_ = [("u32", ctypes.c_uint32), ("binary", _NvdrsBinary)]


class NVDRS_SETTING(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("settingName", ctypes.c_uint16 * 2048), ("settingId", ctypes.c_uint32),
                ("settingType", ctypes.c_int), ("settingLocation", ctypes.c_int),
                ("isCurrentPredefined", ctypes.c_uint32), ("isPredefinedValid", ctypes.c_uint32),
                ("predefined", _NvdrsValue), ("current", _NvdrsValue)]


assert ctypes.sizeof(NVDRS_SETTING) == 12320
NVDRS_SETTING_VER1 = ctypes.sizeof(NVDRS_SETTING) | (1 << 16)
NVDRS_DWORD_TYPE = 0


class NvidiaError(Exception):
    def __init__(self, what: str, code: int):
        reason = {NV_INVALID_USER_PRIVILEGE: "needs administrator rights",
                  NV_DEVICE_NOT_FOUND: "no NVIDIA graphics card in use"}.get(code, f"NVAPI error {code}")
        super().__init__(f"{what}: {reason}")
        self.code = code


class NvidiaDrs:
    """One driver-settings session on the global profile. Use as a context manager."""

    _api: dict | None = None

    @classmethod
    def _load(cls) -> dict:
        if cls._api is None:
            try:
                dll = ctypes.WinDLL("nvapi64.dll")
            except OSError as exc:
                raise NvidiaError("NVIDIA driver", NV_DEVICE_NOT_FOUND) from exc
            query = dll.nvapi_QueryInterface
            query.restype, query.argtypes = ctypes.c_void_p, [ctypes.c_uint32]
            handle, setting = ctypes.c_void_p, ctypes.POINTER(NVDRS_SETTING)
            signatures = {"Initialize": [], "DRS_CreateSession": [ctypes.POINTER(handle)],
                          "DRS_DestroySession": [handle], "DRS_LoadSettings": [handle], "DRS_SaveSettings": [handle],
                          "DRS_GetBaseProfile": [handle, ctypes.POINTER(handle)],
                          "DRS_GetSetting": [handle, handle, ctypes.c_uint32, setting],
                          "DRS_SetSetting": [handle, handle, setting],
                          "DRS_DeleteProfileSetting": [handle, handle, ctypes.c_uint32]}
            api = {}
            for name, args in signatures.items():
                address = query(NVAPI_IDS[name])
                if not address:
                    raise NvidiaError(f"NVAPI {name}", -3)
                api[name] = ctypes.CFUNCTYPE(ctypes.c_int, *args)(address)
            rc = api["Initialize"]()
            if rc != 0:
                raise NvidiaError("NVIDIA driver", rc)
            cls._api = api
        return cls._api

    def __init__(self):
        self.api = self._load()
        self.session, self.profile = ctypes.c_void_p(), ctypes.c_void_p()

    def _call(self, name: str, *args) -> int:
        rc = self.api[name](*args)
        if rc not in (0, NV_SETTING_NOT_FOUND):
            raise NvidiaError(name.replace("DRS_", ""), rc)
        return rc

    def __enter__(self) -> "NvidiaDrs":
        self._call("DRS_CreateSession", ctypes.byref(self.session))
        try:
            self._call("DRS_LoadSettings", self.session)
            self._call("DRS_GetBaseProfile", self.session, ctypes.byref(self.profile))
        except NvidiaError:
            self.api["DRS_DestroySession"](self.session)
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.api["DRS_DestroySession"](self.session)

    def get(self, setting_id: int) -> int | None:
        """The global value the user (or an app like this one) set, or None for the driver default."""
        setting = NVDRS_SETTING()
        setting.version = NVDRS_SETTING_VER1
        if self._call("DRS_GetSetting", self.session, self.profile, setting_id, ctypes.byref(setting)) != 0:
            return None
        if setting.isCurrentPredefined or setting.settingType != NVDRS_DWORD_TYPE:
            return None
        return int(setting.current.u32)

    def set(self, setting_id: int, value: int) -> None:
        setting = NVDRS_SETTING()
        setting.version = NVDRS_SETTING_VER1
        setting.settingId, setting.settingType = setting_id, NVDRS_DWORD_TYPE
        setting.current.u32 = value
        self._call("DRS_SetSetting", self.session, self.profile, ctypes.byref(setting))

    def reset(self, setting_id: int) -> None:
        self._call("DRS_DeleteProfileSetting", self.session, self.profile, setting_id)

    def save(self) -> None:
        self._call("DRS_SaveSettings", self.session)


# --------------------------------------------------------------------------------------
# AMD driver settings through ADLX (amdadlx64.dll, installed with AMD Software). ADLX is a C
# interface: every object is a pointer to a table of functions, and the slot numbers below are
# the order of those tables in AMD's public headers (github.com/GPUOpen-LibrariesAndSDKs/ADLX:
# ISystem.h, I3DSettings.h). Only settings that exist in every ADLX version are used.
# --------------------------------------------------------------------------------------

ADLX_OK, ADLX_ALREADY_ENABLED, ADLX_NOT_SUPPORTED = 0, 1, 12
ADLX_NO_DRIVER = -1                    # our own code: amdadlx64.dll isn't there (no AMD graphics driver)
ADLX_ANTILAG_PLAIN = 0                 # ADLX_ANTILAG_STATE: 0 = Anti-Lag, 1 = the newer in-driver variant
# slots: IADLXSystem has no Acquire/Release; every other interface starts with Acquire, Release, QueryInterface
ADLX_SYS_GET_GPUS, ADLX_SYS_GET_3D = 1, 7
ADLX_RELEASE, ADLX_QUERY_INTERFACE = 1, 2
ADLX_LIST_SIZE, ADLX_LIST_BEGIN, ADLX_LIST_AT_GPU = 3, 5, 11
ADLX_GPU_NAME, ADLX_GPU_UNIQUE_ID = 7, 18
ADLX_3D_GET_ANTILAG, ADLX_3D_GET_CHILL = 3, 4
ADLX_FEATURE_IS_SUPPORTED, ADLX_FEATURE_IS_ENABLED = 3, 4
ADLX_SET_ENABLED = {"antilag": 5, "chill": 8}
ADLX_ANTILAG1_GET_LEVEL = 6
AMD_TWEAKS = {
    "max": [("antilag", True, "AMD Anti-Lag", "On"), ("chill", False, "AMD Radeon Chill", "Off")],
    "cozy": [("antilag", True, "AMD Anti-Lag", "On")],
}


class AmdError(Exception):
    def __init__(self, what: str, code: int):
        reason = {ADLX_NO_DRIVER: "no AMD graphics driver", ADLX_NOT_SUPPORTED: "not supported"}.get(
            code, f"ADLX error {code}")
        super().__init__(f"{what}: {reason}")
        self.code = code


def _adlx_call(obj, slot: int, restype, argtypes: list, *args):
    """Call slot `slot` of an ADLX object's function table."""
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    function = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[slot])
    return function(obj, *args)


class AmdAdlx:
    """One ADLX session listing the AMD GPUs. Use as a context manager."""

    _dll = None

    @classmethod
    def _load(cls):
        if cls._dll is None:
            try:
                dll = ctypes.CDLL("amdadlx64.dll")
            except OSError as exc:
                raise AmdError("AMD driver", ADLX_NO_DRIVER) from exc
            dll.ADLXQueryFullVersion.restype = ctypes.c_int
            dll.ADLXQueryFullVersion.argtypes = [ctypes.POINTER(ctypes.c_uint64)]
            dll.ADLXInitialize.restype = ctypes.c_int
            dll.ADLXInitialize.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_void_p)]
            dll.ADLXTerminate.restype = ctypes.c_int
            dll.ADLXTerminate.argtypes = []
            cls._dll = dll
        return cls._dll

    def __init__(self):
        self.dll = self._load()
        self.system = ctypes.c_void_p()
        self._held: list = []
        self.services = None
        self.gpus: list = []

    def _check(self, what: str, rc: int) -> None:
        if rc not in (ADLX_OK, ADLX_ALREADY_ENABLED):
            raise AmdError(what, rc)

    def _get(self, what: str, obj, slot: int, in_types: tuple = (), *in_args, keep: bool = True):
        """Call a getter that returns an interface through its last argument."""
        out = ctypes.c_void_p()
        rc = _adlx_call(obj, slot, ctypes.c_int, [*in_types, ctypes.POINTER(ctypes.c_void_p)],
                        *in_args, ctypes.pointer(out))
        self._check(what, rc)
        if not out.value:
            raise AmdError(what, ADLX_NOT_SUPPORTED)
        if keep:
            self._held.append(out)
        return out

    def _release(self, obj) -> None:
        try:
            _adlx_call(obj, ADLX_RELEASE, ctypes.c_long, [])
        except Exception:
            pass

    def __enter__(self) -> "AmdAdlx":
        version = ctypes.c_uint64()
        # Ask for the version the installed driver has: everything used here exists since ADLX 1.0.
        self._check("ADLX version", self.dll.ADLXQueryFullVersion(ctypes.pointer(version)))
        self._check("AMD driver", self.dll.ADLXInitialize(version.value, ctypes.pointer(self.system)))
        if not self.system.value:
            self.dll.ADLXTerminate()
            raise AmdError("AMD driver", ADLX_NO_DRIVER)
        try:
            self.services = self._get("3D settings", self.system, ADLX_SYS_GET_3D)
            gpu_list = self._get("GPU list", self.system, ADLX_SYS_GET_GPUS)
            size = _adlx_call(gpu_list, ADLX_LIST_SIZE, ctypes.c_uint, [])
            begin = _adlx_call(gpu_list, ADLX_LIST_BEGIN, ctypes.c_uint, [])
            self.gpus = [self._get("GPU", gpu_list, ADLX_LIST_AT_GPU, (ctypes.c_uint,), begin + i)
                         for i in range(size)]
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        for obj in reversed(self._held):
            self._release(obj)
        self._held.clear()
        if self.system.value:
            self.dll.ADLXTerminate()
            self.system = ctypes.c_void_p()

    def gpu_name(self, gpu) -> str:
        name = ctypes.c_char_p()
        _adlx_call(gpu, ADLX_GPU_NAME, ctypes.c_int, [ctypes.POINTER(ctypes.c_char_p)], ctypes.pointer(name))
        return (name.value or b"AMD GPU").decode("utf-8", "replace")

    def gpu_id(self, gpu) -> int:
        unique = ctypes.c_int()
        _adlx_call(gpu, ADLX_GPU_UNIQUE_ID, ctypes.c_int, [ctypes.POINTER(ctypes.c_int)], ctypes.pointer(unique))
        return int(unique.value)

    def _feature(self, gpu, feature: str):
        slot = {"antilag": ADLX_3D_GET_ANTILAG, "chill": ADLX_3D_GET_CHILL}[feature]
        return self._get(feature, self.services, slot, (ctypes.c_void_p,), gpu)

    def get(self, gpu, feature: str) -> bool | None:
        """On/off, or None when this GPU doesn't have the feature."""
        try:
            obj = self._feature(gpu, feature)
        except AmdError as exc:
            if exc.code == ADLX_NOT_SUPPORTED:
                return None
            raise
        flag = ctypes.c_bool()
        rc = _adlx_call(obj, ADLX_FEATURE_IS_SUPPORTED, ctypes.c_int, [ctypes.POINTER(ctypes.c_bool)],
                        ctypes.pointer(flag))
        if rc != ADLX_OK or not flag.value:
            return None
        self._check(feature, _adlx_call(obj, ADLX_FEATURE_IS_ENABLED, ctypes.c_int,
                                        [ctypes.POINTER(ctypes.c_bool)], ctypes.pointer(flag)))
        return bool(flag.value)

    def set(self, gpu, feature: str, value: bool) -> None:
        obj = self._feature(gpu, feature)
        self._check(feature, _adlx_call(obj, ADLX_SET_ENABLED[feature], ctypes.c_int, [ctypes.c_bool], bool(value)))

    def antilag_level(self, gpu) -> int | None:
        """Which Anti-Lag AMD Software has selected, on drivers that have more than one; else None."""
        try:
            obj = self._feature(gpu, "antilag")
            newer = self._get("Anti-Lag level", obj, ADLX_QUERY_INTERFACE, (ctypes.c_wchar_p,), "IADLX3DAntiLag1")
        except AmdError:
            return None
        level = ctypes.c_int()
        if _adlx_call(newer, ADLX_ANTILAG1_GET_LEVEL, ctypes.c_int, [ctypes.POINTER(ctypes.c_int)],
                      ctypes.pointer(level)) != ADLX_OK:
            return None
        return int(level.value)


def amd_pending(profile: str) -> list[str]:
    """Read-only: which AMD tweaks of a mode would change something now."""
    wanted = AMD_TWEAKS.get(profile) or []
    if not wanted:
        return []
    try:
        with AmdAdlx() as amd:
            out = []
            for gpu in amd.gpus:
                for feature, value, label, shown in wanted:
                    current = amd.get(gpu, feature)
                    if current is None or current == value:
                        continue
                    if feature == "antilag" and amd.antilag_level(gpu) not in (None, ADLX_ANTILAG_PLAIN):
                        continue
                    text = f"{label.replace('AMD ', '')} {shown}"
                    if text not in out:
                        out.append(text)
            return out
    except Exception:
        return []


def nvidia_pending(profile: str) -> list[str]:
    """Read-only: which NVIDIA tweaks of a mode would change something now."""
    wanted = NVIDIA_TWEAKS.get(profile) or []
    if not wanted:
        return []
    try:
        with NvidiaDrs() as nv:
            return [f"{label.replace('NVIDIA ', '')} {shown}" for sid, value, label, shown in wanted
                    if nv.get(sid) != value]
    except Exception:
        return []


class SystemTweaks:
    def __init__(self, log: RunLog, state: StateStore):
        self.log = log
        self.state = state
        self._privileges_ready = False
        # When True, every helper records the pre-change value into the undo snapshot.
        self.recording = True
        # What actually changed this run (for the summary shown afterwards).
        self.stats = {"changed": 0, "paused": [], "freed_mb": None, "plan": None}

    # ---------------- privileges ----------------

    def ensure_privileges(self) -> None:
        if self._privileges_ready:
            return
        self._privileges_ready = True
        for privilege in ("SeDebugPrivilege", "SeProfileSingleProcessPrivilege", "SeIncreaseQuotaPrivilege"):
            if not enable_privilege(privilege):
                self.log.info(f"{privilege} unavailable - some memory operations may be limited")

    # ---------------- registry ----------------

    def _remember_reg(self, hive: str, path: str, name: str, label: str, current) -> None:
        key = f"reg:{hive}\\{path}\\{name}"
        if not self.recording or self.state.snapshot_has(key):
            return
        entry = {"kind": "reg", "hive": hive, "path": path, "name": name, "label": label,
                 "present": current is not None}
        if current is not None:
            value, value_type = current
            if isinstance(value, (bytes, bytearray)):
                entry["hex"] = bytes(value).hex()
            else:
                entry["value"] = value
            entry["type"] = value_type
        self.state.snapshot_add(key, entry)

    def reg_set(self, hive: str, path: str, name: str, value, label: str, kind=winreg.REG_DWORD) -> bool:
        current = self.reg_read(hive, path, name)
        if current is not None and current[0] == value and current[1] == kind:
            self.log.ok(f"{label}: already {name}={value}")
            return True
        self._remember_reg(hive, path, name, label, current)
        try:
            with winreg.CreateKeyEx(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, name, 0, kind, value)
            self.log.ok(f"{label}: {name}={value}")
            self.stats["changed"] += 1
            return True
        except PermissionError:
            self.log.fail(f"{label}: access denied writing {hive}\\{path}\\{name}")
        except OSError as exc:
            self.log.fail(f"{label}: could not write {name} ({exc.strerror or exc})")
        return False

    def reg_delete(self, hive: str, path: str, name: str, label: str) -> bool:
        current = self.reg_read(hive, path, name)
        if current is None:
            self.log.ok(f"{label}: already at Windows default")
            return True
        self._remember_reg(hive, path, name, label, current)
        try:
            with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.DeleteValue(key, name)
            self.log.ok(f"{label}: {name} reset to Windows default")
            self.stats["changed"] += 1
            return True
        except FileNotFoundError:
            self.log.ok(f"{label}: already at Windows default")
            return True
        except PermissionError:
            self.log.fail(f"{label}: access denied removing {hive}\\{path}\\{name}")
        except OSError as exc:
            self.log.fail(f"{label}: could not reset {name} ({exc.strerror or exc})")
        return False

    @staticmethod
    def reg_read(hive: str, path: str, name: str):
        try:
            with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return winreg.QueryValueEx(key, name)
        except OSError:
            return None

    def apply_reg_table(self, table) -> None:
        for hive, path, name, value, label in table:
            if value is DELETE:
                self.reg_delete(hive, path, name, label)
            else:
                self.reg_set(hive, path, name, value, label)

    # ---------------- system parameters ----------------

    def _spi(self, action: int, ui_param: int, pv_param) -> bool:
        ok = user32.SystemParametersInfoW(action, ui_param, pv_param, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
        return bool(ok)

    def _spi_apply(self, action, style: str, value: int) -> bool:
        """style: 'struct' (minimize animation), 'ui' (value in uiParam),
        'pv' (BOOL passed as pvParam), 'pvval' (integer passed as pvParam)."""
        if action == "animation":
            info = ANIMATIONINFO(ctypes.sizeof(ANIMATIONINFO), 1 if value else 0)
            return self._spi(SPI_SETANIMATION, ctypes.sizeof(info), ctypes.byref(info))
        if style == "ui":
            return self._spi(action, int(value), None)
        if style == "pvval":
            return self._spi(action, 0, int(value) or None)
        return self._spi(action, 0, 1 if value else None)

    @staticmethod
    def _spi_get(action) -> int | None:
        if action == "animation":
            info = ANIMATIONINFO(ctypes.sizeof(ANIMATIONINFO), 0)
            ok = user32.SystemParametersInfoW(SPI_GETANIMATION, ctypes.sizeof(info), ctypes.byref(info), 0)
            return int(info.iMinAnimate) if ok else None
        get_action = SPI_GET_FOR_SET.get(action)
        if get_action is None:
            return None
        value = wintypes.DWORD(0)
        ok = user32.SystemParametersInfoW(get_action, 0, ctypes.byref(value), 0)
        return int(value.value) if ok else None

    def change_spi(self, action, style: str, value: int, label: str, shown: str) -> bool:
        """Apply a SystemParametersInfo setting, snapshotting its current value first."""
        current = self._spi_get(action)
        if current is not None:
            same = current == value if style == "pvval" else bool(current) == bool(value)
            if same:
                self.log.ok(f"{label}: already {shown}")
                return True
            key = f"spi:{action}"
            if self.recording and not self.state.snapshot_has(key):
                self.state.snapshot_add(
                    key, {"kind": "spi", "action": action, "style": style, "label": label, "value": current}
                )
        if self._spi_apply(action, style, value):
            self.log.ok(f"{label}: {shown}")
            self.stats["changed"] += 1
            return True
        self.log.fail(f"{label}: SystemParametersInfo failed (error {ctypes.get_last_error()})")
        return False

    def set_access_shortcut(self, which: str, active: bool) -> bool:
        """Switch one accessibility shortcut on or off, leaving the feature itself and its timings alone."""
        _which, label, struct, _get, set_action = ACCESS_BY_KEY[which]
        info = access_shortcut_get(which)
        if info is None:
            self.log.fail(f"{label}: Windows wouldn't say how it's set (error {ctypes.get_last_error()})")
            return False
        current = bool(info.dwFlags & ACCESS_HOTKEY_ON)
        shown = "on" if active else "off"
        if current == active:
            self.log.ok(f"{label}: already {shown}")
            return True
        key = f"access:{which}"
        if self.recording and not self.state.snapshot_has(key):
            self.state.snapshot_add(key, {"kind": "access", "which": which, "label": label, "value": current})
        info.dwFlags = (info.dwFlags | ACCESS_HOTKEY_ON) if active else (info.dwFlags & ~ACCESS_HOTKEY_ON)
        if self._spi(set_action, ctypes.sizeof(struct), ctypes.byref(info)):
            self.log.ok(f"{label}: {shown}")
            self.stats["changed"] += 1
            return True
        self.log.fail(f"{label}: SystemParametersInfo failed (error {ctypes.get_last_error()})")
        return False

    def disable_access_shortcuts(self) -> None:
        for which, *_rest in ACCESS_SHORTCUTS:
            self.set_access_shortcut(which, False)

    def _undo_access(self, entry: dict) -> bool:
        return self.set_access_shortcut(entry["which"], bool(entry["value"]))

    @staticmethod
    def broadcast_setting_change(area: str) -> None:
        result = ctypes.c_size_t(0)
        try:
            user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, area, SMTO_ABORTIFHUNG, 2000, ctypes.byref(result)
            )
        except Exception:
            pass

    def set_cleartype(self) -> None:
        self.change_spi(SPI_SETFONTSMOOTHING, "ui", 1, "Font smoothing", "ON")
        self.change_spi(SPI_SETFONTSMOOTHINGTYPE, "pvval", FE_FONTSMOOTHINGCLEARTYPE, "Font smoothing type", "ClearType")

    def apply_visual_mode(self, mode: str) -> None:
        fx_setting, categories, transparency, description = VISUAL_MODES[mode]
        self.log.head(f"Visual effects -> {description}")
        self.reg_set("HKCU", REG_VFX, "VisualFXSetting", fx_setting, "Visual effects preset")
        self.change_spi(SPI_SETUIEFFECTS, "pv", 1, "UI effects master switch", "ON")
        for label, action, style, category in VISUAL_SPI_EFFECTS:
            enabled = categories[category]
            self.change_spi(action, style, 1 if enabled else 0, label, "ON" if enabled else "OFF")
        for label, hive, path, name, category in VISUAL_REG_EFFECTS:
            self.reg_set(hive, path, name, 1 if categories[category] else 0, label)
        self.set_cleartype()
        if mode == "performance":
            self.log.info("ClearType kept on deliberately - it costs no FPS and keeps text readable")
        self.reg_set("HKCU", REG_PERSONALIZE, "EnableTransparency", 1 if transparency else 0, "Transparency effects")
        for area in ("WindowMetrics", "ImmersiveColorSet", "TraySettings", "Policy"):
            self.broadcast_setting_change(area)
        self.log.info("Taskbar/Explorer-only effects fully apply after the next sign-in")

    # ---------------- power plans ----------------

    def list_power_schemes(self) -> list[tuple[str, str, bool]]:
        rc, out = run_command([POWERCFG, "/list"], timeout=20)
        if rc != 0:
            self.log.warn(f"powercfg /list failed: {first_line(out)}")
            return []
        return [(m.group(1).lower(), m.group(2).strip(), bool(m.group(3))) for m in SCHEME_LINE_RE.finditer(out)]

    def set_active_scheme(self, guid: str) -> bool:
        active_guid, active_name = get_active_power_plan()
        if active_guid and active_guid == guid.lower():
            self.log.ok(f"Power plan: already {active_name}")
            return True
        if self.recording and active_guid and not self.state.snapshot_has("power"):
            self.state.snapshot_add(
                "power", {"kind": "power", "guid": active_guid, "name": active_name, "label": "Power plan"}
            )
        rc, out = run_command([POWERCFG, "/setactive", guid], timeout=20)
        if rc == 0:
            _, name = get_active_power_plan()
            self.log.ok(f"Power plan activated: {name}")
            self.stats["changed"] += 1
            self.stats["plan"] = name
            return True
        self.log.warn(f"powercfg /setactive {guid[:8]}… failed: {first_line(out)}")
        return False

    def _duplicate_scheme(self, source_guid: str, state_key: str, label: str) -> str | None:
        rc, out = run_command([POWERCFG, "-duplicatescheme", source_guid], timeout=20)
        match = GUID_RE.search(out) if rc == 0 else None
        if match:
            new_guid = match.group(1).lower()
            self.state.set(state_key, new_guid)
            self.log.ok(f"{label} plan unlocked ({new_guid[:8]}…)")
            return new_guid
        self.log.warn(f"Could not unlock {label} plan: {first_line(out)}")
        return None

    def _activate_builtin(self, source_guid: str, state_key: str, label: str, name_hint: str) -> bool:
        schemes = self.list_power_schemes()
        guids = {g for g, _, _ in schemes}
        if source_guid in guids and self.set_active_scheme(source_guid):
            return True
        saved = self.state.get(state_key)
        if saved and saved in guids and self.set_active_scheme(saved):
            return True
        for guid, name, _ in schemes:
            if name_hint in name.lower() and self.set_active_scheme(guid):
                return True
        new_guid = self._duplicate_scheme(source_guid, state_key, label)
        return bool(new_guid) and self.set_active_scheme(new_guid)

    def activate_balanced(self) -> bool:
        if self.set_active_scheme(GUID_BALANCED):
            return True
        self.log.fail("Balanced power plan could not be activated")
        return False

    def activate_high_performance(self) -> bool:
        if self._activate_builtin(GUID_HIGH, "created_high_guid", "High Performance", "high performance"):
            return True
        self.log.warn("High Performance plan unavailable on this device - falling back to Balanced")
        return self.activate_balanced()

    def activate_ultimate(self) -> bool:
        if self._activate_builtin(GUID_ULTIMATE, "created_ultimate_guid", "Ultimate Performance", "ultimate"):
            return True
        self.log.warn("Ultimate Performance unsupported here - falling back to High Performance")
        return self.activate_high_performance()

    def remove_created_schemes(self) -> None:
        active_guid, _ = get_active_power_plan()
        existing = {g for g, _, _ in self.list_power_schemes()}
        for key, label in (("created_ultimate_guid", "Ultimate Performance"), ("created_high_guid", "High Performance")):
            guid = self.state.get(key)
            if not guid:
                continue
            if guid not in existing:
                self.state.remove(key)
                continue
            if guid == active_guid:
                self.log.warn(f"Unlocked {label} plan is still active - left in place")
                continue
            rc, out = run_command([POWERCFG, "/delete", guid], timeout=20)
            if rc == 0:
                self.state.remove(key)
                self.log.ok(f"Removed unlocked {label} plan")
            else:
                self.log.warn(f"Could not remove unlocked {label} plan: {first_line(out)}")

    # ---------------- network ----------------

    def flush_dns(self) -> None:
        rc, out = run_command([IPCONFIG, "/flushdns"], timeout=30)
        if rc == 0:
            self.log.ok("DNS resolver cache flushed")
        else:
            self.log.fail(f"DNS flush failed: {first_line(out)}")

    # ---------------- services ----------------

    @staticmethod
    def service_status(name: str) -> str | None:
        try:
            return psutil.win_service_get(name).status()
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, OSError):
            return "unknown"

    @staticmethod
    def service_start_type(name: str) -> str | None:
        """Startup type in sc.exe syntax, read from the service's registry key (detects delayed-auto)."""
        path = f"SYSTEM\\CurrentControlSet\\Services\\{name}"
        start = SystemTweaks.reg_read("HKLM", path, "Start")
        if start is None:
            return None
        if start[0] == 2:
            delayed = SystemTweaks.reg_read("HKLM", path, "DelayedAutostart")
            return "delayed-auto" if delayed and delayed[0] == 1 else "auto"
        return {0: "boot", 1: "system", 3: "demand", 4: "disabled"}.get(start[0])

    def _remember_service(self, name: str, label: str) -> None:
        key = f"svc:{name}"
        if not self.recording or self.state.snapshot_has(key):
            return
        status = self.service_status(name)
        start = self.service_start_type(name)
        if status is None or start is None:
            return
        self.state.snapshot_add(key, {
            "kind": "svc", "name": name, "label": label, "start": start,
            "running": status in ("running", "start_pending"),
        })

    def _wait_service(self, name: str, target: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.service_status(name) == target:
                return True
            time.sleep(0.4)
        return self.service_status(name) == target

    def stop_service(self, name: str, label: str) -> bool:
        if name.lower() in PROTECTED_SERVICES:
            self.log.info(f"{label}: protected service - skipped")
            return False
        status = self.service_status(name)
        if status is None:
            self.log.info(f"{label}: not installed - skipped")
            return True
        if status == "stopped":
            self.log.ok(f"{label}: already stopped")
            return True
        self._remember_service(name, label)
        rc, out = run_command([SC_EXE, "stop", name], timeout=30)
        if rc in (0, 1062):
            if self._wait_service(name, "stopped", 20):
                self.log.ok(f"{label}: stopped")
                self.stats["paused"].append(label)
                return True
            self.log.warn(f"{label}: stop requested, still {self.service_status(name)}")
        elif rc == 1051:
            self.log.warn(f"{label}: other running services depend on it - left running")
        else:
            self.log.fail(f"{label}: stop failed (sc error {rc}: {first_line(out)})")
        return False

    def start_service(self, name: str, label: str) -> bool:
        status = self.service_status(name)
        if status is None:
            self.log.info(f"{label}: not installed - skipped")
            return True
        if status == "running":
            self.log.ok(f"{label}: already running")
            return True
        self._remember_service(name, label)
        rc, out = run_command([SC_EXE, "start", name], timeout=30)
        if rc in (0, 1056):
            if self._wait_service(name, "running", 20):
                self.log.ok(f"{label}: running")
                return True
            self.log.warn(f"{label}: start requested, currently {self.service_status(name)}")
        elif rc == 1058:
            self.log.warn(f"{label}: service is disabled - left as-is")
        else:
            self.log.fail(f"{label}: start failed (sc error {rc}: {first_line(out)})")
        return False

    def set_service_start(self, name: str, label: str, start_type: str) -> bool:
        if self.service_status(name) is None:
            self.log.info(f"{label}: not installed - skipped")
            return False
        if self.service_start_type(name) == start_type:
            self.log.ok(f"{label}: startup type already {start_type}")
            return True
        self._remember_service(name, label)
        rc, out = run_command([SC_EXE, "config", name, "start=", start_type], timeout=30)
        if rc == 0:
            self.log.ok(f"{label}: startup type -> {start_type}")
            return True
        self.log.fail(f"{label}: could not set startup type (sc error {rc}: {first_line(out)})")
        return False

    # ---------------- memory ----------------

    def memory_snapshot(self) -> dict:
        snapshot = {"available_mb": psutil.virtual_memory().available / MB, "free_mb": None, "standby_mb": None}
        info = SYSTEM_MEMORY_LIST_INFORMATION()
        returned = wintypes.ULONG(0)
        status = ntdll.NtQuerySystemInformation(
            SystemMemoryListInformation, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)
        )
        if status == 0:
            snapshot["free_mb"] = (info.ZeroPageCount + info.FreePageCount) * PAGE_SIZE / MB
            snapshot["standby_mb"] = sum(info.PageCountByPriority) * PAGE_SIZE / MB
        return snapshot

    @staticmethod
    def is_excluded(name: str, exe_path: str | None) -> bool:
        if name in EXCLUDED_PROCESSES:
            return True
        if exe_path and os.path.normcase(exe_path) in GAME_EXE_PATHS:
            return True  # never page out the user's own games
        if exe_path and EXCLUDED_INSTALL_PREFIXES:
            return os.path.normcase(os.path.abspath(exe_path)).startswith(EXCLUDED_INSTALL_PREFIXES)
        return False

    def trim_working_sets(self, gentle: bool) -> tuple[float, int]:
        own = {os.getpid(), os.getppid()}
        fg = foreground_pid()
        trimmed_bytes = 0
        trimmed = protected = excluded = spared = inaccessible = 0
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            pid = proc.info.get("pid") or 0
            name = (proc.info.get("name") or "").lower()
            if pid in (0, 4) or pid in own or name in PROTECTED_PROCESSES:
                protected += 1
                continue
            if self.is_excluded(name, proc.info.get("exe")):
                excluded += 1
                continue
            if gentle and (name in COZY_SPARED_PROCESSES or pid == fg):
                spared += 1
                continue
            try:
                before = proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                inaccessible += 1
                continue
            if gentle and before < 32 * MB:
                continue
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid)
            if not handle:
                inaccessible += 1
                continue
            try:
                ok = psapi.EmptyWorkingSet(handle)
            finally:
                kernel32.CloseHandle(handle)
            if not ok:
                inaccessible += 1
                continue
            trimmed += 1
            try:
                trimmed_bytes += max(0, before - proc.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                trimmed_bytes += before
        trimmed_mb = trimmed_bytes / MB
        self.log.ok(f"Working sets trimmed: {trimmed_mb:,.0f} MB from {trimmed} processes")
        detail = f"{protected} protected (system/audio/network/anti-cheat), {excluded} excluded (Medal and your games)"
        if gentle:
            detail += f", {spared} spared (Discord/Spotify/browsers/foreground)"
        if inaccessible:
            detail += f", {inaccessible} inaccessible"
        self.log.info(f"Skipped: {detail}")
        return trimmed_mb, trimmed

    def purge_standby(self, low_priority_only: bool) -> None:
        command = ctypes.c_int(MemoryPurgeLowPriorityStandbyList if low_priority_only else MemoryPurgeStandbyList)
        status = ntdll.NtSetSystemInformation(SystemMemoryListInformation, ctypes.byref(command), ctypes.sizeof(command))
        kind = "Low-priority standby cache" if low_priority_only else "Standby cache"
        if status == 0:
            self.log.ok(f"{kind} purged")
        else:
            self.log.fail(f"{kind} purge failed (NTSTATUS 0x{status & 0xFFFFFFFF:08X})")

    def optimize_memory(self, aggressive: bool) -> None:
        self.log.head("Memory cleanup -> " + ("aggressive" if aggressive else "gentle"))
        self.ensure_privileges()
        before = self.memory_snapshot()
        self.trim_working_sets(gentle=not aggressive)
        self.purge_standby(low_priority_only=not aggressive)
        time.sleep(0.3)
        after = self.memory_snapshot()
        if before["free_mb"] is not None and after["free_mb"] is not None:
            freed = max(0.0, after["free_mb"] - before["free_mb"])
            self.stats["freed_mb"] = freed
            self.log.ok(
                f"RAM freed: {freed:,.0f} MB  (free {before['free_mb']:,.0f} -> {after['free_mb']:,.0f} MB, "
                f"standby {before['standby_mb']:,.0f} -> {after['standby_mb']:,.0f} MB)"
            )
        else:
            freed = max(0.0, after["available_mb"] - before["available_mb"])
            self.stats["freed_mb"] = freed
            self.log.ok(f"RAM freed: {freed:,.0f} MB  (available now {after['available_mb']:,.0f} MB)")

    # ---------------- GPU scheduling ----------------

    def set_hags(self, enabled: bool) -> None:
        if self.reg_set("HKLM", REG_GRAPHICS, "HwSchMode", 2 if enabled else 1, "Hardware-accelerated GPU scheduling"):
            self.log.info("HAGS takes effect after a reboot (needs a WDDM 2.7+ GPU driver)")

    # ---------------- undo (restore the user's own previous values) ----------------

    def _undo_power(self, entry: dict) -> bool:
        guid = entry["guid"]
        if guid in {g for g, _, _ in self.list_power_schemes()}:
            return self.set_active_scheme(guid)
        self.log.warn(f"Your previous plan '{entry.get('name', guid)}' no longer exists - switching to Balanced")
        self.activate_balanced()
        return True

    def _undo_reg(self, entry: dict) -> bool:
        hive, path, name, label = entry["hive"], entry["path"], entry["name"], entry["label"]
        if not entry.get("present"):
            current = self.reg_read(hive, path, name)
            if current is None:
                self.log.ok(f"{label}: already back to not set")
                return True
            try:
                with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                    winreg.DeleteValue(key, name)
                self.log.ok(f"{label}: restored (value removed, as before)")
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                self.log.fail(f"{label}: could not remove {name} ({exc.strerror or exc})")
                return False
        value = bytes.fromhex(entry["hex"]) if "hex" in entry else entry.get("value")
        return self.reg_set(hive, path, name, value, label, kind=int(entry.get("type", winreg.REG_DWORD)))

    def _undo_spi(self, entry: dict) -> bool:
        value = int(entry["value"])
        style = entry["style"]
        shown = str(value) if style == "pvval" else ("ON" if value else "OFF")
        return self.change_spi(entry["action"], style, value, entry["label"], shown)

    def _undo_service(self, entry: dict) -> bool:
        name, label, start = entry["name"], entry["label"], entry["start"]
        if self.service_status(name) is None:
            self.log.info(f"{label}: no longer installed - nothing to restore")
            return True
        if entry.get("running"):
            ok = True
            if start != "disabled":
                ok = self.set_service_start(name, label, start)
            ok = self.start_service(name, label) and ok
            if start == "disabled":
                ok = self.set_service_start(name, label, start) and ok
            return ok
        stopped = self.stop_service(name, label)
        return self.set_service_start(name, label, start) and stopped

    def undo_changes(self) -> None:
        items = self.state.snapshot_items()
        if not items:
            self.log.info("Nothing to undo - Astra Optimizer hasn't changed any of your settings")
            return
        self.recording = False
        self.log.head(f"Putting back {len(items)} settings exactly as you had them")
        handlers = {
            "power": ("Power plan", self._undo_power),
            "reg": ("Registry settings", self._undo_reg),
            "spi": ("Visual effects", self._undo_spi),
            "access": ("Keyboard shortcuts", self._undo_access),
            "svc": ("Services", self._undo_service),
            "nvdrs": ("NVIDIA settings", self._undo_nvidia),
            "amd": ("AMD settings", self._undo_amd),
        }
        for kind, (title, handler) in handlers.items():
            group = [(key, entry) for key, entry in items if entry.get("kind") == kind]
            if not group:
                continue
            self.log.head(title)
            for key, entry in group:
                try:
                    restored = handler(entry)
                except Exception as exc:
                    restored = False
                    self.log.fail(f"{entry.get('label', key)}: {exc.__class__.__name__}: {exc}")
                if restored:
                    self.state.snapshot_discard(key)
        unknown = [key for key, entry in items if entry.get("kind") not in handlers]
        for key in unknown:
            self.state.snapshot_discard(key)
        self.remove_created_schemes()
        for area in ("WindowMetrics", "ImmersiveColorSet", "TraySettings", "Policy"):
            self.broadcast_setting_change(area)
        remaining = self.state.snapshot_count()
        if remaining:
            self.log.warn(f"{remaining} setting(s) could not be restored - kept so you can press UNDO again")
        else:
            self.log.ok("Everything Astra Optimizer changed is back to your previous values")
        self.log.info("RAM cleanup and DNS flush were one-off actions - there is nothing to undo for them")

    # ---------------- preview (read-only: nothing is changed) ----------------

    @classmethod
    def _reg_differs(cls, hive: str, path: str, name: str, value) -> bool:
        current = cls.reg_read(hive, path, name)
        if value is DELETE:
            return current is not None
        return not (current is not None and current[0] == value)

    @classmethod
    def _spi_differs(cls, action, style: str, value: int) -> bool:
        current = cls._spi_get(action)
        if current is None:
            return True
        return not (current == value if style == "pvval" else bool(current) == bool(value))

    @classmethod
    def _reg_changes(cls, table) -> int:
        return sum(cls._reg_differs(hive, path, name, value) for hive, path, name, value, _label in table)

    @classmethod
    def _visual_changes(cls, mode: str) -> int:
        fx_setting, categories, transparency, _ = VISUAL_MODES[mode]
        count = cls._reg_differs("HKCU", REG_VFX, "VisualFXSetting", fx_setting)
        count += cls._spi_differs(SPI_SETUIEFFECTS, "pv", 1)
        for _label, action, style, category in VISUAL_SPI_EFFECTS:
            count += cls._spi_differs(action, style, 1 if categories[category] else 0)
        for _label, hive, path, name, category in VISUAL_REG_EFFECTS:
            count += cls._reg_differs(hive, path, name, 1 if categories[category] else 0)
        count += cls._spi_differs(SPI_SETFONTSMOOTHING, "ui", 1)
        count += cls._spi_differs(SPI_SETFONTSMOOTHINGTYPE, "pvval", FE_FONTSMOOTHINGCLEARTYPE)
        count += cls._reg_differs("HKCU", REG_PERSONALIZE, "EnableTransparency", 1 if transparency else 0)
        return count

    @classmethod
    def plan(cls, profile: str) -> list[str]:
        """Plain-language list of what a mode would change on this PC right now."""
        lines: list[str] = []
        _, plan_name = get_active_power_plan()

        def plural(n: int, word: str) -> str:
            return f"{n} {word}{'' if n == 1 else 's'}"

        def power(target: str, hint: str) -> None:
            if hint in plan_name.lower():
                lines.append(f"Power plan: already {plan_name}")
            else:
                lines.append(f"Power plan: {plan_name} \u2192 {target}")

        def visual(mode: str, wording: str) -> None:
            n = cls._visual_changes(mode)
            lines.append(f"Visual effects: {plural(n, 'setting')} {wording}" if n else "Visual effects: already set")

        if profile == "max":
            power("Ultimate Performance", "ultimate")
            visual("performance", "switched to best performance")
            n = cls._reg_changes(GAME_DVR_OFF) + cls._reg_changes(GAME_MODE_ON)
            lines.append(f"Game Bar capture off and Game Mode on ({plural(n, 'setting')})" if n
                         else "Game Bar capture: already off")
            running = [SERVICE_SHORT.get(name, label) for name, label in SUSPEND_SERVICES
                       if name.lower() not in PROTECTED_SERVICES and cls.service_status(name) not in (None, "stopped")]
            lines.append(f"Pause until restart: {', '.join(running)}" if running
                         else "Background services: already paused")
            lines.append("Free RAM: trim background apps and clear the standby cache (Medal and your games are skipped)")
            keys = access_shortcuts_on()
            if keys:
                names = {"sticky": "Sticky Keys", "filter": "Filter Keys", "toggle": "Toggle Keys"}
                shown = [names[k] for k in keys]
                listed = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
                lines.append(f"{listed} shortcut{'s' if len(shown) > 1 else ''} off, "
                             "so Shift can't pop up a dialog mid-game")
            if windowed_games_pending():
                lines.append("Optimizations for windowed games and variable refresh rate on")
            nv = nvidia_pending("max")
            if nv:
                lines.append("NVIDIA: " + ", ".join(nv))
            amd = amd_pending("max")
            if amd:
                lines.append("AMD: " + ", ".join(amd))
            lines.append("Flush the DNS cache")
        elif profile == "cozy":
            power("High Performance", "high performance")
            lines.append("Free RAM gently: Discord, Spotify, browsers and the window in front are left alone")
            if cls._reg_differs("HKLM", REG_GRAPHICS, "HwSchMode", 2):
                lines.append("GPU scheduling on (takes effect after a restart)")
            visual("smooth", "changed for smooth animations")
            if cls._reg_changes(GAME_MODE_ON):
                lines.append("Game Mode on")
            if windowed_games_pending():
                lines.append("Optimizations for windowed games and variable refresh rate on")
            nv = nvidia_pending("cozy")
            if nv:
                lines.append("NVIDIA: " + ", ".join(nv))
            amd = amd_pending("cozy")
            if amd:
                lines.append("AMD: " + ", ".join(amd))
        else:
            if has_battery():
                power("Balanced (battery detected)", "balanced")
            else:
                power("High Performance", "high performance")
            visual("quality", "switched to best appearance")
            stopped = [SERVICE_SHORT.get(name, label) for name, label in QUALITY_RESTART_SERVICES
                       if cls.service_status(name) not in (None, "running")]
            if stopped:
                lines.append(f"Start again: {', '.join(stopped)}")
            n = cls._reg_changes(GAME_DVR_STOCK)
            if n:
                lines.append(f"Game Bar capture back to Windows defaults ({plural(n, 'setting')})")
        return lines

    # ---------------- profiles ----------------

    def apply_gpu_driver(self, profile: str) -> None:
        found = [self.apply_nvidia(profile, quiet=True), self.apply_amd(profile)]
        if not any(found):
            self.log.info("No NVIDIA or AMD graphics card - driver settings skipped")

    def apply_nvidia(self, profile: str, quiet: bool = False) -> bool:
        """True when an NVIDIA card was there to talk to."""
        wanted = NVIDIA_TWEAKS.get(profile) or []
        try:
            with NvidiaDrs() as nv:
                changed = False
                for sid, value, label, shown in wanted:
                    current = nv.get(sid)
                    if current == value:
                        self.log.ok(f"{label}: already {shown}")
                        continue
                    key = f"nvdrs:{sid:08X}"
                    if self.recording and not self.state.snapshot_has(key):
                        self.state.snapshot_add(key, {"kind": "nvdrs", "id": sid, "label": label, "value": current})
                    nv.set(sid, value)
                    changed = True
                    self.stats["changed"] += 1
                    self.log.ok(f"{label}: {shown}")
                if changed:
                    nv.save()
        except NvidiaError as exc:
            if exc.code == NV_DEVICE_NOT_FOUND:
                if not quiet:
                    self.log.info("No NVIDIA graphics card - driver settings skipped")
                return False
            self.log.warn(f"NVIDIA settings not changed: {exc}")
        except Exception as exc:  # never let a driver call break the rest of the mode
            self.log.warn(f"NVIDIA settings not changed: {exc.__class__.__name__}: {exc}")
        return True

    def apply_amd(self, profile: str) -> bool:
        """AMD Anti-Lag / Radeon Chill on every AMD GPU that has them. True when AMD's driver was there."""
        wanted = AMD_TWEAKS.get(profile) or []
        try:
            with AmdAdlx() as amd:
                if not amd.gpus:
                    return False
                several = len(amd.gpus) > 1
                for gpu in amd.gpus:
                    gid, name = amd.gpu_id(gpu), amd.gpu_name(gpu)
                    where = f" ({name})" if several else ""
                    for feature, value, label, shown in wanted:
                        current = amd.get(gpu, feature)
                        if current is None:
                            continue  # this GPU doesn't have it
                        if feature == "antilag" and value and amd.antilag_level(gpu) not in (None, ADLX_ANTILAG_PLAIN):
                            # Never switch on the newer in-driver variant: Anti-Lag+ got CS2 players banned in 2023.
                            self.log.info(f"{label}{where}: AMD Software has a newer Anti-Lag selected - left as it is")
                            continue
                        if current == value:
                            self.log.ok(f"{label}{where}: already {shown}")
                            continue
                        key = f"amd:{feature}:{gid}"
                        if self.recording and not self.state.snapshot_has(key):
                            self.state.snapshot_add(key, {"kind": "amd", "feature": feature, "gpu": gid,
                                                          "label": label, "value": current})
                        amd.set(gpu, feature, value)
                        self.stats["changed"] += 1
                        self.log.ok(f"{label}{where}: {shown}")
        except AmdError as exc:
            if exc.code == ADLX_NO_DRIVER:
                return False
            self.log.warn(f"AMD settings not changed: {exc}")
        except Exception as exc:  # never let a driver call break the rest of the mode
            self.log.warn(f"AMD settings not changed: {exc.__class__.__name__}: {exc}")
        return True

    def _undo_amd(self, entry: dict) -> bool:
        label = entry.get("label", "AMD setting")
        with AmdAdlx() as amd:
            gpu = next((g for g in amd.gpus if amd.gpu_id(g) == int(entry["gpu"])), None)
            if gpu is None:
                self.log.warn(f"{label}: that graphics card isn't there any more - nothing to put back")
                return True
            amd.set(gpu, entry["feature"], bool(entry["value"]))
        self.log.ok(f"{label}: restored ({'on' if entry['value'] else 'off'})")
        return True

    def _undo_nvidia(self, entry: dict) -> bool:
        with NvidiaDrs() as nv:
            if entry.get("value") is None:
                nv.reset(int(entry["id"]))
            else:
                nv.set(int(entry["id"]), int(entry["value"]))
            nv.save()
        self.log.ok(f"{entry.get('label', 'NVIDIA setting')}: "
                    + ("back to the driver default" if entry.get("value") is None else "restored"))
        return True

    def apply_windowed_games(self) -> None:
        """Optimizations for windowed games + VRR for older games (kept in the undo snapshot)."""
        current = self.reg_read("HKCU", GPU_PREF_KEY, DX_GLOBAL)
        target = dx_globals_with(current[0] if current else "", windowed_game_flags())
        if target is None:
            self.log.ok("Optimizations for windowed games: already on")
            return
        self.reg_set("HKCU", GPU_PREF_KEY, DX_GLOBAL, target, "Optimizations for windowed games", kind=winreg.REG_SZ)

    def profile_max_fps(self) -> None:
        self.log.head("Power")
        self.activate_ultimate()
        self.apply_visual_mode("performance")
        self.log.head("Game Bar / DVR")
        self.apply_reg_table(GAME_DVR_OFF)
        self.apply_reg_table(GAME_MODE_ON)
        self.log.head("Windowed games")
        self.apply_windowed_games()
        self.log.head("Graphics driver")
        self.apply_gpu_driver("max")
        self.log.head("Network")
        self.flush_dns()
        self.log.head("Keyboard shortcuts")
        self.disable_access_shortcuts()
        self.log.head("Background services (suspended until reboot)")
        for name, label in SUSPEND_SERVICES:
            self.stop_service(name, label)
        self.optimize_memory(aggressive=True)
        self.log.info("Tip: run Max FPS before launching your game so it starts with a clean slate")

    def profile_cozy(self) -> None:
        self.log.head("Power")
        self.activate_high_performance()
        self.optimize_memory(aggressive=False)
        self.log.head("GPU")
        self.set_hags(True)
        self.apply_visual_mode("smooth")
        self.log.head("Game Mode")
        self.apply_reg_table(GAME_MODE_ON)
        self.log.head("Windowed games")
        self.apply_windowed_games()
        self.log.head("Graphics driver")
        self.apply_gpu_driver("cozy")

    def profile_quality(self) -> None:
        self.log.head("Power")
        if has_battery():
            self.log.info("Battery detected - using Balanced for best efficiency")
            self.activate_balanced()
        else:
            self.activate_high_performance()
        self.apply_visual_mode("quality")
        self.log.head("Services")
        for name, label in QUALITY_RESTART_SERVICES:
            self.start_service(name, label)
        self.log.head("Game Bar / DVR -> defaults")
        self.apply_reg_table(GAME_DVR_STOCK)

    def restore_defaults(self) -> None:
        self.log.head("Power")
        self.activate_balanced()
        self.remove_created_schemes()
        self.apply_visual_mode("default")
        self.log.head("Game Bar / DVR / Game Mode -> defaults")
        self.apply_reg_table(GAME_DVR_STOCK)
        self.apply_reg_table(GAME_MODE_STOCK)
        self.log.head("GPU")
        self.log.info("GPU scheduling (HAGS) left as-is - its default depends on your GPU; UNDO reverts it")
        self.log.head("Services -> stock startup types")
        for name, label in SUSPEND_SERVICES:
            start_type = SERVICE_STOCK_START[name]
            if self.set_service_start(name, label, start_type) and start_type in ("auto", "delayed-auto"):
                self.start_service(name, label)


# --------------------------------------------------------------------------------------
# Telemetry thread
# --------------------------------------------------------------------------------------


class TelemetryThread(threading.Thread):
    def __init__(self, out_queue: queue.Queue, interval: float = TELEMETRY_INTERVAL):
        super().__init__(name="telemetry", daemon=True)
        self.out_queue = out_queue
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        psutil.cpu_percent(interval=None)
        try:
            gpu = GpuMonitor()
        except Exception:
            gpu = None
        wait = 0.4
        while not self.stop_event.wait(wait):
            wait = self.interval
            try:
                vm = psutil.virtual_memory()
                _, plan = get_active_power_plan()
                gpu_data = None
                if gpu is not None:
                    try:
                        gpu_data = gpu.read()
                    except Exception:
                        gpu_data = None
                self.out_queue.put((
                    "telemetry",
                    {
                        "cpu": psutil.cpu_percent(interval=None),
                        "ram": vm.percent,
                        "ram_used_gb": (vm.total - vm.available) / (1024 ** 3),
                        "ram_total_gb": vm.total / (1024 ** 3),
                        "plan": plan,
                        "gpu": gpu_data,
                        "gpu_name": gpu.name if gpu is not None else "",
                    },
                ))
            except Exception:
                continue


# --------------------------------------------------------------------------------------
# GPU telemetry: NVIDIA through the driver's NVML library (load + temperature), any other
# GPU through the Windows "GPU Engine" performance counters Task Manager uses (load only).
# --------------------------------------------------------------------------------------


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _PdhCounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _PdhCounterItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhCounterValue)]


PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
GPU_ENGINE_COUNTER = r"\GPU Engine(*engtype_3D)\Utilization Percentage"
REG_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def short_gpu_name(name: str) -> str:
    name = re.sub(r"\((R|TM)\)", "", name or "")
    for prefix in ("NVIDIA GeForce ", "NVIDIA ", "AMD ", "Intel "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return " ".join(name.split())


class GpuMonitor:
    """Created and read on the telemetry thread. read() -> {"load": %, "temp": °C or None} or None."""

    def __init__(self):
        self.name = ""
        self.source: str | None = None
        self._nvml = self._handle = None
        self._pdh = self._query = self._counter = None
        if not self._init_nvml():
            self._init_pdh()

    def _init_nvml(self) -> bool:
        program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
        for path in (os.path.join(SYSTEM32, "nvml.dll"),
                     os.path.join(program_files, "NVIDIA Corporation", "NVSMI", "nvml.dll")):
            if not os.path.isfile(path):
                continue
            try:
                nvml = ctypes.CDLL(path)
                if nvml.nvmlInit_v2() != 0:
                    continue
                handle = ctypes.c_void_p()
                if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
                    nvml.nvmlShutdown()
                    continue
                buffer = ctypes.create_string_buffer(96)
                if nvml.nvmlDeviceGetName(handle, buffer, 96) == 0:
                    self.name = buffer.value.decode("utf-8", "replace")
                self._nvml, self._handle, self.source = nvml, handle, "nvml"
                return True
            except (OSError, AttributeError):
                continue
        return False

    def _init_pdh(self) -> None:
        try:
            pdh = ctypes.WinDLL("pdh")
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhOpenQueryW.restype = wintypes.DWORD
            pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t,
                                                  ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
            pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
            pdh.PdhCollectQueryData.restype = wintypes.DWORD
            pdh.PdhGetFormattedCounterArrayW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         ctypes.POINTER(wintypes.DWORD),
                                                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
            pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
            query, counter = wintypes.HANDLE(), wintypes.HANDLE()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                return
            if pdh.PdhAddEnglishCounterW(query, GPU_ENGINE_COUNTER, 0, ctypes.byref(counter)) != 0:
                pdh.PdhCloseQuery(query)
                return
            pdh.PdhCollectQueryData(query)
            self._pdh, self._query, self._counter, self.source = pdh, query, counter, "pdh"
            self.name = self._adapter_name()
        except (OSError, AttributeError):
            self.source = None

    @staticmethod
    def _adapter_name() -> str:
        """First real display adapter (skips virtual/remote-display drivers)."""
        virtual = ("virtual", "spacedesk", "basic display", "basic render", "parsec", "remote", "indirect",
                   "citrix", "vmware", "hyper-v")
        for index in range(16):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_DISPLAY_CLASS + "\\%04d" % index, 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                    name = str(winreg.QueryValueEx(key, "DriverDesc")[0])
            except OSError:
                continue
            if not any(word in name.lower() for word in virtual):
                return name
        return ""

    def read(self) -> dict | None:
        if self.source == "nvml":
            util, temp = _NvmlUtilization(), ctypes.c_uint()
            if self._nvml.nvmlDeviceGetUtilizationRates(self._handle, ctypes.byref(util)) != 0:
                return None
            ok = self._nvml.nvmlDeviceGetTemperature(self._handle, 0, ctypes.byref(temp)) == 0
            data = {"load": float(util.gpu), "temp": int(temp.value) if ok else None}
            memory, clock, power = _NvmlMemory(), ctypes.c_uint(), ctypes.c_uint()
            if self._nvml.nvmlDeviceGetMemoryInfo(self._handle, ctypes.byref(memory)) == 0 and memory.total:
                data["vram_used"], data["vram_total"] = memory.used, memory.total
            if self._nvml.nvmlDeviceGetClockInfo(self._handle, 0, ctypes.byref(clock)) == 0:
                data["clock"] = int(clock.value)                 # graphics clock, MHz
            if self._nvml.nvmlDeviceGetPowerUsage(self._handle, ctypes.byref(power)) == 0:
                data["power"] = power.value / 1000.0             # milliwatts -> W
            return data
        if self.source == "pdh":
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return None
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
            status = self._pdh.PdhGetFormattedCounterArrayW(self._counter, fmt, ctypes.byref(size),
                                                            ctypes.byref(count), None)
            if status != PDH_MORE_DATA or not size.value:
                return {"load": 0.0, "temp": None}
            buffer = (ctypes.c_byte * size.value)()
            if self._pdh.PdhGetFormattedCounterArrayW(self._counter, fmt, ctypes.byref(size),
                                                      ctypes.byref(count), buffer) != 0:
                return None
            items = ctypes.cast(buffer, ctypes.POINTER(_PdhCounterItem))
            per_adapter: dict[str, float] = {}
            for i in range(count.value):
                item = items[i]
                if item.FmtValue.CStatus not in (0, 1):
                    continue
                match = re.search(r"luid_\w+?_phys_\d+", item.szName or "")
                key = match.group(0) if match else ""
                per_adapter[key] = per_adapter.get(key, 0.0) + item.FmtValue.doubleValue
            return {"load": min(100.0, max(per_adapter.values(), default=0.0)), "temp": None}
        return None


# --------------------------------------------------------------------------------------
# Ping tester
# --------------------------------------------------------------------------------------

PING_SAMPLES = 20
PING_INTERVAL = 0.25
PING_TIMEOUT_MS = 1000
PING_TARGETS = [
    # key, display name, host (None = default gateway), method: icmp | tcp (TCP 443 connect time)
    ("router", "Your router", None, "icmp"),
    ("cloudflare", "Cloudflare", "1.1.1.1", "icmp"),
    ("google", "Google DNS", "8.8.8.8", "icmp"),
    ("us-east", "US East (Virginia)", "dynamodb.us-east-1.amazonaws.com", "tcp"),
    ("eu-central", "Europe (Frankfurt)", "dynamodb.eu-central-1.amazonaws.com", "tcp"),
    ("ap-southeast", "Asia (Singapore)", "dynamodb.ap-southeast-1.amazonaws.com", "tcp"),
    ("ap-northeast", "Asia (Tokyo)", "dynamodb.ap-northeast-1.amazonaws.com", "tcp"),
]
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")


def _ipv4_dword(ip: str) -> int:
    return struct.unpack("<I", socket.inet_aton(ip))[0]


def default_gateway() -> str | None:
    """Next hop towards the internet (the router), or None when there is no route."""
    if iphlpapi is None:
        return None
    row = _MibIpForwardRow()
    try:
        if iphlpapi.GetBestRoute(_ipv4_dword("1.1.1.1"), 0, ctypes.byref(row)) != 0 or not row.next_hop:
            return None
        return socket.inet_ntoa(struct.pack("<I", row.next_hop))
    except (OSError, struct.error):
        return None


def icmp_ping(ip: str, timeout_ms: int = PING_TIMEOUT_MS) -> float | None:
    """One ICMP echo through IcmpSendEcho (no console window). Round trip in ms, None on loss."""
    if iphlpapi is None:
        return None
    handle = iphlpapi.IcmpCreateFile()
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        payload = b"OptimizerPingOptimizerPingOptimi"
        reply_size = ctypes.sizeof(_IcmpEchoReply) + len(payload) + 8 + 64
        reply = ctypes.create_string_buffer(reply_size)
        if not iphlpapi.IcmpSendEcho(handle, _ipv4_dword(ip), payload, len(payload), None, reply,
                                     reply_size, timeout_ms):
            return None
        echo = _IcmpEchoReply.from_buffer(reply)
        return float(echo.RoundTripTime) if echo.Status == 0 else None
    except (OSError, struct.error):
        return None
    finally:
        iphlpapi.IcmpCloseHandle(handle)


def tcp_ping(ip: str, port: int = 443, timeout: float = PING_TIMEOUT_MS / 1000) -> float | None:
    """TCP handshake time - works for servers that drop ICMP (cloud regions, most game hosts)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        started = time.perf_counter()
        sock.connect((ip, port))
        return (time.perf_counter() - started) * 1000.0
    except OSError:
        return None
    finally:
        sock.close()


# --------------------------------------------------------------------------------------
# Global hotkey, start with Windows, single instance and tray icon
# --------------------------------------------------------------------------------------

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY_LABEL = "Ctrl+Alt+O"
HOTKEY_VK = ord("O")
OVERLAY_HOTKEY_LABEL = "Ctrl+Alt+F"
OVERLAY_HOTKEY_VK = ord("F")
BENCH_HOTKEY_LABEL = "Ctrl+Alt+K"
BENCH_HOTKEY_VK = ord("K")
MOD_SHIFT = 0x0004
# action: (what it does, default modifiers, default key, RegisterHotKey id, event it posts)
HOTKEY_ACTIONS = {
    "toggle": ("Optimize or undo", MOD_CONTROL | MOD_ALT, HOTKEY_VK, 1, "hotkey"),
    "overlay": ("FPS overlay", MOD_CONTROL | MOD_ALT, OVERLAY_HOTKEY_VK, 2, "overlay-hotkey"),
    "bench": ("Benchmark", MOD_CONTROL | MOD_ALT, BENCH_HOTKEY_VK, 3, "bench-hotkey"),
}
_HOTKEY_KEY_NAMES = {0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End", 0x21: "Page Up", 0x22: "Page Down",
                     0x13: "Pause", 0x91: "Scroll Lock", **{0x60 + i: f"Num {i}" for i in range(10)},
                     **{0x70 + i: f"F{i + 1}" for i in range(24)}}
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def hotkey_key_name(vk: int) -> str | None:
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    return _HOTKEY_KEY_NAMES.get(vk)


def hotkey_label(mods: int, vk: int) -> str:
    parts = [name for bit, name in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift")) if mods & bit]
    return "+".join(parts + [hotkey_key_name(vk) or f"key {vk}"])


def hotkey_problem(mods: int, vk: int) -> str | None:
    """Why this can't be a hotkey, or None when it can."""
    if hotkey_key_name(vk) is None:
        return "Use a letter, a number, an F key or a key like Insert or Page Up."
    if 0x70 <= vk <= 0x87:
        return None  # an F key works on its own
    if not mods & (MOD_CONTROL | MOD_ALT):
        return "Add Ctrl or Alt, so it doesn't fire while you type."
    return None
AUTOSTART_TASK = "Astra Optimizer Autostart"
LEGACY_AUTOSTART_TASK = "Optimizer Autostart"
INSTANCE_EVENT = "Local\\AstraOptimizerShowWindow"
EVENT_MODIFY_STATE = 0x0002
SW_RESTORE = 9
DEFAULT_SETTINGS = {"tray": True, "autostart": False, "hotkey": True, "custom_host": "", "tray_hint": False,
                    "preview": True, "games": [], "game_auto": False, "clean_skip": [], "clean_weekly": False, "clean_at": 0,
                    "dns_original": None,
                    "restore_point": True, "restore_point_at": 0, "updates": True, "sessions": [],
                    "overlay": True, "overlay_pos": None, "overlay_style": None,
                    "slow_background": False, "hotkeys": {}, "startkey": False}


# --------------------------------------------------------------------------------------
# Update check (GitHub releases; it only looks, nothing is downloaded or installed)
# --------------------------------------------------------------------------------------

def parse_version(text: str | None) -> tuple[int, ...] | None:
    """'v1.0.2' -> (1, 0, 2); trailing zeros dropped so 'v1' == '1.0.0'."""
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "", re.IGNORECASE)
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer_version(tag: str | None, current: str = APP_VERSION) -> bool:
    latest, mine = parse_version(tag), parse_version(current)
    return latest is not None and mine is not None and latest > mine


def fetch_latest_release(timeout: float = 8.0) -> dict | None:
    """The newest published (non-draft, non-pre) release as {"tag", "url"}, or None if there
    is none yet. Raises OSError/ValueError when GitHub can't be reached or answers oddly."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(UPDATE_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"Astra-Optimizer/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(512 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # the repository has no releases yet
            return None
        raise OSError(f"GitHub answered {exc.code}") from exc
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        raise ValueError("the latest release has no version number")
    url = str(data.get("html_url") or "")
    if not url.startswith(RELEASES_URL + "/"):  # only ever open this project's own release pages
        url = RELEASES_URL
    release = {"tag": tag, "url": url}
    asset = next((a for a in data.get("assets") or [] if isinstance(a, dict) and a.get("name") == UPDATE_ASSET), None)
    download = str((asset or {}).get("browser_download_url") or "")
    checksum = re.search(r"SHA-256:\s*`?([0-9a-fA-F]{64})\b", str(data.get("body") or ""))
    # Self-update only from this project's own release files, and only with a checksum to verify against.
    if download.startswith(UPDATE_DOWNLOAD_PREFIX) and checksum:
        release.update(download=download, size=int(asset.get("size") or 0), sha256=checksum.group(1).lower())
    return release


def display_tag(tag: str) -> str:
    tag = tag.strip()
    return tag if tag[:1] in "vV" else f"v{tag}"


# --------------------------------------------------------------------------------------
# In-app updates. The release's exe is downloaded next to the running one and checked against
# the SHA-256 in the release notes. Windows won't let a running exe be overwritten but does let
# it be renamed, so the old one moves aside to "<name>.old", the new one takes its place and is
# started with --updated; that copy waits for this one to close and then deletes the .old file.
# --------------------------------------------------------------------------------------

UPDATE_ASSET = "Astra.Optimizer.exe"
UPDATE_DOWNLOAD_PREFIX = f"https://github.com/{UPDATE_REPO}/releases/download/"
UPDATE_MAX_BYTES = 300 * 1024 * 1024
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def download_update(release: dict, dest: str, progress=None, timeout: float = 30.0) -> None:
    """Download the release exe to dest. Raises OSError/ValueError; dest only appears once verified."""
    import hashlib
    import urllib.request
    request = urllib.request.Request(release["download"], headers={"User-Agent": f"Astra-Optimizer/{APP_VERSION}"})
    partial = dest + ".part"
    digest, done, shown = hashlib.sha256(), 0, -1
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as fh:
            total = int(response.headers.get("Content-Length") or release.get("size") or 0)
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > UPDATE_MAX_BYTES:
                    raise ValueError("the download is far bigger than expected")
                digest.update(chunk)
                fh.write(chunk)
                percent = min(99, done * 100 // total) if total else 0
                if progress is not None and percent != shown:
                    shown = percent
                    progress(percent)
        if digest.hexdigest() != release["sha256"]:
            raise ValueError("the download doesn't match the release's SHA-256 checksum")
        with open(partial, "rb") as fh:
            if fh.read(2) != b"MZ":
                raise ValueError("the download isn't a Windows program")
        os.replace(partial, dest)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def install_update(exe: str, new_file: str) -> str:
    """Swap new_file in for the (running) exe. Returns where the old exe went."""
    old = exe + ".old"
    try:
        os.remove(old)
    except FileNotFoundError:
        pass
    except OSError:  # still locked by a copy that hasn't fully closed yet
        old = f"{exe}.{int(time.time())}.old"
    os.replace(exe, old)
    try:
        os.replace(new_file, exe)
    except OSError:
        os.replace(old, exe)  # put the running version back
        raise
    return old


def launch_updated(exe: str) -> None:
    """Start the freshly installed exe as a clean, separate process."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # don't reuse this process's unpacked files
    args = [exe, "--updated"] + [a for a in sys.argv[1:] if a == "--no-elevate"]
    try:
        subprocess.Popen(args, env=env, cwd=os.path.dirname(exe) or None, close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 740:  # ERROR_ELEVATION_REQUIRED: this copy runs without admin
            raise
        os.environ.clear()  # ShellExecute passes this process's environment on; it exits right after
        os.environ.update(env)
        result = shell32.ShellExecuteW(None, "open", exe, subprocess.list2cmdline(args[1:]),
                                       os.path.dirname(exe) or None, 1)
        if int(result or 0) <= 32:
            raise OSError(f"Windows couldn't start the new version (code {result})") from exc


def wait_for_previous_instance(timeout: float = 30.0) -> None:
    """After an update: wait until the old copy has closed its instance event (it exits right away)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, INSTANCE_EVENT)
        if not handle:
            return
        kernel32.CloseHandle(handle)
        time.sleep(0.25)


def update_leftovers(exe: str) -> list[str]:
    folder, name = os.path.split(exe)
    try:
        entries = os.listdir(folder or ".")
    except OSError:
        return []
    pattern = re.compile(re.escape(name) + r"(\.\d+)?\.old$|" + re.escape(name) + r"\.download(\.part)?$",
                         re.IGNORECASE)
    return [os.path.join(folder, e) for e in entries if pattern.match(e)]


def remove_update_leftovers(exe: str, attempts: int = 40) -> None:
    """Delete the previous version's exe; it can stay locked for a moment while that copy exits."""
    for _ in range(attempts):
        left = []
        for path in update_leftovers(exe):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                left.append(path)
        if not left:
            return
        time.sleep(1.5)


# --------------------------------------------------------------------------------------
# Problem reports: one zip with what's needed to understand a bug report from someone else's PC.
# --------------------------------------------------------------------------------------


def write_crash_log(text: str) -> None:
    """Append a crash traceback to the log, even before (or after) the window exists."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} [CRASH] {APP_NAME} {DISPLAY_VERSION}\n{text.rstrip()}\n")
    except OSError:
        pass


def crash_summary(exc_type, exc) -> str:
    text = f"{getattr(exc_type, '__name__', 'Error')}: {exc}" if str(exc) else getattr(exc_type, "__name__", "Error")
    return text if len(text) <= 160 else text[:157] + "…"


def desktop_folder() -> str:
    buffer = ctypes.create_unicode_buffer(260)
    try:
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer) == 0 and buffer.value:
            return buffer.value  # CSIDL_DESKTOPDIRECTORY - follows OneDrive's moved Desktop too
    except (AttributeError, OSError):
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _reg_text(path: str, name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def windows_description() -> str:
    nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    product, release = _reg_text(nt, "ProductName") or "Windows", _reg_text(nt, "DisplayVersion")
    build, ubr = _reg_text(nt, "CurrentBuild"), _reg_text(nt, "UBR")
    if build.isdigit() and int(build) >= 22000:  # Windows 11 still says "Windows 10" in ProductName
        product = product.replace("Windows 10", "Windows 11")
    return " ".join(part for part in (product, release, f"(build {build}.{ubr})" if build else "") if part)


def cpu_description() -> str:
    name = _reg_text(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString").strip()
    cores, threads = psutil.cpu_count(logical=False), psutil.cpu_count(logical=True)
    return f"{name or 'Unknown CPU'} ({cores} cores, {threads} threads)"


def problem_report_text(app_info: dict, system: dict, checkup: dict, sessions: list[dict]) -> str:
    gb = psutil.virtual_memory().total / 1024 ** 3
    lines = [f"{APP_NAME} problem report", f"Made {_dt.datetime.now():%Y-%m-%d %H:%M}", "",
             "PC", f"  Windows: {windows_description()}", f"  CPU: {cpu_description()}", f"  RAM: {gb:.1f} GB"]
    for module in system.get("memory") or []:
        lines.append(f"    {module.get('part', '').strip() or 'module'}: {module.get('configured') or '?'} MT/s "
                     f"(rated {module.get('speed') or '?'})")
    for gpu in system.get("gpus") or []:
        lines.append(f"  GPU: {gpu.get('name')} - driver {gpu.get('version')} ({gpu.get('date') or 'no date'})")
    lines.append(f"  Laptop: {'yes' if has_battery() else 'no'}")
    net = system.get("net") or {}
    if net:
        lines.append(f"  Network: {net.get('desc') or net.get('name')}")
    lines += ["", "Astra"] + [f"  {key}: {value}" for key, value in app_info.items()]
    lines += ["", "Checkup"]
    for key, title in CHECKUP_ITEMS:
        row = checkup.get(key) or {}
        value = f" - {row['value']}" if row.get("value") else ""
        lines.append(f"  [{row.get('status', '?')}] {title}{value}: {row.get('detail', '')}")
    if checkup.get("error"):
        lines.append(f"  Checkup failed: {checkup['error']}")
    lines += ["", f"Last {len(sessions)} game session{'s' if len(sessions) != 1 else ''}" if sessions
              else "No game sessions yet"]
    for report in reversed(sessions):
        lines.append(f"  {report.get('name') or os.path.basename(report.get('exe', '')) or 'Game'}")
        try:
            lines += [f"    {line}" for line in session_report_text(report).splitlines() if line]
        except (KeyError, TypeError, ValueError):
            lines.append("    (report incomplete)")
    return "\n".join(lines) + "\n"


def write_problem_report(path: str, app_info: dict, games: list[dict], sessions: list[dict], state_file: str) -> None:
    import zipfile
    system = checkup_system_info()
    try:
        checkup = run_checkup(games, info=system)
    except Exception as exc:
        checkup = {"error": str(exc) or type(exc).__name__}
    checkup.pop("_setup", None)
    text = problem_report_text(app_info, system, checkup, sessions)
    partial = path + ".part"
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report.txt", text)
            zf.writestr("details.json", json.dumps({"app": app_info, "system": system, "checkup": checkup,
                                                    "sessions": sessions}, indent=2, default=str))
            for source, name in ((LOG_FILE, "optimizer.log"), (LOG_FILE + ".old", "optimizer.log.old"),
                                 (state_file, "settings.json")):
                if os.path.isfile(source):
                    zf.write(source, name)
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


class HotkeyThread(threading.Thread):
    """Owns a RegisterHotKey registration and its message loop; posts ("hotkey",) to the queue."""

    def __init__(self, out_queue: queue.Queue, hotkey_id: int = 1, vk: int = HOTKEY_VK, event: str = "hotkey",
                 modifiers: int = MOD_CONTROL | MOD_ALT):
        super().__init__(name=event, daemon=True)
        self.out_queue = out_queue
        self.hotkey_id, self.vk, self.event, self.modifiers = hotkey_id, vk, event, modifiers
        self.ready = threading.Event()
        self.ok = False
        self.thread_id = 0

    def run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        self.ok = bool(user32.RegisterHotKey(None, self.hotkey_id, self.modifiers | MOD_NOREPEAT, self.vk))
        self.ready.set()
        if not self.ok:
            return
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.out_queue.put((self.event,))
        finally:
            user32.UnregisterHotKey(None, self.hotkey_id)

    def stop(self) -> None:
        if self.ok and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            if self.is_alive() and threading.current_thread() is not self:
                self.join(1.0)  # its finally unregisters the key; wait so it can be taken again at once


class WinKeyGuard:
    """Keeps a lone Windows key press from opening Start while a marked game is the window in front.
    The keyboard hook runs in a small helper process (see winkey_guard_main) that only exists while such a
    game is running: inside Astra a key could wait ~5 ms behind its other threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.pids: frozenset[int] = frozenset()
        self.proc: subprocess.Popen | None = None
        self._event = None
        self._count = 0
        self.failed = False

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @staticmethod
    def command(event_name: str, pids) -> list[str]:
        own = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        return [*own, WINKEY_GUARD_ARG, event_name, str(os.getpid()), ",".join(str(p) for p in sorted(pids))]

    def update(self, pids) -> None:
        """Called by the game watcher with the running games that want the key blocked."""
        pids = frozenset(pids)
        with self._lock:
            if pids == self.pids:
                if self.proc is not None and self.proc.poll() == WINKEY_HOOK_FAILED:
                    self.failed = True
                return
            self._end()
            self.pids = pids
            if not pids:
                return
            self._count += 1
            name = f"Local\\AstraWinKeyGuard-{os.getpid()}-{self._count}"
            self._event = kernel32.CreateEventW(None, True, False, name)
            try:
                self.proc = subprocess.Popen(self.command(name, pids), creationflags=CREATE_NO_WINDOW,
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, close_fds=True)
            except OSError:
                self.proc, self.failed = None, True

    def _end(self) -> None:
        if self._event:
            kernel32.SetEvent(self._event)
        if self.proc is not None:
            try:
                self.proc.wait(2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None

    def stop(self) -> None:
        self.update(())


def launch_command() -> tuple[str, str]:
    """(program, arguments) that start this copy of the app hidden in the tray."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--tray"
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    program = pythonw if os.path.isfile(pythonw) else sys.executable
    return program, subprocess.list2cmdline([os.path.abspath(sys.argv[0]), "--tray"])


AUTOSTART_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts Astra Optimizer in the system tray when you sign in.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT10S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{command}</Command><Arguments>{args}</Arguments></Exec>
  </Actions>
</Task>
"""


class AutoStart:
    """A logon scheduled task (not the Run key): it can start the app elevated without a UAC
    prompt, and it is set never to time out or skip on battery."""

    @staticmethod
    def is_enabled() -> bool:
        rc, _ = run_command([SCHTASKS, "/Query", "/TN", AUTOSTART_TASK], timeout=15)
        return rc == 0

    @staticmethod
    def enable() -> tuple[bool, str]:
        program, args = launch_command()
        domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
        user = f"{domain}\\{name}" if domain else name
        xml = AUTOSTART_XML.format(user=xml_escape(user), command=xml_escape(program), args=xml_escape(args))
        path = os.path.join(STATE_DIR, "autostart.xml")
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-16") as fh:
                fh.write(xml)
        except OSError as exc:
            return False, str(exc)
        try:
            rc, out = run_command([SCHTASKS, "/Create", "/TN", AUTOSTART_TASK, "/XML", path, "/F"], timeout=20)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return rc == 0, first_line(out)

    @staticmethod
    def disable() -> tuple[bool, str]:
        rc, out = run_command([SCHTASKS, "/Delete", "/TN", AUTOSTART_TASK, "/F"], timeout=20)
        return rc == 0 or not AutoStart.is_enabled(), first_line(out)


def signal_running_instance() -> bool:
    """If the app is already running (maybe hidden in the tray), ask it to show itself."""
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, INSTANCE_EVENT)
    if not handle:
        return False
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


def create_instance_event():
    """Named event other copies signal. Its DACL lets a non-elevated copy reach an elevated one."""
    descriptor = ctypes.c_void_p()
    attributes = None
    if advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "D:(A;;GA;;;WD)S:(ML;;NW;;;LW)", 1, ctypes.byref(descriptor), None):
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    handle = kernel32.CreateEventW(ctypes.byref(attributes) if attributes else None, False, False, INSTANCE_EVENT)
    if descriptor:
        kernel32.LocalFree(descriptor)
    return handle or None


class TrayIcon:
    """pystray icon on its own thread. Menu clicks are queued to the UI thread, never run here."""

    def __init__(self, app: "OptimizerApp"):
        self.app = app
        self.icon = None
        self.running = False

    def _image(self) -> Image.Image:
        return self.app.painter.app_icon(64)

    def start(self) -> None:
        app = self.app

        def send(action):
            return lambda icon, item: app.events.put(("tray", action))

        modes = [
            pystray.MenuItem(PROFILE_TITLES[key], send(f"profile:{key}"), radio=True,
                             checked=lambda item, k=key: app.profile == k,
                             enabled=lambda item: not app.undo_count and not app.busy)
            for key in PROFILE_ORDER
        ]
        menu = pystray.Menu(
            pystray.MenuItem(f"Open {APP_NAME}", send("open"), default=True),
            pystray.MenuItem(lambda item: app.tray_primary_label(), send("toggle"),
                             enabled=lambda item: app.admin and not app.busy),
            pystray.MenuItem("Mode", pystray.Menu(*modes)),
            pystray.MenuItem(lambda item: f"Overlay ({app._hk('overlay')})", send("overlay"),
                             checked=lambda item: app.overlay_visible()),
            pystray.MenuItem(lambda item: "Cancel benchmark" if app.bench else f"Benchmark {BENCH_SECONDS} s ({app._hk('bench')})",
                             send("bench"), enabled=lambda item: bool(app.bench or app.sessions_live)),
            pystray.MenuItem("Report a problem", send("report"), enabled=lambda item: not app.report_busy),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Exit {APP_NAME}", send("exit")),
        )
        self.icon = pystray.Icon(APP_NAME, self._image(), app.tray_tooltip(), menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        self.running = True

    def refresh(self) -> None:
        if not self.running:
            return
        try:
            self.icon.title = self.app.tray_tooltip()
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, title: str, message: str) -> None:
        if self.running:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

    def stop(self) -> None:
        if self.running:
            self.running = False
            try:
                self.icon.stop()
            except Exception:
                pass


# --------------------------------------------------------------------------------------
# Games: priority boost, fullscreen optimizations, start/stop detection
# --------------------------------------------------------------------------------------

GAME_EXE_PATHS: set[str] = set()  # normcase paths of the user's games (RAM trimming skips them)
REG_LAYERS = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
FSO_FLAG = "DISABLEDXMAXIMIZEDWINDOWEDMODE"

try:
    _version = ctypes.WinDLL("version")
    _version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    _version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    _version.GetFileVersionInfoW.restype = wintypes.BOOL
    _version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                        ctypes.POINTER(wintypes.UINT)]
    _version.VerQueryValueW.restype = wintypes.BOOL
except (OSError, AttributeError):
    _version = None


def exe_display_name(path: str) -> str:
    """The name a program gives itself (FileDescription / ProductName), else its file name."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if _version is None:
        return stem
    try:
        size = _version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return stem
        buffer = ctypes.create_string_buffer(size)
        if not _version.GetFileVersionInfoW(path, 0, size, buffer):
            return stem
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        languages = []
        if _version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer),
                                   ctypes.byref(length)) and length.value >= 4:
            data = ctypes.string_at(pointer.value, length.value)
            languages = [struct.unpack_from("<HH", data, i) for i in range(0, length.value - 3, 4)]
        languages += [(0x0409, 0x04B0), (0x0409, 0x04E4)]
        for field in ("FileDescription", "ProductName"):
            for language, codepage in languages:
                query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}"
                if _version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) \
                        and length.value > 1:
                    text = ctypes.wstring_at(pointer.value, length.value).strip("\x00 ").strip()
                    if text and len(text) <= 48:
                        return text
    except (OSError, ValueError, struct.error):
        pass
    return stem


def fso_disabled(path: str) -> bool:
    current = SystemTweaks.reg_read("HKCU", REG_LAYERS, path)
    return bool(current) and FSO_FLAG in str(current[0]).split()


def set_fso_disabled(path: str, disabled: bool) -> None:
    """Same switch as the exe's Properties > Compatibility > 'Disable fullscreen optimizations'.
    Other compatibility flags on the same exe are kept. Raises OSError on failure."""
    current = SystemTweaks.reg_read("HKCU", REG_LAYERS, path)
    flags = [f for f in (str(current[0]).split() if current else []) if f not in ("~", FSO_FLAG)]
    if disabled:
        flags.append(FSO_FLAG)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_LAYERS, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        if flags:
            winreg.SetValueEx(key, path, 0, winreg.REG_SZ, "~ " + " ".join(flags))
        else:
            try:
                winreg.DeleteValue(key, path)
            except FileNotFoundError:
                pass


def set_process_priority(pid: int, high: bool) -> str | None:
    """None on success, else a short reason."""
    try:
        psutil.Process(pid).nice(psutil.HIGH_PRIORITY_CLASS if high else psutil.NORMAL_PRIORITY_CLASS)
        return None
    except psutil.AccessDenied:
        return "access denied (anti-cheat may block it)"
    except psutil.NoSuchProcess:
        return "it already closed"
    except (psutil.Error, OSError) as exc:
        return str(exc)


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


_proc_api = ctypes.WinDLL("kernel32", use_last_error=True)  # private function objects for these argtypes
_proc_api.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_proc_api.SetProcessInformation.restype = wintypes.BOOL
_proc_api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_proc_api.OpenProcess.restype = wintypes.HANDLE
PROCESS_SET_INFORMATION = 0x0200
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4


def _set_power_throttling(pid: int, attempts: tuple[tuple[int, int], ...]) -> bool:
    """SetProcessInformation(ProcessPowerThrottling) with the first (control, state) pair that works."""
    try:
        handle = _proc_api.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    except Exception:
        return False
    if not handle:
        return False
    try:
        for control, state_mask in attempts:
            state = _PowerThrottlingState(1, control, state_mask)
            if _proc_api.SetProcessInformation(handle, 4, ctypes.byref(state), ctypes.sizeof(state)):
                return True
        return False
    finally:
        kernel32.CloseHandle(handle)


def disable_power_throttling(pid: int) -> bool:
    """Opt a game out of Windows' power throttling (EcoQoS), so it isn't slowed down to save power
    and its timer requests are honoured even when it's in the background. Lasts until it exits."""
    both = PROCESS_POWER_THROTTLING_EXECUTION_SPEED | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
    # control these flags, state 0 = never throttle; the timer flag needs Windows 11
    return _set_power_throttling(pid, ((both, 0), (PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)))


def set_efficiency_mode(pid: int, on: bool) -> bool:
    """Windows' efficiency mode (EcoQoS) for one process: on = run it slowly and on efficient cores,
    off = hand the choice back to Windows (what every process starts with)."""
    speed = PROCESS_POWER_THROTTLING_EXECUTION_SPEED
    return _set_power_throttling(pid, ((speed, speed),) if on else ((0, 0),))


# Apps that keep working in the background while you play. While a game runs, the "Slow background
# apps" switch drops them to below-normal priority and efficiency mode; both go back when the game
# closes. Voice, music and anything a game needs (Discord, Spotify, Teams, the Steam client itself,
# anti-cheat, Riot's client) are deliberately missing so calls and audio never crackle.
BACKGROUND_APPS = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "opera.exe": "Opera", "vivaldi.exe": "Vivaldi",
    "steamwebhelper.exe": "Steam's store window", "epicgameslauncher.exe": "Epic Games Launcher",
    "epicwebhelper.exe": "Epic Games Launcher", "battle.net.exe": "Battle.net", "eadesktop.exe": "EA app",
    "upc.exe": "Ubisoft Connect", "galaxyclient.exe": "GOG Galaxy",
    "onedrive.exe": "OneDrive", "dropbox.exe": "Dropbox", "googledrivefs.exe": "Google Drive",
}


class BackgroundApps:
    """Slows BACKGROUND_APPS while a game runs and puts them back afterwards. What it touched is kept
    in a small file, so if Astra Optimizer is closed or crashes mid-game the next start undoes it."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.touched: dict[int, dict] = {}  # pid -> {"name", "created"}
        self.closed = False  # set on exit, so a scan finishing late can't slow anything again
        try:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.touched = {int(pid): info for pid, info in saved.items()
                            if isinstance(info, dict) and isinstance(info.get("created"), (int, float))}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self) -> None:
        try:
            if self.touched:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump({str(pid): info for pid, info in self.touched.items()}, handle)
                os.replace(tmp, self.path)
            elif os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass

    def slow(self, candidates: list) -> list[str]:
        """Slow the given psutil processes (already filtered to BACKGROUND_APPS). Only ones at normal
        priority are touched, so a priority someone chose on purpose is left alone. Returns the
        names of the apps newly slowed."""
        names = []
        with self._lock:
            for proc in candidates if not self.closed else ():
                pid = proc.pid
                if pid in self.touched:
                    continue
                try:
                    if proc.nice() != psutil.NORMAL_PRIORITY_CLASS:
                        continue
                    created = proc.create_time()
                    proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                except (psutil.Error, OSError):
                    continue
                set_efficiency_mode(pid, True)
                name = (proc.info.get("name") or "").lower() if hasattr(proc, "info") else ""
                self.touched[pid] = {"name": name, "created": created}
                names.append(BACKGROUND_APPS.get(name, name))
            if names:
                self._save()
        return names

    def restore(self) -> int:
        """Put every process slowed earlier back to normal. Returns how many were still running."""
        count = 0
        with self._lock:
            for pid, info in self.touched.items():
                try:
                    proc = psutil.Process(pid)
                    if abs(proc.create_time() - info["created"]) > 1:  # the pid now belongs to something else
                        continue
                    if proc.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS:
                        proc.nice(psutil.NORMAL_PRIORITY_CLASS)
                    set_efficiency_mode(pid, False)
                    count += 1
                except (psutil.Error, OSError):
                    continue
            self.touched.clear()
            self._save()
        return count


class GameWatcher(threading.Thread):
    """Checks running processes every 2 s for the user's games, raises their priority when they
    start, and reports starts/stops to the UI queue."""

    def __init__(self, out_queue: queue.Queue, background: BackgroundApps | None = None):
        super().__init__(name="games", daemon=True)
        self.out_queue = out_queue
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._games: dict[str, bool] = {}  # normcase path -> raise priority
        self.running: dict[int, str] = {}   # pid -> normcase path
        self.background = background or BackgroundApps(os.path.join(STATE_DIR, "background_apps.json"))
        self.slow_background = False
        self._slow_announced = False
        self.winkey = WinKeyGuard()
        self.winkey_enabled = False  # Settings > Windows > Block the Start menu key in games
        self._winkey_paths: set[str] = set()
        self._winkey_warned = False

    def set_games(self, games: list[dict]) -> None:
        table = {os.path.normcase(g["path"]): bool(g.get("priority", True)) for g in games}
        winkey = {os.path.normcase(g["path"]) for g in games if g.get("winkey")}
        with self._lock:
            self._games = table
            self._winkey_paths = winkey
        GAME_EXE_PATHS.clear()
        GAME_EXE_PATHS.update(table)

    def run(self) -> None:
        wait = 0.5
        while not self.stop_event.wait(wait):
            wait = 2.0
            try:
                self._scan()
            except Exception:
                continue

    def _scan(self) -> None:
        with self._lock:
            games = dict(self._games)
        names = {os.path.basename(path) for path in games}
        seen: dict[int, str] = {}
        extra: list = []  # background apps running right now
        if names:
            for proc in psutil.process_iter(["pid", "name"]):
                name = (proc.info.get("name") or "").lower()
                if name in BACKGROUND_APPS and name not in names:
                    extra.append(proc)
                if name not in names:
                    continue
                try:
                    exe = os.path.normcase(proc.exe())
                except (psutil.Error, OSError):
                    continue
                if exe in games:
                    seen[proc.info["pid"]] = exe
        for pid, exe in seen.items():
            if pid not in self.running:
                wanted = games.get(exe, False)
                error = set_process_priority(pid, True) if wanted else None
                unthrottled = disable_power_throttling(pid)
                self.out_queue.put(("game-start", pid, exe, wanted, error, unthrottled))
        for pid, exe in self.running.items():
            if pid not in seen:
                self.out_queue.put(("game-stop", pid, exe))
        self.running = seen
        self._update_background(bool(seen), extra)
        self._update_winkey(seen)

    def _update_winkey(self, seen: dict[int, str]) -> None:
        with self._lock:
            paths = set(self._winkey_paths) or set(self._games)  # none marked: every game on the list
        on = self.winkey_enabled and not self.stop_event.is_set()
        wanted = {pid for pid, exe in seen.items() if exe in paths} if on else set()
        self.winkey.update(wanted)
        if wanted and self.winkey.failed and not self._winkey_warned:
            self._winkey_warned = True
            self.out_queue.put(("winkey-failed",))

    def _update_background(self, playing: bool, extra: list) -> None:
        """Slow background apps while a game runs (new ones too: browsers keep starting processes),
        and put them back once no game is left, the switch goes off or Astra Optimizer closes."""
        if playing and self.slow_background and not self.stop_event.is_set():
            names = self.background.slow(extra)
            if names and not self._slow_announced:
                self._slow_announced = True
                self.out_queue.put(("background-slowed", names))
        elif self.background.touched:
            count = self.background.restore()
            if self._slow_announced or count:
                self.out_queue.put(("background-restored", count))
            self._slow_announced = False
        else:
            self._slow_announced = False

    def stop(self) -> None:
        """Stop scanning and put background apps back straight away."""
        self.stop_event.set()
        self.winkey.stop()
        with self.background._lock:
            self.background.closed = True
        self.background.restore()


# Finding installed games (Steam, Epic, Riot, GOG)

_NOT_GAME_EXE = re.compile(
    r"unins|setup|install|redist|vc_?redist|dxsetup|directx|crash|report|helper|launcher|updater|update|dotnet|"
    r"bootstrap|webview|msedge|vconsole|dump|patch|qtwebengine|service|tracing|"
    r"prereq|easyanticheat|anticheat|battleye|beservice|belauncher|cefsharp|webhelper|overlay|benchmark|config|settings|"
    r"dedicatedserver|editor|python|java|7z|unitycrash|handler|cleanup|uploader|activation|register", re.IGNORECASE)
_NOT_GAME_DIR = re.compile(r"webview|redist|directx|prereq|crashreport|_support|^support$|^tools?$|dotnet|"
                           r"easyanticheat|battleye|uninstall", re.IGNORECASE)
_NOT_GAME_TITLES = ("steamworks common", "proton", "steam linux runtime", "steamvr", "wallpaper engine")


def _main_exe(folder: str, title: str) -> str | None:
    """Best guess at a game's real executable: the largest .exe (within 4 levels) that isn't a
    launcher, installer or crash reporter, preferring names that echo the game's title."""
    if not os.path.isdir(folder):
        return None
    words = {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2}
    best, best_key = None, None
    base_depth = folder.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(folder):
        if dirpath.count(os.sep) - base_depth >= 6:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not _NOT_GAME_DIR.search(d)]
        for name in filenames:
            if not name.lower().endswith(".exe") or _NOT_GAME_EXE.search(os.path.splitext(name)[0]):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            stem = name.lower()
            # Unreal "-Shipping" builds are the real game; then title-like names; then the biggest file
            key = ("shipping" in stem, any(w in stem for w in words), size)
            if best_key is None or key > best_key:
                best, best_key = full, key
    return best


def _steam_games() -> list[dict]:
    root = None
    for hive, path, value in (("HKCU", r"Software\Valve\Steam", "SteamPath"),
                              ("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
        current = SystemTweaks.reg_read(hive, path, value)
        if current and os.path.isdir(str(current[0])):
            root = os.path.normpath(str(current[0]))
            break
    if not root:
        return []
    libraries = [root]
    try:
        with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as fh:
            for match in re.finditer(r'"path"\s+"([^"]+)"', fh.read()):
                libraries.append(os.path.normpath(match.group(1).replace("\\\\", "\\")))
    except OSError:
        pass
    games, seen = [], set()
    for library in libraries:
        steamapps = os.path.join(library, "steamapps")
        if os.path.normcase(steamapps) in seen or not os.path.isdir(steamapps):
            continue
        seen.add(os.path.normcase(steamapps))
        for name in os.listdir(steamapps):
            if not (name.startswith("appmanifest_") and name.endswith(".acf")):
                continue
            try:
                with open(os.path.join(steamapps, name), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            title = re.search(r'"name"\s+"([^"]+)"', text)
            folder = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not title or not folder or any(t in title.group(1).lower() for t in _NOT_GAME_TITLES):
                continue
            exe = _main_exe(os.path.join(steamapps, "common", folder.group(1)), title.group(1))
            if exe:
                games.append({"name": title.group(1), "path": exe, "source": "Steam"})
    return games


def _epic_games() -> list[dict]:
    folder = os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData",
                          r"Epic\EpicGamesLauncher\Data\Manifests")
    games = []
    try:
        names = os.listdir(folder)
    except OSError:
        return games
    for name in names:
        if not name.endswith(".item"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as fh:
                item = json.load(fh)
        except (OSError, ValueError):
            continue
        if "games" not in [c.lower() for c in item.get("AppCategories", [])]:
            continue
        title = item.get("DisplayName") or ""
        exe = os.path.join(item.get("InstallLocation", ""), item.get("LaunchExecutable", ""))
        if not item.get("LaunchExecutable") or _NOT_GAME_EXE.search(os.path.splitext(os.path.basename(exe))[0]):
            exe = _main_exe(item.get("InstallLocation", ""), title) or ""  # launcher stub: find the real game
        if exe and os.path.isfile(exe):
            games.append({"name": item.get("DisplayName") or os.path.basename(exe), "path": os.path.normpath(exe),
                          "source": "Epic Games"})
    return games


def _riot_games() -> list[dict]:
    metadata = os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", r"Riot Games\Metadata")
    known = {"valorant": ("VALORANT", r"ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe"),
             "league_of_legends": ("League of Legends", r"Game\League of Legends.exe")}
    games = []
    try:
        products = os.listdir(metadata)
    except OSError:
        return games
    for product in products:
        key = product.split(".")[0]
        if key not in known:
            continue
        settings = os.path.join(metadata, product, f"{product}.product_settings.yaml")
        try:
            with open(settings, encoding="utf-8", errors="replace") as fh:
                match = re.search(r'product_install_full_path:\s*"?([^"\r\n]+)"?', fh.read())
        except OSError:
            continue
        if match:
            exe = os.path.normpath(os.path.join(match.group(1).strip(), known[key][1]))
            if os.path.isfile(exe):
                games.append({"name": known[key][0], "path": exe, "source": "Riot"})
    return games


def _gog_games() -> list[dict]:
    games = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GOG.com\Games", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub) as key:
                        exe = str(winreg.QueryValueEx(key, "exe")[0])
                        title = str(winreg.QueryValueEx(key, "gameName")[0])
                except OSError:
                    continue
                if os.path.isfile(exe):
                    games.append({"name": title, "path": os.path.normpath(exe), "source": "GOG"})
    except OSError:
        pass
    return games


def find_installed_games() -> list[dict]:
    found, seen = [], set()
    for finder in (_steam_games, _epic_games, _riot_games, _gog_games):
        try:
            results = finder()
        except Exception:
            continue
        for game in results:
            key = os.path.normcase(game["path"])
            if key not in seen:
                seen.add(key)
                found.append(game)
    found.sort(key=lambda g: g["name"].lower())
    return found


# --------------------------------------------------------------------------------------
# DNS: benchmark public resolvers, switch the active adapter, restore the previous servers
# --------------------------------------------------------------------------------------

POWERSHELL = os.path.join(SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")
DNS_PROVIDERS = [
    ("cloudflare", "Cloudflare", ["1.1.1.1", "1.0.0.1"]),
    ("google", "Google", ["8.8.8.8", "8.8.4.4"]),
    ("quad9", "Quad9", ["9.9.9.9", "149.112.112.112"]),
    ("opendns", "OpenDNS", ["208.67.222.222", "208.67.220.220"]),
    ("adguard", "AdGuard", ["94.140.14.14", "94.140.15.15"]),
]
DNS_TEST_NAMES = ["google.com", "youtube.com", "discord.com", "steamcommunity.com", "riotgames.com",
                  "twitch.tv", "epicgames.com", "microsoft.com", "reddit.com", "cloudflare.com"]


def _valid_ipv4(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return value.count(".") == 3
    except OSError:
        return False


def dns_query_ms(server: str, name: str, timeout: float = 1.2) -> float | None:
    """Time one A-record lookup sent straight to `server` over UDP."""
    query_id = random.randint(0, 0xFFFF)
    packet = struct.pack(">HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    packet += b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".")) + b"\0"
    packet += struct.pack(">HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        started = time.perf_counter()
        sock.sendto(packet, (server, 53))
        while True:
            data, _addr = sock.recvfrom(2048)
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == query_id:
                return (time.perf_counter() - started) * 1000.0
    except OSError:
        return None
    finally:
        sock.close()


def default_route_ifindex() -> int | None:
    if iphlpapi is None:
        return None
    row = _MibIpForwardRow()
    try:
        if iphlpapi.GetBestRoute(_ipv4_dword("1.1.1.1"), 0, ctypes.byref(row)) != 0:
            return None
        return int(row.if_index) or None
    except (OSError, struct.error):
        return None


def dns_adapter_info() -> dict | None:
    """The adapter that carries internet traffic: name, DNS servers in use, and the servers set by
    hand (empty when they come automatically from the router)."""
    index = default_route_ifindex()
    if not index:
        return None
    script = (f"$a = Get-NetAdapter -InterfaceIndex {index} -ErrorAction Stop; "
              f"$d = Get-DnsClientServerAddress -InterfaceIndex {index} -AddressFamily IPv4; "
              "[pscustomobject]@{alias=$a.Name; guid=[string]$a.InterfaceGuid; servers=@($d.ServerAddresses)} "
              "| ConvertTo-Json -Compress")
    rc, out = run_command([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
    if rc != 0:
        return None
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    servers = data.get("servers") or []
    servers = [servers] if isinstance(servers, str) else list(servers)
    guid = str(data.get("guid") or "")
    static = []
    if guid:
        current = SystemTweaks.reg_read(
            "HKLM", rf"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\{guid}", "NameServer")
        if current and str(current[0]).strip():
            static = [s for s in re.split(r"[,\s]+", str(current[0])) if _valid_ipv4(s)]
    return {"if_index": index, "alias": str(data.get("alias") or "Network adapter"),
            "servers": [s for s in servers if _valid_ipv4(s)], "static": static}


def set_dns_servers(if_index: int, servers: list[str] | None) -> tuple[bool, str]:
    """servers=None hands DNS back to the router (DHCP)."""
    if servers:
        if not all(_valid_ipv4(s) for s in servers):
            return False, "invalid server address"
        quoted = ",".join(f"'{s}'" for s in servers)
        script = f"Set-DnsClientServerAddress -InterfaceIndex {int(if_index)} -ServerAddresses @({quoted}) -ErrorAction Stop"
    else:
        script = f"Set-DnsClientServerAddress -InterfaceIndex {int(if_index)} -ResetServerAddresses -ErrorAction Stop"
    rc, out = run_command([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
    if rc == 0:
        run_command([IPCONFIG, "/flushdns"], timeout=20)
    return rc == 0, first_line(out) if rc else "ok"


# --------------------------------------------------------------------------------------
# System Restore (a safety net beyond Undo)
# --------------------------------------------------------------------------------------

SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:").rstrip("\\").upper()
RESTORE_POINT_EVERY = 24 * 3600


def system_protection_on() -> bool | None:
    """True/False when System Protection is on/off for the Windows drive; None if it can't be read."""
    token = SYSTEM_DRIVE.replace(":", "%3A").lower()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SPP\Clients",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            index = 0
            while True:
                try:
                    _name, data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    return False
                index += 1
                text = " ".join(data) if isinstance(data, (list, tuple)) else str(data)
                if f"({token})" in text.lower():
                    return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def enable_system_protection() -> tuple[bool, str]:
    script = f"Enable-ComputerRestore -Drive '{SYSTEM_DRIVE}\\' -ErrorAction Stop"
    rc, out = run_command([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=120)
    return rc == 0, first_line(out) if rc else "ok"


def create_restore_point(log: "RunLog") -> str:
    """Returns ok | skipped (Windows already made one recently) | off | fail."""
    log.head("Safety net")
    log.info("Saving a System Restore point first - this can take up to a minute")
    script = ("$ErrorActionPreference='Stop'; try { Checkpoint-Computer -Description 'Astra Optimizer - before optimizing' "
              "-RestorePointType 'MODIFY_SETTINGS' -WarningVariable w -WarningAction SilentlyContinue; "
              "if ($w) { 'WARN:' + ($w | Select-Object -First 1) } else { 'OK' } } "
              "catch { 'ERR:' + $_.Exception.Message }")
    rc, out = run_command([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=240)
    last = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if last == "OK":
        log.ok("Restore point saved - Windows System Restore can roll the PC back to this moment")
        return "ok"
    if last.startswith("WARN:") and ("1440" in last or "already been created" in last.lower()):
        log.info("Windows already saved a restore point in the last day - that one covers you")
        return "skipped"
    if system_protection_on() is False:
        log.warn(f"System Protection is off for drive {SYSTEM_DRIVE}, so no restore point was saved. "
                 "You can turn it on from the Settings tab.")
        return "off"
    detail = last[5:] if last.startswith(("WARN:", "ERR:")) else first_line(out)
    log.warn(f"Restore point not saved ({detail.strip() or 'no reason given'}) - continuing; Undo still works")
    return "fail"


# --------------------------------------------------------------------------------------
# Startup apps (the same switches Task Manager's Startup tab uses)
# --------------------------------------------------------------------------------------

STARTUP_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def list_startup_apps() -> list[dict]:
    items: list[dict] = []
    sources = [
        ("HKCU", winreg.KEY_WOW64_64KEY, "Run", "This user"),
        ("HKLM", winreg.KEY_WOW64_64KEY, "Run", "All users"),
        ("HKLM", winreg.KEY_WOW64_32KEY, "Run32", "All users, 32-bit"),
    ]
    for hive, view, approved, source in sources:
        try:
            with winreg.OpenKey(HIVES[hive], RUN_KEY, 0, winreg.KEY_READ | view) as key:
                index = 0
                while True:
                    try:
                        name, data, kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and name:
                        items.append({"name": name, "detail": str(data), "source": f"{source} \u00b7 Registry",
                                      "hive": hive, "approved": approved, "value": name})
        except OSError:
            continue
    appdata = os.environ.get("APPDATA") or ""
    programdata = os.environ.get("ProgramData") or r"C:\ProgramData"
    folders = [
        (os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup"), "HKCU", "This user"),
        (os.path.join(programdata, r"Microsoft\Windows\Start Menu\Programs\StartUp"), "HKLM", "All users"),
    ]
    for folder, hive, source in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if name.lower() == "desktop.ini" or not os.path.isfile(os.path.join(folder, name)):
                continue
            items.append({"name": os.path.splitext(name)[0], "detail": os.path.join(folder, name),
                          "source": f"{source} \u00b7 Startup folder", "hive": hive, "approved": "StartupFolder",
                          "value": name})
    for item in items:
        item["enabled"] = startup_enabled(item)
    items.sort(key=lambda i: i["name"].lower())
    return items


def startup_enabled(item: dict) -> bool:
    current = SystemTweaks.reg_read(item["hive"], f"{STARTUP_APPROVED}\\{item['approved']}", item["value"])
    if current is None or not isinstance(current[0], (bytes, bytearray)) or not current[0]:
        return True
    return current[0][0] % 2 == 0  # 02/06 = on, 03/07 = off (the format Task Manager writes)


def set_startup_enabled(item: dict, enabled: bool) -> None:
    if enabled:
        data = bytes([2]) + bytes(11)
    else:
        filetime = int((time.time() + 11644473600) * 10_000_000)
        data = bytes([3, 0, 0, 0]) + struct.pack("<Q", filetime)
    with winreg.CreateKeyEx(HIVES[item["hive"]], f"{STARTUP_APPROVED}\\{item['approved']}", 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        winreg.SetValueEx(key, item["value"], 0, winreg.REG_BINARY, data)


# --------------------------------------------------------------------------------------
# Temporary file cleanup
# --------------------------------------------------------------------------------------

CLEAN_MIN_AGE = 24 * 3600  # only files untouched for a day
CLEAN_WEEK = 7 * 24 * 3600


def clean_targets() -> list[tuple[str, str, str, list[str]]]:
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    programdata = os.environ.get("ProgramData") or r"C:\ProgramData"
    wer = os.path.join(programdata, r"Microsoft\Windows\WER")
    appdata = os.environ.get("APPDATA") or ""
    discord = [os.path.join(appdata, app, sub) for app in ("discord", "discordptb", "discordcanary")
               for sub in (r"Cache\Cache_Data", "Code Cache", "GPUCache")] if appdata else []
    return [
        ("user", "Your temp folder", "Leftovers from installers, updaters and apps", [temp] if temp else []),
        ("windows", "Windows temp folder", "Leftovers from Windows and system services",
         [os.path.join(SYSTEM_ROOT, "Temp")]),
        ("crash", "Crash reports", "Saved Windows Error Reporting files",
         [os.path.join(wer, "ReportArchive"), os.path.join(wer, "ReportQueue")]),
        ("discord", "Discord cache", "Cached images and code. Discord fetches what it needs again", discord),
        ("update", "Windows Update downloads", "Update files Windows already used. It re-downloads anything it needs",
         [os.path.join(SYSTEM_ROOT, r"SoftwareDistribution\Download")]),
        ("delivery", "Delivery Optimization cache", "Update pieces Windows keeps to share with other PCs",
         [os.path.join(SYSTEM_ROOT, r"ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows"
                                    r"\DeliveryOptimization\Cache"),
          os.path.join(SYSTEM_ROOT, r"SoftwareDistribution\DeliveryOptimization")]),
    ]


def _old_files(paths: list[str], cutoff: float):
    """(path, size) of files last modified before cutoff. Never follows links or junctions,
    and never enters this app's own unpacked runtime folder."""
    skip = set()
    if getattr(sys, "_MEIPASS", None):
        skip.add(os.path.normcase(os.path.abspath(sys._MEIPASS)))
    for root in paths:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if not os.path.islink(os.path.join(dirpath, d))
                and not os.path.isjunction(os.path.join(dirpath, d))
                and os.path.normcase(os.path.abspath(os.path.join(dirpath, d))) not in skip
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                if info.st_mtime < cutoff:
                    yield full, info.st_size


def scan_cleanup(paths: list[str]) -> tuple[int, int]:
    cutoff = time.time() - CLEAN_MIN_AGE
    count = size = 0
    for _path, nbytes in _old_files(paths, cutoff):
        count += 1
        size += nbytes
    return count, size


def run_cleanup(paths: list[str]) -> tuple[int, int, int]:
    """Delete old files (permanently). Returns (bytes freed, files deleted, files skipped)."""
    cutoff = time.time() - CLEAN_MIN_AGE
    freed = deleted = skipped = 0
    for path, nbytes in list(_old_files(paths, cutoff)):
        try:
            try:
                os.remove(path)
            except PermissionError:
                os.chmod(path, stat.S_IWRITE)  # clear read-only, then try once more
                os.remove(path)
            freed += nbytes
            deleted += 1
        except OSError:
            skipped += 1  # in use or protected
    for root in paths:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, _files in os.walk(root, topdown=False, followlinks=False):
            if os.path.normcase(dirpath) == os.path.normcase(root):
                continue
            try:
                if os.stat(dirpath).st_ctime < cutoff:
                    os.rmdir(dirpath)  # only succeeds when empty
            except OSError:
                pass
    return freed, deleted, skipped


def human_size(nbytes: float) -> str:
    if nbytes >= 1024 ** 3:
        return f"{nbytes / 1024 ** 3:.1f} GB"
    if nbytes >= 1024 ** 2:
        return f"{nbytes / 1024 ** 2:.0f} MB"
    if nbytes >= 1024:
        return f"{nbytes / 1024:.0f} KB"
    return f"{int(nbytes)} B"


# --------------------------------------------------------------------------------------
# Gaming checkup: settings that quietly cost performance. Read-only, except the refresh-rate
# fix, which the user confirms first.
# --------------------------------------------------------------------------------------

class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128)]


_display_api = ctypes.WinDLL("user32")  # own function objects, so these argtypes stay private
_display_api.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW),
                                             wintypes.DWORD]
_display_api.EnumDisplayDevicesW.restype = wintypes.BOOL
_display_api.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
_display_api.EnumDisplaySettingsW.restype = wintypes.BOOL
_display_api.ChangeDisplaySettingsExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(DEVMODEW), wintypes.HWND,
                                                  wintypes.DWORD, ctypes.c_void_p]
_display_api.ChangeDisplaySettingsExW.restype = wintypes.LONG
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP, DISPLAY_DEVICE_PRIMARY_DEVICE = 0x1, 0x4
DM_INTERLACED = 0x2
DM_BITSPERPEL, DM_PELSWIDTH, DM_PELSHEIGHT, DM_DISPLAYFREQUENCY = 0x40000, 0x80000, 0x100000, 0x400000
CDS_UPDATEREGISTRY, CDS_TEST = 0x1, 0x2

CHECKUP_ITEMS = [("display", "Refresh rate"), ("memory", "Memory speed"), ("driver", "Graphics driver"),
                 ("gamegpu", "Graphics chip for games"), ("slot", "Graphics card slot"),
                 ("features", "Gaming features"), ("security", "Memory Integrity"), ("storage", "Storage"),
                 ("network", "Connection"), ("power", "Power")]
_FAKE_GPU = re.compile(r"microsoft basic|remote display|virtual|parsec|citrix|mirage|displaylink|idd", re.IGNORECASE)

# DirectX per-app GPU choice and the global "windowed games" / VRR switches (Settings > Display > Graphics)
GPU_PREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
DX_GLOBAL = "DirectXUserGlobalSettings"
REG_GAMEBAR = r"Software\Microsoft\GameBar"
REG_HVCI = r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity"
STORAGE_LOW_PCT, STORAGE_LOW_GB = 10, 15


def windows_build() -> int:
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def windowed_game_flags() -> dict[str, str]:
    """Variable refresh rate for DX11 games (Windows 10 1903+) and, on Windows 11, the flip-model
    upgrade for DX10/11 games in a window or borderless ("Optimizations for windowed games")."""
    flags = {"VRROptimizeEnable": "1"}
    if windows_build() >= 22000:
        flags["SwapEffectUpgradeEnable"] = "1"
    return flags


def parse_dx_globals(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (text or "").split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def dx_globals_with(text: str | None, flags: dict[str, str]) -> str | None:
    """The settings string with flags switched on, keeping every other entry (Auto HDR and so on);
    None when they're all on already."""
    current = parse_dx_globals(text)
    if all(current.get(k) == v for k, v in flags.items()):
        return None
    current.update(flags)
    return "".join(f"{k}={v};" for k, v in current.items())


def windowed_games_pending() -> bool:
    current = _reg_get("HKCU", GPU_PREF_KEY, DX_GLOBAL)
    return dx_globals_with(current[0] if current else "", windowed_game_flags()) is not None


def gpu_preference(path: str) -> int | None:
    """Windows' per-app graphics choice: 0 let Windows decide, 1 power saving, 2 high performance."""
    current = _reg_get("HKCU", GPU_PREF_KEY, path)
    match = re.search(r"GpuPreference=(\d+)", str(current[0])) if current else None
    return int(match.group(1)) if match else None


def set_gpu_preference(path: str, preference: int | None) -> None:
    if preference is None:
        _reg_delete("HKCU", GPU_PREF_KEY, path)
    else:
        _reg_put("HKCU", GPU_PREF_KEY, path, f"GpuPreference={preference};", winreg.REG_SZ)


def real_gpus(gpus: list[dict]) -> list[dict]:
    return [g for g in gpus if g.get("name") and not _FAKE_GPU.search(g["name"])]


def fast_gpu(gpus: list[dict]) -> dict | None:
    """The discrete card: NVIDIA first, then an AMD Radeon RX, else the last real adapter."""
    real = real_gpus(gpus)
    for test in (lambda g: _gpu_vendor(g) == "10DE", lambda g: _gpu_vendor(g) == "1002" and " RX" in g["name"]):
        found = next((g for g in real if test(g)), None)
        if found:
            return found
    return real[-1] if real else None


def gpu_label(gpu: dict | None) -> str:
    name = short_gpu_name((gpu or {}).get("name", "")) or "graphics card"
    return re.sub(r"\s+(Laptop GPU|Laptop|GPU)$", "", name, flags=re.IGNORECASE)


def gpu_setup(gpus: list[dict], laptop: bool) -> dict:
    """{"hybrid": a laptop with two graphics chips, "fast": the strong one's short name}."""
    return {"hybrid": laptop and len(real_gpus(gpus)) >= 2, "fast": gpu_label(fast_gpu(gpus))}

DRIVER_PAGES = {"10DE": ("NVIDIA", "https://www.nvidia.com/en-us/drivers/"),
                "1002": ("AMD", "https://www.amd.com/en/support/download/drivers.html"),
                "8086": ("Intel", "https://www.intel.com/content/www/us/en/support/detect.html")}
DRIVER_OLD_DAYS = 120
MEMORY_TYPES = {24: "DDR3", 26: "DDR4", 34: "DDR5", 29: "LPDDR3", 30: "LPDDR4", 35: "LPDDR5"}
JEDEC_TOP = {"DDR4": 2666, "DDR5": 4800}  # at or below this, XMP/EXPO is most likely off


def _new_devmode() -> DEVMODEW:
    mode = DEVMODEW()
    mode.dmSize = ctypes.sizeof(DEVMODEW)
    return mode


def display_modes() -> list[dict]:
    """Every display on the desktop: its refresh rate now and the highest one it offers at the
    same resolution and colour depth. The main display comes first."""
    displays, index = [], 0
    while True:
        dev = DISPLAY_DEVICEW()
        dev.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not _display_api.EnumDisplayDevicesW(None, index, ctypes.byref(dev), 0):
            break
        index += 1
        if not dev.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        current = _new_devmode()
        if not _display_api.EnumDisplaySettingsW(dev.DeviceName, ENUM_CURRENT_SETTINGS, ctypes.byref(current)):
            continue
        best, mode, n = current.dmDisplayFrequency, _new_devmode(), 0
        while n < 5000 and _display_api.EnumDisplaySettingsW(dev.DeviceName, n, ctypes.byref(mode)):
            n += 1
            if (mode.dmPelsWidth, mode.dmPelsHeight, mode.dmBitsPerPel) == \
                    (current.dmPelsWidth, current.dmPelsHeight, current.dmBitsPerPel) \
                    and not mode.dmDisplayFlags & DM_INTERLACED:
                best = max(best, mode.dmDisplayFrequency)
        monitor = DISPLAY_DEVICEW()
        monitor.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        name = monitor.DeviceString if _display_api.EnumDisplayDevicesW(dev.DeviceName, 0, ctypes.byref(monitor), 0) \
            else ""
        displays.append({"device": dev.DeviceName, "monitor": name, "width": current.dmPelsWidth,
                         "height": current.dmPelsHeight, "hz": current.dmDisplayFrequency, "max_hz": best,
                         "primary": bool(dev.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE)})
    displays.sort(key=lambda d: not d["primary"])
    return displays


def set_display_refresh(device: str, hz: int) -> tuple[bool, str]:
    """Switch one display to hz at its current resolution (tested first, then saved)."""
    mode = _new_devmode()
    if not _display_api.EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
        return False, "couldn't read the current display mode"
    mode.dmDisplayFrequency = hz
    mode.dmFields = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    rc = _display_api.ChangeDisplaySettingsExW(device, ctypes.byref(mode), None, CDS_TEST, None)
    if rc != 0:
        return False, f"Windows won't use {hz} Hz on this display (code {rc})"
    rc = _display_api.ChangeDisplaySettingsExW(device, ctypes.byref(mode), None, CDS_UPDATEREGISTRY, None)
    if rc == 1:
        return True, "takes effect after a restart"
    return (True, "") if rc == 0 else (False, f"Windows refused the change (code {rc})")


def _display_label(displays: list[dict], display: dict) -> str:
    return "Your display" if len(displays) == 1 else f"Display {displays.index(display) + 1}"


def check_display(displays: list[dict] | None = None) -> dict:
    if displays is None:
        try:
            displays = display_modes()
        except Exception:
            displays = []
    if not displays:
        return {"status": "info", "detail": "Couldn't read your display settings.", "value": ""}
    slow = [d for d in displays if d["hz"] > 1 and d["max_hz"] - d["hz"] > 2]  # 59 vs 60 Hz is the same mode
    if slow:
        d = slow[0]
        detail = (f"{_display_label(displays, d)} runs at {d['hz']} Hz but supports {d['max_hz']} Hz "
                  f"at {d['width']}×{d['height']}.")
        if len(slow) > 1:
            detail += f" {len(slow) - 1} more display{'s' if len(slow) > 2 else ''} too."
        return {"status": "warn", "detail": detail, "value": f"{d['hz']} Hz",
                "action": ("display", f"Use {d['max_hz']} Hz"),
                "fix": [(s["device"], s["max_hz"], _display_label(displays, s), s["hz"]) for s in slow]}
    d = displays[0]
    if len(displays) == 1:
        detail = f"Already at its highest refresh rate: {d['hz']} Hz at {d['width']}×{d['height']}."
    else:
        detail = f"All {len(displays)} displays are at their highest refresh rate."
    return {"status": "ok", "detail": detail, "value": f"{d['hz']} Hz"}


def rated_memory_speed(part: str) -> int:
    """Rated MT/s read from a module's part number (G.Skill F5-6000…, Corsair …6000C36,
    Kingston KF560C…, Crucial …G32C16…), or 0 when it doesn't say."""
    part = (part or "").upper().strip()
    speeds = [int(v) for v in re.findall(r"(?<!\d)(\d{4})(?!\d)", part) if 2133 <= int(v) <= 8800]
    match = re.match(r"KF(\d)(\d{2})C", part)
    if match:
        speeds.append(int(match.group(2)) * 100)
    match = re.search(r"G(\d{2})C\d{2}", part)
    if match and 21 <= int(match.group(1)) <= 88:
        speeds.append(int(match.group(1)) * 100)
    return max(speeds, default=0)


def check_memory(modules: list[dict], laptop: bool) -> dict:
    modules = [m for m in modules if (m.get("configured") or m.get("speed"))]
    if not modules:
        return {"status": "info", "detail": "Couldn't read your memory details.", "value": ""}
    kind = MEMORY_TYPES.get(int(modules[0].get("type") or 0), "")
    configured = min(int(m.get("configured") or m.get("speed")) for m in modules)
    if configured < {"DDR4": 2000, "DDR5": 3200}.get(kind, 0):  # some firmware reports MHz, not MT/s
        configured *= 2
    rated = max((rated_memory_speed(m.get("part", "")) for m in modules), default=0)
    label = f"{kind} " if kind else ""
    value = f"{configured} MT/s"
    if kind.startswith("LP"):
        return {"status": "ok", "value": value,
                "detail": f"{kind} at {configured} MT/s. It's built into the laptop, so its speed is fixed."}
    if rated and rated - configured >= 200:
        return {"status": "warn", "value": value,
                "detail": f"Rated for {rated} MT/s but running at {configured}. Turn on XMP (Intel) or EXPO (AMD) "
                          "in your BIOS."}
    if not laptop and kind in JEDEC_TOP and configured <= JEDEC_TOP[kind]:
        return {"status": "warn", "value": value,
                "detail": f"Running at the standard {kind} speed. If your RAM is rated faster, turn on XMP or EXPO "
                          "in your BIOS."}
    return {"status": "ok", "value": value, "detail": f"{label}running at {configured} MT/s."}


def _gpu_vendor(gpu: dict) -> str:
    match = re.search(r"VEN_([0-9A-F]{4})", str(gpu.get("pnp") or "").upper())
    return match.group(1) if match else ""


def _nvidia_version(version: str) -> str:
    digits = re.sub(r"\D", "", version or "")
    return f"{digits[-5:-2]}.{digits[-2:]}" if len(digits) >= 5 else version


def check_driver(gpus: list[dict], today: _dt.date | None = None) -> dict:
    today = today or _dt.date.today()
    real = real_gpus(gpus)
    if not real:
        basic = next((g for g in gpus if "basic" in str(g.get("name", "")).lower()), None)
        if basic:
            result = {"status": "warn", "value": "",
                      "detail": "Windows is using its basic display driver, so games run badly or not at all. "
                                "Install your graphics driver."}
            vendor = DRIVER_PAGES.get(_gpu_vendor(basic))
            if vendor:
                result["action"] = ("link", "Get driver", vendor[1])
            return result
        return {"status": "info", "detail": "No graphics card found.", "value": ""}
    gpu = sorted(real, key=lambda g: _gpu_vendor(g) not in ("10DE", "1002"))[0]  # a discrete card first
    vendor_id = _gpu_vendor(gpu)
    vendor = DRIVER_PAGES.get(vendor_id)
    name = short_gpu_name(gpu["name"])
    version = _nvidia_version(gpu.get("version", "")) if vendor_id == "10DE" else ""
    try:
        date = _dt.date.fromisoformat(str(gpu.get("date") or "")[:10])
    except ValueError:
        return {"status": "info", "value": version, "detail": f"{name}: couldn't read the driver date."}
    age = (today - date).days
    when = date.strftime("%B %Y")
    if age > DRIVER_OLD_DAYS:
        months = max(1, round(age / 30.4))
        result = {"status": "warn", "value": version or date.strftime("%b %Y"),
                  "detail": f"The {name} driver is from {when}, {months} months old. Newer ones often run "
                            "recent games better."}
        if vendor:
            result["action"] = ("link", "Get driver", vendor[1])
        return result
    return {"status": "ok", "value": version or date.strftime("%b %Y"),
            "detail": f"{name} driver from {when}, recent enough."}


def check_game_gpu(gpus: list[dict], laptop: bool, games: list[dict], preference=None) -> dict:
    preference = preference or gpu_preference
    setup = gpu_setup(gpus, laptop)
    fast = setup["fast"]
    if not real_gpus(gpus):
        return {"status": "info", "detail": "No graphics card found.", "value": ""}
    if not setup["hybrid"]:
        return {"status": "ok", "value": fast,
                "detail": f"Games run on the {fast}. This only needs fixing on laptops with two graphics chips."}
    if not games:
        return {"status": "info", "value": "",
                "detail": f"Add your games on the Games page and Astra will make them use the {fast}."}
    loose = [g for g in games if preference(g["path"]) != 2]
    if loose:
        return {"status": "warn", "value": "", "fast": fast, "action": ("gamegpu", "Use fast GPU"),
                "games": [g["path"] for g in loose],
                "detail": f"{plural(len(loose), 'game')} of yours {'isn' if len(loose) == 1 else 'aren'}'t set to use "
                          f"the {fast}. Laptops sometimes run games on the built-in graphics."}
    return {"status": "ok", "value": fast, "detail": f"All {plural(len(games), 'game')} use the {fast}."}


def check_gpu_slot(gpus: list[dict], laptop: bool) -> dict:
    gpu = fast_gpu(gpus)
    if gpu is None:
        return {"status": "info", "detail": "No graphics card found.", "value": ""}
    width, top = int(gpu.get("width") or 0), int(gpu.get("max_width") or 0)
    name = gpu_label(gpu)
    if laptop and not top:
        return {"status": "ok", "value": "", "detail": f"The {name} is built into the laptop, so there's no slot to check."}
    if not width or not top:
        return {"status": "info", "value": "", "detail": f"Couldn't read the slot speed of the {name}."}
    if width < top:
        return {"status": "warn", "value": f"x{width}",
                "detail": f"The {name} runs at PCIe x{width} but supports x{top}. Check it sits in the top full-length "
                          "slot, without a riser cable."}
    return {"status": "ok", "value": f"x{width}", "detail": f"The {name} runs at its full PCIe x{top} link."}


def gaming_features() -> dict[str, bool | None]:
    game_mode = _reg_get("HKCU", REG_GAMEBAR, "AutoGameModeEnabled")
    hags = _reg_get("HKLM", REG_GRAPHICS, "HwSchMode")
    dx = parse_dx_globals((_reg_get("HKCU", GPU_PREF_KEY, DX_GLOBAL) or [""])[0])
    return {"game_mode": game_mode is None or game_mode[0] != 0,  # missing means on
            "windowed": dx.get("SwapEffectUpgradeEnable") == "1" if windows_build() >= 22000 else None,
            "vrr": dx.get("VRROptimizeEnable") == "1",
            "hags": hags is not None and hags[0] == 2}


def check_features(state: dict | None = None) -> dict:
    state = state or gaming_features()
    names = {"game_mode": "Game Mode", "windowed": "optimizations for windowed games",
             "vrr": "variable refresh rate for older games", "hags": "GPU scheduling"}
    off = [names[k] for k in ("game_mode", "windowed", "vrr", "hags") if state.get(k) is False]
    if not off:
        return {"status": "ok", "value": "On",
                "detail": "Game Mode, windowed-game optimizations, variable refresh rate and GPU scheduling are on."}
    text = ", ".join(off)
    return {"status": "warn", "value": "", "action": ("features", "Turn on"), "off": off,
            "detail": f"Off: {text[:1].upper()}{text[1:]}. Turning {'it' if len(off) == 1 else 'them'} on helps "
                      "games run smoother, especially in borderless windows."}


def fix_features(admin: bool) -> list[str]:
    """Switch the gaming features on; returns what couldn't be changed."""
    failed = []
    try:
        _reg_put("HKCU", REG_GAMEBAR, "AutoGameModeEnabled", 1)
        current = _reg_get("HKCU", GPU_PREF_KEY, DX_GLOBAL)
        target = dx_globals_with(current[0] if current else "", windowed_game_flags())
        if target:
            _reg_put("HKCU", GPU_PREF_KEY, DX_GLOBAL, target, winreg.REG_SZ)
    except OSError as exc:
        failed.append(f"game settings ({exc.strerror or exc})")
    if admin:
        try:
            _reg_put("HKLM", REG_GRAPHICS, "HwSchMode", 2)
        except OSError as exc:
            failed.append(f"GPU scheduling ({exc.strerror or exc})")
    else:
        failed.append("GPU scheduling (needs administrator rights)")
    return failed


def check_security(running: list | None = None) -> dict:
    """Memory Integrity (HVCI). Reported, never changed: switching it off is a security trade-off."""
    if running is not None:
        on = 2 in [int(v) for v in running if str(v).isdigit()]
    else:
        value = _reg_get("HKLM", REG_HVCI, "Enabled")
        on = value is not None and value[0] == 1
    if on:
        return {"status": "info", "value": "On", "action": ("link", "Open setting", "windowsdefender://coreisolation"),
                "detail": "It blocks malicious drivers, but can cost up to 5-10% FPS in some games. Turning it off "
                          "is your call."}
    return {"status": "ok", "value": "Off",
            "detail": "Off, so it costs no performance. It also isn't guarding against bad drivers."}


def trim_enabled() -> bool | None:
    rc, out = run_command(["fsutil", "behavior", "query", "DisableDeleteNotify"], timeout=10)
    match = re.search(r"NTFS\s+DisableDeleteNotify\s*=\s*(\d)", out) if rc == 0 else None
    return None if match is None else match.group(1) == "0"


def drive_space(paths: list[str]) -> list[dict]:
    drives = []
    for path in [SYSTEM_DRIVE + "\\", *paths]:
        drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
        if drive and drive not in [d["drive"] for d in drives]:
            try:
                usage = __import__("shutil").disk_usage(drive + "\\")
            except OSError:
                continue
            drives.append({"drive": drive, "free": usage.free, "total": usage.total})
    return drives


def check_storage(drives: list[dict], trim: bool | None) -> dict:
    low = [d for d in drives if d["total"] and (d["free"] * 100 / d["total"] < STORAGE_LOW_PCT
                                                 or d["free"] < STORAGE_LOW_GB * 1024 ** 3)]
    if trim is False:
        return {"status": "warn", "value": "", "action": ("trim", "Turn on TRIM"),
                "detail": "TRIM is off, so SSDs slowly lose speed. Windows has it on by default."}
    if low:
        d = low[0]
        pct = d["free"] * 100 / d["total"]
        return {"status": "warn", "value": human_size(d["free"]), "action": ("cleanup", "Clean up"),
                "detail": f"Drive {d['drive']} has only {human_size(d['free'])} free ({pct:.0f}%). Games load and "
                          "update slower on a nearly full drive."}
    free = ", ".join(f"{d['drive']} {human_size(d['free'])}" for d in drives[:3])
    trim_text = " TRIM is on." if trim else ""
    return {"status": "ok", "value": "", "detail": f"Free space: {free}.{trim_text}"}


def _link_speed(bps: float) -> str:
    if bps >= 1e9:
        return f"{bps / 1e9:g} Gbps"
    return f"{bps / 1e6:.0f} Mbps"


def check_network(net: dict | None, wifi_signal: int | None = None) -> dict:
    if not net:
        return {"status": "info", "detail": "No internet connection found.", "value": ""}
    desc = str(net.get("desc") or net.get("name") or "")
    media = f"{net.get('media') or ''} {net.get('ndis') or ''}"
    bps = float(net.get("bps") or 0)
    wifi = "802.11" in media or re.search(r"wi-?fi|wireless|wlan|802\.11", desc, re.IGNORECASE)
    tunnel = re.search(r"vpn|tap-|tun\b|wireguard|tailscale|zerotier|openvpn|nordlynx|wintun|cloudflare warp",
                       desc, re.IGNORECASE)
    if tunnel:
        return {"status": "info", "value": "VPN",
                "detail": f"Your traffic goes through {desc}, which can add ping in online games."}
    if wifi:
        signal = f" with {wifi_signal}% signal" if wifi_signal is not None else ""
        return {"status": "warn", "value": "Wi-Fi",
                "detail": f"You're on Wi-Fi{signal}. A network cable gives steadier ping in online games."}
    if 0 < bps < 1e9 - 1:
        return {"status": "warn", "value": _link_speed(bps),
                "detail": f"Your cable link runs at {_link_speed(bps)}. Most PCs connect at 1 Gbps or more, so try "
                          "another cable or router port."}
    speed = f" at {_link_speed(bps)}" if bps else ""
    return {"status": "ok", "value": "Ethernet", "detail": f"Wired connection{speed}."}


def check_power(battery=None, probe: bool = True) -> dict:
    if probe:
        try:
            battery = psutil.sensors_battery()
        except Exception:
            battery = None
    if battery is None:
        return {"status": "ok", "value": "Plugged in", "detail": "A desktop PC on wall power."}
    percent = round(battery.percent)
    if not battery.power_plugged:
        return {"status": "warn", "value": f"{percent}%",
                "detail": f"Running on battery ({percent}%). Laptops slow the CPU and GPU down on battery, so plug "
                          "in before you play."}
    return {"status": "ok", "value": "Plugged in", "detail": f"Plugged in, battery at {percent}%."}


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def checkup_system_info() -> dict:
    """Memory modules, graphics adapters and the internet adapter in one PowerShell call."""
    index = default_route_ifindex() or 0
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "$m = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [pscustomobject]@{ "
        "speed = [int]$_.Speed; configured = [int]$_.ConfiguredClockSpeed; type = [int]$_.SMBIOSMemoryType; "
        "part = [string]$_.PartNumber } }); "
        "$g = @(Get-CimInstance Win32_VideoController | ForEach-Object { $w = 0; $mw = 0; "
        "if ($_.PNPDeviceID -like 'PCI*') { $p = Get-PnpDeviceProperty -InstanceId $_.PNPDeviceID -KeyName "
        "'DEVPKEY_PciDevice_CurrentLinkWidth','DEVPKEY_PciDevice_MaxLinkWidth'; "
        "$w = [int]($p | Where-Object KeyName -eq 'DEVPKEY_PciDevice_CurrentLinkWidth').Data; "
        "$mw = [int]($p | Where-Object KeyName -eq 'DEVPKEY_PciDevice_MaxLinkWidth').Data }; "
        "[pscustomobject]@{ "
        "name = [string]$_.Name; version = [string]$_.DriverVersion; pnp = [string]$_.PNPDeviceID; "
        "width = $w; max_width = $mw; "
        "date = $(if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' }) } }); "
        "$dg = Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard -ClassName Win32_DeviceGuard; "
        "$hv = if ($dg) { @($dg.SecurityServicesRunning) } else { $null }; "
        "$n = $null; "
        f"if ({index}) {{ $a = Get-NetAdapter -InterfaceIndex {index}; if ($a) {{ $n = [pscustomobject]@{{ "
        "name = [string]$a.Name; desc = [string]$a.InterfaceDescription; media = [string]$a.PhysicalMediaType; "
        "ndis = [string]$a.NdisPhysicalMedium; bps = [double]$a.ReceiveLinkSpeed } } }; "
        "[pscustomobject]@{ memory = $m; gpus = $g; net = $n; hvci = $hv } | ConvertTo-Json -Compress -Depth 4")
    rc, out = run_command([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=45)
    try:
        data = json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}
    except (ValueError, IndexError):
        data = {}
    return {"memory": _as_list(data.get("memory")), "gpus": _as_list(data.get("gpus")), "net": data.get("net"),
            "hvci": None if data.get("hvci") is None else _as_list(data.get("hvci"))}


def wifi_signal() -> int | None:
    rc, out = run_command(["netsh", "wlan", "show", "interfaces"], timeout=10)
    match = re.search(r"^\s*Signal\s*:\s*(\d+)\s*%", out, re.MULTILINE) if rc == 0 else None
    return int(match.group(1)) if match else None


def run_checkup(games: list[dict] | None = None, info: dict | None = None) -> dict[str, dict]:
    games = games or []
    laptop = has_battery()
    results = {"display": check_display(), "power": check_power(), "features": check_features()}
    info = info or checkup_system_info()
    results["memory"] = check_memory(info["memory"], laptop=laptop)
    results["driver"] = check_driver(info["gpus"])
    results["gamegpu"] = check_game_gpu(info["gpus"], laptop, games)
    results["slot"] = check_gpu_slot(info["gpus"], laptop)
    results["security"] = check_security(info["hvci"])
    results["storage"] = check_storage(drive_space([g["path"] for g in games]), trim_enabled())
    results["_setup"] = gpu_setup(info["gpus"], laptop)
    net = info["net"]
    is_wifi = bool(net) and ("802.11" in f"{net.get('media')} {net.get('ndis')}"
                             or re.search(r"wi-?fi|wireless|wlan", str(net.get("desc")), re.IGNORECASE))
    results["network"] = check_network(net, wifi_signal() if is_wifi else None)
    return results


# --------------------------------------------------------------------------------------
# Privacy: ads and tracking built into Windows. Each switch saves the exact values it
# replaces and writes them back when turned off (a value that didn't exist is deleted again).
# --------------------------------------------------------------------------------------

_CDM = r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"
PRIVACY_ITEMS = [
    ("ads", "Block ads in Start and Settings",
     "Stops suggested apps, promotions and apps Windows installs by itself.",
     [("HKCU", _CDM, "SystemPaneSuggestionsEnabled", 0), ("HKCU", _CDM, "SubscribedContent-338388Enabled", 0),
      ("HKCU", _CDM, "SubscribedContent-338393Enabled", 0), ("HKCU", _CDM, "SubscribedContent-353694Enabled", 0),
      ("HKCU", _CDM, "SubscribedContent-353696Enabled", 0), ("HKCU", _CDM, "SilentInstalledAppsEnabled", 0),
      ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced", "Start_IrisRecommendations", 0)]),
    ("search", "Keep Start search on this PC",
     "No Bing web results when you search from Start. Shows after you sign in again.",
     [("HKCU", r"Software\Policies\Microsoft\Windows\Explorer", "DisableSearchBoxSuggestions", 1),
      ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Search", "BingSearchEnabled", 0)]),
    ("adid", "Turn off the advertising ID", "Apps can't use it to show you personalised ads.",
     [("HKCU", r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled", 0)]),
    ("tips", "No tips and suggestions",
     "Hides tip notifications, 'finish setting up' prompts and lock screen fun facts.",
     [("HKCU", _CDM, "SubscribedContent-338389Enabled", 0), ("HKCU", _CDM, "SoftLandingEnabled", 0),
      ("HKCU", _CDM, "SubscribedContent-310093Enabled", 0), ("HKCU", _CDM, "RotatingLockScreenOverlayEnabled", 0),
      ("HKCU", _CDM, "SubscribedContent-338387Enabled", 0),
      ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement", "ScoobeSystemSettingEnabled", 0)]),
    ("telemetry", "Send only required diagnostic data",
     "The minimum Windows allows. Its Settings app will say some options are managed.",
     [("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 1),
      ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Privacy",
       "TailoredExperiencesWithDiagnosticDataEnabled", 0)]),
    ("activity", "Turn off activity history", "Windows stops keeping a list of the apps and files you use.",
     [("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "PublishUserActivities", 0),
      ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "UploadUserActivities", 0)]),
]
PRIVACY_KEYS = [key for key, *_rest in PRIVACY_ITEMS]


def _reg_get(hive: str, path: str, name: str):
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, name)  # (value, type)
    except OSError:
        return None


def _reg_put(hive: str, path: str, name: str, value, kind: int = winreg.REG_DWORD) -> None:
    with winreg.CreateKeyEx(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        winreg.SetValueEx(key, name, 0, kind, value)


def _reg_delete(hive: str, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def privacy_values(key: str) -> list[tuple]:
    return next(values for k, _t, _d, values in PRIVACY_ITEMS if k == key)


def privacy_needs_admin(key: str) -> bool:
    return any(hive == "HKLM" for hive, *_rest in privacy_values(key))


def privacy_is_on(key: str) -> bool:
    """Live: every value it sets currently holds the private setting."""
    for hive, path, name, target in privacy_values(key):
        current = _reg_get(hive, path, name)
        if current is None or current[0] != target:
            return False
    return True


def privacy_apply(key: str, saved: list | None) -> list:
    """Write the private values. Returns the originals to keep: the ones already saved, or the
    values found now (None for a value that didn't exist)."""
    values = privacy_values(key)
    originals = saved
    if not originals:
        originals = []
        for hive, path, name, _target in values:
            current = _reg_get(hive, path, name)
            originals.append([hive, path, name, None if current is None else current[0],
                              None if current is None else current[1]])
    for hive, path, name, target in values:
        _reg_put(hive, path, name, target)
    return originals


def privacy_restore(key: str, saved: list | None) -> None:
    """Put back exactly what was there. Without saved originals, removes the values this app sets,
    which returns them to Windows' own default."""
    entries = saved or [[hive, path, name, None, None] for hive, path, name, _t in privacy_values(key)]
    for hive, path, name, value, kind in entries:
        if value is None:
            _reg_delete(hive, path, name)
        else:
            _reg_put(hive, path, name, value, kind if kind is not None else winreg.REG_DWORD)


# --------------------------------------------------------------------------------------
# Settings backup: one JSON file with settings, games, appearance (custom picture included)
# and session history. Things tied to this PC or to Windows' own state stay out of it.
# --------------------------------------------------------------------------------------

BACKUP_FORMAT = 1
BACKUP_SKIP = {"autostart", "dns_original", "restore_point_at", "tray_hint", "clean_at"}


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def make_backup(settings: dict, background_path: str | None) -> dict:
    import base64
    data = {"astra_backup": BACKUP_FORMAT, "app_version": APP_VERSION,
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "settings": {k: v for k, v in settings.items() if k not in BACKUP_SKIP}}
    look = settings.get("appearance") or {}
    if look.get("background") == "image" and background_path and os.path.isfile(background_path):
        with open(background_path, "rb") as handle:
            data["background_jpg"] = base64.b64encode(handle.read()).decode("ascii")
    return data


def read_backup(path: str) -> dict:
    """Parsed and checked backup; raises ValueError with a plain reason when it isn't one."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"couldn't read it ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict) or data.get("astra_backup") is None or not isinstance(data.get("settings"), dict):
        raise ValueError("it isn't an Astra Optimizer backup")
    if int(data.get("astra_backup") or 0) > BACKUP_FORMAT:
        raise ValueError("it was made by a newer version of Astra Optimizer")
    return data


def merge_backup(current: dict, backup: dict) -> dict:
    """Current settings with the backup's known keys laid over them (unknown or PC-specific keys
    are ignored, and values of the wrong type are dropped)."""
    merged = dict(current)
    for key, value in backup.get("settings", {}).items():
        if key in BACKUP_SKIP or (key not in DEFAULT_SETTINGS and key != "appearance"):
            continue
        default = DEFAULT_SETTINGS.get(key)
        if key == "appearance" and not isinstance(value, dict):
            continue
        if isinstance(default, bool) and not isinstance(value, bool):
            continue
        if isinstance(default, list) and not isinstance(value, list):
            continue
        if isinstance(default, dict) and not isinstance(value, dict):
            continue
        merged[key] = value
    games = []
    for game in merged.get("games") or []:
        if isinstance(game, dict) and isinstance(game.get("path"), str):
            games.append({k: v for k, v in game.items() if not str(k).startswith("_")})
    merged["games"] = games
    merged["sessions"] = [r for r in merged.get("sessions") or []
                          if isinstance(r, dict) and {"exe", "start", "end", "cpu_avg"} <= set(r)][-SESSION_KEEP:]
    return merged


# --------------------------------------------------------------------------------------
# Game session reports
# --------------------------------------------------------------------------------------

SESSION_MIN_SECONDS = 60
BENCH_SECONDS = 60     # a benchmark measures exactly this long...
BENCH_COUNTDOWN = 3    # ...after this countdown on the overlay
BENCH_RESULT_SECONDS = 10  # how long the result stays on the overlay
SESSION_PING_HOST = "1.1.1.1"
SESSION_KEEP = 100


class SessionPinger(threading.Thread):
    """Pings Cloudflare every 2 s while a game runs; results go to the UI queue."""

    def __init__(self, out_queue: queue.Queue, host: str = SESSION_PING_HOST, interval: float = 2.0):
        super().__init__(name="session-ping", daemon=True)
        self.out_queue, self.host, self.interval = out_queue, host, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.out_queue.put(("session-ping", icmp_ping(self.host)))
            except Exception:
                continue


# ---- FPS: Intel PresentMon (MIT, bundled in assets/presentmon) reads frame timing from Windows'
# own event tracing. Nothing is injected into the game, so anti-cheat isn't involved.

PRESENTMON_EXE = ("assets", "presentmon", "PresentMon.exe")


def presentmon_path() -> str | None:
    path = resource_path(*PRESENTMON_EXE)
    return path if os.path.isfile(path) else None


BOUND_KINDS = ("gpu", "cpu", "cap")
BOUND_BUSY = 0.80   # busy for this share of a frame's time: that part set the pace
BOUND_IDLE = 0.60   # neither busy for this much: a frame cap or V-Sync set the pace
STEADY_SPREAD = 0.04  # frame times within ~4% of each other over the last 60 frames: something is pacing them
STEADY_FRAMES = 60


class Steadiness:
    """Rolling spread of the last STEADY_FRAMES frame times. A frame limiter holds frames to a fixed time;
    a game that's CPU- or GPU-bound wanders with the scene."""

    __slots__ = ("window", "total", "squares")

    def __init__(self):
        self.window: deque = deque()
        self.total = self.squares = 0.0

    def add(self, ms: float) -> bool:
        self.window.append(ms)
        self.total += ms
        self.squares += ms * ms
        if len(self.window) > STEADY_FRAMES:
            old = self.window.popleft()
            self.total -= old
            self.squares -= old * old
        n = len(self.window)
        if n < STEADY_FRAMES // 2:
            return False
        mean = self.total / n
        spread = max(0.0, self.squares / n - mean * mean) ** 0.5
        return spread <= STEADY_SPREAD * mean


def frame_bound(ms: float, gpu_busy: float | None, cpu_busy: float | None, steady: bool = False) -> str | None:
    """What held one frame back, from PresentMon's GPU and CPU busy times: 'gpu', 'cpu' or 'cap'.
    In-game limiters wait inside what PresentMon counts as CPU busy time, so a very steady frame rate
    that the GPU isn't holding back counts as capped. CPU and GPU work overlap: a strong hint, not proof."""
    if gpu_busy is None or cpu_busy is None or ms <= 0:
        return None
    gpu, cpu = gpu_busy / ms, cpu_busy / ms
    if gpu >= BOUND_BUSY:
        return "gpu"
    if steady:
        return "cap"
    if cpu >= BOUND_BUSY:
        return "cpu"
    if max(gpu, cpu) < BOUND_IDLE:
        return "cap"
    return "gpu" if gpu >= cpu else "cpu"


def bound_shares(totals: dict) -> dict | None:
    """{'gpu': %, 'cpu': %, 'cap': %} of the classified time, or None with too little of it."""
    total = sum(totals.get(k, 0.0) for k in BOUND_KINDS)
    if total <= 0:
        return None
    return {k: 100.0 * totals.get(k, 0.0) / total for k in BOUND_KINDS}


class FrameStats:
    """Streaming frame-time histogram (0.05 ms buckets up to 250 ms), so a long session costs
    a fixed ~40 KB however many frames it has."""

    RES = 0.05
    MAX_MS = 250.0

    def __init__(self):
        self.buckets = [0] * (int(self.MAX_MS / self.RES) + 1)
        self.count = 0
        self.total_ms = 0.0
        self.bound = {k: 0.0 for k in BOUND_KINDS}  # frame time spent GPU-bound, CPU-bound and capped

    def add(self, ms: float, bound: str | None = None) -> None:
        if not 0.05 <= ms <= 5000:  # ignore junk and the gap of a paused or alt-tabbed game
            return
        self.buckets[min(len(self.buckets) - 1, int(ms / self.RES))] += 1
        self.count += 1
        self.total_ms += ms
        if bound is not None:
            self.bound[bound] += ms

    def percentile(self, pct: float) -> float:
        """Frame time (ms) that pct% of frames are at or below."""
        target, seen = self.count * pct / 100.0, 0
        for index, n in enumerate(self.buckets):
            seen += n
            if seen >= target and n:
                return (index + 0.5) * self.RES
        return self.MAX_MS

    def summary(self) -> dict | None:
        if self.count < 120 or self.total_ms <= 0:  # not enough frames to say anything
            return None
        median = self.percentile(50)
        threshold = max(2 * median, median + 8)  # a frame that took twice as long, and noticeably so
        first = min(len(self.buckets) - 1, int(threshold / self.RES) + 1)
        result = {"fps_avg": self.count * 1000.0 / self.total_ms, "fps_low1": 1000.0 / self.percentile(99),
                  "fps_spikes": sum(self.buckets[first:]), "fps_spike_ms": threshold, "frames": self.count}
        if sum(self.bound.values()) >= self.total_ms * 0.5:  # most frames had GPU and CPU busy times
            shares = bound_shares(self.bound)
            result.update({f"bound_{k}": v for k, v in shares.items()})
        return result


class FrameCapture(threading.Thread):
    """Runs PresentMon for one game (by exe name) and keeps one FrameStats per swap chain. The game is the
    swap chain drawing the most frames right now (launchers and overlays draw far fewer); a game makes a new
    one when its video settings change, so this follows the new one straight away and the session's own
    FrameStats keeps counting across both."""

    def __init__(self, exe_name: str, command: list[str] | None = None):
        super().__init__(name=f"fps-{exe_name}", daemon=True)
        safe = re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(exe_name)[0])[:24] or "game"
        self.session_name = f"AstraOptimizer-{safe}"
        self.command = command or [presentmon_path() or "PresentMon.exe", "--process_name", exe_name,
                                   "--output_stdout", "--no_console_stats", "--session_name", self.session_name,
                                   "--stop_existing_session", "--terminate_on_proc_exit", "--no_track_input"]
        self.stats: dict[tuple, FrameStats] = {}
        self.recent: dict[tuple, deque] = {}       # last few thousand frame times, for the live overlay
        self.recent_bound: dict[tuple, deque] = {}  # (frame time, what held it back) for the last few seconds
        self.last_frame: dict[tuple, float] = {}
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        self._stopping = False
        self.bench: FrameStats | None = None  # a benchmark's frames, while one runs
        self.main_key: tuple | None = None    # the swap chain that is the game right now
        self.main = FrameStats()              # the game's frames for the whole session
        self._steady: dict[tuple, Steadiness] = {}
        self._pick_at = 0.0

    def run(self) -> None:
        import csv
        try:
            self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                         creationflags=CREATE_NO_WINDOW)
        except OSError as exc:
            self.error = str(exc)
            return
        columns: dict[str, int] = {}
        try:
            for row in csv.reader(self.proc.stdout):
                if not columns:
                    lowered = [c.strip().lower() for c in row]
                    if "msbetweenpresents" in lowered or "frametime" in lowered:
                        columns = {name: i for i, name in enumerate(lowered)}
                    continue
                ms_col = columns.get("msbetweenpresents", columns.get("frametime"))
                try:
                    ms = float(row[ms_col])
                except (ValueError, IndexError, TypeError):
                    continue
                key = (row[columns["processid"]] if "processid" in columns and len(row) > columns["processid"] else "",
                       row[columns["swapchainaddress"]] if "swapchainaddress" in columns
                       and len(row) > columns["swapchainaddress"] else "")
                stats = self.stats.get(key)
                if stats is None:
                    stats = self.stats[key] = FrameStats()
                    self.recent[key] = deque(maxlen=2400)
                    self.recent_bound[key] = deque(maxlen=1200)
                    self._steady[key] = Steadiness()
                steady = self._steady[key].add(ms)
                bound = frame_bound(ms, _csv_float(row, columns.get("msgpubusy", columns.get("gpubusy"))),
                                    _csv_float(row, columns.get("mscpubusy", columns.get("cpubusy"))), steady)
                stats.add(ms, bound)
                self.recent[key].append(ms)
                if bound is not None:
                    self.recent_bound[key].append((ms, bound))
                now = self.last_frame[key] = time.monotonic()
                if key != self.main_key and now >= self._pick_at:
                    self._pick_at = now + 0.5
                    self.main_key = self._busiest_now(now)
                if key == self.main_key:
                    self.main.add(ms, bound)
                    bench = self.bench
                    if bench is not None:
                        bench.add(ms, bound)
        except (OSError, ValueError):
            pass
        if not self.stats and not self._stopping:
            try:
                err = self.proc.stderr.read() if self.proc.stderr else ""
            except (OSError, ValueError):
                err = ""
            lines = [l.strip() for l in err.splitlines() if l.strip().lower().startswith("error")]
            self.error = lines[0] if lines else None

    def stop(self, wait: float = 3.0) -> None:
        self._stopping = True
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.join(wait)
        if presentmon_path():  # make sure the trace session is gone, not just the process
            run_command([presentmon_path(), "--session_name", self.session_name, "--terminate_existing_session"],
                        timeout=10)

    def _busiest_now(self, now: float) -> tuple | None:
        """The swap chain that drew the most frames in the last second (among those drawing at all)."""
        best, best_n = self.main_key, -1
        for key, last in list(self.last_frame.items()):
            if now - last > 1.0:
                continue
            total = n = 0
            for ms in reversed(self.recent[key]):
                total += ms
                n += 1
                if total >= 1000:
                    break
            if n > best_n:
                best, best_n = key, n
        return best

    def _game_key(self) -> tuple | None:
        """The game's swap chain, or None when it hasn't drawn for 2 s (minimised, loading)."""
        key = self.main_key
        if key is None or time.monotonic() - self.last_frame.get(key, 0) > 2.0:
            return None
        return key

    def summary(self) -> dict | None:
        return self.main.summary() if self.main.count else None

    def start_bench(self) -> bool:
        """Start counting the game's frames on their own; False while it isn't drawing."""
        if self.live() is None:
            return False
        self.bench = FrameStats()
        return True

    def end_bench(self) -> dict | None:
        bench, self.bench = self.bench, None
        return bench.summary() if bench is not None else None

    def recent_frames(self, count: int) -> list[float]:
        """The game's last frame times (ms), oldest first; empty when it's gone quiet."""
        key = self._game_key()
        return list(self.recent.get(key) or [])[-count:] if key is not None else []

    def live_bound(self) -> str | None:
        """What mostly held the game back over the last second: 'gpu', 'cpu' or 'cap' (None when unknown)."""
        key = self._game_key()
        if key is None:
            return None
        totals = {k: 0.0 for k in BOUND_KINDS}
        span = 0.0
        for ms, bound in reversed(list(self.recent_bound.get(key) or [])):
            totals[bound] += ms
            span += ms
            if span >= 1000:
                break
        if span < 250:  # too few classified frames to say
            return None
        return max(totals, key=totals.get)

    def live(self) -> tuple[float, float] | None:
        """(FPS over the last second, 1% low over the last ~2400 frames), or None when the game
        hasn't drawn anything for 2 s (minimised, loading)."""
        key = self._game_key()
        if key is None:
            return None
        frames = list(self.recent.get(key) or [])
        if len(frames) < 10:
            return None
        total = count = 0
        for ms in reversed(frames):
            total += ms
            count += 1
            if total >= 1000:
                break
        ordered = sorted(frames)
        slow = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        return count * 1000.0 / total, 1000.0 / slow if slow > 0 else 0.0


def _csv_float(row: list, index: int | None) -> float | None:
    if index is None or index >= len(row):
        return None
    try:
        return float(row[index])
    except ValueError:  # "NA"
        return None


def new_session(exe: str, name: str, now: float | None = None) -> dict:
    return {"exe": exe, "name": name, "start": now or time.time(), "n": 0, "cpu_sum": 0.0, "cpu_peak": 0.0,
            "gpu_sum": 0.0, "gpu_n": 0, "gpu_peak": 0.0, "temp_peak": None, "ram_peak": 0.0, "pings": []}


def add_session_sample(session: dict, data: dict) -> None:
    cpu, ram = float(data.get("cpu") or 0), float(data.get("ram") or 0)
    session["n"] += 1
    session["cpu_sum"] += cpu
    session["cpu_peak"] = max(session["cpu_peak"], cpu)
    session["ram_peak"] = max(session["ram_peak"], ram)
    gpu = data.get("gpu") or {}
    if gpu.get("load") is not None:
        session["gpu_sum"] += float(gpu["load"])
        session["gpu_n"] += 1
        session["gpu_peak"] = max(session["gpu_peak"], float(gpu["load"]))
    if gpu.get("temp") is not None:
        session["temp_peak"] = max(session["temp_peak"] or 0, float(gpu["temp"]))


def finish_session(session: dict, now: float | None = None, min_seconds: float = SESSION_MIN_SECONDS) -> dict | None:
    """The saved report, or None for a session too short (or too empty) to be worth one."""
    end = now or time.time()
    if end - session["start"] < min_seconds or not session["n"]:
        return None
    report = {"exe": session["exe"], "name": session["name"], "start": session["start"], "end": end,
              **(session.get("fps") or {}),
              "cpu_avg": session["cpu_sum"] / session["n"], "cpu_peak": session["cpu_peak"],
              "ram_peak": session["ram_peak"], "temp_peak": session["temp_peak"],
              "gpu_avg": session["gpu_sum"] / session["gpu_n"] if session["gpu_n"] else None,
              "gpu_peak": session["gpu_peak"] if session["gpu_n"] else None}
    pings = session["pings"]
    answered = sorted(p for p in pings if p is not None)
    if len(pings) >= 5 and answered:
        median = answered[len(answered) // 2]
        threshold = median + 50
        report.update(ping_avg=sum(answered) / len(answered), ping_threshold=threshold,
                      ping_spikes=sum(1 for p in answered if p > threshold),
                      ping_loss=100.0 * (len(pings) - len(answered)) / len(pings))
    return report


def format_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def format_clock(stamp: float) -> str:
    t = time.localtime(stamp)
    return f"{(t.tm_hour % 12) or 12}:{t.tm_min:02d} {'AM' if t.tm_hour < 12 else 'PM'}"


def format_day(stamp: float, now: float | None = None) -> str:
    day = _dt.date.fromtimestamp(stamp)
    today = _dt.date.fromtimestamp(now or time.time())
    if day == today:
        return "today"
    if day == today - _dt.timedelta(days=1):
        return "yesterday"
    return f"{day.strftime('%b')} {day.day}"


def session_report_text(report: dict) -> str:
    if report.get("benchmark"):
        lines = [f"{BENCH_SECONDS}-second benchmark at {format_clock(report['start'])}, "
                 f"with {report.get('label') or 'no mode'} applied.", ""]
    else:
        lines = [f"Played for {format_duration(report['end'] - report['start'])}, "
                 f"{format_clock(report['start'])} to {format_clock(report['end'])}.", ""]
    if report.get("fps_avg") is not None:
        spikes = report.get("fps_spikes", 0)
        lines.append(f"FPS: {report['fps_avg']:.0f} average, {report['fps_low1']:.0f} 1% low, "
                     + (f"{spikes} stutter spike{'s' if spikes != 1 else ''} over {report['fps_spike_ms']:.0f} ms"
                        if spikes else "no stutter spikes"))
    held = bound_line(report)
    if held:
        lines.append(held)
    lines.append(f"CPU load: {report['cpu_avg']:.0f}% average, {report['cpu_peak']:.0f}% peak")
    if report.get("gpu_avg") is not None:
        lines.append(f"GPU load: {report['gpu_avg']:.0f}% average, {report['gpu_peak']:.0f}% peak")
    if report.get("temp_peak") is not None:
        lines.append(f"GPU temperature: {report['temp_peak']:.0f} °C at its hottest")
    lines.append(f"Memory in use: {report['ram_peak']:.0f}% at its peak")
    if report.get("ping_avg") is not None:
        spikes = report["ping_spikes"]
        spike_text = (f"{spikes} spike{'s' if spikes != 1 else ''} over {report['ping_threshold']:.0f} ms"
                      if spikes else "no spikes")
        loss = report["ping_loss"]
        loss_text = "none lost" if loss < 0.5 else f"{loss:.0f}% lost"
        lines.append(f"Ping to Cloudflare: {report['ping_avg']:.0f} ms average, {spike_text}, {loss_text}")
    notes = []
    tip = bound_tip(report)
    if tip:
        notes.append(tip)
    if (report.get("temp_peak") or 0) >= 85:
        notes.append("Your GPU ran hot. Dusty fans or poor case airflow are the usual cause.")
    if (report.get("ping_loss") or 0) >= 2:
        notes.append("Some pings were lost. On Wi-Fi, a network cable usually fixes this.")
    if notes:
        lines += [""] + notes
    return "\n".join(lines)


BOUND_NAMES = {"gpu": "the GPU", "cpu": "the CPU", "cap": "an FPS cap or V-Sync"}
BOUND_SHORT = {"gpu": "GPU-bound", "cpu": "CPU-bound", "cap": "Capped"}


def bound_line(report: dict) -> str | None:
    """'Held back by: the GPU 78% of the time, the CPU 15%, an FPS cap or V-Sync 7%'."""
    shares = [(k, report.get(f"bound_{k}")) for k in BOUND_KINDS]
    shares = sorted(((k, v) for k, v in shares if v is not None and v >= 1), key=lambda kv: -kv[1])
    if not shares:
        return None
    first, rest = shares[0], shares[1:]
    text = f"Held back by: {BOUND_NAMES[first[0]]} {first[1]:.0f}% of the time"
    return text + "".join(f", {BOUND_NAMES[k]} {v:.0f}%" for k, v in rest)


def bound_tip(report: dict) -> str | None:
    """One sentence on what would raise FPS, when one part clearly set the pace."""
    shares = {k: report.get(f"bound_{k}") for k in BOUND_KINDS}
    if any(v is None for v in shares.values()):
        return None
    top = max(shares, key=shares.get)
    if shares[top] < 60:
        return None
    return {"gpu": "Mostly GPU-bound: lower graphics settings or resolution scale for more FPS. Max FPS and other "
                   "CPU tweaks change little here.",
            "cpu": "Mostly CPU-bound: Max FPS, closing background apps and lowering CPU-heavy settings (view "
                   "distance, crowds, physics) help most. Lower graphics settings won't add much.",
            "cap": "Mostly capped: an FPS cap or V-Sync set the pace, so your PC had power to spare."}[top]


def benchmark_summary(report: dict) -> str:
    if report.get("fps_avg") is None:
        return "no FPS measured"
    return f"{report['fps_avg']:.0f} FPS average, {report['fps_low1']:.0f} 1% low"


def session_summary(report: dict) -> str:
    parts = [format_duration(report["end"] - report["start"])]
    if report.get("fps_avg") is not None:
        parts.append(f"{report['fps_avg']:.0f} FPS ({report['fps_low1']:.0f} 1% low)")
    if report.get("gpu_avg") is not None:
        parts.append(f"GPU {report['gpu_avg']:.0f}% average")
    if report.get("temp_peak") is not None:
        parts.append(f"{report['temp_peak']:.0f} °C peak")
    if report.get("ping_avg") is not None:
        parts.append(f"{report['ping_avg']:.0f} ms ping")
    return " · ".join(parts)


# --------------------------------------------------------------------------------------
# UI - Galaxy theme
#
# Every visual is generated at runtime with Pillow (antialiased shapes, real blur glows,
# gradients, a seeded starfield and vector-style icons) and composited into a single
# scene image on a Tk canvas. Text stays as native canvas text for crisp rendering.
# No image, font or stylesheet files are loaded - the UI is fully offline.
# --------------------------------------------------------------------------------------

SUPERSAMPLE = 3
WINDOW_W, WINDOW_H = 720, 480

# Logical layout (multiplied by the Windows display scale at runtime)
L_TITLE_H = 36                                  # custom title strip (drag to move)
L_BRAND_GLYPH = (14, 9, 20)
L_WINDOW_BUTTONS = {"min": (640, 6, 32, 24), "close": (674, 6, 32, 24)}
L_RAIL_W = 64                                   # side tab rail
CX = 78                                         # content column: x 78..706
TAB_ORDER = ["boost", "tools", "privacy", "settings", "about"]  # Appearance is a page of Settings
TAB_SPECS = {"boost": ("Boost", "comet"), "tools": ("Tools", "signal"), "privacy": ("Privacy", "shield"),
             "settings": ("Settings", "sliders"), "about": ("About", "info")}
L_ABOUT_ROWS = {"app": (78, 52, 628, 136), "credits": (78, 198, 628, 128), "info": (78, 336, 628, 130)}
L_ABOUT_ICON = (100, 74, 92)          # x, y, size
L_ABOUT_AVATAR = (100, 220, 84)
L_ABOUT_REPORT = (546, 216, 140, 40)
L_ABOUT_PROFILE = (546, 268, 140, 40)  # "GitHub page"
L_ABOUT_LINK = (204, 286, 250, 20)    # the link text itself is clickable too
L_ABOUT_UPDATE = (546, 74, 140, 40)   # "Check for updates" / "Download v…"
L_RAIL_BOX = (6, 44, 60, 422)                  # floating glass box holding the tabs
L_TABS = {key: (9, 52 + i * 69, 54, 58) for i, key in enumerate(TAB_ORDER[:-1])}
L_TABS[TAB_ORDER[-1]] = (9, 52 + 5 * 69, 54, 58)  # About stays at the foot of the rail
# Boost tab
L_PILLS = {"cpu": (78, 44, 104, 52), "gpu": (190, 44, 224, 52), "ram": (422, 44, 136, 52), "plan": (566, 44, 140, 52)}
GPU_EXTRA_W = 118  # right-hand column of the GPU pill (VRAM, clock, power)
L_CARDS = {"max": (78, 108, 202, 130), "cozy": (291, 108, 202, 130), "quality": (504, 108, 202, 130)}
L_BUTTONS = {"optimize": (78, 250, 500, 48), "defaults": (586, 250, 120, 48)}
L_CONSOLE = (78, 310, 628, 156)
# Tools tab
TOOL_PAGES = [("checkup", "Checkup"), ("ping", "Ping"), ("games", "Games"), ("startup", "Startup"),
              ("cleanup", "Cleanup"), ("dns", "DNS"), ("monitor", "Monitor")]
L_TOOL_ACTION = (566, 48, 140, 38)
L_TOOL_BUTTONS = {"ping": L_TOOL_ACTION, "host": (586, 424, 120, 42)}
L_GAME_FIND = (510, 48, 94, 38)
L_GAME_ADD = (610, 48, 96, 38)
TOOL_PAGE_BUTTONS = {"checkup": {"checkup": L_TOOL_ACTION}, "ping": L_TOOL_BUTTONS,
                     "games": {"findgames": L_GAME_FIND, "addgame": L_GAME_ADD},
                     "startup": {"refresh": L_TOOL_ACTION}, "cleanup": {"clean": L_TOOL_ACTION},
                     "dns": {"dnstest": L_TOOL_ACTION}, "monitor": {}}
PRIMARY_TOOL_BUTTONS = {"ping", "clean", "dnstest", "checkup"}
CHECK_VISIBLE = 5
L_CHECK_SLOTS = [(78, 128 + i * 68, 628, 62) for i in range(CHECK_VISIBLE)]  # the list scrolls through these
L_CHECK_SCORE = (78, 104, 16, 8)  # x, y, pip width, pip height: one pip per check above the rows
L_CHECK_SCROLL = (710, 128, 3, 334)  # thin scroll track right of the rows
L_CHECK_BTN = (112, 30)  # row action button, right-aligned in its row
L_DNS_TABLE = (78, 100, 628, 280)
L_DNS_ROW_Y0, L_DNS_ROW_H, DNS_ROWS = 134, 40, 6
L_DNS_COLS = {"name": 94, "median": 384, "answered": 470, "use": 620}
L_DNS_BAR = (78, 392, 628, 74)
L_DNS_RESTORE = (546, 409, 144, 40)
L_MONITOR = {key: (78, 100 + i * 92, 628, 84) for i, key in enumerate(("cpu", "gpu", "temp", "ram"))}
MONITOR_SPECS = {"cpu": ("CPU load", "%"), "gpu": ("GPU load", "%"), "temp": ("GPU temperature", "\u00b0C"),
                 "ram": ("Memory in use", "%")}
HISTORY_LEN = 240  # samples at 1.5 s = the last 6 minutes
L_TABLE = (78, 100, 628, 312)
L_GAME_ROW_Y0, L_GAME_ROW_H, GAME_ROWS = 134, 46, 6
L_GAME_COLS = {"name": 94, "prio": 366, "fso": 466, "win": 566, "del": 664}
L_GAME_NAME_W = 258  # game name and its subtitle, left of the switches
GAME_HEADS = {"prio": "HIGH PRIORITY", "fso": "FULLSCREEN OPT. OFF", "win": "START KEY OFF"}
L_GAME_AUTO = (78, 424, 628, 42)
L_GAME_SPLIT = 210   # the bottom row holds three switches: auto mode | overlay | background apps
L_GAME_SPLIT2 = 414
GAME_AUTO_TEXT = "Auto-apply my mode"
GAME_BACKGROUND_TEXT = "Slow background apps"
L_HIST_COLS = {"fps": 382, "cpu": 456, "gpu": 522, "temp": 588, "ping": 654}  # centre of each stat column
L_CMP_COLS = {"a": 438, "b": 540, "chg": 646}  # Compare view: older, newer, change
COMPARE_METRICS = [  # key, label, hint, unit, which way is better
    ("fps_avg", "Average FPS", "Higher is better", "fps", "up"),
    ("fps_low1", "1% low FPS", "Higher means fewer dips", "fps", "up"),
    ("spike_rate", "Stutter spikes", "Per hour, lower is better", "/h", "down"),
    ("gpu_avg", "GPU load", "How hard the graphics card worked", "%", None),
    ("temp_peak", "Hottest GPU", "Lower is better", "°C", "down"),
    ("ping_avg", "Ping", "To Cloudflare, lower is better", "ms", "down"),
]
L_START_ROW_Y0, L_START_ROW_H, START_ROWS = 134, 39, 7
L_START_COLS = {"name": 94, "source": 446, "toggle": 646}
L_CLEAN_ROWS = {key: (78, 100 + i * 48, 628, 44)
                for i, key in enumerate(("user", "windows", "crash", "discord", "update", "delivery"))}
L_CLEAN_BAR = (78, 394, 628, 72)
L_PING_TABLE = (78, 100, 628, 312)
L_PING_ROW_Y0, L_PING_ROW_H = 134, 34
L_PING_HOST_FIELD = (78, 424, 500, 42)
L_PING_COLS = {"name": 94, "latency": 322, "jitter": 410, "loss": 478, "bars": 536}
# Settings tab
L_SETTING_ROWS = {key: (78, 96 + i * 53, 628, 48)
                  for i, key in enumerate(("tray", "autostart", "hotkey", "preview", "restore", "updates"))}
L_SETTING_TEXT_Y = (6, 26)  # title and description offsets inside a row
L_WINSET_ROWS = {key: (78, 96 + i * 53, 628, 48)  # Windows page
                 for i, key in enumerate(("mouse", "pointer", "sticky", "startkey", "deliveryopt", "faststartup"))}
WINSET_ADMIN = {"deliveryopt", "faststartup"}
L_TOGGLE = (44, 24)
L_FOLDER_ROW = (78, 416, 628, 46)
L_SETTING_BUTTONS = {"backup": (386, 420, 92, 38), "restore": (486, 420, 92, 38), "folder": (586, 420, 104, 38)}
SETTING_BUTTON_TEXT = {"backup": "Back up…", "restore": "Restore…", "folder": "Open folder"}
L_PROTECT_BUTTON = (78 + 628 - 20 - 44 - 16 - 104, L_SETTING_ROWS["restore"][1] + 9, 104, 30)  # "Turn on…"
L_HOTKEYS_BUTTON = (78 + 628 - 20 - 44 - 16 - 104, L_SETTING_ROWS["hotkey"][1] + 9, 104, 30)  # "Hotkeys…"
L_DIALOG_W = 404
# Privacy tab (same row geometry as Settings)
L_PRIV_ROWS = {key: (78, 96 + i * 53, 628, 48) for i, key in enumerate(PRIVACY_KEYS)}
L_PRIV_FOOT = (78, 416, 628, 46)
L_PRIV_ALL = (78 + 628 - 16 - 120, 416 + 4, 120, 38)
# Appearance tab
L_LOOK_RESET = (586, 50, 120, 36)
L_LOOK_ROWS = {"background": (78, 100, 628, 88), "adjust": (78, 198, 628, 136), "motion": (78, 344, 628, 122)}
L_SEGMENTS = {"bg": (386, 112, 300, 32), "motion": (326, 358, 360, 32), "tool": (78, 50, 424, 34),
              "set": (78, 50, 330, 34)}
SETTING_PAGES = [("app", "App"), ("windows", "Windows"), ("appearance", "Appearance")]
L_LOOK_IMAGE_BTN = (98, 146, 140, 30)
L_LOOK_HINT_Y = 161
L_SLIDERS = {"blur": (228, 222, 370), "dim": (228, 251, 370), "glass": (228, 280, 370),
             "stand_out": (228, 309, 370)}  # x, centre y, width
L_LOOK_INTRO = (78, 428, 628, 38)  # the "Startup intro" line at the foot of the Motion row
L_LOOK_INTRO_Y = 446               # its centre line

BG_KINDS = [("stars", "Starfield"), ("black", "Black"), ("image", "Image")]
MOTION_MODES = [("system", "Windows"), ("full", "Full"), ("reduced", "Reduced"), ("off", "Off")]
SLIDER_SPECS = {"blur": ("Blur", 0, 24), "dim": ("Dim", 0, 85), "glass": ("Panel opacity", 40, 100),
                "stand_out": ("Frost", 0, 100)}
GLOBAL_SLIDERS = ("glass", "stand_out")  # the rest are kept per background
DEFAULT_LOOK = {
    "background": "stars",
    "bg_name": "",
    "adjust": {"stars": {"blur": 0, "dim": 0}, "image": {"blur": 8, "dim": 40}},
    "glass": 60,
    "stand_out": 50,
    "motion": "system",
    "intro": True,
}
# Startup intro (logical units). The mark lights up at the centre, its orbit is traced, the name
# appears, then both fly into the title strip - landing exactly on the real glyph and name.
INTRO_SECONDS = {"full": 2.2, "reduced": 0.95}  # timeline length, in intro seconds
INTRO_PACE = {"full": 1.45, "reduced": 1.3}      # real seconds per intro second (about 3.2 s and 1.2 s)
INTRO_HANDOFF = {"full": 1.67, "reduced": 0.55}  # when the real UI starts to fade in
INTRO_MARK = (360, 196, 132)   # centre x, centre y, size
INTRO_WORD = (296, 30)         # wordmark centre y, font size
INTRO_ORBIT_TILT = math.radians(24)
INTRO_MOON_ANGLE = math.atan2(-8 / 15, 39 / 44)  # where the orbit's moon rests in the mark
BG_HINTS = {
    "stars": "Drawn by Astra Optimizer, the same sky on every launch.",
    "black": "Plain black behind every panel.",
}
MOTION_TEXT = {
    "full": "Cards glow and grow, buttons shimmer while working, and tabs and dialogs crossfade.",
    "reduced": "Short fades only. Nothing slides, grows or shimmers.",
    "off": "Every change appears instantly.",
}

SETTING_TEXT = {
    "tray": ("Keep running in the tray", "Closing hides it by the clock. Right-click the tray icon for modes and Exit."),
    "autostart": ("Start with Windows", "Opens in the tray at sign-in, already elevated, with no UAC prompt."),
    "hotkey": (f"Global hotkey  \u00b7  {HOTKEY_LABEL}", "Applies the selected mode, or undoes it. Works in-game."),
    "preview": ("Preview and summary", "Lists what a mode will change, then what it did. Hotkey and tray skip it."),
    "restore": ("Restore point first", "Saves a Windows restore point before your first optimize each day."),
    "updates": ("Check for updates", "Asks GitHub for a newer version at startup. Nothing downloads on its own."),
    # Windows page
    "mouse": ("Turn off mouse acceleration",
              "Turns off Windows' Enhance pointer precision, so aim feels the same at any mouse speed."),
    "pointer": ("Mouse pointer speed 1:1",
                "The middle notch (6 of 11), where one count from your mouse moves the pointer one pixel."),
    "sticky": ("Turn off the Sticky Keys pop-ups",
               "Shift five times, or holding right Shift or Num Lock, won't open a dialog mid-game."),
    "deliveryopt": ("Stop sharing updates with other PCs",
                    "Windows stops uploading update files to other PCs, which can raise ping. Updates still come."),
    "faststartup": ("Turn off Fast Startup",
                    "Shut down fully restarts Windows and its drivers. Booting takes a few seconds longer."),
    "startkey": ("Block the Start menu key in games", "Off. The Windows key opens Start as usual, even in games."),
}

CARD_SPECS = [
    # key, title, purpose, description, accent, icon
    ("max", "Max FPS", "Competitive",
     "Ultimate power plan, lean visuals, Game DVR off, background services paused and RAM purged.",
     STARLIGHT, "comet"),
    ("cozy", "Cozy Mode", "Balanced",
     "High Performance plan and a gentle RAM cleanup that leaves Discord, Spotify and browsers alone.",
     STARLIGHT, "planet"),
    ("quality", "Best Looking", "Visual quality",
     "Full visual effects with ClearType and transparency, Search and SysMain restarted.",
     STARLIGHT, "sparkle"),
]
CARD_BY_KEY = {spec[0]: spec for spec in CARD_SPECS}
PROFILE_ORDER = [spec[0] for spec in CARD_SPECS]
PROFILE_TITLES = {spec[0]: spec[1] for spec in CARD_SPECS}

# level -> (console prefix, prefix colour, message colour)
CONSOLE_STYLES = {
    "ok": ("[OK]", SILVER, "#CFCFCF"),
    "info": ("[INFO]", ASH, "#8A8A8A"),
    "warn": ("[WARN]", WARN_YELLOW, "#FFE680"), # drawn as an outlined yellow chip
    "fail": ("[FAIL]", VOID, "#FFA3A4"),        # drawn as a filled red chip
    "head": ("[STEP]", STARLIGHT, TEXT_HI),
}


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb[:3])


def mix(a: str, b: str, t: float) -> str:
    ca, cb = hex_rgb(a), hex_rgb(b)
    return rgb_hex(tuple(x + (y - x) * t for x, y in zip(ca, cb)))


def rgba(color: str, alpha: float = 255) -> tuple[int, int, int, int]:
    r, g, b = hex_rgb(color)
    return r, g, b, max(0, min(255, int(round(alpha))))


def paste_layer(base: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """alpha_composite that tolerates layers hanging off any edge of the base."""
    left, top = max(0, -x), max(0, -y)
    right = min(layer.width, base.width - x)
    bottom = min(layer.height, base.height - y)
    if right <= left or bottom <= top:
        return
    if (left, top, right, bottom) != (0, 0, layer.width, layer.height):
        layer = layer.crop((left, top, right, bottom))
    base.alpha_composite(layer, (x + left, y + top))


class GalaxyPainter:
    """Pillow renderer for every visual element of the galaxy theme."""

    def __init__(self, scale: float):
        self.scale = scale
        self.glass = 1.0  # panel opacity (Appearance > Panel opacity)
        self._memo_cache: dict = {}

    def _memo(self, key, factory) -> Image.Image:
        """Remember rendered pieces; callers get a copy they may draw on."""
        image = self._memo_cache.get(key)
        if image is None:
            if len(self._memo_cache) > 400:
                self._memo_cache.clear()
            image = self._memo_cache[key] = factory()
        return image.copy()

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    # ---------------- primitives ----------------

    @staticmethod
    def _mask(w: int, h: int, radius: float, inset: float = 0.0) -> Image.Image:
        big_w, big_h = w * SUPERSAMPLE, h * SUPERSAMPLE
        mask = Image.new("L", (big_w, big_h), 0)
        i = inset * SUPERSAMPLE
        if big_w - 1 - 2 * i > 0 and big_h - 1 - 2 * i > 0:
            ImageDraw.Draw(mask).rounded_rectangle(
                (i, i, big_w - 1 - i, big_h - 1 - i), radius=max(0.0, radius - inset) * SUPERSAMPLE, fill=255
            )
        return mask

    @staticmethod
    def gradient(size, c1, c2, horizontal: bool = True) -> Image.Image:
        ramp = Image.linear_gradient("L")
        if horizontal:
            ramp = ramp.transpose(Image.Transpose.ROTATE_90)
        ramp = ramp.resize(size, Image.Resampling.BICUBIC)
        return Image.composite(Image.new("RGBA", size, c2), Image.new("RGBA", size, c1), ramp)

    def shape(self, w: int, h: int, radius: float, fill=None, grad=None, border=None,
              border_width: float = 1.0) -> Image.Image:
        """Antialiased rounded rectangle. fill/border are RGBA tuples, grad = (c1, c2, horizontal)."""
        return self._memo(("shape", int(w), int(h), radius, fill, grad, border, border_width),
                          lambda: self._shape(w, h, radius, fill, grad, border, border_width))

    def _shape(self, w, h, radius, fill, grad, border, border_width) -> Image.Image:
        w, h = max(1, int(w)), max(1, int(h))
        big = (w * SUPERSAMPLE, h * SUPERSAMPLE)
        outer = self._mask(w, h, radius)
        out = Image.new("RGBA", big, (0, 0, 0, 0))
        layer = None
        if grad is not None:
            layer = self.gradient(big, grad[0], grad[1], grad[2])
        elif fill is not None:
            layer = Image.new("RGBA", big, fill)
        if layer is not None:
            layer.putalpha(ImageChops.multiply(layer.getchannel("A"), outer))
            out = layer
        if border is not None:
            bw = max(1.0, border_width * self.scale)
            ring = ImageChops.subtract(outer, self._mask(w, h, radius, inset=bw))
            alpha = border[3]
            ring = ring.point(lambda v, a=alpha: v * a // 255)
            border_layer = Image.new("RGBA", big, border[:3] + (0,))
            border_layer.putalpha(ring)
            out = Image.alpha_composite(out, border_layer)
        return out.resize((w, h), Image.Resampling.LANCZOS)

    def glow_layer(self, size, box, radius: float, color: str, alpha: float, blur: float,
                   outline: int = 0) -> Image.Image:
        return self._memo(("glow", tuple(size), tuple(box), radius, color, int(alpha), blur, outline),
                          lambda: self._glow_layer(size, box, radius, color, alpha, blur, outline))

    @staticmethod
    def _glow_layer(size, box, radius, color, alpha, blur, outline) -> Image.Image:
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        if outline:
            draw.rounded_rectangle(box, radius=radius, outline=int(alpha), width=outline)
        else:
            draw.rounded_rectangle(box, radius=radius, fill=int(alpha))
        mask = mask.filter(ImageFilter.GaussianBlur(max(1.0, blur)))
        layer = Image.new("RGBA", size, rgba(color, 0))
        layer.putalpha(mask)
        return layer

    # ---------------- backdrop ----------------

    def background(self, w: int, h: int, keep_clear=()) -> Image.Image:
        """keep_clear: logical (x, y, w, h) rects where bright flare stars must not land."""
        img = Image.new("RGBA", (w, h), rgba(VOID))

        # Nebula haze: painted small and upscaled so the blur stays cheap and silky.
        q = 6
        sw, sh = max(8, w // q), max(8, h // q)
        nebula = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        clouds = [
            (0.08, 0.02, 0.42, 0.34, STARLIGHT, 15),
            (0.95, 0.52, 0.42, 0.56, STARLIGHT, 20),
            (0.46, 1.06, 0.58, 0.30, SILVER, 13),
            (0.99, 0.99, 0.22, 0.20, STARLIGHT, 7),
        ]
        for cx, cy, rx, ry, color, alpha in clouds:
            mask = Image.new("L", (sw, sh), 0)
            ImageDraw.Draw(mask).ellipse(
                ((cx - rx) * sw, (cy - ry) * sh, (cx + rx) * sw, (cy + ry) * sh), fill=alpha
            )
            mask = mask.filter(ImageFilter.GaussianBlur(sw * 0.09))
            layer = Image.new("RGBA", (sw, sh), rgba(color, 0))
            layer.putalpha(mask)
            nebula = Image.alpha_composite(nebula, layer)
        img = Image.alpha_composite(img, nebula.resize((w, h), Image.Resampling.BICUBIC))

        # Starfield: seeded so the sky is identical on every launch.
        rnd = random.Random(20261114)
        stars = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(stars)
        tints = ["#FFFFFF"] * 7 + ["#DADADA", "#BFBFBF", "#EDEDED"]
        count = int((w / self.scale) * (h / self.scale) / 1050)
        for _ in range(count):
            x, y = rnd.random() * w, rnd.random() * h
            brightness = rnd.random() ** 3
            color = rgba(rnd.choice(tints), 45 + 190 * brightness)
            if brightness > 0.82:
                r = max(1.0, 0.9 * self.scale)
                draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
            else:
                draw.point((x, y), fill=color)
        img = Image.alpha_composite(img, stars)

        # A handful of flared stars with soft halos.
        flares = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fdraw = ImageDraw.Draw(flares)
        placed = attempts = 0
        while placed < 7 and attempts < 200:
            attempts += 1
            x, y = rnd.random() * w, rnd.random() * h
            lx, ly = x / self.scale, y / self.scale
            if any(rx - 12 <= lx <= rx + rw + 12 and ry - 12 <= ly <= ry + rh + 12 for rx, ry, rw, rh in keep_clear):
                continue
            placed += 1
            tint = rnd.choice(["#FFFFFF", "#E6E6E6"])
            core = max(1.2, 1.3 * self.scale)
            arm = self.px(rnd.uniform(5, 9))
            fdraw.line((x - arm, y, x + arm, y), fill=rgba(tint, 120), width=1)
            fdraw.line((x, y - arm, x, y + arm), fill=rgba(tint, 120), width=1)
            fdraw.ellipse((x - core * 2.2, y - core * 2.2, x + core * 2.2, y + core * 2.2), fill=rgba(tint, 150))
        halo = flares.filter(ImageFilter.GaussianBlur(max(1.0, 1.6 * self.scale)))
        img = Image.alpha_composite(img, halo)
        img = Image.alpha_composite(img, flares)
        return img

    # ---------------- icons (vector-style, drawn in code) ----------------

    def icon(self, kind: str, size: int, accent: str) -> Image.Image:
        return self._memo(("icon", kind, size, accent), lambda: self._icon(kind, size, accent))

    def _icon(self, kind: str, size: int, accent: str) -> Image.Image:
        big = size * SUPERSAMPLE
        u = big / 100.0
        im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)

        if kind == "comet":
            for i in range(40, -1, -1):  # tail drawn far-to-near so each step overwrites cleanly
                f = i / 40
                x, y = 64 - 50 * f, 36 + 50 * f
                r = 14 - 11 * f
                d.ellipse(((x - r) * u, (y - r) * u, (x + r) * u, (y + r) * u),
                          fill=rgba(mix(STARLIGHT, SILVER, f), 235 * (1 - f) ** 1.7))
            d.ellipse((52 * u, 22 * u, 78 * u, 48 * u), fill=rgba(mix("#FFFFFF", STARLIGHT, 0.25)))
            for dx, dy, length in ((30, 20, 16), (40, 56, 12)):
                d.line((dx * u, dy * u, (dx - length) * u, (dy + length) * u),
                       fill=rgba(STARLIGHT, 150), width=max(1, int(3 * u)))
        elif kind == "planet":
            ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((6 * u, 39 * u, 94 * u, 61 * u),
                                         outline=rgba(mix(STARLIGHT, "#FFFFFF", 0.45)), width=max(1, int(5 * u)))
            front_mask = Image.new("L", (big, big), 0)
            ImageDraw.Draw(front_mask).rectangle((0, 50 * u, big, big), fill=255)
            front, back = ring.copy(), ring.copy()
            front.putalpha(ImageChops.multiply(front.getchannel("A"), front_mask))
            back.putalpha(ImageChops.multiply(back.getchannel("A"), ImageChops.invert(front_mask)))
            sphere_mask = Image.new("L", (big, big), 0)
            ImageDraw.Draw(sphere_mask).ellipse((25 * u, 25 * u, 75 * u, 75 * u), fill=255)
            sphere = self.gradient((big, big), rgba(mix(STARLIGHT, "#FFFFFF", 0.30)),
                                   rgba(mix(STARLIGHT, VOID, 0.45)), horizontal=False)
            sphere.putalpha(sphere_mask)
            im = Image.alpha_composite(Image.alpha_composite(back, sphere), front)
            im = im.rotate(-18, resample=Image.Resampling.BICUBIC)
        elif kind == "sparkle":
            def star(cx, cy, arm, waist, color):
                pts = []
                for k in range(8):
                    ang = math.pi / 4 * k - math.pi / 2
                    rad = arm if k % 2 == 0 else waist
                    pts.append(((cx + math.cos(ang) * rad) * u, (cy + math.sin(ang) * rad) * u))
                d.polygon(pts, fill=color)
            star(44, 56, 40, 9, rgba(STARLIGHT))
            star(80, 20, 14, 3.5, rgba(mix(STARLIGHT, "#FFFFFF", 0.4)))
            d.ellipse((38 * u, 50 * u, 50 * u, 62 * u), fill=rgba(mix(STARLIGHT, "#FFFFFF", 0.6)))
        elif kind == "signal":
            for i, height in enumerate((26, 44, 62, 80)):
                x = 14 + i * 20
                d.rounded_rectangle((x * u, (90 - height) * u, (x + 13) * u, 90 * u), radius=4 * u,
                                    fill=rgba(STARLIGHT, 150 + 35 * i))
        elif kind == "sliders":
            for y, knob in ((24, 64), (50, 34), (76, 58)):
                d.line((12 * u, y * u, 88 * u, y * u), fill=rgba(SILVER, 200), width=max(1, int(7 * u)))
                d.ellipse(((knob - 11) * u, (y - 11) * u, (knob + 11) * u, (y + 11) * u), fill=rgba(STARLIGHT))
        elif kind in ("astra", "astra-small"):
            # The app's mark: a four-point flare star, circled by a tilted orbit (dropped at tiny sizes).
            if kind == "astra":
                ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
                rd = ImageDraw.Draw(ring)
                rd.ellipse((6 * u, 35 * u, 94 * u, 65 * u), outline=rgba(SILVER, 215), width=max(1, int(4.5 * u)))
                rd.ellipse((83 * u, 36 * u, 95 * u, 48 * u), fill=rgba(STARLIGHT))
                im = Image.alpha_composite(im, ring.rotate(-24, resample=Image.Resampling.BICUBIC))
                d = ImageDraw.Draw(im)
            arm, waist = (40, 8.5) if kind == "astra" else (46, 11)
            points = []
            for k in range(8):
                angle = math.pi / 4 * k - math.pi / 2
                radius = arm if k % 2 == 0 else waist
                points.append(((50 + math.cos(angle) * radius) * u, (50 + math.sin(angle) * radius) * u))
            d.polygon(points, fill=rgba(STARLIGHT))
            d.ellipse((43 * u, 43 * u, 57 * u, 57 * u), fill=rgba("#FFFFFF"))
        elif kind == "info":
            d.ellipse((12 * u, 12 * u, 88 * u, 88 * u), outline=rgba(STARLIGHT), width=max(1, int(7 * u)))
            d.ellipse((44 * u, 26 * u, 56 * u, 38 * u), fill=rgba(STARLIGHT))
            d.rounded_rectangle((44 * u, 45 * u, 56 * u, 74 * u), radius=5 * u, fill=rgba(STARLIGHT))
        elif kind == "shield":
            outline = [(50, 8), (86, 21), (86, 46), (80, 66), (66, 82), (50, 92), (34, 82), (20, 66), (14, 46),
                       (14, 21)]
            d.polygon([(x * u, y * u) for x, y in outline], outline=rgba(STARLIGHT), width=max(1, int(7 * u)))
            d.ellipse((41 * u, 32 * u, 59 * u, 50 * u), fill=rgba(STARLIGHT))
            d.polygon([(45 * u, 46 * u), (55 * u, 46 * u), (58 * u, 68 * u), (42 * u, 68 * u)], fill=rgba(STARLIGHT))
        elif kind == "contrast":
            d.ellipse((14 * u, 14 * u, 86 * u, 86 * u), outline=rgba(STARLIGHT), width=max(1, int(7 * u)))
            d.pieslice((14 * u, 14 * u, 86 * u, 86 * u), 90, 270, fill=rgba(STARLIGHT))
        elif kind == "orbit":
            ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ring)
            rd.ellipse((4 * u, 30 * u, 96 * u, 70 * u), outline=rgba(SILVER, 230), width=max(1, int(6 * u)))
            rd.ellipse((80 * u, 36 * u, 94 * u, 50 * u), fill=rgba("#FFFFFF"))
            ring = ring.rotate(24, resample=Image.Resampling.BICUBIC)
            core_mask = Image.new("L", (big, big), 0)
            ImageDraw.Draw(core_mask).ellipse((30 * u, 30 * u, 70 * u, 70 * u), fill=255)
            core = self.gradient((big, big), rgba(STARLIGHT), rgba(SILVER), horizontal=False)
            core.putalpha(core_mask)
            im = Image.alpha_composite(core, ring)

        halo = im.filter(ImageFilter.GaussianBlur(big * 0.07))
        halo.putalpha(halo.getchannel("A").point(lambda v: int(v * 0.75)))
        im = Image.alpha_composite(halo, im)
        return im.resize((size, size), Image.Resampling.LANCZOS)

    # ---------------- components ----------------

    def radio(self, r: int, accent: str, selected: bool) -> Image.Image:
        s = 2 * r + self.px(6)
        big = s * SUPERSAMPLE
        im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        c, rr = big / 2, r * SUPERSAMPLE
        lw = max(1, int(1.5 * self.scale * SUPERSAMPLE))
        if selected:
            d.ellipse((c - rr, c - rr, c + rr, c + rr), outline=rgba(accent), width=lw)
            ri = rr * 0.52
            d.ellipse((c - ri, c - ri, c + ri, c + ri), fill=rgba(accent))
        else:
            d.ellipse((c - rr, c - rr, c + rr, c + rr), outline=(255, 255, 255, 64), width=lw)
        return im.resize((s, s), Image.Resampling.LANCZOS)

    def card(self, w: int, h: int, accent: str, t: float, selected: bool, icon: str, grow: bool = True):
        """Profile card with ambient glow. t (0..1) drives glow, border light and (if grow) scale."""
        m = self.px(40)  # room for the full blur (3 x radius) so the glow never ends in a hard edge
        size = (w + 2 * m, h + 2 * m)
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        k = 1.0 + (0.03 * t if grow else 0.0)
        cw, ch = int(round(w * k)), int(round(h * k))
        x0, y0 = (size[0] - cw) // 2, (size[1] - ch) // 2
        radius = self.px(16) * k
        if t > 0.01:
            img = Image.alpha_composite(img, self.glow_layer(
                size, (x0, y0, x0 + cw, y0 + ch), radius, accent, 90 * t, self.px(12)))
        top = mix(SURFACE, accent, 0.03 + 0.07 * t)
        border_color = mix("#FFFFFF", accent, min(1.0, t * 1.6))
        alpha = 255 * self.glass
        body = self.shape(cw, ch, radius, grad=(rgba(top, alpha), rgba(SURFACE, alpha), False),
                          border=rgba(border_color, 15 + 205 * t), border_width=1.0 + 0.4 * t)
        img.alpha_composite(body, (x0, y0))
        glyph = self.icon(icon, self.px(26 * k), accent)
        img.alpha_composite(glyph, (x0 + self.px(14 * k), y0 + self.px(13 * k)))
        dot = self.radio(self.px(7 * k), accent, selected)
        img.alpha_composite(dot, (x0 + cw - self.px(21 * k) - dot.width // 2, y0 + self.px(23 * k) - dot.height // 2))
        return img, m

    def primary_button(self, w: int, h: int, state: str, t: float = 0.0, phase=None):
        """Cosmic gradient button. state: normal|hover|pressed|busy|disabled. t = bloom amount."""
        m = self.px(52)
        size = (w + 2 * m, h + 2 * m)
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        radius = self.px(14)
        if state == "disabled":
            c1, c2 = "#1A1A1A", "#141414"
        else:
            if state == "pressed":
                c1, c2 = mix(STARLIGHT, VOID, 0.14), mix(SILVER, VOID, 0.14)
            else:
                c1, c2 = STARLIGHT, mix("#C4C4C4", "#FFFFFF", 0.45 * t)
            bloom = 40 + 100 * t
            img = Image.alpha_composite(img, self.glow_layer(
                size, (m, m, m + w, m + h), radius, mix(STARLIGHT, SILVER, 0.5), bloom, self.px(10 + 6 * t)))
        body = self.shape(w, h, radius, grad=(rgba(c1), rgba(c2), True))
        if state != "disabled":
            sheen = self.gradient((w, h), (255, 255, 255, 44), (255, 255, 255, 0), horizontal=False)
            sheen.putalpha(ImageChops.multiply(sheen.getchannel("A"), body.getchannel("A")))
            body = Image.alpha_composite(body, sheen)
        if phase is not None and state == "busy":
            band = Image.new("L", (w, h), 0)
            cx = -0.35 * w + phase * 1.7 * w
            half = 0.09 * w
            ImageDraw.Draw(band).polygon(
                [(cx - half + h * 0.5, 0), (cx + half + h * 0.5, 0), (cx + half - h * 0.5, h), (cx - half - h * 0.5, h)],
                fill=70,
            )
            band = band.filter(ImageFilter.GaussianBlur(self.px(5)))
            shine = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            shine.putalpha(ImageChops.multiply(band, body.getchannel("A")))
            body = Image.alpha_composite(body, shine)
        img.alpha_composite(body, (m, m))
        return img, m

    def ghost_button(self, w: int, h: int, state: str) -> Image.Image:
        fills = {"normal": (255, 255, 255, 6), "hover": (255, 255, 255, 14),
                 "pressed": (255, 255, 255, 24), "disabled": (255, 255, 255, 3)}
        borders = {"normal": (255, 255, 255, 46), "hover": rgba(STARLIGHT, 175),
                   "pressed": rgba(STARLIGHT, 230), "disabled": (255, 255, 255, 18)}
        return self.shape(w, h, self.px(14), fill=fills[state], border=borders[state], border_width=1.0)

    def window_button(self, w: int, h: int, kind: str, state: str) -> Image.Image:
        """Title-strip control (minimize / close) drawn to match the theme."""
        fill = {"normal": None, "hover": (255, 255, 255, 22), "pressed": (255, 255, 255, 40)}[state]
        img = self.shape(w, h, self.px(8), fill=fill) if fill else Image.new("RGBA", (w, h), (0, 0, 0, 0))
        big = Image.new("RGBA", (w * SUPERSAMPLE, h * SUPERSAMPLE), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        color = rgba(TEXT_HI if state != "normal" else TEXT_MID)
        cx, cy = big.width / 2, big.height / 2
        arm = 5 * self.scale * SUPERSAMPLE
        lw = max(1, int(round(1.3 * self.scale * SUPERSAMPLE)))
        if kind == "min":
            d.line((cx - arm, cy, cx + arm, cy), fill=color, width=lw)
        else:
            d.line((cx - arm, cy - arm, cx + arm, cy + arm), fill=color, width=lw)
            d.line((cx - arm, cy + arm, cx + arm, cy - arm), fill=color, width=lw)
        return Image.alpha_composite(img, big.resize((w, h), Image.Resampling.LANCZOS))

    def checkbox(self, size: int, checked: bool, hover: bool) -> Image.Image:
        if checked:
            img = self.shape(size, size, self.px(5), fill=rgba(STARLIGHT))
            big = Image.new("RGBA", (size * SUPERSAMPLE, size * SUPERSAMPLE), (0, 0, 0, 0))
            u = big.width / 100.0
            ImageDraw.Draw(big).line([(24 * u, 52 * u), (43 * u, 70 * u), (77 * u, 32 * u)], fill=rgba(VOID),
                                     width=max(1, int(11 * u)), joint="curve")
            return Image.alpha_composite(img, big.resize((size, size), Image.Resampling.LANCZOS))
        return self.shape(size, size, self.px(5), fill=(255, 255, 255, 10 if hover else 4),
                          border=(255, 255, 255, 200 if hover else 120), border_width=1.4)

    def status_mark(self, size: int, status: str) -> Image.Image:
        """Checkup row marks: ok = white ring with a tick, warn = yellow disc with '!', info = grey ring
        with a dash (couldn't tell), pending = faint grey ring."""
        return self._memo(("status", size, status), lambda: self._status_mark(size, status))

    @staticmethod
    def _status_mark(size: int, status: str) -> Image.Image:
        big = size * 4
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        lw = max(2, round(big * 0.085))
        box = (lw / 2 + 1, lw / 2 + 1, big - lw / 2 - 1, big - lw / 2 - 1)
        if status == "ok":
            d.ellipse(box, outline=rgba(STARLIGHT, 235), width=lw)
            d.line([(big * 0.30, big * 0.52), (big * 0.44, big * 0.66), (big * 0.71, big * 0.37)],
                   fill=rgba(STARLIGHT), width=lw, joint="curve")
        elif status == "warn":
            d.ellipse((1, 1, big - 1, big - 1), fill=rgba(WARN_YELLOW))
            cx, half = big / 2, lw * 0.62
            d.rounded_rectangle((cx - half, big * 0.22, cx + half, big * 0.60), radius=half, fill=rgba(VOID))
            d.ellipse((cx - half * 1.15, big * 0.68, cx + half * 1.15, big * 0.68 + half * 2.3), fill=rgba(VOID))
        elif status == "info":
            d.ellipse(box, outline=rgba(ASH), width=lw)
            d.line([(big * 0.32, big / 2), (big * 0.68, big / 2)], fill=rgba(ASH), width=lw)
        else:
            d.ellipse(box, outline=rgba(ASH, 140), width=lw)
        return img.resize((size, size), Image.LANCZOS)

    def toggle(self, w: int, h: int, on: bool, state: str) -> Image.Image:
        """Switch. state: normal|hover|disabled."""
        hover = state == "hover"
        if on:
            track = self.shape(w, h, h / 2, fill=rgba(STARLIGHT))
        else:
            track = self.shape(w, h, h / 2, fill=(255, 255, 255, 16 if hover else 8),
                               border=(255, 255, 255, 190 if hover else 110), border_width=1.3)
        big = Image.new("RGBA", (w * SUPERSAMPLE, h * SUPERSAMPLE), (0, 0, 0, 0))
        pad = 4 * self.scale * SUPERSAMPLE
        dia = big.height - 2 * pad
        x = big.width - pad - dia if on else pad
        ImageDraw.Draw(big).ellipse((x, pad, x + dia, pad + dia),
                                    fill=rgba(VOID) if on else rgba(TEXT_HI if hover else SILVER))
        out = Image.alpha_composite(track, big.resize((w, h), Image.Resampling.LANCZOS))
        if state == "disabled":
            out.putalpha(out.getchannel("A").point(lambda v: int(v * 0.35)))
        return out

    def round_mask(self, w: int, h: int, radius: float) -> Image.Image:
        return self._memo(("mask", w, h, radius),
                          lambda: self._mask(w, h, radius).resize((w, h), Image.Resampling.LANCZOS))

    def app_icon(self, size: int) -> Image.Image:
        """The app icon at an exact pixel size: the Astra mark on a black rounded tile."""
        tile = self._shape(size, size, size * 0.24,
                           fill=None, grad=(rgba("#1C1C1C"), rgba("#000000"), False),
                           border=(255, 255, 255, 60) if size >= 32 else None, border_width=max(1.0, size / 128))
        kind = "astra" if size >= 40 else "astra-small"
        glyph = self._icon(kind, max(8, int(round(size * (0.80 if size >= 40 else 0.86)))), STARLIGHT)
        tile.alpha_composite(glyph, ((size - glyph.width) // 2, (size - glyph.height) // 2))
        return tile

    # ---------------- startup intro ----------------

    def intro_mark(self, size: int, arm: float, sweep: float) -> Image.Image:
        """The Astra mark part-drawn, same geometry as the "astra" icon: arm = how far the star has
        grown (0..1, may overshoot), sweep = how much of the orbit is traced (0..1, moon leading)."""
        ss = 2
        big = max(4, size * ss)
        u = big / 100.0
        im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        cos_t, sin_t = math.cos(INTRO_ORBIT_TILT), math.sin(INTRO_ORBIT_TILT)

        def at(theta: float) -> tuple[float, float]:
            dx, dy = 44 * math.cos(theta), 15 * math.sin(theta)
            return (50 + dx * cos_t - dy * sin_t) * u, (50 + dx * sin_t + dy * cos_t) * u

        if sweep > 0:
            start = INTRO_MOON_ANGLE - 2 * math.pi
            steps = max(2, int(120 * sweep))
            points = [at(start + 2 * math.pi * sweep * i / steps) for i in range(steps + 1)]
            d.line(points, fill=rgba(SILVER, 215), width=max(1, int(round(4.5 * u))), joint="curve")
            mx, my = points[-1]
            d.ellipse((mx - 6 * u, my - 6 * u, mx + 6 * u, my + 6 * u), fill=rgba(STARLIGHT))
        if arm > 0:
            length, waist = 40 * arm, 8.5 * min(1.0, arm)
            points = []
            for k in range(8):
                angle = math.pi / 4 * k - math.pi / 2
                radius = length if k % 2 == 0 else waist
                points.append(((50 + math.cos(angle) * radius) * u, (50 + math.sin(angle) * radius) * u))
            d.polygon(points, fill=rgba(STARLIGHT))
            core = 7 * min(1.0, arm * 1.4)
            d.ellipse(((50 - core) * u, (50 - core) * u, (50 + core) * u, (50 + core) * u), fill=rgba("#FFFFFF"))
        return im.resize((size, size), Image.Resampling.LANCZOS)

    def intro_glow(self, size: int) -> Image.Image:
        """Soft starlight bloom behind the mark (size = the mark's size; the bloom is twice that)."""
        def make() -> Image.Image:
            g = size * 2
            mask = Image.new("L", (g, g), 0)
            ImageDraw.Draw(mask).ellipse((g * 0.36, g * 0.36, g * 0.64, g * 0.64), fill=150)
            mask = mask.filter(ImageFilter.GaussianBlur(g * 0.11))
            layer = Image.new("RGBA", (g, g), rgba(STARLIGHT, 0))
            layer.putalpha(mask)
            return layer
        return self._memo(("intro-glow", size), make)

    def intro_word(self, text: str, size: int, tracking: float) -> Image.Image:
        """The wordmark set glyph by glyph so its letter-spacing can close up as it appears.
        tracking is extra space per letter as a fraction of the font size. The image is one line
        box tall (ascent + descent), like a Tk text item, so it can be centred the same way."""
        font = _overlay_font(size, b"SemiBold")
        try:
            ascent, descent = font.getmetrics()
        except Exception:
            ascent, descent = size, size // 4
        gap = tracking * size
        advances = [font.getlength(ch) for ch in text]
        width = int(math.ceil(sum(advances) + gap * (len(text) - 1))) + 2
        im = Image.new("RGBA", (max(1, width), ascent + descent), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        x = 1.0
        for ch, advance in zip(text, advances):
            d.text((x, 0), ch, font=font, fill=rgba(TEXT_HI))
            x += advance + gap
        return im

    def avatar(self, size: int, path: str) -> Image.Image:
        """Round avatar with a fine light ring; a lettered disc if the picture is missing."""
        big = size * SUPERSAMPLE
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
        try:
            with Image.open(path) as source:
                picture = ImageOps.fit(source.convert("RGBA"), (big, big), Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            picture = self.gradient((big, big), rgba("#2A2A2A"), rgba("#0A0A0A"), horizontal=False)
        out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        out.paste(picture, (0, 0), mask)
        ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        width = max(2, int(2 * self.scale * SUPERSAMPLE))
        ImageDraw.Draw(ring).ellipse((width // 2, width // 2, big - 1 - width // 2, big - 1 - width // 2),
                                     outline=(255, 255, 255, 150), width=width)
        out = Image.alpha_composite(out, ring)
        return out.resize((size, size), Image.Resampling.LANCZOS)

    def seg_track(self, w: int, h: int) -> Image.Image:
        return self.shape(w, h, h / 2, fill=(255, 255, 255, 8), border=(255, 255, 255, 42), border_width=1.0)

    def seg_thumb(self, w: int, h: int):
        """The lit capsule that marks the chosen segment."""
        m = self.px(16)
        size = (w + 2 * m, h + 2 * m)
        img = self.glow_layer(size, (m, m, m + w, m + h), h / 2, STARLIGHT, 55, self.px(5))
        img.alpha_composite(self.shape(w, h, h / 2, grad=(rgba(STARLIGHT), rgba("#D4D4D4"), False)), (m, m))
        return img, m

    SLIDER_MARGIN = 18

    def slider_image(self, w: int, frac: float, state: str):
        """Hairline track whose filled part is a comet tail ending in a glowing dot (the knob).
        Built from cached pieces so it can be redrawn on every mouse move.
        state: normal | hover | drag | disabled. Returns (image, margin)."""
        m, h = self.px(self.SLIDER_MARGIN), max(2, self.px(4))

        def track() -> Image.Image:
            base = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
            base.alpha_composite(self.shape(w, h, h / 2, fill=(255, 255, 255, 30)), (m, m))
            return base

        img = self._memo(("strack", w, h, m), track)
        fx = int(round(w * max(0.0, min(1.0, frac))))
        if fx >= 2:
            tail = self.gradient((fx, h), rgba(SILVER, 0), rgba(STARLIGHT), True)
            mask = self.round_mask(w, h, h / 2).crop((0, 0, fx, h))
            tail.putalpha(ImageChops.multiply(tail.getchannel("A"), mask))
            img.alpha_composite(tail, (m, m))
        knob = self._memo(("knob", state), lambda: self._slider_knob(state))
        paste_layer(img, knob, m + fx - knob.width // 2, m + h // 2 - knob.height // 2)
        if state == "disabled":
            img.putalpha(img.getchannel("A").point(lambda v: int(v * 0.3)))
        return img, m

    def _slider_knob(self, state: str) -> Image.Image:
        size = 2 * self.px(self.SLIDER_MARGIN)
        active = state in ("hover", "drag")
        big = Image.new("RGBA", (size * SUPERSAMPLE, size * SUPERSAMPLE), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        c = size * SUPERSAMPLE / 2
        core = {"drag": 6.4, "hover": 5.8}.get(state, 5.0) * self.scale * SUPERSAMPLE  # grows a little when touched
        d.ellipse((c - core, c - core, c + core, c + core), fill=rgba(STARLIGHT))
        star = big.resize((size, size), Image.Resampling.LANCZOS)
        halo = star.filter(ImageFilter.GaussianBlur(self.px(4.5 if active else 3)))
        return Image.alpha_composite(halo, star)

    def rail_tab(self, w: int, h: int, icon: str, selected: bool, hovered: bool):
        """Side-rail tab: icon on a glass tile, with a lit edge marker when selected."""
        m = self.px(22)
        size = (w + 2 * m, h + 2 * m)
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        radius = self.px(12)
        if selected:
            img = Image.alpha_composite(img, self.glow_layer(size, (m, m, m + w, m + h), radius, STARLIGHT, 34,
                                                             self.px(7)))
            img.alpha_composite(self.shape(w, h, radius, fill=(255, 255, 255, 20), border=(255, 255, 255, 80)), (m, m))
            bar = self.shape(self.px(3), self.px(22), self.px(1.5), fill=rgba(STARLIGHT))
            img.alpha_composite(bar, (m + self.px(4), m + (h - self.px(22)) // 2))
        elif hovered:
            img.alpha_composite(self.shape(w, h, radius, fill=(255, 255, 255, 9), border=(255, 255, 255, 28)), (m, m))
        glyph = self.icon(icon, self.px(22), STARLIGHT)
        if not selected:
            factor = 0.8 if hovered else 0.5
            glyph.putalpha(glyph.getchannel("A").point(lambda v, f=factor: int(v * f)))
        img.alpha_composite(glyph, (m + (w - glyph.width) // 2, m + self.px(9)))
        return img, m

    def pill(self, w: int, h: int, color: str):
        """Telemetry status module: capsule with a soft neon edge."""
        m = self.px(14)
        size = (w + 2 * m, h + 2 * m)
        img = self.glow_layer(size, (m, m, m + w - 1, m + h - 1), h / 2, color, 70, self.px(4),
                              outline=max(2, self.px(2)))
        alpha = 255 * self.glass
        body = self.shape(w, h, h / 2, grad=(rgba(mix(SURFACE, color, 0.07), alpha), rgba(SURFACE, alpha), False),
                          border=rgba(color, 95), border_width=1.0)
        img.alpha_composite(body, (m, m))
        return img, m

    def panel(self, w: int, h: int, accent: str):
        m = self.px(50)
        size = (w + 2 * m, h + 2 * m)
        img = self.glow_layer(size, (m, m, m + w, m + h), self.px(18), accent, 40, self.px(16))
        body = self.shape(w, h, self.px(18), grad=(rgba(mix(SURFACE, accent, 0.06)), rgba(SURFACE), False),
                          border=(255, 255, 255, 26), border_width=1.0)
        img.alpha_composite(body, (m, m))
        return img, m


class ConsoleView:
    """Terminal-style log: deep core, faint grid, colour-coded status prefixes, wheel scrolling."""

    MAX_ROWS = 1200
    KEEP_ROWS = 900

    def __init__(self, app: "OptimizerApp", x: int, y: int, w: int, h: int):
        self.app = app
        self.w, self.h = w, h
        self.canvas = tk.Canvas(app.canvas, width=w, height=h, bg=CONSOLE_CORE, highlightthickness=0,
                                bd=0, yscrollincrement=1)
        self.window = app.canvas.create_window(x, y, anchor="nw", window=self.canvas)
        self.pad = app.px(10)
        char = app.f_mono.measure("0")
        self.line_gap = app.px(3)
        self.x_ts = self.pad
        self.x_tag = self.pad + char * 9
        self.x_msg = self.x_tag + char * 7
        self.grid_step = app.px(22)
        self.grid_color = mix(CONSOLE_CORE, STARLIGHT, 0.055)
        self.history: list[tuple[str, str, str]] = []
        self.follow = True
        self.bg_photo = None
        self.bg_item = None
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        # The backdrop is pinned to the visible top; re-pin once the widget has its real size
        # (at start-up it is 1 px tall, which parked the image out of view - a black box).
        self.canvas.bind("<Configure>", self._on_configure)
        self._reset()

    def _on_configure(self, _event) -> None:
        if self.follow:
            self.canvas.yview_moveto(1.0)
        self._pin_backdrop()

    def set_backdrop(self, image: Image.Image) -> None:
        """Show the window's own backdrop behind the log (so it is glass, not a black box).
        The grid is drawn into that image and stays put while the text scrolls."""
        image = image.convert("RGBA")
        grid = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(grid)
        for x in range(self.grid_step, self.w, self.grid_step):
            draw.line((x, 0, x, self.h), fill=(255, 255, 255, 13))
        for y in range(self.grid_step, self.h, self.grid_step):
            draw.line((0, y, self.w, y), fill=(255, 255, 255, 13))
        self.bg_photo = ImageTk.PhotoImage(Image.alpha_composite(image, grid).convert("RGB"))
        self.canvas.delete("grid")
        self.vlines = []
        if self.bg_item is None:
            self.bg_item = self.canvas.create_image(0, 0, anchor="nw", image=self.bg_photo, tags="bg")
        else:
            self.canvas.itemconfigure(self.bg_item, image=self.bg_photo)
        self._pin_backdrop()

    def _pin_backdrop(self) -> None:
        if self.bg_item is not None:
            self.canvas.coords(self.bg_item, 0, self.canvas.canvasy(0))
            self.canvas.tag_lower(self.bg_item)

    def _reset(self) -> None:
        self.canvas.delete("all")
        self.cursor_y = self.pad
        self.grid_bottom = 0
        self.vlines = []
        self.bg_item = None
        if self.bg_photo is not None:
            self.bg_item = self.canvas.create_image(0, 0, anchor="nw", image=self.bg_photo, tags="bg")
        else:
            self.vlines = [
                (self.canvas.create_line(x, 0, x, self.h, fill=self.grid_color, tags="grid"), x)
                for x in range(self.grid_step, self.w, self.grid_step)
            ]
            self._extend_grid(self.h)
        self.canvas.configure(scrollregion=(0, 0, self.w, self.h))

    def _extend_grid(self, to_y: int) -> None:
        if self.bg_photo is not None:
            return  # the grid lives in the backdrop image
        target = max(to_y, self.h)
        while self.grid_bottom < target:
            self.grid_bottom += self.grid_step
            self.canvas.create_line(0, self.grid_bottom, self.w, self.grid_bottom, fill=self.grid_color, tags="grid")
        for line_id, x in self.vlines:
            self.canvas.coords(line_id, x, 0, x, self.grid_bottom)
        self.canvas.tag_lower("grid")

    def append(self, stamp: str, level: str, message: str, record: bool = True) -> None:
        if record:
            self.history.append((stamp, level, message))
            if len(self.history) > self.MAX_ROWS:
                self.history = self.history[-self.KEEP_ROWS:]
                self._reset()
                for row in self.history:
                    self.append(*row, record=False)
                return
        prefix, prefix_color, text_color = CONSOLE_STYLES.get(level, CONSOLE_STYLES["info"])
        y = self.cursor_y
        self.canvas.create_text(self.x_ts, y, anchor="nw", text=stamp, fill="#3A3A3A", font=self.app.f_mono)
        tag_item = self.canvas.create_text(self.x_tag, y, anchor="nw", text=prefix, fill=prefix_color,
                                           font=self.app.f_mono_b)
        if level in ("warn", "fail"):
            x1, y1, x2, y2 = self.canvas.bbox(tag_item)
            chip = self.canvas.create_rectangle(
                x1 - self.app.px(2), y1, x2 + self.app.px(1), y2,
                fill=ERROR_RED if level == "fail" else "",
                outline=ERROR_RED if level == "fail" else WARN_YELLOW, width=1)
            self.canvas.tag_raise(tag_item, chip)
        item = self.canvas.create_text(self.x_msg, y, anchor="nw", text=message, fill=text_color,
                                       font=self.app.f_mono, width=self.w - self.x_msg - self.pad)
        bbox = self.canvas.bbox(item)
        self.cursor_y = (bbox[3] if bbox else y + self.app.px(14)) + self.line_gap
        self._extend_grid(self.cursor_y + self.pad)
        self.canvas.configure(scrollregion=(0, 0, self.w, max(self.h, self.cursor_y + self.pad)))
        if self.follow:
            self.canvas.yview_moveto(1.0)
        self._pin_backdrop()

    def _on_wheel(self, event) -> None:
        step = int(-event.delta / 120 * self.app.f_mono.metrics("linespace") * 3)
        self.canvas.yview_scroll(step, "units")
        self.follow = self.canvas.yview()[1] >= 0.999
        self._pin_backdrop()


_ov_api = ctypes.WinDLL("user32")  # private function objects for the overlay's layered window
_ov_api.GetParent.argtypes, _ov_api.GetParent.restype = [wintypes.HWND], wintypes.HWND
_ov_api.GetWindowLongW.argtypes, _ov_api.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
_ov_api.SetWindowLongW.argtypes, _ov_api.SetWindowLongW.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long
_ov_api.GetDC.argtypes, _ov_api.GetDC.restype = [wintypes.HWND], wintypes.HDC
_ov_api.ReleaseDC.argtypes, _ov_api.ReleaseDC.restype = [wintypes.HWND, wintypes.HDC], ctypes.c_int
_ov_api.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                        ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_ov_api.UpdateLayeredWindow.restype = wintypes.BOOL
_ov_api.GetForegroundWindow.argtypes, _ov_api.GetForegroundWindow.restype = [], wintypes.HWND
_ov_api.SetForegroundWindow.argtypes, _ov_api.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
_ov_api.GetSystemMetrics.argtypes, _ov_api.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
_ov_api.EndMenu.argtypes, _ov_api.EndMenu.restype = [], wintypes.BOOL
_ov_gdi = ctypes.WinDLL("gdi32")
_ov_gdi.CreateCompatibleDC.argtypes, _ov_gdi.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
_ov_gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                     wintypes.HANDLE, wintypes.DWORD]
_ov_gdi.CreateDIBSection.restype = wintypes.HBITMAP
_ov_gdi.SelectObject.argtypes, _ov_gdi.SelectObject.restype = [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ
_ov_gdi.DeleteObject.argtypes, _ov_gdi.DeleteObject.restype = [wintypes.HGDIOBJ], wintypes.BOOL
_ov_gdi.DeleteDC.argtypes, _ov_gdi.DeleteDC.restype = [wintypes.HDC], wintypes.BOOL
GWL_EXSTYLE_I = -20
WS_EX_TOPMOST_, WS_EX_TOOLWINDOW_, WS_EX_LAYERED_, WS_EX_NOACTIVATE_ = 0x8, 0x80, 0x80000, 0x08000000
ULW_ALPHA_ = 0x2


class _BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


def push_layered(hwnd: int, image: Image.Image, x: int, y: int) -> bool:
    """Show an RGBA image as a per-pixel-transparent window (alpha 0 is also click-through)."""
    w, h = image.size
    r, g, b, a = image.split()
    premultiplied = Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a),
                                         ImageChops.multiply(r, a), a))  # BGRA, premultiplied
    data = premultiplied.tobytes()
    screen = _ov_api.GetDC(None)
    memory = _ov_gdi.CreateCompatibleDC(screen)
    bits = ctypes.c_void_p()
    header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bitmap = _ov_gdi.CreateDIBSection(memory, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    try:
        if not bitmap or not bits.value:
            return False
        ctypes.memmove(bits.value, data, len(data))
        previous = _ov_gdi.SelectObject(memory, bitmap)
        blend = _BlendFunction(0, 0, 255, 1)  # AC_SRC_OVER, per-pixel alpha
        ok = _ov_api.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                                         ctypes.byref(wintypes.SIZE(w, h)), memory,
                                         ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA_)
        _ov_gdi.SelectObject(memory, previous)
        return bool(ok)
    finally:
        if bitmap:
            _ov_gdi.DeleteObject(bitmap)
        _ov_gdi.DeleteDC(memory)
        _ov_api.ReleaseDC(None, screen)


OVERLAY_DEFAULTS = {"bg": "glass", "size": "small", "low": True, "graph": True, "limit": True, "gpu": True, "cpu": True,
                    "ping": True, "locked": False}
OVERLAY_BACKGROUNDS = [("clear", "Clear"), ("glass", "Glass"), ("solid", "Solid")]
OVERLAY_ITEMS = [("low", "1% low"), ("graph", "Frame-time graph"), ("limit", "Bottleneck (GPU / CPU / capped)"),
                 ("gpu", "GPU"), ("cpu", "CPU"), ("ping", "Ping")]


def _overlay_font(size: int, variation: bytes):
    for name in ("bahnschrift.ttf", "segoeuib.ttf" if b"Bold" in variation else "segoeui.ttf"):
        try:
            font = ImageFont.truetype(os.path.join(SYSTEM_ROOT, "Fonts", name), size)
        except OSError:
            continue
        try:
            font.set_variation_by_name(variation)
        except Exception:
            pass
        return font
    return ImageFont.load_default()


class OverlayRenderer:
    """Draws the in-game strip: value + small unit pairs in fixed-width slots (so nothing jitters
    as numbers change), with a frame-time trace after the FPS where stutter shows as a spike."""

    def __init__(self, scale: float, style: dict):
        self.style = dict(OVERLAY_DEFAULTS, **(style or {}))
        k = scale * (1.3 if self.style["size"] == "large" else 1.0)
        self.k = k
        self.f_value = _overlay_font(max(9, round(14 * k)), b"SemiBold")
        self.f_unit = _overlay_font(max(8, round(11 * k)), b"SemiCondensed")
        self.h = round(22 * k)
        self.pad, self.gap, self.unit_gap = round(9 * k), round(10 * k), round(3 * k)
        self.graph_w, self.graph_h = round(46 * k), round(12 * k)

    def _w(self, font, text: str) -> int:
        return math.ceil(font.getlength(text))

    def segments(self, d: dict) -> list:
        """(kind, value, unit, slot text, warn) in display order; 'graph' is its own kind."""
        st, out = self.style, []
        fps, low = d.get("fps"), d.get("low")
        banner = d.get("banner")  # (text, widest text it will show, highlight) during a benchmark
        if banner:
            out.append(("text", banner[0], "", banner[1], bool(banner[2])))
        out.append(("pair", "–" if fps is None else f"{fps:.0f}", "fps", "000", False))
        if st["graph"]:
            out.append(("graph", None, None, None, False))
        if st["low"]:
            out.append(("pair", "–" if low is None else f"{low:.0f}", "low", "000",
                        low is not None and fps is not None and low < fps * 0.5))
        if st["limit"]:
            bound = d.get("limit")
            out.append(("text", BOUND_SHORT.get(bound, "–"), "", "CPU-bound", False))
        if st["gpu"]:
            load, temp = d.get("gpu_load"), d.get("gpu_temp")
            out.append(("pair", "–" if load is None else f"{load:.0f}%", "gpu", "00%", False))
            if temp is not None:
                out.append(("tight", f"{temp:.0f}°", "", "00°", temp >= 85))  # sits with its gpu reading
        if st["cpu"]:
            cpu = d.get("cpu")
            out.append(("pair", "–" if cpu is None else f"{cpu:.0f}%", "cpu", "00%", False))
        if st["ping"] and d.get("ping") is not None:
            out.append(("pair", f"{d['ping']:.0f}", "ms", "00", d["ping"] >= 80))
        return out

    def render(self, d: dict) -> Image.Image:
        segs = self.segments(d)
        widths, slots = [], []
        for kind, value, unit, slot, _warn in segs:
            if kind == "graph":
                widths.append(self.graph_w)
                slots.append(0)
            else:
                slot_w = max(self._w(self.f_value, slot), self._w(self.f_value, value))
                slots.append(slot_w)
                widths.append(slot_w + (self.unit_gap + self._w(self.f_unit, unit) if unit else 0))
        gaps = [self.unit_gap * 2 if kind == "tight" else self.gap for kind, *_r in segs[1:]]
        w = self.pad * 2 + sum(widths) + sum(gaps)
        h = self.h
        bg = self.style["bg"]
        img = Image.new("RGBA", (w, h), (0, 0, 0, 1))  # alpha 1: invisible but still grabbable to drag
        if bg != "clear":
            fill = (0, 0, 0, 120) if bg == "glass" else (10, 10, 10, 225)
            panel = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(panel).rounded_rectangle((0, 0, w - 1, h - 1), radius=round(5 * self.k), fill=fill,
                                                   outline=(255, 255, 255, 26 if bg == "solid" else 0))
            img = Image.alpha_composite(img, panel)
        text = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(text)
        baseline = round(h * 0.73)
        x = self.pad
        for i, ((kind, value, unit, slot, warn), seg_w, slot_w) in enumerate(zip(segs, widths, slots)):
            if i:
                x += gaps[i - 1]
            if kind == "graph":
                self._graph(text, x, (h - self.graph_h) // 2, d.get("frames") or [])
            else:
                colour = hex_rgb(WARN_YELLOW) + (255,) if warn else (255, 255, 255, 255)
                left = x if kind == "text" else x + slot_w - self._w(self.f_value, value)  # banners read left to right
                draw.text((left, baseline), value, font=self.f_value, fill=colour, anchor="ls")
                if unit:
                    draw.text((x + slot_w + self.unit_gap, baseline), unit, font=self.f_unit,
                              fill=(255, 255, 255, 140), anchor="ls")
            x += seg_w
        if bg != "solid":  # a soft dark halo keeps white text readable over bright scenes
            alpha = text.getchannel("A").filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.1 * self.k))
            halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            halo.putalpha(alpha.point(lambda v: int(v * (0.85 if bg == "clear" else 0.5))))
            img = Image.alpha_composite(img, halo)
        return Image.alpha_composite(img, text)

    def _graph(self, layer: Image.Image, x0: int, y0: int, frames: list) -> None:
        frames = [f for f in frames[-90:] if f > 0]
        gw, gh = self.graph_w, self.graph_h
        ss = 3
        big = Image.new("RGBA", (gw * ss, gh * ss), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        draw.line((0, gh * ss - 1, gw * ss, gh * ss - 1), fill=(255, 255, 255, 46), width=ss)  # baseline
        if len(frames) >= 2:
            median = sorted(frames)[len(frames) // 2]
            top = max(median * 1.8, median + 6)
            spike = max(2 * median, median + 8)
            step = gw * ss / (len(frames) - 1)
            points = [(i * step, (gh * ss - 2) - min(1.0, f / top) * (gh * ss - 3)) for i, f in enumerate(frames)]
            for i in range(1, len(points)):
                hot = frames[i] > spike or frames[i - 1] > spike
                colour = hex_rgb(WARN_YELLOW) + (255,) if hot else (255, 255, 255, 215)
                draw.line((points[i - 1], points[i]), fill=colour, width=round(1.4 * ss * self.k))
        layer.alpha_composite(big.resize((gw, gh), Image.Resampling.LANCZOS), (x0, y0))


class OverlayWindow:
    """The in-game strip as a layered window: real per-pixel transparency, never takes focus
    (WS_EX_NOACTIVATE), no taskbar button. Drag to move; right-click for options."""

    def __init__(self, app: "OptimizerApp"):
        self.app = app
        top = self.top = tk.Toplevel(app)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        self.visible = False
        self.menu_open = False
        self._grab = (0, 0)
        self._last = {}
        pos = app._setting("overlay_pos")
        self.x, self.y = (int(pos[0]), int(pos[1])) if isinstance(pos, list) and len(pos) == 2 else (app.px(20),
                                                                                                    app.px(20))
        # Keep it reachable if the monitor it was on is gone (virtual screen = all monitors together).
        vx, vy = _ov_api.GetSystemMetrics(76), _ov_api.GetSystemMetrics(77)
        vw, vh = _ov_api.GetSystemMetrics(78), _ov_api.GetSystemMetrics(79)
        if vw > 0 and vh > 0:
            self.x = min(max(self.x, vx), vx + vw - app.px(60))
            self.y = min(max(self.y, vy), vy + vh - app.px(24))
        self.renderer = OverlayRenderer(app.scale, self.style())
        top.geometry(f"10x10+{self.x}+{self.y}")
        top.update_idletasks()
        self.hwnd = _ov_api.GetParent(top.winfo_id()) or top.winfo_id()
        style = _ov_api.GetWindowLongW(self.hwnd, GWL_EXSTYLE_I)
        _ov_api.SetWindowLongW(self.hwnd, GWL_EXSTYLE_I,
                               style | WS_EX_LAYERED_ | WS_EX_TOPMOST_ | WS_EX_TOOLWINDOW_ | WS_EX_NOACTIVATE_)
        for seq, handler in (("<ButtonPress-1>", self._press), ("<B1-Motion>", self._drag),
                             ("<ButtonRelease-1>", self._release), ("<Button-3>", self._menu)):
            top.bind(seq, handler)

    def style(self) -> dict:
        saved = self.app._setting("overlay_style")
        return dict(OVERLAY_DEFAULTS, **(saved if isinstance(saved, dict) else {}))

    def set_style(self, key: str, value) -> None:
        style = self.style()
        style[key] = value
        self.app._set_setting("overlay_style", style)
        self.renderer = OverlayRenderer(self.app.scale, style)
        self.update(self._last)

    def _menu(self, event) -> None:
        style = self.style()
        menu = tk.Menu(self.top, tearoff=0)
        lock_var = tk.BooleanVar(self.top, bool(style["locked"]))
        menu.add_checkbutton(label="Lock position", variable=lock_var,
                             command=lambda: self.set_style("locked", lock_var.get()))
        menu.add_separator()
        bg_var = tk.StringVar(self.top, style["bg"])
        size_var = tk.StringVar(self.top, style["size"])
        for value, label in OVERLAY_BACKGROUNDS:
            menu.add_radiobutton(label=f"{label} background", value=value, variable=bg_var,
                                 command=lambda v=value: self.set_style("bg", v))
        menu.add_separator()
        for value, label in (("small", "Small"), ("large", "Large")):
            menu.add_radiobutton(label=label, value=value, variable=size_var,
                                 command=lambda v=value: self.set_style("size", v))
        menu.add_separator()
        self._vars = []
        for key, label in OVERLAY_ITEMS:
            var = tk.BooleanVar(self.top, bool(style[key]))
            self._vars.append(var)
            menu.add_checkbutton(label=label, variable=var, command=lambda k=key, v=var: self.set_style(k, v.get()))
        menu.add_separator()
        menu.add_command(label=(f"Cancel benchmark  ({self.app._hk('bench')})" if self.app.bench
                                else f"Benchmark {BENCH_SECONDS} s  ({self.app._hk('bench')})"),
                         command=self.app._toggle_benchmark)
        menu.add_command(label=f"Hide overlay  ({self.app._hk('overlay')})", command=lambda: self.app._toggle_overlay(False))
        # Windows only closes a popup menu on a click outside it while the menu's window is in the
        # foreground, and the overlay normally never is (it doesn't take focus). So it may take focus
        # just while the menu is open, then the game gets it back.
        previous = _ov_api.GetForegroundWindow()
        style_bits = _ov_api.GetWindowLongW(self.hwnd, GWL_EXSTYLE_I)
        _ov_api.SetWindowLongW(self.hwnd, GWL_EXSTYLE_I, style_bits & ~WS_EX_NOACTIVATE_)
        _ov_api.SetForegroundWindow(self.hwnd)
        self.menu_open, self._menu_widget = True, menu
        if _ov_api.GetForegroundWindow() == self.hwnd:
            self.top.after(150, self._watch_menu)  # backup: close it if focus goes anywhere else
        try:
            menu.tk_popup(event.x_root, event.y_root)  # returns once the menu has closed
        finally:
            self.menu_open = False
            menu.grab_release()
            _ov_api.SetWindowLongW(self.hwnd, GWL_EXSTYLE_I, style_bits)
            if previous and previous != self.hwnd:
                _ov_api.SetForegroundWindow(previous)

    def _watch_menu(self) -> None:
        """While the menu is open (Tk keeps running timers during it): if another window got focus,
        for example the game after a click outside, close the menu."""
        if not self.menu_open:
            return
        if _ov_api.GetForegroundWindow() not in (self.hwnd, None):
            _ov_api.EndMenu()
            return
        self.top.after(150, self._watch_menu)

    def _press(self, event) -> None:
        self._grab = (event.x_root - self.x, event.y_root - self.y)

    def _drag(self, event) -> None:
        if self.renderer.style["locked"]:
            return
        self.x, self.y = event.x_root - self._grab[0], event.y_root - self._grab[1]
        self._push(self._image)

    def _release(self, _event) -> None:
        if not self.renderer.style["locked"]:
            self.app._set_setting("overlay_pos", [self.x, self.y])  # reopens here, even after a restart

    def _push(self, image: Image.Image) -> None:
        self._image = image
        w, h = image.size
        self.top.geometry(f"{w}x{h}+{self.x}+{self.y}")
        push_layered(self.hwnd, image, self.x, self.y)

    def show(self) -> None:
        self.top.deiconify()
        self.visible = True
        self.update(self._last)

    def hide(self) -> None:
        self.top.withdraw()
        self.visible = False

    def update(self, data: dict) -> None:
        self._last = data or {}
        if self.visible:
            self._push(self.renderer.render(self._last))

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class _HostValue:
    """get()/set() for the custom-server text, like the StringVar it replaced."""

    def __init__(self, app: "OptimizerApp"):
        self.app = app

    def get(self) -> str:
        return self.app.host_value

    def set(self, value: str) -> None:
        self.app.host_value = str(value)
        self.app.host_cursor = len(self.app.host_value)
        self.app._paint_host()


class OptimizerApp(ctk.CTk):
    def __init__(self, admin: bool, state_store: StateStore | None = None, start_hidden: bool = False,
                 background_services: bool = True, instance_event=None):
        ctk.set_appearance_mode("dark")
        # customtkinter re-checks the Windows light/dark theme every 30 ms, even though Astra is always
        # dark; that timer alone was about half the app's idle CPU. Once a minute is plenty.
        ctk.AppearanceModeTracker.update_loop_interval = 60_000
        super().__init__(fg_color=VOID)
        self.admin = admin
        self.state_store = state_store or StateStore()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self._log_lock = threading.Lock()

        self.title(f"{APP_NAME} {DISPLAY_VERSION}")
        self.resizable(False, False)
        try:
            self.scale = float(ctk.ScalingTracker.get_window_scaling(self))
        except Exception:
            self.scale = 1.0
        self._place_window(WINDOW_W, WINDOW_H)
        # Pin the size: customtkinter re-applies the 0x0 size Windows reports while minimized
        # (Win+D, minimize button) on restore, collapsing the window to its title bar.
        self.minsize(WINDOW_W, WINDOW_H)
        self.maxsize(WINDOW_W, WINDOW_H)
        self.bind("<Map>", self._on_map, add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # No native title bar: the app draws its own strip (drag, minimize, close).
        self.overrideredirect(True)
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._closing = False

        self.painter = GalaxyPainter(self.scale)
        self.px = self.painter.px
        self._init_fonts()
        self.look = self._load_look()
        self.painter.glass = self.look["glass"] / 100
        self._bg_cache: dict = {}
        self._bg_version = 0
        self.update_state = "idle"  # idle | checking | current | available | downloading | ready | failed
        self.update_info: dict | None = None
        self.update_progress = 0
        self.update_file: str | None = None
        self.report_busy = False
        self._crash_dialog_shown = False
        self.report_callback_exception = self._on_tk_error
        threading.excepthook = self._on_thread_error
        self._update_job = None
        self._backdrop_job = None
        self._slider_drag: str | None = None
        self._fade: dict | None = None
        self._overlay: dict | None = None
        self._frost_cache: dict = {}
        self.tool_page = "checkup"
        self.settings_page = "app"  # app | windows
        self.checkup_state = "idle"  # idle | running
        self.checkup: dict[str, dict] = {}
        self.checkup_at = 0.0
        self.check_offset = 0
        self.gpu_setup: dict | None = None
        self.sessions_live: dict[str, dict] = {}
        self.session_view: str | None = None  # a game's path while its session history is shown
        self.compare_pick: list[int] | None = None  # picking sessions to compare (indices, newest first)
        self.compare_pair: list[dict] | None = None  # the two sessions being compared, older first
        self.privacy_state: dict[str, bool] = {}
        self._privacy_busy = False
        self._privacy_defaults: list = []
        self.session_pinger: SessionPinger | None = None
        self.games: list[dict] = [dict(g) for g in (self._setting("games") or []) if isinstance(g, dict) and g.get("path")]
        self.game_offset = 0
        self.game_watcher: GameWatcher | None = None
        self._game_auto: str | None = None
        GAME_EXE_PATHS.clear()
        GAME_EXE_PATHS.update(os.path.normcase(g["path"]) for g in self.games)
        self.startup_items: list[dict] | None = None
        self.startup_offset = 0
        self.startup_note: tuple[str, str] | None = None
        self.clean_state = "idle"           # idle | scanning | cleaning
        self.clean_scan: dict | None = None  # key -> (files, bytes)
        self.clean_scanned_at = 0.0
        self.clean_last: tuple[int, int, int] | None = None
        self._clean_auto = False
        self.game_finder: dict | None = None  # {"state": "searching"|"ready", "items": [...]}
        self.dns_info: dict | None = None
        self.dns_state = "idle"               # idle | loading | testing | switching
        self.dns_rows = [{"key": "current", "name": "Your current DNS", "servers": []}] + [
            {"key": key, "name": name, "servers": servers} for key, name, servers in DNS_PROVIDERS]
        for row in self.dns_rows:
            row.update(median=None, answered=None, total=None, status="idle")
        self._dns_run = 0
        self._dns_pending = 0
        self._dns_note: tuple[str, str] | None = None
        self.history = {key: deque(maxlen=HISTORY_LEN) for key in MONITOR_SPECS}
        self.protection: bool | None = None   # System Protection on the Windows drive (None = unknown)
        self._protect_pending = False
        self._slider_frac = 0.0
        self._slider_photos: dict = {}

        applied = self.state_store.get("applied_profile")
        self.profile = applied if applied in PROFILE_ORDER and self.state_store.snapshot_count() else "max"
        self._busy_label = "OPTIMIZING…"
        self._pending_applied: str | None = None
        self.hover: str | None = None
        self.pressed: str | None = None
        self.dialog: dict | None = None
        self.card_t = {key: 0.0 for key in PROFILE_ORDER}
        self.bloom_t = 0.0
        self.shimmer = 0.0
        self.undo_count = self.state_store.snapshot_count()
        self._cache: dict = {}
        self._anim_job = None
        self._motion_checked = 0.0
        self._motion = True
        self._level = "full"
        self.seg_t: dict[str, float] = {}
        self.tab = "boost"
        self.tray: TrayIcon | None = None
        self.hotkey: HotkeyThread | None = None
        self.overlay_hotkey: HotkeyThread | None = None
        self.bench_hotkey: HotkeyThread | None = None
        self.bench: dict | None = None  # the benchmark in progress
        self._bench_result: tuple[str, float] | None = None  # (overlay text, monotonic time it goes)
        self.overlay: OverlayWindow | None = None
        self._overlay_auto = False   # shown because a game started (hidden again when it ends)
        self._last_tel: dict = {}
        self._last_ping: float | None = None
        self._start_hidden = start_hidden
        self._job_origin: str | None = None
        self._last_summary: tuple[str, list[str]] | None = None
        self._autostart_pending = False
        self._autostart_desired = False
        self.ping_running = False
        self._ping_run_id = 0
        self._ping_pending = 0
        self._ping_stop = threading.Event()
        self.ping_rows = self._make_ping_rows()

        self.canvas = tk.Canvas(self, width=self.px(WINDOW_W), height=self.px(WINDOW_H), bg=VOID,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._build_backdrop()
        self._scene = self.backdrops[self.tab].copy()
        self.scene_photo = ImageTk.PhotoImage(self._scene.convert("RGB"))
        self.canvas.create_image(0, 0, anchor="nw", image=self.scene_photo)
        self._build_text()
        self._refresh_privacy()
        cx, cy, cw, ch = L_CONSOLE
        inset = 6
        self.console = ConsoleView(self, self.px(cx + inset), self.px(cy + inset),
                                   self.px(cw - 2 * inset), self.px(ch - 2 * inset))
        self._build_host_entry()
        self._update_console_backdrop()

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_canvas_wheel)
        self.bind("<Left>", lambda _e: self._on_arrow(-1))
        self.bind("<Right>", lambda _e: self._on_arrow(1))
        for index, key in enumerate(PROFILE_ORDER, start=1):
            self.bind(str(index), lambda _e, k=key: None if self._typing() else self._select_profile(k))
        self.bind("<Control-Tab>", lambda _e: self._cycle_tab(1))
        self.bind("<Control-Shift-Tab>", lambda _e: self._cycle_tab(-1))
        self.bind("<Return>", self._on_return)
        self.bind("<space>", self._on_space)
        self.bind("<Escape>", self._on_escape)
        self.bind("<Alt-F4>", lambda _e: self._on_close())

        self._snap_animations()
        self._apply_visibility()
        self._render()
        self._set_window_icon()
        self.intro: dict | None = None
        self._intro_pending = self._intro_wanted()
        if self._intro_pending:
            # Never shown until the intro covers it. (No transparency tricks on the main window: shells
            # and window managers such as Seelen UI track new windows and can mishandle invisible ones.)
            self.withdraw()
        self.after(10, self._apply_borderless)

        self.telemetry = TelemetryThread(self.events)
        self.telemetry.start()
        self.after(80, self._pump_events)
        if background_services:
            self._start_background_services(instance_event)

        self._emit("head", f"{APP_NAME} {DISPLAY_VERSION} ready. Pick a mode, then press OPTIMIZE NOW.")
        if self.admin:
            self._emit("ok", "Running with administrator rights")
        else:
            self._emit("warn", "Monitor only: restart Astra Optimizer and accept the UAC prompt to apply changes")
        if self.undo_count:
            self._emit("info", f"{self.undo_count} change(s) are applied - press UNDO CHANGES to revert them")

    # ---------------- setup ----------------

    def _place_window(self, width: int, height: int) -> None:
        x = max(0, int((self.winfo_screenwidth() - width * self.scale) / 2))
        y = max(0, int((self.winfo_screenheight() - height * self.scale) / 2.4))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _hwnd(self) -> int:
        return int(self.wm_frame(), 16)

    def _set_window_icon(self) -> None:
        """Taskbar / Alt+Tab icon (Tk would otherwise show its feather)."""
        # customtkinter swaps in its own icon ~200 ms after start unless iconbitmap was called.
        self._iconbitmap_method_called = True
        try:
            ico = resource_path("assets", "icon.ico")
            if os.path.isfile(ico):
                tk.Tk.wm_iconbitmap(self, default=ico)
            self._icon_photos = [ImageTk.PhotoImage(self.painter.app_icon(size)) for size in (16, 32, 48, 64)]
            tk.Tk.iconphoto(self, True, *self._icon_photos)
        except tk.TclError:
            pass

    def _apply_borderless(self) -> None:
        """Keep a frameless window behaving like a normal app: taskbar button, Alt+Tab,
        minimize from the taskbar, and Windows 11 rounded corners with a subtle border."""
        try:
            hwnd = self._hwnd()
            ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX | WS_SYSMENU)
            # The frameless restyle leaves the window without an icon of its own, and the taskbar reads
            # that first, so give it ours directly (Tk's iconbitmap doesn't reach it here).
            self._hicons = set_window_icons(hwnd, resource_path("assets", "icon.ico"), self.scale)
            if dwmapi is not None:
                corner = ctypes.c_int(DWMWCP_ROUND)
                dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), 4)
                border = wintypes.DWORD(0x00333333)  # COLORREF 0x00BBGGRR
                dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(border), 4)
        except Exception:
            pass
        # Re-show so the shell picks up the new taskbar/Alt+Tab style.
        self.withdraw()
        if self._start_hidden and self.tray is not None and self.tray.running:
            return  # launched at sign-in: stay in the tray until opened
        self.after(20, self._reveal)
        if self._start_hidden:
            self.after(120, self._minimize)

    def _reveal(self) -> None:
        if self._intro_pending:
            self._intro_pending = False
            self._start_intro()  # puts the cover up, then shows the window underneath it
            return
        self._show_main()

    def _show_main(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        self._ensure_window_size()

    # ---------------- startup intro ----------------

    def _intro_wanted(self) -> bool:
        return bool(self.look.get("intro", True)) and not self._start_hidden and self._motion_level() != "off"

    def _start_intro(self) -> None:
        """Cover the window with a borderless twin and play the intro on it. The real UI is ready
        underneath; the last frames crossfade into a snapshot of it, so the handoff is invisible."""
        px = self.px
        shown = False
        try:
            self.update_idletasks()
            level = self._motion_level()
            if level == "off":
                raise RuntimeError("motion is off")
            size = (px(WINDOW_W), px(WINDOW_H))
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            win.configure(bg=VOID, cursor="")
            label = tk.Label(win, bd=0, highlightthickness=0, bg=VOID)
            label.place(x=0, y=0)
            sky = self._background_layer()
            if sky.size != size:
                sky = sky.resize(size, Image.Resampling.LANCZOS)
            black = Image.new("RGBA", size, rgba(VOID))
            photo = ImageTk.PhotoImage(black.convert("RGB"))
            label.configure(image=photo)
            win.update_idletasks()
            hwnd = int(win.wm_frame(), 16)
            ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            # Never activated: the main window stays the foreground window throughout, so closing the
            # cover can't hand activation to some other app and leave Astra behind it.
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, self._hwnd())  # owned: stays above, hides with it
            match = re.match(r"\d+x\d+([+-]-?\d+)([+-]-?\d+)", tk.Tk.wm_geometry(self))  # where it will open
            x, y = ((int(match.group(1).replace("+", "")), int(match.group(2).replace("+", ""))) if match
                    else (self.winfo_rootx(), self.winfo_rooty()))
            user32.SetWindowPos(hwnd, None, x, y, size[0], size[1], SWP_SHOWWINDOW | SWP_NOACTIVATE)
            if dwmapi is not None:
                corner = ctypes.c_int(DWMWCP_ROUND)
                dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), 4)
            label.update()
            # Now the real window, a perfectly ordinary one. The cover is owned by it, and Windows keeps
            # owned windows above their owner, so the UI never flashes before the intro.
            self._show_main()
            shown = True
            if not self._can_fade():
                raise RuntimeError("window not showing")
            user32.SetWindowPos(hwnd, None, self.winfo_rootx(), self.winfo_rooty(), size[0], size[1],
                                SWP_NOACTIVATE | 0x0004)  # SWP_NOZORDER: line up exactly with the real window
            win.bind("<ButtonPress>", lambda _e: self._skip_intro())
            # Keys go to the main window; a sink with no other bindings takes them for now, so a key
            # that skips the intro can't also press something in the UI underneath.
            sink = tk.Frame(self, width=1, height=1, bg=VOID, takefocus=0)
            sink.place(x=0, y=0)
            sink.bindtags((str(sink),))
            sink.bind("<KeyPress>", lambda _e: self._skip_intro())
            sink.update_idletasks()  # mapped first, or Tk only remembers the focus for later
            sink.focus_set()
            mark = INTRO_MARK[2]
            self.intro = {
                "win": win, "label": label, "hwnd": hwnd, "photo": photo, "level": level, "sink": sink,
                "start": time.monotonic(), "length": INTRO_SECONDS[level], "sky": sky, "black": black,
                "snapshot": None, "word": {}, "leaving": None,
                "glow": self.painter.intro_glow(px(mark)),
            }
        except Exception as exc:
            try:
                win.destroy()  # may not exist yet
            except Exception:
                pass
            self.intro = None
            if not shown:
                self._show_main()
            if str(exc) not in ("window not showing", "motion is off"):
                self._write_log_file("WARN", f"Startup intro skipped: {exc!r}")
            return
        self._intro_step()

    def _intro_frame(self, t: float) -> Image.Image:
        """One frame of the intro at t intro seconds (see INTRO_PACE)."""
        intro, px, painter = self.intro, self.px, self.painter
        full = intro["level"] == "full"
        cx, cy, mark = INTRO_MARK
        word_y, word_size = INTRO_WORD
        gx, gy, gs = L_BRAND_GLYPH

        def span(a: float, b: float) -> float:
            return 0.0 if t <= a else 1.0 if t >= b else (t - a) / (b - a)

        def ease_out(v: float) -> float:
            return 1 - (1 - v) ** 3

        def ease_in_out(v: float) -> float:
            return 4 * v ** 3 if v < 0.5 else 1 - (-2 * v + 2) ** 3 / 2

        def faded(img: Image.Image, alpha: float) -> Image.Image:
            if alpha >= 0.999:
                return img
            img = img.copy()
            img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
            return img

        if full:
            dawn = ease_out(span(0.0, 0.32))
            arm_t = span(0.10, 0.62)
            arm = 1 + 2.4 * (arm_t - 1) ** 3 + 1.4 * (arm_t - 1) ** 2 if arm_t > 0 else 0.0  # ease-out-back
            sweep = ease_in_out(span(0.38, 1.08))
            word_in = ease_out(span(0.78, 1.28))
            fly = ease_in_out(span(1.65, 2.05))
            reveal = ease_in_out(span(1.67, 2.03))
            vanish = span(1.91, 2.09)  # gone before the end, so nothing shows double over the real UI
            glow = (ease_out(span(0.10, 0.45)) * (1 - 0.55 * span(0.45, 1.0))) * (1 - span(1.67, 1.91))
        else:  # reduced: no movement, the finished mark simply fades in and hands over
            dawn = word_in = ease_out(span(0.0, 0.30))
            arm, sweep = 1.0, 1.0
            fly, vanish = 0.0, 0.0
            reveal = ease_in_out(span(0.55, 0.95))
            glow = 0.45 * dawn * (1 - reveal)

        frame = intro["sky"] if dawn >= 1 else Image.blend(intro["black"], intro["sky"], dawn)
        if reveal > 0 and intro["snapshot"] is not None:
            frame = Image.blend(frame, intro["snapshot"], reveal)
        elif frame is intro["sky"]:
            frame = frame.copy()
        keep = 1.0 - vanish if full else (1.0 - reveal) * dawn  # how visible the drawn pieces are

        # Mark: from the centre into the title-strip glyph.
        mx = cx + (gx + gs / 2 - cx) * fly
        my = cy + (gy + gs / 2 - cy) * fly
        msize = mark + (gs - mark) * fly
        if glow > 0.01:
            img = intro["glow"]
            g = max(2, int(img.width * msize / mark))
            img = faded(img if g == img.width else img.resize((g, g), Image.Resampling.BILINEAR), glow * keep)
            paste_layer(frame, img, px(mx) - g // 2, px(my) - g // 2)
        if keep > 0.01 and (arm > 0 or sweep > 0):
            s = max(4, px(msize))
            img = faded(painter.intro_mark(s, arm, sweep), keep)
            paste_layer(frame, img, px(mx) - s // 2, px(my) - s // 2)

        # Wordmark: letter-spacing closes as it appears, then it shrinks onto the title text.
        if word_in > 0.01 and keep > 0.01:
            tracking = round(0.42 * (1 - word_in), 3)
            cache = intro["word"]
            img = cache.get(tracking)
            if img is None:
                if len(cache) > 80:
                    cache.clear()
                img = cache[tracking] = painter.intro_word(APP_NAME, px(word_size), tracking)
            if "word_end" not in intro:  # scale that makes it exactly as wide as the title-strip name
                plain = cache.get(0.0) or painter.intro_word(APP_NAME, px(word_size), 0.0)
                intro["word_end"] = self.f_brand.measure(APP_NAME) / max(1, plain.width - 2)
            k = 1 + (intro["word_end"] - 1) * fly
            if k < 0.999:
                img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.Resampling.LANCZOS)
            left0 = (WINDOW_W - img.width / self.scale / k) / 2  # centred at full size
            left = left0 + (42 - left0) * fly
            centre = word_y + (18 - word_y) * fly
            img = faded(img, word_in * keep)
            paste_layer(frame, img, px(left) - 1, px(centre) - img.height // 2)
        return frame

    def _intro_step(self) -> None:
        intro = self.intro
        if intro is None:
            return
        if self._closing:
            self._end_intro()
            return
        try:
            t = (time.monotonic() - intro["start"]) / INTRO_PACE[intro["level"]]
            leaving = intro["leaving"]
            if leaving is not None:  # skipped: fade the cover away quickly
                k = (time.monotonic() - leaving) / 0.16
                if k >= 1:
                    self._end_intro()
                    return
                intro["win"].attributes("-alpha", (1 - k) ** 1.6)
                self.after(16, self._intro_step)
                return
            handoff = INTRO_HANDOFF[intro["level"]]
            if intro["snapshot"] is None and t >= handoff - 0.05:
                snap = capture_window(self._hwnd())
                if snap is None or snap.size != intro["sky"].size:
                    intro["snapshot"] = False  # can't see the UI: fade the cover instead
                else:
                    intro["snapshot"] = snap.convert("RGBA")
            if t >= intro["length"]:
                self._end_intro()
                return
            if intro["snapshot"] is False and t >= handoff:
                intro["leaving"] = time.monotonic()
                self.after(16, self._intro_step)
                return
            frame = self._intro_frame(t)
            intro["photo"].paste(frame.convert("RGB"))
            self.after(12, self._intro_step)
        except Exception as exc:
            self._write_log_file("WARN", f"Startup intro skipped: {exc!r}")
            self._end_intro()

    def _skip_intro(self) -> None:
        if self.intro is not None and self.intro["leaving"] is None:
            self.intro["leaving"] = time.monotonic()

    def _end_intro(self) -> None:
        intro, self.intro = self.intro, None
        if intro is not None:
            for key in ("sink", "win"):
                try:
                    intro[key].destroy()
                except tk.TclError:
                    pass
            if not self._closing:
                self._ensure_in_front()

    def _ensure_in_front(self) -> None:
        """After the intro: make sure the window is up front. If Windows gave the foreground to another
        app meanwhile, a brief topmost flip still raises it into view (that is allowed when activation isn't)."""
        try:
            self.focus_set()
            hwnd = self._hwnd()
            front = user32.GetForegroundWindow()
            if front == hwnd or self.state() != "normal":
                return
            name = ctypes.create_unicode_buffer(96)
            user32.GetClassNameW(front, name, 96)
            self._write_log_file("INFO", f"After the intro another window was in front ({name.value or 'none'}); raised Astra")
            self.attributes("-topmost", True)
            self.lift()
            self.focus_force()
            self.after(60, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _minimize(self) -> None:
        # Tk refuses iconify() on frameless windows, so ask Windows directly.
        user32.ShowWindow(self._hwnd(), SW_MINIMIZE)

    def _on_map(self, event) -> None:
        if event.widget is self:
            self.after(30, self._ensure_window_size)

    def _ensure_window_size(self) -> None:
        """Safety net: if a restore ever leaves the window the wrong size, put it back."""
        try:
            if self.state() != "normal":
                return
            if self.winfo_width() != self.px(WINDOW_W) or self.winfo_height() != self.px(WINDOW_H):
                self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        except tk.TclError:
            pass

    def _init_fonts(self) -> None:
        families = set(tkfont.families(self))

        def pick(*names: str) -> str:
            for name in names:
                if name in families:
                    return name
            return "Segoe UI"

        display = pick("Bahnschrift SemiBold", "Segoe UI Semibold")
        regular = pick("Bahnschrift", "Segoe UI")
        condensed = pick("Bahnschrift SemiCondensed", "Bahnschrift", "Segoe UI")
        mono = pick("Cascadia Mono", "Consolas")
        mono_bold = pick("Cascadia Mono SemiBold", mono)

        def font(family: str, size: float) -> tkfont.Font:
            return tkfont.Font(self, family=family, size=-max(8, self.px(size)))

        self.f_brand = font(display, 16)
        self.f_status = font(regular, 11)
        self.f_label = font(condensed, 11)
        self.f_value = font(display, 18)
        self.f_plan = font(display, 13)
        self.f_title = font(display, 15)
        self.f_purpose = font(regular, 11)
        self.f_desc = font(regular, 12)
        self.f_primary = font(display, 15)
        self.f_ghost = font(display, 12)
        self.f_ghost_small = font(display, 11)
        self.f_mono = font(mono, 11)
        self.f_mono_b = font(mono_bold, 11)
        self.f_dialog_title = font(display, 18)
        self.f_dialog_body = font(regular, 13)
        self.f_hero = font(display, 26)
        self.f_name = font(display, 21)

    def _hairline(self, img: Image.Image, x: float, y: float, w: float, h: float, alpha: int) -> None:
        """Composited 1px line (ImageDraw would overwrite the alpha instead of blending)."""
        line = Image.new("RGBA", (max(1, self.px(w)), max(1, self.px(h))), (255, 255, 255, alpha))
        paste_layer(img, line, self.px(x), self.px(y))

    def _build_backdrop(self) -> None:
        """One static backdrop per tab; dynamic parts are composited on top in _render."""
        px = self.px
        glass = self.painter.glass
        stand = self.look["stand_out"] / 100
        base = self._background_layer()
        # Frost (stored as "stand_out"): what shows through a panel is blurred, and the panel gets a
        # soft drop shadow and a brighter edge. It never darkens - darkness is Panel opacity's job.
        frosted = None
        if stand > 0:
            key = (self._bg_cache.get("key"), tuple(self._bg_adjust().values()), stand)
            if self._frost_cache.get("key") != key:
                self._frost_cache = {"key": key, "image": base.filter(ImageFilter.GaussianBlur(px(3 + 15 * stand)))}
            frosted = self._frost_cache["image"]
        edge = int(18 + 85 * stand)

        def bed(img, rect, radius):
            if frosted is None:
                return
            x, y, w, h = rect
            m = px(48)  # shadow blur reaches ~3 x 12 px past the panel + 5 px offset
            size = (px(w) + 2 * m, px(h) + 2 * m)
            shadow = self.painter.glow_layer(size, (m, m + px(5), m + px(w), m + px(h) + px(5)), px(radius),
                                             VOID, 150 * stand, px(12))
            paste_layer(img, shadow, px(x) - m, px(y) - m)
            region = frosted.crop((px(x), px(y), px(x) + px(w), px(y) + px(h)))
            img.paste(region, (px(x), px(y)), self.painter.round_mask(px(w), px(h), px(radius)))

        gx, gy, gs = L_BRAND_GLYPH
        paste_layer(base, self.painter.icon("astra", px(gs), STARLIGHT), px(gx), px(gy))
        bed(base, L_RAIL_BOX, 16)
        rx, ry, rw, rh = L_RAIL_BOX
        rail_alpha = 255 * max(0.5, glass)
        rail = self.painter.shape(px(rw), px(rh), px(16),
                                  grad=(rgba(mix(SURFACE, STARLIGHT, 0.035), rail_alpha), rgba(SURFACE, rail_alpha), False),
                                  border=(255, 255, 255, edge), border_width=1.0)
        paste_layer(base, rail, px(rx), px(ry))

        def surface(rect, radius=14, fill=rgba(CONSOLE_CORE), border=(255, 255, 255, 15)):
            x, y, w, h = rect
            return self.painter.shape(px(w), px(h), px(radius), fill=fill, border=border, border_width=1.0), x, y

        boost = base.copy()
        for x, y, w, h in L_PILLS.values():
            bed(boost, (x, y, w, h), h / 2)
            img, m = self.painter.pill(px(w), px(h), STARLIGHT)
            paste_layer(boost, img, px(x) - m, px(y) - m)
        for rect in L_CARDS.values():
            bed(boost, rect, 16)
        bed(boost, L_CONSOLE, 14)
        img, x, y = surface(L_CONSOLE, fill=rgba(CONSOLE_CORE, 255 * max(0.55, glass)),
                            border=(255, 255, 255, edge))
        paste_layer(boost, img, px(x), px(y))

        def table(img, rect, row_y0, row_h, rows_count):
            bed(img, rect, 14)
            panel, x, y = surface(rect, fill=rgba(CONSOLE_CORE, 235 * max(0.7, glass)), border=(255, 255, 255, edge))
            paste_layer(img, panel, px(x), px(y))
            tx, _ty, tw, _th = rect
            for i in range(rows_count):
                self._hairline(img, tx + 12, row_y0 + i * row_h - 2, tw - 24, 1 / self.scale, 26 if i == 0 else 12)

        tools = base.copy()
        table(tools, L_PING_TABLE, L_PING_ROW_Y0, L_PING_ROW_H, len(self.ping_rows))
        bed(tools, L_PING_HOST_FIELD, 12)
        img, x, y = surface(L_PING_HOST_FIELD, radius=12, fill=rgba(CONSOLE_CORE, 235 * max(0.7, glass)),
                            border=(255, 255, 255, max(34, edge)))
        paste_layer(tools, img, px(x), px(y))
        games = base.copy()
        table(games, L_TABLE, L_GAME_ROW_Y0, L_GAME_ROW_H, GAME_ROWS)
        startup = base.copy()
        table(startup, L_TABLE, L_START_ROW_Y0, L_START_ROW_H, START_ROWS)
        dns = base.copy()
        table(dns, L_DNS_TABLE, L_DNS_ROW_Y0, L_DNS_ROW_H, DNS_ROWS)

        def rows(img, rects):
            for x, y, w, h in rects:
                bed(img, (x, y, w, h), 14)
                row = self.painter.shape(px(w), px(h), px(14),
                                         grad=(rgba(mix(SURFACE, STARLIGHT, 0.035), 255 * glass),
                                               rgba(SURFACE, 255 * glass), False),
                                         border=(255, 255, 255, edge), border_width=1.0)
                paste_layer(img, row, px(x), px(y))

        settings = base.copy()
        rows(settings, [*L_SETTING_ROWS.values(), L_FOLDER_ROW])
        settings_windows = base.copy()
        rows(settings_windows, L_WINSET_ROWS.values())
        privacy = base.copy()
        rows(privacy, [*L_PRIV_ROWS.values(), L_PRIV_FOOT])
        appearance = base.copy()
        rows(appearance, L_LOOK_ROWS.values())
        ix, iy, iw, _ih = L_LOOK_INTRO
        self._hairline(appearance, ix + 20, iy, iw - 40, 1 / self.scale, 16)
        rows(games, [L_GAME_AUTO])
        cleanup = base.copy()
        rows(cleanup, [*L_CLEAN_ROWS.values(), L_CLEAN_BAR])
        checkup = base.copy()
        rows(checkup, L_CHECK_SLOTS)
        rows(dns, [L_DNS_BAR])
        monitor = base.copy()
        rows(monitor, L_MONITOR.values())
        for rect in L_MONITOR.values():
            cx, cy, cw, ch = self._chart_rect(rect)
            for frac in (0.0, 0.5, 1.0):
                self._hairline(monitor, cx, cy + ch * frac, cw, 1 / self.scale, 16 if frac in (0.0, 1.0) else 9)

        self.backdrops = {"boost": boost, "tools:checkup": checkup, "tools:ping": tools,"tools:games": games, "tools:startup": startup,
                          "tools:cleanup": cleanup, "tools:dns": dns, "tools:monitor": monitor,
                          "settings:appearance": appearance, "settings:app": settings, "settings:windows": settings_windows,
                          "privacy": privacy, "about": self._about_backdrop(base, rows)}
        for img in self.backdrops.values():
            # Hairline window edge (the native frame is gone).
            self._hairline(img, 0, 0, WINDOW_W, 1 / self.scale, 22)
            self._hairline(img, 0, WINDOW_H - 1 / self.scale, WINDOW_W, 1 / self.scale, 22)
            self._hairline(img, 0, 0, 1 / self.scale, WINDOW_H, 22)
            self._hairline(img, WINDOW_W - 1 / self.scale, 0, 1 / self.scale, WINDOW_H, 22)
        if getattr(self, "console", None) is not None:
            self._update_console_backdrop()

    def _about_backdrop(self, base: Image.Image, rows) -> Image.Image:
        px = self.px
        about = base.copy()
        rows(about, L_ABOUT_ROWS.values())
        x, y, size = L_ABOUT_ICON
        glow = self.painter.glow_layer((px(size) + px(60), px(size) + px(60)),
                                       (px(30), px(30), px(30) + px(size), px(30) + px(size)), px(size * 0.24),
                                       STARLIGHT, 40, px(12))
        paste_layer(about, glow, px(x) - px(30), px(y) - px(30))
        paste_layer(about, self.painter.app_icon(px(size)), px(x), px(y))
        ax, ay, asize = L_ABOUT_AVATAR
        paste_layer(about, self.painter.avatar(px(asize), resource_path("assets", "avatar.png")), px(ax), px(ay))
        return about

    def _update_console_backdrop(self) -> None:
        cx, cy, cw, ch = L_CONSOLE
        inset = 6
        x0, y0 = self.px(cx + inset), self.px(cy + inset)
        self.console.set_backdrop(self.backdrops["boost"].crop((x0, y0, x0 + self.console.w, y0 + self.console.h)))

    # ---------------- background (Appearance) ----------------

    def _keep_clear(self) -> list:
        return [(0, 0, WINDOW_W, L_TITLE_H), L_RAIL_BOX, *L_ABOUT_ROWS.values(), *L_PILLS.values(), *L_CARDS.values(),
                *L_BUTTONS.values(), L_CONSOLE, *L_TOOL_BUTTONS.values(), L_PING_TABLE, L_PING_HOST_FIELD,
                L_SEGMENTS["tool"], L_GAME_AUTO, *L_CLEAN_ROWS.values(), L_CLEAN_BAR, L_GAME_FIND, L_GAME_ADD, L_DNS_BAR,
                *L_CHECK_SLOTS,
                *L_MONITOR.values(),
                *L_SETTING_ROWS.values(), L_FOLDER_ROW, *L_WINSET_ROWS.values(), L_SEGMENTS["set"],
                *L_LOOK_ROWS.values(), L_LOOK_RESET,
                *L_PRIV_ROWS.values(), L_PRIV_FOOT]

    def _bg_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(self.state_store.path)), "background.jpg")

    def _bg_adjust(self) -> dict:
        kind = self.look["background"]
        return self.look["adjust"].get(kind, {"blur": 0, "dim": 0}) if kind != "black" else {"blur": 0, "dim": 0}

    def _background_layer(self) -> Image.Image:
        """Source (starfield / black / image) -> cached blur -> dim. Returns a fresh RGBA copy."""
        px = self.px
        width, height = px(WINDOW_W), px(WINDOW_H)
        cache = self._bg_cache
        kind = self.look["background"]
        if cache.get("key") != (kind, self._bg_version):
            image = None
            if kind == "image":
                try:
                    with Image.open(self._bg_path()) as source:
                        image = ImageOps.fit(source.convert("RGB"), (width, height),
                                             Image.Resampling.LANCZOS).convert("RGBA")
                except Exception:
                    self._emit("warn", "Background image is missing or unreadable - showing the starfield instead")
                    kind = self.look["background"] = "stars"
            if kind == "black":
                image = Image.new("RGBA", (width, height), rgba(VOID))
            elif image is None:
                image = self.painter.background(width, height, keep_clear=self._keep_clear())
            cache.clear()
            cache.update(key=(kind, self._bg_version), image=image, blurred={})
        adjust = self._bg_adjust()
        blur = int(adjust["blur"])
        layer = cache["blurred"].get(blur)
        if layer is None:
            layer = cache["image"] if blur <= 0 else cache["image"].filter(ImageFilter.GaussianBlur(px(blur)))
            if len(cache["blurred"]) > 8:
                cache["blurred"].clear()
            cache["blurred"][blur] = layer
        layer = layer.copy()
        if adjust["dim"]:
            layer.alpha_composite(Image.new("RGBA", layer.size, (0, 0, 0, int(255 * adjust["dim"] / 100))))
        return layer

    def _load_look(self) -> dict:
        look = json.loads(json.dumps(DEFAULT_LOOK))
        saved = (self.state_store.get("settings") or {}).get("appearance")
        if isinstance(saved, dict):
            for key in ("background", "bg_name", "glass", "stand_out", "motion"):
                if key in saved:
                    look[key] = saved[key]
            if isinstance(saved.get("intro"), bool):
                look["intro"] = saved["intro"]
            if "stand_out" not in saved and saved.get("glass") == 100:
                look["glass"] = DEFAULT_LOOK["glass"]  # pre-1.8 solid panels -> the new glass default
            for kind, values in (saved.get("adjust") or {}).items():
                if kind in look["adjust"] and isinstance(values, dict):
                    for name in ("blur", "dim"):
                        if isinstance(values.get(name), (int, float)):
                            look["adjust"][kind][name] = int(values[name])
        if look["background"] not in dict(BG_KINDS):
            look["background"] = "stars"
        if look["motion"] not in dict(MOTION_MODES):
            look["motion"] = "system"
        for key in GLOBAL_SLIDERS:
            lo, hi = SLIDER_SPECS[key][1:]
            value = look[key] if isinstance(look[key], (int, float)) else DEFAULT_LOOK[key]
            look[key] = int(min(hi, max(lo, value)))
        for kind in look["adjust"]:
            for name in ("blur", "dim"):
                lo, hi = SLIDER_SPECS[name][1:]
                look["adjust"][kind][name] = int(min(hi, max(lo, look["adjust"][kind][name])))
        return look

    def _save_look(self) -> None:
        self._set_setting("appearance", json.loads(json.dumps(self.look)))

    def _apply_look(self, fade: bool = False) -> None:
        """Rebuild every backdrop for the current look and repaint (optionally crossfaded)."""
        def change() -> None:
            self.painter.glass = self.look["glass"] / 100
            self._cache.clear()
            self._build_backdrop()
            self._refresh_look_texts()
            self._apply_visibility()
            self._render()

        self._transition(change) if fade else change()

    def _schedule_backdrop(self) -> None:
        """Rebuild once the pointer has rested for a moment (debounced)."""
        if self._backdrop_job is not None:
            self.after_cancel(self._backdrop_job)
        self._backdrop_job = self.after(110, self._run_backdrop_job)

    def _flush_backdrop(self) -> None:
        if self._backdrop_job is not None:
            self.after_cancel(self._backdrop_job)
            self._run_backdrop_job()

    def _run_backdrop_job(self) -> None:
        self._backdrop_job = None
        self._apply_look()

    def _import_background(self, path: str) -> bool:
        """Copy the picked image (cropped to the window's shape) next to the app's state."""
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, (WINDOW_W * 2, WINDOW_H * 2), Image.Resampling.LANCZOS)
            os.makedirs(os.path.dirname(self._bg_path()), exist_ok=True)
            image.save(self._bg_path(), "JPEG", quality=92)
        except Exception as exc:
            self._emit("fail", f"Could not use that image: {exc}")
            return False
        self.look["bg_name"] = os.path.basename(path)
        self._bg_version += 1
        self._emit("ok", f"Background set to {self.look['bg_name']}")
        return True

    def _pick_background(self) -> bool:
        path = filedialog.askopenfilename(
            parent=self, title="Choose a background image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp *.gif"), ("All files", "*.*")])
        return bool(path) and self._import_background(path)

    def _build_text(self) -> None:
        c, px = self.canvas, self.px
        common = ("ui", "common")
        boost, tools, settings = ("ui", "tab-boost"), ("ui", "tab-tools"), ("ui", "tab-settings")
        c.create_text(px(42), px(18), anchor="w", text=APP_NAME, font=self.f_brand, fill=TEXT_HI, tags=common)
        status_x = 42 + self.f_brand.measure(APP_NAME) / self.scale + 16
        status_color = STARLIGHT if self.admin else ASH
        c.create_oval(px(status_x), px(15), px(status_x + 6), px(21), fill=status_color, outline="", tags=common)
        c.create_text(px(status_x + 12), px(18), anchor="w", text="Elevated" if self.admin else "Monitor only",
                      font=self.f_status, fill=mix(status_color, TEXT_HI, 0.35), tags=common)

        self.t_tabs = {}
        for key, (x, y, w, h) in L_TABS.items():
            self.t_tabs[key] = c.create_text(px(x + w / 2), px(y + 45), text=TAB_SPECS[key][0], font=self.f_label,
                                             fill=TEXT_MID, tags=common)

        # ---- Boost tab ----
        self.segments: dict[str, list[int]] = {}
        for key in ("cpu", "gpu", "ram"):
            x, y, w, _ = L_PILLS[key]
            ids = []
            seg_w = (w - 36 - (GPU_EXTRA_W if key == "gpu" else 0)) / 10
            for i in range(10):
                sx = x + 18 + i * seg_w
                ids.append(c.create_rectangle(px(sx), px(y + 42), px(sx + seg_w - 1.6), px(y + 45.5),
                                              fill=SEGMENT_OFF, outline="", tags=boost))
            self.segments[key] = ids
        x, y, _, _ = L_PILLS["cpu"]
        c.create_text(px(x + 18), px(y + 7), anchor="nw", text="CPU", font=self.f_label, fill=TEXT_MID, tags=boost)
        self.t_cpu = c.create_text(px(x + 18), px(y + 19), anchor="nw", text="--%", font=self.f_value,
                                   fill=TEXT_HI, tags=boost)
        x, y, _, _ = L_PILLS["gpu"]
        self.t_gpu_label = c.create_text(px(x + 18), px(y + 7), anchor="nw", text="GPU", font=self.f_label,
                                         fill=TEXT_MID, tags=boost)
        self.t_gpu = c.create_text(px(x + 18), px(y + 19), anchor="nw", text="--%", font=self.f_value,
                                   fill=TEXT_HI, tags=boost)
        gx_right = x + L_PILLS["gpu"][2] - 16
        self.t_gpu_vram = c.create_text(px(gx_right), px(y + 21), anchor="ne", font=self.f_label, fill=TEXT_MID,
                                        tags=boost)
        self.t_gpu_clock = c.create_text(px(gx_right), px(y + 34), anchor="ne", font=self.f_label, fill=TEXT_MID,
                                         tags=boost)
        x, y, _, _ = L_PILLS["ram"]
        self.t_ram_label = c.create_text(px(x + 18), px(y + 7), anchor="nw", text="RAM", font=self.f_label,
                                         fill=TEXT_MID, tags=boost)
        self.t_ram = c.create_text(px(x + 18), px(y + 19), anchor="nw", text="--%", font=self.f_value,
                                   fill=TEXT_HI, tags=boost)
        x, y, _, _ = L_PILLS["plan"]
        c.create_text(px(x + 18), px(y + 9), anchor="nw", text="Power plan", font=self.f_label, fill=TEXT_MID,
                      tags=boost)
        self.t_plan = c.create_text(px(x + 18), px(y + 25), anchor="nw", text="Detecting…", font=self.f_plan,
                                    fill=mix(STARLIGHT, "#FFFFFF", 0.2), tags=boost)

        self.t_purpose = {}
        for key, title, purpose, desc, accent, _icon in CARD_SPECS:
            x, y, w, _ = L_CARDS[key]
            c.create_text(px(x + 48), px(y + 12), anchor="nw", text=title, font=self.f_title, fill=TEXT_HI, tags=boost)
            self.t_purpose[key] = c.create_text(px(x + 48), px(y + 32), anchor="nw", text=purpose,
                                                font=self.f_purpose, fill=SILVER, tags=boost)
            c.create_text(px(x + 15), px(y + 56), anchor="nw", text=desc, font=self.f_desc, fill=TEXT_MID,
                          width=px(w - 30), tags=boost)

        self.t_buttons = {}
        for key, (x, y, w, h) in L_BUTTONS.items():
            label, font = {
                "optimize": ("OPTIMIZE NOW", self.f_primary),
                "defaults": ("Windows\ndefaults", self.f_ghost_small),
            }[key]
            self.t_buttons[key] = c.create_text(px(x + w / 2), px(y + h / 2), text=label, font=font,
                                                fill=TEXT_HI, justify="center", width=px(w - 16), tags=boost)

        # ---- Tools tab: sub-page switcher, then the ping tester ----
        sx, sy, sw, sh = L_SEGMENTS["tool"]
        step = sw / len(TOOL_PAGES)
        self.t_seg = {}
        for i, (key, label) in enumerate(TOOL_PAGES):
            self.t_seg[("tool", key)] = c.create_text(px(sx + step * (i + 0.5)), px(sy + sh / 2), text=label,
                                                      font=self.f_ghost_small, fill=TEXT_MID, tags=tools)
        tools = ("ui", "tab-tools", "page-ping")
        header_y = L_PING_TABLE[1] + 11
        for col, label in (("name", "SERVER"), ("latency", "AVERAGE"), ("jitter", "JITTER"), ("loss", "LOSS"),
                           ("bars", f"LAST {PING_SAMPLES} PINGS")):
            c.create_text(px(L_PING_COLS[col]), px(header_y), anchor="nw", text=label, font=self.f_label,
                          fill=ASH, tags=tools)
        self.t_ping = []
        bar_w = 7.8
        for i, _row in enumerate(self.ping_rows):
            y = L_PING_ROW_Y0 + i * L_PING_ROW_H
            items = {
                "name": c.create_text(px(L_PING_COLS["name"]), px(y + 2), anchor="nw", font=self.f_desc,
                                      fill=TEXT_HI, tags=tools),
                "host": c.create_text(px(L_PING_COLS["name"]), px(y + 17), anchor="nw", font=self.f_label,
                                      fill=TEXT_MID, tags=tools),
                "latency": c.create_text(px(L_PING_COLS["latency"]), px(y + 6), anchor="nw", font=self.f_plan,
                                         fill=TEXT_HI, tags=tools),
                "jitter": c.create_text(px(L_PING_COLS["jitter"]), px(y + 8), anchor="nw", font=self.f_desc,
                                        fill=TEXT_MID, tags=tools),
                "loss": c.create_text(px(L_PING_COLS["loss"]), px(y + 8), anchor="nw", font=self.f_desc,
                                      fill=TEXT_MID, tags=tools),
                "bars": [c.create_rectangle(0, 0, 0, 0, fill=SEGMENT_OFF, outline="", tags=tools)
                         for _ in range(PING_SAMPLES)],
                "bar_x": [L_PING_COLS["bars"] + k * bar_w for k in range(PING_SAMPLES)],
                "bar_w": bar_w - 1.8,
                "base_y": y + 26,
            }
            self.t_ping.append(items)
        fx, fy, _, fh = L_PING_HOST_FIELD
        c.create_text(px(fx + 16), px(fy + fh / 2), anchor="w", text="Custom server", font=self.f_label,
                      fill=TEXT_MID, tags=tools)
        for key, (x, y, w, h) in L_TOOL_BUTTONS.items():
            label = {"ping": "RUN TEST", "host": "Add & test"}[key]
            self.t_buttons[key] = c.create_text(px(x + w / 2), px(y + h / 2), text=label, font=self.f_ghost,
                                                fill=TEXT_HI, tags=tools)
        self._build_tool_pages()

        # ---- Settings tab: App page (the Windows page and the page switcher follow) ----
        self.t_setting_desc, self.t_setting_title = {}, {}
        for key, (x, y, w, h) in L_SETTING_ROWS.items():
            title, desc = SETTING_TEXT[key]
            ty, dy = L_SETTING_TEXT_Y
            self.t_setting_title[key] = c.create_text(px(x + 20), px(y + ty), anchor="nw", text=title,
                                                      font=self.f_title, fill=TEXT_HI, tags=settings)
            self.t_setting_desc[key] = c.create_text(px(x + 20), px(y + dy), anchor="nw", font=self.f_desc,
                                                     fill=TEXT_MID, tags=settings)
            self._set_setting_desc(key, desc)
        if not self.admin:
            self._set_setting_desc("autostart", "Needs administrator rights - restart the app and accept UAC.")
            self._set_setting_desc("restore", "Needs administrator rights - restart the app and accept UAC.")
        if pystray is None:
            self._set_setting_desc("tray", "Tray support is not included in this build.")
        bx, by, bw, bh = L_HOTKEYS_BUTTON
        self.t_buttons["hotkeys"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text="Hotkeys…",
                                                  font=self.f_ghost_small, fill=TEXT_HI, tags=settings)
        self._refresh_hotkey_labels()
        bx, by, bw, bh = L_PROTECT_BUTTON
        self.t_buttons["protect"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text="Turn on…",
                                                  font=self.f_ghost_small, fill=TEXT_HI, tags=(*settings, "protect-btn"))
        x, y, w, h = L_FOLDER_ROW
        c.create_text(px(x + 20), px(y + 5), anchor="nw", text="Your data", font=self.f_title,
                      fill=TEXT_HI, tags=settings)
        c.create_text(px(x + 20), px(y + 25), anchor="nw", text=STATE_DIR,
                      font=self.f_mono, fill=TEXT_MID, tags=settings)
        for key, (bx, by, bw, bh) in L_SETTING_BUTTONS.items():
            self.t_buttons[key] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text=SETTING_BUTTON_TEXT[key],
                                                font=self.f_ghost, fill=TEXT_HI, tags=settings)
        c.addtag_withtag("spage-app", "tab-settings")  # everything so far belongs to the App page
        sx, sy, sw, sh = L_SEGMENTS["set"]
        step = sw / len(SETTING_PAGES)
        for i, (key, label) in enumerate(SETTING_PAGES):
            self.t_seg[("set", key)] = c.create_text(px(sx + step * (i + 0.5)), px(sy + sh / 2), text=label,
                                                     font=self.f_ghost_small, fill=TEXT_MID, tags=settings)
        winset = (*settings, "spage-windows")
        for key, (x, y, w, h) in L_WINSET_ROWS.items():
            title, desc = SETTING_TEXT[key]
            ty, dy = L_SETTING_TEXT_Y
            self.t_setting_title[key] = c.create_text(px(x + 20), px(y + ty), anchor="nw", text=title,
                                                      font=self.f_title, fill=TEXT_HI, tags=winset)
            self.t_setting_desc[key] = c.create_text(px(x + 20), px(y + dy), anchor="nw", font=self.f_desc,
                                                     fill=TEXT_MID, tags=winset)
            self._set_setting_desc(key, desc)
        self._refresh_startkey_row()
        if not self.admin:
            for key in WINSET_ADMIN:
                self._set_setting_desc(key, "Needs administrator rights - restart the app and accept UAC.")

        # ---- Privacy tab ----
        pv = ("ui", "tab-privacy")
        c.create_text(px(CX), px(52), anchor="nw", text="Privacy", font=self.f_title, fill=TEXT_HI, tags=pv)
        c.create_text(px(CX), px(74), anchor="nw", font=self.f_purpose, fill=TEXT_MID, tags=pv,
                      text="Turns off ads and tracking built into Windows. Switch one off to put back exactly what "
                           "you had.")
        self.t_priv_desc = {}
        for key, title, desc, _values in PRIVACY_ITEMS:
            x, y, w, h = L_PRIV_ROWS[key]
            ty, dy = L_SETTING_TEXT_Y
            c.create_text(px(x + 20), px(y + ty), anchor="nw", text=title, font=self.f_title, fill=TEXT_HI, tags=pv)
            if privacy_needs_admin(key) and not self.admin:
                desc = "Needs administrator rights - restart the app and accept UAC."
            self.t_priv_desc[key] = c.create_text(px(x + 20), px(y + dy), anchor="nw", font=self.f_desc,
                                                  fill=TEXT_MID, tags=pv, text=self._fit(desc, self.f_desc, w - 120))
        x, y, w, h = L_PRIV_FOOT
        self.t_priv_note = c.create_text(px(x + 20), px(y + h / 2), anchor="w", font=self.f_desc, fill=TEXT_MID,
                                         width=px(w - 20 - 150), tags=pv)
        bx, by, bw, bh = L_PRIV_ALL
        self.t_buttons["privall"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text="Turn all on",
                                                  font=self.f_ghost, fill=TEXT_HI, tags=pv)

        # ---- About tab ----
        ab = ("ui", "tab-about")
        x, y, w, h = L_ABOUT_ROWS["app"]
        tx = L_ABOUT_ICON[0] + L_ABOUT_ICON[2] + 22
        c.create_text(px(tx), px(y + 24), anchor="nw", text=APP_NAME, font=self.f_hero, fill=TEXT_HI, tags=ab)
        self.t_about_version = c.create_text(px(tx), px(y + 62), anchor="nw", font=self.f_desc, fill=TEXT_MID,
                                             tags=ab, text=self._version_line())
        bx, by, bw, bh = L_ABOUT_UPDATE
        self.t_buttons["update"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text=self._update_label(),
                                                 font=self.f_ghost, fill=TEXT_HI, tags=ab)
        c.create_text(px(tx), px(y + 86), anchor="nw", font=self.f_desc, fill=TEXT_HI, width=px(x + w - 24 - tx),
                      tags=ab, text="Tunes Windows for gaming in one click, and puts it all back just as easily.")
        x, y, w, h = L_ABOUT_ROWS["credits"]
        nx = L_ABOUT_AVATAR[0] + L_ABOUT_AVATAR[2] + 20
        c.create_text(px(nx), px(y + 26), anchor="nw", text="Made by", font=self.f_label, fill=ASH, tags=ab)
        c.create_text(px(nx), px(y + 42), anchor="nw", text=CREDIT_NAME, font=self.f_name, fill=TEXT_HI, tags=ab)
        self.t_about_link = c.create_text(px(nx), px(y + 78), anchor="nw", font=self.f_desc, fill=SILVER, tags=ab,
                                          text=CREDIT_URL.replace("https://", ""))
        bx, by, bw, bh = L_ABOUT_PROFILE
        self.t_buttons["profile"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text="GitHub page",
                                                  font=self.f_ghost, fill=TEXT_HI, tags=ab)
        bx, by, bw, bh = L_ABOUT_REPORT
        self.t_buttons["report"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text="Report a problem",
                                                 font=self.f_ghost, fill=TEXT_HI, tags=ab)
        x, y, w, h = L_ABOUT_ROWS["info"]
        for i, (label, value, font) in enumerate((
                ("License", "MIT - free to use, change and share", self.f_desc),
                ("Built with", "Python, customtkinter, Pillow, psutil, pystray and Intel PresentMon", self.f_desc),
                ("Your data", STATE_DIR, self.f_mono))):
            ry = y + 22 + i * 32
            c.create_text(px(x + 20), px(ry), anchor="nw", text=label, font=self.f_label, fill=TEXT_MID, tags=ab)
            c.create_text(px(x + 122), px(ry - 1), anchor="nw", text=value, font=font, fill=TEXT_HI, tags=ab)

        # ---- Appearance tab ----
        look = ("ui", "tab-settings", "spage-appearance")
        x, y, w, h = L_LOOK_RESET
        self.t_buttons["reset"] = c.create_text(px(x + w / 2), px(y + h / 2), text="Reset", font=self.f_ghost,
                                                fill=TEXT_HI, tags=look)
        x, y, _, _ = L_LOOK_ROWS["background"]
        c.create_text(px(x + 20), px(y + 20), anchor="nw", text="Background", font=self.f_title, fill=TEXT_HI,
                      tags=look)
        self.t_bg_hint = c.create_text(px(x + 20), px(L_LOOK_HINT_Y), anchor="w", font=self.f_desc, fill=TEXT_MID,
                                       tags=look)
        bx, by, bw, bh = L_LOOK_IMAGE_BTN
        self.t_buttons["image"] = c.create_text(px(bx + bw / 2), px(by + bh / 2), text="Change image…",
                                                font=self.f_ghost_small, fill=TEXT_HI, tags=(*look, "look-image"))
        x, y, _, _ = L_LOOK_ROWS["motion"]
        c.create_text(px(x + 20), px(y + 20), anchor="nw", text="Motion", font=self.f_title, fill=TEXT_HI, tags=look)
        self.t_motion_desc = c.create_text(px(x + 20), px(y + 54), anchor="nw", font=self.f_desc, fill=TEXT_MID,
                                           width=px(588), tags=look)
        c.create_text(px(x + 20), px(L_LOOK_INTRO_Y), anchor="w", text="Startup intro", font=self.f_desc,
                      fill=TEXT_HI, tags=look)
        self.t_intro_hint = c.create_text(px(x + 20) + self.f_desc.measure("Startup intro") + px(12),
                                          px(L_LOOK_INTRO_Y), anchor="w", font=self.f_desc, fill=TEXT_MID, tags=look)
        for seg, options in (("bg", BG_KINDS), ("motion", MOTION_MODES)):
            sx, sy, sw, sh = L_SEGMENTS[seg]
            step = sw / len(options)
            for i, (key, label) in enumerate(options):
                self.t_seg[(seg, key)] = c.create_text(px(sx + step * (i + 0.5)), px(sy + sh / 2), text=label,
                                                       font=self.f_ghost_small, fill=TEXT_MID, tags=look)
        self.t_slider_label, self.t_slider_value = {}, {}
        for key, (sx, cy, sw) in L_SLIDERS.items():
            self.t_slider_label[key] = c.create_text(px(98), px(cy), anchor="w", text=SLIDER_SPECS[key][0],
                                                     font=self.f_desc, fill=TEXT_HI, tags=look)
            self.t_slider_value[key] = c.create_text(px(686), px(cy), anchor="e", font=self.f_desc,
                                                     fill=TEXT_MID, tags=look)
        self.slider_items = {key: c.create_image(0, 0, anchor="nw", tags=look) for key in L_SLIDERS}

        for i in range(len(self.ping_rows)):
            self._paint_ping_row(i)
        self._refresh_look_texts()
        self._refresh_primary()

    def _build_tool_pages(self) -> None:
        c, px = self.canvas, self.px
        tags = {page: ("ui", "tab-tools", f"page-{page}") for page, _ in TOOL_PAGES}
        for key, label, page in (("addgame", "Add game…", "games"), ("refresh", "Refresh", "startup"),
                                 ("clean", "CLEAN UP", "cleanup"), ("dnstest", "RUN TEST", "dns"),
                                 ("checkup", "RUN CHECKUP", "checkup")):
            x, y, w, h = TOOL_PAGE_BUTTONS[page][key]
            self.t_buttons[key] = c.create_text(px(x + w / 2), px(y + h / 2), text=label, font=self.f_ghost,
                                                fill=TEXT_HI, tags=tags[page])
        head_y = L_TABLE[1] + 11

        # Checkup
        ck = tags["checkup"]
        sx, sy, pw, ph = L_CHECK_SCORE
        self.t_check_score = c.create_text(px(sx + len(CHECKUP_ITEMS) * (pw + 5) + 8), px(sy + ph / 2), anchor="w",
                                           font=self.f_desc, fill=TEXT_HI, tags=ck)
        self.t_check = []
        bw, bh = L_CHECK_BTN
        for rx, ry, rw, rh in L_CHECK_SLOTS:
            self.t_check.append({
                "title": c.create_text(px(rx + 52), px(ry + 8), anchor="nw", font=self.f_title,
                                       fill=TEXT_HI, tags=ck),
                "detail": c.create_text(px(rx + 52), px(ry + 30), anchor="nw", font=self.f_desc, fill=TEXT_MID,
                                        tags=ck),
                "value": c.create_text(px(rx + rw - 20), px(ry + rh / 2), anchor="e", font=self.f_plan,
                                       fill=TEXT_HI, tags=ck),
                "btn": c.create_text(px(rx + rw - 20 - bw / 2), px(ry + rh / 2), font=self.f_ghost_small,
                                     fill=TEXT_HI, tags=ck),
            })

        # Games
        g = tags["games"]
        fx, fy, fw, fh = L_GAME_FIND
        self.t_buttons["findgames"] = c.create_text(px(fx + fw / 2), px(fy + fh / 2), text="Find games",
                                                    font=self.f_ghost, fill=TEXT_HI, tags=g)
        self.t_game_heads = {"name": c.create_text(px(L_GAME_COLS["name"]), px(head_y), anchor="nw", text="GAME",
                                                   font=self.f_label, fill=ASH, tags=g)}
        for col, label in GAME_HEADS.items():
            self.t_game_heads[col] = c.create_text(px(L_GAME_COLS[col] + 22), px(head_y), anchor="n", text=label,
                                                   font=self.f_label, fill=ASH, tags=g)
        for col, cx in L_HIST_COLS.items():
            self.t_game_heads[col] = c.create_text(px(cx), px(head_y), anchor="n", font=self.f_label, fill=ASH, tags=g)
        self.t_cmp_heads = {col: c.create_text(px(cx), px(head_y), anchor="n", font=self.f_label, fill=ASH, tags=g)
                            for col, cx in L_CMP_COLS.items()}
        self.t_cmp = [{col: c.create_text(px(cx), px(L_GAME_ROW_Y0 + i * L_GAME_ROW_H + 12), anchor="n",
                                          font=self.f_plan, fill=TEXT_HI, tags=g) for col, cx in L_CMP_COLS.items()}
                      for i in range(GAME_ROWS)]
        self.t_games = []
        for i in range(GAME_ROWS):
            ry = L_GAME_ROW_Y0 + i * L_GAME_ROW_H
            self.t_games.append({
                "name": c.create_text(px(L_GAME_COLS["name"]), px(ry + 5), anchor="nw", font=self.f_desc,
                                      fill=TEXT_HI, tags=g),
                "sub": c.create_text(px(L_GAME_COLS["name"]), px(ry + 23), anchor="nw", font=self.f_label,
                                     fill=TEXT_MID, tags=g),
                **{col: c.create_text(px(cx), px(ry + 12), anchor="n", font=self.f_plan, fill=TEXT_HI, tags=g)
                   for col, cx in L_HIST_COLS.items()},
            })
        tx, ty, tw, th = L_TABLE
        self.t_games_empty = c.create_text(px(tx + tw / 2), px(ty + th / 2 + 10), font=self.f_desc, fill=TEXT_MID,
                                           justify="center", width=px(420), tags=g,
                                           text="No games yet. Press Add game… and pick the game's .exe file.\n"
                                                "It gets high CPU priority every time it runs.")
        ax, ay, aw, ah = L_GAME_AUTO
        self.t_game_auto = c.create_text(px(ax + 76), px(ay + ah / 2), anchor="w", font=self.f_desc, fill=TEXT_HI,
                                         tags=g, text=GAME_AUTO_TEXT)
        self.t_game_overlay = c.create_text(px(ax + L_GAME_SPLIT + 58), px(ay + ah / 2), anchor="w", font=self.f_desc,
                                            fill=TEXT_HI, tags=g, text=self._game_overlay_text())
        self.t_game_background = c.create_text(px(ax + L_GAME_SPLIT2 + 58), px(ay + ah / 2), anchor="w",
                                               font=self.f_desc, fill=TEXT_HI, tags=g, text=GAME_BACKGROUND_TEXT)

        # Startup apps
        st = tags["startup"]
        for col, label in (("name", "PROGRAM"), ("source", "STARTS FROM")):
            c.create_text(px(L_START_COLS[col]), px(head_y), anchor="nw", text=label, font=self.f_label, fill=ASH,
                          tags=st)
        c.create_text(px(L_START_COLS["toggle"] + 22), px(head_y), anchor="n", text="ON", font=self.f_label,
                      fill=ASH, tags=st)
        self.t_startup = []
        for i in range(START_ROWS):
            ry = L_START_ROW_Y0 + i * L_START_ROW_H
            self.t_startup.append({
                "name": c.create_text(px(L_START_COLS["name"]), px(ry + 3), anchor="nw", font=self.f_desc,
                                      fill=TEXT_HI, tags=st),
                "sub": c.create_text(px(L_START_COLS["name"]), px(ry + 20), anchor="nw", font=self.f_label,
                                     fill=TEXT_MID, tags=st),
                "source": c.create_text(px(L_START_COLS["source"]), px(ry + 11), anchor="nw", font=self.f_label,
                                        fill=TEXT_MID, tags=st),
            })
        self.t_startup_empty = c.create_text(px(tx + tw / 2), px(ty + th / 2 + 10), font=self.f_desc,
                                             fill=TEXT_MID, tags=st, text="Loading startup apps…")
        self.t_startup_note = c.create_text(px(CX), px(436), anchor="nw", font=self.f_desc, fill=TEXT_MID,
                                            width=px(628), tags=st)

        # Cleanup
        cl = tags["cleanup"]
        self.t_clean = {}
        for key, title, desc, _paths in clean_targets():
            rx, ry, rw, rh = L_CLEAN_ROWS[key]
            c.create_text(px(rx + 20), px(ry + 4), anchor="nw", text=title, font=self.f_title, fill=TEXT_HI, tags=cl)
            c.create_text(px(rx + 20), px(ry + 25), anchor="nw", text=desc, font=self.f_label, fill=TEXT_MID, tags=cl)
            self.t_clean[key] = c.create_text(px(rx + rw - 20 - 44 - 18), px(ry + rh / 2), anchor="e",
                                              font=self.f_plan, fill=TEXT_HI, tags=cl)
        bx, by, bw, bh = L_CLEAN_BAR
        self.t_clean_total = c.create_text(px(bx + 18), px(by + 10), anchor="nw", font=self.f_value, fill=TEXT_HI,
                                           tags=cl)
        self.t_clean_last = c.create_text(px(bx + 18), px(by + 42), anchor="nw", font=self.f_desc, fill=TEXT_MID,
                                          width=px(370), tags=cl)
        c.create_text(px(bx + bw - 20 - 44 - 14), px(by + 25), anchor="e", text="Clean up every week",
                      font=self.f_desc, fill=TEXT_HI, tags=cl)
        c.create_text(px(bx + bw - 20 - 44 - 14), px(by + 45), anchor="e", text="While Astra is open, never mid-game",
                      font=self.f_label, fill=TEXT_MID, tags=cl)
        # DNS
        dn = tags["dns"]
        for col, label in (("name", "DNS SERVER"), ("median", "TYPICAL"), ("answered", "ANSWERED")):
            c.create_text(px(L_DNS_COLS[col]), px(head_y), anchor="nw", text=label, font=self.f_label, fill=ASH,
                          tags=dn)
        self.t_dns = []
        for i in range(DNS_ROWS):
            ry = L_DNS_ROW_Y0 + i * L_DNS_ROW_H
            self.t_dns.append({
                "name": c.create_text(px(L_DNS_COLS["name"]), px(ry + 3), anchor="nw", font=self.f_desc,
                                      fill=TEXT_HI, tags=dn),
                "sub": c.create_text(px(L_DNS_COLS["name"]), px(ry + 20), anchor="nw", font=self.f_label,
                                     fill=TEXT_MID, tags=dn),
                "median": c.create_text(px(L_DNS_COLS["median"]), px(ry + 8), anchor="nw", font=self.f_plan,
                                        fill=TEXT_HI, tags=dn),
                "answered": c.create_text(px(L_DNS_COLS["answered"]), px(ry + 10), anchor="nw", font=self.f_desc,
                                          fill=TEXT_MID, tags=dn),
                "use": c.create_text(px(L_DNS_COLS["use"] + 34), px(ry + 18), font=self.f_ghost_small,
                                     fill="#CFCFCF", tags=dn),
            })
        bx, by, bw, bh = L_DNS_BAR
        self.t_dns_note = c.create_text(px(bx + 18), px(by + 14), anchor="nw", font=self.f_desc, fill=TEXT_MID,
                                        width=px(440), tags=dn)
        rx, ry, rw, rh = L_DNS_RESTORE
        self.t_buttons["dnsrestore"] = c.create_text(px(rx + rw / 2), px(ry + rh / 2), text="Restore previous",
                                                     font=self.f_ghost_small, fill=TEXT_HI, tags=(*dn, "dns-restore"))

        # Monitor
        mo = tags["monitor"]
        self.t_monitor = {}
        for key, (mx, my, mw, mh) in L_MONITOR.items():
            title, _unit = MONITOR_SPECS[key]
            cx, cy, cw, ch = self._chart_rect((mx, my, mw, mh))
            self.t_monitor[key] = {
                "title": c.create_text(px(mx + 18), px(my + 10), anchor="nw", text=title, font=self.f_desc,
                                       fill=TEXT_HI, tags=mo),
                "value": c.create_text(px(mx + 18), px(my + 28), anchor="nw", font=self.f_value, fill=TEXT_HI,
                                       tags=mo),
                "stats": c.create_text(px(mx + 18), px(my + 58), anchor="nw", font=self.f_label, fill=TEXT_MID,
                                       tags=mo),
                "line": c.create_line(px(cx), px(cy + ch), px(cx + 1), px(cy + ch), fill=STARLIGHT,
                                      width=max(1, self.px(1.6)), joinstyle="round", tags=mo),
                "top": c.create_text(px(cx + 4), px(cy + 2), anchor="nw", font=self.f_label, fill=ASH, tags=mo),
                "bottom": c.create_text(px(cx + 4), px(cy + ch - 2), anchor="sw", font=self.f_label, fill=ASH,
                                        tags=mo),
            }
        self._paint_checkup()
        self._paint_games()
        self._paint_startup()
        self._paint_cleanup()
        self._paint_dns()
        self._paint_monitor()

    def _build_host_entry(self) -> None:
        """A text box drawn on the canvas itself, so it is glass like every other panel
        (a native Entry widget is always an opaque rectangle)."""
        c, px = self.canvas, self.px
        fx, fy, fw, fh = L_PING_HOST_FIELD
        self._host_x0 = fx + 16 + self.f_label.measure("Custom server") / self.scale + 14
        self._host_cy = fy + fh / 2
        self.host_value = self._setting("custom_host") or ""
        self.host_cursor = len(self.host_value)
        self.host_focus = False
        self.host_select_all = False
        self._caret_on = True
        self._caret_job = None
        tags = ("ui", "tab-tools", "page-ping")
        self.t_host_sel = c.create_rectangle(0, 0, 0, 0, fill="#3A3A3A", outline="", tags=tags)
        self.t_host = c.create_text(px(self._host_x0), px(self._host_cy), anchor="w", font=self.f_desc,
                                    fill=TEXT_HI, tags=tags)
        self.t_host_caret = c.create_line(0, 0, 0, 0, fill=TEXT_HI, width=max(1, px(1.3)), tags=tags)
        self.host_var = _HostValue(self)
        c.bind("<Key>", self._on_host_key)
        self._paint_host()

    def _paint_host(self) -> None:
        c, px = self.canvas, self.px
        value = self.host_value
        if value or self.host_focus:
            c.itemconfigure(self.t_host, text=value, fill=TEXT_HI)
        else:
            c.itemconfigure(self.t_host, text="Type a host name or IP, then press Enter", fill=ASH)
        x_text = self.f_desc.measure(value[:self.host_cursor]) / self.scale
        half = 9
        if self.host_select_all and value:
            width = self.f_desc.measure(value) / self.scale
            c.coords(self.t_host_sel, px(self._host_x0 - 2), px(self._host_cy - half),
                     px(self._host_x0 + width + 2), px(self._host_cy + half))
        else:
            c.coords(self.t_host_sel, 0, 0, 0, 0)
        if self.host_focus and self._caret_on and not self.host_select_all:
            x = self._host_x0 + x_text + 0.5
            c.coords(self.t_host_caret, px(x), px(self._host_cy - half), px(x), px(self._host_cy + half))
        else:
            c.coords(self.t_host_caret, 0, 0, 0, 0)

    def _blink_caret(self) -> None:
        self._caret_job = None
        if not self.host_focus or self._closing:
            return
        self._caret_on = not self._caret_on
        self._paint_host()
        self._caret_job = self.after(530, self._blink_caret)

    def _focus_host(self, x_logical: float | None = None) -> None:
        self.host_focus = True
        self.host_select_all = False
        self.canvas.focus_set()
        if x_logical is not None:
            offset, best = x_logical - self._host_x0, 0
            for i in range(len(self.host_value) + 1):
                if self.f_desc.measure(self.host_value[:i]) / self.scale <= offset + 3:
                    best = i
            self.host_cursor = best
        self._restart_caret()
        self._render()

    def _blur_host(self) -> None:
        if not self.host_focus:
            return
        self.host_focus = False
        self.host_select_all = False
        self._paint_host()
        self._render()

    def _restart_caret(self) -> None:
        if self._caret_job is not None:
            self.after_cancel(self._caret_job)
        self._caret_on = True
        self._paint_host()
        self._caret_job = self.after(530, self._blink_caret)

    def _on_host_key(self, event):
        if self.dialog and self.dialog.get("recording"):
            return self._on_hotkey_record(event)
        if not self.host_focus:
            return None
        keysym, ctrl = event.keysym, bool(event.state & 0x4)
        value, cursor = self.host_value, self.host_cursor
        if keysym in ("Tab", "ISO_Left_Tab"):
            self._blur_host()
            return None
        if keysym in ("Return", "KP_Enter"):
            self._use_custom_host()
            return "break"
        if keysym == "Escape":
            self._blur_host()
            return "break"
        if ctrl and keysym.lower() == "a":
            self.host_select_all = True
        elif ctrl and keysym.lower() in ("c", "x"):
            if self.host_select_all and value:
                self.clipboard_clear()
                self.clipboard_append(value)
                if keysym.lower() == "x":
                    value, cursor, self.host_select_all = "", 0, False
        elif ctrl and keysym.lower() == "v":
            try:
                pasted = "".join(self.clipboard_get().split())
            except tk.TclError:
                pasted = ""
            if self.host_select_all:
                value, cursor = "", 0
            value, cursor = value[:cursor] + pasted + value[cursor:], cursor + len(pasted)
            self.host_select_all = False
        elif keysym == "BackSpace":
            if self.host_select_all:
                value, cursor = "", 0
            elif cursor:
                value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
            self.host_select_all = False
        elif keysym == "Delete":
            if self.host_select_all:
                value, cursor = "", 0
            else:
                value = value[:cursor] + value[cursor + 1:]
            self.host_select_all = False
        elif keysym in ("Left", "Right", "Home", "End"):
            if self.host_select_all:
                cursor = 0 if keysym in ("Left", "Home") else len(value)
            else:
                cursor = {"Left": max(0, cursor - 1), "Right": min(len(value), cursor + 1),
                          "Home": 0, "End": len(value)}[keysym]
            self.host_select_all = False
        elif event.char and event.char.isprintable() and not ctrl and event.char != " ":
            if self.host_select_all:
                value, cursor = "", 0
            value, cursor = value[:cursor] + event.char + value[cursor:], cursor + 1
            self.host_select_all = False
        else:
            return "break"
        self.host_value, self.host_cursor = value[:253], min(cursor, 253)
        self._restart_caret()
        return "break"

    def _set_setting_desc(self, key: str, text: str, color: str = TEXT_MID) -> None:
        x, _y, w, _h = L_SETTING_ROWS.get(key) or L_WINSET_ROWS[key]
        room = w - 120 - (L_PROTECT_BUTTON[2] + 14 if key == "restore" and self.protection is False else 0) \
            - (L_HOTKEYS_BUTTON[2] + 14 if key == "hotkey" else 0)
        self.canvas.itemconfigure(self.t_setting_desc[key], text=self._fit(text, self.f_desc, room), fill=color)

    def _refresh_startkey_row(self) -> None:
        if not self._setting("startkey"):
            text = SETTING_TEXT["startkey"][1]
        else:
            marked = sum(1 for g in self.games if g.get("winkey"))
            where = (f"in your {marked} marked game{'s' if marked != 1 else ''}" if marked
                     else "in any game on your list")
            text = f"A lone press won't open Start {where}. Win+D and other shortcuts still work."
        self._set_setting_desc("startkey", text)

    def _refresh_restore_row(self) -> None:
        if not self.admin:
            return
        if self.protection is False:
            self._set_setting_desc("restore", f"System Protection is off on {SYSTEM_DRIVE}, so Windows can't save "
                                              "restore points.", WARN_YELLOW)
        else:
            self._set_setting_desc("restore", SETTING_TEXT["restore"][1])

    def _apply_visibility(self) -> None:
        """Show the active tab's canvas items and widgets; hide everything under a dialog."""
        c = self.canvas
        if self.dialog:
            c.itemconfigure("ui", state="hidden")
            c.itemconfigure(self.console.window, state="hidden")
            return
        c.itemconfigure("common", state="normal")
        for tab in TAB_ORDER:
            c.itemconfigure(f"tab-{tab}", state="normal" if tab == self.tab else "hidden")
        c.itemconfigure(self.console.window, state="normal" if self.tab == "boost" else "hidden")
        if self.tab == "tools":
            for page, _label in TOOL_PAGES:
                if page != self.tool_page:
                    c.itemconfigure(f"page-{page}", state="hidden")
            self._paint_checkup()
            self._paint_games()
            self._paint_startup()
            self._paint_cleanup()
            self._paint_dns()
            self._paint_monitor()
            if not self.settings_dns_original():
                c.itemconfigure("dns-restore", state="hidden")
        for key, item in self.t_tabs.items():
            c.itemconfigure(item, fill=TEXT_HI if key == self.tab else TEXT_MID)
        if self.tab == "settings":
            for page, _label in SETTING_PAGES:
                if page != self.settings_page:
                    c.itemconfigure(f"spage-{page}", state="hidden")
        if not self._look_page() or self.look["background"] != "image":
            c.itemconfigure("look-image", state="hidden")
        if self.protection is not False or not self.admin:
            c.itemconfigure("protect-btn", state="hidden")

    # ---------------- settings ----------------

    def _setting(self, key: str):
        settings = self.state_store.get("settings") or {}
        return settings.get(key, DEFAULT_SETTINGS[key])

    def _set_setting(self, key: str, value) -> None:
        settings = dict(self.state_store.get("settings") or {})
        settings[key] = value
        self.state_store.set("settings", settings)

    # ---------------- state helpers ----------------

    def _enabled(self, target: str | None) -> bool:
        if not target:
            return False
        kind, _, key = target.partition(":")
        if kind == "dlg":
            if key == "ok" and self.dialog and self.dialog["needs_check"]:
                return self.dialog["checked"]
            return True
        if kind == "win":
            return not self.dialog
        if self.dialog:
            return False
        if kind == "tab":
            return True
        if kind == "card":
            return self.tab == "boost" and not self.busy
        if kind == "look":
            return self._look_page()
        if kind == "set":
            if self.tab != "settings":
                return False
            if key == "tray":
                return pystray is not None
            if key in ("autostart", "restore"):
                return self.admin and not (key == "autostart" and self._autostart_pending)
            if key in WINSET_ADMIN:
                return self.admin
            return True
        if kind == "seg":
            if key.startswith("tool:"):
                return self.tab == "tools"
            if key.startswith("set:"):
                return self.tab == "settings"
            return self._look_page()
        if kind == "slider":
            return self._look_page() and (key in GLOBAL_SLIDERS or self.look["background"] != "black")
        if key == "reset":
            return self._look_page()
        if key == "image":
            return self._look_page() and self.look["background"] == "image"
        if key in ("optimize", "defaults"):
            return self.tab == "boost" and self.admin and not self.busy
        tools_page = self.tool_page if self.tab == "tools" else None
        if kind == "chk":
            return tools_page == "checkup" and self.checkup_state == "idle" \
                and bool((self.checkup.get(key) or {}).get("action"))
        if key == "checkup":
            return tools_page == "checkup" and self.checkup_state == "idle"
        if kind == "hist":
            return tools_page == "games" and bool(self.session_view) and not self.compare_pair
        if key == "addgame" and self.session_view and not self.game_finder:
            return tools_page == "games" and not self.compare_pair and len(self._game_sessions(self.session_view)) >= 2
        if kind == "game":
            return tools_page == "games"
        if kind == "startup":
            return tools_page == "startup" and self.startup_items is not None
        if kind == "clean":
            return tools_page == "cleanup" and self.clean_state == "idle"
        if key == "ping":
            return tools_page == "ping" and not self.ping_running
        if key == "host":
            return tools_page == "ping"
        if kind == "host":
            return tools_page == "ping"
        if kind == "find":
            return tools_page == "games" and bool(self.game_finder) and self.game_finder["state"] == "ready"
        if kind == "dnsuse":
            return tools_page == "dns" and self.admin and self.dns_state == "idle" and self.dns_info is not None
        if key == "findgames":
            return tools_page == "games" and not (self.game_finder and self.game_finder["state"] == "searching")
        if key == "addgame":
            if self.game_finder:
                return tools_page == "games" and any(i["pick"] for i in self.game_finder.get("items", []))
            return tools_page == "games"
        if key == "dnstest":
            return tools_page == "dns" and self.dns_state == "idle"
        if key == "dnsrestore":
            return tools_page == "dns" and self.admin and self.dns_state == "idle" and bool(self.settings_dns_original())
        if key == "refresh":
            return tools_page == "startup"
        if key == "clean":
            return tools_page == "cleanup" and self.clean_state == "idle" and self._clean_total()[1] > 0
        if key in ("folder", "backup", "restore", "hotkeys"):
            return self.tab == "settings"
        if key == "profile":
            return self.tab == "about"
        if key == "report":
            return self.tab == "about" and not self.report_busy
        if key == "update":
            return self.tab == "about" and self.update_state not in ("checking", "downloading")
        if kind == "priv":
            return self.tab == "privacy" and not self._privacy_busy and (self.admin or not privacy_needs_admin(key))
        if key == "privall":
            return self.tab == "privacy" and not self._privacy_busy and any(
                not self.privacy_state.get(k) and (self.admin or not privacy_needs_admin(k)) for k in PRIVACY_KEYS)
        if key == "protect":
            return self.tab == "settings" and self.admin and self.protection is False and not self._protect_pending
        return False

    def _button_state(self, key: str) -> str:
        target = f"btn:{key}"
        if (key == "optimize" and self.busy) or (key == "ping" and self.ping_running) \
                or (key == "clean" and self.clean_state != "idle") or (key == "dnstest" and self.dns_state != "idle") \
                or (key == "checkup" and self.checkup_state == "running"):
            return "busy"
        if not self._enabled(target):
            return "disabled"
        if self.pressed == target and self.hover == target:
            return "pressed"
        if self.hover == target:
            return "hover"
        return "normal"

    def _windows_motion(self) -> bool:
        """Windows 'Animation effects' (cached for 2 s)."""
        now = time.monotonic()
        if now - self._motion_checked > 2.0:
            self._motion_checked = now
            value = SystemTweaks._spi_get(SPI_SETCLIENTAREAANIMATION)
            self._motion = True if value is None else bool(value)
        return self._motion

    def _motion_level(self) -> str:
        """full | reduced | off. 'Windows' maps Animation effects on -> full, off -> reduced."""
        mode = self.look["motion"]
        if mode == "system":
            return "full" if self._windows_motion() else "reduced"
        return mode

    def _seg_targets(self) -> dict[str, float]:
        return {"bg": float([k for k, _ in BG_KINDS].index(self.look["background"])),
                "motion": float([k for k, _ in MOTION_MODES].index(self.look["motion"])),
                "tool": float([k for k, _ in TOOL_PAGES].index(self.tool_page)),
                "set": float([k for k, _ in SETTING_PAGES].index(self.settings_page))}

    def _targets(self) -> tuple[dict, float]:
        cards = {}
        for key in PROFILE_ORDER:
            if key == self.profile:
                cards[key] = 1.0
            elif self.hover == f"card:{key}" and self._enabled(self.hover):
                cards[key] = 0.6
            else:
                cards[key] = 0.0
        bloom = 1.0 if self.busy or (self.hover == "btn:optimize" and self._enabled(self.hover)) else 0.0
        return cards, bloom

    def _snap_animations(self) -> None:
        cards, bloom = self._targets()
        self.card_t.update(cards)
        self.bloom_t = bloom
        self.seg_t.update(self._seg_targets())
        self._level = self._motion_level()

    def _kick(self) -> None:
        if self._anim_job is None:
            self._anim_job = self.after(0, self._animate)

    def _animate(self) -> None:
        self._anim_job = None
        level = self._level = self._motion_level()
        cards, bloom = self._targets()
        changed = active = False
        rate = {"full": 0.3, "reduced": 0.6}.get(level)  # reduced: quicker fades; off: snap

        def approach(current: float, target: float, moves: bool = False) -> float:
            if rate is None or (moves and level != "full"):
                return target
            nxt = current + (target - current) * rate
            return target if abs(target - nxt) < 0.02 else nxt

        for key, target in cards.items():
            if self.card_t[key] != target:
                self.card_t[key] = approach(self.card_t[key], target)
                changed = True
                active = active or self.card_t[key] != target
        if self.bloom_t != bloom:
            self.bloom_t = approach(self.bloom_t, bloom)
            changed = True
            active = active or self.bloom_t != bloom
        for key, target in self._seg_targets().items():
            if self.seg_t.get(key) != target:
                self.seg_t[key] = approach(self.seg_t.get(key, target), target, moves=True)
                changed = True
                active = active or self.seg_t[key] != target
        if (self.busy or self.ping_running) and level == "full":
            self.shimmer = (self.shimmer + 0.03) % 1.0
            changed = active = True
        if changed:
            self._render()
        if active:
            self._anim_job = self.after(33 if self.busy or self.ping_running else 16, self._animate)

    # ---------------- rendering ----------------

    def _cached(self, key, factory):
        image = self._cache.get(key)
        if image is None:
            if len(self._cache) > 200:
                self._cache.clear()
            image = self._cache[key] = factory()
        return image

    def _render(self) -> None:
        px = self.px
        if self.dialog:
            scene = self.dialog["backdrop"].copy()
            x, y, w, h = self.dialog["rect"]
            panel, m = self._cached(("panel", w, h), lambda: self.painter.panel(px(w), px(h), STARLIGHT))
            paste_layer(scene, panel, px(x) - m, px(y) - m)
            for action, chip in (self.dialog.get("chips") or {}).items():
                if self.dialog.get("recording") == action:
                    chip_state = "pressed"
                else:
                    chip_state = "hover" if self.hover == f"dlg:chip:{action}" else "normal"
                bx, by, bw, bh = chip["rect"]
                img = self._cached(("dlg-chip", bw, bh, chip_state),
                                   lambda: self.painter.ghost_button(px(bw), px(bh), chip_state))
                paste_layer(scene, img, px(bx), px(by))
                self.canvas.itemconfigure(chip["text"], fill="#CFCFCF" if chip_state == "normal" else "#FFFFFF")
            if self.dialog["needs_check"]:
                bx, by, bs = self.dialog["box"]
                checked, hovered = self.dialog["checked"], self.hover == "dlg:check"
                box = self._cached(("check", checked, hovered), lambda: self.painter.checkbox(px(bs), checked, hovered))
                paste_layer(scene, box, px(bx), px(by))
            if not self._enabled("dlg:ok"):
                ok_state = "disabled"
            elif self.pressed == "dlg:ok" and self.hover == "dlg:ok":
                ok_state = "pressed"
            else:
                ok_state = "hover" if self.hover == "dlg:ok" else "normal"
            bx, by, bw, bh = self.dialog["ok_rect"]
            img, m = self._cached(("dlg-ok", ok_state), lambda: self.painter.primary_button(
                px(bw), px(bh), ok_state, 1.0 if ok_state == "hover" else 0.0))
            paste_layer(scene, img, px(bx) - m, px(by) - m)
            self.canvas.itemconfigure(self.dialog["ok_text"], fill="#4D4D4D" if ok_state == "disabled" else VOID)
            if self.dialog["cancel_rect"]:
                cancel_state = "hover" if self.hover == "dlg:cancel" else "normal"
                bx, by, bw, bh = self.dialog["cancel_rect"]
                img = self._cached(("dlg-cancel", cancel_state),
                                   lambda: self.painter.ghost_button(px(bw), px(bh), cancel_state))
                paste_layer(scene, img, px(bx), px(by))
                self.canvas.itemconfigure(self.dialog["cancel_text"],
                                          fill="#FFFFFF" if cancel_state == "hover" else "#CFCFCF")
        else:
            scene = self.backdrops[self._backdrop_key()].copy()
            for key, (x, y, w, h) in L_TABS.items():
                selected, hovered = key == self.tab, self.hover == f"tab:{key}"
                img, m = self._cached(("tab", key, selected, hovered), lambda: self.painter.rail_tab(
                    px(w), px(h), TAB_SPECS[key][1], selected, hovered))
                paste_layer(scene, img, px(x) - m, px(y) - m)
                if not selected:
                    self.canvas.itemconfigure(self.t_tabs[key], fill="#CFCFCF" if hovered else TEXT_MID)
                if key == "about" and self.update_state in ("available", "ready"):  # a new version is out
                    r, cx, cy = px(3.5), px(x + w - 13), px(y + 11)
                    ImageDraw.Draw(scene).ellipse((cx - r, cy - r, cx + r, cy + r), fill=STARLIGHT)
            if self.tab == "boost":
                self._render_boost(scene)
            elif self.tab == "tools":
                self._render_tools(scene)
            elif self._look_page():
                self._render_segment(scene, "set", SETTING_PAGES)
                self._render_look(scene)
            elif self.tab == "privacy":
                tw, th = L_TOGGLE
                for key, (x, y, w, h) in L_PRIV_ROWS.items():
                    target = f"priv:{key}"
                    state = "disabled" if not self._enabled(target) else (
                        "hover" if self.hover == target else "normal")
                    on = self.privacy_state.get(key, False)
                    img = self._cached(("toggle", on, state), lambda: self.painter.toggle(px(tw), px(th), on, state))
                    paste_layer(scene, img, px(x + w - 20 - tw), px(y + (h - th) / 2))
                self._render_ghost(scene, "privall", L_PRIV_ALL)
            elif self.tab == "about":
                if self.update_state in ("available", "ready"):
                    self._render_primary(scene, "update", L_ABOUT_UPDATE)
                else:
                    self._render_ghost(scene, "update", L_ABOUT_UPDATE)
                self._render_ghost(scene, "report", L_ABOUT_REPORT)
                self._render_ghost(scene, "profile", L_ABOUT_PROFILE)
                self.canvas.itemconfigure(self.t_about_link,
                                          fill=TEXT_HI if self.hover == "btn:profile" else SILVER)
            else:
                self._render_segment(scene, "set", SETTING_PAGES)
                page_rows = L_SETTING_ROWS if self.settings_page == "app" else L_WINSET_ROWS
                for key, (x, y, w, h) in page_rows.items():
                    target = f"set:{key}"
                    if not self._enabled(target):
                        state = "disabled"
                    else:
                        state = "hover" if self.hover == target else "normal"
                    on = self._setting_on(key)
                    tw, th = L_TOGGLE
                    img = self._cached(("toggle", on, state), lambda: self.painter.toggle(px(tw), px(th), on, state))
                    paste_layer(scene, img, px(x + w - 20 - tw), px(y + (h - th) / 2))
                if self.settings_page == "app":
                    for key, rect in L_SETTING_BUTTONS.items():
                        self._render_ghost(scene, key, rect)
                    if self.protection is False and self.admin:
                        self._render_ghost(scene, "protect", L_PROTECT_BUTTON)
                    self._render_ghost(scene, "hotkeys", L_HOTKEYS_BUTTON)

            for key, (x, y, w, h) in L_WINDOW_BUTTONS.items():
                target = f"win:{key}"
                state = "pressed" if self.pressed == target and self.hover == target else (
                    "hover" if self.hover == target else "normal")
                img = self._cached(("win", key, state), lambda: self.painter.window_button(px(w), px(h), key, state))
                paste_layer(scene, img, px(x), px(y))
        self._scene = scene
        self.scene_photo.paste(scene.convert("RGB"))

    def _render_segment(self, scene: Image.Image, seg: str, options) -> None:
        px = self.px
        x, y, w, h = L_SEGMENTS[seg]
        step = w / len(options)
        paste_layer(scene, self._cached(("segtrack", w, h), lambda: self.painter.seg_track(px(w), px(h))),
                    px(x), px(y))
        thumb, m = self._cached(("segthumb", step, h), lambda: self.painter.seg_thumb(px(step - 6), px(h - 6)))
        pos = self.seg_t.get(seg, self._seg_targets()[seg])
        paste_layer(scene, thumb, px(x + 3 + pos * step) - m, px(y + 3) - m)
        for i, (key, _label) in enumerate(options):
            lit = abs(pos - i) < 0.5
            hovered = self.hover == f"seg:{seg}:{key}"
            self.canvas.itemconfigure(self.t_seg[(seg, key)],
                                      fill=VOID if lit else ("#FFFFFF" if hovered else TEXT_MID))

    def _backdrop_key(self) -> str:
        if self.tab == "settings":
            return f"settings:{self.settings_page}"
        return f"tools:{self.tool_page}" if self.tab == "tools" else self.tab

    def _render_tools(self, scene: Image.Image) -> None:
        px = self.px
        self._render_segment(scene, "tool", TOOL_PAGES)
        for key, rect in TOOL_PAGE_BUTTONS[self.tool_page].items():
            if key == "addgame" and self.session_view and not self.game_finder and self.compare_pair:
                continue
            if key in PRIMARY_TOOL_BUTTONS or (key == "addgame" and self.game_finder):
                self._render_primary(scene, key, rect)
            else:
                self._render_ghost(scene, key, rect)
        tw, th = L_TOGGLE

        def toggle(target: str, on: bool, x: float, y: float) -> None:
            state = "hover" if self.hover == target else "normal"
            img = self._cached(("toggle", on, state), lambda: self.painter.toggle(px(tw), px(th), on, state))
            paste_layer(scene, img, px(x), px(y))

        if self.tool_page == "checkup":
            sx, sy, pw, ph = L_CHECK_SCORE
            for i, (key, _title) in enumerate(CHECKUP_ITEMS):
                status = (self.checkup.get(key) or {}).get("status", "pending")
                color = {"ok": STARLIGHT, "warn": WARN_YELLOW}.get(status, "#3A3A3A")
                img = self._cached(("pip", color, pw, ph), lambda: self.painter.shape(
                    px(pw), px(ph), px(ph / 2), fill=rgba(color)))
                paste_layer(scene, img, px(sx + i * (pw + 5)), px(sy))
            bw, bh = L_CHECK_BTN
            mark = 22
            for slot, key, (rx, ry, rw, rh) in self._check_slots():
                result = self.checkup.get(key)
                status = result["status"] if result else "pending"
                img = self.painter.status_mark(px(mark), status)
                paste_layer(scene, img, px(rx + 18), px(ry + rh / 2 - mark / 2))
                if result and result.get("action"):
                    target = f"chk:{key}"
                    state = "disabled" if not self._enabled(target) else (
                        "pressed" if self.pressed == target and self.hover == target else
                        "hover" if self.hover == target else "normal")
                    img = self._cached(("ghost-check", state), lambda: self.painter.ghost_button(px(bw), px(bh), state))
                    paste_layer(scene, img, px(rx + rw - 20 - bw), px(ry + (rh - bh) / 2))
                    color = {"disabled": "#4D4D4D", "hover": "#FFFFFF", "pressed": "#FFFFFF"}.get(state, "#CFCFCF")
                    self.canvas.itemconfigure(self.t_check[slot]["btn"], fill=color)
            if len(CHECKUP_ITEMS) > CHECK_VISIBLE:  # scroll position
                tx, ty, tw, th = L_CHECK_SCROLL
                track = self._cached(("chk-track", th), lambda: self.painter.shape(
                    px(tw), px(th), px(tw / 2), fill=rgba(STARLIGHT, 22)))
                paste_layer(scene, track, px(tx), px(ty))
                span = th * CHECK_VISIBLE / len(CHECKUP_ITEMS)
                top = ty + (th - span) * self.check_offset / (len(CHECKUP_ITEMS) - CHECK_VISIBLE)
                thumb = self._cached(("chk-thumb", span), lambda: self.painter.shape(
                    px(tw), px(span), px(tw / 2), fill=rgba(STARLIGHT, 120)))
                paste_layer(scene, thumb, px(tx), px(top))
        if self.tool_page == "games" and not self.game_finder:
            for i, row in enumerate(self.t_games):
                if self.hover == f"game:report:{self.game_offset + i}":
                    self.canvas.itemconfigure(row["sub"], fill=TEXT_HI)
        if self.tool_page == "ping" and self.host_focus:
            fx, fy, fw, fh = L_PING_HOST_FIELD
            ring = self._cached(("focus-ring", fw, fh), lambda: self.painter.shape(
                px(fw), px(fh), px(12), border=(255, 255, 255, 170), border_width=1.2))
            paste_layer(scene, ring, px(fx), px(fy))
        if self.tool_page == "games" and self.game_finder:
            items = self.game_finder.get("items", [])
            for i in range(min(GAME_ROWS, len(items) - self.game_offset)):
                index = self.game_offset + i
                toggle(f"find:{index}", items[index]["pick"], L_GAME_COLS["win"], L_GAME_ROW_Y0 + i * L_GAME_ROW_H + 10)
            ax, ay, aw, ah = L_GAME_AUTO
            toggle("game:auto", bool(self._setting("game_auto")), ax + 18, ay + (ah - th) / 2)
            toggle("game:overlay", bool(self._setting("overlay")), ax + L_GAME_SPLIT, ay + (ah - th) / 2)
            toggle("game:background", bool(self._setting("slow_background")), ax + L_GAME_SPLIT2, ay + (ah - th) / 2)
        elif self.tool_page == "dns":
            current = set(self.dns_info["servers"]) if self.dns_info else set()
            for i, row in enumerate(self.dns_rows):
                if i == 0 or not row["servers"] or current & set(row["servers"]):
                    continue
                target = f"dnsuse:{i}"
                state = "disabled" if not self._enabled(target) else ("hover" if self.hover == target else "normal")
                img = self._cached(("ghost-small", state), lambda: self.painter.ghost_button(px(68), px(26), state))
                paste_layer(scene, img, px(L_DNS_COLS["use"]), px(L_DNS_ROW_Y0 + i * L_DNS_ROW_H + 5))
            if self.settings_dns_original():
                self._render_ghost(scene, "dnsrestore", L_DNS_RESTORE)
        elif self.tool_page == "games" and self.session_view:
            if self.compare_pick:
                for index in self.compare_pick:
                    i = index - self.game_offset
                    if 0 <= i < GAME_ROWS:
                        rx, ry, rw, rh = L_TABLE[0] + 6, L_GAME_ROW_Y0 + i * L_GAME_ROW_H - 4, L_TABLE[2] - 12, 42
                        ring = self._cached(("pick-ring", rw, rh), lambda: self.painter.shape(
                            px(rw), px(rh), px(10), fill=rgba(STARLIGHT, 14), border=(255, 255, 255, 150),
                            border_width=1.2))
                        paste_layer(scene, ring, px(rx), px(ry))
        elif self.tool_page == "games":
            for i in range(min(GAME_ROWS, len(self.games) - self.game_offset)):
                index = self.game_offset + i
                game, ry = self.games[index], L_GAME_ROW_Y0 + i * L_GAME_ROW_H
                toggle(f"game:prio:{index}", bool(game.get("priority", True)), L_GAME_COLS["prio"], ry + 10)
                toggle(f"game:fso:{index}", bool(game.get("_fso")), L_GAME_COLS["fso"], ry + 10)
                toggle(f"game:win:{index}", bool(game.get("winkey")), L_GAME_COLS["win"], ry + 10)
                target = f"game:del:{index}"
                state = "hover" if self.hover == target else "normal"
                img = self._cached(("win", "close-row", state), lambda: self.painter.window_button(
                    px(28), px(24), "close", state))
                paste_layer(scene, img, px(L_GAME_COLS["del"]), px(ry + 10))
            ax, ay, aw, ah = L_GAME_AUTO
            toggle("game:auto", bool(self._setting("game_auto")), ax + 18, ay + (ah - th) / 2)
            toggle("game:overlay", bool(self._setting("overlay")), ax + L_GAME_SPLIT, ay + (ah - th) / 2)
            toggle("game:background", bool(self._setting("slow_background")), ax + L_GAME_SPLIT2, ay + (ah - th) / 2)
        elif self.tool_page == "startup" and self.startup_items:
            for i in range(min(START_ROWS, len(self.startup_items) - self.startup_offset)):
                index = self.startup_offset + i
                ry = L_START_ROW_Y0 + i * L_START_ROW_H
                toggle(f"startup:{index}", self.startup_items[index]["enabled"], L_START_COLS["toggle"], ry + 6)
        elif self.tool_page == "cleanup":
            skip = set(self._setting("clean_skip") or [])
            for key, (rx, ry, rw, rh) in L_CLEAN_ROWS.items():
                toggle(f"clean:{key}", key not in skip, rx + rw - 20 - tw, ry + (rh - th) / 2)
            bx, by, bw, bh = L_CLEAN_BAR
            toggle("clean:auto", bool(self._setting("clean_weekly")), bx + bw - 20 - tw, by + (bh - th) / 2)

    def _render_look(self, scene: Image.Image) -> None:
        self._render_ghost(scene, "reset", L_LOOK_RESET)
        if self.look["background"] == "image":
            self._render_ghost(scene, "image", L_LOOK_IMAGE_BTN)
        for seg, options in (("bg", BG_KINDS), ("motion", MOTION_MODES)):
            self._render_segment(scene, seg, options)
        for key in L_SLIDERS:
            self._paint_slider(key)
        px = self.px
        tw, th = L_TOGGLE
        x, _y, w, _h = L_LOOK_INTRO
        state = "hover" if self.hover == "look:intro" else "normal"
        on = bool(self.look.get("intro", True))
        img = self._cached(("toggle", on, state), lambda: self.painter.toggle(px(tw), px(th), on, state))
        paste_layer(scene, img, px(x + w - 20 - tw), px(L_LOOK_INTRO_Y - th / 2))

    def _paint_slider(self, key: str) -> None:
        """Redraw one slider's own small layer - cheap enough for every mouse move."""
        x, cy, w = L_SLIDERS[key]
        target = f"slider:{key}"
        if not self._enabled(target) and self._look_page():
            state = "disabled"
        elif self._slider_drag == key:
            state = "drag"
        else:
            state = "hover" if self.hover == target else "normal"
        if self._slider_drag == key:
            frac = self._slider_frac  # follows the pointer exactly, not the rounded value
        else:
            _label, lo, hi = SLIDER_SPECS[key]
            frac = (self._slider_value(key) - lo) / (hi - lo)
        img, m = self.painter.slider_image(self.px(w), frac, state)
        photo = self._slider_photos.get(key)
        if photo is None or (photo.width(), photo.height()) != img.size:
            photo = self._slider_photos[key] = ImageTk.PhotoImage(img)
            self.canvas.itemconfigure(self.slider_items[key], image=photo)
        else:
            photo.paste(img)
        self.canvas.coords(self.slider_items[key], self.px(x) - m, self.px(cy) - self.px(2) - m)

    def _slider_value(self, key: str) -> int:
        return int(self.look[key] if key in GLOBAL_SLIDERS else self._bg_adjust()[key])

    def _refresh_look_texts(self) -> None:
        c = self.canvas
        kind = self.look["background"]
        if kind == "image":
            hint_x, hint = L_LOOK_IMAGE_BTN[0] + L_LOOK_IMAGE_BTN[2] + 14, self.look["bg_name"] or "Your image"
        else:
            hint_x, hint = L_LOOK_ROWS["background"][0] + 20, BG_HINTS[kind]
        c.coords(self.t_bg_hint, self.px(hint_x), self.px(L_LOOK_HINT_Y))
        c.itemconfigure(self.t_bg_hint, text=self._fit(hint, self.f_desc, 686 - hint_x))
        for key in SLIDER_SPECS:
            value = self._slider_value(key)
            if key == "blur":
                text = "Off" if value == 0 else f"{value} px"
            else:
                text = f"{value}%"
            enabled = key in GLOBAL_SLIDERS or kind != "black"
            c.itemconfigure(self.t_slider_value[key], text=text if enabled else "—")
            c.itemconfigure(self.t_slider_label[key], fill=TEXT_HI if enabled else ASH)
        mode = self.look["motion"]
        if mode == "system":
            on = self._windows_motion()
            desc = ("Follows the Windows animation effects setting. It's on, so motion is full."
                    if on else "Follows the Windows animation effects setting. It's off, so motion is reduced.")
        else:
            desc = MOTION_TEXT[mode]
        c.itemconfigure(self.t_motion_desc, text=desc)
        if not self.look.get("intro", True):
            hint = "Off. Astra opens straight to the app."
        elif self._motion_level() == "off":
            hint = "Skipped while Motion is Off."
        elif self._motion_level() == "reduced":
            hint = "A short fade when Astra opens. Click to skip."
        else:
            hint = "Plays when Astra opens. Click or press a key to skip."
        c.itemconfigure(self.t_intro_hint, text=hint)

    def _render_boost(self, scene: Image.Image) -> None:
        px = self.px
        for key, _title, _purpose, _desc, accent, icon in CARD_SPECS:
            x, y, w, h = L_CARDS[key]
            level = round(self.card_t[key] * 14) / 14
            selected = key == self.profile
            grow = self._level == "full"
            img, m = self._cached(("card", key, level, selected, grow), lambda: self.painter.card(
                px(w), px(h), accent, level, selected, icon, grow))
            paste_layer(scene, img, px(x) - m, px(y) - m)
        self._render_primary(scene, "optimize", L_BUTTONS["optimize"], round(self.bloom_t * 10) / 10)
        self._render_ghost(scene, "defaults", L_BUTTONS["defaults"])

    def _render_primary(self, scene: Image.Image, key: str, rect, bloom: float | None = None) -> None:
        px = self.px
        x, y, w, h = rect
        state = self._button_state(key)
        if bloom is None:
            bloom = 1.0 if state in ("hover", "busy") else 0.0
        phase = round(self.shimmer * 30) / 30 if state == "busy" and self._level == "full" else None
        img, m = self._cached(("primary", key, state, bloom, phase), lambda: self.painter.primary_button(
            px(w), px(h), state, bloom, phase))
        paste_layer(scene, img, px(x) - m, px(y) - m)
        self.canvas.itemconfigure(self.t_buttons[key], fill="#4D4D4D" if state == "disabled" else VOID)

    def _render_ghost(self, scene: Image.Image, key: str, rect) -> None:
        px = self.px
        x, y, w, h = rect
        state = self._button_state(key)
        img = self._cached(("ghost", key, state), lambda: self.painter.ghost_button(px(w), px(h), state))
        paste_layer(scene, img, px(x), px(y))
        color = {"disabled": "#4D4D4D", "hover": "#FFFFFF", "pressed": "#FFFFFF"}.get(state, "#CFCFCF")
        self.canvas.itemconfigure(self.t_buttons[key], fill=color)

    def _setting_on(self, key: str) -> bool:
        """What the switch shows: the live state, not just the saved preference."""
        if key == "restore":
            return bool(self._setting("restore_point"))
        if key == "tray":
            return self.tray is not None and self.tray.running
        if key == "hotkey":
            return self.hotkey is not None
        if key == "autostart":
            return bool(self._autostart_desired if self._autostart_pending else self._setting("autostart"))
        if key == "mouse":
            values = mouse_accel_get()
            return values is not None and values[2] == 0
        if key == "pointer":
            return pointer_speed_get() == POINTER_SPEED_1TO1
        if key == "sticky":
            return not access_shortcuts_on()
        if key in WINSET_REG:
            hive, path, name, on_value, _off = WINSET_REG[key]
            current = SystemTweaks.reg_read(hive, path, name)
            return current is not None and current[0] == on_value
        return bool(self._setting(key))

    # ---------------- input ----------------

    def _hit(self, ex: int, ey: int) -> str | None:
        lx, ly = ex / self.scale, ey / self.scale

        def inside(rect) -> bool:
            x, y, w, h = rect
            return x <= lx <= x + w and y <= ly <= y + h

        if self.dialog:
            if inside(self.dialog["ok_rect"]):
                return "dlg:ok"
            if self.dialog["cancel_rect"] and inside(self.dialog["cancel_rect"]):
                return "dlg:cancel"
            if self.dialog["needs_check"] and inside(self.dialog["check_rect"]):
                return "dlg:check"
            for action, chip in (self.dialog.get("chips") or {}).items():
                if inside(chip["rect"]):
                    return f"dlg:chip:{action}"
            return "drag" if ly < L_TITLE_H else None
        for key, rect in L_WINDOW_BUTTONS.items():
            if inside(rect):
                return f"win:{key}"
        if ly < L_TITLE_H:
            return "drag"
        for key, rect in L_TABS.items():
            if inside(rect):
                return f"tab:{key}"
        if self.tab == "boost":
            for key, rect in L_CARDS.items():
                if inside(rect):
                    return f"card:{key}"
            for key, rect in L_BUTTONS.items():
                if inside(rect):
                    return f"btn:{key}"
        elif self.tab == "tools":
            x, y, w, h = L_SEGMENTS["tool"]
            if inside((x, y, w, h)):
                index = min(len(TOOL_PAGES) - 1, max(0, int((lx - x) / (w / len(TOOL_PAGES)))))
                return f"seg:tool:{TOOL_PAGES[index][0]}"
            for key, rect in TOOL_PAGE_BUTTONS[self.tool_page].items():
                if key == "addgame" and self.session_view and not self.game_finder and self.compare_pair:
                    continue
                if inside(rect):
                    return f"btn:{key}"
            if self.tool_page == "games" and self.session_view and not self.game_finder:
                if self.compare_pair:
                    return None
                sessions = self._game_sessions(self.session_view)
                for i in range(min(GAME_ROWS, len(sessions) - self.game_offset)):
                    ry = L_GAME_ROW_Y0 + i * L_GAME_ROW_H
                    if inside((L_TABLE[0] + 6, ry - 2, L_TABLE[2] - 12, L_GAME_ROW_H)):
                        return f"hist:{self.game_offset + i}"
                return None
            if self.tool_page == "ping" and inside(L_PING_HOST_FIELD):
                return "host:field"
            if self.tool_page == "checkup":
                bw, bh = L_CHECK_BTN
                for _slot, key, (rx, ry, rw, rh) in self._check_slots():
                    if (self.checkup.get(key) or {}).get("action") \
                            and inside((rx + rw - 20 - bw, ry + (rh - bh) / 2, bw, bh)):
                        return f"chk:{key}"
            if self.tool_page == "games" and self.game_finder:
                items = self.game_finder.get("items", [])
                for i in range(min(GAME_ROWS, len(items) - self.game_offset)):
                    ry = L_GAME_ROW_Y0 + i * L_GAME_ROW_H
                    if inside((L_TABLE[0] + 6, ry - 2, L_TABLE[2] - 12, L_GAME_ROW_H)):
                        return f"find:{self.game_offset + i}"
                if inside(L_GAME_AUTO):
                    return self._game_switch_at(lx)
            elif self.tool_page == "dns":
                current = set(self.dns_info["servers"]) if self.dns_info else set()
                for i, row in enumerate(self.dns_rows):
                    ry = L_DNS_ROW_Y0 + i * L_DNS_ROW_H
                    if i and row["servers"] and not current & set(row["servers"]) \
                            and inside((L_DNS_COLS["use"], ry + 5, 68, 26)):
                        return f"dnsuse:{i}"
                if self.settings_dns_original() and inside(L_DNS_RESTORE):
                    return "btn:dnsrestore"
            elif self.tool_page == "games":
                for i in range(min(GAME_ROWS, len(self.games) - self.game_offset)):
                    index, ry = self.game_offset + i, L_GAME_ROW_Y0 + i * L_GAME_ROW_H
                    if inside((L_GAME_COLS["prio"] - 8, ry + 4, 60, 36)):
                        return f"game:prio:{index}"
                    if inside((L_GAME_COLS["fso"] - 8, ry + 4, 60, 36)):
                        return f"game:fso:{index}"
                    if inside((L_GAME_COLS["win"] - 8, ry + 4, 60, 36)):
                        return f"game:win:{index}"
                    if inside((L_GAME_COLS["del"] - 4, ry + 6, 36, 32)):
                        return f"game:del:{index}"
                    if inside((L_GAME_COLS["name"] - 6, ry + 2, L_GAME_NAME_W + 12, L_GAME_ROW_H - 4)) \
                            and self._last_session(self.games[index]["path"]):
                        return f"game:report:{index}"
                if inside(L_GAME_AUTO):
                    return self._game_switch_at(lx)
            elif self.tool_page == "startup" and self.startup_items:
                for i in range(min(START_ROWS, len(self.startup_items) - self.startup_offset)):
                    ry = L_START_ROW_Y0 + i * L_START_ROW_H
                    if inside((L_TABLE[0] + 6, ry - 2, L_TABLE[2] - 12, L_START_ROW_H)):
                        return f"startup:{self.startup_offset + i}"
            elif self.tool_page == "cleanup":
                for key, rect in L_CLEAN_ROWS.items():
                    if inside(rect):
                        return f"clean:{key}"
                bx, by, bw, bh = L_CLEAN_BAR
                if inside((bx + bw - 250, by, 250, bh)):
                    return "clean:auto"
        elif self.tab == "privacy":
            if inside(L_PRIV_ALL):
                return "btn:privall"
            for key, rect in L_PRIV_ROWS.items():
                if inside(rect):
                    return f"priv:{key}"
        elif self.tab == "about":
            if inside(L_ABOUT_UPDATE):
                return "btn:update"
            if inside(L_ABOUT_REPORT):
                return "btn:report"
            if inside(L_ABOUT_PROFILE) or inside(L_ABOUT_LINK):
                return "btn:profile"
        elif self._look_page():
            x, y, w, h = L_SEGMENTS["set"]
            if inside((x, y, w, h)):
                index = min(len(SETTING_PAGES) - 1, max(0, int((lx - x) / (w / len(SETTING_PAGES)))))
                return f"seg:set:{SETTING_PAGES[index][0]}"
            if inside(L_LOOK_RESET):
                return "btn:reset"
            if self.look["background"] == "image" and inside(L_LOOK_IMAGE_BTN):
                return "btn:image"
            for seg, options in (("bg", BG_KINDS), ("motion", MOTION_MODES)):
                x, y, w, h = L_SEGMENTS[seg]
                if inside((x, y, w, h)):
                    index = min(len(options) - 1, max(0, int((lx - x) / (w / len(options)))))
                    return f"seg:{seg}:{options[index][0]}"
            for key, (x, cy, w) in L_SLIDERS.items():
                if inside((x - 12, cy - 14, w + 24, 28)):
                    return f"slider:{key}"
            if inside(L_LOOK_INTRO):
                return "look:intro"
        else:
            x, y, w, h = L_SEGMENTS["set"]
            if inside((x, y, w, h)):
                index = min(len(SETTING_PAGES) - 1, max(0, int((lx - x) / (w / len(SETTING_PAGES)))))
                return f"seg:set:{SETTING_PAGES[index][0]}"
            if self.settings_page == "windows":
                for key, rect in L_WINSET_ROWS.items():
                    if inside(rect):
                        return f"set:{key}"
                return None
            for key, rect in L_SETTING_BUTTONS.items():
                if inside(rect):
                    return f"btn:{key}"
            if self.protection is False and self.admin and inside(L_PROTECT_BUTTON):
                return "btn:protect"
            if inside(L_HOTKEYS_BUTTON):
                return "btn:hotkeys"
            for key, rect in L_SETTING_ROWS.items():
                if inside(rect):
                    return f"set:{key}"
        return None

    def _set_hover(self, target: str | None) -> None:
        if target == self.hover:
            return
        self.hover = target
        if target == "host:field":
            self.canvas.configure(cursor="xterm")
        else:
            self.canvas.configure(cursor="hand2" if self._enabled(target) else "")
        self._render()
        self._kick()

    def _on_motion(self, event) -> None:
        if self._slider_drag is not None:
            self._slider_set(event.x)
            return
        if self._look_page():
            target = self._hit(event.x, event.y)
            if target != self.hover and ((target or "").startswith("slider:")
                                         or (self.hover or "").startswith("slider:")):
                previous, self.hover = self.hover, target  # slider hover: repaint just those layers
                self.canvas.configure(cursor="hand2" if self._enabled(target) else "")
                for item in (previous, target):
                    if item and item.startswith("slider:"):
                        self._paint_slider(item.partition(":")[2])
                return
        if self._drag_origin is not None:
            px0, py0, wx0, wy0 = self._drag_origin
            tk.Tk.wm_geometry(self, f"+{wx0 + event.x_root - px0}+{wy0 + event.y_root - py0}")
            return
        self._set_hover(self._hit(event.x, event.y))

    def _on_leave(self, _event) -> None:
        self._set_hover(None)

    def _on_press(self, event) -> None:
        target = self._hit(event.x, event.y)
        if target == "host:field":
            self._focus_host(event.x / self.scale)
            return
        if self._typing():
            self._blur_host()
        if target == "drag":
            self._drag_origin = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())
            return
        if target and target.startswith("slider:") and self._enabled(target):
            self._slider_drag = target.partition(":")[2]
            self._slider_set(event.x)
            self._paint_slider(self._slider_drag)
            return
        if self._enabled(target):
            self.pressed = target
            self._render()

    def _on_release(self, event) -> None:
        if self._drag_origin is not None:
            self._drag_origin = None
            return
        if self._slider_drag is not None:
            key, self._slider_drag = self._slider_drag, None
            self._flush_backdrop()
            self._save_look()
            self.hover = self._hit(event.x, event.y)
            self._paint_slider(key)
            return
        target = self._hit(event.x, event.y)
        pressed, self.pressed = self.pressed, None
        if pressed and pressed == target and self._enabled(target):
            self._activate(target)
        self._render()

    def _slider_set(self, ex: float) -> None:
        key = self._slider_drag
        x, _cy, w = L_SLIDERS[key]
        _label, lo, hi = SLIDER_SPECS[key]
        self._slider_frac = min(1.0, max(0.0, (ex / self.scale - x) / w))
        self._slider_apply(key, int(round(lo + self._slider_frac * (hi - lo))), live=True)
        self._paint_slider(key)

    def _slider_apply(self, key: str, value: int, live: bool = False) -> None:
        """live (dragging): only the value text and knob update now; the background preview waits
        until the pointer rests, so the drag itself never stalls on a rebuild."""
        _label, lo, hi = SLIDER_SPECS[key]
        value = int(min(hi, max(lo, value)))
        if value == self._slider_value(key):
            return
        if key in GLOBAL_SLIDERS:
            self.look[key] = value
        else:
            self.look["adjust"][self.look["background"]][key] = value
        self._refresh_look_texts()
        if live:
            self._schedule_backdrop()
        else:
            self._apply_look()

    def _on_arrow(self, step: int) -> None:
        if self._typing() or self.dialog:
            return
        if self.tab == "boost":
            self._cycle_profile(step)
        elif self._look_page() and self.hover and self.hover.startswith("slider:") \
                and self._enabled(self.hover):
            key = self.hover.partition(":")[2]
            self._slider_apply(key, self._slider_value(key) + step)
            self._save_look()

    def _choose_segment(self, seg: str, key: str) -> None:
        if seg == "tool":
            self._switch_tool_page(key)
            return
        if seg == "set":
            self._switch_settings_page(key)
            return
        if seg == "motion":
            if self.look["motion"] == key:
                return
            self.look["motion"] = key
            self._save_look()
            self._level = self._motion_level()
            self._cache.clear()
            self._refresh_look_texts()
            self._emit("ok", f"Motion set to {dict(MOTION_MODES)[key]}")
            self._render()
            self._kick()
            return
        if key == self.look["background"]:
            return
        if key == "image" and not os.path.isfile(self._bg_path()) and not self._pick_background():
            return
        self.look["background"] = key
        self._save_look()
        self._apply_look(fade=True)
        self._kick()

    def _toggle_intro(self) -> None:
        self.look["intro"] = not self.look.get("intro", True)
        self._save_look()
        self._refresh_look_texts()
        self._emit("ok", "Startup intro turned " + ("on" if self.look["intro"] else "off"))
        self._render()

    def _reset_look(self) -> None:
        name = self.look.get("bg_name", "")
        self.look = json.loads(json.dumps(DEFAULT_LOOK))
        self.look["bg_name"] = name  # the copied image stays, so "Image" brings it back
        self._save_look()
        self._apply_look(fade=True)
        self._kick()
        self._emit("ok", "Appearance reset to the starfield, glass panels and Windows motion")

    def _activate(self, target: str) -> None:
        kind, _, key = target.partition(":")
        if kind == "card":
            self._select_profile(key)
        elif kind == "seg":
            seg, _, option = key.partition(":")
            self._choose_segment(seg, option)
        elif target == "btn:reset":
            self._reset_look()
        elif target == "look:intro":
            self._toggle_intro()
        elif target == "btn:image":
            if self._pick_background():
                self._save_look()
                self._apply_look(fade=True)
        elif kind == "tab":
            self._switch_tab(key)
        elif kind == "set":
            self._toggle_setting(key)
        elif target == "btn:optimize":
            self._on_primary()
        elif target == "btn:defaults":
            self._on_restore()
        elif target == "btn:ping":
            self._start_ping()
        elif target == "btn:host":
            self._use_custom_host()
        elif target == "btn:folder":
            self._open_state_folder()
        elif target == "btn:backup":
            self._export_backup()
        elif target == "btn:restore":
            self._import_backup()
        elif target == "btn:profile":
            self._open_link(CREDIT_URL)
        elif target == "btn:report":
            self._report_problem()
        elif target == "btn:update":
            if self.update_state == "ready":
                self._confirm_update_restart()
            elif self.update_state == "available" and self.update_info:
                if self._can_self_update():
                    self._start_update_download()
                else:
                    self._open_link(self.update_info["url"])
            else:
                self._check_updates(manual=True)
        elif target == "btn:hotkeys":
            self._open_hotkeys()
        elif target == "btn:protect":
            self._confirm_protection()
        elif kind == "game":
            what, _, index = key.partition(":")
            self._game_action(what, int(index) if index else -1)
        elif kind == "chk":
            self._checkup_action(key)
        elif kind == "hist":
            sessions = self._game_sessions(self.session_view or "")
            if self.compare_pick is not None:
                self._pick_session(int(key))
            elif 0 <= int(key) < len(sessions):
                self._show_session_report(sessions[int(key)])
        elif kind == "priv":
            self._toggle_privacy([key], not self.privacy_state.get(key, False))
        elif target == "btn:privall":
            self._toggle_privacy([k for k in PRIVACY_KEYS if not self.privacy_state.get(k)
                                  and (self.admin or not privacy_needs_admin(k))], True)
        elif target == "btn:checkup":
            self._run_checkup()
        elif kind == "startup":
            self._toggle_startup(int(key))
        elif kind == "clean":
            self._toggle_clean_target(key)
        elif target == "btn:addgame":
            if self.session_view and not self.game_finder:
                self._toggle_compare_pick()
            else:
                self._add_found_games() if self.game_finder else self._add_game()
        elif target == "btn:refresh":
            self._load_startup()
        elif target == "btn:clean":
            self._confirm_cleanup()
        elif kind == "find":
            self._toggle_found_game(int(key))
        elif target == "btn:findgames":
            self._find_games()
        elif kind == "dnsuse":
            self._confirm_dns_switch(int(key))
        elif target == "btn:dnstest":
            self._start_dns_test()
        elif target == "btn:dnsrestore":
            self._confirm_dns_restore()
        elif kind == "win":
            if key == "min":
                self._minimize()
            else:
                self._on_close()
        elif target == "dlg:check" and self.dialog:
            self.dialog["checked"] = not self.dialog["checked"]
            self.canvas.configure(cursor="hand2")
            self._render()
        elif kind == "dlg" and self.dialog and key.startswith("chip:"):
            self._record_hotkey_start(key[5:])
        elif kind == "dlg" and self.dialog and key == "cancel" and self.dialog.get("on_cancel"):
            self.dialog["on_cancel"]()
        elif kind == "dlg" and self.dialog:
            if key == "ok" and not self._enabled("dlg:ok"):
                return
            on_ok = self.dialog["on_ok"] if key == "ok" else None
            self._close_dialog()
            if on_ok:
                on_ok()

    def _typing(self) -> bool:
        try:
            return bool(self.host_focus)
        except (tk.TclError, KeyError, AttributeError):
            return False

    def _on_return(self, _event) -> None:
        if self.dialog and self.dialog.get("recording"):
            return
        if self.dialog:
            self._activate("dlg:ok")
        elif self._typing():
            return  # the entry's own binding handles it
        elif self.tab == "boost" and self._enabled("btn:optimize"):
            self._on_primary()
        elif self.tab == "tools" and self._enabled("btn:ping"):
            self._start_ping()

    def _on_space(self, _event) -> None:
        if self.dialog and self.dialog["needs_check"]:
            self._activate("dlg:check")

    def _on_escape(self, _event) -> None:
        if self.dialog and self.dialog.get("recording"):
            self._record_hotkey_stop()
            self._hotkey_status("")
        elif self.dialog:
            self._close_dialog()
        elif self._typing():
            self._blur_host()

    def _select_profile(self, key: str) -> None:
        if self.busy or self.dialog or key == self.profile or key not in PROFILE_ORDER:
            return
        self.profile = key
        applied = self.state_store.get("applied_profile")
        if self.undo_count and applied in PROFILE_ORDER and key != applied:
            self._emit("info", f"{PROFILE_TITLES[applied]} is applied - undo it first to switch to {PROFILE_TITLES[key]}")
        self._tray_refresh()
        self._render()
        self._kick()

    def _cycle_profile(self, step: int) -> None:
        if self.tab != "boost":
            return
        index = PROFILE_ORDER.index(self.profile)
        self._select_profile(PROFILE_ORDER[(index + step) % len(PROFILE_ORDER)])

    def _look_page(self) -> bool:
        """Appearance is the third page of the Settings tab."""
        return self.tab == "settings" and self.settings_page == "appearance"

    def _switch_tab(self, key: str) -> None:
        if key == "appearance":  # the old tab's name still leads to it
            if self.tab == "settings":
                self._switch_settings_page("appearance")
                return
            self.settings_page = "appearance"
            key = "settings"
        if key == self.tab or self.dialog or key not in TAB_ORDER:
            return
        if self._typing():
            self.canvas.focus_set()

        def change() -> None:
            self.tab = key
            self.pressed = None
            self._apply_visibility()
            self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
            self._render()

        self._transition(change)
        self._kick()
        if key == "tools" and self.tool_page == "checkup":
            self._maybe_auto_checkup()
        if key == "privacy":
            self._refresh_privacy()
            self._render()

    def _transition(self, change) -> None:
        """Crossfade the whole window - text, log and widgets included - into the result of change().

        A snapshot of the window as it looks now goes into a click-through overlay sitting exactly on
        top; change() swaps the real UI underneath, and the snapshot fades out. Nothing is hidden or
        re-shown, so there is no flicker. About 180 ms on Full, 90 ms on Reduced, instant on Off."""
        level = self._motion_level()
        if level == "off" or self._fade is not None or not self._can_fade():
            change()
            return
        snapshot = None
        try:
            self.update_idletasks()
            snapshot = capture_window(self._hwnd())
        except Exception:
            snapshot = None
        if snapshot is None or snapshot.size != (self.px(WINDOW_W), self.px(WINDOW_H)) or not self._show_overlay(snapshot):
            change()
            return
        self._fade = {"i": 0, "n": 11 if level == "full" else 6}
        try:
            change()
        finally:
            self.after(16, self._fade_step)

    def _can_fade(self) -> bool:
        try:
            return self.state() == "normal" and bool(self.winfo_viewable()) and not user32.IsIconic(self._hwnd())
        except tk.TclError:
            return False

    def _show_overlay(self, image: Image.Image) -> bool:
        try:
            if self._overlay is None:
                win = tk.Toplevel(self)
                win.overrideredirect(True)
                win.configure(bg=VOID)
                win.geometry(f"{image.width}x{image.height}+-32000+-32000")
                label = tk.Label(win, bd=0, highlightthickness=0, bg=VOID)
                label.place(x=0, y=0)
                win.attributes("-alpha", 0.0)
                win.update_idletasks()
                hwnd = int(win.wm_frame(), 16)
                ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                         ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
                self._overlay = {"win": win, "label": label, "hwnd": hwnd, "photo": None}
            overlay = self._overlay
            overlay["photo"] = ImageTk.PhotoImage(image)
            overlay["label"].configure(image=overlay["photo"])
            overlay["win"].attributes("-alpha", 0.0)
            user32.SetWindowPos(overlay["hwnd"], None, self.winfo_rootx(), self.winfo_rooty(),
                                image.width, image.height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            overlay["win"].update_idletasks()
            overlay["label"].update()  # paint the new snapshot while still invisible
            overlay["win"].attributes("-alpha", 1.0)
            return True
        except Exception:
            self._hide_overlay()
            return False

    def _hide_overlay(self) -> None:
        if self._overlay is None:
            return
        try:
            self._overlay["win"].attributes("-alpha", 0.0)
            user32.ShowWindow(self._overlay["hwnd"], SW_HIDE)
        except Exception:
            pass

    def _fade_step(self) -> None:
        fade = self._fade
        if fade is None:
            return
        fade["i"] += 1
        if fade["i"] >= fade["n"] or self._closing:
            self._fade = None
            self._hide_overlay()
            return
        t = fade["i"] / fade["n"]
        try:
            self._overlay["win"].attributes("-alpha", (1 - t) ** 2.2)  # ease-out reveal
        except (tk.TclError, TypeError):
            self._fade = None
            self._hide_overlay()
            return
        self.after(16, self._fade_step)

    def _switch_settings_page(self, page: str) -> None:
        if page == self.settings_page or page not in dict(SETTING_PAGES):
            return

        def change() -> None:
            self.settings_page = page
            self._apply_visibility()
            self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
            self._render()

        self._transition(change)
        self._kick()

    def _switch_tool_page(self, page: str) -> None:
        if page == self.tool_page or page not in dict(TOOL_PAGES):
            return
        if self._typing():
            self.canvas.focus_set()

        def change() -> None:
            self.tool_page = page
            self._apply_visibility()
            self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
            self._render()

        self._transition(change)
        self._kick()
        if page == "checkup":
            self._maybe_auto_checkup()
        if page == "startup" and self.startup_items is None:
            self.after(30, self._load_startup)
        if page == "cleanup" and self.clean_state == "idle" and time.monotonic() - self.clean_scanned_at > 60:
            self.after(30, self._scan_cleanup)
        if page == "dns" and self.dns_info is None and self.dns_state == "idle":
            self.after(30, self._load_dns_info)

    def _on_canvas_wheel(self, event) -> None:
        if self.dialog or self.tab != "tools" or self.tool_page not in ("games", "startup", "checkup"):
            return
        step = -1 if event.delta > 0 else 1
        if self.tool_page == "checkup":
            self.check_offset = max(0, min(len(CHECKUP_ITEMS) - CHECK_VISIBLE, self.check_offset + step))
            self._paint_checkup()
        elif self.tool_page == "games":
            if self.game_finder:
                total = len(self.game_finder.get("items", []))
            elif self.session_view:
                total = 0 if self.compare_pair else len(self._game_sessions(self.session_view))
            else:
                total = len(self.games)
            self.game_offset = max(0, min(max(0, total - GAME_ROWS), self.game_offset + step))
            self._paint_games()
        else:
            total = len(self.startup_items or [])
            self.startup_offset = max(0, min(max(0, total - START_ROWS), self.startup_offset + step))
            self._paint_startup()
        self._set_hover(self._hit(event.x, event.y))
        self._render()

    def _cycle_tab(self, step: int) -> str:
        self._switch_tab(TAB_ORDER[(TAB_ORDER.index(self.tab) + step) % len(TAB_ORDER)])
        return "break"

    # ---------------- dialogs (in-app, themed) ----------------

    def _text_height(self, text: str, font: tkfont.Font, width: float) -> float:
        """Logical height a wrapped text block will take."""
        # Measured off-canvas: Tk reports no bbox for hidden items.
        probe = self.canvas.create_text(-10000, -10000, anchor="nw", text=text, font=font, width=self.px(width))
        bbox = self.canvas.bbox(probe)
        self.canvas.delete(probe)
        return ((bbox[3] - bbox[1]) if bbox else 0) / self.scale

    def _open_dialog(self, title: str, body: str, ok_label: str, on_ok, cancel_label: str | None = "Cancel",
                     confirm_text: str | None = None, width: int = L_DIALOG_W, extra_h: int = 0,
                     on_cancel=None) -> None:
        """Themed modal. With confirm_text, the confirm button stays disabled until the box is ticked."""
        px = self.px
        backdrop = self._scene.filter(ImageFilter.GaussianBlur(px(4)))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.38)
        w = width
        inner = w - 52
        body_h = self._text_height(body, self.f_dialog_body, inner)
        check_h = 0.0
        if confirm_text:
            check_h = max(20.0, self._text_height(confirm_text, self.f_dialog_body, inner - 32)) + 18
        h = round(60 + body_h + check_h + extra_h + 24 + 40 + 24)
        x, y = (WINDOW_W - w) // 2, max(L_TITLE_H + 8, (WINDOW_H - h) // 2)
        ok_rect = (x + w - 26 - 176, y + h - 24 - 40, 176, 40)
        cancel_rect = (ok_rect[0] - 12 - 112, ok_rect[1], 112, 40) if cancel_label else None

        c = self.canvas
        c.create_text(px(x + 26), px(y + 24), anchor="nw", text=title, font=self.f_dialog_title, fill=TEXT_HI,
                      tags="dialog")
        c.create_text(px(x + 26), px(y + 60), anchor="nw", text=body, font=self.f_dialog_body, fill=TEXT_MID,
                      width=px(inner), tags="dialog")
        box = check_rect = None
        if confirm_text:
            cy = y + 60 + body_h + 18
            box = (x + 26, cy, 20)
            c.create_text(px(x + 26 + 32), px(cy + 1), anchor="nw", text=confirm_text, font=self.f_dialog_body,
                          fill=TEXT_HI, width=px(inner - 32), tags="dialog")
            check_rect = (x + 20, cy - 6, inner + 12, check_h - 6)
        ok_text = c.create_text(px(ok_rect[0] + ok_rect[2] / 2), px(ok_rect[1] + ok_rect[3] / 2), text=ok_label,
                                font=self.f_ghost, fill=VOID, tags="dialog")
        cancel_text = None
        if cancel_rect:
            cancel_text = c.create_text(px(cancel_rect[0] + cancel_rect[2] / 2), px(cancel_rect[1] + cancel_rect[3] / 2),
                                        text=cancel_label, font=self.f_ghost, fill="#CFCFCF", tags="dialog")
        self.hover = self.pressed = None
        self.canvas.configure(cursor="")
        self.dialog = {"backdrop": backdrop, "on_ok": on_ok, "rect": (x, y, w, h), "ok_rect": ok_rect,
                       "cancel_rect": cancel_rect, "ok_text": ok_text, "cancel_text": cancel_text,
                       "needs_check": bool(confirm_text), "checked": False, "box": box, "check_rect": check_rect,
                       "body_bottom": y + 60 + body_h + check_h, "on_cancel": on_cancel}
        self.canvas.focus_set()
        self._transition(lambda: (self._apply_visibility(), self._render()))

    def _close_dialog(self) -> None:
        if not self.dialog:
            return
        recording = self.dialog.get("recording")
        self.dialog = None
        if recording:
            self._restart_hotkeys()
        self.canvas.delete("dialog")
        self.hover = self.pressed = None
        self._transition(lambda: (self._apply_visibility(), self._render()))
        self._kick()

    # ---------------- actions (bound to the backend) ----------------

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._refresh_primary()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()
        self._kick()

    def _refresh_primary(self) -> None:
        """The main button optimizes, and turns into UNDO CHANGES while changes are applied."""
        self.undo_count = self.state_store.snapshot_count()
        if self.busy:
            text = self._busy_label
        elif self.undo_count:
            text = "UNDO CHANGES"
        else:
            text = "OPTIMIZE NOW"
        self.canvas.itemconfigure(self.t_buttons["optimize"], text=text)
        applied = self.state_store.get("applied_profile") if self.undo_count else None
        for key, _title, purpose, _desc, _accent, _icon in CARD_SPECS:
            active = key == applied
            self.canvas.itemconfigure(self.t_purpose[key], text="Active now" if active else purpose,
                                      fill=TEXT_HI if active else SILVER)
        self._tray_refresh()

    def _on_primary(self) -> None:
        self._refresh_primary()
        if self.undo_count:
            self._on_undo()
        else:
            self._on_optimize(confirm=True)

    def _on_optimize(self, confirm: bool = False) -> None:
        if not self.admin or self.busy:
            return
        if confirm and self._setting("preview"):
            title = PROFILE_TITLES[self.profile]
            try:
                lines = SystemTweaks.plan(self.profile)
            except Exception as exc:
                lines = [f"Could not read your current settings ({exc.__class__.__name__}). "
                         "Every change will still be listed in the log."]
            if self._restore_point_due():
                lines.insert(0, "Save a System Restore point first (can take up to a minute)")
            body = "\n".join(f"\u2022  {line}" for line in lines)
            body += "\n\nEverything except the RAM cleanup and DNS flush can be undone."
            self._open_dialog(f"Apply {title}?", body, f"Apply {title}", self._on_optimize, width=500)
            return
        actions = {
            "max": SystemTweaks.profile_max_fps,
            "cozy": SystemTweaks.profile_cozy,
            "quality": SystemTweaks.profile_quality,
        }
        action = actions[self.profile]
        if self._restore_point_due():
            profile_action = action

            def action(tweaks: SystemTweaks) -> None:
                self.events.put(("busy-label", "SAVING RESTORE POINT…"))
                self.events.put(("restore-point", create_restore_point(tweaks.log)))
                self.events.put(("busy-label", "OPTIMIZING…"))
                profile_action(tweaks)

        self._start_job(f"{PROFILE_TITLES[self.profile]} profile", action,
                        busy_label="OPTIMIZING…", applies=self.profile)

    def _restore_point_due(self) -> bool:
        """Only before a fresh optimize (nothing applied yet), at most once a day, when it can work."""
        if not (self.admin and self._setting("restore_point")) or self.protection is False:
            return False
        if self.state_store.snapshot_count():
            return False
        return time.time() - float(self._setting("restore_point_at") or 0) > RESTORE_POINT_EVERY

    def _check_protection(self) -> None:
        def work() -> None:
            self.events.put(("protection", system_protection_on()))

        threading.Thread(target=work, name="protection", daemon=True).start()

    def _on_protection(self, state) -> None:
        self.protection = state
        self._refresh_restore_row()
        self._apply_visibility()
        self._render()

    def _confirm_protection(self) -> None:
        self._open_dialog(
            f"Turn on System Protection for {SYSTEM_DRIVE}?",
            "Windows will keep restore points on this drive, using a small slice of its space that it manages "
            "itself. Astra Optimizer can then save one before optimizing. You can turn it off again in Windows' "
            "System Protection settings.",
            "Turn on", self._enable_protection)

    def _enable_protection(self) -> None:
        self._protect_pending = True
        self._set_setting_desc("restore", "Turning on System Protection…")
        self._render()

        def work() -> None:
            ok, message = enable_system_protection()
            self.events.put(("protection-enabled", ok, message))

        threading.Thread(target=work, name="protection-on", daemon=True).start()

    def _on_protection_enabled(self, ok: bool, message: str) -> None:
        self._protect_pending = False
        if ok:
            self._emit("ok", f"System Protection is on for {SYSTEM_DRIVE}")
        else:
            self._emit("fail", f"Could not turn on System Protection: {message}")
        self._check_protection()

    def _on_undo(self) -> None:
        self._refresh_primary()
        if not (self.admin and not self.busy and self.undo_count):
            return
        count = self.undo_count
        self._open_dialog(
            f"Undo {count} change{'s' if count != 1 else ''}?",
            "Your power plan, visual effects, Game Bar, GPU scheduling and services go back to exactly "
            "how they were before you first optimized.",
            "Undo changes",
            lambda: self._start_job("Undo changes", SystemTweaks.undo_changes, busy_label="UNDOING…"),
        )

    def _on_restore(self) -> None:
        if not self._enabled("btn:defaults"):
            return
        self._open_dialog(
            "Reset to Windows defaults?",
            "This switches to the Balanced plan and turns back on services you may have disabled yourself, "
            "such as telemetry. You can still undo it afterwards.",
            "Reset to defaults",
            lambda: self._start_job("Windows defaults", SystemTweaks.restore_defaults,
                                    busy_label="RESETTING…", applies="defaults"),
            confirm_text="I understand this also resets settings I changed myself, not just Astra Optimizer's.",
        )

    def _start_job(self, title: str, action, busy_label: str = "WORKING…", applies: str | None = None) -> None:
        """applies: what is active afterwards (a profile key, 'defaults', or None for undo)."""
        self._busy_label = busy_label
        self._pending_applied = applies
        self._set_busy(True)
        self._emit("head", f"{title} started")
        worker = threading.Thread(target=self._run_job, args=(title, action), name="optimizer-job", daemon=True)
        worker.start()

    def _run_job(self, title: str, action) -> None:
        log = RunLog(self._emit)
        tweaks = SystemTweaks(log, self.state_store)
        started = time.monotonic()
        try:
            action(tweaks)
        except Exception as exc:
            log.fail(f"Unexpected error: {exc.__class__.__name__}: {exc}. Report a problem on the About tab "
                     "sends the details.")
            write_crash_log(f"in {title}:\n{traceback.format_exc()}")
        finally:
            elapsed = time.monotonic() - started
            self._emit(
                "head",
                f"{title} finished in {elapsed:.1f}s: {log.ok_count} done, "
                f"{log.warn_count} warnings, {log.fail_count} errors",
            )
            self.events.put(("done", (log.ok_count, log.warn_count, log.fail_count), dict(tweaks.stats)))

    # ---------------- event pump (UI thread only) ----------------

    def _emit(self, level: str, message: str) -> None:
        """Thread-safe: queue a log line for the UI and append it to the log file."""
        self.events.put(("log", level, message))
        self._write_log_file(level.upper(), message)

    def _write_log_file(self, level: str, message: str) -> None:
        with self._log_lock:
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_FILE_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".old")
                with open(LOG_FILE, "a", encoding="utf-8") as fh:
                    fh.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {message}\n")
            except OSError:
                pass

    def _pump_events(self) -> None:
        processed = 0
        try:
            while processed < 250:
                event = self.events.get_nowait()
                processed += 1
                kind = event[0]
                if kind == "log":
                    self.console.append(_dt.datetime.now().strftime("%H:%M:%S"), event[1], event[2])
                elif kind == "telemetry":
                    self._update_telemetry(event[1])
                elif kind == "done":
                    self._finish_job(event[1], event[2] if len(event) > 2 else None)
                elif kind == "ping":
                    self._on_ping_event(*event[1:])
                elif kind == "hotkey":
                    self._quick_toggle("hotkey")
                elif kind == "tray":
                    self._on_tray(event[1])
                elif kind == "show":
                    self._show_window()
                elif kind == "autostart":
                    self._on_autostart_result(*event[1:])
                elif kind == "autostart-state":
                    self._set_setting("autostart", bool(event[1]))
                    self._render()
                elif kind == "update":
                    self._on_update_result(*event[1:])
                elif kind == "update-progress":
                    if self.update_state == "downloading":
                        self.update_progress = event[1]
                        self._refresh_update_ui()
                elif kind == "update-downloaded":
                    self._on_update_downloaded(*event[1:])
                elif kind == "report":
                    self._on_report(*event[1:])
                elif kind == "crash":
                    self._on_crash(event[1])
                elif kind == "checkup":
                    self._on_checkup(event[1])
                elif kind == "privacy":
                    self._on_privacy(*event[1:])
                elif kind == "display-fixed":
                    self._on_display_fixed(event[1])
                elif kind == "checkup-fixed":
                    self._on_checkup_fixed(*event[1:])
                elif kind == "gpu-setup":
                    self.gpu_setup = event[1]
                elif kind == "session-ping":
                    if event[1] is not None:
                        self._last_ping = event[1]
                    if self.bench and self.bench["phase"] == "running":
                        self.bench["acc"]["pings"].append(event[1])
                    for session in self.sessions_live.values():
                        session["pings"].append(event[1])
                elif kind == "overlay-hotkey":
                    self._toggle_overlay()
                elif kind == "bench-hotkey":
                    self._toggle_benchmark()
                elif kind == "game-start":
                    self._on_game_start(*event[1:])
                elif kind == "game-stop":
                    self._on_game_stop(*event[1:])
                elif kind == "background-slowed":
                    apps = list(dict.fromkeys(event[1]))
                    shown = ", ".join(apps[:4]) + (f" and {len(apps) - 4} more" if len(apps) > 4 else "")
                    self._emit("ok", f"Slowed background apps while you play: {shown}")
                elif kind == "background-restored":
                    self._emit("info", "Background apps are back to normal")
                elif kind == "winkey-failed":
                    self._emit("warn", "Couldn't block the Windows key: its helper didn't start or Windows "
                                       "refused its keyboard hook")
                elif kind in ("clean-scan", "clean-done"):
                    self._on_clean_event(kind, event[1])
                elif kind == "games-found":
                    self._on_games_found(event[1])
                elif kind == "busy-label":
                    if self.busy:
                        self._busy_label = event[1]
                        self._refresh_primary()
                elif kind == "restore-point":
                    if event[1] in ("ok", "skipped"):
                        self._set_setting("restore_point_at", time.time())
                    elif event[1] == "off":
                        self._on_protection(False)
                elif kind == "protection":
                    self._on_protection(event[1])
                elif kind == "protection-enabled":
                    self._on_protection_enabled(*event[1:])
                elif kind == "dns-info":
                    self._on_dns_info(event[1])
                elif kind == "dns-result":
                    self._on_dns_result(*event[1:])
                elif kind == "dns-switched":
                    self._on_dns_switched(*event[1:])
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.after(80, self._pump_events)

    @staticmethod
    def _summary_lines(result, stats: dict) -> list[str]:
        _ok, warn, fail = result
        lines = []
        if stats.get("plan"):
            lines.append(f"{stats['plan']} power plan is active")
        changed = stats.get("changed", 0)
        lines.append(f"{changed} setting{'s' if changed != 1 else ''} changed" if changed else "No settings needed changing")
        paused = stats.get("paused") or []
        if paused:
            lines.append(f"{len(paused)} background service{'s' if len(paused) != 1 else ''} paused until restart")
        if stats.get("freed_mb") is not None:
            lines.append(f"{stats['freed_mb']:,.0f} MB of RAM freed")
        if fail:
            lines.append(f"{fail} step{'s' if fail != 1 else ''} failed - details are in the log on the Boost tab")
        elif warn:
            lines.append(f"{warn} warning{'s' if warn != 1 else ''} - details are in the log on the Boost tab")
        return lines

    def _finish_job(self, result=None, stats: dict | None = None) -> None:
        if result is not None and stats is not None and self._pending_applied in PROFILE_TITLES:
            lines = self._summary_lines(result, stats)
            self._last_summary = (PROFILE_TITLES[self._pending_applied], lines)
            if self._job_origin is None and self._setting("preview") and not self._closing:
                self.after(10, self._show_summary)
        applied, origin = self._pending_applied, self._job_origin
        self._job_origin = None
        if self.state_store.snapshot_count() == 0:
            self.state_store.remove("applied_profile")
        elif self._pending_applied:
            self.state_store.set("applied_profile", self._pending_applied)
        self._pending_applied = None
        self._set_busy(False)
        if origin and result is not None:
            ok, _warn, fail = result
            failed = f", {fail} failed" if fail else ""
            if applied in PROFILE_TITLES:
                again = f" Press {self._hk('toggle')} again to undo." if self.hotkey else ""
                lines = self._summary_lines(result, stats) if stats else [f"{ok} change(s) made{failed}"]
                self._notify(f"{PROFILE_TITLES[applied]} applied", "; ".join(lines[:3]) + f".{again}")
            else:
                self._notify("Changes undone", f"Your previous settings are back{failed}.")

    def _show_summary(self) -> None:
        if self.dialog or not self._last_summary:
            return
        title, lines = self._last_summary
        body = "\n".join(f"\u2022  {line}" for line in lines) + "\n\nPress UNDO CHANGES to put everything back."
        self._open_dialog(f"{title} applied", body, "Done", lambda: None, cancel_label=None, width=440)

    # ---------------- hotkey, tray and window visibility ----------------

    def _start_background_services(self, instance_event) -> None:
        self._start_game_watcher()
        if self._setting("tray"):
            self._start_tray()
        if self._setting("hotkey") and self._start_hotkey():
            self._emit("info", f"Hotkey {self._hk('toggle')} toggles {PROFILE_TITLES[self.profile]} on and off")
        if instance_event:
            threading.Thread(target=self._watch_instance, args=(instance_event,), name="instance",
                             daemon=True).start()
        if self.admin:
            threading.Thread(target=self._sync_autostart, name="autostart", daemon=True).start()
            self._check_protection()
        if self._setting("updates"):
            self._schedule_update_check(4000)  # after startup has settled
        self.after(90 * 1000, self._weekly_cleanup_tick)
        self._detect_gpu_setup()
        self._start_overlay_hotkey()

    def _start_tray(self) -> bool:
        if self.tray is not None:
            return True
        if pystray is None:
            return False
        try:
            tray = TrayIcon(self)
            tray.start()
        except Exception as exc:
            self._emit("warn", f"Tray icon unavailable: {exc}")
            return False
        self.tray = tray
        return True

    def _stop_tray(self) -> None:
        if self.tray is not None:
            self.tray.stop()
            self.tray = None

    def overlay_visible(self) -> bool:
        return self.overlay is not None and self.overlay.visible

    def _toggle_overlay(self, show: bool | None = None, auto: bool = False) -> None:
        show = (not self.overlay_visible()) if show is None else show
        if show == self.overlay_visible():
            if show and not auto:
                self._overlay_auto = False  # asked for by hand: keep it after the game closes
            return
        if show:
            if self.overlay is None:
                self.overlay = OverlayWindow(self)
            self.overlay.update(self._overlay_data())
            self.overlay.show()
            self._overlay_auto = auto
            self.after(500, self._overlay_tick)
        elif self.overlay is not None:
            self.overlay.hide()
            self._overlay_auto = False
        self._tray_refresh()

    def _overlay_data(self) -> dict:
        live, frames, bound = None, [], None
        for session in self.sessions_live.values():
            capture = session.get("capture")
            if capture is not None:
                live = capture.live() or live
                frames = capture.recent_frames(90) or frames
                bound = capture.live_bound() or bound
        gpu = self._last_tel.get("gpu") or {}
        return {"fps": live[0] if live else None, "low": live[1] if live else None, "frames": frames,
                "gpu_temp": gpu.get("temp"), "gpu_load": gpu.get("load"), "cpu": self._last_tel.get("cpu"),
                "ping": self._last_ping if self.sessions_live else None, "banner": self._bench_banner(),
                "limit": bound}

    # ---------------- benchmark: a fixed 60 s FPS capture, saved to the game's history ----------------

    def _bench_banner(self) -> tuple[str, str, bool] | None:
        now = time.monotonic()
        bench = self.bench
        if bench is not None:
            left = max(0, math.ceil(bench["until"] - now))
            if bench["phase"] == "countdown":
                return f"Benchmark in {left}", "Benchmark in 0", True
            return f"Benchmark {left} s", "Benchmark 00 s", False
        if self._bench_result and now < self._bench_result[1]:
            return self._bench_result[0], self._bench_result[0], False
        return None

    def _bench_mode_label(self) -> str:
        applied = self.state_store.get("applied_profile") if self.undo_count else None
        return PROFILE_TITLES.get(applied, "No mode")

    def _toggle_benchmark(self) -> None:
        if self.bench is not None:
            self._end_benchmark(f"Benchmark of {self.bench['name']} cancelled")
            return
        playing = [(exe, s) for exe, s in self.sessions_live.items() if s.get("capture") is not None]
        drawing = [(exe, s) for exe, s in playing if s["capture"].live() is not None]
        if not drawing:
            if not self.sessions_live:
                why = "Start one of your games first, then press it in the game."
            elif not playing:
                why = ("FPS can't be measured here (it needs administrator rights)." if not self.admin
                       else "FPS isn't being measured for this game.")
            else:
                why = "The game isn't drawing frames right now (loading, paused or minimized)."
            self._emit("warn", f"No benchmark: {why}")
            self._notify("No benchmark", why)
            return
        exe, session = drawing[-1]
        self._bench_result = None
        opened = not self.overlay_visible()
        if opened:
            self._toggle_overlay(True, auto=True)
        self.bench = {"exe": exe, "name": session["name"], "phase": "countdown", "label": self._bench_mode_label(),
                      "until": time.monotonic() + BENCH_COUNTDOWN, "opened_overlay": opened}
        self._emit("info", f"Benchmark of {session['name']} starts in {BENCH_COUNTDOWN} s and runs for {BENCH_SECONDS} s "
                           f"with {self.bench['label']} applied. Play normally; {self._hk('bench')} cancels it.")
        self._tray_refresh()
        self.after(100, self._bench_tick)

    def _bench_tick(self) -> None:
        bench = self.bench
        if bench is None or self._closing:
            return
        session = self.sessions_live.get(bench["exe"])
        capture = session.get("capture") if session else None
        if capture is None:
            self._end_benchmark(f"Benchmark of {bench['name']} stopped: the game closed")
            return
        now = time.monotonic()
        if bench["phase"] == "countdown" and now >= bench["until"]:
            if not capture.start_bench():
                self._end_benchmark(f"Benchmark of {bench['name']} stopped: the game isn't drawing frames")
                return
            bench.update(phase="running", until=now + BENCH_SECONDS,
                         acc=new_session(bench["exe"], bench["name"]))
        elif bench["phase"] == "running" and now >= bench["until"]:
            self._finish_benchmark(bench, capture)
            return
        self.after(200, self._bench_tick)

    def _finish_benchmark(self, bench: dict, capture: FrameCapture) -> None:
        acc = bench["acc"]
        acc["fps"] = capture.end_bench()
        if not acc["n"] and self._last_tel:
            add_session_sample(acc, self._last_tel)
        report = finish_session(acc, acc["start"] + BENCH_SECONDS, min_seconds=0)
        self.bench = None
        if report is None or report.get("fps_avg") is None:
            self._end_benchmark(f"Benchmark of {bench['name']} found too few frames to measure", bench)
            return
        report.update(benchmark=True, label=bench["label"])
        saved = list(self._setting("sessions") or [])
        saved.append(report)
        self._set_setting("sessions", saved[-SESSION_KEEP:])
        self._bench_result = (f"{report['fps_avg']:.0f} avg · {report['fps_low1']:.0f} low",
                              time.monotonic() + BENCH_RESULT_SECONDS)
        self._emit("ok", f"Benchmark of {bench['name']} ({bench['label']}): {benchmark_summary(report)}. "
                         f"Compare it with another run in Tools → Games → {bench['name']}")
        self._after_benchmark(bench)
        self._paint_games()
        self._render()

    def _end_benchmark(self, message: str, bench: dict | None = None) -> None:
        bench = bench or self.bench
        self.bench = None
        if bench is not None:
            session = self.sessions_live.get(bench["exe"])
            if session and session.get("capture") is not None:
                session["capture"].end_bench()
        self._bench_result = None
        self._emit("warn", message)
        self._after_benchmark(bench)

    def _after_benchmark(self, bench: dict | None) -> None:
        self._tray_refresh()
        if self.overlay_visible():
            self.overlay.update(self._overlay_data())
        if bench and bench.get("opened_overlay"):
            wait = BENCH_RESULT_SECONDS * 1000 if self._bench_result else 0
            self.after(wait, self._hide_bench_overlay)

    def _hide_bench_overlay(self) -> None:
        # Only the overlay this benchmark opened, and only while nothing else wants it shown.
        if self.bench is None and self._overlay_auto and self.overlay_visible() \
                and not (self.sessions_live and self._setting("overlay")):
            self._toggle_overlay(False)

    def _overlay_tick(self) -> None:
        if self._closing or not self.overlay_visible():
            return
        self.overlay.update(self._overlay_data())
        self.after(500, self._overlay_tick)

    def _start_overlay_hotkey(self) -> None:
        self._extra_hotkeys_on = True
        hotkey = self._register_hotkey("overlay")
        if hotkey is not None:
            self.overlay_hotkey = hotkey
        else:
            self._emit("warn", f"{self._hk('overlay')} is already taken by another app; use the tray menu for the overlay")
        bench = self._register_hotkey("bench")
        if bench is not None:
            self.bench_hotkey = bench
        else:
            self._emit("warn", f"{self._hk('bench')} is already taken by another app; "
                               "start benchmarks from the tray menu or the overlay's right-click menu")

    def _start_hotkey(self) -> bool:
        if self.hotkey is not None:
            return True
        hotkey = self._register_hotkey("toggle")
        if hotkey is None:
            self._emit("warn", f"{self._hk('toggle')} is already taken by another app, so the hotkey is off")
            return False
        self.hotkey = hotkey
        return True

    def _stop_hotkey(self) -> None:
        if self.hotkey is not None:
            self.hotkey.stop()
            self.hotkey = None

    # ---------------- hotkeys you choose (Settings → Hotkeys…) ----------------

    def _hotkey_spec(self, action: str) -> tuple[int, int]:
        _what, mods, vk, _id, _event = HOTKEY_ACTIONS[action]
        saved = self._setting("hotkeys")
        chosen = saved.get(action) if isinstance(saved, dict) else None
        if isinstance(chosen, list) and len(chosen) == 2 and all(isinstance(v, int) for v in chosen) \
                and hotkey_problem(chosen[0], chosen[1]) is None:
            return chosen[0], chosen[1]
        return mods, vk

    def _hk(self, action: str) -> str:
        return hotkey_label(*self._hotkey_spec(action))

    def _game_overlay_text(self) -> str:
        return f"FPS overlay · {self._hk('overlay')}"

    def _register_hotkey(self, action: str) -> "HotkeyThread | None":
        _what, _mods, _vk, hotkey_id, event = HOTKEY_ACTIONS[action]
        mods, vk = self._hotkey_spec(action)
        hotkey = HotkeyThread(self.events, hotkey_id, vk, event, mods)
        hotkey.start()
        hotkey.ready.wait(2.0)
        return hotkey if hotkey.ok else None

    def _stop_all_hotkeys(self) -> None:
        self._stop_hotkey()
        for attr in ("overlay_hotkey", "bench_hotkey"):
            hotkey = getattr(self, attr)
            if hotkey is not None:
                hotkey.stop()
                setattr(self, attr, None)

    def _restart_hotkeys(self) -> None:
        """Register every hotkey again from the settings (after a change, a backup or the picker)."""
        self._stop_all_hotkeys()
        if self._setting("hotkey"):
            self._start_hotkey()
        if getattr(self, "_extra_hotkeys_on", False):
            self._start_overlay_hotkey()
        self._refresh_hotkey_labels()

    def _refresh_hotkey_labels(self) -> None:
        title = SETTING_TEXT["hotkey"][0].split("\u00b7")[0].rstrip()
        self.canvas.itemconfigure(self.t_setting_title["hotkey"],
                                  text=self._fit(f"{title}  \u00b7  {self._hk('toggle')}", self.f_title, 360))
        if getattr(self, "t_game_overlay", None) and self.canvas.itemcget(self.t_game_overlay, "text").startswith("FPS overlay"):
            self.canvas.itemconfigure(self.t_game_overlay, text=self._game_overlay_text())
        if getattr(self, "tray", None) is not None:
            self._tray_refresh()

    def _open_hotkeys(self) -> None:
        actions = list(HOTKEY_ACTIONS)
        self._open_dialog("Hotkeys", "Click one, then press the keys you want: Ctrl or Alt with a letter or number, "
                                     "or an F key on its own. They work in games too.",
                          "Done", lambda: None, cancel_label="Defaults", width=460,
                          extra_h=len(actions) * 46 + 34, on_cancel=self._reset_hotkeys)
        c, px = self.canvas, self.px
        x, y, w, _h = self.dialog["rect"]
        top = self.dialog["body_bottom"] + 14
        chips = {}
        for i, action in enumerate(actions):
            ry = top + i * 46
            c.create_text(px(x + 26), px(ry + 17), anchor="w", text=HOTKEY_ACTIONS[action][0],
                          font=self.f_dialog_body, fill=TEXT_HI, tags="dialog")
            rect = (x + w - 26 - 200, ry, 200, 34)
            text = c.create_text(px(rect[0] + rect[2] / 2), px(ry + 17), text=self._hk(action), font=self.f_ghost,
                                 fill="#CFCFCF", tags="dialog")
            chips[action] = {"rect": rect, "text": text}
        status = c.create_text(px(x + 26), px(top + len(actions) * 46 + 2), anchor="nw", text="", font=self.f_desc,
                               fill=TEXT_MID, width=px(w - 52), tags="dialog")
        self.dialog.update(chips=chips, status=status, recording=None)
        self._render()

    def _hotkey_status(self, text: str, color: str = TEXT_MID) -> None:
        if self.dialog and self.dialog.get("status"):
            self.canvas.itemconfigure(self.dialog["status"], text=text, fill=color)

    def _record_hotkey_start(self, action: str) -> None:
        if self.dialog.get("recording"):
            self._record_hotkey_stop()
        self.dialog["recording"] = action
        self._stop_all_hotkeys()  # so pressing one of Astra's own keys reaches the window
        self.canvas.itemconfigure(self.dialog["chips"][action]["text"], text="Press keys…")
        self._hotkey_status(f"Press the new keys for {HOTKEY_ACTIONS[action][0].lower()}, "
                            f"or Esc to keep {self._hk(action)}.")
        self.canvas.focus_set()
        self._render()

    def _record_hotkey_stop(self) -> None:
        action = self.dialog.get("recording") if self.dialog else None
        if not action:
            return
        self.dialog["recording"] = None
        self.canvas.itemconfigure(self.dialog["chips"][action]["text"], text=self._hk(action))
        self._restart_hotkeys()
        self._render()

    def _on_hotkey_record(self, event):
        action = self.dialog["recording"]
        vk = int(event.keycode or 0)
        if event.keysym == "Escape" or vk in _MODIFIER_VKS:
            return "break"  # Esc is handled by its own binding; a modifier alone isn't a choice yet
        mods = ((MOD_CONTROL if event.state & 0x0004 else 0) | (MOD_ALT if event.state & 0x20000 else 0)
                | (MOD_SHIFT if event.state & 0x0001 else 0))
        problem = hotkey_problem(mods, vk)
        if problem:
            self._hotkey_status(problem, WARN_YELLOW)
            return "break"
        label = hotkey_label(mods, vk)
        for other in HOTKEY_ACTIONS:
            if other != action and self._hotkey_spec(other) == (mods, vk):
                self._hotkey_status(f"{label} is already the {HOTKEY_ACTIONS[other][0].lower()} hotkey.", WARN_YELLOW)
                return "break"
        test = HotkeyThread(self.events, 9, vk, "hotkey-test", mods)
        test.start()
        test.ready.wait(2.0)
        free = test.ok
        test.stop()
        if not free:
            self._hotkey_status(f"{label} is taken by another app. Try another.", WARN_YELLOW)
            return "break"
        saved = dict(self._setting("hotkeys") or {})
        saved[action] = [mods, vk]
        self._set_setting("hotkeys", saved)
        self.dialog["recording"] = None
        self.canvas.itemconfigure(self.dialog["chips"][action]["text"], text=label)
        self._restart_hotkeys()
        self._hotkey_status(f"{HOTKEY_ACTIONS[action][0]} is now {label}.", TEXT_HI)
        self._emit("ok", f"{HOTKEY_ACTIONS[action][0]} hotkey is now {label}")
        self._render()
        return "break"

    def _reset_hotkeys(self) -> None:
        if self.dialog.get("recording"):
            self.dialog["recording"] = None
        self._set_setting("hotkeys", {})
        self._restart_hotkeys()
        for action, chip in self.dialog["chips"].items():
            self.canvas.itemconfigure(chip["text"], text=self._hk(action))
        defaults = ", ".join(self._hk(a) for a in HOTKEY_ACTIONS)
        self._hotkey_status(f"Back to the defaults: {defaults}.", TEXT_HI)
        self._emit("ok", f"Hotkeys back to the defaults ({defaults})")
        self._render()

    def _tray_refresh(self) -> None:
        if self.tray is not None:
            self.tray.refresh()

    def tray_tooltip(self) -> str:
        applied = self.state_store.get("applied_profile") if self.undo_count else None
        if applied in PROFILE_TITLES:
            return f"{APP_NAME} - {PROFILE_TITLES[applied]} active"
        return f"{APP_NAME} - nothing applied"

    def tray_primary_label(self) -> str:
        hint = f"\t{self._hk('toggle')}" if self.hotkey else ""
        if self.busy:
            return "Working…"
        if self.undo_count:
            return f"Undo changes{hint}"
        return f"Optimize ({PROFILE_TITLES[self.profile]}){hint}"

    def _notify(self, title: str, message: str) -> None:
        if self.tray is not None:
            self.tray.notify(title, message)

    def _quick_toggle(self, origin: str) -> None:
        """Hotkey / tray: optimize with the selected mode, or undo when something is applied."""
        if not self.admin:
            self._notify(APP_NAME, "Administrator rights are needed to apply changes.")
            return
        if self.busy:
            self._notify(f"{APP_NAME} is busy", "Wait for the current task to finish.")
            return
        if self.dialog:
            self._close_dialog()
        self._refresh_primary()
        self._emit("info", f"Triggered from the {'hotkey' if origin == 'hotkey' else 'tray menu'}")
        if self.undo_count:
            self._start_job("Undo changes", SystemTweaks.undo_changes, busy_label="UNDOING…")
        else:
            self._on_optimize()
        if self.busy:
            self._job_origin = origin

    def _on_tray(self, action: str) -> None:
        if action == "open":
            self._show_window()
        elif action == "toggle":
            self._quick_toggle("tray")
        elif action == "exit":
            self._quit()
        elif action == "overlay":
            self._toggle_overlay()
        elif action == "bench":
            self._toggle_benchmark()
        elif action == "report":
            self._show_window()
            self._report_problem()
        elif action.startswith("profile:"):
            self._select_profile(action.partition(":")[2])

    def _watch_instance(self, handle) -> None:
        while not self._closing:
            if kernel32.WaitForSingleObject(handle, 500) == 0:
                self.events.put(("show",))

    def _show_window(self) -> None:
        try:
            if self.state() == "withdrawn":
                self.deiconify()
            hwnd = self._hwnd()
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            self.lift()
            self.attributes("-topmost", True)
            self.after(60, lambda: self.attributes("-topmost", False))
            self.focus_force()
            self.after(30, self._ensure_window_size)
        except tk.TclError:
            pass

    def _hide_to_tray(self) -> None:
        self.withdraw()
        if not self._setting("tray_hint"):
            self._set_setting("tray_hint", True)
            self._notify(f"{APP_NAME} is still running",
                         "It lives in the tray now. Right-click the icon to switch modes or exit.")

    def _quit(self) -> None:
        if self.busy:
            self._show_window()
            if not self.dialog:
                self._open_dialog(
                    "Optimization still running",
                    "Closing now stops it partway. Anything already changed stays changed until you undo it.",
                    "Close anyway", self._shutdown, cancel_label="Keep running")
            return
        self._shutdown()

    # ---------------- settings actions ----------------

    def _toggle_setting(self, key: str) -> None:
        if key == "tray":
            on = not self._setting_on("tray")
            if on and not self._start_tray():
                return
            if not on:
                self._stop_tray()
            self._set_setting("tray", on)
            self._emit("ok", "Closing the window now keeps Astra Optimizer in the tray" if on
                       else "Tray icon off: closing the window exits Astra Optimizer")
        elif key == "hotkey":
            on = not self._setting_on("hotkey")
            if on and not self._start_hotkey():
                return
            if not on:
                self._stop_hotkey()
            self._set_setting("hotkey", on)
            self._emit("ok", f"Hotkey {self._hk('toggle')} on" if on else f"Hotkey {self._hk('toggle')} off")
            self._tray_refresh()
        elif key == "restore":
            on = not self._setting("restore_point")
            self._set_setting("restore_point", on)
            self._emit("ok", "A restore point will be saved before your first optimize each day" if on
                       else "Restore points before optimizing are off")
        elif key == "preview":
            on = not self._setting("preview")
            self._set_setting("preview", on)
            self._emit("ok", "Modes now show a preview and a summary" if on else "Modes now run without a preview")
        elif key == "mouse":
            current = mouse_accel_get()
            if current is None:
                self._emit("fail", "Windows wouldn't say how mouse acceleration is set")
            elif current[2]:
                self.state_store.set("mouse_saved", list(current))  # what to put back
                if mouse_accel_set((0, 0, 0)):
                    self._emit("ok", "Mouse acceleration off: the pointer moves the same distance at any speed")
                else:
                    self._emit("fail", "Windows didn't accept the mouse setting")
            else:
                saved = self.state_store.get("mouse_saved")
                values = tuple(saved) if isinstance(saved, list) and len(saved) == 3 and saved[2] \
                    else MOUSE_ACCEL_DEFAULT
                if mouse_accel_set(values):
                    self.state_store.set("mouse_saved", None)
                    self._emit("ok", "Mouse acceleration is back on (Enhance pointer precision)")
                else:
                    self._emit("fail", "Windows didn't accept the mouse setting")
        elif key == "pointer":
            self._toggle_pointer()
        elif key == "sticky":
            self._toggle_sticky()
        elif key in WINSET_REG:
            self._toggle_winset_reg(key)
        elif key == "startkey":
            on = not self._setting("startkey")
            self._set_setting("startkey", on)
            if self.game_watcher is not None:
                self.game_watcher.winkey_enabled = on
            if on:
                marked = sum(1 for g in self.games if g.get("winkey"))
                where = f"the {marked} marked game{'s' if marked != 1 else ''}" if marked else "your games"
                self._emit("ok", f"A lone Windows key press won't open Start in {where}; shortcuts like Win+D "
                                 "still work")
            else:
                self._emit("ok", "The Windows key opens Start as usual again, games included")
            self._refresh_startkey_row()
        elif key == "updates":
            on = not self._setting("updates")
            self._set_setting("updates", on)
            self._emit("ok", "Astra Optimizer will check GitHub for new versions" if on
                       else "Automatic update checks are off. About → Check for updates still works")
            self._schedule_update_check(5000 if on else None)
        elif key == "autostart":
            desired = not self._setting("autostart")
            self._autostart_pending, self._autostart_desired = True, desired

            def work():
                ok, message = AutoStart.enable() if desired else AutoStart.disable()
                self.events.put(("autostart", desired, ok, message))

            threading.Thread(target=work, name="autostart", daemon=True).start()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    # ---------------- Settings > Windows: each switch saves exactly what it replaces ----------------

    def _winset_saved(self) -> dict:
        saved = self.state_store.get("winset_saved")
        return dict(saved) if isinstance(saved, dict) else {}

    def _winset_save(self, key: str, value) -> None:
        saved = self._winset_saved()
        if value is None:
            saved.pop(key, None)
        else:
            saved[key] = value
        self.state_store.set("winset_saved", saved)

    def _toggle_pointer(self) -> None:
        current = pointer_speed_get()
        if current is None:
            self._emit("fail", "Windows wouldn't say what your pointer speed is")
            return
        if current != POINTER_SPEED_1TO1:
            self._winset_save("pointer", current)
            if pointer_speed_set(POINTER_SPEED_1TO1):
                self._emit("ok", f"Pointer speed set to 1:1 (it was notch {pointer_notch(current)} of 11)")
            else:
                self._emit("fail", "Windows didn't accept the pointer speed")
            return
        saved = self._winset_saved().get("pointer")
        if not isinstance(saved, int) or not 1 <= saved <= 20 or saved == POINTER_SPEED_1TO1:
            self._emit("info", "Your pointer speed was already 1:1 before Astra changed anything, so there's "
                               "nothing to put back. Change it in Windows' mouse settings if you like")
            return
        if pointer_speed_set(saved):
            self._winset_save("pointer", None)
            self._emit("ok", f"Pointer speed is back to notch {pointer_notch(saved)} of 11")
        else:
            self._emit("fail", "Windows didn't accept the pointer speed")

    def _toggle_sticky(self) -> None:
        tweaks = SystemTweaks(RunLog(lambda *_a: None), self.state_store)
        tweaks.recording = False  # this switch keeps its own record, apart from a mode's undo
        for which, *_rest in ACCESS_SHORTCUTS:
            self.state_store.snapshot_discard(f"access:{which}")  # your choice here wins over a later Undo
        on_now = access_shortcuts_on()
        if on_now:
            self._winset_save("sticky", on_now)
            ok = all(tweaks.set_access_shortcut(which, False) for which in on_now)
            self._emit("ok" if ok else "fail", "Sticky Keys, Filter Keys and Toggle Keys pop-ups are off" if ok
                       else "Windows didn't accept every shortcut change")
            return
        saved = self._winset_saved().get("sticky")
        back = [w for w in saved if w in ACCESS_BY_KEY] if isinstance(saved, list) and saved \
            else [which for which, *_rest in ACCESS_SHORTCUTS]  # nothing saved: Windows' default, all three on
        ok = all(tweaks.set_access_shortcut(which, True) for which in back)
        if ok:
            self._winset_save("sticky", None)
        self._emit("ok" if ok else "fail", "The Sticky Keys shortcuts are back on" if ok
                   else "Windows didn't accept every shortcut change")

    def _toggle_winset_reg(self, key: str) -> None:
        hive, path, name, on_value, off_value = WINSET_REG[key]
        title = SETTING_TEXT[key][0]
        current = SystemTweaks.reg_read(hive, path, name)
        try:
            if not (current is not None and current[0] == on_value):
                self._winset_save(key, {"present": current is not None,
                                        "value": current[0] if current else None,
                                        "type": current[1] if current else winreg.REG_DWORD})
                _reg_put(hive, path, name, on_value)
                self._emit("ok", WINSET_MESSAGES[key][0])
                return
            saved = self._winset_saved().get(key)
            if isinstance(saved, dict) and saved.get("present"):
                _reg_put(hive, path, name, saved["value"], int(saved.get("type", winreg.REG_DWORD)))
            elif isinstance(saved, dict) or off_value is None:
                _reg_delete(hive, path, name)  # it wasn't there before: Windows decides again
            else:
                _reg_put(hive, path, name, off_value)
            self._winset_save(key, None)
            self._emit("ok", WINSET_MESSAGES[key][1])
        except OSError as exc:
            self._emit("fail", f"{title}: Windows refused the change ({exc.strerror or exc})")

    def _on_autostart_result(self, desired: bool, ok: bool, message: str) -> None:
        self._autostart_pending = False
        if ok:
            self._set_setting("autostart", desired)
            self._emit("ok", "Astra Optimizer will start with Windows, in the tray" if desired
                       else "Astra Optimizer no longer starts with Windows")
        else:
            self._emit("fail", f"Could not {'set up' if desired else 'remove'} start with Windows: {message}")
        self._render()

    def _sync_autostart(self) -> None:
        """Mirror the real task state; re-point an existing task at this exe in case it moved.
        A task left by the pre-rename app is replaced by one under the new name."""
        legacy = run_command([SCHTASKS, "/Query", "/TN", LEGACY_AUTOSTART_TASK], timeout=15)[0] == 0
        if legacy:
            run_command([SCHTASKS, "/Delete", "/TN", LEGACY_AUTOSTART_TASK, "/F"], timeout=20)
        exists = AutoStart.is_enabled() or legacy
        if exists and (getattr(sys, "frozen", False) or legacy):
            AutoStart.enable()
        self.events.put(("autostart-state", exists))

    # ---------------- update check ----------------

    def _version_line(self) -> str:
        base = f"Version {DISPLAY_VERSION[1:]}  ·  for Windows 10 and 11"
        status = {"checking": "Checking for updates…", "current": "Up to date",
                  "failed": "Couldn't reach GitHub"}.get(self.update_state)
        if self.update_info and self.update_state in ("available", "downloading", "ready"):
            tag = display_tag(self.update_info["tag"])
            status = {"available": f"{tag} is available", "downloading": f"Downloading {tag}…",
                      "ready": f"{tag} is ready to install"}[self.update_state]
        return f"{base}  ·  {status}" if status else base

    def _update_label(self) -> str:
        if self.update_state == "downloading":
            return f"Downloading {self.update_progress}%"
        if self.update_state == "ready":
            return "Restart to update"
        if self.update_state == "available" and self.update_info:
            verb = "Update to" if self._can_self_update() else "Download"
            return f"{verb} {display_tag(self.update_info['tag'])}"
        return "Checking…" if self.update_state == "checking" else "Check for updates"

    def _refresh_update_ui(self) -> None:
        self.canvas.itemconfigure(self.t_about_version, text=self._version_line(),
                                  fill=TEXT_HI if self.update_state in ("available", "ready") else TEXT_MID)
        self.canvas.itemconfigure(self.t_buttons["update"], text=self._update_label())
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _schedule_update_check(self, delay_ms: int | None) -> None:
        """(Re)arm the automatic check; None cancels it."""
        if self._update_job is not None:
            self.after_cancel(self._update_job)
            self._update_job = None
        if delay_ms is not None:
            self._update_job = self.after(delay_ms, self._auto_update_check)

    def _auto_update_check(self) -> None:
        self._update_job = None
        if not self._setting("updates"):
            return
        self._check_updates(manual=False)
        self._schedule_update_check(UPDATE_INTERVAL_MS)

    def _check_updates(self, manual: bool) -> None:
        if self.update_state in ("checking", "downloading", "ready"):
            return
        previous = self.update_state
        self.update_state = "checking"
        self._refresh_update_ui()

        def work():
            try:
                self.events.put(("update", manual, previous, fetch_latest_release(), None))
            except Exception as exc:  # offline, blocked, GitHub down: never bother the user unasked
                self.events.put(("update", manual, previous, None, str(exc) or type(exc).__name__))

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _on_update_result(self, manual: bool, previous: str, release: dict | None, error: str | None) -> None:
        if error is not None:
            # A failed automatic check keeps whatever we knew before; only a manual one says so.
            self.update_state = "failed" if manual or previous in ("idle", "failed") else previous
            if manual:
                self._emit("warn", f"Couldn't check for updates: {error}")
        elif release and is_newer_version(release["tag"]):
            announce = not (previous == "available" and self.update_info
                            and self.update_info["tag"] == release["tag"])
            self.update_state, self.update_info = "available", release
            if announce or manual:
                how = "Update from the About tab" if self._can_self_update() else "Download it from the About tab"
                self._emit("info", f"{APP_NAME} {display_tag(release['tag'])} is available. {how}.")
        else:
            self.update_state, self.update_info = "current", None
            if manual:
                self._emit("ok", f"{APP_NAME} {DISPLAY_VERSION} is the latest version")
        self._refresh_update_ui()

    def _can_self_update(self) -> bool:
        return (bool(getattr(sys, "frozen", False)) and sys.executable.lower().endswith(".exe")
                and bool(self.update_info and self.update_info.get("download")))

    def _start_update_download(self) -> None:
        info = self.update_info
        self.update_state, self.update_progress = "downloading", 0
        self._refresh_update_ui()
        self._emit("info", f"Downloading {APP_NAME} {display_tag(info['tag'])}…")
        dest = sys.executable + ".download"

        def work() -> None:
            try:
                download_update(info, dest, progress=lambda pct: self.events.put(("update-progress", pct)))
                self.events.put(("update-downloaded", dest, None))
            except Exception as exc:
                self.events.put(("update-downloaded", None, str(exc) or type(exc).__name__))

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _on_update_downloaded(self, path: str | None, error: str | None) -> None:
        if error is not None or not path:
            self.update_state = "available"
            self._emit("warn", f"Update not downloaded: {error}. Nothing was changed - try again, or get it from "
                               "the release page.")
            self._refresh_update_ui()
            return
        self.update_state, self.update_file = "ready", path
        self._emit("ok", f"{display_tag(self.update_info['tag'])} downloaded and checked")
        self._refresh_update_ui()
        self._confirm_update_restart()

    def _confirm_update_restart(self) -> None:
        if self.dialog:
            return
        if self.busy:
            self._emit("info", "Restart to update once the running job has finished")
            return
        tag = display_tag(self.update_info["tag"]) if self.update_info else "The update"
        self._open_dialog(f"{tag} is ready",
                          f"{APP_NAME} closes and opens again as {tag}. Your settings, games and any mode you "
                          "applied stay as they are.",
                          "Restart now", self._install_update, cancel_label="Later")

    def _install_update(self) -> None:
        if self.busy or not self.update_file:
            return
        exe = sys.executable
        try:
            install_update(exe, self.update_file)
        except OSError as exc:
            self.update_state, self.update_file = "available", None
            self._emit("fail", f"Couldn't install the update: {exc}")
            self._refresh_update_ui()
            return
        try:
            launch_updated(exe)
        except OSError as exc:
            self._emit("warn", f"Updated, but couldn't restart ({exc}). Open {APP_NAME} again to use the new version.")
            self.update_state, self.update_file = "current", None
            self._refresh_update_ui()
            return
        self._shutdown()

    # ---------------- crash catcher ----------------

    def _record_crash(self, where: str, exc_type, exc, tb) -> None:
        """Log the full traceback and tell the UI thread. Never raises."""
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            write_crash_log(f"in {where}:\n{text}")
            self.events.put(("crash", crash_summary(exc_type, exc)))
        except Exception:
            pass

    def _on_tk_error(self, exc_type, exc, tb) -> None:
        self._record_crash("the window", exc_type, exc, tb)

    def _on_thread_error(self, args) -> None:
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", None) or "a background task"
        self._record_crash(f"thread {name}", args.exc_type, args.exc_value, args.exc_traceback)

    def _on_crash(self, summary: str) -> None:
        if self._closing:
            return
        self._emit("fail", f"Something went wrong: {summary}. Details are in the log - Report a problem "
                           "on the About tab sends them.")
        if self._crash_dialog_shown or self.dialog:
            return
        self._crash_dialog_shown = True  # once per run, so a repeating error can't stack dialogs
        self._show_window()
        self._open_dialog("Something went wrong",
                          f"{APP_NAME} hit an unexpected error ({summary}). It keeps running, but the last thing "
                          f"you did may not have finished.\n\nA problem report lets {CREDIT_NAME} see what happened.",
                          "Report a problem", self._report_problem, cancel_label="Not now", width=440)

    # ---------------- problem report ----------------

    def _report_problem(self) -> None:
        if self.report_busy:
            return
        self.report_busy = True
        self.canvas.itemconfigure(self.t_buttons["report"], text="Making report…")
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()
        if self.tray is not None:
            self.tray.refresh()
        self._emit("info", "Making a problem report - this runs a checkup and takes a few seconds…")
        applied = self.state_store.get("applied_profile")
        app_info = {
            "Version": DISPLAY_VERSION + ("" if getattr(sys, "frozen", False) else " (from source)"),
            "Administrator": "yes" if self.admin else "no (monitor-only)",
            "Applied mode": PROFILE_TITLES.get(applied, "none") if self.undo_count else "none",
            "Changes to undo": self.undo_count,
            "Games in list": len(self.games),
            "Display scale": f"{self.scale:.2f}",
            "Overlay": "on" if self.overlay_visible() else "off",
        }
        sessions = list(self._setting("sessions") or [])[-5:]
        games = [dict(g) for g in self.games]
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H%M")
        path = os.path.join(desktop_folder(), f"Astra Optimizer report {stamp}.zip")
        state_file = self.state_store.path

        def work() -> None:
            try:
                write_problem_report(path, app_info, games, sessions, state_file)
                self.events.put(("report", path, None))
            except Exception as exc:
                self.events.put(("report", None, str(exc) or type(exc).__name__))

        threading.Thread(target=work, name="report", daemon=True).start()

    def _on_report(self, path: str | None, error: str | None) -> None:
        self.report_busy = False
        self.canvas.itemconfigure(self.t_buttons["report"], text="Report a problem")
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        if self.tray is not None:
            self.tray.refresh()
        if error is not None or not path:
            self._emit("fail", f"Couldn't make the report: {error}")
            self._render()
            return
        name = os.path.basename(path)
        self._emit("ok", f"Report saved to your desktop: {name}")
        try:
            subprocess.Popen(f'explorer.exe /select,"{path}"', creationflags=CREATE_NO_WINDOW)
        except OSError:
            pass
        self._render()
        if not self.dialog:
            self._open_dialog("Report saved",
                              f"{name} is on your desktop. Send it to {CREDIT_NAME} with a line or two about what "
                              "went wrong.\n\nIt holds your PC specs, a checkup, your last game sessions and "
                              f"{APP_NAME}'s log and settings - no personal files.",
                              "Done", lambda: None, cancel_label=None, width=440)

    # ---------------- settings backup ----------------

    def _export_backup(self) -> None:
        stamp = _dt.date.today().isoformat()
        path = filedialog.asksaveasfilename(parent=self, title="Back up Astra Optimizer settings",
                                            initialfile=f"Astra Optimizer backup {stamp}.json",
                                            defaultextension=".json", filetypes=[("Backup", "*.json")])
        if not path:
            return
        try:
            data = make_backup(dict(self.state_store.get("settings") or {}), self._bg_path())
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1)
        except (OSError, TypeError, ValueError) as exc:
            self._emit("fail", f"Couldn't save the backup: {exc}")
            return
        games, sessions = len(data["settings"].get("games") or []), len(data["settings"].get("sessions") or [])
        self._emit("ok", f"Backup saved to {path} ({plural(games, 'game')}, {plural(sessions, 'session')})")

    def _import_backup(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Restore Astra Optimizer settings",
                                          filetypes=[("Backup", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = read_backup(path)
        except ValueError as exc:
            self._emit("fail", f"Couldn't restore {os.path.basename(path)}: {exc}")
            return
        try:
            created = _dt.datetime.fromisoformat(str(data.get("created")))
            when = f"from {created.strftime('%b')} {created.day}, {created.year}"
        except ValueError:
            when = os.path.basename(path)
        games, sessions = len(data["settings"].get("games") or []), len(data["settings"].get("sessions") or [])
        self._open_dialog(f"Restore the backup {when}?",
                          f"Your settings, games list ({plural(games, 'game')} in the backup), appearance and "
                          f"session history ({plural(sessions, 'session')}) are replaced with the backup's. Changes Astra made to Windows, and the "
                          "Privacy switches, stay as they are.",
                          "Restore", lambda: self._apply_backup(data))

    def _apply_backup(self, data: dict) -> None:
        import base64
        merged = merge_backup(dict(self.state_store.get("settings") or {}), data)
        if data.get("background_jpg") and (merged.get("appearance") or {}).get("background") == "image":
            try:
                raw = base64.b64decode(data["background_jpg"], validate=True)
                Image.open(__import__("io").BytesIO(raw)).verify()
                os.makedirs(os.path.dirname(self._bg_path()), exist_ok=True)
                with open(self._bg_path(), "wb") as handle:
                    handle.write(raw)
            except Exception:
                merged["appearance"] = dict(merged["appearance"], background="stars")
        self.state_store.set("settings", merged)
        # Bring everything running in line with the restored settings.
        self.game_finder, self.session_view, self.game_offset = None, None, 0
        self.games = list(merged["games"])
        self._save_games()
        self.look = self._load_look()
        self._bg_version += 1
        if merged.get("tray") and (self.tray is None or not self.tray.running):
            self._start_tray()
        elif not merged.get("tray") and self.tray is not None:
            self._stop_tray()
        self._restart_hotkeys()
        self._schedule_update_check(5000 if merged.get("updates") else None)
        if self.game_watcher is not None:
            self.game_watcher.slow_background = bool(merged.get("slow_background"))
            self.game_watcher.winkey_enabled = bool(merged.get("startkey"))
        self.host_value = merged.get("custom_host") or ""
        self._refresh_startkey_row()
        self.host_cursor = len(self.host_value)
        for row in self.ping_rows:
            if row["key"] == "custom":
                row["host"] = self.host_value or None
        self._paint_host()
        self._paint_games()
        self._paint_cleanup()
        self._tray_refresh()
        self._apply_look(fade=True)
        self._emit("ok", f"Backup restored: {plural(len(self.games), 'game')}, "
                         f"{plural(len(merged['sessions']), 'session')}")

    def _open_link(self, url: str) -> None:
        """Open in the user's browser. Going through Explorer keeps the browser out of the
        elevated (administrator) session this app runs in."""
        try:
            subprocess.Popen(["explorer.exe", url], creationflags=CREATE_NO_WINDOW)
        except OSError:
            import webbrowser
            webbrowser.open(url)

    def _open_state_folder(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            os.startfile(STATE_DIR)
        except OSError as exc:
            self._emit("fail", f"Could not open {STATE_DIR}: {exc}")

    # ---------------- games ----------------

    def _start_game_watcher(self) -> None:
        if self.game_watcher is None:
            self.game_watcher = GameWatcher(self.events)
            self.game_watcher.slow_background = bool(self._setting("slow_background"))
            self.game_watcher.winkey_enabled = bool(self._setting("startkey"))
            self.game_watcher.set_games(self.games)
            self.game_watcher.start()

    def _save_games(self) -> None:
        self._set_setting("games", [{k: v for k, v in g.items() if not k.startswith("_")} for g in self.games])
        if getattr(self, "t_setting_desc", None):
            self._refresh_startkey_row()  # its text counts the marked games
        if self.game_watcher is not None:
            self.game_watcher.set_games(self.games)
        else:
            GAME_EXE_PATHS.clear()
            GAME_EXE_PATHS.update(os.path.normcase(g["path"]) for g in self.games)

    def _running_games(self) -> dict[int, str]:
        return dict(self.game_watcher.running) if self.game_watcher is not None else {}

    def _game_name(self, exe: str) -> str:
        for game in self.games:
            if os.path.normcase(game["path"]) == exe:
                return game.get("name") or os.path.basename(exe)
        return os.path.basename(exe)

    def _add_game(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Choose a game's .exe",
                                          filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if path:
            self._add_game_path(path)

    def _add_game_path(self, path: str) -> None:
        path = os.path.normpath(path)
        if any(os.path.normcase(g["path"]) == os.path.normcase(path) for g in self.games):
            self._emit("info", f"{os.path.basename(path)} is already in your games")
            return
        name = exe_display_name(path)
        game = {"path": path, "name": name, "priority": True, "fso_by_us": False}
        self.games.append(game)
        self.games.sort(key=lambda g: g.get("name", "").lower())
        pinned = self._pin_fast_gpu([game])
        self._save_games()
        self._emit("ok", f"{name} added - it gets high CPU priority whenever it runs"
                         + (f" and runs on the {self.gpu_setup['fast']}" if pinned else ""))
        self._paint_games()
        self._render()

    @staticmethod
    def _game_switch_at(lx: float) -> str:
        rel = lx - L_GAME_AUTO[0]
        return "game:auto" if rel < L_GAME_SPLIT - 8 else "game:overlay" if rel < L_GAME_SPLIT2 - 8 else "game:background"

    def _game_action(self, what: str, index: int) -> None:
        if what == "background":
            on = not self._setting("slow_background")
            self._set_setting("slow_background", on)
            if self.game_watcher is not None:
                self.game_watcher.slow_background = on
            self._emit("ok", "While a game runs, browsers, launchers and cloud sync get low priority and "
                             "efficiency mode. Discord and Spotify are left alone." if on
                       else "Background apps are no longer slowed while you play")
            self._render()
            return
        if what == "overlay":
            on = not self._setting("overlay")
            self._set_setting("overlay", on)
            if on and self.sessions_live:
                self._toggle_overlay(True, auto=True)
            elif not on and self._overlay_auto:
                self._toggle_overlay(False)
            self._emit("ok", f"The FPS overlay will show while a game runs ({self._hk('overlay')} toggles it any time)"
                       if on else f"The overlay no longer opens by itself; {self._hk('overlay')} still shows it")
            self._render()
            return
        if what == "auto":
            on = not self._setting("game_auto")
            self._set_setting("game_auto", on)
            self._emit("ok", "Astra Optimizer will apply your mode while a game runs" if on
                       else "Games no longer switch modes automatically")
            self._render()
            return
        if not 0 <= index < len(self.games):
            return
        game = self.games[index]
        name, path = game.get("name") or os.path.basename(game["path"]), game["path"]
        if what == "report":
            if self._last_session(path):
                self._open_session_history(path)
            return
        if what == "prio":
            game["priority"] = not game.get("priority", True)
            self._save_games()
            for pid, exe in self._running_games().items():
                if exe == os.path.normcase(path):
                    error = set_process_priority(pid, game["priority"])
                    if error:
                        self._emit("warn", f"{name}: priority not changed - {error}")
            self._emit("ok", f"{name}: high priority {'on' if game['priority'] else 'off'}")
        elif what == "win":
            game["winkey"] = not game.get("winkey")
            self._save_games()
            if not self._setting("startkey"):
                self._emit("info", f"{name}: {'marked' if game['winkey'] else 'unmarked'}. It takes effect once "
                                   "Settings > Windows > Block the Start menu key in games is on")
            elif game["winkey"]:
                self._emit("ok", f"{name}: a lone Windows key press won't open Start while it's in front")
            elif any(g.get("winkey") for g in self.games):
                self._emit("ok", f"{name}: the Windows key opens Start as usual again")
            else:
                self._emit("ok", "No games marked, so a lone Windows key press is blocked in every game again")
        elif what == "fso":
            want = not fso_disabled(path)
            try:
                set_fso_disabled(path, want)
                game["fso_by_us"] = want
                self._save_games()
                self._emit("ok", f"{name}: fullscreen optimizations {'off' if want else 'back on'} "
                                 "(applies next time it starts)")
            except OSError as exc:
                self._emit("fail", f"{name}: could not change fullscreen optimizations ({exc.strerror or exc})")
        elif what == "del":
            if game.get("fso_by_us") and fso_disabled(path):
                try:
                    set_fso_disabled(path, False)
                except OSError:
                    pass
            if game.get("gpu_by_us") and gpu_preference(path) == 2:
                try:
                    set_gpu_preference(path, None)  # back to "Let Windows decide"
                except OSError:
                    pass
            del self.games[index]
            self.game_offset = max(0, min(self.game_offset, len(self.games) - GAME_ROWS))
            self._save_games()
            self._emit("ok", f"{name} removed from your games")
        self._paint_games()
        self._render()

    # ---------------- gaming checkup ----------------

    def _checkup_score(self) -> str:
        if self.checkup_state == "running" and not self.checkup:
            return "Checking your PC…"
        if not self.checkup:
            return "Not checked yet"
        total = len(CHECKUP_ITEMS)
        good = sum(1 for key, _t in CHECKUP_ITEMS if (self.checkup.get(key) or {}).get("status") == "ok")
        warn = sum(1 for key, _t in CHECKUP_ITEMS if (self.checkup.get(key) or {}).get("status") == "warn")
        text = f"All {total} look good" if good == total else f"{good} of {total} look good"
        if warn:
            text += f"  ·  {warn} worth fixing"
        return text

    def _paint_checkup(self) -> None:
        c = self.canvas
        c.itemconfigure(self.t_check_score, text=self._checkup_score())
        bw = L_CHECK_BTN[0]
        titles = dict(CHECKUP_ITEMS)
        for slot, key, (rx, _ry, rw, _rh) in self._check_slots():
            items = self.t_check[slot]
            c.itemconfigure(items["title"], text=titles[key])
            result = self.checkup.get(key)
            if result is None:
                text = "Checking…" if self.checkup_state == "running" else "Not checked yet."
                c.itemconfigure(items["detail"], text=text, width=0)
                c.itemconfigure(items["value"], text="")
                c.itemconfigure(items["btn"], text="")
                continue
            action = result.get("action")
            value = "" if action else result.get("value", "")
            right = bw + 16 if action else (self.f_plan.measure(value) / self.scale + 18 if value else 0)
            room = rw - 52 - 20 - max(right, 90)
            c.itemconfigure(items["detail"], text=result["detail"], width=self.px(room))
            c.itemconfigure(items["value"], text=value)
            c.itemconfigure(items["btn"], text=action[1] if action else "")

    def _check_slots(self):
        """(slot, key, rect) for the checks currently scrolled into view."""
        for slot, rect in enumerate(L_CHECK_SLOTS):
            index = self.check_offset + slot
            if index < len(CHECKUP_ITEMS):
                yield slot, CHECKUP_ITEMS[index][0], rect

    def _pin_fast_gpu(self, games: list[dict]) -> bool:
        """On a laptop with two graphics chips, start these games on the strong one (unless the user
        already chose something for them in Windows)."""
        if not (self.gpu_setup and self.gpu_setup.get("hybrid")):
            return False
        pinned = False
        for game in games:
            try:
                if gpu_preference(game["path"]) is None:
                    set_gpu_preference(game["path"], 2)
                    game["gpu_by_us"] = True
                    pinned = True
            except OSError:
                continue
        return pinned

    def _detect_gpu_setup(self) -> None:
        """Laptops only: find out early whether there are two graphics chips, for new games."""
        if not has_battery():
            return

        def work() -> None:
            try:
                setup = gpu_setup(checkup_system_info()["gpus"], True)
            except Exception:
                return
            self.events.put(("gpu-setup", setup))

        threading.Thread(target=work, name="gpu-setup", daemon=True).start()

    def _maybe_auto_checkup(self) -> None:
        """First visit (or a stale result): run it straight away, it only reads."""
        if self.checkup_state == "idle" and (not self.checkup_at or time.monotonic() - self.checkup_at > 600):
            self.after(120, self._run_checkup)

    def _run_checkup(self) -> None:
        if self.checkup_state == "running":
            return
        self.checkup_state, self.checkup = "running", {}
        self._paint_checkup()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

        def work() -> None:
            try:
                results = run_checkup([dict(g) for g in self.games])
            except Exception as exc:
                results = {"error": str(exc) or type(exc).__name__}
            self.events.put(("checkup", results))

        threading.Thread(target=work, name="checkup", daemon=True).start()

    def _on_checkup(self, results: dict) -> None:
        self.checkup_state, self.checkup_at = "idle", time.monotonic()
        error = results.pop("error", None) if isinstance(results, dict) else "no results"
        if isinstance(results, dict) and "_setup" in results:
            self.gpu_setup = results.pop("_setup")
        if error:
            self._emit("fail", f"Checkup failed: {error}")
        self.checkup = results
        issues = [title for key, title in CHECKUP_ITEMS if (results.get(key) or {}).get("status") == "warn"]
        if issues:
            self._emit("info", f"Checkup: {len(issues)} thing{'s' if len(issues) != 1 else ''} to look at "
                               f"({', '.join(t.lower() for t in issues)}). Details in Tools → Checkup")
        elif not error:
            self._emit("ok", "Checkup: everything looks good")
        self._paint_checkup()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _checkup_action(self, key: str) -> None:
        result = self.checkup.get(key) or {}
        action = result.get("action")
        if not action:
            return
        if action[0] == "link":
            self._open_link(action[2])
            return
        if action[0] == "cleanup":
            self._switch_tool_page("cleanup")
            return
        if action[0] == "gamegpu":
            paths = [os.path.normcase(p) for p in result.get("games", [])]
            names = [self._game_name(p) for p in paths]
            body = (", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
                    + f" will start on the {result.get('fast', 'graphics card')} from now on. Removing a game from "
                      "Astra puts its setting back.")
            self._open_dialog("Use the fast graphics chip?", body, action[1], lambda: self._fix_game_gpu(paths))
            return
        if action[0] == "features":
            body = ("Turns on " + ", ".join(result.get("off", [])) + ". GPU scheduling takes effect after a "
                    "restart.\n\nYou can change these back in Windows Settings under Gaming and Display > Graphics.")
            self._open_dialog("Turn on gaming features?", body, action[1],
                              lambda: self._run_fix("Gaming features", lambda: fix_features(self.admin)))
            return
        if action[0] == "trim":
            def trim() -> list[str]:
                rc, out = run_command(["fsutil", "behavior", "set", "DisableDeleteNotify", "0"], timeout=20)
                return [] if rc == 0 else [first_line(out) or f"fsutil failed ({rc})"]
            self._open_dialog("Turn on TRIM?", "Windows will tell your SSDs which space is free again, which keeps "
                              "them fast. This is the Windows default.", action[1], lambda: self._run_fix("TRIM", trim))
            return
        if action[0] == "display":
            fixes = result["fix"]
            lines = [f"{label}: {old} Hz → {hz} Hz" for _device, hz, label, old in fixes]
            body = ("\n".join(lines) + "\n\nThe screen may go black for a moment while it switches. "
                    "You can change it back in Windows display settings.")
            self._open_dialog("Use the highest refresh rate?", body, action[1], lambda: self._fix_display(fixes))

    def _fix_game_gpu(self, paths: list[str]) -> None:
        games = [g for g in self.games if os.path.normcase(g["path"]) in paths]
        failed = 0
        for game in games:
            try:
                set_gpu_preference(game["path"], 2)
                game["gpu_by_us"] = True
            except OSError:
                failed += 1
        self._save_games()
        fast = (self.checkup.get("gamegpu") or {}).get("fast", "fast graphics chip")
        self._emit("ok" if not failed else "warn",
                   f"{plural(len(games) - failed, 'game')} now start on the {fast}"
                   + (f", {failed} couldn't be changed" if failed else ""))
        self._run_checkup()

    def _run_fix(self, label: str, work_fn) -> None:
        """Run a checkup fix off the UI thread, then check again."""
        self.checkup_state = "running"
        self._render()

        def work() -> None:
            try:
                failed = work_fn()
            except Exception as exc:
                failed = [str(exc)]
            self.events.put(("checkup-fixed", label, failed))

        threading.Thread(target=work, name="checkup-fix", daemon=True).start()

    def _on_checkup_fixed(self, label: str, failed: list) -> None:
        if failed:
            self._emit("warn", f"{label}: not everything changed - {'; '.join(failed)}")
        else:
            self._emit("ok", f"{label}: turned on")
        self.checkup_state = "idle"
        self._run_checkup()

    def _fix_display(self, fixes: list) -> None:
        self.checkup_state = "running"
        self._render()

        def work() -> None:
            outcomes = []
            for device, hz, label, _old in fixes:
                try:
                    ok, note = set_display_refresh(device, hz)
                except Exception as exc:
                    ok, note = False, str(exc)
                outcomes.append((label, hz, ok, note))
            self.events.put(("display-fixed", outcomes))

        threading.Thread(target=work, name="display-fix", daemon=True).start()

    def _on_display_fixed(self, outcomes: list) -> None:
        for label, hz, ok, note in outcomes:
            if ok:
                self._emit("ok", f"{label} now runs at {hz} Hz" + (f" ({note})" if note else ""))
            else:
                self._emit("fail", f"{label}: couldn't switch to {hz} Hz - {note}")
        self.checkup_state = "idle"
        self.checkup["display"] = check_display()
        self._paint_checkup()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    # ---------------- privacy ----------------

    def _refresh_privacy(self) -> None:
        try:
            self.privacy_state = {key: privacy_is_on(key) for key in PRIVACY_KEYS}
        except Exception:
            self.privacy_state = {}
        self._paint_privacy()

    def _paint_privacy(self) -> None:
        on = sum(1 for k in PRIVACY_KEYS if self.privacy_state.get(k))
        if on == len(PRIVACY_KEYS):
            note = "All turned off. Each switch puts back your original setting."
        elif on:
            note = f"{on} of {len(PRIVACY_KEYS)} turned off. Nothing here changes performance."
        else:
            note = "Nothing changed yet. These are about privacy and clutter, not performance."
        self.canvas.itemconfigure(self.t_priv_note, text=note)

    def _toggle_privacy(self, keys: list, on: bool) -> None:
        if not keys or self._privacy_busy:
            return
        self._privacy_busy = True
        saved_all = dict(self.state_store.get("privacy_saved") or {})
        self._privacy_defaults = [] if on else [k for k in keys if not saved_all.get(k)]

        def work() -> None:
            done, errors, saved = [], [], dict(saved_all)
            for key in keys:
                try:
                    if on:
                        saved[key] = privacy_apply(key, saved.get(key))
                    else:
                        privacy_restore(key, saved.get(key))
                        saved.pop(key, None)
                    done.append(key)
                except OSError as exc:
                    errors.append((key, exc.strerror or str(exc)))
            SystemTweaks.broadcast_setting_change("Policy")
            SystemTweaks.broadcast_setting_change("TraySettings")
            self.events.put(("privacy", on, done, errors, saved))

        threading.Thread(target=work, name="privacy", daemon=True).start()
        self._render()

    def _on_privacy(self, on: bool, done: list, errors: list, saved: dict) -> None:
        self._privacy_busy = False
        self.state_store.set("privacy_saved", saved)
        titles = {key: title for key, title, _d, _v in PRIVACY_ITEMS}
        if len(done) == 1 and not on and done[0] in self._privacy_defaults:
            # It was already private before Astra touched it, so there is no "before" to go back to.
            self._emit("ok", f"{titles[done[0]]}: off, back to the Windows default")
        elif len(done) == 1:
            self._emit("ok", f"{titles[done[0]]}: {'on' if on else 'off, original setting restored'}")
        elif done:
            self._emit("ok", f"{len(done)} privacy settings {'turned on' if on else 'restored'}")
        for key, message in errors:
            self._emit("fail", f"{titles[key]}: couldn't change it - {message}")
        self._refresh_privacy()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    # ---------------- game session reports ----------------

    def _start_session(self, exe: str) -> None:
        if exe in self.sessions_live:
            return
        session = self.sessions_live[exe] = new_session(exe, self._game_name(exe))
        if self.admin and presentmon_path():
            capture = FrameCapture(os.path.basename(exe))
            capture.start()
            session["capture"] = capture
        if self._setting("overlay"):
            self._toggle_overlay(True, auto=True)
        if self.session_pinger is None:
            self.session_pinger = SessionPinger(self.events)
            self.session_pinger.start()

    def _end_session(self, exe: str) -> None:
        if self.bench and self.bench["exe"] == exe:
            self._end_benchmark(f"Benchmark of {self.bench['name']} stopped: the game closed")
        session = self.sessions_live.pop(exe, None)
        if not self.sessions_live and self.session_pinger is not None:
            self.session_pinger.stop_event.set()
            self.session_pinger = None
        if not self.sessions_live:
            self._last_ping = None
            if self._overlay_auto:
                self._toggle_overlay(False)
        if session is None:
            return
        capture = session.pop("capture", None)
        if capture is not None:
            capture.stop()
            session["fps"] = capture.summary()
            if session["fps"] is None and capture.error:
                self._emit("warn", f"FPS wasn't measured for {session['name']}: {capture.error}")
        report = finish_session(session)
        if report is None:
            return
        saved = list(self._setting("sessions") or [])
        saved.append(report)
        self._set_setting("sessions", saved[-SESSION_KEEP:])
        self._emit("info", f"{report['name']} session: {session_summary(report)}. Report in Tools → Games")
        if self._window_visible() and not self.dialog:
            self._show_session_report(report)
        else:
            self._notify(f"{report['name']} session", session_summary(report))

    def _last_session(self, path: str) -> dict | None:
        exe = os.path.normcase(path)
        for report in reversed(self._setting("sessions") or []):
            if os.path.normcase(report.get("exe", "")) == exe:
                return report
        return None

    def _show_session_report(self, report: dict) -> None:
        kind = "benchmark" if report.get("benchmark") else "session"
        self._open_dialog(f"{report['name']} {kind}", session_report_text(report), "Done", lambda: None,
                          cancel_label=None, width=460)

    def _window_visible(self) -> bool:
        try:
            return self.state() == "normal" and not user32.IsIconic(self._hwnd())
        except tk.TclError:
            return False

    def _game_sessions(self, path: str) -> list[dict]:
        """This game's saved sessions, newest first."""
        exe = os.path.normcase(path)
        return [r for r in reversed(self._setting("sessions") or []) if os.path.normcase(r.get("exe", "")) == exe]

    def _open_session_history(self, path: str) -> None:
        self.session_view, self.game_offset = path, 0
        self.compare_pick = self.compare_pair = None
        self._paint_games()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _close_session_history(self) -> None:
        self.session_view, self.game_offset = None, 0
        self.compare_pick = self.compare_pair = None
        self._clear_compare_texts()
        self._paint_games()
        self._render()

    def _paint_session_history(self) -> None:
        c = self.canvas
        self._clear_compare_texts()
        if self.compare_pair:
            self._paint_compare()
            return
        path = self.session_view
        sessions = self._game_sessions(path)
        name = self._game_name(os.path.normcase(path))
        c.itemconfigure(self.t_buttons["findgames"], text="Back")
        c.itemconfigure(self.t_buttons["addgame"], text="Cancel" if self.compare_pick is not None else "Compare")
        benches = sum(1 for r in sessions if r.get("benchmark"))
        played = len(sessions) - benches
        counts = [f"{played} SESSION{'S' if played != 1 else ''}"] if played or not benches else []
        if benches:
            counts.append(f"{benches} BENCHMARK{'S' if benches != 1 else ''}")
        c.itemconfigure(self.t_game_heads["name"], text=self._fit(f"{name.upper()}  ·  {'  ·  '.join(counts)}",
                                                                  self.f_label, 280))
        for col in GAME_HEADS:
            c.itemconfigure(self.t_game_heads[col], text="")
        for col, label in (("fps", "FPS · 1% LOW"), ("cpu", "CPU"), ("gpu", "GPU"), ("temp", "HOTTEST"),
                           ("ping", "PING")):
            c.itemconfigure(self.t_game_heads[col], text=label)
        for i, row in enumerate(self.t_games):
            index = self.game_offset + i
            if index >= len(sessions):
                for item in row.values():
                    c.itemconfigure(item, text="")
                continue
            r = sessions[index]
            when = format_day(r["end"])
            c.itemconfigure(row["name"], text=f"{when[:1].upper()}{when[1:]}, {format_clock(r['start'])}",
                            fill=TEXT_HI)
            c.itemconfigure(row["sub"], text=(f"Benchmark · {r.get('label') or 'No mode'}" if r.get("benchmark")
                                              else format_duration(r["end"] - r["start"])),
                            fill=SILVER if r.get("benchmark") else TEXT_MID)
            hot = (r.get("temp_peak") or 0) >= 85
            lossy = (r.get("ping_loss") or 0) >= 2 or (r.get("ping_spikes") or 0) >= 5
            c.itemconfigure(row["fps"], text="–" if r.get("fps_avg") is None
                            else f"{r['fps_avg']:.0f} / {r['fps_low1']:.0f}", fill=TEXT_HI)
            c.itemconfigure(row["cpu"], text=f"{r['cpu_avg']:.0f}%", fill=TEXT_HI)
            c.itemconfigure(row["gpu"], text="–" if r.get("gpu_avg") is None else f"{r['gpu_avg']:.0f}%", fill=TEXT_HI)
            c.itemconfigure(row["temp"], text="–" if r.get("temp_peak") is None else f"{r['temp_peak']:.0f} °C",
                            fill=WARN_YELLOW if hot else TEXT_HI)
            c.itemconfigure(row["ping"], text="–" if r.get("ping_avg") is None else f"{r['ping_avg']:.0f} ms",
                            fill=WARN_YELLOW if lossy else TEXT_HI)
        c.itemconfigure(self.t_games_empty, text="" if sessions else "No sessions saved for this game yet.")
        ax, ay, aw, ah = L_GAME_AUTO
        c.coords(self.t_game_auto, self.px(ax + 18), self.px(ay + ah / 2))  # no switch in this view
        if self.compare_pick is not None:
            summary = f"Pick two sessions to compare ({len(self.compare_pick)} of 2 picked)"
            if not any(r.get("benchmark") for r in sessions):
                summary += f". For a fair test, press {self._hk('bench')} in the game: a {BENCH_SECONDS} s benchmark"
        else:
            summary = self._history_summary(sessions)
        c.itemconfigure(self.t_game_overlay, text="")
        c.itemconfigure(self.t_game_background, text="")
        c.itemconfigure(self.t_game_auto, text=self._fit(summary, self.f_desc, aw - 36))

    def _clear_compare_texts(self) -> None:
        c = self.canvas
        for item in self.t_cmp_heads.values():
            c.itemconfigure(item, text="")
        for row in self.t_cmp:
            for item in row.values():
                c.itemconfigure(item, text="")

    @staticmethod
    def _compare_value(report: dict, key: str):
        if key == "spike_rate":
            if report.get("fps_spikes") is None:
                return None
            hours = max(1 / 60, (report["end"] - report["start"]) / 3600)
            return report["fps_spikes"] / hours
        return report.get(key)

    @staticmethod
    def _compare_change(key: str, old, new, better):
        """(text, colour) for the change from the older to the newer session."""
        if old is None or new is None:
            return "", TEXT_MID
        if key.startswith("fps_"):
            pct = (new - old) / old * 100 if old else 0
            if abs(pct) < 2:
                return "Same", TEXT_MID
            text, delta = f"{pct:+.0f}%", pct
        else:
            delta = new - old
            if abs(delta) < 1:
                return "Same", TEXT_MID
            unit = {"spike_rate": "/h", "gpu_avg": " pts", "temp_peak": " °C", "ping_avg": " ms"}[key]
            text = f"{delta:+.0f}{unit}"
        if better is None:
            return text, TEXT_MID
        good = delta > 0 if better == "up" else delta < 0
        return text, TEXT_HI if good else WARN_YELLOW

    def _paint_compare(self) -> None:
        c = self.canvas
        old, new = self.compare_pair
        c.itemconfigure(self.t_buttons["findgames"], text="Back")
        c.itemconfigure(self.t_buttons["addgame"], text="")
        c.itemconfigure(self.t_game_heads["name"], text="COMPARING")
        for col in (*GAME_HEADS, *L_HIST_COLS):
            c.itemconfigure(self.t_game_heads[col], text="")

        def when(r: dict) -> str:
            return f"{format_day(r['end']).upper()}, {format_clock(r['start'])}"

        c.itemconfigure(self.t_cmp_heads["a"], text=when(old))
        c.itemconfigure(self.t_cmp_heads["b"], text=when(new))
        c.itemconfigure(self.t_cmp_heads["chg"], text="CHANGE")
        units = {"fps": "", "/h": "/h", "%": "%", "°C": " °C", "ms": " ms"}
        for row, cmp_row, (key, label, hint, unit, better) in zip(self.t_games, self.t_cmp, COMPARE_METRICS):
            a, b = self._compare_value(old, key), self._compare_value(new, key)
            c.itemconfigure(row["name"], text=label, fill=TEXT_HI)
            c.itemconfigure(row["sub"], text=hint, fill=TEXT_MID)
            for col in L_HIST_COLS:
                c.itemconfigure(row[col], text="")
            c.itemconfigure(cmp_row["a"], text="–" if a is None else f"{a:.0f}{units[unit]}", fill=TEXT_HI)
            c.itemconfigure(cmp_row["b"], text="–" if b is None else f"{b:.0f}{units[unit]}", fill=TEXT_HI)
            text, colour = self._compare_change(key, a, b, better)
            c.itemconfigure(cmp_row["chg"], text=text, fill=colour)
        c.itemconfigure(self.t_games_empty, text="")
        ax, ay, aw, ah = L_GAME_AUTO
        c.coords(self.t_game_auto, self.px(ax + 18), self.px(ay + ah / 2))
        c.itemconfigure(self.t_game_overlay, text="")
        c.itemconfigure(self.t_game_background, text="")
        c.itemconfigure(self.t_game_auto, text=self._fit(self._compare_note(old, new), self.f_desc, aw - 36))

    @staticmethod
    def _compare_note(old: dict, new: dict) -> str:
        """Whether the two sessions can fairly be compared."""
        da, db = old["end"] - old["start"], new["end"] - new["start"]
        if old.get("benchmark") and new.get("benchmark"):
            a, b = old.get("label") or "No mode", new.get("label") or "No mode"
            setup = f"{a} vs {b}" if a != b else f"both with {a}"
            return f"Two {BENCH_SECONDS}-second benchmarks, {setup}. Same spot in the game both times keeps it fair."
        if old.get("benchmark") or new.get("benchmark"):
            return "A benchmark and a full session measure different stretches of play; compare two benchmarks instead."
        if old.get("fps_avg") is None or new.get("fps_avg") is None:
            return "One of these sessions has no FPS data, so only the other numbers compare."
        if min(da, db) < 300:
            return "One session is under 5 minutes, too short for a reliable comparison."
        if max(da, db) / max(1, min(da, db)) > 3:
            return (f"Very different lengths ({format_duration(da)} vs {format_duration(db)}), so the numbers may "
                    "not be comparable.")
        return f"{format_duration(da)} vs {format_duration(db)}. For a fair test, play the same map or mode both times."

    def _toggle_compare_pick(self) -> None:
        self.compare_pick = None if self.compare_pick is not None else []
        self._paint_games()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _pick_session(self, index: int) -> None:
        if index in self.compare_pick:
            self.compare_pick.remove(index)
        else:
            self.compare_pick.append(index)
        if len(self.compare_pick) == 2:
            sessions = self._game_sessions(self.session_view)
            pair = [sessions[i] for i in self.compare_pick if 0 <= i < len(sessions)]
            self.compare_pick = None
            if len(pair) == 2:
                self.compare_pair = sorted(pair, key=lambda r: r["start"])
        self._paint_games()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    @staticmethod
    def _history_summary(sessions: list[dict]) -> str:
        if not sessions:
            return ""
        benches = sum(1 for r in sessions if r.get("benchmark"))
        sessions = [r for r in sessions if not r.get("benchmark")]
        played = sum(r["end"] - r["start"] for r in sessions)
        bench_text = f"{benches} benchmark{'s' if benches != 1 else ''}"
        if not sessions:
            return bench_text
        parts = [f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}, {format_duration(played)} played"]
        if benches:
            parts[0] += f", {bench_text}"
        fps = [r["fps_avg"] for r in sessions if r.get("fps_avg") is not None]
        gpu = [r["gpu_avg"] for r in sessions if r.get("gpu_avg") is not None]
        temps = [r["temp_peak"] for r in sessions if r.get("temp_peak") is not None]
        pings = [r["ping_avg"] for r in sessions if r.get("ping_avg") is not None]
        if fps:
            parts.append(f"{sum(fps) / len(fps):.0f} FPS on average")
        if gpu:
            parts.append(f"GPU {sum(gpu) / len(gpu):.0f}% on average")
        if temps:
            parts.append(f"hottest {max(temps):.0f} °C")
        if pings:
            parts.append(f"ping {sum(pings) / len(pings):.0f} ms")
        return "  ·  ".join(parts)

    def _paint_games(self) -> None:
        c = self.canvas
        finder = self.game_finder
        if self.session_view and not finder:
            self._paint_session_history()
            return
        self._clear_compare_texts()
        for row in self.t_games:
            for col in L_HIST_COLS:
                c.itemconfigure(row[col], text="")
        for col in L_HIST_COLS:
            c.itemconfigure(self.t_game_heads[col], text="")
        ax, ay, aw, ah = L_GAME_AUTO
        c.coords(self.t_game_auto, self.px(ax + 76), self.px(ay + ah / 2))
        c.itemconfigure(self.t_game_auto, text=GAME_AUTO_TEXT)
        c.itemconfigure(self.t_game_overlay, text=self._game_overlay_text())
        c.itemconfigure(self.t_game_background, text=GAME_BACKGROUND_TEXT)
        c.itemconfigure(self.t_buttons["findgames"], text="Cancel" if finder else "Find games")
        if finder:
            items = finder.get("items", [])
            picked = sum(1 for i in items if i["pick"])
            c.itemconfigure(self.t_buttons["addgame"],
                            text=f"Add {picked} game{'s' if picked != 1 else ''}" if items else "Add games")
            c.itemconfigure(self.t_game_heads["name"], text=f"FOUND ON THIS PC ({len(items)})")
            c.itemconfigure(self.t_game_heads["prio"], text="")
            c.itemconfigure(self.t_game_heads["fso"], text="")
            c.itemconfigure(self.t_game_heads["win"], text="ADD")
            for i, row in enumerate(self.t_games):
                index = self.game_offset + i
                if index >= len(items):
                    c.itemconfigure(row["name"], text="")
                    c.itemconfigure(row["sub"], text="")
                    continue
                item = items[index]
                c.itemconfigure(row["name"], text=self._fit(item["name"], self.f_desc, 350),
                                fill=TEXT_HI if item["pick"] else ASH)
                c.itemconfigure(row["sub"], text=self._fit(f"{item['source']} \u00b7 {item['path']}", self.f_label, 350),
                                fill=TEXT_MID)
            if finder["state"] == "searching":
                empty = "Looking through Steam, Epic Games, Riot and GOG…"
            elif not items:
                empty = "No new games found. Games from other launchers can be added with Add game…"
            else:
                empty = ""
            c.itemconfigure(self.t_games_empty, text=empty)
            return
        c.itemconfigure(self.t_buttons["addgame"], text="Add game…")
        c.itemconfigure(self.t_game_heads["name"], text="GAME")
        for col, label in GAME_HEADS.items():
            c.itemconfigure(self.t_game_heads[col], text=label)
        running = set(self._running_games().values())
        for i, items in enumerate(self.t_games):
            index = self.game_offset + i
            if index >= len(self.games):
                c.itemconfigure(items["name"], text="")
                c.itemconfigure(items["sub"], text="")
                continue
            game = self.games[index]
            game["_fso"] = fso_disabled(game["path"])
            c.itemconfigure(items["name"], text=self._fit(game.get("name") or game["path"], self.f_desc, L_GAME_NAME_W))
            if os.path.normcase(game["path"]) in running:
                sub = "Running now" + (" \u00b7 high priority" if game.get("priority", True) else "")
                c.itemconfigure(items["sub"], text=sub, fill=TEXT_HI)
            elif not os.path.isfile(game["path"]):
                c.itemconfigure(items["sub"], text="File not found - it may have moved", fill=WARN_YELLOW)
            elif (last := self._last_session(game["path"])) is not None:
                count = len(self._game_sessions(game["path"]))
                sub = (f"Last played {format_day(last['end'])} for {format_duration(last['end'] - last['start'])}"
                       f"  ·  {'View report' if count == 1 else f'{count} sessions'}")
                c.itemconfigure(items["sub"], text=self._fit(sub, self.f_label, L_GAME_NAME_W), fill=SILVER)
            else:
                c.itemconfigure(items["sub"], text=self._fit(game["path"], self.f_label, L_GAME_NAME_W), fill=TEXT_MID)
        c.itemconfigure(self.t_games_empty, text="" if self.games else
                        "No games yet. Press Add game… and pick the game's .exe file.\n"
                        "It gets high CPU priority every time it runs.")

    def _on_game_start(self, pid: int, exe: str, wanted: bool, error: str | None, unthrottled: bool = False) -> None:
        name = self._game_name(exe)
        self._start_session(exe)
        if wanted and error is None:
            self._emit("ok", f"{name} started - CPU priority set to High"
                             + (", power throttling off" if unthrottled else ""))
        elif wanted:
            self._emit("warn", f"{name} started, but its priority could not be raised: {error}")
        else:
            self._emit("info", f"{name} started" + (" - power throttling off" if unthrottled else ""))
        self._paint_games()
        if self._setting("game_auto") and self.admin and not self.busy and not self.undo_count \
                and self._game_auto is None:
            self._game_auto = exe
            self._emit("info", f"Applying {PROFILE_TITLES[self.profile]} while {name} runs")
            self._on_optimize()
            if self.busy:
                self._job_origin = "game"

    def _on_game_stop(self, pid: int, exe: str) -> None:
        self._emit("info", f"{self._game_name(exe)} closed")
        if exe not in self._running_games().values():  # its last process is gone
            self._end_session(exe)
        self._paint_games()
        if self._game_auto and not self._running_games():
            self._game_auto = None
            self._undo_after_game()

    def _undo_after_game(self) -> None:
        if self._closing:
            return
        if self.busy:
            self.after(3000, self._undo_after_game)
            return
        self._refresh_primary()
        if self.undo_count and self.state_store.get("applied_profile") in PROFILE_ORDER:
            self._emit("info", "Game closed - undoing the automatic changes")
            self._start_job("Undo changes", SystemTweaks.undo_changes, busy_label="UNDOING…")
            self._job_origin = "game"

    def _find_games(self) -> None:
        if self.session_view and not self.game_finder:  # "Back": compare -> history -> your games
            if self.compare_pair or self.compare_pick is not None:
                self.compare_pair = self.compare_pick = None
                self._paint_games()
                self._render()
            else:
                self._close_session_history()
            return
        if self.game_finder:  # "Cancel"
            self.game_finder = None
            self.game_offset = 0
            self._paint_games()
            self._render()
            return
        self.game_finder = {"state": "searching", "items": []}
        self.game_offset = 0
        self._paint_games()
        self._render()
        known = {os.path.normcase(g["path"]) for g in self.games}

        def work() -> None:
            try:
                found = [g for g in find_installed_games() if os.path.normcase(g["path"]) not in known]
            except Exception:
                found = []
            self.events.put(("games-found", found))

        threading.Thread(target=work, name="find-games", daemon=True).start()

    def _on_games_found(self, found: list[dict]) -> None:
        if not self.game_finder:
            return
        self.game_finder = {"state": "ready", "items": [dict(g, pick=True) for g in found]}
        self._emit("info", f"Found {len(found)} game{'s' if len(found) != 1 else ''} not in your list yet")
        self._paint_games()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _toggle_found_game(self, index: int) -> None:
        items = (self.game_finder or {}).get("items", [])
        if 0 <= index < len(items):
            items[index]["pick"] = not items[index]["pick"]
            self._paint_games()
            self._render()

    def _add_found_games(self) -> None:
        picked = [g for g in (self.game_finder or {}).get("items", []) if g["pick"]]
        known = {os.path.normcase(g["path"]) for g in self.games}
        added = []
        for game in picked:
            if os.path.normcase(game["path"]) not in known:
                added.append({"path": game["path"], "name": game["name"], "priority": True, "fso_by_us": False})
        self.games.extend(added)
        self.games.sort(key=lambda g: g.get("name", "").lower())
        if self._pin_fast_gpu(added):
            self._emit("ok", f"New games will run on the {self.gpu_setup['fast']}")
        self.game_finder = None
        self.game_offset = 0
        self._save_games()
        self._emit("ok", f"Added {len(picked)} game{'s' if len(picked) != 1 else ''} - each gets high CPU "
                         "priority whenever it runs")
        self._paint_games()
        self._render()

    # ---------------- DNS ----------------

    def settings_dns_original(self):
        return self._setting("dns_original")

    def _load_dns_info(self) -> None:
        if self.dns_state != "idle":
            return
        self.dns_state = "loading"
        self._paint_dns()

        def work() -> None:
            try:
                info = dns_adapter_info()
            except Exception:
                info = None
            self.events.put(("dns-info", info))

        threading.Thread(target=work, name="dns-info", daemon=True).start()

    def _on_dns_info(self, info: dict | None) -> None:
        self.dns_state = "idle"
        self.dns_info = info
        self.dns_rows[0]["servers"] = list(info["servers"]) if info else []
        if info is None:
            self._dns_note = ("Could not find the network adapter you use for the internet.", ERROR_RED)
        self._apply_visibility()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _start_dns_test(self) -> None:
        if self.dns_state != "idle":
            return
        self.dns_state = "testing"
        self._dns_run += 1
        self._dns_pending = 0
        for index, row in enumerate(self.dns_rows):
            row.update(median=None, answered=None, total=None, status="idle")
            if not row["servers"]:
                continue
            row["status"] = "testing"
            self._dns_pending += 1
            threading.Thread(target=self._dns_worker, args=(self._dns_run, index, row["servers"][0]),
                             name="dns-test", daemon=True).start()
        if not self._dns_pending:
            self.dns_state = "idle"
        self._paint_dns()
        self._render()
        self._kick()

    def _dns_worker(self, run: int, index: int, server: str) -> None:
        times = []
        for name in DNS_TEST_NAMES:
            ms = dns_query_ms(server, name)
            if ms is not None:
                times.append(ms)
        times.sort()
        median = times[len(times) // 2] if times else None
        self.events.put(("dns-result", run, index, median, len(times), len(DNS_TEST_NAMES)))

    def _on_dns_result(self, run: int, index: int, median, answered: int, total: int) -> None:
        if run != self._dns_run:
            return
        self.dns_rows[index].update(median=median, answered=answered, total=total, status="done")
        self._dns_pending -= 1
        if self._dns_pending <= 0:
            self.dns_state = "idle"
            done = [(r["median"], r["name"]) for r in self.dns_rows if r["median"] is not None]
            if done:
                best = min(done)
                self._dns_note = (f"Fastest here: {best[1]} at {best[0]:.0f} ms.", TEXT_HI)
                self._emit("ok", f"DNS test finished: fastest was {best[1]} at {best[0]:.0f} ms")
            self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
            self._render()
        self._paint_dns()

    def _confirm_dns_switch(self, index: int) -> None:
        row, info = self.dns_rows[index], self.dns_info
        if not info:
            return
        self._open_dialog(
            f"Switch to {row['name']} DNS?",
            f"Your {info['alias']} adapter will look up websites and game servers through "
            f"{' and '.join(row['servers'])}. It applies straight away, and Restore previous puts your old "
            "setting back.",
            f"Use {row['name']}", lambda: self._switch_dns(row["servers"], row["name"]))

    def _confirm_dns_restore(self) -> None:
        original = self.settings_dns_original()
        if not original:
            return
        target = ", ".join(original["static"]) if original.get("static") else "the servers your router provides"
        self._open_dialog("Restore your previous DNS?", f"{original.get('alias', 'Your adapter')} goes back to {target}.",
                          "Restore", lambda: self._switch_dns(None, "your previous DNS"))

    def _switch_dns(self, servers: list[str] | None, label: str) -> None:
        info = self.dns_info
        original = self.settings_dns_original()
        if servers is not None and not original and info:
            # remember what was there before the first switch (static list, or [] for automatic)
            self._set_setting("dns_original", {"if_index": info["if_index"], "alias": info["alias"],
                                               "static": list(info["static"])})
            original = self.settings_dns_original()
        if servers is None:
            if not original:
                return
            if_index, servers = original["if_index"], (original["static"] or None)
        else:
            if_index = info["if_index"]
        self.dns_state = "switching"
        self._paint_dns()
        self._render()

        def work() -> None:
            ok, message = set_dns_servers(if_index, servers)
            self.events.put(("dns-switched", ok, message, label, servers is None or label == "your previous DNS"))

        threading.Thread(target=work, name="dns-switch", daemon=True).start()

    def _on_dns_switched(self, ok: bool, message: str, label: str, restored: bool) -> None:
        self.dns_state = "idle"
        if ok:
            if restored:
                self._set_setting("dns_original", None)
            self._dns_note = (f"Now using {label}. The DNS cache was flushed.", TEXT_HI)
            self._emit("ok", f"DNS switched to {label}")
        else:
            self._dns_note = (f"Could not change DNS: {message}", ERROR_RED)
            self._emit("fail", f"DNS change failed: {message}")
        self.dns_info = None
        self._load_dns_info()

    def _paint_dns(self) -> None:
        c = self.canvas
        current = set(self.dns_info["servers"]) if self.dns_info else set()
        for i, row in enumerate(self.dns_rows):
            items = self.t_dns[i]
            c.itemconfigure(items["name"], text=row["name"])
            if i == 0:
                if self.dns_state == "loading" and not row["servers"]:
                    sub = "Checking your adapter…"
                elif row["servers"]:
                    origin = "set by hand" if self.dns_info and self.dns_info["static"] else "from your router"
                    sub = f"{', '.join(row['servers'][:2])}  ({origin})"
                else:
                    sub = "Not found"
            else:
                sub = " and ".join(row["servers"])
            c.itemconfigure(items["sub"], text=self._fit(sub, self.f_label, 280))
            if row["status"] == "testing":
                median, color = "…", TEXT_MID
            elif row["median"] is None:
                median, color = ("No answer" if row["status"] == "done" else "—"), (ERROR_RED if row["status"] == "done" else TEXT_MID)
            else:
                median, color = f"{row['median']:.0f} ms", (TEXT_HI if row["median"] < 60 else WARN_YELLOW)
            c.itemconfigure(items["median"], text=median, fill=color)
            if row["answered"] is not None:
                c.itemconfigure(items["answered"], text=f"{row['answered']}/{row['total']}",
                                fill=WARN_YELLOW if row["answered"] < row["total"] else TEXT_MID)
            else:
                c.itemconfigure(items["answered"], text="")
            if i == 0 or not row["servers"]:
                use = ""
            elif current & set(row["servers"]):
                use = "In use"
            else:
                use = "Use"
            c.itemconfigure(items["use"], text=use, fill=TEXT_HI if use == "In use" else "#CFCFCF")
        if self.dns_state == "switching":
            note, color = "Changing DNS…", TEXT_MID
        elif self._dns_note:
            note, color = self._dns_note
        elif self.dns_info:
            note, color = (f"Test how fast each service answers, then press Use to switch your "
                           f"{self.dns_info['alias']} adapter."), TEXT_MID
        else:
            note, color = "Test how fast each service answers lookups from this PC.", TEXT_MID
        if not self.admin:
            note, color = "Switching DNS needs administrator rights - testing still works.", TEXT_MID
        c.itemconfigure(self.t_dns_note, text=note, fill=color)

    # ---------------- monitor (telemetry history) ----------------

    @staticmethod
    def _chart_rect(rect) -> tuple[float, float, float, float]:
        x, y, w, h = rect
        return x + 214, y + 12, w - 232, h - 24

    def _record_history(self, data: dict) -> None:
        self.history["cpu"].append(float(data["cpu"]))
        self.history["ram"].append(float(data["ram"]))
        gpu = data.get("gpu") or {}
        self.history["gpu"].append(float(gpu["load"]) if "load" in gpu else None)
        self.history["temp"].append(float(gpu["temp"]) if gpu.get("temp") is not None else None)
        if self.tab == "tools" and self.tool_page == "monitor" and not self.dialog:
            self._paint_monitor()

    def _paint_monitor(self) -> None:
        c, px = self.canvas, self.px
        for key, rect in L_MONITOR.items():
            items, unit = self.t_monitor[key], MONITOR_SPECS[key][1]
            samples = list(self.history[key])
            values = [v for v in samples if v is not None]
            cx, cy, cw, ch = self._chart_rect(rect)
            if key == "temp":
                lo = min(30.0, min(values, default=30.0))
                hi = max(90.0, max(values, default=90.0))
            else:
                lo, hi = 0.0, 100.0
            c.itemconfigure(items["top"], text=f"{hi:.0f}{unit}")
            c.itemconfigure(items["bottom"], text=f"{lo:.0f}{unit}")
            if not values:
                c.itemconfigure(items["value"], text="—")
                c.itemconfigure(items["stats"], text="Not available on this PC" if samples else "Collecting…")
                c.coords(items["line"], px(cx), px(cy + ch), px(cx + 1), px(cy + ch))
                continue
            current = values[-1]
            hot = key == "temp" and current >= 85
            c.itemconfigure(items["value"], text=f"{current:.0f}{unit}", fill=WARN_YELLOW if hot else TEXT_HI)
            minutes = len(samples) * TELEMETRY_INTERVAL / 60
            span = f"last {minutes:.0f} min" if minutes >= 1 else "last few seconds"
            c.itemconfigure(items["stats"], text=f"avg {sum(values) / len(values):.0f}{unit}  \u00b7  "
                                                 f"peak {max(values):.0f}{unit}  \u00b7  {span}")
            points = []
            step = cw / (HISTORY_LEN - 1)
            start = HISTORY_LEN - len(samples)
            for i, value in enumerate(samples):
                if value is None:
                    continue
                frac = (value - lo) / (hi - lo) if hi > lo else 0
                points += [px(cx + (start + i) * step), px(cy + ch - ch * max(0.0, min(1.0, frac)))]
            if len(points) == 2:
                points += [points[0] + 1, points[1]]
            c.coords(items["line"], *points)
            c.itemconfigure(items["line"], fill=WARN_YELLOW if hot else STARLIGHT)

    # ---------------- startup apps ----------------

    def _load_startup(self) -> None:
        try:
            self.startup_items = list_startup_apps()
        except Exception as exc:
            self.startup_items = []
            self.startup_note = (f"Could not read startup apps: {exc}", ERROR_RED)
        self.startup_offset = max(0, min(self.startup_offset, len(self.startup_items) - START_ROWS))
        self._paint_startup()
        self._render()

    def _toggle_startup(self, index: int) -> None:
        if not self.startup_items or not 0 <= index < len(self.startup_items):
            return
        item = self.startup_items[index]
        want = not item["enabled"]
        try:
            set_startup_enabled(item, want)
            item["enabled"] = startup_enabled(item)
            self.startup_note = (f"{item['name']} will {'start' if want else 'no longer start'} with Windows "
                                 "from your next sign-in.", TEXT_HI)
            self._emit("ok", f"Startup app {item['name']}: {'on' if want else 'off'}")
        except OSError as exc:
            reason = "administrator rights are needed" if isinstance(exc, PermissionError) else (exc.strerror or exc)
            self.startup_note = (f"Could not change {item['name']}: {reason}.", ERROR_RED)
            self._emit("fail", f"Startup app {item['name']}: {reason}")
        self._paint_startup()
        self._render()

    def _paint_startup(self) -> None:
        c = self.canvas
        items = self.startup_items or []
        for i, row in enumerate(self.t_startup):
            index = self.startup_offset + i
            if index >= len(items):
                for part in row.values():
                    c.itemconfigure(part, text="")
                continue
            item = items[index]
            color = TEXT_HI if item["enabled"] else ASH
            c.itemconfigure(row["name"], text=self._fit(item["name"], self.f_desc, 330), fill=color)
            c.itemconfigure(row["sub"], text=self._fit(item["detail"], self.f_label, 330))
            c.itemconfigure(row["source"], text=item["source"])
        if self.startup_items is None:
            empty = "Loading startup apps…"
        elif not items:
            empty = "No startup apps found."
        else:
            empty = ""
        c.itemconfigure(self.t_startup_empty, text=empty)
        note, color = self.startup_note or (
            "Same switches as Task Manager's Startup tab. Changes take effect the next time you sign in."
            + (f"  {len(items) - START_ROWS} more - scroll to see them." if len(items) > START_ROWS else ""), TEXT_MID)
        c.itemconfigure(self.t_startup_note, text=note, fill=color)

    # ---------------- temp file cleanup ----------------

    def _clean_total(self) -> tuple[int, int]:
        skip = set(self._setting("clean_skip") or [])
        files = size = 0
        for key, (count, nbytes) in (self.clean_scan or {}).items():
            if key not in skip:
                files += count
                size += nbytes
        return files, size

    def _scan_cleanup(self) -> None:
        if self.clean_state != "idle":
            return
        self.clean_state = "scanning"
        self.canvas.itemconfigure(self.t_buttons["clean"], text="SCANNING…")
        self._paint_cleanup()
        self._render()
        self._kick()
        targets = clean_targets()

        def work() -> None:
            results = {}
            for key, _title, _desc, paths in targets:
                try:
                    results[key] = scan_cleanup(paths)
                except Exception:
                    results[key] = (0, 0)
            self.events.put(("clean-scan", results))

        threading.Thread(target=work, name="clean-scan", daemon=True).start()

    def _toggle_clean_target(self, key: str) -> None:
        if key == "auto":
            on = not self._setting("clean_weekly")
            self._set_setting("clean_weekly", on)
            if on and not self._setting("clean_at"):
                self._set_setting("clean_at", time.time())  # the first weekly run is a week from now
            self._emit("ok", "Cleanup will run once a week while Astra Optimizer is open" if on
                       else "Weekly cleanup is off")
            self._render()
            return
        skip = set(self._setting("clean_skip") or [])
        skip.symmetric_difference_update({key})
        self._set_setting("clean_skip", sorted(skip))
        self._paint_cleanup()
        self._render()

    def _confirm_cleanup(self) -> None:
        files, size = self._clean_total()
        if not size:
            return
        self._open_dialog(
            f"Delete {human_size(size)} of temporary files?",
            f"{files:,} files that nothing has touched for at least a day will be deleted for good - they don't "
            "go to the Recycle Bin. Files that are in use are skipped.",
            "Delete files", self._run_cleanup)

    def _run_cleanup(self, auto: bool = False) -> None:
        if self.clean_state != "idle":
            return
        skip = set(self._setting("clean_skip") or [])
        paths = [p for key, _t, _d, ps in clean_targets() if key not in skip for p in ps]
        self.clean_state = "cleaning"
        self._clean_auto = auto
        self._set_setting("clean_at", time.time())
        self.canvas.itemconfigure(self.t_buttons["clean"], text="CLEANING…")
        self._emit("head", "Weekly cleanup started" if auto else "Temp file cleanup started")
        self._render()
        self._kick()

        def work() -> None:
            try:
                result = run_cleanup(paths)
            except Exception:
                result = (0, 0, 0)
            self.events.put(("clean-done", result))

        threading.Thread(target=work, name="clean", daemon=True).start()

    def _on_clean_event(self, kind: str, payload) -> None:
        self.clean_state = "idle"
        self.canvas.itemconfigure(self.t_buttons["clean"], text="CLEAN UP")
        if kind == "clean-scan":
            self.clean_scan = payload
            self.clean_scanned_at = time.monotonic()
        else:
            freed, deleted, skipped = payload
            self.clean_last = payload
            self._emit("ok", f"{'Weekly cleanup' if self._clean_auto else 'Cleanup'} freed {human_size(freed)}: "
                             f"{deleted:,} files deleted, {skipped:,} in use skipped")
            if self._clean_auto and freed >= 50 * 1024 ** 2 and not self._window_visible():
                self._notify("Weekly cleanup", f"Freed {human_size(freed)} of temporary files.")
            self._clean_auto = False
            self.after(50, self._scan_cleanup)
        self._paint_cleanup()
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()

    def _weekly_cleanup_tick(self) -> None:
        """Hourly: run the weekly cleanup when it's due, Astra is idle and no game is running."""
        if self._closing:
            return
        self.after(3600 * 1000, self._weekly_cleanup_tick)
        if not (self._setting("clean_weekly") and self.admin) or self.busy or self.clean_state != "idle":
            return
        if self._running_games() or time.time() - float(self._setting("clean_at") or 0) < CLEAN_WEEK:
            return
        self._run_cleanup(auto=True)

    def _paint_cleanup(self) -> None:
        c = self.canvas
        skip = set(self._setting("clean_skip") or [])
        for key, item in self.t_clean.items():
            if self.clean_scan is None:
                text = "…" if self.clean_state == "scanning" else ""
            else:
                count, nbytes = self.clean_scan.get(key, (0, 0))
                text = human_size(nbytes) if count else "Clean"
            c.itemconfigure(item, text=text, fill=ASH if key in skip else TEXT_HI)
        files, size = self._clean_total()
        if self.clean_state == "scanning":
            total = "Looking for old temporary files…"
        elif self.clean_state == "cleaning":
            total = f"Deleting {human_size(size)}…"
        elif self.clean_scan is None:
            total = ""
        elif size:
            total = f"{human_size(size)} ready to clean"
        else:
            total = "Nothing to clean right now"
        c.itemconfigure(self.t_clean_total, text=total)
        if self.clean_last:
            freed, deleted, _skipped = self.clean_last
            last = f"Last cleanup freed {human_size(freed)} ({deleted:,} files)."
        elif self._setting("clean_at"):
            last = f"Last cleaned {format_day(float(self._setting('clean_at')))}. Only files untouched for a day go."
        else:
            last = "Only files nobody has touched for a day are deleted."
        c.itemconfigure(self.t_clean_last, text=self._fit(last, self.f_desc, 370))

    # ---------------- ping tester ----------------

    def _make_ping_rows(self) -> list[dict]:
        rows = [{"key": key, "name": name, "host": host, "method": method}
                for key, name, host, method in PING_TARGETS]
        rows.append({"key": "custom", "name": "Custom server", "host": self._setting("custom_host") or None,
                     "method": "auto"})
        for row in rows:
            row.update(samples=[], status="idle", ip=None, error=None)
        return rows

    def _start_ping(self, indices=None) -> None:
        targets = range(len(self.ping_rows)) if indices is None else indices
        if not self.ping_running:
            self._ping_run_id += 1
            self._ping_pending = 0
            self._ping_stop.clear()
        started = 0
        for index in targets:
            row = self.ping_rows[index]
            if row["status"] == "running" or (row["key"] == "custom" and not row["host"]):
                continue
            row.update(samples=[], status="running", error=None)
            if row["key"] != "router":
                row["ip"] = None
            threading.Thread(target=self._ping_worker,
                             args=(self._ping_run_id, index, row["host"], row["method"]),
                             name="ping", daemon=True).start()
            self._paint_ping_row(index)
            started += 1
        self._ping_pending += started
        if started and not self.ping_running:
            self.ping_running = True
            self._emit("head", f"Ping test started ({started} server{'s' if started != 1 else ''})")
        self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
        self._render()
        self._kick()

    def _ping_worker(self, run: int, index: int, host: str | None, method: str) -> None:
        def put(kind: str, value=None) -> None:
            self.events.put(("ping", run, index, kind, value))

        try:
            if host is None:
                host = default_gateway()
                if not host:
                    put("error", "No router found")
                    return
            try:
                ip = socket.gethostbyname(host)
            except (OSError, UnicodeError):
                put("error", "Can't find that host")
                return
            put("ip", ip)
            probe = tcp_ping if method == "tcp" else icmp_ping
            if method == "auto" and icmp_ping(ip) is None and tcp_ping(ip) is not None:
                probe = tcp_ping  # the server drops ICMP but accepts TCP
            for _ in range(PING_SAMPLES):
                if self._ping_stop.is_set():
                    break
                started = time.monotonic()
                put("sample", probe(ip))
                if self._ping_stop.wait(max(0.0, PING_INTERVAL - (time.monotonic() - started))):
                    break
        except Exception as exc:
            put("error", f"{exc.__class__.__name__}")
        finally:
            put("done")

    def _on_ping_event(self, run: int, index: int, kind: str, value) -> None:
        if run != self._ping_run_id or self._closing:
            return
        row = self.ping_rows[index]
        if kind == "ip":
            row["ip"] = value
        elif kind == "sample":
            row["samples"].append(value)
        elif kind == "error":
            row["status"], row["error"] = "error", value
        elif kind == "done":
            if row["status"] == "running":
                row["status"] = "done"
            self._ping_pending -= 1
            if self._ping_pending <= 0:
                self.ping_running = False
                self._ping_summary()
                self.canvas.configure(cursor="hand2" if self._enabled(self.hover) else "")
                self._render()
                self._kick()
        self._paint_ping_row(index)

    @staticmethod
    def _ping_stats(samples: list) -> tuple[float | None, float | None, float | None]:
        """(average ms, jitter ms, loss %) - None where there is not enough data."""
        if not samples:
            return None, None, None
        good = [s for s in samples if s is not None]
        loss = 100.0 * (len(samples) - len(good)) / len(samples)
        if not good:
            return None, None, loss
        jitter = None
        if len(good) >= 2:
            jitter = sum(abs(a - b) for a, b in zip(good, good[1:])) / (len(good) - 1)
        return sum(good) / len(good), jitter, loss

    @staticmethod
    def _latency_color(ms: float) -> str:
        if ms < 80:
            return TEXT_HI
        return WARN_YELLOW

    def _paint_ping_row(self, index: int) -> None:
        row, items, c, px = self.ping_rows[index], self.t_ping[index], self.canvas, self.px
        c.itemconfigure(items["name"], text=row["name"])
        if row["status"] == "error":
            host_text, host_color = row["error"], ERROR_RED
        elif row["key"] == "router":
            host_text, host_color = row["ip"] or "Default gateway", TEXT_MID
        elif row["host"]:
            host_text, host_color = row["host"], TEXT_MID
        else:
            host_text, host_color = "Type a host name or IP below", ASH
        c.itemconfigure(items["host"], text=self._fit(host_text, self.f_label, 212), fill=host_color)

        avg, jitter, loss = self._ping_stats(row["samples"])
        if row["status"] == "error" or (loss == 100.0 and row["status"] != "running"):
            latency, latency_color = ("Timed out" if row["status"] != "error" else "—"), ERROR_RED
        elif avg is None:
            latency, latency_color = ("…" if row["status"] == "running" else "—"), TEXT_MID
        else:
            latency = "<1 ms" if avg < 1 else f"{avg:.0f} ms"
            latency_color = self._latency_color(avg)
        c.itemconfigure(items["latency"], text=latency, fill=latency_color)
        c.itemconfigure(items["jitter"], text="—" if jitter is None else f"{jitter:.0f} ms",
                        fill=WARN_YELLOW if jitter is not None and jitter >= 20 else TEXT_MID)
        c.itemconfigure(items["loss"], text="—" if loss is None else f"{loss:.0f}%",
                        fill=WARN_YELLOW if loss else TEXT_MID)

        samples = row["samples"][-PING_SAMPLES:]
        top = 200.0  # one fixed scale for every row, so bars compare across servers
        base, full = items["base_y"], 20.0
        for k, rect in enumerate(items["bars"]):
            x0, x1 = items["bar_x"][k], items["bar_x"][k] + items["bar_w"]
            if k >= len(samples):
                c.coords(rect, px(x0), px(base - 1.5), px(x1), px(base))
                c.itemconfigure(rect, fill=SEGMENT_OFF)
            elif samples[k] is None:
                c.coords(rect, px(x0), px(base - 4), px(x1), px(base))
                c.itemconfigure(rect, fill=ERROR_RED)
            else:
                height = max(2.0, full * min(1.0, samples[k] / top))
                c.coords(rect, px(x0), px(base - height), px(x1), px(base))
                c.itemconfigure(rect, fill=SILVER if samples[k] < 80 else WARN_YELLOW)

    def _fit(self, text: str, font: tkfont.Font, width: float) -> str:
        limit = self.px(width)
        if font.measure(text) <= limit:
            return text
        while text and font.measure(text + "…") > limit:
            text = text[:-1]
        return text.rstrip() + "…"

    def _ping_summary(self) -> None:
        results = []
        for row in self.ping_rows:
            avg, jitter, loss = self._ping_stats(row["samples"])
            if avg is not None:
                results.append((avg, row["name"], loss or 0.0))
        if not results:
            self._emit("warn", "Ping test finished: no server answered - check your connection")
            return
        avg, name, _ = min(results)
        lossy = [n for _a, n, l in results if l > 0]
        self._emit("ok", f"Ping test finished: best was {name} at {avg:.0f} ms")
        if lossy:
            self._emit("warn", f"Packet loss to {', '.join(lossy)}")

    def _use_custom_host(self) -> None:
        host = self.host_var.get().strip()
        index = len(self.ping_rows) - 1
        row = self.ping_rows[index]
        if host and not HOST_RE.match(host):
            row.update(status="error", error="Not a valid host name or IP", samples=[])
            self._paint_ping_row(index)
            return
        self._set_setting("custom_host", host)
        row.update(host=host or None, status="idle", error=None, samples=[], ip=None)
        self._paint_ping_row(index)
        self._blur_host()
        if host:
            self._start_ping([index])

    def _paint_segments(self, key: str, percent: float) -> None:
        lit = int(round(percent / 10.0))
        for i, rect in enumerate(self.segments[key]):
            if i >= lit:
                color = SEGMENT_OFF
            elif percent >= 85:
                color = STARLIGHT                       # critical load: full white
            elif percent >= 65:
                color = "#D6D6D6"                       # high load: bright
            else:
                color = mix("#6E6E6E", SILVER, i / 9)   # normal: grey ramp
            self.canvas.itemconfigure(rect, fill=color)

    def _update_telemetry(self, data: dict) -> None:
        self._last_tel = data
        for session in self.sessions_live.values():
            add_session_sample(session, data)
        if self.bench and self.bench["phase"] == "running":
            add_session_sample(self.bench["acc"], data)
        cpu = float(data["cpu"])
        ram = float(data["ram"])
        self.canvas.itemconfigure(self.t_cpu, text=f"{cpu:.0f}%")
        self.canvas.itemconfigure(self.t_ram, text=f"{ram:.0f}%")
        self.canvas.itemconfigure(self.t_ram_label, text=f"RAM {data['ram_used_gb']:.1f}/{data['ram_total_gb']:.0f} GB")
        self._paint_segments("cpu", cpu)
        self._paint_segments("ram", ram)
        plan = data["plan"] or "Unknown"
        limit = self.px(L_PILLS["plan"][2] - 34)
        if self.f_plan.measure(plan) > limit:
            while plan and self.f_plan.measure(plan + "…") > limit:
                plan = plan[:-1]
            plan = plan.rstrip() + "…"
        self.canvas.itemconfigure(self.t_plan, text=plan)
        self._update_gpu(data.get("gpu"), data.get("gpu_name") or "")
        self._record_history(data)

    def _update_gpu(self, gpu: dict | None, name: str) -> None:
        c = self.canvas
        name = short_gpu_name(name)
        room = L_PILLS["gpu"][2] - 36
        if not gpu:
            c.itemconfigure(self.t_gpu_vram, text="")
            c.itemconfigure(self.t_gpu_clock, text="")
            c.itemconfigure(self.t_gpu, text="n/a")
            c.itemconfigure(self.t_gpu_label, text=self._fit(f"GPU {name}".strip(), self.f_label, room), fill=TEXT_MID)
            self._paint_segments("gpu", 0)
            return
        load, temp = float(gpu["load"]), gpu.get("temp")
        suffix = f" \u00b7 {temp}\u00b0C" if temp is not None else ""
        label = self._fit(f"GPU {name}".strip(), self.f_label, room - self.f_label.measure(suffix) / self.scale)
        c.itemconfigure(self.t_gpu, text=f"{load:.0f}%")
        c.itemconfigure(self.t_gpu_label, text=label + suffix,
                        fill=WARN_YELLOW if temp is not None and temp >= 85 else TEXT_MID)
        self._paint_segments("gpu", load)
        vram = ""
        if gpu.get("vram_total"):
            vram = f"VRAM {gpu['vram_used'] / 1024 ** 3:.1f} / {gpu['vram_total'] / 1024 ** 3:.0f} GB"
        parts = []
        if gpu.get("clock") is not None:
            parts.append(f"{gpu['clock']:,} MHz")
        if gpu.get("power") is not None:
            parts.append(f"{gpu['power']:.0f} W")
        c.itemconfigure(self.t_gpu_vram, text=vram)
        c.itemconfigure(self.t_gpu_clock, text=" \u00b7 ".join(parts))

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.tray is not None and self.tray.running:
            self._hide_to_tray()
            return
        if self.busy:
            if self.dialog:
                return
            self._open_dialog(
                "Optimization still running",
                "Closing now stops it partway. Anything already changed stays changed until you undo it.",
                "Close anyway",
                self._shutdown,
                cancel_label="Keep running",
            )
            return
        self._shutdown()

    def _shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.telemetry.stop_event.set()
        self._ping_stop.set()
        if self.session_pinger is not None:
            self.session_pinger.stop_event.set()
        for session in self.sessions_live.values():
            if session.get("capture") is not None:
                session["capture"].stop(wait=1.0)
        if self.game_watcher is not None:
            self.game_watcher.stop()
        self._stop_hotkey()
        if self.overlay_hotkey is not None:
            self.overlay_hotkey.stop()
        if self.bench_hotkey is not None:
            self.bench_hotkey.stop()
        if self.overlay is not None:
            self.overlay.destroy()
        self._stop_tray()
        self.destroy()


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def write_icon_files(folder: str) -> None:
    """Render icon.ico (every Windows size, each drawn natively) and icon.png into folder."""
    painter = GalaxyPainter(1.0)
    images = [painter.app_icon(size) for size in ICON_SIZES]
    os.makedirs(folder, exist_ok=True)
    images[-1].save(os.path.join(folder, "icon.ico"), format="ICO",
                    sizes=[(size, size) for size in ICON_SIZES], append_images=images[:-1])
    images[-1].save(os.path.join(folder, "icon.png"))


def main() -> None:
    args = sys.argv[1:]
    if "--write-icon" in args:  # used by build.bat: python optimizer.py --write-icon <folder>
        index = args.index("--write-icon")
        write_icon_files(args[index + 1] if index + 1 < len(args) else resource_path("assets"))
        return
    no_elevate = "--no-elevate" in args
    try:  # own taskbar identity: groups and pins as Astra Optimizer, with its own icon
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass
    if getattr(sys, "frozen", False):
        # Tell the Shell this exe may have changed, so its cached icon is refreshed. Taskbars that ask
        # the Shell for the 256 px icon (Seelen UI does) otherwise keep showing an older build's icon.
        try:
            ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005 | 0x2000, ctypes.c_wchar_p(sys.executable), None)
        except (AttributeError, OSError):
            pass  # SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSHNOWAIT
    updated = "--updated" in args
    if updated:
        wait_for_previous_instance()  # the version this replaced is closing right now
    if signal_running_instance():
        return  # already running (maybe in the tray): it shows itself
    if getattr(sys, "frozen", False):
        threading.Thread(target=remove_update_leftovers, args=(sys.executable,), name="update-cleanup",
                         daemon=True).start()
    admin = is_admin()
    if not admin and not no_elevate:
        if relaunch_as_admin():
            return
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            APP_NAME,
            "Administrator rights were not granted.\n\n"
            "Astra Optimizer will open in monitor-only mode. Restart it and accept the UAC prompt "
            "to apply optimizations.",
            parent=root,
        )
        root.destroy()
    try:
        app = OptimizerApp(admin=admin, start_hidden="--tray" in args, instance_event=create_instance_event())
    except Exception as exc:
        startup_crash(exc)
        return
    if updated:
        app.after(1200, lambda: app._emit("ok", f"Updated to {APP_NAME} {DISPLAY_VERSION}"))
    app.mainloop()


def startup_crash(exc: BaseException) -> None:
    """The window couldn't open: log it and offer a report, with plain Windows dialogs."""
    write_crash_log(f"at startup:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}")
    summary = crash_summary(type(exc), exc)
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        return
    try:
        if not messagebox.askyesno(
                APP_NAME, f"{APP_NAME} couldn't start because of an unexpected error:\n\n{summary}\n\n"
                          f"Save a problem report to your desktop to send to {CREDIT_NAME}?", parent=root):
            return
        root.config(cursor="watch")
        root.update()
        path = os.path.join(desktop_folder(), f"Astra Optimizer report {_dt.datetime.now():%Y-%m-%d %H%M}.zip")
        try:
            write_problem_report(path, {"Version": DISPLAY_VERSION, "Startup error": summary}, [], [], STATE_FILE)
        except Exception as report_exc:
            messagebox.showerror(APP_NAME, f"Couldn't make the report: {report_exc}\n\nThe error is in "
                                           f"{LOG_FILE}", parent=root)
            return
        try:
            subprocess.Popen(f'explorer.exe /select,"{path}"', creationflags=CREATE_NO_WINDOW)
        except OSError:
            pass
        messagebox.showinfo(APP_NAME, f"Saved to your desktop:\n{os.path.basename(path)}", parent=root)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
