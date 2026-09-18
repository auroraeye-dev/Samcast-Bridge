#!/usr/bin/env python3
"""Check whether this machine can run Samcast, and say precisely what is wrong.

    python doctor.py

Written because the Windows half cannot be debugged from the machine it was
developed on. "It didn't work" is not something anyone can act on remotely;
this turns it into a list with one line per thing that could be broken.
Every check says what it looked for, so a failure is a next step rather than
a dead end.
"""

from __future__ import annotations

import platform
import socket
import sys

PASS, FAIL, WARN = "  ok  ", " FAIL ", " warn "
problems = 0


def report(state: str, what: str, detail: str = "") -> None:
    global problems
    if state is FAIL:
        problems += 1
    print(f"[{state}] {what}" + (f"\n         {detail}" if detail else ""))


print("Samcast — what this machine can do\n")

# --- Python -----------------------------------------------------------------
version = sys.version_info
print(f"Python {platform.python_version()} on {platform.system()} {platform.release()}\n")
if version < (3, 9):
    report(FAIL, "Python is too old", "3.11 or 3.12 is what you want.")
elif version >= (3, 13):
    report(WARN, f"Python {version.major}.{version.minor} has no MediaPipe wheels yet",
           "Links will work; hand gestures will not. Install 3.11 or 3.12 for those.")
else:
    report(PASS, f"Python {version.major}.{version.minor} is supported")

# --- What is needed for what ------------------------------------------------
print("\nLinks — the core feature. Needs nothing but the standard library.")
for module in ("socket", "json", "threading", "webbrowser"):
    try:
        __import__(module)
        report(PASS, f"{module}")
    except ImportError:
        report(FAIL, f"{module} is missing", "This Python install is broken.")

print("\nThe app window. Ships with python.org installers.")
try:
    import tkinter  # noqa: F401
    report(PASS, "tkinter")
except ImportError:
    report(WARN, "tkinter is missing",
           "Reinstall Python from python.org — Tk is part of it. "
           "Until then use peer_cli.py, which needs no window.")

print("\nHand gestures.")
for module, why in (("cv2", "opencv-python"), ("mediapipe", "mediapipe")):
    try:
        __import__(module)
        report(PASS, module)
    except ImportError:
        report(WARN, f"{module} is missing", f"pip install {why}")

print("\nReading the browser's address, and sharing a window.")
for module, why in (("uiautomation", "uiautomation"), ("win32gui", "pywin32"),
                    ("psutil", "psutil"), ("mss", "mss"), ("PIL", "Pillow")):
    try:
        __import__(module)
        report(PASS, module)
    except ImportError:
        report(WARN, f"{module} is missing", f"pip install {why}")

# --- The camera itself ------------------------------------------------------
print("\nCamera.")
try:
    import cv2
    found = False
    for index in range(3):
        for name in ("CAP_DSHOW", "CAP_MSMF", "CAP_V4L2", "CAP_AVFOUNDATION"):
            backend = getattr(cv2, name, None)
            if backend is None:
                continue
            capture = cv2.VideoCapture(index, backend)
            if capture.isOpened():
                ok, frame = capture.read()
                capture.release()
                if ok:
                    report(PASS, f"camera works at index {index} via {name}",
                           f"{frame.shape[1]}x{frame.shape[0]}")
                    found = True
                    break
            else:
                capture.release()
        if found:
            break
    if not found:
        report(WARN, "no camera could be opened",
               "Close anything else using it (Teams, Zoom, Camera). On Windows, "
               "check Settings > Privacy & security > Camera and make sure "
               "'Let desktop apps access your camera' is on.")
except ImportError:
    report(WARN, "cannot test the camera without opencv-python", "pip install opencv-python")

# --- Network ----------------------------------------------------------------
print("\nNetwork.")
try:
    addresses = {
        info[4][0]
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        if not info[4][0].startswith("127.")
    }
    if addresses:
        report(PASS, "this machine's address: " + ", ".join(sorted(addresses)),
               "The other device must share the first three parts of one of these.")
    else:
        report(FAIL, "no network address", "Not connected to a network.")
except OSError as exc:
    report(FAIL, "could not read the network address", str(exc))

try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    probe.bind(("0.0.0.0", 50505))
    probe.close()
    report(PASS, "UDP port 50505 is available")
except OSError as exc:
    report(WARN, "could not bind UDP 50505", f"{exc}. Another copy may already be running.")

# --- Verdict ----------------------------------------------------------------
print()
if problems:
    print(f"{problems} thing(s) stop this working at all. Fix those first.")
    sys.exit(1)
print("Nothing is broken. Anything marked 'warn' limits features, not the basics.")
print("Firewall note: the first run raises a Windows Firewall prompt.")
print("Allow it on PRIVATE networks — miss it and discovery fails with no error.")
