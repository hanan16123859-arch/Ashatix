#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
برنامج التحكم التفاعلي بطائرة Tello لمسار (القيادة والتحكم)
=============================================================

الفكرة: الطائرة تبقى في الهواء، وأنت تدخل القياسات خطوة بخطوة.
كل خطوة تُنفَّذ ثم تبقى عليها حتى تتأكد؛ تستطيع التعديل (زيادة/نقصان)
بدون هبوط وبدون إعادة المسار من البداية. عند الضغط على Enter تنتقل
للخطوة التالية. القيم التي جرّبتها تُحفظ تلقائياً لتكون هي الافتراضية
في المرة القادمة.

التشغيل:
    python tello_course.py                 # الطائرة الحقيقية (اتصل بشبكة Tello أولاً)
    python tello_course.py --ip 127.0.0.1  # مع المحاكي tello_sim.py للتدريب
    python tello_course.py --free          # وضع الأوامر الحرة فقط (بدون مسار)

لا يحتاج أي مكتبات إضافية (Python 3.7+ فقط).
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import time

# عرض العربية بشكل صحيح في الطرفية (ويندوز خصوصاً)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

TELLO_PORT = 8889
LOCAL_PORT = 9000
MIN_MOVE = 20      # أقل حركة يقبلها Tello (سم)
MAX_MOVE = 500     # أكبر حركة في أمر واحد (سم)
KEEPALIVE_SEC = 7  # Tello يهبط تلقائياً إذا لم يستقبل أمراً خلال 15 ثانية

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SETTINGS = os.path.join(HERE, "course_settings.json")

# ---------------------------------------------------------------------------
# المسار الافتراضي (يمكن تعديله من الملف course_settings.json)
# أنواع الخطوات:
#   takeoff                 إقلاع
#   height  value=سم        الذهاب إلى ارتفاع محدد (يقيس الارتفاع بالحساس ويصحح)
#   move    dir=forward/back/left/right/up/down  value=سم
#   rotate  dir=cw (يمين) / ccw (يسار)  value=درجات
#   land                    هبوط
# ---------------------------------------------------------------------------
DEFAULT_COURSE = {
    "speed": 40,
    "steps": [
        {"type": "takeoff", "title": "الإقلاع"},
        {"type": "height", "value": 100, "title": "ضبط ارتفاع الإقلاع (لعبور الحلقة 1)"},
        {"type": "move", "dir": "forward", "value": 80, "title": "تقدم لعبور الحلقة 1"},
        {"type": "height", "value": 50, "title": "النزول لارتفاع عبور المستطيل 2"},
        {"type": "move", "dir": "forward", "value": 180, "title": "تقدم لعبور المستطيل 2"},
        {"type": "height", "value": 110, "title": "الارتفاع لعبور الحلقة 3"},
        {"type": "move", "dir": "right", "value": 0, "title": "إزاحة جانبية للحلقة 3 (موجب=يمين، سالب=يسار، 0=لا شيء)"},
        {"type": "move", "dir": "forward", "value": 100, "title": "تقدم لعبور الحلقة 3"},
        {"type": "rotate", "dir": "cw", "value": 0, "title": "لف يمين (درجات، 0=لا شيء)"},
        {"type": "move", "dir": "forward", "value": 100, "title": "تقدم لعبور الحلقة 4"},
        {"type": "rotate", "dir": "ccw", "value": 0, "title": "لف يسار (درجات، 0=لا شيء)"},
        {"type": "move", "dir": "forward", "value": 40, "title": "تقدم نحو نقطة الهبوط"},
        {"type": "land", "title": "الهبوط"},
    ],
}

OPPOSITE = {"forward": "back", "back": "forward", "left": "right",
            "right": "left", "up": "down", "down": "up", "cw": "ccw", "ccw": "cw"}

DIR_AR = {"forward": "أمام", "back": "خلف", "left": "يسار", "right": "يمين",
          "up": "أعلى", "down": "أسفل", "cw": "لف يمين", "ccw": "لف يسار"}

