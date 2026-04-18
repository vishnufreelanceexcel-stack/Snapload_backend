"""
SnapLoad Backend - Flask + yt-dlp
Render.com free tier compatible
"""
import os, uuid, time, json, threading, hashlib, shutil
from datetime import datetime, date
from functools import wraps
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# ── Config ──────────────────────────────────────────────────────────
ADMIN_SECRET   = os.environ.get("ADMIN_SECRET", "snapload_admin_2026")
DOWNLOAD_DIR   = "/tmp/snapload"
ANALYTICS_FILE = "/tmp/snapload_analytics.json"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Check if ffmpeg is available on this server
FFMPEG_OK = shutil.which("ffmpeg") is not None
print(f"[SnapLoad] FFmpeg available: {FFMPEG_OK}")

# ── Analytics ────────────────────────────────────────────────────────
lock = threading.Lock()

def load_stats():
    try:
        with open(ANALYTICS_FILE) as f:
            return json.load(f)
    except:
        return {
            "total_downloads": 0,
            "total_fetches": 0,
            "daily": {},
            "monthly": {},
            "reasons": {},
            "formats": {}
        }

def save_stats(data):
    try:
        with open(ANALYTICS_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

stats = load_stats()

def record(event, ip=None, fmt=None, reason=None):
    today     = date.today().isoformat()
    month_key = today[:7]
    with lock:
        if event == "fetch":
            stats["total_fetches"] = stats.get("total_fetches", 0) + 1
        if event == "download":
            stats["total_downloads"] = stats.get("total_downloads", 0) + 1

        day = stats.setdefault("daily", {}).setdefault(today, {"fetches":0,"downloads":0,"visitors":[]})
        mon = stats.setdefault("monthly", {}).setdefault(month_key, {"fetches":0,"downloads":0})

        if event == "fetch":
            day["fetches"] += 1
            mon["fetches"] += 1
        if event == "download":
            day["downloads"] += 1
            mon["downloads"] += 1

        if ip and ip not in day["visitors"]:
            day["visitors"].append(ip)

        if fmt:
            stats.setdefault("formats", {})[fmt] = stats["formats"].get(fmt, 0) + 1
        if reason and reason != "skipped":
            stats.setdefault("reasons", {})[reason] = stats["reasons"].get(reason, 0) + 1

        save_stats(stats)

def get_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

def cleanup(path, delay=90):
    def _del():
        time.sleep(delay)
        try:
            os.remove(path)
        except:
            pass
    threading.Thread(target=_del, daemon=True).start()

# ── Admin auth ───────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = request.headers.get("X-Admin-Secret") or request.args.get("secret", "")
        if secret != ADMIN_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ════════════════════════════════════════════════
#  ROUTE: /health  — status check
# ════════════════════════════════════════════════
@app.route("/health")
def health():
    return jsonify({"status": "ok", "ffmpeg": FFMPEG_OK})

# ════════════════════════════════════════════════
#  ROUTE: /info  — fetch video metadata
# ════════════════════════════════════════════════
@app.route("/info")
def get_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    record("fetch", ip=get_ip())

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private" in msg:
            return jsonify({"error": "This video is private."}), 400
        if "age" in msg.lower():
            return jsonify({"error": "This video is age-restricted."}), 400
        return jsonify({"error": f"Could not fetch: {msg[:150]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)[:150]}), 500

    formats     = info.get("formats", [])
    video_fmts  = []
    audio_fmts  = []
    QUALITY_MAP = {"1080":1,"720":2,"480":3,"360":4,"240":5,"144":6}

    for f in formats:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        fsize  = f.get("filesize") or f.get("filesize_approx")
        fid    = f.get("format_id", "")
        abr    = f.get("abr")
        ext    = f.get("ext", "mp4")

        if vcodec != "none" and height:
            video_fmts.append({
                "format_id":  fid,
                "label":      f"{height}p",
                "height":     height,
                "has_audio":  acodec != "none",
                "size":       fsize,
                "ext":        ext,
            })

        elif vcodec == "none" and acodec != "none" and abr:
            audio_fmts.append({
                "format_id": fid,
                "label":     f"MP3 {int(abr)}kbps",
                "abr":       abr,
                "size":      fsize,
                "ext":       ext,
            })

    # Deduplicate by height, prefer has_audio
    best = {}
    for f in sorted(video_fmts, key=lambda x: (x["height"], x["has_audio"])):
        best[f["height"]] = f
    clean_video = sorted(best.values(), key=lambda x: QUALITY_MAP.get(str(x["height"]), 9))

    # Best 2 audio formats
    seen_abr, clean_audio = set(), []
    for f in sorted(audio_fmts, key=lambda x: -(x.get("abr") or 0)):
        if f.get("abr") not in seen_abr:
            seen_abr.add(f.get("abr"))
            clean_audio.append(f)
        if len(clean_audio) >= 2:
            break

    thumbs = info.get("thumbnails", [])
    thumb  = thumbs[-1]["url"] if thumbs else info.get("thumbnail", "")

    return jsonify({
        "id":            info.get("id", ""),
        "title":         info.get("title", "YouTube Video"),
        "channel":       info.get("channel") or info.get("uploader", "YouTube"),
        "duration":      info.get("duration", 0),
        "thumb":         thumb,
        "ffmpeg":        FFMPEG_OK,
        "video_formats": [{"format_id":f["format_id"],"label":f["label"],"size":f["size"],"has_audio":f["has_audio"],"ext":"mp4"} for f in clean_video],
        "audio_formats": [{"format_id":f["format_id"],"label":f["label"],"size":f["size"],"ext":"mp3"} for f in clean_audio],
    })

# ════════════════════════════════════════════════
#  ROUTE: /download  — download + send file
# ════════════════════════════════════════════════
@app.route("/download")
def download_video():
    url      = request.args.get("url", "").strip()
    fmt_id   = request.args.get("format_id", "best")
    is_audio = request.args.get("audio", "0") == "1"
    reason   = request.args.get("reason", "")
    label    = request.args.get("label", "video")

    if not url:
        return jsonify({"error": "No URL"}), 400

    uid      = uuid.uuid4().hex
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")

    # Build yt-dlp options based on what's available
    if is_audio:
        if FFMPEG_OK:
            fmt  = "bestaudio/best"
            post = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]
        else:
            fmt  = "bestaudio[ext=m4a]/bestaudio/best"
            post = []
        out_ext = "mp3" if FFMPEG_OK else "m4a"
        fname   = f"snapload_audio.{out_ext}"
    else:
        if FFMPEG_OK:
            fmt  = f"{fmt_id}+bestaudio/bestvideo+bestaudio/best"
            post = [{"key":"FFmpegVideoConvertor","preferedformat":"mp4"}]
        else:
            # Progressive mp4 — already has audio, no merge needed
            fmt  = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
            post = []
        out_ext = "mp4"
        fname   = f"snapload_{label}.mp4"

    ydl_opts = {
        "format":                         fmt,
        "outtmpl":                        out_tmpl,
        "quiet":                          True,
        "no_warnings":                    True,
        "noplaylist":                     True,
        "postprocessors":                 post,
        "merge_output_format":            "mp4" if (not is_audio and FFMPEG_OK) else None,
        "concurrent_fragment_downloads":  4,
        "http_chunk_size":                10 * 1024 * 1024,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return jsonify({"error": f"Download failed: {str(e)[:150]}"}), 500

    # Find the output file
    final_path = None
    for ext in [out_ext, "mp4", "webm", "mkv", "m4a", "mp3"]:
        p = os.path.join(DOWNLOAD_DIR, f"{uid}.{ext}")
        if os.path.exists(p):
            final_path = p
            out_ext    = ext
            break

    if not final_path:
        return jsonify({"error": "File not found after download"}), 500

    record("download", ip=get_ip(), fmt=label, reason=reason or None)
    cleanup(final_path, delay=120)

    mime = "audio/mpeg" if out_ext in ["mp3","m4a"] else "video/mp4"
    return send_file(final_path, as_attachment=True, download_name=fname, mimetype=mime)

# ════════════════════════════════════════════════
#  ADMIN ROUTES
# ════════════════════════════════════════════════
@app.route("/admin/stats")
@admin_required
def admin_stats():
    today     = date.today().isoformat()
    yesterday = date.fromordinal(date.today().toordinal()-1).isoformat()
    month_key = today[:7]

    with lock:
        daily   = stats.get("daily", {})
        monthly = stats.get("monthly", {})
        today_d = daily.get(today, {})
        yest_d  = daily.get(yesterday, {})
        month_d = monthly.get(month_key, {})

        last7 = []
        for i in range(6, -1, -1):
            d   = date.fromordinal(date.today().toordinal()-i).isoformat()
            ent = daily.get(d, {})
            last7.append({
                "date":      d,
                "downloads": ent.get("downloads", 0),
                "fetches":   ent.get("fetches", 0),
                "visitors":  len(ent.get("visitors", [])),
            })

        return jsonify({
            "total_downloads": stats.get("total_downloads", 0),
            "total_fetches":   stats.get("total_fetches", 0),
            "today":     {"downloads": today_d.get("downloads",0), "fetches": today_d.get("fetches",0), "visitors": len(today_d.get("visitors",[]))},
            "yesterday": {"downloads": yest_d.get("downloads",0),  "fetches": yest_d.get("fetches",0),  "visitors": len(yest_d.get("visitors",[]))},
            "this_month":{"downloads": month_d.get("downloads",0), "fetches": month_d.get("fetches",0)},
            "last_7_days": last7,
            "top_reasons": sorted(stats.get("reasons",{}).items(), key=lambda x:-x[1])[:10],
            "top_formats": sorted(stats.get("formats",{}).items(), key=lambda x:-x[1])[:10],
        })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
