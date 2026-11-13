#!/usr/bin/env python3
"""Simple Tello simulator for practice without a drone.
Run in one window:   python tello_sim.py
Then in another:     python tello_course.py --ip 127.0.0.1
"""
import socket
import time

pos = {"x": 0, "y": 0, "z": 0, "yaw": 0, "flying": False}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 8889))
print("Tello simulator running on 127.0.0.1:8889")

while True:
    data, addr = s.recvfrom(1024)
    cmd = data.decode().strip()
    parts = cmd.split()
    c, v = parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 0)
    resp = "ok"
    if c == "battery?":
        resp = "87"
    elif c == "tof?":
        resp = f"{max(pos['z'], 10) * 10}mm"
    elif c == "height?":
        resp = f"{pos['z'] // 10}dm"
    elif c == "takeoff":
        pos.update(z=80, flying=True); time.sleep(0.5)
    elif c in ("land", "emergency"):
        pos.update(z=0, flying=False)
    elif c in ("forward", "back", "left", "right", "up", "down", "cw", "ccw"):
        if not pos["flying"]:
            resp = "error Not joystick"
        elif c not in ("cw", "ccw") and not 20 <= v <= 500:
            resp = "error Out of range"
        else:
            d = {"forward": ("x", 1), "back": ("x", -1), "right": ("y", 1), "left": ("y", -1),
                 "up": ("z", 1), "down": ("z", -1), "cw": ("yaw", 1), "ccw": ("yaw", -1)}[c]
            pos[d[0]] += d[1] * v
            time.sleep(0.3)
    if c != "battery?":
        print(f"{cmd:<14} -> {resp:<20} pos: forward={pos['x']} right={pos['y']} height={pos['z']} yaw={pos['yaw']}")
    s.sendto(resp.encode(), addr)
