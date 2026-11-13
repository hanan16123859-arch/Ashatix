#!/usr/bin/env python3
"""
Tello interactive course controller (Leading & Control track)
=============================================================

The drone stays in the air while you enter the measurements step by step.
Each step runs, then waits on the same step so you can adjust it
(add / subtract) without landing or restarting the course. Press Enter
to go to the next step. The values you tried are saved automatically
and become the defaults next time.

Run:
    python tello_course.py                 # real drone (connect to TELLO-xxxx Wi-Fi first)
    python tello_course.py --ip 127.0.0.1  # with the simulator tello_sim.py
    python tello_course.py --free          # free commands only (no course)
    python tello_course.py --reset         # reset course values to defaults

No extra libraries needed (Python 3.7+).
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time

TELLO_PORT = 8889
LOCAL_PORT = 9000
MIN_MOVE = 20      # smallest move Tello accepts (cm)
MAX_MOVE = 500     # largest move in one command (cm)
KEEPALIVE_SEC = 7  # Tello auto-lands after 15 s without commands

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SETTINGS = os.path.join(HERE, "course_settings_en.json")

# ---------------------------------------------------------------------------
# Default course (edit course_settings_en.json to change it)
# Step types:
#   takeoff
#   height  value=cm          go to an exact height (measures and corrects)
#   move    dir=forward/back/left/right/up/down  value=cm
#   rotate  dir=cw (right) / ccw (left)          value=degrees
#   land
# ---------------------------------------------------------------------------
DEFAULT_COURSE = {
    "speed": 40,
    "steps": [
        {"type": "takeoff", "title": "Takeoff"},
        {"type": "height", "value": 100, "title": "Set height for ring 1"},
        {"type": "move", "dir": "forward", "value": 80, "title": "Forward through ring 1"},
        {"type": "height", "value": 50, "title": "Go down for rectangle 2"},
        {"type": "move", "dir": "forward", "value": 180, "title": "Forward through rectangle 2"},
        {"type": "height", "value": 110, "title": "Go up for ring 3"},
        {"type": "move", "dir": "right", "value": 0, "title": "Side shift for ring 3 (+ right, - left, 0 none)"},
        {"type": "move", "dir": "forward", "value": 100, "title": "Forward through ring 3"},
        {"type": "rotate", "dir": "cw", "value": 0, "title": "Turn right (degrees, 0 none)"},
        {"type": "move", "dir": "forward", "value": 100, "title": "Forward through ring 4"},
        {"type": "rotate", "dir": "ccw", "value": 0, "title": "Turn left (degrees, 0 none)"},
        {"type": "move", "dir": "forward", "value": 40, "title": "Forward to landing pad"},
        {"type": "land", "title": "Land"},
    ],
}

OPPOSITE = {"forward": "back", "back": "forward", "left": "right",
            "right": "left", "up": "down", "down": "up", "cw": "ccw", "ccw": "cw"}

DIR_NAME = {"forward": "forward", "back": "back", "left": "left", "right": "right",
            "up": "up", "down": "down", "cw": "turn right", "ccw": "turn left"}

ALIASES = {
    "f": "forward", "forward": "forward",
    "b": "back", "back": "back",
    "l": "left", "left": "left",
    "r": "right", "right": "right",
    "u": "up", "up": "up",
    "d": "down", "down": "down",
    "cw": "cw", "ccw": "ccw",
}

HELP = """
Commands you can type at any time (distances in cm, turns in degrees):
  f 30     forward 30          b 30     back 30
  l 20     left 20             r 20     right 20
  u 20     up 20               d 20     down 20
  cw 90    turn right 90       ccw 90   turn left 90
  h 60     go to height 60 cm (measures and corrects automatically)
  speed 30 set speed (10-100 cm/s)
  stop     hover in place
  bat      battery %           alt      current height
  takeoff  take off            land     land
  e        EMERGENCY - stop motors immediately!
  next / Enter  next step      prev     previous step
  goto 5   jump to step 5      list     show course steps
  help     this list           quit     land and exit

