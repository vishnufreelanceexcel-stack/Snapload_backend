"""
SnapLoad Backend — Flask + yt-dlp + FFmpeg
Handles: download proxy, audio/video merge, analytics, admin dashboard
Deploy free on: Render.com or Railway.app
"""

import os, uuid, time, json, threading, hashlib
from datetime import datetime, date
from functools import wraps
from flask import Flask, request, jsonify, Response, send_file, stream_with_context, after_this_request
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app, origins=["*"])   # allow your Netlify frontend

# ── Config ──────────────────────────────────────────────
ADMIN_SECRET   = os.environ.get("ADMIN_SECRET", "snapload_admin_2026")
DOWNLOAD_DIR   = "/tmp/snapload_downloads"
ANALYTICS_FILE = "/tmp/snapload_analytics.json"
MAX_FILESIZE_MB = 500   # refuse files bigger than this
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ── Analytics store (in-memory + file persistence) ──────
analytics_lock = threading.Lock()

def load_analytics():
    try:
        with open(ANALYTICS_FILE) as f:
            return json.load(f)
    except:
        return {"total_downloads": 0, "total_fetches": 0, "daily": {}, "monthly": {}, "visitors": {}, "reasons": {}, "formats": {}}

def save_analytics(data):
    try:
        with open(ANALYTICS_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

analytics = load_analytics()

def record_event(event_type, extra=None):
    today     = date.today().isoformat()
    month_key = today[:7]   # YYYY-MM
    hour      = datetime.now().hour

    with analytics_lock:
        if event_type == "fetch":
            analytics["total_fetches"] = analytics.get("total_fetches", 0) + 1
        elif event_type == "download":
            analytics["total_downloads"] = analytics.get("total_downloads", 0) + 1

        # Daily
        d = analytics.setdefault("daily", {}).setdefault(today, {"fetches":0,"downloads":0,"visitors":set()})
        if isinstance(d.get("visitors"), list):
            d["visitors"] = set(d["visitors"])
        if event_type == "fetch":    d["fetches"] += 1
        if event_type == "download": d["downloads"] += 1
        if extra and extra.get("ip"): d["visitors"].add(extra["ip"])
        d["visitors"] = list(d["visitors"])

        # Monthly
        m = analytics.setdefault("monthly", {}).setdefault(month_key, {"fetches":0,"downloads":0})
        if event_type == "fetch":    m["fetches"] += 1
        if event_type == "download": m["downloads"] += 1

        # Format tracking
        if extra and extra.get("format"):
            analytics.setdefault("formats", {})[extra["format"]] = analytics["formats"].get(extra["format"], 0) + 1

        # Reason tracking
        if extra and extra.get("reason"):
            analytics.setdefault("reasons", {})[extra["reason"]] = analytics["reasons"].get(extra["reason"], 0) + 1

        save_analytics(analytics)

def visitor_fingerprint(req):
    ip  = req.headers.get("X-Forwarded-For", req.remote_addr or "unknown").split(",")[0].strip()
    ua  = req.headers.get("User-Agent", "")
    raw = f"{ip}:{ua}"
    return hashlib.md5(raw.encode()).hexdigest()[:12], ip

# ── Admin auth decorator ─────────────────────────────────
def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = request.headers.get("X-Admin-Secret") or request.args.get("secret")
        if secret != ADMIN_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Cleanup old files ────────────────────────────────────
def cleanup_file(path, delay=120):
    """Delete temp file after delay seconds"""
    def _delete():
        time.sleep(delay)
        try: os.remove(path)
        except: pass
    threading.Thread(target=_delete, daemon=True).start()

# ════════════════════════════════════════════════════════
#  ROUTE: GET /info  — fetch video metadata
# ════════════════════════════════════════════════════════
@app.route("/info")
def get_info():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    fp, ip = visitor_fingerprint(request)
    record_event("fetch", {"ip": ip})

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg:
            return jsonify({"error": "This video is private and cannot be downloaded."}), 400
        if "age" in msg.lower():
            return jsonify({"error": "This video is age-restricted."}), 400
        return jsonify({"error": f"Could not fetch video: {msg[:120]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 500

    # Build format list
    formats      = info.get("formats", [])
    video_fmts   = []
    audio_fmts   = []

    QUALITY_ORDER = {"1080p":1,"720p":2,"480p":3,"360p":4,"240p":5,"144p":6}

    for f in formats:
        vcodec = f.get("vcodec","")
        acodec = f.get("acodec","")
        height = f.get("height")
        fsize  = f.get("filesize") or f.get("filesize_approx")
        ext    = f.get("ext","mp4")
        fid    = f.get("format_id","")
        abr    = f.get("abr")

        if vcodec != "none" and height:
            label = f"{height}p"
            video_fmts.append({
                "format_id": fid,
                "label":     label,
                "height":    height,
                "ext":       ext,
                "size":      fsize,
                "has_audio": acodec != "none",
                "vcodec":    vcodec,
                "acodec":    acodec,
            })
        elif vcodec == "none" and acodec != "none" and abr:
            audio_fmts.append({
                "format_id": fid,
                "label":     f"MP3 {int(abr)}kbps" if abr else "MP3",
                "abr":       abr,
                "ext":       ext,
                "size":      fsize,
            })

    # Deduplicate video by resolution, prefer combined (has audio)
    seen_heights = {}
    for f in sorted(video_fmts, key=lambda x: (x["height"], x["has_audio"]), reverse=True):
        h = f["height"]
        if h not in seen_heights:
            seen_heights[h] = f

    clean_video = sorted(seen_heights.values(), key=lambda x: QUALITY_ORDER.get(f"{x['height']}p", 99))

    # Best audio only (max 2 options)
    seen_abr = set()
    clean_audio = []
    for f in sorted(audio_fmts, key=lambda x: x.get("abr",0), reverse=True):
        abr_key = f.get("abr",0)
        if abr_key not in seen_abr:
            seen_abr.add(abr_key)
            clean_audio.append(f)
        if len(clean_audio) >= 2: break

    # Thumbnails
    thumbs = info.get("thumbnails", [])
    thumb  = thumbs[-1]["url"] if thumbs else info.get("thumbnail","")

    return jsonify({
        "id":       info.get("id",""),
        "title":    info.get("title","YouTube Video"),
        "channel":  info.get("channel") or info.get("uploader","YouTube"),
        "duration": info.get("duration",0),
        "thumb":    thumb,
        "video_formats": [
            {
                "format_id": f["format_id"],
                "label":     f["label"],
                "size":      f["size"],
                "has_audio": f["has_audio"],
                "ext":       "mp4",
            } for f in clean_video
        ],
        "audio_formats": [
            {
                "format_id": f["format_id"],
                "label":     f["label"],
                "size":      f["size"],
                "ext":       "mp3",
            } for f in clean_audio
        ],
    })

# ════════════════════════════════════════════════════════
#  ROUTE: GET /download  — stream file to browser
# ════════════════════════════════════════════════════════
@app.route("/download")
def download_video():
    url       = request.args.get("url","").strip()
    fmt_id    = request.args.get("format_id","bestvideo+bestaudio/best")
    is_audio  = request.args.get("audio","0") == "1"
    reason    = request.args.get("reason","")
    label     = request.args.get("label","video")
    fp, ip    = visitor_fingerprint(request)

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    uid      = uuid.uuid4().hex
    out_path = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")

    # Detect if ffmpeg is available
    import shutil
    ffmpeg_ok = shutil.which("ffmpeg") is not None

    if is_audio:
        fmt     = "bestaudio/best"
        post    = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}] if ffmpeg_ok else []
        out_ext = "mp3" if ffmpeg_ok else "m4a"
        fname   = "snapload_audio.mp3" if ffmpeg_ok else "snapload_audio.m4a"
    else:
        if ffmpeg_ok:
            fmt  = f"{fmt_id}+bestaudio/bestvideo+bestaudio/best"
            post = [{"key":"FFmpegVideoConvertor","preferedformat":"mp4"}]
        else:
            fmt  = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            post = []
        out_ext = "mp4"
        fname   = f"snapload_{label}.mp4"

    ydl_opts = {
        "format":            fmt,
        "outtmpl":           out_path,
        "quiet":             True,
        "no_warnings":       True,
        "noplaylist":        True,
        "postprocessors":    post,
        "merge_output_format": "mp4" if (not is_audio and ffmpeg_ok) else None,
        "concurrent_fragment_downloads": 4,
        "http_chunk_size":   10 * 1024 * 1024,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return jsonify({"error": f"Download failed: {str(e)[:120]}"}), 500

    # Find the output file
    final_path = None
    for ext in [out_ext, "mp4", "mkv", "webm", "mp3", "m4a"]:
        p = os.path.join(DOWNLOAD_DIR, f"{uid}.{ext}")
        if os.path.exists(p):
            final_path = p
            out_ext    = ext
            break

    if not final_path:
        return jsonify({"error": "File not found after download"}), 500

    # Check size
    size_mb = os.path.getsize(final_path) / 1048576
    if size_mb > MAX_FILESIZE_MB:
        os.remove(final_path)
        return jsonify({"error": f"File too large ({size_mb:.0f} MB). Max is {MAX_FILESIZE_MB} MB."}), 400

    # Record analytics
    record_event("download", {"ip": ip, "format": label, "reason": reason if reason else None})

    # Schedule cleanup
    cleanup_file(final_path, delay=90)

    mime = "audio/mpeg" if out_ext == "mp3" else "video/mp4"
    return send_file(
        final_path,
        as_attachment=True,
        download_name=fname,
        mimetype=mime,
    )

# ════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ════════════════════════════════════════════════════════
@app.route("/admin/stats")
@require_admin
def admin_stats():
    today      = date.today().isoformat()
    month_key  = today[:7]
    yesterday  = (date.today().__class__.fromordinal(date.today().toordinal()-1)).isoformat()

    with analytics_lock:
        daily   = analytics.get("daily", {})
        monthly = analytics.get("monthly", {})
        today_d = daily.get(today, {})
        yest_d  = daily.get(yesterday, {})
        month_d = monthly.get(month_key, {})

        # Last 7 days
        last7 = []
        for i in range(6,-1,-1):
            d = date.fromordinal(date.today().toordinal()-i).isoformat()
            entry = daily.get(d,{})
            last7.append({
                "date":      d,
                "downloads": entry.get("downloads",0),
                "fetches":   entry.get("fetches",0),
                "visitors":  len(entry.get("visitors",[])),
            })

        return jsonify({
            "total_downloads": analytics.get("total_downloads", 0),
            "total_fetches":   analytics.get("total_fetches", 0),
            "today": {
                "downloads": today_d.get("downloads", 0),
                "fetches":   today_d.get("fetches", 0),
                "visitors":  len(today_d.get("visitors", [])),
            },
            "yesterday": {
                "downloads": yest_d.get("downloads", 0),
                "fetches":   yest_d.get("fetches", 0),
                "visitors":  len(yest_d.get("visitors", [])),
            },
            "this_month": {
                "downloads": month_d.get("downloads", 0),
                "fetches":   month_d.get("fetches", 0),
            },
            "last_7_days": last7,
            "top_reasons": sorted(analytics.get("reasons",{}).items(), key=lambda x:-x[1])[:10],
            "top_formats": sorted(analytics.get("formats",{}).items(), key=lambda x:-x[1])[:10],
        })

@app.route("/admin/ping")
@require_admin
def admin_ping():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
