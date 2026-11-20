import os, uuid, shutil, subprocess, tempfile, threading
from pathlib import Path
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles

DG_KEY = os.environ["DEEPGRAM_API_KEY"]
DG_URL = "https://api.deepgram.com/v1/listen"
PARAMS = {"model": "nova-3", "smart_format": "true", "punctuate": "true",
          "utterances": "true", "detect_language": "true"}
WORK = Path(tempfile.gettempdir()) / "scribely"
WORK.mkdir(exist_ok=True)
jobs = {}
app = FastAPI()


def run(job_id, src, url):
    job, d = jobs[job_id], WORK / job_id
    try:
        if url:
            job["status"] = "downloading"
            subprocess.run(["yt-dlp", "-f", "bestaudio", "--no-playlist",
                            "-o", str(d / "src.%(ext)s"), url],
                           check=True, capture_output=True, timeout=3600)
            src = next(d.glob("src.*"))
        # Mono 16 kHz MP3 keeps a 10-hour file near 300 MB and drops video tracks.
        job["status"] = "preparing"
        audio = d / "audio.mp3"
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1",
                        "-ar", "16000", "-b:a", "48k", str(audio)],
                       check=True, capture_output=True)
        job["status"] = "transcribing"
        with open(audio, "rb") as f, httpx.Client(timeout=httpx.Timeout(3600.0)) as c:
            r = c.post(DG_URL, params=PARAMS,
                       headers={"Authorization": f"Token {DG_KEY}", "Content-Type": "audio/mpeg"},
                       content=iter(lambda: f.read(1 << 20), b""))
        r.raise_for_status()
        res = r.json()["results"]
        job["language"] = res["channels"][0].get("detected_language")
        job["segments"] = [{"start": u["start"], "end": u["end"], "text": u["transcript"]}
                           for u in res.get("utterances", [])]
        job["status"] = "done"
    except subprocess.CalledProcessError as e:
        job.update(status="error", error=(e.stderr or b"").decode()[-300:] or "Could not read that media.")
    except Exception as e:
        job.update(status="error", error=str(e)[:300])
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
    jobs[job_id] = {"status": "queued", "name": file.filename if file else url}
    threading.Thread(target=run, args=(job_id, src, None if file else url), daemon=True).start()
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found. It may have been cleared after a restart.")
    return jobs[job_id]


if Path("static").exists():
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
