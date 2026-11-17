"""
مهمة طيران Tello مع تصحيح الانحراف الجانبي — نسخة 2 (بدون يمين/يسار بعد الإقلاع)
==================================================================================

المسار:
  إقلاع + معايرة خطأ الحساس (تحويم 3 ثواني فقط، بدون أي حركة)،
  ثم الخطوات في قائمة MISSION بالأسفل، ثم هبوط.
  التقدم/الارتفاع/النزول مع تصحيح الانحراف، ويمين/يسار حركات مقصودة.

طريقة التصحيح:
  - Tello تعطي السرعة (vgx, vgy) في اتجاهات لا تطابق جسم الطائرة بالضرورة.
  - أثناء أول تقدم للأمام يتعلم البرنامج أي اتجاه في الحساس هو "التقدم"،
    واليمين هو العمودي عليه (قبل ذلك يُقدَّر من البوصلة yaw).
  - كل حركة (تقدم/ارتفاع/نزول) تُقسَّم إلى قطع (≤ 40 سم، ≥ 20 سم).
  - بعد كل قطعة: انحراف يسار > 10 سم -> يمين 20 | انحراف يمين > 10 سم -> يسار 20
  - ونرجع اتجاه الرأس (yaw) لو لفّ.
  - إذا صار التصحيح بالعكس: اجعل FLIP_RIGHT = True

التشغيل:
    pip install djitellopy
    python tello_mission2.py --simulate   # تجربة بدون طائرة
    python tello_mission2.py              # مع الطائرة (اتصل بشبكة Tello WiFi)
"""

import argparse
import math
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
FLIP_RIGHT = False          # البداية. يُعكس تلقائياً إذا رأى الحساس حركة يمين كأنها يسار
LEARN_MIN = 15              # أقل حركة (سم) يجب أن يراها الحساس ليتعلم اتجاه التقدم
CALIBRATE_SEC = 3.0         # مدة التحويم بعد الإقلاع لقياس خطأ الحساس (الطائرة ثابتة)
LOG_FILE = "flight_log.csv" # ملف تسجيل كل القراءات (أرسله إذا فيه مشكلة)


