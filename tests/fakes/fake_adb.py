"""Answers the adb commands qa-perf issues with realistic Android output (two fake devices)."""
import sys
import time

MEMINFO = """Applications Memory Usage (in Kilobytes):
** MEMINFO in pid 4321 [org.commcare.dalvik] **
                   Pss  Private  Private  SwapPss      Rss     Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free
 App Summary
           TOTAL PSS:   145373            TOTAL RSS:   230112       TOTAL SWAP PSS:       12
"""
GFX = """Applications Graphics Acceleration Info:
** Graphics info for pid 4321 [org.commcare.dalvik] **
Stats since: 1234ns
Total frames rendered: 820
Janky frames: 41 (5.00%)
50th percentile: 8ms
90th percentile: 14ms
95th percentile: 19ms
99th percentile: 40ms
"""
AM_START = """Starting: Intent { cmp=org.commcare.dalvik/org.commcare.activities.DispatchActivity }
Status: ok
LaunchState: {state}
Activity: org.commcare.dalvik/org.commcare.activities.DispatchActivity
TotalTime: {total}
WaitTime: {wait}
Complete
"""

args = sys.argv[1:]
if args[:1] == ["devices"]:
    print("List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n")
    sys.exit(0)
serial = args[1] if args[:1] == ["-s"] else ""
rest = args[2:] if serial else args
cmd = " ".join(rest)
slow = 300 if serial.endswith("6") else 0  # second device is a slower phone

if rest[:1] == ["install"]:
    print("Success")
elif "getprop ro.product.model" in cmd:
    print("Pixel 7" if not slow else "Galaxy A10")
elif "getprop ro.build.version.release" in cmd:
    print("14")
elif "getprop ro.build.version.sdk" in cmd:
    print("34")
elif "pm path" in cmd:
    print("package:/data/app/org.commcare.dalvik/base.apk")
elif "resolve-activity" in cmd:
    print("priority=0 preferredOrder=0\norg.commcare.dalvik/org.commcare.activities.DispatchActivity")
elif "am start" in cmd:
    cold = "-S" in cmd
    total = (1400 if cold else 350) + slow + int(time.time() * 1000) % 50
    print(AM_START.replace("{state}", "COLD" if cold else "WARM")
          .replace("{total}", str(total)).replace("{wait}", str(total + 20)))
elif "pidof" in cmd:
    print("4321")
elif "dumpsys meminfo" in cmd:
    print(MEMINFO)
elif "/proc/4321/stat" in cmd:
    ticks = int(time.time() * 40)  # ~40% of one core at CLK_TCK=100
    print(f"4321 (commcare.dalvik) S 1 1 0 0 -1 1077952832 1 0 0 0 {ticks // 2} {ticks - ticks // 2} 0 0 20 0")
elif "getconf CLK_TCK" in cmd:
    print("100")
elif "dumpsys gfxinfo" in cmd and "reset" not in cmd:
    print(GFX)
