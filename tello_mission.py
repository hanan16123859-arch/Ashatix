"""
مهمة طيران Tello مع تصحيح الانحراف الجانبي
=============================================

المسار:
  1. إقلاع + تثبيت بدون انحراف
  2. ارتفاع 35 سم        (مع التصحيح)
  3. تقدم 120 سم         (مع التصحيح)
  4. نزول 57 سم          (مع التصحيح)
  5. تقدم 100 سم         (مع التصحيح)
  6. ارتفاع 95 سم        (مع التصحيح)
  7. يمين 30 سم
  8. تقدم 70 سم          (مع التصحيح)
  9. يسار 50 سم
 10. هبوط

طريقة التصحيح:
  - خيط (thread) في الخلفية يقرأ السرعة الجانبية vgy من الطائرة
    20 مرة في الثانية ويجمعها ليعرف كم انحرفت يمين/يسار.
  - كل حركة (تقدم/ارتفاع/نزول) تُقسَّم إلى قطع صغيرة (≤ 40 سم، ≥ 20 سم).
  - بعد كل قطعة: إذا الانحراف يسار > 10 سم  -> يمين 20
                  إذا الانحراف يمين > 10 سم -> يسار 20
  - ونرجع اتجاه الرأس (yaw) لو لفّ.

التشغيل:
    pip install djitellopy
    python tello_mission.py --simulate   # تجربة بدون طائرة
    python tello_mission.py              # مع الطائرة (اتصل بشبكة Tello WiFi)
"""

import argparse
import random
import threading
import time

# ------------------ الإعدادات ------------------
MISSION = [
    ("up", 35),
    ("forward", 120),
    ("down", 57),
    ("forward", 100),
    ("up", 95),
    ("right", 30),
    ("forward", 70),
    ("left", 50),
]

CORRECTED = {"up", "down", "forward"}  # الحركات التي يُطبَّق عليها التصحيح

CHUNK_CM = 40               # أكبر قطعة حركة قبل فحص الانحراف
MIN_MOVE_CM = 20            # أقل حركة تقبلها Tello
CORRECTION_CM = 20          # مسافة التصحيح
DRIFT_THRESHOLD_CM = 10     # متى نعتبرها انحرفت
YAW_THRESHOLD_DEG = 8       # الانحراف المسموح في اتجاه الرأس
MOVE_SPEED = 30             # سرعة الحركة سم/ث (10-100)
STABILIZE_SEC = 1.0         # انتظار للثبات بعد الإقلاع
LOOP_HZ = 20

SPEED_TO_CM_S = 10          # vgy بوحدة dm/s -> cm/s
SIGN_Y = 1                  # غيّرها إلى -1 إذا كان التصحيح بالعكس (موجب = يمين)


# ------------------ متتبع الانحراف ------------------
class DriftTracker:
    """يجمع السرعة الجانبية مع الزمن في الخلفية."""

    def __init__(self, tello):
        self.tello = tello
        self.drift = 0.0          # سم، موجب = يمين
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        dt = 1.0 / LOOP_HZ
        last = time.time()
        while not self._stop.is_set():
            time.sleep(dt)
            now = time.time()
            vy = SIGN_Y * self.tello.get_speed_y() * SPEED_TO_CM_S
            with self._lock:
                self.drift += vy * (now - last)
            last = now

    def get(self):
        with self._lock:
            return self.drift

    def reset(self):
        with self._lock:
            self.drift = 0.0


# ------------------ أدوات ------------------
def split_distance(total):
    """تقسيم المسافة إلى قطع كل واحدة بين 20 و 40 سم. مثال: 57 -> [37, 20]"""
    chunks = []
    remaining = total
    while remaining > 0:
        if remaining <= CHUNK_CM:
            chunks.append(remaining)
            break
        nxt = CHUNK_CM
        if remaining - nxt < MIN_MOVE_CM:
            nxt = remaining - MIN_MOVE_CM
        chunks.append(nxt)
        remaining -= nxt
    return chunks


def angle_diff(a, b):
    return (a - b + 180) % 360 - 180


def lateral_move(tello, tracker, direction, cm):
    """حركة جانبية. المتتبع يستمر بالقياس أثناءها، فيحسب الحركة الفعلية
    (مع أثر الريح) ويعرف كم بقي من الانحراف بعد التصحيح."""
    if direction == "right":
        tello.move_right(cm)
    else:
        tello.move_left(cm)
    time.sleep(0.3)


