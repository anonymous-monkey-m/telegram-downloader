#!/usr/bin/env python3
"""Private Telegram media downloader bot.

Design: same pattern as the X feed — a GitHub Actions workflow runs this
script every few minutes. Each run pulls pending updates from Telegram
(getUpdates), and for every allowlisted message or channel post that
contains a link it downloads the media (yt-dlp first, gallery-dl as the
image/gallery fallback), normalises it for Telegram, posts it back as a
reply, and commits the new update offset to state.json.

Config (environment):
  BOT_TOKEN         the downloader bot's token (GitHub secret)
  ALLOWED_CHAT_IDS  comma-separated chat/channel IDs the bot serves
  ALLOWED_USER_ID   Michael's Telegram user ID (for DMs)
  COOKIES_FILE      optional path to a Netscape cookies.txt for
                    login-walled links (Instagram / X)
  STATE_FILE        offset state path (default: state.json)

Setup mode: while both allowlists are empty the bot answers /id in any
chat with the chat + user IDs, so the owner can learn the IDs to lock in.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_CHATS = {int(x) for x in
                 os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",")
                 if x}
ALLOWED_USER = int(os.environ.get("ALLOWED_USER_ID") or 0)
SETUP_MODE = not ALLOWED_CHATS and not ALLOWED_USER
COOKIES_FILE = os.environ.get("COOKIES_FILE") or ""
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_UPLOAD = 49 * 1024 * 1024          # Bot API upload cap is 50 MB
URL_RE = re.compile(r"https?://[^\s<>'\"\)\]]+")
WORKROOT = tempfile.mkdtemp(prefix="dlbot-")
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav"}


# --- Telegram API ------------------------------------------------------

def tg(method, params=None, timeout=120):
    data = None
    if params:
        data = json.dumps(params).encode()
    req = urllib.request.Request(API + "/" + method, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('description', out)}")
    return out["result"]


def tg_upload(method, fields, files, timeout=900):
    """Multipart upload. fields: {name: str}; files: {name: path}."""
    boundary = "----dlbot" + str(int(time.time() * 1000))
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, path in files.items():
        with open(path, "rb") as fh:
            payload = fh.read()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{k}\"; filename=\"{os.path.basename(path)}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode()
        body += payload + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API + "/" + method, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    if not out.get("ok"):
        raise RuntimeError(f"{method}: {out.get('description', out)}")
    return out["result"]


def react(chat_id, message_id, emoji):
    """Best-effort status reaction; never fatal."""
    try:
        tg("setMessageReaction", {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}] if emoji else []})
    except Exception as e:
        print(f"reaction failed: {e}", flush=True)


# --- State -------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"offset": 0}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


# --- Downloading -------------------------------------------------------

def cookie_args(tool):
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]
    return []


def run(cmd, timeout=600):
    print("+ " + " ".join(cmd[:2]) + " ...", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"exit {p.returncode}")
    return p


def media_files(destdir, before):
    out = []
    for name in sorted(os.listdir(destdir)):
        path = os.path.join(destdir, name)
        if path in before or not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS or ext == ".gif":
            out.append(path)
    return out


def download(url, destdir):
    """yt-dlp first (video/audio); gallery-dl fallback (photos, carousels,
    galleries). Returns a list of file paths (possibly empty)."""
    before = {os.path.join(destdir, n) for n in os.listdir(destdir)}
    try:
        run(["yt-dlp", "--no-playlist", "--no-warnings",
             "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
             "--merge-output-format", "mp4",
             "--max-filesize", "400M",
             "-o", os.path.join(destdir, "media.%(ext)s")]
            + cookie_args("yt-dlp") + [url])
    except Exception as e:
        print(f"yt-dlp: {e}", flush=True)
    files = media_files(destdir, before)
    if files:
        return files
    try:
        run(["gallery-dl", "-d", destdir] + cookie_args("gallery-dl") + [url])
    except Exception as e:
        print(f"gallery-dl: {e}", flush=True)
    # gallery-dl nests files in subfolders
    found = []
    for root, _dirs, names in os.walk(destdir):
        for n in sorted(names):
            p = os.path.join(root, n)
            if p not in before:
                ext = os.path.splitext(n)[1].lower()
                if ext in VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS or ext == ".gif":
                    found.append(p)
    return found


# --- Media preparation ---------------------------------------------------

def probe(path):
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True, text=True)
    return json.loads(p.stdout or "{}")


def duration_of(path):
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except Exception:
        return 0.0


def prepare_video(path):
    """Return a Telegram-friendly MP4: H.264 + AAC, fast-start, <= 49 MB."""
    info = probe(path)
    streams = {s.get("codec_type"): s.get("codec_name")
               for s in info.get("streams", [])}
    size = os.path.getsize(path)
    out = path + ".tg.mp4"
    if (streams.get("video") == "h264" and streams.get("audio") in ("aac", None)
            and size <= MAX_UPLOAD and path.lower().endswith(".mp4")):
        run(["ffmpeg", "-y", "-v", "error", "-i", path, "-c", "copy",
             "-movflags", "+faststart", out], timeout=300)
        return out
    # Encode; if the result is still too big, re-encode to a bitrate budget.
    run(["ffmpeg", "-y", "-v", "error", "-i", path,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
         "-crf", "23", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", out], timeout=900)
    if os.path.getsize(out) > MAX_UPLOAD:
        dur = duration_of(path) or duration_of(out)
        if dur > 0:
            total_kbps = int(MAX_UPLOAD * 8 / dur / 1000 * 0.94)
            v_kbps = max(120, total_kbps - 96)
            smaller = path + ".sm.mp4"
            run(["ffmpeg", "-y", "-v", "error", "-i", path,
                 "-vf", "scale='min(720,iw)':-2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                 "-b:v", f"{v_kbps}k", "-maxrate", f"{v_kbps}k",
                 "-bufsize", f"{v_kbps * 2}k",
                 "-c:a", "aac", "-b:a", "96k",
                 "-movflags", "+faststart", smaller], timeout=1200)
            return smaller
    return out


def prepare(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS or ext == ".gif":
        return prepare_video(path)
    if ext == ".webp":
        out = path + ".jpg"
        run(["ffmpeg", "-y", "-v", "error", "-i", path, out], timeout=120)
        return out
    return path


# --- Sending -------------------------------------------------------------

def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "photo"
    return "audio"


def build_groups(files):
    """Chunk into Telegram media groups of at most 10."""
    return [files[i:i + 10] for i in range(0, len(files), 10)]


def send_media(chat_id, reply_to, files, source_url):
    caption = source_url[:1000]
    if len(files) == 1:
        path = files[0]
        kind = kind_of(path)
        method = {"video": "sendVideo", "photo": "sendPhoto",
                  "audio": "sendAudio"}[kind]
        fields = {"chat_id": str(chat_id), "caption": caption,
                  "reply_to_message_id": str(reply_to)}
        if kind == "video":
            fields["supports_streaming"] = "true"
        tg_upload(method, fields, {kind: path})
        return
    first = True
    for group in build_groups(files):
        media, uploads = [], {}
        for i, path in enumerate(group):
            key = f"file{i}"
            item = {"type": kind_of(path), "media": f"attach://{key}"}
            if kind_of(path) == "video":
                item["supports_streaming"] = True
            if first and i == 0:
                item["caption"] = caption
            media.append(item)
            uploads[key] = path
        tg_upload("sendMediaGroup",
                  {"chat_id": str(chat_id), "media": json.dumps(media),
                   "reply_to_message_id": str(reply_to)}, uploads)
        first = False


# --- Main loop -----------------------------------------------------------

def extract_urls(text):
    return [u.rstrip(".,") for u in URL_RE.findall(text or "")]


def is_allowed(chat_id, user_id):
    return chat_id in ALLOWED_CHATS or (ALLOWED_USER and user_id == ALLOWED_USER)


def process_link(url, chat_id, message_id):
    react(chat_id, message_id, "⏳")
    destdir = tempfile.mkdtemp(dir=WORKROOT)
    try:
        files = download(url, destdir)
        if not files:
            raise RuntimeError("no media found")
        prepared = [prepare(f) for f in files]
        send_media(chat_id, message_id, prepared, url)
        react(chat_id, message_id, "✅")
        print(f"done {url} ({len(prepared)} file(s))", flush=True)
    except Exception as e:
        print(f"FAILED {url}: {e}", flush=True)
        react(chat_id, message_id, "❌")
        try:
            tg("sendMessage", {"chat_id": chat_id,
                               "reply_to_message_id": message_id,
                               "text": "⚠️ Couldn't download that link "
                                       "(private, broken, or unsupported)."})
        except Exception:
            pass
    finally:
        shutil.rmtree(destdir, ignore_errors=True)


def main():
    state = load_state()
    updates = tg("getUpdates", {
        "offset": state.get("offset", 0), "timeout": 0,
        "allowed_updates": ["message", "channel_post"]}, timeout=60)
    print(f"{len(updates)} update(s); setup_mode={SETUP_MODE}", flush=True)
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id", 0)
        text = msg.get("text") or msg.get("caption") or ""
        if text.strip().startswith("/id"):
            if SETUP_MODE or is_allowed(chat_id, user_id):
                tg("sendMessage", {"chat_id": chat_id,
                                   "reply_to_message_id": msg["message_id"],
                                   "text": f"chat_id: {chat_id}\n"
                                           f"user_id: {user_id}"})
            continue
        urls = extract_urls(text)
        if not urls:
            continue
        if not is_allowed(chat_id, user_id):
            print(f"ignored chat {chat_id} (not allowlisted)", flush=True)
            continue
        for url in urls[:3]:
            process_link(url, chat_id, msg["message_id"])
    save_state(state)
    shutil.rmtree(WORKROOT, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