# اختصارات الأوامر الحرة (إنجليزي وعربي)
ALIASES = {
    "f": "forward", "forward": "forward", "تقدم": "forward", "ق": "forward",
    "b": "back", "back": "back", "رجوع": "back", "خلف": "back",
    "l": "left", "left": "left", "يسار": "left",
    "r": "right", "right": "right", "يمين": "right",
    "u": "up", "up": "up", "ارتفع": "up", "فوق": "up",
    "d": "down", "down": "down", "انزل": "down", "تحت": "down",
    "cw": "cw", "لف_يمين": "cw", "لفيمين": "cw",
    "ccw": "ccw", "لف_يسار": "ccw", "لفيسار": "ccw",
}

HELP = """
أوامر يمكنك كتابتها في أي وقت (القيم بالسنتيمتر، واللف بالدرجات):
  f 30  / تقدم 30      تقدم للأمام          b 30  / رجوع 30   للخلف
  l 20  / يسار 20      إزاحة يسار           r 20  / يمين 20   إزاحة يمين
  u 20  / ارتفع 20     ارتفاع               d 20  / انزل 20   نزول
  cw 90 / لف_يمين 90   دوران يمين           ccw 90 / لف_يسار 90  دوران يسار
  h 60  / ارتفاع 60    اذهب إلى ارتفاع 60 سم (يقيس ويصحح تلقائياً)
  speed 30             تغيير السرعة (10-100 سم/ث)
  stop / ثبات          تثبيت الطائرة مكانها
  bat / بطارية         نسبة البطارية       alt / كم           الارتفاع الحالي
  land / هبوط          هبوط                 e / طوارئ          إيقاف المحركات فوراً!
  takeoff / اقلاع      إقلاع
  next / Enter         الخطوة التالية       prev / سابق        الرجوع خطوة في القائمة
  goto 5 / خطوة 5      الانتقال لخطوة معينة   list / قائمة       عرض خطوات المسار
  help / مساعدة        هذه القائمة          quit / خروج        هبوط وخروج

داخل خطوة المسار: اكتب رقماً فقط لتنفيذ إضافة بنفس اتجاه الخطوة
(مثلاً 20 = 20 سم إضافية، -10 = 10 سم بعكس الاتجاه). في خطوة الارتفاع
الرقم يعني الارتفاع المطلوب مباشرة.
* الحركات الأقل من 20 سم تُنفَّذ بحيلة (تحرك عكسي 20 ثم أمامي 20+القيمة).
"""


class Tello:
    """اتصال UDP مباشر مع Tello (SDK 2.0)."""

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
                print(f"   ← إرسال: {cmd}")
            self.sock.sendto(cmd.encode("utf-8"), self.addr)
            self.last_cmd_time = time.time()
            self.sock.settimeout(timeout)
            try:
                data, _ = self.sock.recvfrom(1024)
                resp = data.decode("utf-8", errors="ignore").strip()
            except socket.timeout:
                resp = "timeout"
            self.last_cmd_time = time.time()
        if self.verbose and not quiet:
            print(f"   → رد الطائرة: {resp}")
        return resp

    def send_no_wait(self, cmd):
        """لأوامر الطوارئ: ترسل فوراً دون انتظار القفل."""
        self.sock.sendto(cmd.encode("utf-8"), self.addr)
        print(f"   ← إرسال فوري: {cmd}")

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
        """الارتفاع بالسم من حساس المسافة السفلي (tof)، وإلا من height?."""
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
        """حركة بأي قيمة: تقسم الكبيرة وتعالج الصغيرة (<20)."""
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
        # حركة صغيرة < 20 سم
        print(f"   (حركة صغيرة {cm} سم: تنفيذ بخطوتين)")
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
            print("   ⚠ تعذّرت قراءة الارتفاع.")
            return False
        diff = target - cur
        print(f"   الارتفاع الحالي ≈ {cur} سم، المطلوب {target} سم (الفرق {diff:+d})")
        if abs(diff) < 5:
            print("   الارتفاع مناسب، لا حاجة للحركة.")
            return True
        ok = self.move("up", diff)
        after = self.altitude()
        if after is not None:
            print(f"   الارتفاع الآن ≈ {after} سم")
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
            print(f"⚠ خطأ في قراءة {path}: {e}  — سيتم استخدام المسار الافتراضي")
    save_settings(path, DEFAULT_COURSE)
    return json.loads(json.dumps(DEFAULT_COURSE))


