import os, json, re, threading, uuid, subprocess, requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
jobs = {}

COBALT_INSTANCES = [
    "https://cobalt.lunar.icu",
    "https://cobalt.api.lostless.de",
    "https://cobalt.zing.ovh",
    "https://cbl.jame.work",
    "https://cobalt.drgns.space",
    "https://api.cobalt.tools",
]
COBALT_HEADERS = {"Accept":"application/json","Content-Type":"application/json"}
QUALITY_MAP = {"2160p":"2160","1440p":"1440","1080p":"1080","720p":"720","480p":"480","360p":"360","240p":"240","144p":"144"}
AUDIO_MAP = {"mp3":"mp3","m4a":"m4a","opus":"opus","flac":"flac","wav":"wav"}

def cobalt_get_link(url, quality="1080", audio_only=False, audio_format="mp3"):
    payload = ({"url":url,"downloadMode":"audio","audioFormat":audio_format,"filenameStyle":"pretty"}
               if audio_only else
               {"url":url,"videoQuality":quality,"downloadMode":"auto","filenameStyle":"pretty"})
    last_err = "Ningún servidor respondió."
    for inst in COBALT_INSTANCES:
        try:
            r = requests.post(f"{inst}/", headers=COBALT_HEADERS, json=payload, timeout=15)
            if r.status_code in (200,201):
                d = r.json()
                st = d.get("status","")
                if st in ("tunnel","redirect"):
                    return d["url"], d.get("filename","video.mp4")
                last_err = d.get("error",{}).get("code","error") if st=="error" else f"status:{st}"
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err)

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
        r = subprocess.run(["yt-dlp","--dump-json","--no-playlist",url],
                           capture_output=True, text=True, timeout=25)
        if r.returncode == 0:
            info = json.loads(r.stdout)
            LABELS = {2160:"4K Ultra HD",1440:"1440p QHD",1080:"1080p Full HD",
                      720:"720p HD",480:"480p SD",360:"360p Básico",240:"240p",144:"144p"}
            heights = sorted(set(f.get("height") for f in info.get("formats",[])
                             if f.get("height") and f.get("vcodec","none")!="none"), reverse=True)
            qualities = [{"id":f"{h}p","label":LABELS.get(h,f"{h}p"),"height":h}
                         for h in heights if h>=144] or default_qualities()
            return {"title":info.get("title","Video"),"duration":info.get("duration_string",""),
                    "uploader":info.get("uploader",""),"thumbnail":info.get("thumbnail",""),
                    "view_count":info.get("view_count",0),"quality_options":qualities}
    except Exception:
        pass
    try:
        nd = requests.get(f"https://noembed.com/embed?url={requests.utils.quote(url)}",timeout=10).json()
        vid = extract_id(url)
        return {"title":nd.get("title","Video"),"duration":"","uploader":nd.get("author_name",""),
                "thumbnail":f"https://img.youtube.com/vi/{vid}/mqdefault.jpg" if vid else "",
                "view_count":0,"quality_options":default_qualities()}
    except Exception as e:
        raise RuntimeError(str(e))

def download_worker(job_id, cobalt_url, filename):
    try:
        jobs[job_id]["status"] = "downloading"
        out = os.path.join(DOWNLOAD_DIR, f"{job_id}_{filename}")
        with requests.get(cobalt_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length",0))
            done = 0
            with open(out,"wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk); done += len(chunk)
                        if total: jobs[job_id]["progress"] = round(done/total*100,1)
        jobs[job_id].update({"status":"done","progress":100,"file":out,"filename":filename})
    except Exception as e:
        jobs[job_id].update({"status":"error","error":str(e)})

@app.route("/")
def root(): return jsonify({"status":"YTDown API running"})

@app.route("/api/health")
def health(): return jsonify({"status":"ok"})

@app.route("/api/info", methods=["POST"])
def api_info():
    url = (request.json or {}).get("url","").strip()
    if not url: return jsonify({"error":"URL requerida"}),400
    try: return jsonify(get_info(url))
    except RuntimeError as e: return jsonify({"error":str(e)}),400

@app.route("/api/download", methods=["POST"])
def api_download():
    d = request.json or {}
    url = d.get("url","").strip()
    if not url: return jsonify({"error":"URL requerida"}),400
    try:
        cobalt_url, filename = cobalt_get_link(
            url, quality=QUALITY_MAP.get(d.get("quality_id","1080p"),"1080"),
            audio_only=d.get("audio_only",False),
            audio_format=AUDIO_MAP.get(d.get("audio_format","mp3"),"mp3"))
    except RuntimeError as e: return jsonify({"error":str(e)}),502
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',filename) or "video.mp4"
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"status":"queued","progress":0,"file":None,"filename":None,"error":None}
    threading.Thread(target=download_worker,args=(job_id,cobalt_url,filename),daemon=True).start()
    return jsonify({"job_id":job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    j = jobs.get(job_id)
    if not j: return jsonify({"error":"No encontrado"}),404
    return jsonify({"status":j["status"],"progress":j["progress"],"filename":j["filename"],"error":j["error"]})

@app.route("/api/file/<job_id>")
def api_file(job_id):
    j = jobs.get(job_id)
    if not j or j["status"]!="done" or not j["file"]: return jsonify({"error":"No disponible"}),404
    return send_file(j["file"],as_attachment=True,download_name=j["filename"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print(f"\n YTDown API → http://localhost:{port}\n")
    app.run(host="0.0.0.0",port=port)
