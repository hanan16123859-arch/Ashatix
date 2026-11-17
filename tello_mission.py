"""
مهمة طيران Tello مع تصحيح الانحراف الجانبي
=============================================

المسار:
  إقلاع + معايرة، ثم الخطوات في قائمة MISSION بالأسفل، ثم هبوط.
  التقدم/الارتفاع/النزول مع تصحيح الانحراف، ويمين/يسار حركات مقصودة.

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
    ("down", 62),
    ("forward", 100),
    ("up", 95),
    ("forward", 70),
    ("right", 35),
    ("forward", 70),
    ("down", 35),
    ("left", 50),
]

VALID_ACTIONS = {"up", "down", "forward", "right", "left"}

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
SIGN_Y = -1                 # مُعدَّلة بعد تجربة حقيقية: كان التصحيح بالعكس مع 1
AXIS_CAL_CM = 30            # مسافة حركة المعايرة يمين/يسار
CALIBRATE_SEC = 3.0         # مدة التحويم بعد الإقلاع لقياس خطأ الحساس (الطائرة ثابتة)
LOG_FILE = "flight_log.csv" # ملف تسجيل كل القراءات (أرسله إذا فيه مشكلة)


# ------------------ متتبع الانحراف ------------------
class DriftTracker:
    """يجمع السرعة الجانبية مع الزمن في الخلفية.

    Tello تعطي السرعة (vgx, vgy) في إطار غير محاذي لجسم الطائرة بالضرورة،
    لذلك نتعلم اتجاه "اليمين" في الحساس بحركة يمين تجريبية (calibrate_axis)
    ثم نأخذ مركّبة السرعة على هذا الاتجاه فقط.
    """

    def __init__(self, tello):
        self.tello = tello
        self.drift = 0.0              # سم، موجب = يمين
        self.bias = (0.0, 0.0)        # خطأ الحساس الثابت (vgx, vgy) dm/s
        self.right = (0.0, SIGN_Y)    # اتجاه اليمين في إطار الحساس (يُعاير بعد الإقلاع)
        self.paused = False
        self.axis_ok = False          # هل نجحت معايرة اتجاه اليمين
        self.window = []              # قراءات (vgx, vgy) في القطعة الحالية (للعرض)
        self._samples = None
        self._log = open(LOG_FILE, "w", encoding="utf-8")
        self._log.write("time,vgx,vgy,lateral_cm_s,drift_cm,event\n")
        self._t0 = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=1)
        self._log.close()

    def _lateral(self, vx, vy):
        """السرعة الجانبية (سم/ث) بعد طرح خطأ الحساس. موجب = يمين."""
        bx, by = self.bias
        ux, uy = self.right
        return ((vx - bx) * ux + (vy - by) * uy) * SPEED_TO_CM_S

    def _run(self):
        dt = 1.0 / LOOP_HZ
        last = time.time()
        while not self._stop.is_set():
            time.sleep(dt)
            now = time.time()
            vx = self.tello.get_speed_x()
            vy = self.tello.get_speed_y()
            with self._lock:
                self.window.append((vx, vy))
                if self._samples is not None:
                    self._samples.append((vx, vy))
                lat = self._lateral(vx, vy)
                if not self.paused:
                    self.drift += lat * (now - last)
                self._log.write(f"{now - self._t0:.2f},{vx},{vy},{lat:.1f},{self.drift:.1f},\n")
            last = now

    def event(self, text):
        with self._lock:
            self._log.write(f"{time.time() - self._t0:.2f},,,,{self.drift:.1f},{text}\n")
            self._log.flush()

    def _collect(self, action):
        """يجمع القراءات أثناء تنفيذ action() ويرجع متوسطها."""
        with self._lock:
            self._samples = []
        action()
        with self._lock:
            samples, self._samples = self._samples, None
        n = len(samples)
        if not n:
            return (0.0, 0.0), 0
        return (sum(a for a, _ in samples) / n, sum(b for _, b in samples) / n), n

    def calibrate_bias(self, seconds):
        """الطائرة تحوم ثابتة، فأي قراءة غير صفر هي خطأ في الحساس."""
        self.bias, n = self._collect(lambda: time.sleep(seconds))
        self.reset()
        return self.bias, n

    def calibrate_axis(self, tello, cm):
        """يتحرك يمين ثم يسار نفس المسافة، ويتعلم اتجاه اليمين في الحساس."""
        self.paused = True
        (mx, my), n = self._collect(lambda: tello.move_right(cm))
        tello.move_left(cm)
        time.sleep(0.3)
        mx -= self.bias[0]
        my -= self.bias[1]
        mag = (mx * mx + my * my) ** 0.5
        ok = mag >= 0.3
        if ok:
            self.right = (mx / mag, my / mag)
        self.axis_ok = ok
        self.reset()
        self.paused = False
        return ok, (mx, my), n

    def get(self):
        with self._lock:
            return self.drift

    def adjust(self, cm):
        with self._lock:
            self.drift += cm

    def take_window(self):
        """يرجع قراءات الحساس منذ آخر مرة ويبدأ من جديد."""
        with self._lock:
            w, self.window = self.window, []
        return w

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
    """حركة جانبية.
    - إذا نجحت معايرة الاتجاه: الحساس يقيس الحركة نفسها (مع أي انحراف أثناءها).
    - إذا فشلت: نوقف القياس ونحسب الحركة بالمسافة المطلوبة، حتى لا يكرر التصحيح بلا نهاية."""
    sign = 1 if direction == "right" else -1
    trust_sensor = tracker.axis_ok
    if not trust_sensor:
        tracker.paused = True
    if direction == "right":
        tello.move_right(cm)
    else:
        tello.move_left(cm)
    time.sleep(0.3)
    if not trust_sensor:
        tracker.adjust(sign * cm)
        tracker.paused = False


def correct_drift(tello, tracker, max_tries=3):
    """يصحح بخطوات 20 سم حتى يرجع الانحراف ضمن الحد."""
    for _ in range(max_tries):
        d = tracker.get()
        if d < -DRIFT_THRESHOLD_CM:
            print(f"    انحرفت يسار {abs(d):.0f} سم -> يمين {CORRECTION_CM}")
            tracker.event(f"correct_right drift={d:.0f}")
            lateral_move(tello, tracker, "right", CORRECTION_CM)
        elif d > DRIFT_THRESHOLD_CM:
            print(f"    انحرفت يمين {d:.0f} سم -> يسار {CORRECTION_CM}")
            tracker.event(f"correct_left drift={d:.0f}")
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
    tracker.event(f"{action} {cm}")
    if action in ("right", "left"):
        lateral_move(tello, tracker, action, cm)
        tracker.reset()          # المسار الجديد هو المرجع
        print(f"    الانحراف ≈ {tracker.get():+.1f} سم (مرجع جديد)")
        return

    move = {"forward": tello.move_forward,
            "up": tello.move_up,
            "down": tello.move_down}[action]

    for chunk in split_distance(cm):
        tracker.take_window()
        move(chunk)
        w = tracker.take_window()
        if w:
            xs = [a for a, _ in w]
            ys = [b for _, b in w]
            sensor = f"vgx {min(xs)}..{max(xs)}  vgy {min(ys)}..{max(ys)}"
        else:
            sensor = "-"
        print(f"    {action} {chunk} | الانحراف ≈ {tracker.get():+.1f} سم | الحساس: {sensor}")
        correct_drift(tello, tracker)
        correct_heading(tello, target_yaw)


# ------------------ تشغيل المهمة ------------------
MIN_BATTERY = 30            # لا تقلع إذا البطارية أقل من هذا


def explain_error(e):
    msg = str(e)
    print("\n" + "=" * 50)
    print("توقفت المهمة بسبب خطأ:")
    print("  ", msg)
    if "Motor stop" in msg:
        print("المعنى: محركات الطائرة متوقفة، يعني الطائرة صارت على الأرض.")
        print("الأسباب المحتملة: هبوط تلقائي بسبب بطارية ضعيفة أو حرارة عالية،")
        print("أو اصطدمت بشي، أو الحساس السفلي ما شاف الأرض زين (إضاءة ضعيفة/أرض بدون نقوش).")
    elif "timeout" in msg.lower() or "No valid response" in msg:
        print("المعنى: انقطع الاتصال بالطائرة. تأكد إنك متصل بشبكة TELLO وقريب منها.")
    print("=" * 50)


def check_mission():
    """يفحص المسار قبل الإقلاع حتى لا يتوقف في الجو بسبب خطأ كتابة."""
    ok = True
    for i, step in enumerate(MISSION, 1):
        action, cm = step
        if action not in VALID_ACTIONS:
            print(f"خطأ في الخطوة {i}: '{action}' غير معروفة. المسموح: {sorted(VALID_ACTIONS)}")
            ok = False
        if not isinstance(cm, int) or not 20 <= cm <= 500:
            print(f"خطأ في الخطوة {i}: المسافة {cm} لازم تكون رقم صحيح بين 20 و 500")
            ok = False
    return ok


def run(tello):
    if not check_mission():
        print("صحّح المسار في أعلى الملف ثم أعد التشغيل. الطائرة لم تقلع.")
        return
    tello.connect()
    battery = tello.get_battery()
    temp = tello.get_temperature()
    print(f"البطارية: {battery} %  |  الحرارة: {temp:.0f}°C")
    if battery < MIN_BATTERY:
        print(f"البطارية أقل من {MIN_BATTERY}% — اشحن الطائرة أولاً.")
        tello.end()
        return
    tello.set_speed(MOVE_SPEED)

    tracker = DriftTracker(tello)
    tracker.start()              # يبدأ القياس قبل الإقلاع ليحسب انحراف الإقلاع نفسه
    try:
        target_yaw = tello.get_yaw()
        tello.takeoff()

        # تثبيت بعد الإقلاع ثم معايرة خطأ الحساس
        time.sleep(STABILIZE_SEC)
        print(f">> معايرة 1: خطأ الحساس ({CALIBRATE_SEC:.0f} ثواني تحويم بدون حركة)...")
        (bx, by), n = tracker.calibrate_bias(CALIBRATE_SEC)
        print(f"   خطأ الحساس: vgx={bx:+.2f}  vgy={by:+.2f}  (من {n} قراءة)")
        tracker.event(f"bias vgx={bx:.2f} vgy={by:.2f}")

        print(f">> معايرة 2: اتجاه اليمين (يمين {AXIS_CAL_CM} ثم يسار {AXIS_CAL_CM})...")
        ok, (mx, my), n = tracker.calibrate_axis(tello, AXIS_CAL_CM)
        if ok:
            ux, uy = tracker.right
            print(f"   أثناء الحركة يمين قرأ الحساس: vgx={mx:+.2f}  vgy={my:+.2f}")
            print(f"   => اتجاه اليمين = ({ux:+.2f}, {uy:+.2f})")
        else:
            print(f"   الحساس ما سجّل الحركة يمين (vgx={mx:+.2f} vgy={my:+.2f})،"
                  " نستخدم الإعداد الافتراضي")
        tracker.event(f"axis ok={ok} vgx={mx:.2f} vgy={my:.2f}")
        correct_heading(tello, target_yaw)

        for action, cm in MISSION:
            do_move(tello, tracker, target_yaw, action, cm)

        print("\n>> هبوط")
    except KeyboardInterrupt:
        print("\nأوقفتها يدوياً (Ctrl+C) -> هبوط")
    except Exception as e:
        explain_error(e)
    finally:
        tracker.stop()
        try:
            tello.land()
        except Exception:
            print("(الطائرة على الأرض أصلاً)")
        tello.end()
        print(f"القراءات محفوظة في: {LOG_FILE}")


# ------------------ طائرة وهمية للتجربة ------------------
SIM_SENSOR_BIAS = 0.0
SIM_ROT = 0.0                # دوران إطار الحساس في المحاكاة (درجات)


class FakeTello:
    SIM_SPEED = 50   # سم/ث في المحاكاة

    def __init__(self):
        self.y = 0.0
        self.cmd_vy = 0.0
        self.cmd_vx = 0.0          # سرعة التقدم المأمورة
        self.flying = False
        self.moving = False
        self.wind = random.choice([-1, 1]) * random.uniform(8, 15)
        self.sensor_bias = SIM_SENSOR_BIAS   # خطأ ثابت في الحساس (dm/s)
        threading.Thread(target=self._physics, daemon=True).start()

    def _physics(self):
        dt = 0.02
        while True:
            time.sleep(dt)
            if self.flying:
                if random.random() < 0.003:
                    self.wind = -self.wind
                self.y += self._vy() * dt

    def _vy(self):
        # بعد الإقلاع وقبل أول حركة، Tello تثبت نفسها فلا تتأثر بالريح
        return (self.wind if self.flying and self.moving else 0) + self.cmd_vy

    def connect(self): print("[sim] connected")
    def get_battery(self): return 90
    def get_temperature(self): return 60
    def set_speed(self, s): pass
    def takeoff(self): self.flying = True; print("[sim] takeoff")
    def land(self): self.flying = False; print(f"[sim] land | الموضع الجانبي الحقيقي = {self.y:+.1f} سم")
    def end(self): pass
    def get_yaw(self): return 0
    def _sensor(self):
        """الحساس يقيس (تقدم، يمين) في إطار مُدار بزاوية SIM_ROT مثل الطائرة الحقيقية."""
        import math
        th = math.radians(SIM_ROT)
        f, r = self.cmd_vx / SPEED_TO_CM_S, self._vy() / SPEED_TO_CM_S
        sx = f * math.cos(th) - r * math.sin(th)
        sy = f * math.sin(th) + r * math.cos(th)
        return sx, sy

    def get_speed_x(self):
        return round(self._sensor()[0] + random.uniform(-0.5, 0.5))

    def get_speed_y(self):
        return round(self._sensor()[1] + self.sensor_bias + random.uniform(-0.5, 0.5))
    def rotate_clockwise(self, d): pass
    def rotate_counter_clockwise(self, d): pass

    def _wait(self, cm):
        self.moving = True
        time.sleep(cm / self.SIM_SPEED)
    def move_forward(self, cm):
        self.cmd_vx = self.SIM_SPEED
        self._wait(cm)
        self.cmd_vx = 0
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
    parser.add_argument("--sim-bias", type=float, default=0.0,
                        help="خطأ حساس وهمي في المحاكاة (dm/s)")
    parser.add_argument("--sim-rot", type=float, default=-90.0,
                        help="دوران إطار الحساس في المحاكاة (درجات)")
    args = parser.parse_args()
    if args.simulate:
        global SIM_SENSOR_BIAS, SIM_ROT
        SIM_SENSOR_BIAS = args.sim_bias
        SIM_ROT = args.sim_rot
        tello = FakeTello()
    else:
        from djitellopy import Tello
        tello = Tello()
    run(tello)


if __name__ == "__main__":
    main()