Inside a course step: type just a number to add more in the step's
direction (20 = 20 cm more, -10 = 10 cm the opposite way).
In a height step the number is the target height itself.
* Moves under 20 cm are done in two moves (back 20, then forward 20+value).
"""


class Tello:
    """Direct UDP link to Tello (SDK 2.0)."""

    def __init__(self, ip="192.168.10.1", verbose=True):
        self.addr = (ip, TELLO_PORT)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", LOCAL_PORT))
        self.lock = threading.Lock()
        self.last_cmd_time = time.time()
        self.flying = False
        self.verbose = verbose
        self._stop = False
        threading.Thread(target=self._keepalive, daemon=True).start()

    # -- low level ----------------------------------------------------------
    def _flush(self):
        self.sock.setblocking(False)
        try:
            while True:
                self.sock.recvfrom(1024)
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.setblocking(True)

    def send(self, cmd, timeout=10.0, quiet=False):
        with self.lock:
            self._flush()
            if self.verbose and not quiet:
                print(f"   >> send: {cmd}")
            self.sock.sendto(cmd.encode("utf-8"), self.addr)
            self.last_cmd_time = time.time()
            self.sock.settimeout(timeout)
            try:
                data, _ = self.sock.recvfrom(1024)
                resp = data.decode("utf-8", errors="ignore").strip()
            except socket.timeout:
                resp = "timeout"
            except OSError as e:
                resp = f"error {e}"
            self.last_cmd_time = time.time()
        if self.verbose and not quiet:
            print(f"   << drone: {resp}")
        return resp

    def send_no_wait(self, cmd):
        """For emergency: send right away without waiting."""
        self.sock.sendto(cmd.encode("utf-8"), self.addr)
        print(f"   >> send now: {cmd}")

    def _keepalive(self):
        while not self._stop:
            time.sleep(1)
            if time.time() - self.last_cmd_time > KEEPALIVE_SEC:
                if self.lock.acquire(blocking=False):
                    self.lock.release()
                    self.send("battery?", timeout=3, quiet=True)

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass

    # -- high level ---------------------------------------------------------
    @staticmethod
    def ok(resp):
        return resp.lower().startswith("ok")

    def connect(self):
        for _ in range(3):
            if self.ok(self.send("command", timeout=5)):
                return True
        return False

    def battery(self):
        r = self.send("battery?", timeout=5, quiet=True)
        return int(r) if r.isdigit() else None

    def altitude(self):
        """Height in cm from the bottom distance sensor (tof), else height?."""
        r = self.send("tof?", timeout=5, quiet=True)
        m = re.search(r"(\d+)", r)
        if m and "error" not in r.lower():
            mm = int(m.group(1))
            if 0 < mm < 8000:
                return round(mm / 10)
        r = self.send("height?", timeout=5, quiet=True)
        m = re.search(r"(\d+)", r)
        if m:
            return int(m.group(1)) * 10  # dm -> cm
        return None

    def takeoff(self):
        r = self.send("takeoff", timeout=20)
        if self.ok(r):
            self.flying = True
        return self.ok(r)

    def land(self):
        r = self.send("land", timeout=20)
        if self.ok(r):
            self.flying = False
        return self.ok(r)

    def emergency(self):
        self.send_no_wait("emergency")
        self.flying = False

    def _raw_move(self, direction, cm):
        return self.ok(self.send(f"{direction} {int(cm)}", timeout=25))

    def move(self, direction, cm):
        """Move any distance: splits big moves, handles small (<20) ones."""
        cm = int(round(cm))
        if cm == 0:
            return True
        if cm < 0:
            direction, cm = OPPOSITE[direction], -cm
        ok = True
        while cm > MAX_MOVE:
            ok &= self._raw_move(direction, MAX_MOVE)
            cm -= MAX_MOVE
        if cm >= MIN_MOVE:
            return ok and self._raw_move(direction, cm)
        print(f"   (small move {cm} cm: done in two moves)")
        if direction == "up":
            return ok and self._raw_move("up", MIN_MOVE + cm) and self._raw_move("down", MIN_MOVE)
        return ok and self._raw_move(OPPOSITE[direction], MIN_MOVE) and \
            self._raw_move(direction, MIN_MOVE + cm)

    def rotate(self, direction, deg):
        deg = int(round(deg))
        if deg == 0:
            return True
        if deg < 0:
            direction, deg = OPPOSITE[direction], -deg
        deg = min(deg, 360)
        return self.ok(self.send(f"{direction} {deg}", timeout=20))

    def go_height(self, target):
        cur = self.altitude()
        if cur is None:
            print("   ! Could not read the height.")
            return False
        diff = target - cur
        print(f"   Height now ~{cur} cm, target {target} cm (difference {diff:+d})")
        if abs(diff) < 5:
            print("   Height is OK, no move needed.")
            return True
        ok = self.move("up", diff)
        after = self.altitude()
        if after is not None:
            print(f"   Height now ~{after} cm")
        return ok

    def set_speed(self, v):
        v = max(10, min(100, int(v)))
        return self.ok(self.send(f"speed {v}"))


# ---------------------------------------------------------------------------
def load_settings(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"! Error reading {path}: {e} -- using the default course")
    save_settings(path, DEFAULT_COURSE)
    return json.loads(json.dumps(DEFAULT_COURSE))


def save_settings(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"! Settings not saved: {e}")


def parse_number(s):
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def step_text(i, st):
    t = st["type"]
    title = st.get("title", "")
    if t in ("move", "rotate"):
        unit = " deg" if t == "rotate" else " cm"
        return f"{i + 1:>2}. {title}  [{DIR_NAME[st['dir']]} {st.get('value', 0)}{unit}]"
    if t == "height":
        return f"{i + 1:>2}. {title}  [height {st.get('value', 0)} cm]"
    return f"{i + 1:>2}. {title}"


class Pilot:
    def __init__(self, tello, settings, path):
        self.t = tello
        self.s = settings
        self.path = path
        self.steps = settings["steps"]
        self.i = 0

    # -- free commands, return True if handled ------------------------------
    def free_command(self, line):
        parts = line.strip().split()
        if not parts:
            return False
        c = parts[0].lower()
        arg = parse_number(parts[1]) if len(parts) > 1 else None

        if c in ALIASES and arg is not None:
            d = ALIASES[c]
            if d in ("cw", "ccw"):
                self.t.rotate(d, arg)
            else:
                self.t.move(d, arg)
            return True
        if c == "h" and arg is not None:
            if 20 <= arg <= 300:
                self.t.go_height(int(arg))
            else:
                print("   Height must be between 20 and 300 cm.")
            return True
        if c == "speed" and arg is not None:
            if self.t.set_speed(arg):
                self.s["speed"] = int(arg)
                save_settings(self.path, self.s)
            return True
        if c == "stop":
            self.t.send("stop")
            return True
        if c in ("bat", "battery"):
            print(f"   Battery: {self.t.battery()}%")
            return True
        if c in ("alt", "tof"):
            print(f"   Height ~{self.t.altitude()} cm")
            return True
        if c == "takeoff":
            self.t.takeoff()
            return True
        if c == "land":
            self.t.land()
            return True
        if c in ("e", "emergency"):
            self.t.emergency()
            return True
        if c in ("help", "?"):
            print(HELP)
            return True
        if c == "list":
            self.show_course()
            return True
        return False

    def show_course(self):
        print("\n--- Course steps ---")
        for k, st in enumerate(self.steps):
            mark = ">" if k == self.i else " "
            print(f"{mark} {step_text(k, st)}")
        print()

    # -- execute a step ------------------------------------------------------
    def run_step(self, st, value):
        t = st["type"]
        if t == "takeoff":
            return self.t.takeoff()
        if t == "land":
            return self.t.land()
        if t == "height":
            return self.t.go_height(int(value))
        if t == "move":
            return self.t.move(st["dir"], value)
        if t == "rotate":
            return self.t.rotate(st["dir"], value)
        return False

    def course(self):
        print("\n=========== STEP BY STEP COURSE ===========")
        print("Enter  = run default value / go to next step")
        print("number = run with another value or add an adjustment")
        print("s      = skip step,   help = all commands\n")
        self.show_course()

        while self.i < len(self.steps):
            st = self.steps[self.i]
            executed = False
            total = 0.0  # total done in this step (move/rotate)
            print("-" * 55)
            print(f"STEP {step_text(self.i, st)}")

            while True:
                t = st["type"]
                if not executed:
                    if t in ("takeoff", "land"):
                        hint = "Enter=run, s=skip"
                    elif t == "height":
                        hint = f"target height cm [Enter={st.get('value')}]"
                    else:
                        unit = "deg" if t == "rotate" else "cm"
                        hint = f"amount {unit} [Enter={st.get('value')}]"
                else:
                    if t == "height":
                        hint = "DONE | Enter=next | number=new height"
                    elif t in ("move", "rotate"):
                        hint = f"DONE {total:g} | Enter=next | number=add more (- = opposite)"
                    else:
                        hint = "DONE | Enter=next"
                try:
                    line = input(f"  {hint} > ").strip()
                except EOFError:
                    line = "quit"

                low = line.lower()
                if low in ("quit", "exit", "q"):
                    return "quit"
                if low in ("s", "skip") or (executed and low in ("", "next", "n")):
                    self._remember(st, executed, total)
                    self.i += 1
                    break
                if low in ("prev", "p"):
                    self._remember(st, executed, total)
                    self.i = max(0, self.i - 1)
                    break
                m = re.match(r"^goto\s+(\d+)$", low)
                if m:
                    self._remember(st, executed, total)
                    self.i = max(0, min(len(self.steps) - 1, int(m.group(1)) - 1))
                    break

                # Enter on a step not yet run = default value
                if line == "":
                    value = st.get("value", 0)
                else:
                    value = parse_number(line)
                    if value is None:
                        if not self.free_command(line):
                            print("   ? Unknown command - type help")
                        continue

                if t in ("takeoff", "land"):
                    if line == "":
                        self.run_step(st, 0)
                        executed = True
                    else:
                        print("   This step needs no number.")
                    continue

                if t == "height":
                    if not 20 <= value <= 300:
                        print("   Height must be between 20 and 300 cm.")
                        continue
                    self.run_step(st, value)
                    st["value"] = int(value)
                    executed = True
                    save_settings(self.path, self.s)
                    continue

                # move / rotate
                if value == 0:
                    if not executed:
                        print("   Value 0: no move. (press Enter again for next step)")
                        executed = True
                    continue
                if self.run_step(st, value):
                    total += value
                executed = True

        print("\nCourse finished. You can keep using free commands or type quit.")

    def _remember(self, st, executed, total):
        """Save the tried value as the new default."""
        if executed and st["type"] in ("move", "rotate") and total != 0:
            st["value"] = int(total)  # negative = opposite direction
            save_settings(self.path, self.s)
            print(f"   (saved value {int(total)} for this step)")

    def free_mode(self):
        print("\n=========== FREE COMMANDS ===========")
        print(HELP)
        while True:
            try:
                line = input("command > ").strip()
            except EOFError:
                return
            if line.lower() in ("quit", "exit", "q"):
                return
            if line.lower() == "course":
                if self.i >= len(self.steps):
                    self.i = 0
                if self.course() == "quit":
                    return
                continue
            if line and not self.free_command(line):
                print("   ? Unknown command - type help")


def main():
    ap = argparse.ArgumentParser(description="Tello interactive course control")
    ap.add_argument("--ip", default="192.168.10.1", help="drone IP (127.0.0.1 for simulator)")
    ap.add_argument("--settings", default=DEFAULT_SETTINGS, help="course settings file")
    ap.add_argument("--free", action="store_true", help="free commands only")
    ap.add_argument("--reset", action="store_true", help="reset course to default values")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.settings):
        os.remove(args.settings)
    settings = load_settings(args.settings)

    print("Connecting to drone ...")
    tello = Tello(args.ip)
    if not tello.connect():
        print("X Not connected. Make sure you are on the TELLO-xxxx Wi-Fi and the drone is on.")
        tello.close()
        return
    bat = tello.battery()
    print(f"OK Connected. Battery: {bat}%")
    if bat is not None and bat < 20:
        print("! Battery low (under 20%) - the drone may refuse to take off.")
    tello.set_speed(settings.get("speed", 40))

    pilot = Pilot(tello, settings, args.settings)
    try:
        if args.free:
            pilot.free_mode()
        elif pilot.course() != "quit" and tello.flying:
            pilot.free_mode()
    except KeyboardInterrupt:
        print("\n! Ctrl+C - landing ...")
    finally:
        if tello.flying:
            tello.land()
        tello.close()
        print("Closed.")


if __name__ == "__main__":
    main()
