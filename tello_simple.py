"""
مهمة طيران Tello — بدون تصحيح انحراف
======================================

إقلاع، ثم الخطوات في قائمة MISSION بالترتيب، ثم هبوط.

التشغيل:
    pip install djitellopy
    python tello_simple.py --simulate   # تجربة بدون طائرة
    python tello_simple.py              # مع الطائرة (اتصل بشبكة Tello WiFi)

للطوارئ: اضغط Ctrl+C والطائرة تهبط.
"""

import argparse
import time

# ------------------ المسار ------------------
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

MOVE_SPEED = 30             # سرعة الحركة سم/ث (10-100)
MIN_BATTERY = 30            # لا تقلع إذا البطارية أقل من هذا
PAUSE_SEC = 0.5             # توقف قصير بين الخطوات حتى تثبت الطائرة

VALID_ACTIONS = {"up", "down", "forward", "back", "right", "left"}


def check_mission():
    """يفحص المسار قبل الإقلاع حتى لا يتوقف في الجو بسبب خطأ كتابة."""
    ok = True
    for i, (action, cm) in enumerate(MISSION, 1):
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
    print(f"البطارية: {battery} %")
    if battery < MIN_BATTERY:
        print(f"البطارية أقل من {MIN_BATTERY}% — اشحن الطائرة أولاً.")
        tello.end()
        return
    tello.set_speed(MOVE_SPEED)

    moves = {
        "up": tello.move_up,
        "down": tello.move_down,
        "forward": tello.move_forward,
        "back": tello.move_back,
        "right": tello.move_right,
        "left": tello.move_left,
    }

    try:
        print(">> إقلاع")
        tello.takeoff()
        time.sleep(1)
        for i, (action, cm) in enumerate(MISSION, 1):
            print(f">> {i}. {action} {cm} سم")
            moves[action](cm)
            time.sleep(PAUSE_SEC)
        print(">> هبوط")
    except KeyboardInterrupt:
        print("\nأوقفتها يدوياً (Ctrl+C) -> هبوط")
    except Exception as e:
        print("\nتوقفت المهمة بسبب خطأ:", e)
        if "Motor stop" in str(e):
            print("المعنى: الطائرة صارت على الأرض (بطارية ضعيفة، حرارة، أو اصطدام).")
    finally:
        try:
            tello.land()
        except Exception:
            print("(الطائرة على الأرض أصلاً)")
        tello.end()


class FakeTello:
    """طائرة وهمية للتجربة بدون طيران."""

    def __getattr__(self, name):
        def cmd(*args):
            print(f"   [sim] {name} {' '.join(map(str, args))}".rstrip())
            return 90 if name == "get_battery" else None
        return cmd


def main():
    parser = argparse.ArgumentParser(description="Tello mission without drift correction")
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