# ------------------ متتبع الانحراف ------------------
class DriftTracker:
    """يجمع السرعة الجانبية مع الزمن في الخلفية.

    Tello تعطي السرعة (vgx, vgy) في إطار غير محاذي لجسم الطائرة بالضرورة.
    بدون أي حركة يمين/يسار تجريبية: نتعلم اتجاه "التقدم" من قراءات الحساس
    أثناء حركات التقدم نفسها، واليمين هو العمودي عليه.
    """

    def __init__(self, tello):
        self.tello = tello
        self.drift = 0.0              # سم، موجب = يمين
        self.bias = (0.0, 0.0)        # خطأ الحساس الثابت (vgx, vgy) dm/s
        self.right = (0.0, SIGN_Y)    # اتجاه اليمين في إطار الحساس (يُعاير بعد الإقلاع)
        self.paused = False
        self.axis_ok = False          # هل تعلّم اتجاه التقدم من الحساس
        self.forward = (1.0, 0.0)
        self.conv = (1, 0)            # (hand, offset) العلاقة بين البوصلة والحساس
        self.flip = FLIP_RIGHT        # يُعكس تلقائياً إذا تبيّن أن اليمين بالعكس
        self._fsum = [0.0, 0.0]       # مجموع قراءات التقدم (لتعلم الاتجاه)
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
            self._update_axis(self.tello.get_yaw())   # اتجاه اليمين يتبع البوصلة
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

    # العلاقة بين البوصلة واتجاهات الحساس:
    #   زاوية التقدم في الحساس = hand * yaw + offset   (offset من مضاعفات 90)
    #   زاوية اليمين           = زاوية التقدم + hand * 90
    # نبدأ بالافتراض (hand=1, offset=0) ونصححه من أول تقدم.
    def _update_axis(self, yaw_deg):
        hand, offset = self.conv
        fa = math.radians(hand * yaw_deg + offset)
        ra = fa + math.radians(hand * 90)
        rx, ry = math.cos(ra), math.sin(ra)
        if self.flip:
            rx, ry = -rx, -ry
        with self._lock:
            self.forward = (math.cos(fa), math.sin(fa))
            self.right = (rx, ry)

    def set_axis_from_yaw(self, yaw_deg):
        self._update_axis(yaw_deg)

    def learn_forward(self, window, yaw_deg):
        """يحدد العلاقة بين البوصلة والحساس من قراءات حركة تقدم.
        الاتجاه الدقيق يأتي من البوصلة، فالانحراف أثناء الحركة لا يُخفي نفسه."""
        bx, by = self.bias
        for vx, vy in window:
            self._fsum[0] += vx - bx
            self._fsum[1] += vy - by
        mag = math.hypot(*self._fsum)
        if mag / LOOP_HZ * SPEED_TO_CM_S < LEARN_MIN:   # الحركة ما ظهرت في الحساس بشكل كافٍ
            return None
        measured = math.degrees(math.atan2(self._fsum[1], self._fsum[0]))
        best = None
        # حركة التقدم وحدها لا تميّز اليمين من اليسار (hand)،
        # لذلك نتعلم offset فقط، واليمين/اليسار يُتحقق منه من حركات التصحيح نفسها.
        hand = self.conv[0]
        for offset in (0, 90, 180, 270):
            err = abs(angle_diff(measured, hand * yaw_deg + offset))
            if best is None or err < best[0]:
                best = (err, hand, offset)
        err, hand, offset = best
        self.conv = (hand, offset)
        self._update_axis(yaw_deg)
        self.axis_ok = True
        return measured, err

    def lateral_of(self, window):
        """الانحراف الجانبي (سم) لمجموعة قراءات حسب اتجاه اليمين الحالي."""
        return sum(self._lateral(vx, vy) for vx, vy in window) / LOOP_HZ

    def set_drift(self, cm):
        with self._lock:
            self.drift = cm

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
    """حركة جانبية (تصحيح أو حركة من المسار).
    - بعد تعلّم الاتجاه: الحساس يقيس الحركة نفسها، ونتحقق أنه رآها بالاتجاه الصحيح؛
      إذا رآها بالعكس نعكس اتجاه اليمين تلقائياً.
    - قبل تعلّم الاتجاه: نوقف القياس ونحسبها بالمسافة المطلوبة، حتى لا يكرر التصحيح بلا نهاية."""
    sign = 1 if direction == "right" else -1
    trust_sensor = tracker.axis_ok
    if not trust_sensor:
        tracker.paused = True
    tracker.take_window()
    d0 = tracker.get()
    if direction == "right":
        tello.move_right(cm)
    else:
        tello.move_left(cm)
    time.sleep(0.3)
    w = tracker.take_window()
    if not trust_sensor:
        tracker.adjust(sign * cm)
        tracker.paused = False
        return
    # تحقق: هل رأى الحساس الحركة بنفس الاتجاه المطلوب؟
    measured = tracker.lateral_of(w)
    if measured * sign < -0.4 * cm:
        tracker.flip = not tracker.flip
        tracker.set_axis_from_yaw(tello.get_yaw())
        # كل الانحراف المحسوب قبل كان بإشارة معكوسة
        tracker.set_drift(-d0 + tracker.lateral_of(w))
        print(f"    تنبيه: الحساس رأى الحركة {direction} بالعكس -> عكست اتجاه اليمين تلقائياً")
        tracker.event("auto flip right axis")


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
        drift_before = tracker.get()
        move(chunk)
        w = tracker.take_window()
        if action == "forward" and not tracker.axis_ok:
            res = tracker.learn_forward(w, tello.get_yaw())
            if res:
                measured, err = res
                # نعيد حساب انحراف هذه القطعة بالاتجاه الصحيح
                tracker.set_drift(drift_before + tracker.lateral_of(w))
                hand, offset = tracker.conv
                ux, uy = tracker.right
                print(f"    تعلّمت الاتجاه: hand={hand:+d} offset={offset}°"
                      f" (فرق {err:.0f}°)  اليمين=({ux:+.2f}, {uy:+.2f})")
                if err > 35:
                    print("    تنبيه: الفرق كبير — الانحراف أثناء أول تقدم كان قوياً")
                tracker.event(f"learned hand={hand} offset={offset} err={err:.0f}")
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

        tracker.set_axis_from_yaw(target_yaw)
        ux, uy = tracker.right
        print(f">> تقدير أولي لاتجاه اليمين من البوصلة (yaw={target_yaw}°): ({ux:+.2f}, {uy:+.2f})")
        print("   (سيُضبط بدقة من الحساس أثناء أول تقدم)")
        tracker.event(f"axis from yaw={target_yaw}")
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
SIM_YAW_ERR = 0.0            # خطأ البوصلة في المحاكاة
SIM_HAND = 1                 # اتجاه دوران الحساس بالنسبة للبوصلة في المحاكاة (1 أو -1)


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
    def get_yaw(self):
        # الحساس يقيس التقدم بزاوية SIM_ROT = hand*yaw  =>  yaw = hand*SIM_ROT
        return int(SIM_HAND * SIM_ROT + SIM_YAW_ERR)
    def _sensor(self):
        """الحساس يقيس (تقدم، يمين) في إطار مُدار بزاوية SIM_ROT مثل الطائرة الحقيقية."""
        import math
        th = math.radians(SIM_ROT)
        f, r = self.cmd_vx / SPEED_TO_CM_S, self._vy() / SPEED_TO_CM_S
        rt = th + math.radians(SIM_HAND * 90)
        sx = f * math.cos(th) + r * math.cos(rt)
        sy = f * math.sin(th) + r * math.sin(rt)
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
    parser.add_argument("--sim-yaw-err", type=float, default=0.0,
                        help="خطأ البوصلة في المحاكاة (درجات)")
    parser.add_argument("--sim-hand", type=int, default=1, choices=(1, -1),
                        help="اتجاه دوران الحساس في المحاكاة")
    args = parser.parse_args()
    if args.simulate:
        global SIM_SENSOR_BIAS, SIM_ROT, SIM_YAW_ERR, SIM_HAND
        SIM_HAND = args.sim_hand
        SIM_SENSOR_BIAS = args.sim_bias
        SIM_ROT = args.sim_rot
        SIM_YAW_ERR = args.sim_yaw_err
        tello = FakeTello()
    else:
        from djitellopy import Tello
        tello = Tello()
    run(tello)


if __name__ == "__main__":
    main()
