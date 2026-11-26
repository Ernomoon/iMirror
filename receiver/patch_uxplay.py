#!/usr/bin/env python3
"""Adds machine-readable statistics lines to UxPlay's stdout.

iMirror reads these lines to show the REAL stream parameters that the iPhone sends:
  IMIRROR_VIDEO codec=h265 w=1179 h=2556          (on every codec/size change)
  IMIRROR_STATS fps=59.9 kbps=24500 frames=60     (once per second while video flows)
  IMIRROR_CONN open=1                              (connection count changes)
  IMIRROR_MDNS type=_airplay._tcp port=7000 name=... txt=<hex>   (with IMIRROR_EXTERNAL_MDNS=1)
  IMIRROR_STAGE register / IMIRROR_STAGE ready     (start-up progress, for diagnosing hangs)
stdout is made unbuffered so the last line before any crash always reaches iMirror's log.

Usage: python patch_uxplay.py <path-to-UxPlay-source>
The patch is idempotent: running it twice does nothing the second time.
"""
import sys
from pathlib import Path

MARK = "/* IMIRROR_PATCH */"

STATS_FUNC = r'''
/* IMIRROR_PATCH */
static gint64 imirror_window_start = 0;
static unsigned int imirror_frames = 0;
static unsigned long long imirror_bytes = 0;
static void imirror_count_frame(size_t bytes) {
    gint64 now = g_get_monotonic_time();   /* microseconds */
    if (imirror_window_start == 0) {
        imirror_window_start = now;
    }
    imirror_frames++;
    imirror_bytes += bytes;
    gint64 elapsed = now - imirror_window_start;
    if (elapsed >= 1000000) {
        double secs = (double) elapsed / 1000000.0;
        printf("IMIRROR_STATS fps=%.1f kbps=%.0f frames=%u\n",
               imirror_frames / secs, (imirror_bytes * 8.0 / 1000.0) / secs, imirror_frames);
        fflush(stdout);
        imirror_window_start = now;
        imirror_frames = 0;
        imirror_bytes = 0;
    }
}

extern "C" void video_process (void *cls, raop_ntp_t *ntp, video_decode_struct *data) {
    imirror_count_frame((size_t) data->data_len);
'''

def _read(path: Path) -> str:
    # open(newline="") keeps CRLF visible (Path.read_text(newline=) needs Python 3.13)
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def replace_once(src: str, old: str, new: str, what: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"[patch_uxplay] cannot apply '{what}': anchor found {n} times (expected 1). "
                 f"UxPlay source changed; use the pinned commit.")
    return src.replace(old, new)

MDNS_HELPER = r"""
/* IMIRROR_PATCH */
/* external mDNS. When IMIRROR_EXTERNAL_MDNS is set, UxPlay does not run its
 * single-interface responder; it prints the service data and iMirror announces it on every
 * LAN interface (python-zeroconf). */
static int imirror_external_mdns(const char *type, const char *full_name, unsigned short port,
                                 const mdnsd_txt_t *txt) {
    if (!getenv("IMIRROR_EXTERNAL_MDNS")) return 0;
    printf("IMIRROR_MDNS type=%s port=%u name=%s txt=", type, (unsigned) port, full_name);
    for (int i = 0; i < txt->length; i++) printf("%02x", (unsigned) (unsigned char) txt->bytes[i]);
    printf("\n");
    fflush(stdout);
    return 1;
}

void *
dnssd_private_init("""

