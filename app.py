import os
import uuid
import threading
import time
from flask import Flask, request, render_template, send_file, redirect, url_for, flash, abort
import yt_dlp

app = Flask(__name__)
app.secret_key = os.urandom(24)

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Cleanup old files after 1 hour (playlist downloads might take a while)
def cleanup_old_files():
   while True:
      now = time.time()
      for fname in os.listdir(DOWNLOAD_FOLDER):
         fpath = os.path.join(DOWNLOAD_FOLDER, fname)
         if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > 3600:  # 1 hour
               try:
                  os.remove(fpath)
               except Exception:
                  pass
      time.sleep(300)   # check every 5 minutes

threading.Thread(target=cleanup_old_files, daemon=True).start()

def get_quality_format(quality, audio_only=False):
   """Map quality string to yt-dlp format selector."""
   if audio_only:
      # Audio only: best audio, convert to mp3 later, but format should be bestaudio
      return "bestaudio/best"

   quality_map = {
      "best": "best[ext=mp4]/best",
      "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
      "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
      "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
      "360p": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
   }
   return quality_map.get(quality, "best[ext=mp4]/best")

def download_single_video(url, quality, audio_only):
   """Download a single video, return the file path."""
   unique_id = uuid.uuid4().hex[:8]
   out_template = os.path.join(DOWNLOAD_FOLDER, f"%(title)s-{unique_id}.%(ext)s")
   ydl_opts = {
      "format": get_quality_format(quality, audio_only),
      "outtmpl": out_template,
      "quiet": True,
      "no_warnings": True,
      "merge_output_format": "mp4" if not audio_only else None,
      "noplaylist": True,
   }
   if audio_only:
      ydl_opts["postprocessors"] = [{
         "key": "FFmpegExtractAudio",
         "preferredcodec": "mp3",
         "preferredquality": "192",
      }]

   with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      info = ydl.extract_info(url, download=True)
      file_path = ydl.prepare_filename(info)
      # After post-processing, extension might have changed
      if audio_only:
         file_path = os.path.splitext(file_path)[0] + ".mp3"
      # Verify file actually exists
      if not os.path.exists(file_path):
         # maybe extension was kept as original, search for unique_id
         for f in os.listdir(DOWNLOAD_FOLDER):
               if unique_id in f:
                  file_path = os.path.join(DOWNLOAD_FOLDER, f)
                  break
         else:
               raise Exception("Downloaded file not found")
   return file_path

def download_playlist(url, quality, audio_only):
   """Download all videos in a playlist, return list of file paths."""
   unique_prefix = uuid.uuid4().hex[:8]
   out_template = os.path.join(DOWNLOAD_FOLDER, f"{unique_prefix}-%(playlist_index)s-%(title)s.%(ext)s")
   ydl_opts = {
      "format": get_quality_format(quality, audio_only),
      "outtmpl": out_template,
      "quiet": True,
      "no_warnings": True,
      "merge_output_format": "mp4" if not audio_only else None,
      "noplaylist": False,   # allow playlist
      "extract_flat": False,
   }
   if audio_only:
      ydl_opts["postprocessors"] = [{
         "key": "FFmpegExtractAudio",
         "preferredcodec": "mp3",
         "preferredquality": "192",
      }]

   with yt_dlp.YoutubeDL(ydl_opts) as ydl:
      ydl.download([url])

   # Find all files with the unique prefix
   downloaded = []
   for fname in os.listdir(DOWNLOAD_FOLDER):
      if fname.startswith(unique_prefix) and os.path.isfile(os.path.join(DOWNLOAD_FOLDER, fname)):
         downloaded.append(fname)
   if not downloaded:
      raise Exception("No files were downloaded from playlist")
   return downloaded

@app.route("/", methods=["GET"])
def index():
   return render_template("index.html")

@app.route("/download", methods=["POST"])
def download():
   url = request.form.get("url", "").strip()
   quality = request.form.get("quality", "best")
   audio_only = request.form.get("audio_only") == "yes"
   playlist = request.form.get("playlist") == "yes"

   if not url:
      flash("Please provide a YouTube URL.", "error")
      return redirect(url_for("index"))

   # If playlist checkbox is not explicitly set, we could auto-detect
   # but for safety, rely on the checkbox. We'll still try to detect if "list=" in url
   # but let the checkbox force the decision.
   if not playlist and ("list=" not in url):
      playlist = False
   # If the user didn't tick the playlist box but there is a list=, we still treat as single
   # because user explicitly didn't check it. That's the safe assumption.

   try:
      if playlist:
         # Download entire playlist
         downloaded_files = download_playlist(url, quality, audio_only)
         flash(f"Playlist downloaded ({len(downloaded_files)} videos). See below.", "success")
         return redirect(url_for("list_files"))
      else:
         # Single video download
         file_path = download_single_video(url, quality, audio_only)
         return send_file(file_path, as_attachment=True)
   except Exception as e:
      flash(f"Error: {str(e)}", "error")
      return redirect(url_for("index"))

@app.route("/files")
def list_files():
   files = []
   for fname in os.listdir(DOWNLOAD_FOLDER):
      fpath = os.path.join(DOWNLOAD_FOLDER, fname)
      if os.path.isfile(fpath):
         try:
               size_mb = os.path.getsize(fpath) / (1024 * 1024)
               files.append({
                  "name": fname,
                  "size": f"{size_mb:.1f} MB",
                  "time": time.ctime(os.path.getmtime(fpath))
               })
         except Exception:
               pass
   # Sort by modification time, newest first
   files.sort(key=lambda x: x["time"], reverse=True)
   return render_template("files.html", files=files)

@app.route("/download_existing/<path:filename>")
def download_existing(filename):
   # Prevent directory traversal
   safe_path = os.path.join(DOWNLOAD_FOLDER, filename)
   # normalize path
   abs_safe_path = os.path.abspath(safe_path)
   abs_downloads = os.path.abspath(DOWNLOAD_FOLDER)
   if not abs_safe_path.startswith(abs_downloads):
      abort(403)
   if not os.path.isfile(abs_safe_path):
      abort(404)
   return send_file(abs_safe_path, as_attachment=True)

if __name__ == "__main__":
   app.run(host="0.0.0.0", port=5000, debug=True)