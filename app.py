import os
import uuid
import threading
import time
import shutil
from flask import Flask, request, render_template, send_file, redirect, url_for, flash, abort, jsonify
import yt_dlp

app = Flask(__name__)
app.secret_key = os.urandom(24)
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

download_tasks = {}

def find_ffmpeg():
   return shutil.which("ffmpeg") or (os.path.exists("/usr/bin/ffmpeg") and "/usr/bin/ffmpeg") or None

FFMPEG_PATH = find_ffmpeg()

def get_ydl_opts(out_template, progress_hook=None, audio_only=False, playlist=False):
   opts = {
      "outtmpl": out_template,
      "quiet": True,
      "no_warnings": True,
      "ignoreerrors": True,
      "noplaylist": not playlist,
      "progress_hooks": [progress_hook] if progress_hook else [],
   }
   if audio_only:
      opts["format"] = "bestaudio/best"
      opts["postprocessors"] = [{
         "key": "FFmpegExtractAudio",
         "preferredcodec": "mp3",
         "preferredquality": "192",
      }]
   else:
      opts["format"] = "bestvideo+bestaudio/best"
      opts["merge_output_format"] = "mp4"
   if FFMPEG_PATH:
      opts["ffmpeg_location"] = FFMPEG_PATH
   return opts

def download_task(task_id, url, quality, audio_only, is_playlist):
   # Update existing entry (already created in download_async)
   download_tasks[task_id].update({
      "progress": 0,
      "status": "starting",
      "files": [],
      "error": None,
   })

   def progress_hook(d):
      if d["status"] == "downloading":
         total = d.get("total_bytes") or d.get("total_bytes_estimate")
         if total:
               percent = d["downloaded_bytes"] / total * 100
               download_tasks[task_id]["progress"] = round(percent, 1)
         download_tasks[task_id]["status"] = "downloading"
      elif d["status"] == "finished":
         download_tasks[task_id]["progress"] = 100
         download_tasks[task_id]["status"] = "processing"

   try:
      if is_playlist:
         unique_prefix = uuid.uuid4().hex[:8]
         out_template = os.path.join(DOWNLOAD_FOLDER, f"{unique_prefix}-%(playlist_index)s-%(title)s.%(ext)s")
         ydl_opts = get_ydl_opts(out_template, progress_hook, audio_only, playlist=True)
         with yt_dlp.YoutubeDL(ydl_opts) as ydl:
               ydl.download([url])
         files = [f for f in os.listdir(DOWNLOAD_FOLDER) if f.startswith(unique_prefix) and os.path.isfile(os.path.join(DOWNLOAD_FOLDER, f))]
         if not files:
               raise Exception("No files downloaded from playlist")
         download_tasks[task_id]["files"] = files
         download_tasks[task_id]["status"] = "complete"
      else:
         unique_id = uuid.uuid4().hex[:8]
         out_template = os.path.join(DOWNLOAD_FOLDER, f"%(title)s-{unique_id}.%(ext)s")
         ydl_opts = get_ydl_opts(out_template, progress_hook, audio_only, playlist=False)
         with yt_dlp.YoutubeDL(ydl_opts) as ydl:
               info = ydl.extract_info(url, download=True)
               file_path = ydl.prepare_filename(info)
               if audio_only:
                  file_path = os.path.splitext(file_path)[0] + ".mp3"
               if not os.path.exists(file_path):
                  for f in os.listdir(DOWNLOAD_FOLDER):
                     if unique_id in f:
                           file_path = os.path.join(DOWNLOAD_FOLDER, f)
                           break
               download_tasks[task_id]["files"] = [os.path.basename(file_path)]
               download_tasks[task_id]["status"] = "complete"
   except Exception as e:
      download_tasks[task_id]["error"] = str(e)
      download_tasks[task_id]["status"] = "error"

@app.route("/")
def index():
   return render_template("index.html", ffmpeg_ok=FFMPEG_PATH is not None)

@app.route("/download_async", methods=["POST"])
def download_async():
   url = request.form.get("url", "").strip()
   quality = request.form.get("quality", "best")
   audio_only = request.form.get("audio_only") == "yes"
   is_playlist = request.form.get("playlist") == "yes"

   if not url:
      return jsonify({"error": "URL required"}), 400
   if not FFMPEG_PATH:
      return jsonify({"error": "ffmpeg not found. Please install: sudo apt install ffmpeg -y"}), 400

   task_id = uuid.uuid4().hex
   # Register immediately with "queued" status
   download_tasks[task_id] = {
      "progress": 0,
      "status": "queued",
      "files": [],
      "error": None,
      "ts": time.time()
   }
   thread = threading.Thread(target=download_task, args=(task_id, url, quality, audio_only, is_playlist))
   thread.daemon = True
   thread.start()
   return jsonify({"task_id": task_id})

@app.route("/progress/<task_id>")
def progress(task_id):
   if task_id not in download_tasks:
      return jsonify({"error": "Unknown task"}), 404
   return jsonify(download_tasks[task_id])

@app.route("/loading/<task_id>")
def loading(task_id):
   return render_template("loading.html", task_id=task_id)

@app.route("/result/<task_id>")
def result(task_id):
   if task_id not in download_tasks:
      flash("Task expired", "error")
      return redirect(url_for("index"))
   task = download_tasks[task_id]
   if task["status"] == "error":
      flash(f"Error: {task['error']}", "error")
      return redirect(url_for("index"))
   if len(task["files"]) == 1:
      return send_file(os.path.join(DOWNLOAD_FOLDER, task["files"][0]), as_attachment=True)
   else:
      flash(f"Playlist downloaded ({len(task['files'])} files)", "success")
      return redirect(url_for("list_files"))

@app.route("/files")
def list_files():
   files = []
   for fname in os.listdir(DOWNLOAD_FOLDER):
      fpath = os.path.join(DOWNLOAD_FOLDER, fname)
      if os.path.isfile(fpath):
         size_mb = os.path.getsize(fpath) / (1024*1024)
         files.append({"name": fname, "size": f"{size_mb:.1f} MB", "time": time.ctime(os.path.getmtime(fpath))})
   files.sort(key=lambda x: x["time"], reverse=True)
   return render_template("files.html", files=files)

@app.route("/download_existing/<path:filename>")
def download_existing(filename):
   safe_path = os.path.join(DOWNLOAD_FOLDER, filename)
   if not os.path.isfile(safe_path) or not os.path.realpath(safe_path).startswith(os.path.realpath(DOWNLOAD_FOLDER)):
      abort(404)
   return send_file(safe_path, as_attachment=True)

def cleanup_old_files():
   while True:
      now = time.time()
      for fname in os.listdir(DOWNLOAD_FOLDER):
         fpath = os.path.join(DOWNLOAD_FOLDER, fname)
         if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 3600:
               try: os.remove(fpath)
               except: pass
      for tid in list(download_tasks.keys()):
         if download_tasks[tid].get("status") in ("complete", "error"):
               if time.time() - download_tasks[tid].get("ts", 0) > 300:
                  del download_tasks[tid]
      time.sleep(300)

threading.Thread(target=cleanup_old_files, daemon=True).start()

if __name__ == "__main__":
   app.run(host="0.0.0.0", port=5000, debug=True)