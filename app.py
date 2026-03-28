import os, json, re, threading, uuid, subprocess, requests, tempfile
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

jobs = {}  # job_id -> {status, progress, data, filename, error}

COBALT_INSTANCES = [
    "https://cobalt.lunar.icu",
    "https://cobalt.api.lostless.de",
    "https://cobalt.zing.ovh",
    "https://cbl.jame.work",
    "https://cobalt.drgns.space",
    "https://cobalt.imput.net",
    "https://cobalt.privacyredirect.com",
]
COBALT_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
QUALITY_MAP = {"2160p":"2160","1440p":"1440","1080p":"1080","720p":"720",
               "480p":"480","360p":"360","240p":"240","144p":"144"}
AUDIO_MAP = {"mp3":"mp3","m4a":"m4a","opus":"opus","flac":"flac","wav":"wav"}


def cobalt_get_link(url, quality="720", audio_only=False, audio_format="mp3"):
    payload = ({"url": url, "downloadMode": "audio",
                "audioFormat": audio_format, "filenameStyle": "pretty"}
               if audio_only else
               {"url": url, "videoQuality": quality,
                "downloadMode": "auto", "filenameStyle": "pretty"})
    last_err = "Sin respuesta de cobalt."
    for inst in COBALT_INSTANCES:
        try:
            r = requests.post(inst.rstrip('/') + '/',
                              headers=COBALT_HEADERS, json=payload, timeout=20)
            if r.status_code in (200, 201):
                d = r.json()
                st = d.get("status", "")
                if st in ("tunnel", "redirect", "stream"):
                    return d["url"], d.get("filename") or "video.mp4"
                last_err = "cobalt: " + str(d.get("error", {}).get("code", st))
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err)


def download_to_memory(job_id, url, quality, audio_only, audio_format):
    """Download file into RAM, then serve it. Works on Railway ephemeral filesystem."""
    try:
        jobs[job_id]["status"] = "downloading"
        filename = "video.mp4"
        data = None

        # 1) Try cobalt — streams directly from their CDN
        try:
            cobalt_url, filename = cobalt_get_link(
                url, quality=quality, audio_only=audio_only,
                audio_format=audio_format)

            buf = bytearray()
            with requests.get(cobalt_url, stream=True, timeout=180,
                              headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                for chunk in r.iter_content(65536):
                    if chunk:
                        buf.extend(chunk)
                        done += len(chunk)
                        if total:
                            jobs[job_id]["progress"] = round(done/total*100, 1)
            data = bytes(buf)

        except Exception:
            # 2) Fallback: yt-dlp writes to temp file, we read it into memory
            tmp_dir = tempfile.mkdtemp()
            out_tmpl = os.path.join(tmp_dir, "%(title).60s.%(ext)s")
            h = quality

            if audio_only:
                cmd = ["yt-dlp", "--no-playlist",
                       "-f", "bestaudio/best",
                       "-x", "--audio-format", audio_format,
                       "--audio-quality", "0",
                       "--output", out_tmpl, "--newline", url]
            else:
                fmt = (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                       f"/bestvideo[height<={h}]+bestaudio"
                       f"/best[height<={h}][ext=mp4]"
                       f"/best[height<={h}]")
                cmd = ["yt-dlp", "--no-playlist", "-f", fmt,
                       "--merge-output-format", "mp4",
                       "--output", out_tmpl, "--newline", url]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                if "[download]" in line and "%" in line:
                    try:
                        pct = float(line.split("%")[0].split()[-1])
                        jobs[job_id]["progress"] = round(min(pct, 98), 1)
                    except Exception:
                        pass
            proc.wait()

            if proc.returncode != 0:
                raise RuntimeError("No se pudo descargar el video.")

            # Find the output file
            files = os.listdir(tmp_dir)
            if not files:
                raise RuntimeError("yt-dlp no generó archivo.")

            out_file = os.path.join(tmp_dir, files[0])
            filename = files[0]
            with open(out_file, "rb") as f:
                data = f.read()

            # Cleanup temp
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        # Sanitize filename
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename) or "video.mp4"

        jobs[job_id].update({
            "status": "done",
            "progress": 100,
            "data": data,
            "filename": filename,
            "error": None
        })

    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e), "data": None})


