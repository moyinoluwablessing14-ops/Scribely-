import os, re, uuid, shutil, subprocess, tempfile, threading, time
from pathlib import Path
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

DG_KEY = os.environ["DEEPGRAM_API_KEY"]
DG_URL = "https://api.deepgram.com/v1/listen"
PARAMS = {"model": "nova-3", "smart_format": "true", "punctuate": "true",
          "utterances": "true", "detect_language": "true"}
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL")
ACTIVE = {"queued", "downloading", "preparing", "transcribing"}
WORK = Path(tempfile.gettempdir()) / "scribely"
WORK.mkdir(exist_ok=True)
jobs = {}
app = FastAPI()


def keepalive():
    while True:
        time.sleep(240)
        if SELF_URL and any(j["status"] in ACTIVE for j in list(jobs.values())):
            try:
                httpx.get(SELF_URL + "/health", timeout=20)
            except Exception:
                pass


threading.Thread(target=keepalive, daemon=True).start()


def set_status(job_id, status):
    jobs[job_id]["status"] = status
    print(f"[{job_id}] {status}", flush=True)


def download(job, d, url):
    p = subprocess.Popen(
        ["yt-dlp", "-f", "bestaudio/worst", "-N", "8", "--no-playlist", "--newline",
         "-o", str(d / "src.%(ext)s"), url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in p.stdout:
        tail = (tail + [line.strip()])[-6:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            job["progress"] = float(m.group(1))
    if p.wait() != 0:
        raise RuntimeError(" | ".join(tail)[-300:] or "Download failed.")
    return max(d.glob("src.*"), key=lambda f: f.stat().st_size)


def run(job_id, src, url):
    job, d = jobs[job_id], WORK / job_id
    try:
        if url:
            set_status(job_id, "downloading")
            src = download(job, d, url)
        set_status(job_id, "preparing")
        audio = d / "audio.mp3"
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1",
                        "-ar", "16000", "-b:a", "48k", str(audio)],
                       check=True, capture_output=True, timeout=7200)
        set_status(job_id, "transcribing")
        with open(audio, "rb") as f, httpx.Client(timeout=httpx.Timeout(7200.0)) as c:
            r = c.post(DG_URL, params=PARAMS,
                       headers={"Authorization": f"Token {DG_KEY}", "Content-Type": "audio/mpeg"},
                       content=iter(lambda: f.read(1 << 20), b""))
        r.raise_for_status()
        res = r.json()["results"]
        job["language"] = res["channels"][0].get("detected_language")
        job["segments"] = [{"start": u["start"], "end": u["end"], "text": u["transcript"]}
                           for u in res.get("utterances", [])]
        set_status(job_id, "done")
    except subprocess.CalledProcessError as e:
        job.update(status="error", error=(e.stderr or b"").decode()[-300:] or "Could not read that media.")
        print(f"[{job_id}] ERROR {job['error']}", flush=True)
    except Exception as e:
        job.update(status="error", error=str(e)[:300])
        print(f"[{job_id}] ERROR {job['error']}", flush=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@app.post("/api/jobs")
async def create_job(file: UploadFile | None = File(None), url: str | None = Form(None)):
    if not file and not url:
        raise HTTPException(400, "Send a file or a link.")
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "The link must start with https://")
    job_id = uuid.uuid4().hex[:12]
    d = WORK / job_id
    d.mkdir()
    src = None
    if file:
        src = d / "upload"
        with open(src, "wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
    jobs[job_id] = {"status": "queued", "name": file.filename if file else url, "progress": 0}
    threading.Thread(target=run, args=(job_id, src, None if file else url), daemon=True).start()
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found. The server may have restarted.")
    return jobs[job_id]


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def home():
    if Path("index.html").exists():
        return FileResponse("index.html")
    return {"ok": True}
