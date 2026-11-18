"""
برنامج طائرة DJI Tello / RoboMaster TT
-------------------------------------
- تقلع الطائرة ثم تنتظر نصف ثانية
- ترتفع 35 سم، تتقدم 120 سم، ترجع للخلف 120 سم، تنزل 70 سم، تتقدم 120 سم ... وهكذا
- انتظار نصف ثانية بين كل أمر وآخر
- يستمر البرنامج لمدة دقيقتين ثم تهبط الطائرة

التثبيت:
    pip install djitellopy

التشغيل:
    1) شغّل الطائرة واتصل بشبكة الواي فاي الخاصة بها (TELLO-xxxx أو RMTT-xxxx)
    2) python tello_mission.py
"""

import time
from djitellopy import Tello

# مدة المهمة بالثواني (دقيقتان)
MISSION_DURATION = 120

# زمن الانتظار بين كل أمر (نصف ثانية)
WAIT = 0.5

# تسلسل الحركات (يتكرر حتى تنتهي الدقيقتان)
# ملاحظة: أقل مسافة يقبلها Tello هي 20 سم وأكبر مسافة 500 سم
# التسلسل مصمم بحيث ترجع الطائرة لنفس النقطة والارتفاع في نهاية كل دورة
# حتى لا تبتعد أو تلمس الأرض مع التكرار.
SEQUENCE = [
    ("up", 35),
    ("forward", 120),
    ("back", 120),
    ("down", 70),
    ("forward", 120),
    ("back", 120),
    ("up", 35),
]


def do_move(tello: Tello, direction: str, distance: int) -> None:
    """تنفيذ أمر حركة واحد."""
    moves = {
        "up": tello.move_up,
        "down": tello.move_down,
        "forward": tello.move_forward,
        "back": tello.move_back,
        "left": tello.move_left,
        "right": tello.move_right,
    }
    print(f"→ {direction} {distance} سم")
    moves[direction](distance)


def main() -> None:
    tello = Tello()
    tello.connect()
    print(f"البطارية: {tello.get_battery()}%")

    if tello.get_battery() < 20:
        print("البطارية منخفضة! اشحن الطائرة قبل الطيران.")
        return

    try:
        print("إقلاع...")
        tello.takeoff()
        time.sleep(WAIT)

        start = time.time()
        step = 0
        while time.time() - start < MISSION_DURATION:
            direction, distance = SEQUENCE[step % len(SEQUENCE)]
            do_move(tello, direction, distance)
            time.sleep(WAIT)
            step += 1

        print("انتهت الدقيقتان، جاري الهبوط...")
    except KeyboardInterrupt:
        print("تم الإيقاف يدوياً، جاري الهبوط...")
    except Exception as e:
        print(f"حدث خطأ: {e} — جاري الهبوط...")
    finally:
        try:
            tello.land()
        except Exception:
            tello.emergency()  # إيقاف المحركات إذا فشل الهبوط
        tello.end()


if __name__ == "__main__":
    main()