def extract_id(url):
    m = re.search(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None

def default_qualities():
    return [{"id":"1080p","label":"1080p Full HD","height":1080},
            {"id":"720p","label":"720p HD","height":720},
            {"id":"480p","label":"480p SD","height":480},
            {"id":"360p","label":"360p Básico","height":360}]

def get_info(url):
    try:
        r = subprocess.run(["yt-dlp","--dump-json","--no-playlist", url],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            info = json.loads(r.stdout)
            LABELS = {2160:"4K Ultra HD",1440:"1440p QHD",1080:"1080p Full HD",
                      720:"720p HD",480:"480p SD",360:"360p Básico",240:"240p",144:"144p"}
            heights = sorted(set(
                f.get("height") for f in info.get("formats", [])
                if f.get("height") and f.get("vcodec","none") != "none"
            ), reverse=True)
            qualities = [{"id":f"{h}p","label":LABELS.get(h,f"{h}p"),"height":h}
                         for h in heights if h >= 144] or default_qualities()
            return {"title": info.get("title","Video"),
                    "duration": info.get("duration_string",""),
                    "uploader": info.get("uploader",""),
                    "thumbnail": info.get("thumbnail",""),
                    "view_count": info.get("view_count",0),
                    "quality_options": qualities}
    except Exception:
        pass
    try:
        nd = requests.get(
            f"https://noembed.com/embed?url={requests.utils.quote(url)}", timeout=10
        ).json()
        vid = extract_id(url)
        return {"title": nd.get("title","Video"), "duration": "",
                "uploader": nd.get("author_name",""),
                "thumbnail": f"https://img.youtube.com/vi/{vid}/mqdefault.jpg" if vid else "",
                "view_count": 0, "quality_options": default_qualities()}
    except Exception as e:
        raise RuntimeError(str(e))


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def root(): return jsonify({"status": "YTDown API running"})

@app.route("/api/health")
def health(): return jsonify({"status": "ok"})

@app.route("/api/info", methods=["POST"])
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url: return jsonify({"error": "URL requerida"}), 400
    try: return jsonify(get_info(url))
    except RuntimeError as e: return jsonify({"error": str(e)}), 400

@app.route("/api/download", methods=["POST"])
def api_download():
    d = request.json or {}
    url = d.get("url", "").strip()
    if not url: return jsonify({"error": "URL requerida"}), 400
    quality = QUALITY_MAP.get(d.get("quality_id", "720p"), "720")
    audio_only = d.get("audio_only", False)
    audio_format = AUDIO_MAP.get(d.get("audio_format", "mp3"), "mp3")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"status":"queued","progress":0,"data":None,"filename":None,"error":None}
    threading.Thread(target=download_to_memory,
                     args=(job_id, url, quality, audio_only, audio_format),
                     daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    j = jobs.get(job_id)
    if not j: return jsonify({"error": "No encontrado"}), 404
    return jsonify({"status": j["status"], "progress": j["progress"],
                    "filename": j["filename"], "error": j["error"]})

@app.route("/api/file/<job_id>")
def api_file(job_id):
    j = jobs.get(job_id)
    if not j or j["status"] != "done" or not j.get("data"):
        return jsonify({"error": "No disponible"}), 404

    data = j["data"]
    filename = j["filename"] or "video.mp4"

    # Detect content type
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
    ct_map = {"mp4":"video/mp4","mp3":"audio/mpeg","m4a":"audio/mp4",
              "webm":"video/webm","opus":"audio/ogg","flac":"audio/flac","wav":"audio/wav"}
    content_type = ct_map.get(ext, "application/octet-stream")

    from flask import make_response
    resp = make_response(data)
    resp.headers["Content-Type"] = content_type
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Content-Length"] = len(data)

    # Free memory after serving
    j["data"] = None

    return resp

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n YTDown API → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port)