def save_settings(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠ لم يتم حفظ الإعدادات: {e}")


def parse_number(s):
    try:
        return float(s.replace("،", ".").replace(",", "."))
    except ValueError:
        return None


def step_text(i, st):
    t = st["type"]
    title = st.get("title", "")
    if t in ("move", "rotate"):
        unit = "°" if t == "rotate" else " سم"
        return f"{i + 1:>2}. {title}  [{DIR_AR[st['dir']]} {st.get('value', 0)}{unit}]"
    if t == "height":
        return f"{i + 1:>2}. {title}  [ارتفاع {st.get('value', 0)} سم]"
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
        if c in ("h", "ارتفاع") and arg is not None:
            if 20 <= arg <= 300:
                self.t.go_height(int(arg))
            else:
                print("   الارتفاع يجب أن يكون بين 20 و 300 سم.")
            return True
        if c in ("speed", "سرعة") and arg is not None:
            if self.t.set_speed(arg):
                self.s["speed"] = int(arg)
                save_settings(self.path, self.s)
            return True
        if c in ("stop", "ثبات"):
            self.t.send("stop")
            return True
        if c in ("bat", "battery", "بطارية"):
            print(f"   البطارية: {self.t.battery()}%")
            return True
        if c in ("alt", "كم", "tof"):
            print(f"   الارتفاع ≈ {self.t.altitude()} سم")
            return True
        if c in ("takeoff", "اقلاع", "إقلاع"):
            self.t.takeoff()
            return True
        if c in ("land", "هبوط"):
            self.t.land()
            return True
        if c in ("e", "emergency", "طوارئ"):
            self.t.emergency()
            return True
        if c in ("help", "?", "مساعدة"):
            print(HELP)
            return True
        if c in ("list", "قائمة"):
            self.show_course()
            return True
        return False

    def show_course(self):
        print("\n—— خطوات المسار ——")
        for k, st in enumerate(self.steps):
            mark = "◀" if k == self.i else " "
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
        print("\n=========== وضع المسار خطوة بخطوة ===========")
        print("Enter = تنفيذ القيمة الافتراضية / الانتقال للتالي")
        print("رقم   = تنفيذ بقيمة مختلفة أو إضافة تعديل")
        print("s     = تخطي الخطوة بدون تنفيذ،  help = كل الأوامر\n")
        self.show_course()

        while self.i < len(self.steps):
            st = self.steps[self.i]
            executed = False
            total = 0.0  # مجموع ما نُفذ في هذه الخطوة (move/rotate)
            print("─" * 55)
            print(f"الخطوة {step_text(self.i, st)}")

            while True:
                t = st["type"]
                if not executed:
                    if t in ("takeoff", "land"):
                        hint = "Enter=تنفيذ، s=تخطي"
                    elif t == "height":
                        hint = f"الارتفاع المطلوب سم [Enter={st.get('value')}]"
                    else:
                        unit = "درجة" if t == "rotate" else "سم"
                        hint = f"المقدار {unit} [Enter={st.get('value')}]"
                else:
                    if t == "height":
                        hint = "✔ Enter=التالي | رقم=ارتفاع جديد"
                    elif t in ("move", "rotate"):
                        hint = (f"✔ نُفذ {total:g} | Enter=التالي | رقم=إضافة "
                                f"(سالب=عكس الاتجاه)")
                    else:
                        hint = "✔ Enter=التالي"
                try:
                    line = input(f"  {hint} > ").strip()
                except EOFError:
                    line = "quit"

                low = line.lower()
                if low in ("quit", "exit", "q", "خروج"):
                    return "quit"
                if low in ("s", "skip", "تخطي") or (executed and low in ("", "next", "n", "التالي")):
                    self._remember(st, executed, total)
                    self.i += 1
                    break
                if low in ("prev", "p", "سابق"):
                    self._remember(st, executed, total)
                    self.i = max(0, self.i - 1)
                    break
                m = re.match(r"^(goto|خطوة)\s+(\d+)$", low)
                if m:
                    self._remember(st, executed, total)
                    self.i = max(0, min(len(self.steps) - 1, int(m.group(2)) - 1))
                    break

                # Enter على خطوة لم تنفذ = القيمة الافتراضية
                if line == "":
                    value = st.get("value", 0)
                else:
                    value = parse_number(line)
                    if value is None:
                        if not self.free_command(line):
                            print("   ؟ أمر غير معروف — اكتب help")
                        continue

                if t in ("takeoff", "land"):
                    if line == "":
                        self.run_step(st, 0)
                        executed = True
                    else:
                        print("   هذه الخطوة لا تحتاج رقماً.")
                    continue

                if t == "height":
                    if not 20 <= value <= 300:
                        print("   الارتفاع يجب أن يكون بين 20 و 300 سم.")
                        continue
                    self.run_step(st, value)
                    st["value"] = int(value)
                    executed = True
                    save_settings(self.path, self.s)
                    continue

                # move / rotate
                if value == 0:
                    if not executed:
                        print("   القيمة 0: لا حركة. (Enter مرة أخرى للانتقال)")
                        executed = True
                    continue
                if self.run_step(st, value):
                    total += value
                executed = True

        print("\n✔ انتهت خطوات المسار. يمكنك الاستمرار بالأوامر الحرة أو كتابة quit.")

    def _remember(self, st, executed, total):
        """حفظ القيمة المجربة لتصبح الافتراضية في المرة القادمة."""
        if executed and st["type"] in ("move", "rotate") and total != 0:
            st["value"] = int(total)  # السالب يعني الاتجاه المعاكس
            save_settings(self.path, self.s)
            print(f"   (حُفظت القيمة {int(total)} لهذه الخطوة)")

    def free_mode(self):
        print("\n=========== وضع الأوامر الحرة ===========")
        print(HELP)
        while True:
            try:
                line = input("أمر > ").strip()
            except EOFError:
                return
            if line.lower() in ("quit", "exit", "q", "خروج"):
                return
            if line in ("course", "مسار"):
                if self.i >= len(self.steps):
                    self.i = 0
                if self.course() == "quit":
                    return
                continue
            if line and not self.free_command(line):
                print("   ؟ أمر غير معروف — اكتب help")


def main():
    ap = argparse.ArgumentParser(description="تحكم تفاعلي بمسار Tello")
    ap.add_argument("--ip", default="192.168.10.1", help="عنوان الطائرة (127.0.0.1 للمحاكي)")
    ap.add_argument("--settings", default=DEFAULT_SETTINGS, help="ملف قياسات المسار")
    ap.add_argument("--free", action="store_true", help="وضع الأوامر الحرة فقط")
    ap.add_argument("--reset", action="store_true", help="إرجاع المسار للقيم الافتراضية")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.settings):
        os.remove(args.settings)
    settings = load_settings(args.settings)

    print("جاري الاتصال بالطائرة ...")
    tello = Tello(args.ip)
    if not tello.connect():
        print("✖ لم يتم الاتصال. تأكد أنك متصل بشبكة الواي فاي TELLO-xxxx وأن الطائرة مشغلة.")
        tello.close()
        return
    bat = tello.battery()
    print(f"✔ تم الاتصال. البطارية: {bat}%")
    if bat is not None and bat < 20:
        print("⚠ البطارية منخفضة (أقل من 20%) — قد ترفض الطائرة الإقلاع.")
    tello.set_speed(settings.get("speed", 40))

    pilot = Pilot(tello, settings, args.settings)
    try:
        if args.free:
            pilot.free_mode()
        else:
            if pilot.course() != "quit" and tello.flying:
                pilot.free_mode()
    except KeyboardInterrupt:
        print("\n⚠ Ctrl+C — هبوط ...")
    finally:
        if tello.flying:
            tello.land()
        tello.close()
        print("تم الإغلاق.")


if __name__ == "__main__":
    main()
