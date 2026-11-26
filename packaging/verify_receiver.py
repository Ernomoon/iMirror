"""CI check: the bundled receiver works in isolation (no MSYS2 on PATH).

python packaging/verify_receiver.py dist-receiver
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from imirror.receiver import receiver_env  # noqa: E402

REQUIRED = ["appsrc", "queue", "h264parse", "h265parse", "decodebin", "videoconvert", "videoscale",
            "avdec_h264", "avdec_h265", "avdec_aac", "avdec_alac", "aacparse", "audioconvert",
            "audioresample", "volume", "level", "wasapisink", "autoaudiosink", "autovideosink",
            "d3d11videosink", "fakesink"]
WANTED = ["d3d12videosink", "d3d11h264dec", "d3d11h265dec", "d3d12h264dec", "d3d12h265dec", "videoflip",
          "jpegdec", "imagefreeze"]


def main(root):
    root = Path(root).resolve()
    exe = root / "bin" / "uxplay.exe"
    env = receiver_env(str(exe))
    # isolation: nothing from MSYS2 or other GStreamer installs
    env["PATH"] = os.pathsep.join([str(root / "bin"), os.environ["SystemRoot"] + r"\System32", os.environ["SystemRoot"]])
    inspect = root / "bin" / "gst-inspect-1.0.exe"
    missing = []
    for el in REQUIRED + WANTED:
        r = subprocess.run([str(inspect), el], env=env, capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"{'OK ' if ok else '-- '} {el}")
        if not ok and el in REQUIRED:
            missing.append(el)
    r = subprocess.run([str(exe), "-h"], env=env, capture_output=True, text=True, timeout=30)
    print("uxplay -h exit", r.returncode, (r.stdout + r.stderr).splitlines()[:2])
    if r.returncode != 0:
        missing.append("uxplay.exe runs")
    if missing:
        sys.exit("MISSING: " + ", ".join(missing))
    print("RECEIVER BUNDLE OK")


if __name__ == "__main__":
    main(sys.argv[1])
