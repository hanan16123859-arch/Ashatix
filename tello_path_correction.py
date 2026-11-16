"""
تصحيح مسار طائرة DJI Tello أثناء التقدم للأمام
==================================================

الفكرة:
  - الطائرة تتقدم للأمام على مراحل قصيرة.
  - أثناء التقدم نقرأ السرعة الجانبية (vgy) من حساسات الطائرة
    ونجمعها مع الزمن لنعرف كم انحرفت يمين أو يسار.
  - إذا انحرفت يسار أكثر من حد معيّن  -> تتحرك يمين 20 سم.
  - إذا انحرفت يمين أكثر من حد معيّن -> تتحرك يسار 20 سم.
  - كذلك نراقب اتجاه الرأس (yaw)؛ إذا لفّت الطائرة نرجعها لاتجاهها الأصلي.

التثبيت:
    pip install djitellopy

التشغيل:
    python tello_path_correction.py            # مع الطائرة الحقيقية (اتصل بشبكة Tello أولاً)
    python tello_path_correction.py --simulate # تجربة بدون طائرة

ملاحظة: أقل مسافة تقبلها Tello في أوامر الحركة هي 20 سم،
لذلك 20 سم هي أصغر تصحيح ممكن.
"""

import argparse
import random
import time

# ------------------ الإعدادات ------------------
TOTAL_FORWARD_CM = 300      # المسافة الكلية للتقدم (سم)
FORWARD_SPEED = 30          # سرعة التقدم (0-100) في أمر rc
SEGMENT_TIME = 1.0          # مدة كل مرحلة تقدم (ثانية) قبل الفحص
CORRECTION_CM = 20          # مسافة التصحيح يمين/يسار
DRIFT_THRESHOLD_CM = 10     # الانحراف الذي يُعتبر "انحراف" ويحتاج تصحيح
YAW_THRESHOLD_DEG = 8       # الانحراف المسموح في اتجاه الرأس
LOOP_HZ = 20                # عدد القراءات في الثانية

# vgy في Tello SDK بوحدة dm/s ، نحولها إلى cm/s
SPEED_TO_CM_S = 10
# إذا وجدت أن الطائرة تصحح بالعكس، غيّر هذه القيمة إلى -1
# (موجب = انحراف لليمين)
SIGN_Y = -1  # مُعدَّلة بعد تجربة حقيقية: كان التصحيح بالعكس مع 1


# ------------------ طائرة وهمية للتجربة ------------------
class FakeTello:
    """تحاكي الطائرة لتجربة المنطق بدون طيران."""

    def __init__(self):
        self.y = 0.0          # الموضع الجانبي الحقيقي (سم) موجب = يمين
        self.vy = 0.0         # السرعة الجانبية (سم/ث)
        self.yaw = 0
        self.battery = 90
        self.wind = random.choice([-1, 1]) * random.uniform(8, 15)  # رياح ثابتة يمين/يسار

    def connect(self): print("[sim] connected")
    def get_battery(self): return self.battery
    def takeoff(self): print("[sim] takeoff")
    def land(self): print("[sim] land")
    def get_yaw(self): return self.yaw
    def end(self): pass

    def send_rc_control(self, lr, fb, ud, yaw):
        # رياح عشوائية تدفع الطائرة يمين أو يسار
        if fb > 0:
            if random.random() < 0.01:          # أحياناً تغيّر الرياح اتجاهها
                self.wind = -self.wind
            self.vy = self.wind + random.uniform(-3, 3)
        else:
            self.vy = 0
        self.y += self.vy / LOOP_HZ

    def get_speed_y(self):
        return round(-self.vy / SPEED_TO_CM_S)  # إشارة معكوسة مثل الطائرة الحقيقية

    def move_right(self, cm):
        self.y += cm
        print(f"[sim] move_right {cm}  -> موضع جانبي حقيقي = {self.y:.1f} سم")

    def move_left(self, cm):
        self.y -= cm
        print(f"[sim] move_left {cm}   -> موضع جانبي حقيقي = {self.y:.1f} سم")

    def rotate_clockwise(self, deg): self.yaw += deg
    def rotate_counter_clockwise(self, deg): self.yaw -= deg


# ------------------ منطق التصحيح ------------------
def angle_diff(a, b):
    """الفرق بين زاويتين في المدى -180..180"""
    return (a - b + 180) % 360 - 180


def fly_forward_segment(tello, seconds):
    """يتقدم للأمام لمدة معينة ويرجع الانحراف الجانبي المقدّر (سم)."""
    drift = 0.0
    dt = 1.0 / LOOP_HZ
    end = time.time() + seconds
    while time.time() < end:
        tello.send_rc_control(0, FORWARD_SPEED, 0, 0)
        vy_cm_s = SIGN_Y * tello.get_speed_y() * SPEED_TO_CM_S
        drift += vy_cm_s * dt
        time.sleep(dt)
    # توقف وثبات
    tello.send_rc_control(0, 0, 0, 0)
    time.sleep(0.5)
    return drift


def correct_heading(tello, target_yaw):
    err = angle_diff(tello.get_yaw(), target_yaw)
    if abs(err) > YAW_THRESHOLD_DEG:
        deg = int(abs(err))
        if err > 0:
            print(f"  الرأس لفّ يمين {deg}° -> تدوير يسار")
            tello.rotate_counter_clockwise(deg)
        else:
            print(f"  الرأس لفّ يسار {deg}° -> تدوير يمين")
            tello.rotate_clockwise(deg)


def run(tello):
    tello.connect()
    print("البطارية:", tello.get_battery(), "%")
    tello.takeoff()
    time.sleep(1)

    target_yaw = tello.get_yaw()
    lateral = 0.0                      # الانحراف الجانبي المتراكم (سم)
    traveled = 0.0
    step_cm = FORWARD_SPEED * SEGMENT_TIME  # تقدير تقريبي للمسافة في كل مرحلة

    try:
        while traveled < TOTAL_FORWARD_CM:
            lateral += fly_forward_segment(tello, SEGMENT_TIME)
            traveled += step_cm
            print(f"تقدم ≈ {traveled:.0f} سم | الانحراف الجانبي ≈ {lateral:+.1f} سم")

            if lateral < -DRIFT_THRESHOLD_CM:
                print(f"  انحرفت يسار -> تحرك يمين {CORRECTION_CM} سم")
                tello.move_right(CORRECTION_CM)
                lateral += CORRECTION_CM
            elif lateral > DRIFT_THRESHOLD_CM:
                print(f"  انحرفت يمين -> تحرك يسار {CORRECTION_CM} سم")
                tello.move_left(CORRECTION_CM)
                lateral -= CORRECTION_CM

            correct_heading(tello, target_yaw)
    finally:
        tello.send_rc_control(0, 0, 0, 0)
        tello.land()
        tello.end()


def main():
    parser = argparse.ArgumentParser(description="Tello forward path correction")
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