def patch_mdns(root: Path) -> None:
    path = root / "lib" / "mdnsd" / "dnssd_mdnsd.c"
    src = _read(path).replace("\r\n", "\n")
    if MARK in src:
        print("[patch_uxplay] mdns already patched")
        return
    src = replace_once(src, "void *\ndnssd_private_init(", MDNS_HELPER.lstrip("\n"), "mdns helper")
    for kind, txt in (("raop", "raop_record"), ("airplay", "airplay_record")):
        src = replace_once(
            src,
            f"    dnssd->{kind}_port = port;\n    dnssd->{kind}_registered = 1;\n",
            f"    dnssd->{kind}_port = port;\n"
            f"    if (imirror_external_mdns(\"_{kind}._tcp\", dnssd->{kind}_name, port, &dnssd->{txt})) {{\n"
            f"        return 0;\n    }}\n"
            f"    dnssd->{kind}_registered = 1;\n",
            f"{kind} external mdns")
    path.write_text(src, encoding="utf-8", newline="\n")
    print("[patch_uxplay] patched", path)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1]) / "uxplay.cpp"
    # newline="" keeps CRLF visible; normalise so anchors match even if git converted line endings
    src = _read(path).replace("\r\n", "\n")
    if MARK in src:
        print("[patch_uxplay] already patched")
        patch_mdns(Path(sys.argv[1]))
        return

    src = replace_once(
        src,
        "extern \"C\" void video_process (void *cls, raop_ntp_t *ntp, video_decode_struct *data) {\n",
        STATS_FUNC.lstrip("\n"),
        "frame counter")

    src = replace_once(
        src,
        "extern \"C\" int video_set_codec(void *cls, video_codec_t codec) {\n",
        "static const char *imirror_codec = \"h264\";  /* IMIRROR */\n"
        "extern \"C\" int video_set_codec(void *cls, video_codec_t codec) {\n",
        "codec variable")

    src = replace_once(
        src,
        "    bool video_is_h265 = (codec == VIDEO_CODEC_H265);\n    if (mux_to_file) {",
        "    bool video_is_h265 = (codec == VIDEO_CODEC_H265);\n"
        "    imirror_codec = video_is_h265 ? \"h265\" : \"h264\";\n"
        "    if (mux_to_file) {",
        "codec report")

    src = replace_once(
        src,
        "extern \"C\" void video_report_size(void *cls, float *width_source, float *height_source, float *width, float *height) {\n",
        "extern \"C\" void video_report_size(void *cls, float *width_source, float *height_source, float *width, float *height) {\n"
        "    printf(\"IMIRROR_VIDEO codec=%s w=%u h=%u\\n\", imirror_codec,\n"
        "           (unsigned) *width_source, (unsigned) *height_source);\n"
        "    fflush(stdout);\n",
        "resolution report")

    src = replace_once(
        src,
        "    open_connections++;\n    LOGD(\"Open connections: %i\", open_connections);\n",
        "    open_connections++;\n    LOGD(\"Open connections: %i\", open_connections);\n"
        "    printf(\"IMIRROR_CONN open=%u\\n\", open_connections); fflush(stdout);\n",
        "connect report")

    src = replace_once(
        src,
        "    open_connections--;\n    LOGD(\"Open connections: %i\", open_connections);\n",
        "    open_connections--;\n    LOGD(\"Open connections: %i\", open_connections);\n"
        "    printf(\"IMIRROR_CONN open=%u\\n\", open_connections); fflush(stdout);\n",
        "disconnect report")

    src = replace_once(
        src,
        "#else\nint main (int argc, char *argv[]) {\n#endif\n",
        "#else\nint main (int argc, char *argv[]) {\n#endif\n"
        "    setvbuf(stdout, NULL, _IONBF, 0);  /* IMIRROR: never lose the last lines */\n"
        "    setvbuf(stderr, NULL, _IONBF, 0);\n",
        "unbuffered stdout")

    src = replace_once(
        src,
        "    if (register_dnssd()) {\n",
        "    printf(\"IMIRROR_STAGE register\\n\"); fflush(stdout);\n"
        "    if (register_dnssd()) {\n"
        "        printf(\"IMIRROR_STAGE register_failed\\n\"); fflush(stdout);\n",
        "register stage")

    path.write_text(src, encoding="utf-8", newline="\n")
    print("[patch_uxplay] patched", path)
    patch_mdns(Path(sys.argv[1]))

if __name__ == "__main__":
    main()
