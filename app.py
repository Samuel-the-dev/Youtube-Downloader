import os
import uuid
import threading
import time
import json
import zipfile
from flask import Flask, request, render_template, send_file, redirect, url_for, flash, abort, jsonify
import yt_dlp

app = Flask(__name__)
app.secret_key = os.urandom(24)
DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "downloads")
TASKS_FOLDER = os.path.join(DOWNLOAD_FOLDER, ".tasks")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(TASKS_FOLDER, exist_ok=True)

# --------------------------------------------------------------------------- #
#  Task state helpers
# --------------------------------------------------------------------------- #
def save_task(task_id, data):
   data["updated"] = time.time()
   with open(os.path.join(TASKS_FOLDER, f"{task_id}.json"), "w") as f:
      json.dump(data, f)

def load_task(task_id):
   path = os.path.join(TASKS_FOLDER, f"{task_id}.json")
   if not os.path.exists(path):
      return None
   with open(path, "r") as f:
      return json.load(f)

def delete_task(task_id):
   try:
      os.remove(os.path.join(TASKS_FOLDER, f"{task_id}.json"))
   except:
      pass

# --------------------------------------------------------------------------- #
#  ffmpeg detection (uses shutil.which and a common fallback)
# --------------------------------------------------------------------------- #
import shutil
FFMPEG_PATH = shutil.which("ffmpeg") or (
   os.path.exists("/usr/bin/ffmpeg") and "/usr/bin/ffmpeg"
)

# --------------------------------------------------------------------------- #
#  yt-dlp helper – maps quality string to a format selector
# --------------------------------------------------------------------------- #
def get_format_for_quality(quality: str) -> str:
   if quality == "best":
      return "bestvideo+bestaudio/best"
   height_map = {
      "2160p": 2160,
      "1440p": 1440,
      "1080p": 1080,
      "720p": 720,
      "480p": 480,
      "360p": 360,
   }
   max_height = height_map.get(quality, 720)
   return f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"

def get_ydl_opts(out_template, progress_hook=None, audio_only=False, playlist=False, quality="best"):
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
      opts["format"] = get_format_for_quality(quality)
      opts["merge_output_format"] = "mp4"
   if FFMPEG_PATH:
      opts["ffmpeg_location"] = FFMPEG_PATH
   return opts

# --------------------------------------------------------------------------- #
#  Background download task
# --------------------------------------------------------------------------- #
def download_task(task_id, url, quality, audio_only, is_playlist):
   save_task(task_id, {"progress": 0, "status": "starting", "error": None, "output_file": None})

   def progress_hook(d):
      if d["status"] == "downloading":
         total = d.get("total_bytes") or d.get("total_bytes_estimate")
         if total:
               percent = min(100, d["downloaded_bytes"] / total * 100)
               save_task(task_id, {
                  "progress": round(percent, 1),
                  "status": "downloading",
                  "error": None,
                  "output_file": None,
               })
      elif d["status"] == "finished":
         save_task(task_id, {"progress": 100, "status": "processing", "error": None, "output_file": None})

   try:
      if is_playlist:
         prefix = uuid.uuid4().hex[:8]
         out_tmpl = os.path.join(DOWNLOAD_FOLDER, f"{prefix}-%(playlist_index)s-%(title)s.%(ext)s")
         ydl_opts = get_ydl_opts(out_tmpl, progress_hook, audio_only, playlist=True, quality=quality)
         with yt_dlp.YoutubeDL(ydl_opts) as ydl:
               ydl.download([url])

         # Collect all files with the current prefix
         all_files = []
         for f in os.listdir(DOWNLOAD_FOLDER):
               if f.startswith(prefix) and os.path.isfile(os.path.join(DOWNLOAD_FOLDER, f)):
                  all_files.append(f)

         if not all_files:
               raise Exception("No files downloaded – maybe the URL is invalid or the playlist is empty.")

         # Create ZIP archive
         zip_name = f"playlist_{prefix}.zip"
         zip_path = os.path.join(DOWNLOAD_FOLDER, zip_name)
         with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
               for idx, fname in enumerate(sorted(all_files), 1):
                  rest = fname[len(prefix)+1:]               # PLAYINDEX-TITLE.EXT
                  title_part = rest.split("-", 1)[1]         # TITLE.EXT
                  title_part = os.path.splitext(title_part)[0]
                  ext = os.path.splitext(fname)[1]
                  arcname = f"{idx:02d} - {title_part}{ext}"
                  zf.write(os.path.join(DOWNLOAD_FOLDER, fname), arcname)

         # Remove individual files
         for fname in all_files:
               os.remove(os.path.join(DOWNLOAD_FOLDER, fname))

         save_task(task_id, {"progress": 100, "status": "complete", "error": None, "output_file": zip_name})
      else:
         # Single video
         unique = uuid.uuid4().hex[:8]
         out_tmpl = os.path.join(DOWNLOAD_FOLDER, f"%(title)s-{unique}.%(ext)s")
         ydl_opts = get_ydl_opts(out_tmpl, progress_hook, audio_only, playlist=False, quality=quality)
         with yt_dlp.YoutubeDL(ydl_opts) as ydl:
               info = ydl.extract_info(url, download=True)
               file_path = ydl.prepare_filename(info)
               if audio_only:
                  file_path = os.path.splitext(file_path)[0] + ".mp3"
               if not os.path.exists(file_path):
                  for f in os.listdir(DOWNLOAD_FOLDER):
                     if unique in f:
                           file_path = os.path.join(DOWNLOAD_FOLDER, f)
                           break
               if not os.path.exists(file_path):
                  raise Exception("Downloaded file not found on disk.")
               save_task(task_id, {
                  "progress": 100,
                  "status": "complete",
                  "error": None,
                  "output_file": os.path.basename(file_path),
               })
   except Exception as e:
      save_task(task_id, {"progress": 0, "status": "error", "error": str(e), "output_file": None})

# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
   return render_template("index.html")

@app.route("/download_async", methods=["POST"])
def download_async():
   url = request.form.get("url", "")
   quality = request.form.get("quality", "best")
   audio_only = request.form.get("audio_only") == "yes"
   is_playlist = request.form.get("playlist") == "yes"

   if not url:
      return jsonify({"error": "URL required"}), 400
   if not FFMPEG_PATH:
      return jsonify({"error": "ffmpeg not installed. Run: sudo apt install ffmpeg -y"}), 400

   task_id = uuid.uuid4().hex
   save_task(task_id, {"progress": 0, "status": "queued", "error": None, "output_file": None})
   threading.Thread(
      target=download_task,
      args=(task_id, url, quality, audio_only, is_playlist),
      daemon=True,
   ).start()
   return jsonify({"task_id": task_id})

@app.route("/progress/<task_id>")
def progress(task_id):
   data = load_task(task_id)
   if not data:
      return jsonify({"error": "Unknown task"}), 404
   return jsonify({k: v for k, v in data.items() if k != "output_file"})

@app.route("/loading/<task_id>")
def loading(task_id):
   return render_template("loading.html", task_id=task_id)

@app.route("/result/<task_id>")
def result(task_id):
   data = load_task(task_id)
   if not data:
      flash("Task expired", "error")
      return redirect(url_for("index"))
   if data["status"] == "error":
      flash(f"Error: {data['error']}", "error")
      return redirect(url_for("index"))
   if data["status"] != "complete":
      flash("Not ready yet", "error")
      return redirect(url_for("index"))

   file_path = os.path.join(DOWNLOAD_FOLDER, data["output_file"])
   if not os.path.exists(file_path):
      flash("File expired", "error")
      return redirect(url_for("index"))

   delete_task(task_id)
   download_name = data["output_file"]
   if download_name.endswith(".zip"):
      download_name = "playlist.zip"
   return send_file(file_path, as_attachment=True, download_name=download_name)

@app.route("/files")
def list_files():
   files = []
   for f in os.listdir(DOWNLOAD_FOLDER):
      if f == ".tasks":
         continue
      p = os.path.join(DOWNLOAD_FOLDER, f)
      if os.path.isfile(p):
         files.append({
               "name": f,
               "size": f"{os.path.getsize(p) / (1024 * 1024):.1f} MB",
               "time": time.ctime(os.path.getmtime(p)),
         })
   return render_template("files.html", files=sorted(files, key=lambda x: x["time"], reverse=True))

@app.route("/download_existing/<path:filename>")
def download_existing(filename):
   """Direct download of a file already on the server (used from /files page)."""
   path = os.path.join(DOWNLOAD_FOLDER, filename)
   if not os.path.exists(path):
      flash("File not found", "error")
      return redirect(url_for("list_files"))
   return send_file(path, as_attachment=True)

# --------------------------------------------------------------------------- #
#  Background cleanup thread
# --------------------------------------------------------------------------- #
def cleanup():
   while True:
      now = time.time()
      # Delete files older than 1 hour
      for f in os.listdir(DOWNLOAD_FOLDER):
         if f == ".tasks":
               continue
         p = os.path.join(DOWNLOAD_FOLDER, f)
         if os.path.isfile(p) and now - os.path.getmtime(p) > 3600:
               try:
                  os.remove(p)
               except:
                  pass
      # Delete old task states (completed/error) after 10 minutes
      for t in os.listdir(TASKS_FOLDER):
         p = os.path.join(TASKS_FOLDER, t)
         try:
               with open(p) as f:
                  d = json.load(f)
               if d.get("status") in ("complete", "error") and now - d.get("updated", 0) > 600:
                  os.remove(p)
         except:
               pass
      time.sleep(300)

threading.Thread(target=cleanup, daemon=True).start()

if __name__ == "__main__":
   app.run(host="0.0.0.0", port=5000, debug=False)