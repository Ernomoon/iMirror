#!/usr/bin/env bash
# Runs inside MSYS2 UCRT64. Copies uxplay.exe + the GStreamer plugins UxPlay uses + every
# DLL they depend on into a self-contained folder:  OUT/{bin,lib/gstreamer-1.0,libexec/gstreamer-1.0}
set -euo pipefail
UXPLAY_EXE="$1"
OUT="$2"
PREFIX=/ucrt64

rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/lib/gstreamer-1.0" "$OUT/libexec/gstreamer-1.0"
cp "$UXPLAY_EXE" "$OUT/bin/"
cp "$PREFIX/bin/gst-inspect-1.0.exe" "$OUT/bin/"
cp "$PREFIX/libexec/gstreamer-1.0/gst-plugin-scanner.exe" "$OUT/libexec/gstreamer-1.0/"

# Elements in UxPlay's pipelines (renderers/*.c): appsrc queue h264parse h265parse decodebin
# (-> d3d12/d3d11 hardware decoders, avdec_* fallback) videoconvert videoscale videoflip
# d3d12videosink d3d11videosink autovideosink, avdec_aac avdec_alac aacparse audioconvert
# audioresample volume level wasapisink autoaudiosink, jpegdec imagefreeze (cover art).
PLUGINS="coreelements app videoparsersbad playback typefindfunctions d3d11 d3d12 libav
         videoconvertscale videofilter audioconvert audioresample volume level wasapi wasapi2
         directsound autodetect audioparsers jpeg imagefreeze isomp4 nvcodec qsv"
for p in $PLUGINS; do
  f="$PREFIX/lib/gstreamer-1.0/libgst$p.dll"
  if [ -f "$f" ]; then cp "$f" "$OUT/lib/gstreamer-1.0/"; else echo "note: plugin $p not present"; fi
done

# Collect DLL dependencies recursively (only those from the MSYS2 prefix).
collect() {
  ldd "$1" 2>/dev/null | awk '{print $3}' | grep -i "^$PREFIX/" || true
}
todo=$(ls "$OUT"/bin/*.exe "$OUT"/libexec/gstreamer-1.0/*.exe "$OUT"/lib/gstreamer-1.0/*.dll)
for round in 1 2 3; do
  for f in $todo; do collect "$f"; done | sort -u > /tmp/deps.txt
  added=""
  while read -r d; do
    b=$(basename "$d")
    if [ ! -f "$OUT/bin/$b" ]; then cp "$d" "$OUT/bin/"; added="$added $OUT/bin/$b"; fi
  done < /tmp/deps.txt
  [ -z "$added" ] && break
  todo="$added"
done
echo "bundle: $(ls "$OUT/bin" | wc -l) files in bin, $(ls "$OUT/lib/gstreamer-1.0" | wc -l) plugins, $(du -sh "$OUT" | cut -f1)"