def correct_drift(tello, tracker, max_tries=3):
    """يصحح بخطوات 20 سم حتى يرجع الانحراف ضمن الحد."""
    for _ in range(max_tries):
        d = tracker.get()
        if d < -DRIFT_THRESHOLD_CM:
            print(f"    انحرفت يسار {abs(d):.0f} سم -> يمين {CORRECTION_CM}")
            lateral_move(tello, tracker, "right", CORRECTION_CM)
        elif d > DRIFT_THRESHOLD_CM:
            print(f"    انحرفت يمين {d:.0f} سم -> يسار {CORRECTION_CM}")
            lateral_move(tello, tracker, "left", CORRECTION_CM)
        else:
            return


def correct_heading(tello, target_yaw):
    err = angle_diff(tello.get_yaw(), target_yaw)
    if abs(err) > YAW_THRESHOLD_DEG:
        deg = int(abs(err))
        print(f"    الرأس لفّ {err:+.0f}° -> إرجاعه")
        if err > 0:
            tello.rotate_counter_clockwise(deg)
        else:
            tello.rotate_clockwise(deg)


def do_move(tello, tracker, target_yaw, action, cm):
    print(f"\n>> {action} {cm} سم")
    if action in ("right", "left"):
        lateral_move(tello, tracker, action, cm)
        tracker.reset()          # المسار الجديد هو المرجع
        print(f"    الانحراف ≈ {tracker.get():+.1f} سم (مرجع جديد)")
        return

    move = {"forward": tello.move_forward,
            "up": tello.move_up,
            "down": tello.move_down}[action]

    for chunk in split_distance(cm):
        move(chunk)
        print(f"    {action} {chunk} | الانحراف ≈ {tracker.get():+.1f} سم")
        correct_drift(tello, tracker)
        correct_heading(tello, target_yaw)


# ------------------ تشغيل المهمة ------------------
def run(tello):
    tello.connect()
    print("البطارية:", tello.get_battery(), "%")
    tello.set_speed(MOVE_SPEED)

    tracker = DriftTracker(tello)
    tracker.start()              # يبدأ القياس قبل الإقلاع ليحسب انحراف الإقلاع نفسه
    try:
        target_yaw = tello.get_yaw()
        tello.takeoff()

        # تثبيت بعد الإقلاع: مراقبة قصيرة ثم تصحيح لو انحرفت
        time.sleep(STABILIZE_SEC)
        print(">> فحص الانحراف بعد الإقلاع")
        correct_drift(tello, tracker)
        correct_heading(tello, target_yaw)

        for action, cm in MISSION:
            do_move(tello, tracker, target_yaw, action, cm)

        print("\n>> هبوط")
    finally:
        tracker.stop()
        tello.land()
        tello.end()


# ------------------ طائرة وهمية للتجربة ------------------
class FakeTello:
    SIM_SPEED = 50   # سم/ث في المحاكاة

    def __init__(self):
        self.y = 0.0
        self.cmd_vy = 0.0
        self.flying = False
        self.wind = random.choice([-1, 1]) * random.uniform(8, 15)
        threading.Thread(target=self._physics, daemon=True).start()

    def _physics(self):
        dt = 0.02
        while True:
            time.sleep(dt)
            if self.flying:
                if random.random() < 0.003:
                    self.wind = -self.wind
                self.y += (self.wind + self.cmd_vy) * dt

    def _vy(self):
        return (self.wind if self.flying else 0) + self.cmd_vy

    def connect(self): print("[sim] connected")
    def get_battery(self): return 90
    def set_speed(self, s): pass
    def takeoff(self): self.flying = True; print("[sim] takeoff")
    def land(self): self.flying = False; print(f"[sim] land | الموضع الجانبي الحقيقي = {self.y:+.1f} سم")
    def end(self): pass
    def get_yaw(self): return 0
    def get_speed_y(self): return round(self._vy() / SPEED_TO_CM_S)
    def rotate_clockwise(self, d): pass
    def rotate_counter_clockwise(self, d): pass

    def _wait(self, cm): time.sleep(cm / self.SIM_SPEED)
    def move_forward(self, cm): self._wait(cm)
    def move_up(self, cm): self._wait(cm)
    def move_down(self, cm): self._wait(cm)

    def _side(self, cm, sign):
        self.cmd_vy = sign * self.SIM_SPEED
        self._wait(cm)
        self.cmd_vy = 0
        print(f"    [sim] الموضع الجانبي الحقيقي = {self.y:+.1f} سم")

    def move_right(self, cm): self._side(cm, 1)
    def move_left(self, cm): self._side(cm, -1)


def main():
    parser = argparse.ArgumentParser(description="Tello mission with drift correction")
    parser.add_argument("--simulate", action="store_true", help="تجربة بدون طائرة")
    args = parser.parse_args()
    if args.simulate:
        tello = FakeTello()
    else:
        from djitellopy import Tello
        tello = Tello()
    run(tello)


if __name__ == "__main__":
    main()
