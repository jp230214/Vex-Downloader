"""Vex Downloader.

Created by Vex.

A PyQt6 + yt-dlp desktop app for downloading YouTube videos, playlists and
whole channels as MP4 or MP3.

On launch you pick a mode:

    * Single Video  - paste one watch URL
    * Playlist      - paste a playlist URL, pick the entries you want
    * Channel       - paste a channel/handle URL and grab everything
    * Audio only    - shortcut that forces MP3 output

Threading model
---------------
Every network / disk heavy operation runs on a plain ``threading.Thread``.
Workers NEVER touch Qt widgets; they only emit the signals declared on
:class:`DownloaderSignals`, which are connected to slots living on the main
window. Because the receiver lives in the main thread, Qt automatically uses a
queued connection, so the UI is always updated from the GUI thread.
"""

import os
import re
import sys
import json
import shutil
from datetime import datetime
import threading
import time
import traceback
from queue import Empty, Queue
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit, urlunsplit

import yt_dlp
from yt_dlp.utils import DownloadError

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QSizePolicy,
    QLineEdit, QFileDialog, QTableWidget, QTableWidgetItem, QComboBox,
    QStackedWidget, QListWidget, QListWidgetItem, QMessageBox, QHeaderView,
    QSplashScreen, QGridLayout, QSpinBox, QFrame, QProgressBar
)
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtCore import Qt, QTimer, QObject, QSize, pyqtSignal

# The product name is platform-neutral now that YouTube, Spotify and
# SoundCloud are all first-class. The active platform is appended to
# the window title instead of being baked into the name.
APP_NAME = "Vex Downloader"
APP_AUTHOR = "Vex"
APP_VERSION = "2.2.0"

# --- Politeness / anti-blocking limits -------------------------------------
#
# YouTube has no published request quota, but it does watch for bursts. Firing
# many downloads at once is the single most reliable way to get served
# "Sign in to confirm you're not a bot", and the block then sticks for your
# IP for hours. So downloads run through a small queue instead of one thread
# per video.

# How many videos may download at the same time. 3 is fast enough to feel
# snappy and slow enough to look like a normal viewer.
MAX_PARALLEL_DOWNLOADS = 3

# Pause between finishing one video and starting the next, so the request
# pattern looks unhurried.
DELAY_BETWEEN_DOWNLOADS = 1.5

# If YouTube pushes back, wait this long before giving up on a video.
BACKOFF_SECONDS = (10, 30, 60)

# One lock per video URL. A playlist can legitimately contain the same video
# twice, and without this both workers would write the same target file at the
# same time - yt-dlp then deletes its temp file out from under the other
# worker and one of them reports a failure. Different videos still run in
# parallel; only duplicate URLs queue up behind each other.
_url_locks = {}
_url_locks_guard = threading.Lock()


def lock_for(url):
    """Return the lock guarding downloads of ``url``."""
    with _url_locks_guard:
        lock = _url_locks.get(url)
        if lock is None:
            lock = threading.Lock()
            _url_locks[url] = lock
        return lock

# --- Optional account support -----------------------------------------------
#
# Sign-in exists for one reason: reaching content that is otherwise invisible,
# namely private playlists, age-restricted videos you can already watch, and
# your own uploads. It is opt-in and OFF by default.
#
# It is NOT a way to raise the download limit. Signing in does not grant a
# bigger allowance - it just supplies session cookies and a PO token, which
# lower the odds of tripping bot detection. If you are already blocked, an
# IP block stays an IP block.
#
# No password or token is ever stored by this app. It asks yt-dlp to read
# cookies straight out of your browser profile, which you already logged into.

CONFIG_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "VexDownloader")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# browser key -> (label, profile directory)
AUTH_BROWSERS = {
    "none": ("Not signed in", None),
    "chrome": ("Google Chrome", "Google/Chrome"),
    "edge": ("Microsoft Edge", "Microsoft/Edge"),
    "firefox": ("Mozilla Firefox", "Mozilla/Firefox"),
    "brave": ("Brave", "BraveSoftware/Brave-Browser"),
    "opera": ("Opera", "Opera Software/Opera Stable"),
}

# Set at runtime by the settings page; None means "do not send cookies".
_auth_browser = "none"


def auth_opts():
    """yt-dlp options for using the signed-in browser session, if enabled.

    Returns an empty dict when signed out, so call sites can always splat it.
    """
    if _auth_browser == "none":
        return {}
    return {"cookiesfrombrowser": (_auth_browser,)}


def load_config():
    """Read saved settings, ignoring anything unreadable."""
    try:
        # utf-8-sig transparently strips a BOM when present, so a config edited
        # or written by a Windows tool (Notepad, PowerShell) still loads.
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - corrupt config must not block startup
        log("[config] Could not read config; using defaults.")
    return {}


def save_config(config):
    """Write settings atomically so a crash cannot truncate the file."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        temp = CONFIG_PATH + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
        os.replace(temp, CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - a read-only profile is survivable
        log(f"[config] Could not save config: {exc}")


DOCS = {
    "README.md": """# {name} {version}

A PyQt6 + yt-dlp desktop app for downloading YouTube videos, playlists and
whole channels as MP4 or MP3. Created by {author}.

## Running the portable build

Double-click **`{exe}`**. Nothing to install first - Python, PyQt6 and yt-dlp
are all bundled inside the executable.

The first launch shows a short setup guide. After you close it, the app starts
normally from then on.

## Optional: FFmpeg

FFmpeg is the one thing that cannot be bundled, and the app works without it -
you just get fewer options:

| Feature            | Without FFmpeg        | With FFmpeg                  |
| ------------------ | --------------------- | ---------------------------- |
| MP4, pre-muxed     | works                 | works                        |
| MP3 (audio only)   | **unavailable**       | works                        |
| 1080p+ streams     | best pre-muxed only   | video and audio merged       |

Install it with:

    winget install Gyan.FFmpeg

then restart the app.

## Your own logo

Drop a square PNG named `logo.png` into the `assets` folder next to the
executable. It becomes the window, taskbar and splash icon automatically. With
no `logo.png`, the app falls back to the selected service's mark.

## Where things are kept

    %APPDATA%\\VexDownloader\\config.json    settings
    %APPDATA%\\VexDownloader\\history.json   download history
    ~/Downloads/Vex_Download/               downloaded files

Deleting the `VexDownloader` folder resets the app to its first-run state.

## Running from source instead

    pip install -r requirements.txt
    python Vex.py

## Requirements (source install only)

    PyQt6>=6.5
    yt-dlp>=2024.1.1
""",
    "how to install.txt": """{name} {version} - how to install
=================================

Created by {author}.

OPTION 1 - PORTABLE (recommended)
---------------------------------
1. Unzip the download anywhere (for example your Desktop).
2. Double-click {exe}.
3. The first launch shows a setup guide. Read it, then close it.

That is the whole install. Python, PyQt6 and yt-dlp are bundled inside the
executable, so there is nothing else to download or install.


OPTION 2 - RUNNING FROM SOURCE
------------------------------
1. Install Python 3.10 or newer from python.org
   (tick "Add Python to PATH" during setup).
2. Open a terminal in this folder.
3. Run:

       pip install -r requirements.txt
       python Vex.py

   Or on Windows, just double-click run_vex.bat - it installs what is missing
   and then starts the app.


OPTIONAL - FFMPEG
-----------------
FFmpeg adds MP3 export and high-quality 1080p+ merging. Without it the app
still downloads pre-muxed MP4 video fine, but the MP3 option is unavailable.

    winget install Gyan.FFmpeg

Restart the app afterwards. The Settings page will confirm FFmpeg was found.


FIRST RUN
---------
On first launch the app walks you through setup and writes these files:

    README.md          this overview
    requirements.txt   the Python packages, for source installs
    how to install.txt this file

Your settings and download history live outside the app folder, in:

    %APPDATA%\\VexDownloader\\

To reset everything, close the app, delete that folder, and run it again.
""",
    "requirements.txt": """# Python packages required to run Vex from source.
#
# The portable build bundles all of these - you do NOT need to install them
# by hand when using the .exe.
#
# Install with:  pip install -r requirements.txt

PyQt6>=6.5
yt-dlp>=2024.1.1

# Already pulled in by yt-dlp, listed because Vex uses it directly to write
# Spotify title/artist/album tags onto downloaded files.
mutagen>=1.46

# Optional, recommended but not required:
#   FFmpeg  - not installable via pip. Enables MP3 export and 1080p+ merging.
#             Install with:  winget install Gyan.FFmpeg
""",
}


def generate_docs():
    """Write README.md, how to install.txt and requirements.txt beside the app.

    Called by the setup installer at install time, *not* on every app launch.
    A portable copy of the exe is expected to run from wherever it was put -
    including a read-only folder or a USB stick - so it must not write anything
    just by starting. A failure is logged rather than raised: a read-only
    install folder must never stop the app from launching.
    """
    exe = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "Vex.py"
    values = {
        "name": APP_NAME,
        "version": APP_VERSION,
        "author": APP_AUTHOR,
        "exe": exe,
    }
    written = []
    for filename, template in DOCS.items():
        try:
            target = os.path.join(app_dir(), filename)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(template.format(**values))
            written.append(filename)
        except Exception as exc:  # noqa: BLE001 - never block startup on docs
            log(f"[docs] Could not write {filename}: {exc}")
    return written


def detect_browsers():
    """Which supported browsers actually have a profile on this machine."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    roaming = os.environ.get("APPDATA") or os.path.expanduser("~")
    candidates = {
        "chrome": os.path.join(base, "Google", "Chrome", "User Data"),
        "edge": os.path.join(base, "Microsoft", "Edge", "User Data"),
        "firefox": os.path.join(roaming, "Mozilla", "Firefox", "Profiles"),
        "brave": os.path.join(base, "BraveSoftware", "Brave-Browser", "User Data"),
        "opera": os.path.join(roaming, "Opera Software", "Opera Stable"),
    }
    found = []
    for key, path in candidates.items():
        if os.path.isdir(path):
            found.append(key)
    return found

# The download modes offered on the launcher screen.
MODE_VIDEO = "video"
MODE_PLAYLIST = "playlist"
MODE_CHANNEL = "channel"

MODE_CARDS = [
    (MODE_VIDEO, "\U0001F3AC", "Single Video",
     "Download one video from a watch link."),
    (MODE_PLAYLIST, "\U0001F3B9", "Playlist",
     "Load a whole playlist and pick what to save."),
    (MODE_CHANNEL, "\U0001F4E2", "Channel",
     "Grab every upload from a channel."),
]

# The streaming services offered on the first-launch screen.
SERVICE_YT = "yt"
SERVICE_SPOTIFY = "spotify"
SERVICE_SOUNDCLOUD = "soundcloud"

# Exactly three cards: YouTube/YT Music, Spotify, SoundCloud.
#
# Apple Music was dropped from this list deliberately. Unlike Spotify - which is
# only metadata, with the audio sourced from elsewhere - an Apple Music download
# has to decrypt FairPlay DRM, which means circumventing a protection measure
# rather than reading public metadata. SoundCloud is a like-for-like swap that
# yt-dlp already understands natively.
SERVICES = [
    (SERVICE_YT, "YouTube / YT Music",
     "Videos, playlists and whole channels, as MP4 or MP3.", False),
    (SERVICE_SPOTIFY, "Spotify",
     "Track, album and playlist downloads.", False),
    (SERVICE_SOUNDCLOUD, "SoundCloud",
     "Tracks, sets and artist profiles.", False),
]

# Short label for each service, used in the title bar.
SERVICE_LABELS = {
    SERVICE_YT: "YouTube",
    SERVICE_SPOTIFY: "Spotify",
    SERVICE_SOUNDCLOUD: "SoundCloud",
}


# Placeholder shown in each mode's URL box.
MODE_URL_HINT = {
    MODE_VIDEO: "Paste a YouTube Video URL",
    MODE_PLAYLIST: "Paste a YouTube Playlist URL",
    MODE_CHANNEL: "Channel URL or @handle",
}

# Sidebar index for each download mode.
MODE_INDEX = {MODE_VIDEO: 0, MODE_PLAYLIST: 1, MODE_CHANNEL: 2}
DOWNLOAD_PAGE_COUNT = len(MODE_INDEX)


# --------------------------------------------------------------------------- #
def default_download_dir():
    """Where downloads should land when the user has not chosen a folder.

    Prefers the real Downloads location, which on Windows is frequently NOT
    ``C:\\Users\\<you>\\Downloads`` - it is often redirected into OneDrive, and
    users can move it anywhere. Asking the shell keeps that working instead of
    writing files to a folder the user never looks at.

    Falls back to the home folder if nothing usable exists.
    """
    home = os.path.expanduser("~")
    candidates = []

    # The Downloads known-folder GUID, read from the registry.
    downloads_guid = "{374DE290-123F-4565-9164-39C4925E467B}"
    try:
        import winreg  # noqa: PLC0415 - Windows only, imported lazily

        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw, _ = winreg.QueryValueEx(key, downloads_guid)
        expanded = os.path.expandvars(raw).rstrip("\\/")
        if expanded:
            candidates.append(expanded)
    except Exception:  # noqa: BLE001 - not Windows, or the key is missing
        pass

    # Also try the User Shell Folders entry that holds a plain path, and the
    # conventional folders, in case the GUID lookup is unavailable.
    candidates.append(os.path.join(home, "Downloads"))
    candidates.append(os.path.join(home, "downloads"))

    for path in candidates:
        if path and os.path.isdir(path):
            return path

    return home


# Global variables (kept for backwards compatibility with the original code)
# --------------------------------------------------------------------------- #
download_path = default_download_dir()
playlist_name = ""
download_threads = {}
paused_downloads = {}


# Characters Windows forbids in file / folder names, plus control characters.
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Device names Windows refuses to use even with an extension.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_name(name, fallback="Vex_Downloads", max_length=120):
    """Make ``name`` safe to use as a Windows folder name."""
    name = _INVALID_FILENAME_CHARS.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    # Windows silently strips trailing dots / spaces, which breaks the path.
    name = name.rstrip(" .")
    if not name:
        name = fallback
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"
    return name[:max_length]


def log(message):
    """Print to the console without ever dying on a non-UTF8 console.

    The Windows CMD default code page (cp1252/cp437) cannot encode the emoji
    that yt-dlp and this app emit, which would raise UnicodeEncodeError and
    kill the download worker.

    A windowed build has no console at all, so every message is also appended
    to ``%APPDATA%\\VexDownloader\\vex.log``. Without it a frozen GUI build has
    nowhere to report a crash, and the user has no way to diagnose it.
    """
    text = str(message)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            print(text.encode(encoding, "replace").decode(encoding, "replace"),
                  flush=True)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
    except Exception:  # noqa: BLE001 - logging must never raise
        pass
    _log_to_file(text)


# Keep the on-disk log from growing without bound across a long session.
_LOG_MAX_BYTES = 512 * 1024
_log_lock = threading.Lock()


def _log_to_file(text):
    """Mirror a log line into the app's log file, never raising."""
    path = os.path.join(CONFIG_DIR, "vex.log")
    try:
        with _log_lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            # Rotate once it gets large so a stuck loop cannot fill the disk.
            try:
                if os.path.getsize(path) > _LOG_MAX_BYTES:
                    os.replace(path, path + ".old")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as handle:
                stamp = datetime.now().strftime("%H:%M:%S")
                handle.write(f"{stamp} {text}\n")
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


def sanitize_path_env():
    """Remove PATH entries the OS cannot even stat.

    yt-dlp walks every PATH entry with ``os.path.realpath`` while looking for a
    JavaScript runtime. A single unreadable entry (e.g. a stale path on a
    removed drive raising ``WinError 1392``) makes every yt-dlp call explode
    before any network request happens, so we drop just those entries.
    """
    raw_path = os.environ.get("PATH", "")
    if not raw_path:
        return []

    kept, dropped = [], []
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        try:
            os.path.realpath(entry)
        except OSError as exc:
            dropped.append((entry, exc))
            continue
        kept.append(entry)

    if dropped:
        os.environ["PATH"] = os.pathsep.join(kept)
    return dropped


def ffmpeg_available():
    """True when an ffmpeg binary can be found on PATH."""
    return shutil.which("ffmpeg") is not None


_path_cleaned = False


def ensure_clean_path():
    """Idempotently remove unreadable PATH entries (see sanitize_path_env)."""
    global _path_cleaned
    if _path_cleaned:
        return
    _path_cleaned = True
    for entry, exc in sanitize_path_env():
        log(f"[warning] Removed unreadable PATH entry {entry!r} ({exc}) - "
            f"fix or delete it in your system environment variables.")


def build_format_selector(file_format, quality, has_ffmpeg):
    """Return a yt-dlp format string honouring the user's format/quality choice.

    Every selector ends with a plain ``b`` ("best available") so that a video
    which simply does not offer the requested height/extension still downloads
    instead of failing with "Requested format is not available".
    """
    if file_format == "mp3":
        # Audio only - the MP3 conversion itself is done by FFmpegExtractAudio.
        return "bestaudio/best"

    height = None
    if quality and quality != "best":
        try:
            height = int(quality)
        except ValueError:
            height = None

    if height is None:
        if has_ffmpeg:
            return "bv*+ba/b/bv*+ba/b"
        # Without FFmpeg only a pre-muxed single file can be saved.
        return "b[ext=mp4]/b"

    if has_ffmpeg:
        return (
            f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={height}][ext=mp4]/"
            f"bv*[height<={height}]+ba/"
            f"b[height<={height}]/b"
        )
    return f"b[height<={height}][ext=mp4]/b[height<={height}]/b"


def js_runtime_opts():
    """yt-dlp needs a JavaScript runtime for YouTube; use whichever is installed.

    YouTube extraction without one is deprecated and silently loses formats, so
    prefer the runtime that is actually on PATH. ``remote_components`` fetches
    yt-dlp's EJS challenge-solver scripts, which current YouTube requires to
    resolve every stream signature (otherwise formats silently go missing).
    """
    opts = {}
    for runtime in ("deno", "node", "bun"):
        if shutil.which(runtime):
            opts["js_runtimes"] = {runtime: {}}
            break
    try:
        opts["remote_components"] = ["ejs:github"]
    except Exception:  # noqa: BLE001 - never fatal
        pass
    return opts


_YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "youtube-nocookie.com"}

_SPOTIFY_HOSTS = {
    "open.spotify.com", "play.spotify.com", "spotify.com",
    "spotify.link", "spoti.fi",
}

# SoundCloud hosts. "on.soundcloud.com" and "snd.sc" are the share/short links
# that appear when a track is shared from the mobile app.
_SOUNDCLOUD_HOSTS = {
    "soundcloud.com", "on.soundcloud.com", "snd.sc", "api.soundcloud.com",
}

# Which hosts each service accepts. Everything that validates a URL consults
# this instead of a single hard-coded list, so adding a service later is a
# one-line change rather than a rewrite.
SERVICE_HOSTS = {
    SERVICE_YT: _YOUTUBE_HOSTS,
    SERVICE_SOUNDCLOUD: _SOUNDCLOUD_HOSTS,
    SERVICE_SPOTIFY: _SPOTIFY_HOSTS,
}


def service_of_host(host):
    """Which service a host belongs to, or None when it belongs to none."""
    host = (host or "").lower()
    if host.startswith("m."):
        host = host[2:]
    for service, hosts in SERVICE_HOSTS.items():
        if host in hosts:
            return service
    return None


# Per-service wording for the URL box. SoundCloud has no bare-id or @handle
# shortcut, so its hints name real URL shapes instead.
SERVICE_URL_HINT = {
    SERVICE_YT: MODE_URL_HINT,
    SERVICE_SOUNDCLOUD: {
        MODE_VIDEO: "Paste a SoundCloud track URL",
        MODE_PLAYLIST: "Paste a SoundCloud set (playlist) URL",
        MODE_CHANNEL: "SoundCloud artist profile URL",
    },
    SERVICE_SPOTIFY: {
        MODE_VIDEO: "Paste a Spotify track link",
        MODE_PLAYLIST: "Paste a Spotify playlist or album link",
        MODE_CHANNEL: "Paste a Spotify artist link",
    },
}


def url_hint(mode, service=SERVICE_YT):
    """Placeholder text for ``mode`` under ``service``."""
    hints = SERVICE_URL_HINT.get(service) or MODE_URL_HINT
    return hints.get(mode, "Paste a URL")

# Smarter quotes sneak in when a URL is copied out of Word/Discord/email and
# stop the URL from ever matching.
_SMART_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00a0": " ", "\u200b": "", "\ufeff": "",
}


def clean_pasted(text):
    """Strip the junk that comes along when a URL is pasted from elsewhere."""
    for bad, good in _SMART_QUOTES.items():
        text = text.replace(bad, good)
    return text.strip().strip("<>\"'").strip()


def normalize_url(raw, service=SERVICE_YT):
    """Turn anything the user pasted into a real https:// URL.

    Returns ``(url, error)``. ``error`` is a message meant for the user when
    the text cannot be understood.

    This is deliberately forgiving: people paste ``www.youtube.com/watch?v=x``
    without a scheme, wrap links in <>, or drop a trailing slash, and none of
    that should be treated as "not a link".

    The YouTube-only shortcuts (``@handle``, a bare 11-character id) are
    applied only when the YouTube service is active. Offering them for a
    SoundCloud paste would just produce a confusing wrong answer.
    """
    text = clean_pasted(raw or "")
    if not text:
        return None, "Paste a URL first."

    if service == SERVICE_YT:
        # A bare channel handle instead of a full link, e.g. "@SomeChannel" or
        # "@SomeChannel/videos". The channel's /videos tab is resolved later by
        # resolve_channel_tab, so the bare handle URL is all that is needed here.
        handle = re.fullmatch(r"@([A-Za-z][\w.-]*)(/[\w/-]*)?", text)
        if handle:
            path = handle.group(2) or ""
            if path and not path.startswith("/"):
                path = "/" + path
            return f"https://www.youtube.com/@{handle.group(1)}{path}", None

        # A bare 11 character video id, the thing YouTube shows under a video.
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
            return f"https://www.youtube.com/watch?v={text}", None

    # Anything that already carries a scheme.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        candidate = text
    else:
        # No scheme: assume https rather than rejecting it.
        candidate = "https://" + text.lstrip("/")

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None, "That does not look like a valid link."

    raw_host = (parts.hostname or "").lower()

    # Drop a leading "www."/"m."/"music." so the host matches our allow list.
    host = raw_host
    for prefix in ("www.", "m.", "music."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break

    accepted = SERVICE_HOSTS.get(service) or set()
    if host not in accepted and raw_host not in accepted:
        return None, (wrong_service_message(host or text, service))

    # Rebuild the URL with a clean host and the original path/query/fragment.
    # SoundCloud share links (on.soundcloud.com / snd.sc) are kept on their own
    # host: yt-dlp resolves the short code, and rewriting the host would break it.
    keep_host = raw_host if (
        raw_host in ("on.soundcloud.com", "snd.sc")
        or raw_host in _SPOTIFY_HOSTS
    ) else host
    path = parts.path or "/"
    rebuilt = f"https://{keep_host}{path}"
    if parts.query:
        rebuilt += f"?{parts.query}"
    return rebuilt, None


def wrong_service_message(host, service):
    """Explain, in the user's own terms, why a URL was not accepted."""
    if service == SERVICE_SPOTIFY:
        return (
            f"'{host}' is not a Spotify link.\n\n"
            "These all work:\n"
            "  https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT  (track)\n"
            "  https://open.spotify.com/playlist/37i9dQZF1DX4WYpdgoIcn6\n"
            "  https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX\n"
            "  spotify:track:4cOdK2wGLETKBW3PvgPWqT                  (URI form)\n\n"
            "The music itself is fetched from YouTube; Spotify is only used "
            "for the track list and tags."
        )
    if service == SERVICE_SOUNDCLOUD:
        return (
            f"'{host}' is not a SoundCloud link.\n\n"
            "These all work:\n"
            "  https://soundcloud.com/artist/track-slug       (track)\n"
            "  https://soundcloud.com/artist/sets/my-playlist (set)\n"
            "  https://soundcloud.com/artist                  (artist profile)\n"
            "  soundcloud.com/artist/track-slug               (no https:// needed)"
        )
    return (
        f"'{host}' is not a YouTube link.\n\n"
        "These all work:\n"
        "  https://www.youtube.com/watch?v=dQw4w9WgXcQ   (video)\n"
        "  https://www.youtube.com/playlist?list=PLxxxx  (playlist)\n"
        "  https://www.youtube.com/@SomeChannel          (channel)\n"
        "  @SomeChannel                                  (channel handle)\n"
        "  dQw4w9WgXcQ                                    (just the id)\n"
        "  www.youtube.com/watch?v=dQw4w9WgXcQ            (no https:// needed)"
    )


def classify_url(url):
    """Guess which download mode a pasted URL belongs to.

    Returns one of ``MODE_VIDEO`` / ``MODE_PLAYLIST`` / ``MODE_CHANNEL``, or
    None when the link is too ambiguous to place.
    """
    lowered = (url or "").lower()
    if not lowered:
        return None

    try:
        parts = urlsplit(lowered)
        host = (parts.hostname or "").lower()
        for prefix in ("www.", "m.", "music."):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        path = parts.path or ""
        query = parts.query or ""
    except ValueError:
        host, path, query = "", lowered, ""

    if host in _SOUNDCLOUD_HOSTS:
        return _classify_soundcloud(host, path)

    if host in _SPOTIFY_HOSTS or lowered.startswith("spotify:"):
        return _classify_spotify(lowered)

    # Channel first: a channel tab can also carry ?list=, and treating that as
    # a playlist would only grab one page of uploads.
    if host == "youtu.be" or path.startswith(("/shorts/", "/live/", "/embed/")):
        return MODE_VIDEO
    if "/@" in path or any(p in path for p in ("/channel/", "/c/", "/user/",
                                               "/videos", "/streams", "/playlists")):
        return MODE_CHANNEL

    # A dedicated /playlist page is a playlist regardless of the query.
    if "/playlist" in path:
        return MODE_PLAYLIST

    # A watch page stays a video even when YouTube appends ?list= - that link
    # means "this video, played from that playlist", not "the whole playlist".
    if "/watch" in path or re.fullmatch(r"/[A-Za-z0-9_-]{11}", path):
        return MODE_VIDEO

    # Anything else carrying a playlist id is a playlist.
    if re.search(r"[?&]list=", query):
        return MODE_PLAYLIST
    return None


def _classify_spotify(url):
    """Map a Spotify link onto a download mode.

        open.spotify.com/track/<id>    -> one track
        open.spotify.com/playlist/<id> -> playlist
        open.spotify.com/album/<id>    -> playlist (an album is a fixed list)
        open.spotify.com/artist/<id>   -> artist (their top tracks)
    """
    kind, _ident = spotify_urls(url)
    if kind == "track":
        return MODE_VIDEO
    if kind in ("playlist", "album"):
        return MODE_PLAYLIST
    if kind == "artist":
        return MODE_CHANNEL
    return None


def _classify_soundcloud(host, path):
    """Map a SoundCloud URL onto a download mode.

    SoundCloud's shapes are quite unlike YouTube's:

        soundcloud.com/artist                -> artist profile (all tracks)
        soundcloud.com/artist/tracks         -> artist profile, tracks tab
        soundcloud.com/artist/reposts        -> artist profile, reposts tab
        soundcloud.com/artist/sets/my-set    -> a set (their "playlist")
        soundcloud.com/artist/track-slug     -> one track
        on.soundcloud.com/AbCdE              -> share link for one track

    A bare ``/artist`` is deliberately a CHANNEL rather than a track: the
    trailing slug is what makes a track, so a one-segment path is the profile.
    """
    segments = [seg for seg in path.split("/") if seg]

    # Share/short links always resolve to a single track.
    if host in ("on.soundcloud.com", "snd.sc"):
        return MODE_VIDEO

    if not segments:
        return None

    # A set is the playlist equivalent, and it is named explicitly.
    if "sets" in segments:
        return MODE_PLAYLIST

    # /artist or /artist/<tab> is the artist profile.
    if len(segments) == 1:
        return MODE_CHANNEL
    if len(segments) == 2 and segments[1] in ("tracks", "reposts", "albums",
                                              "popular", "likes"):
        return MODE_CHANNEL

    # /artist/track-slug is one track.
    if len(segments) == 2:
        return MODE_VIDEO

    # Deeper paths are still just the track (e.g. a reposts sub-path).
    return MODE_VIDEO


def is_list_url(url):
    """True when the URL can expand into more than one video."""
    return classify_url(url) in (MODE_PLAYLIST, MODE_CHANNEL)


# --------------------------------------------------------------------------- #
# Spotify
# --------------------------------------------------------------------------- #
#
# How this works, and why it is built this way
# ------------------------------------------
# Spotify offers no supported way to download audio. A subscription is
# DRM-protected and there is no export at all, so this feature never tries to
# take audio out of Spotify. It does what spotDL does:
#
#   1. Spotify is used only as a *catalogue*. Public metadata (title, artists,
#      album, duration, track listing) is read through the official Web API.
#   2. Each track is then located on YouTube, which serves that audio openly.
#   3. The file is fetched by yt-dlp and tagged with the Spotify metadata.
#
# Step 1 needs an access token, and Spotify only issues those to a registered
# application. Rather than shipping a scraper that breaks the day Spotify
# changes a private endpoint, the user pastes their own free Client ID and
# Secret in Settings. No password, no account login, no captcha - the official
# client-credentials flow is a machine-to-machine grant.
#
# So Spotify needs one piece of one-time setup before it works. The app says so
# plainly rather than failing later with a confusing error.
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_API = "https://api.spotify.com/v1"

# Cached token as (token, expiry). A client-credentials token lasts an hour, so
# a long playlist fetch authenticates only once.
_spotify_token_cache = {"token": None, "expires": 0.0}
# Reentrant on purpose: spotify_token() holds this lock across the HTTP call
# and its error handlers take it again to clear the cache. A plain Lock is not
# reentrant, so the 400/401 path would deadlock that thread forever.
_spotify_token_lock = threading.RLock()


def spotify_urls(raw):
    """Pull a Spotify type and id out of anything pasted in.

    Accepts ``open.spotify.com/track/<id>``, the ``spotify:track:<id>`` URI
    form used by the desktop app, and share links. Returns ``(kind, id)`` or
    ``(None, None)``.
    """
    text = clean_pasted(raw or "")
    if not text:
        return None, None

    # spotify:track:abc form, copied straight out of the desktop client.
    uri = re.fullmatch(r"spotify:([a-z]+):([A-Za-z0-9]+)", text, re.I)
    if uri:
        return uri.group(1).lower(), uri.group(2)

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return None, None

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _SPOTIFY_HOSTS:
        return None, None

    segments = [seg for seg in (parts.path or "").split("/") if seg]
    if (len(segments) >= 2
            and segments[0].lower() in ("track", "playlist", "album",
                                        "artist", "show", "episode")):
        return segments[0].lower(), segments[1]
    return None, None


# --------------------------------------------------------------------------- #
# Spotify sign-in (OAuth 2.0 with PKCE)
# --------------------------------------------------------------------------- #
#
# Two different things are going on here, and they are worth keeping apart:
#
#   * The app credentials (Client ID / Secret) identify Vex to Spotify. They
#     are enough to read public catalogue data, which is what a plain
#     open.spotify.com link needs.
#
#   * Signing in as a *user* is extra. It is what lets the app read your own
#     library, playlists you follow, and anything private to your account.
#     Nothing here needs your password - you approve in the browser and the app
#     only ever receives a token.
#
# PKCE is used so no client secret has to be embedded in the redirect exchange.
# A temporary loopback server catches the redirect, so no copy-pasting a long
# URL back into a box is needed.
_SCOPES = "user-library-read playlist-read-private user-read-private"
_spotify_auth = {"verifier": None, "server": None}


def _pkce_pair():
    """A PKCE verifier and the S256 challenge derived from it."""
    import base64
    import hashlib
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def spotify_signed_in():
    """True when a user has completed Spotify sign-in."""
    return bool((load_config().get("spotify_refresh_token") or "").strip())


def _spotify_auth_url(redirect_uri, challenge):
    cid, _secret = spotify_credentials()
    query = urllib.parse.urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": _SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"https://accounts.spotify.com/authorize?{query}"


def _exchange_spotify_code(code, redirect_uri, verifier):
    """Swap an authorization code for tokens."""
    import base64
    cid, secret = spotify_credentials()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        _SPOTIFY_TOKEN_URL, data=body, method="POST",
        headers={"Authorization": "Basic " + auth,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def spotify_sign_in_flow():
    """Run the whole browser sign-in. Blocking - call from a worker thread.

    Returns ``(ok, message)``.
    """
    import http.server
    import socketserver
    import threading
    import webbrowser

    verifier, challenge = _pkce_pair()
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required name
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                captured["code"] = params["code"][0]
                self._reply(
                    "<h2>Signed in to Spotify</h2>"
                    "<p>You can close this tab and go back to Vex.</p>")
            elif "error" in params:
                captured["error"] = params["error"][0]
                self._reply("<h2>Sign-in cancelled</h2>"
                            "<p>Go back to Vex.</p>")
            else:
                self._reply("<h2>Nothing to do here</h2>")

        def _reply(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            """Silence the default stderr logging."""

    # Port 0 lets the OS pick a free port, so two installs never collide.
    try:
        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:
        return False, f"Could not open a local callback port: {exc}"

    port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    def serve():
        # A short timeout so a cancelled sign-in does not hang forever.
        httpd.timeout = 300
        try:
            httpd.handle_request()
        finally:
            httpd.server_close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        webbrowser.open(_spotify_auth_url(redirect_uri, challenge))
    except Exception:  # noqa: BLE001 - user can copy the URL manually
        pass

    thread.join(timeout=300)

    if not captured.get("code"):
        return False, ("Sign-in was not completed. If your browser did not "
                       "open, check that pop-ups are allowed and try again.")

    try:
        data = _exchange_spotify_code(captured["code"], redirect_uri, verifier)
    except Exception as exc:  # noqa: BLE001 - report whatever Spotify said
        return False, f"Spotify rejected the sign-in: {exc}"

    refresh = data.get("refresh_token")
    if not refresh:
        return False, "Spotify did not return a refresh token."

    config = load_config()
    config["spotify_refresh_token"] = refresh
    save_config(config)

    # Validate immediately so the user is not told "signed in" wrongly.
    with _spotify_token_lock:
        _spotify_token_cache["token"] = None
    try:
        profile = spotify_user_profile()
        name = (profile.get("display_name")
                or profile.get("id") or "your account")
        return True, f"Signed in to Spotify as {name}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Signed in, but could not read your profile: {exc}"


def spotify_user_profile():
    """Read the signed-in user's profile (proves the token works)."""
    token = spotify_user_token()
    req = urllib.request.Request(
        f"{_SPOTIFY_API}/me", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _spotify_user_token():
    """A user token, refreshing it when the access token has expired."""
    with _spotify_token_lock:
        now = time.time()
        cached = _spotify_token_cache.get("user_token")
        if cached and now < _spotify_token_cache.get("user_expires", 0):
            return cached

        refresh = (load_config().get("spotify_refresh_token") or "").strip()
        if not refresh:
            raise ValueError("Not signed in to Spotify.")

        cid, secret = spotify_credentials()
        import base64
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }).encode()
        req = urllib.request.Request(
            _SPOTIFY_TOKEN_URL, data=body, method="POST",
            headers={"Authorization": "Basic " + auth,
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        _spotify_token_cache["user_token"] = data.get("access_token")
        _spotify_token_cache["user_expires"] = (
            now + int(data.get("expires_in", 3600)) - 60)
        # Spotify may rotate the refresh token; keep the newest one.
        if data.get("refresh_token"):
            config = load_config()
            config["spotify_refresh_token"] = data["refresh_token"]
            save_config(config)
        return _spotify_token_cache["user_token"]


def spotify_sign_out():
    """Forget the stored user token."""
    config = load_config()
    config.pop("spotify_refresh_token", None)
    save_config(config)
    with _spotify_token_lock:
        _spotify_token_cache.pop("user_token", None)
        _spotify_token_cache.pop("user_expires", None)


def spotify_credentials():
    """Client ID and Secret from settings, or raise a helpful error."""
    cid = (load_config().get("spotify_client_id") or "").strip()
    secret = (load_config().get("spotify_client_secret") or "").strip()
    if not cid or not secret:
        raise ValueError(
            "Spotify is not connected yet.\n\n"
            "Open Settings -> Spotify and paste your Client ID and Secret.\n"
            "You can get both free at https://developer.spotify.com/dashboard"
            " (create an app, then 'Show Client Secret')."
        )
    return cid, secret


def spotify_token():
    """Fetch, and cache, a Spotify access token via client credentials."""
    with _spotify_token_lock:
        cached = _spotify_token_cache
        if cached["token"] and time.time() < cached["expires"]:
            return cached["token"]

        cid, secret = spotify_credentials()
        import base64
        auth = base64.b64encode(f"{cid}:{secret}".encode("utf-8")).decode("ascii")
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials"}).encode()

        req = urllib.request.Request(
            _SPOTIFY_TOKEN_URL, data=body, method="POST",
            headers={"Authorization": "Basic " + auth,
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401):
                with _spotify_token_lock:
                    _spotify_token_cache["token"] = None
                raise ValueError(
                    "Spotify rejected the Client ID or Secret. Check both "
                    "values in Settings -> Spotify and paste them again."
                ) from exc
            raise ValueError(
                f"Could not reach Spotify ({exc.code}). Try again shortly."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - network problems
            raise ValueError(f"Could not reach Spotify: {exc}") from exc

        token = data.get("access_token")
        if not token:
            raise ValueError("Spotify did not return an access token.")
        # Renew a minute early so a long fetch never races the expiry.
        cached["token"] = token
        cached["expires"] = (time.time()
                             + int(data.get("expires_in", 3600)) - 60)
        return token


def spotify_api(path, params=None):
    """Call a Spotify Web API endpoint and return decoded JSON."""
    token = spotify_token()
    url = f"{_SPOTIFY_API}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            with _spotify_token_lock:
                _spotify_token_cache["token"] = None
            raise ValueError("Spotify session expired - please try again.") from exc
        raise ValueError(f"Spotify returned an error ({exc.code}).") from exc


def _track_fields(item):
    """Flatten a Spotify track object into what we match and save on."""
    track = (item or {}).get("track") if "track" in (item or {}) else item
    track = track or {}
    if not track.get("name"):
        return None
    artists = [a.get("name") for a in (track.get("artists") or [])
               if a.get("name")]
    return {
        "id": track.get("id"),
        "title": track.get("name"),
        "artists": artists,
        "artist": artists[0] if artists else "",
        "album": (track.get("album") or {}).get("name") or "",
        "duration_ms": track.get("duration_ms") or 0,
        "url": (track.get("external_urls") or {}).get("spotify") or "",
    }


def spotify_tracks(kind, ident, limit=0):
    """List the tracks behind a Spotify track/playlist/album/artist link."""
    entries = []

    if kind == "track":
        parsed = _track_fields(spotify_api(f"tracks/{ident}"))
        return [parsed] if parsed else []

    if kind == "playlist":
        offset, total = 0, None
        while True:
            data = spotify_api(f"playlists/{ident}/tracks",
                               {"limit": 100, "offset": offset})
            total = data.get("total")
            for item in (data.get("items") or []):
                parsed = _track_fields(item)
                if parsed:
                    entries.append(parsed)
            offset += 100
            if not data.get("items") or (total is not None and offset >= total):
                break
            if limit and len(entries) >= limit:
                break

    elif kind == "album":
        offset = 0
        while True:
            data = spotify_api(f"albums/{ident}/tracks",
                               {"limit": 50, "offset": offset})
            for item in (data.get("items") or []):
                parsed = _track_fields(item)
                if parsed:
                    entries.append(parsed)
            offset += 50
            if not data.get("items"):
                break
            if limit and len(entries) >= limit:
                break

    elif kind == "artist":
        data = spotify_api(f"artists/{ident}/top-tracks", {"limit": 50})
        for item in (data.get("tracks") or []):
            parsed = _track_fields(item)
            if parsed:
                entries.append(parsed)

    else:
        raise ValueError(
            f"Spotify links of type '{kind}' are not supported yet.")

    if limit and len(entries) > limit:
        entries = entries[:limit]
    return entries


# --- Matching a Spotify track to a YouTube upload ---------------------------
def _match_text(value):
    """Normalise a title for comparison: lower-case, no brackets, no punct."""
    text = (value or "").lower()
    text = re.sub(
        r"\((?:official\s*)?(?:audio|video|lyric[s]?|visuali[sz]er|"
        r"official\s*music\s*video|mv|hd|hq)[^)]*\)", " ", text)
    text = re.sub(r"\[.*?\]", " ", text)
    text = re.sub(r"\bfeat\.?\b|\bft\.?\b|\bfeaturing\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def score_youtube_candidate(track, candidate):
    """Confidence, 0..1, that a YouTube result is this Spotify track.

    Duration carries real weight because it is the one signal that survives a
    wildly different title: a live take or an extended mix still reads as a
    word-overlap match but fails on length.
    """
    title = candidate.get("title") or ""
    uploader = candidate.get("uploader") or candidate.get("channel") or ""

    wanted = _match_text(f"{track.get('artist')} {track.get('title')}")
    got = _match_text(f"{uploader} {title}")
    if not wanted or not got:
        return 0.0

    wanted_words = set(wanted.split())
    got_words = set(got.split())
    title_words = set(_match_text(title).split())
    artist_words = set(_match_text(track.get("artist")).split())

    coverage = len(wanted_words & got_words) / len(wanted_words)
    # Leaving a featured artist out of the title is normal, not a mismatch.
    if not title_words >= (wanted_words - artist_words):
        coverage *= 0.5
    if not artist_words <= (got_words | title_words):
        coverage *= 0.6

    duration_score = None
    target_ms = track.get("duration_ms") or 0
    seconds = candidate.get("duration")
    if target_ms and seconds:
        target_s = target_ms / 1000.0
        delta = abs(seconds - target_s) / target_s
        duration_score = max(0.0, 1.0 - delta / 0.10)
        if delta > 0.10:
            # More than 10% out is a different recording.
            coverage *= 0.15

    if duration_score is None:
        return coverage
    return coverage * 0.55 + duration_score * 0.45


def find_youtube_for_track(track, candidates=6):
    """Search YouTube. Returns ``(url, confidence, matched_title)``.

    A confidence below 0.55 means the match was too weak to trust, and the
    caller should say so rather than silently fetch the wrong song.
    """
    query = urllib.parse.quote_plus(
        " ".join(p for p in (track.get("artist"), track.get("title")) if p)
        + " audio")
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": candidates, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{candidates}:{query}",
                                    download=False)
    except Exception as exc:  # noqa: BLE001 - network / yt-dlp failures
        log(f"[spotify] YouTube search failed for "
            f"{track.get('artist')} - {track.get('title')}: {exc}")
        return None, 0.0, ""

    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        return None, 0.0, ""

    best = max(entries, key=lambda e: score_youtube_candidate(track, e))
    confidence = score_youtube_candidate(track, best)
    video_id = best.get("id")
    if not video_id:
        return None, 0.0, ""
    return (f"https://www.youtube.com/watch?v={video_id}", confidence,
            best.get("title") or "")


def apply_spotify_tags(path, track):
    """Write Spotify metadata onto a downloaded file."""
    try:
        from mutagen.easyid3 import EasyID3
    except Exception:  # noqa: BLE001 - tagging is a nicety, never fatal
        log("[spotify] mutagen unavailable - skipping tags.")
        return False
    try:
        audio = EasyID3(path)
        audio["title"] = [track.get("title") or ""]
        artists = track.get("artists") or []
        if artists:
            audio["artist"] = artists
        if track.get("album"):
            audio["album"] = [track["album"]]
        audio.save()
        return True
    except Exception as exc:  # noqa: BLE001 - never fail a download over tags
        log(f"[spotify] Could not write tags to {path}: {exc}")
        return False


# Phrases YouTube / yt-dlp use when it is throttling rather than the video
# genuinely being unavailable. These are worth retrying; a deleted video is not.
_THROTTLE_MARKERS = (
    "sign in to confirm you're not a bot",
    "too many requests",
    "http error 429",
    "429:",
    "rate-limit",
    "ratelimit",
    "temporary block",
    "temporarily blocked",
)

# Phrases that look like the above but are a different problem entirely, so
# they must NOT be treated as a rate limit.
_NOT_THROTTLE_MARKERS = (
    "confirm your age",     # age gate - retrying will not get past it
    "private",
    "members-only",
    "removed",
    "terminated",
)


# Some SoundCloud uploads are served with DRM and cannot be downloaded by any
# tool. That is a permanent property of the track, not a transient error, so it
# gets its own wording instead of being reported as a generic failure - and it
# must not be retried.
_DRM_MARKERS = (
    "drm protected",
    "drm-protected",
    "is drm",
    "protected by drm",
)


def is_drm_error(exc):
    """True when a track is DRM-protected and therefore undownloadable."""
    text = str(exc).lower()
    return any(marker in text for marker in _DRM_MARKERS)


def is_throttle_error(exc):
    """True when this failure is a rate limit / bot check rather than a bad URL."""
    text = str(exc).lower()
    if any(marker in text for marker in _NOT_THROTTLE_MARKERS):
        return False
    return any(marker in text for marker in _THROTTLE_MARKERS)


def resolve_channel_tab(ydl, url):
    """Find a channel's actual video list URL.

    Extracting a bare channel root (``/@Name``) returns the page tabs - Home,
    Videos, Playlists - not the uploads, so the caller would end up listing
    three bogus entries. Appending ``/videos`` is what actually yields the
    uploads, so that is tried first and the tab lookup is the fallback.
    """
    candidate = url.rstrip("/")
    for suffix in ("/videos", "/streams", "/shorts"):
        probe = candidate + suffix
        try:
            info = ydl.extract_info(probe, download=False, process=False)
        except DownloadError:
            continue
        except Exception:  # noqa: BLE001 - fall through to the next candidate
            continue
        if info and info.get("_type") == "playlist":
            return probe

    # Fall back to reading the tabs off the channel root itself.
    try:
        info = ydl.extract_info(url, download=False, process=False)
    except Exception:  # noqa: BLE001 - caller reports the real failure
        return url
    for entry in (info or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        tab_url = entry.get("url") or ""
        if tab_url.rstrip("/").endswith("/videos"):
            if tab_url.startswith("http"):
                return tab_url
            return url.rstrip("/") + tab_url
    return url


class ConsoleLogger:
    """Compact yt-dlp logger.

    Keeps the console useful (errors and warnings are always printed) while
    silencing the duplicate progress bar - the GUI shows its own progress.
    """

    def debug(self, message):
        text = str(message)
        # yt-dlp emits progress through debug() - ignore those, hooks handle it.
        if text.startswith("[debug] ") or "Downloading" in text or "%" in text:
            return
        log(f"[yt-dlp] {text}")

    def info(self, message):
        log(f"[yt-dlp] {message}")

    def warning(self, message):
        log(f"[yt-dlp][WARNING] {message}")

    def error(self, message):
        log(f"[yt-dlp][ERROR] {message}")


class DownloaderSignals(QObject):
    """Signals used to marshal worker results back onto the GUI thread."""

    list_fetched = pyqtSignal(list, str)          # entries [(title, url)], list_name
    fetch_failed = pyqtSignal(str)                # human readable error
    status_changed = pyqtSignal(object, int, str)  # page, row, status text
    log_message = pyqtSignal(str)
    history_saved = pyqtSignal(str, str, str)     # title, status, file path
    download_finished = pyqtSignal()              # one video finished (ok or not)
    auth_tested = pyqtSignal(bool, str)          # signed in?, message
    progress_updated = pyqtSignal(float, float, float)  # percent, eta_s, speed_B/s
    spotify_tested = pyqtSignal(str, str)                # message, colour
    spotify_auth_done = pyqtSignal(bool, str)            # signed in?, message


class CancelledError(Exception):
    """Raised inside the progress hook to abort an in-flight yt-dlp download.

    yt-dlp does not swallow exceptions raised from progress hooks, so throwing
    this from the hook aborts the download immediately (it propagates up out of
    ``extract_info``) instead of waiting for the transfer to finish.
    """


class ProgressTracker:
    """Aggregate bytes across every active download and produce a stable ETA.

    Windows' file-dialog ETA is useless because it divides the remaining bytes
    by the *instantaneous* speed, which swings wildly on a throttled connection
    and makes the estimate jump from "2 minutes" to "4 hours". The fix is to
    measure speed across a rolling window of recent history instead of the last
    sample, so a single stall or spike cannot throw the number off.
    """

    WINDOW_SECONDS = 12.0   # how much history the speed is averaged over

    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}        # key -> (done, total) for each live download
        self._history = []       # (monotonic_time, cumulative_done) for the window

    def reset(self):
        """Call when a fresh batch of downloads starts."""
        with self._lock:
            self._active.clear()
            self._history = []

    def update(self, key, done, total):
        """Record progress for one download (safe to call from any thread)."""
        with self._lock:
            self._active[key] = (done, total)
            now = time.monotonic()
            cumulative = sum(d for d, _ in self._active.values())
            self._history.append((now, cumulative))
            # Drop samples that have fallen outside the window.
            cutoff = now - self.WINDOW_SECONDS
            while len(self._history) > 1 and self._history[0][0] < cutoff:
                self._history.pop(0)

    def finish(self, key):
        """Stop counting a download that has completed or failed."""
        with self._lock:
            self._active.pop(key, None)

    def snapshot(self):
        """Return (percent, eta_seconds, speed_bps); any of them may be None."""
        with self._lock:
            done = sum(d for d, _ in self._active.values())
            total = sum(t for _, t in self._active.values())
            history = list(self._history)

        percent = (min(100.0, done * 100.0 / total) if total > 0 else None)

        speed = None
        if len(history) >= 2:
            t0, b0 = history[0]
            t1, b1 = history[-1]
            elapsed = t1 - t0
            if elapsed > 0:
                speed = max(0.0, (b1 - b0) / elapsed)

        eta = None
        if speed and speed > 0 and total > done:
            eta = (total - done) / speed

        return percent, eta, speed


# --------------------------------------------------------------------------- #
# Splash screen
# --------------------------------------------------------------------------- #
class SplashScreen(QSplashScreen):
    """Frameless splash screen shown while the app boots."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(600, 400)
        self.setStyleSheet("background-color: #141414; color: white;")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.center_splash()

        # The app's own branding, if assets/logo.png exists. The splash shows
        # before a service is picked, so the YouTube mark is only the fallback.
        logo = _user_logo("logo")
        if logo:
            pixmap = QPixmap(logo)
            if not pixmap.isNull():
                self.logo_label = QLabel(self)
                self.logo_label.setPixmap(pixmap.scaled(
                    72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                self.logo_label.setFixedSize(72, 72)
                self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.logo_label.setGeometry((600 - 72) // 2, 12, 72, 72)

        self.app_name = QLabel(APP_NAME, self)
        self.app_name.setStyleSheet("font-size: 30px; font-weight: bold; color: #ff2e63;")
        self.app_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.app_name.setGeometry(0, 90, 600, 44)

        self.author = QLabel(f"Created by {APP_AUTHOR}", self)
        self.author.setStyleSheet("font-size: 18px; color: #dddddd;")
        self.author.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.author.setGeometry(0, 140, 600, 28)

        self.version_label = QLabel(f"Version {APP_VERSION}", self)
        self.version_label.setStyleSheet("font-size: 15px; color: #888888;")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setGeometry(0, 175, 600, 24)

        self.tagline = QLabel("Videos \u2022 Playlists \u2022 Channels \u2022 Audio", self)
        self.tagline.setStyleSheet("font-size: 13px; color: #666666;")
        self.tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tagline.setGeometry(0, 250, 600, 22)

        self.bottom_bar = QLabel(self)
        self.bottom_bar.setStyleSheet("background-color: #ff2e63;")
        self.bottom_bar.setGeometry(0, 370, 600, 30)

        self.loading_text = QLabel("Loading...", self)
        self.loading_text.setStyleSheet("font-size: 14px; font-weight: bold; color: white;")
        self.loading_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_text.setGeometry(0, 370, 600, 30)

    def center_splash(self):
        """Centers the splash screen on the screen."""
        screen = QApplication.primaryScreen().geometry()
        splash_size = self.geometry()
        x = (screen.width() - splash_size.width()) // 2
        y = (screen.height() - splash_size.height()) // 2
        self.move(x, y)


# --------------------------------------------------------------------------- #
# Service icons
# --------------------------------------------------------------------------- #
#
# Icons are drawn at runtime with QPainter rather than shipped as image files.
# That keeps the app a single .py with no binary assets, and it scales cleanly
# to any DPI. Real brand artwork is often licensed for use only in its own
# context, so these are original simplified marks, not copies.
#
# If you have official logos you would rather use, drop them in the assets
# folder next to Vex.py and they are picked up automatically:
#
#     assets/yt.png
#     assets/spotify.png
#     assets/soundcloud.png
#
# Square PNG or SVG with transparency, at least 128x128, works best.


def app_dir():
    """The folder the app lives in: next to the .exe when frozen, else here.

    PyInstaller's onefile bundle unpacks into a temp dir that is deleted when
    the process exits, so anything the user should keep - the generated docs,
    their own logo.png - has to live beside the executable instead.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _logo_folders():
    """Folders searched for logos, most specific first.

    Beside the executable wins, so a logo.png the user dropped in overrides
    the one bundled with the app. PyInstaller's bundled copy is the fallback.
    """
    folders = [os.path.join(app_dir(), "assets")]
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = os.path.join(bundled, "assets")
        if candidate not in folders:
            folders.append(candidate)
    return folders


def asset_dir():
    """Folder holding optional user-supplied brand logos."""
    return _logo_folders()[0]


def _user_logo(name):
    """Path to a user-supplied logo for ``name``, or None."""
    for folder in _logo_folders():
        for ext in (".png", ".svg", ".jpg", ".jpeg", ".webp"):
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate):
                return candidate
    return None


def _draw_service_icon(service, size):
    """Paint an original simplified mark for ``service``."""
    from PyQt6.QtCore import QPointF, QRectF, Qt as _Qt
    from PyQt6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    s = size / 100.0  # every coordinate below is in a 0-100 space

    def rect(x, y, w, h):
        return QRectF(x * s, y * s, w * s, h * s)

    def dot(x, y, r):
        return QPointF(x * s, y * s), r * s

    if service == SERVICE_YT:
        # Red rounded rectangle with a white play triangle.
        painter.setBrush(QBrush(QColor("#FF0033")))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(rect(4, 20, 92, 60), 18 * s, 18 * s)
        painter.setBrush(QBrush(QColor("#FFFFFF")))
        path = QPainterPath()
        path.moveTo(41 * s, 36 * s)
        path.lineTo(69 * s, 50 * s)
        path.lineTo(41 * s, 64 * s)
        path.closeSubpath()
        painter.drawPath(path)

    elif service == SERVICE_SPOTIFY:
        # Spotify green circle with three white arcs.
        painter.setBrush(QBrush(QColor("#1ED760")))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(rect(4, 4, 92, 92))
        painter.setPen(QPen(QColor("#0B3D1E"), 7 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        for y, inset, span in ((36, 14, 72), (50, 20, 60), (63, 26, 48)):
            path = QPainterPath()
            path.moveTo((50 - span / 2) * s, y * s)
            path.quadTo(50 * s, (y - 9) * s, (50 + span / 2) * s, y * s)
            painter.drawPath(path)

    else:  # SERVICE_SOUNDCLOUD
        # SoundCloud orange disc with a white level-meter burst.
        painter.setBrush(QBrush(QColor("#FF5500")))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(rect(4, 4, 92, 92))
        painter.setPen(QPen(QColor("#FFFFFF"), 6 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        # Centre bar is tallest, the bars either side step down from it.
        for x, top, bottom in ((26, 44, 56), (39, 34, 66),
                               (52, 24, 76), (65, 38, 62), (76, 47, 53)):
            painter.drawLine(QPointF(x * s, top * s), QPointF(x * s, bottom * s))

    painter.end()
    return pixmap


def glyph_icon(name, color="#ffffff", size=16):
    """Small drawn icon for use beside a label.

    Emoji were used for these once, but emoji rendering depends entirely on a
    font being present that has the glyph - on a machine without it you get an
    empty box instead. Drawing them means they always look the same.
    """
    from PyQt6.QtCore import QPointF, QRectF, Qt as _Qt
    from PyQt6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    tint = QColor(color)
    s = size / 16.0

    def line(x1, y1, x2, y2, width=1.8):
        painter.setPen(QPen(tint, width * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(x1 * s, y1 * s), QPointF(x2 * s, y2 * s))

    if name == "folder":
        path = QPainterPath()
        path.moveTo(1.5 * s, 4 * s)
        path.lineTo(6 * s, 4 * s)
        path.lineTo(7.6 * s, 6 * s)
        path.lineTo(14.5 * s, 6 * s)
        path.lineTo(14.5 * s, 12.5 * s)
        path.lineTo(1.5 * s, 12.5 * s)
        path.closeSubpath()
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    elif name == "film":
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(1.5 * s, 3 * s, 13 * s, 10 * s), 1.5 * s, 1.5 * s)
        for x in (4, 8, 12):
            for y in (4.6, 8.6):
                painter.drawRect(QRectF((x - 0.5) * s, y * s, 1.4 * s, 2.4 * s))
    elif name == "quality":
        # Three ascending bars.
        painter.setPen(QPen(tint, 2 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        for x, top in ((4, 10), (8, 7), (12, 4)):
            painter.drawLine(QPointF(x * s, 12.5 * s), QPointF(x * s, top * s))
    elif name == "user":
        # Head plus shoulders - used for the "Sign in" heading.
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(5.4 * s, 2.2 * s, 5.2 * s, 5.2 * s))
        body = QPainterPath()
        body.moveTo(1.8 * s, 14 * s)
        body.cubicTo(1.8 * s, 9.6 * s, 5.2 * s, 8.4 * s, 8 * s, 8.4 * s)
        body.cubicTo(10.8 * s, 8.4 * s, 14.2 * s, 9.6 * s, 14.2 * s, 14 * s)
        painter.drawPath(body)
    elif name == "play":
        # A play triangle - the YouTube mark beside its heading.
        path = QPainterPath()
        path.moveTo(4.5 * s, 2.6 * s)
        path.lineTo(13 * s, 8 * s)
        path.lineTo(4.5 * s, 13.4 * s)
        path.closeSubpath()
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(tint)
        painter.drawPath(path)
    elif name == "check":
        line(3.5, 8.5, 6.8, 12, 2)
        line(6.8, 12, 12.5, 4.5, 2)
    elif name == "cross":
        line(4, 4, 12, 12, 2)
        line(12, 4, 4, 12, 2)
    elif name == "warning":
        path = QPainterPath()
        path.moveTo(8 * s, 2 * s)
        path.lineTo(15 * s, 14 * s)
        path.lineTo(1 * s, 14 * s)
        path.closeSubpath()
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        line(8, 6.5, 8, 10, 1.8)
        painter.setBrush(QBrush(tint))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawEllipse(QRectF(7.2 * s, 11.2 * s, 1.6 * s, 1.6 * s))
    elif name == "book":
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(2 * s, 2.5 * s, 12 * s, 11 * s), 1.2 * s, 1.2 * s)
        line(8, 2.5, 8, 13.5, 1.3)
    elif name == "spark":
        # Four-point sparkle.
        path = QPainterPath()
        path.moveTo(8 * s, 1 * s)
        path.lineTo(9.7 * s, 6.3 * s)
        path.lineTo(15 * s, 8 * s)
        path.lineTo(9.7 * s, 9.7 * s)
        path.lineTo(8 * s, 15 * s)
        path.lineTo(6.3 * s, 9.7 * s)
        path.lineTo(1 * s, 8 * s)
        path.lineTo(6.3 * s, 6.3 * s)
        path.closeSubpath()
        painter.setPen(QPen(tint, 1.3 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    elif name == "tools":
        # Wrench: a circle-headed bar with an open jaw.
        painter.setPen(QPen(tint, 1.6 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(4.5 * s, 11.5 * s), QPointF(11.5 * s, 4.5 * s))
        painter.drawEllipse(QRectF(2 * s, 9.5 * s, 4 * s, 4 * s))
        painter.drawLine(QPointF(11.5 * s, 2 * s), QPointF(14 * s, 4.5 * s))
        painter.drawLine(QPointF(14 * s, 2 * s), QPointF(11.5 * s, 4.5 * s))
    elif name == "person":
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(tint))
        painter.drawEllipse(QRectF(5 * s, 1.5 * s, 6 * s, 6 * s))
        path = QPainterPath()
        path.moveTo(2.5 * s, 14.5 * s)
        path.arcTo(QRectF(2.5 * s, 8 * s, 11 * s, 11 * s), 180, -180)
        painter.drawPath(path)
    elif name == "channel":
        # Avatar: a person inside a ring, the usual "channel" shorthand.
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(1.5 * s, 1.5 * s, 13 * s, 13 * s))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(tint))
        painter.drawEllipse(QRectF(6 * s, 4.6 * s, 4 * s, 4 * s))
        body = QPainterPath()
        body.moveTo(4.3 * s, 12 * s)
        body.arcTo(QRectF(4.3 * s, 8 * s, 7.4 * s, 7.4 * s), 180, -180)
        painter.drawPath(body)
    elif name == "download":
        # Down arrow into a tray.
        line(8, 2, 8, 9.5, 1.8)
        line(4.8, 6.3, 8, 9.6, 1.8)
        line(11.2, 6.3, 8, 9.6, 1.8)
        path = QPainterPath()
        path.moveTo(2.6 * s, 11 * s)
        path.lineTo(2.6 * s, 14 * s)
        path.lineTo(13.4 * s, 14 * s)
        path.lineTo(13.4 * s, 11 * s)
        painter.setPen(QPen(tint, 1.6 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap, _Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    elif name == "playlist":
        # Stacked rows, each with a leading marker.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(tint))
        for y in (3.4, 7.4, 11.4):
            painter.drawRoundedRect(QRectF(1.8 * s, (y + 0.2) * s, 2.4 * s, 2.4 * s),
                                    0.6 * s, 0.6 * s)
        painter.setPen(QPen(tint, 1.6 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        for y in (4.6, 8.6, 12.6):
            painter.drawLine(QPointF(6.2 * s, y * s), QPointF(13.6 * s, y * s))
    elif name == "history":
        # Clock face with a hand and a rewind arrow.
        painter.setPen(QPen(tint, 1.4 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(2 * s, 2 * s, 12 * s, 12 * s), 40 * 16, 300 * 16)
        line(8, 5, 8, 8.4, 1.7)
        line(8, 8.4, 10.6, 9.6, 1.7)
        line(1.6, 6.4, 2.6, 3.2, 1.7)
    elif name == "settings":
        # Sliders: three rails with a knob each.
        painter.setPen(QPen(tint, 1.4 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        for y, knob in ((4.2, 10.5), (8, 5.5), (11.8, 9)):
            painter.drawLine(QPointF(2 * s, y * s), QPointF(14 * s, y * s))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(tint))
            painter.drawEllipse(QRectF((knob - 1.3) * s, (y - 1.3) * s,
                                       2.6 * s, 2.6 * s))
            painter.setPen(QPen(tint, 1.4 * s, _Qt.PenStyle.SolidLine,
                                _Qt.PenCapStyle.RoundCap))
    elif name == "info":
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(2.5 * s, 2.5 * s, 11 * s, 11 * s))
        painter.setPen(QPen(tint, 1.7 * s, _Qt.PenStyle.SolidLine,
                            _Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(8 * s, 7 * s), QPointF(8 * s, 11 * s))
        painter.drawLine(QPointF(8 * s, 5 * s), QPointF(8 * s, 5.6 * s))
    elif name == "shield":
        path = QPainterPath()
        path.moveTo(8 * s, 1.5 * s)
        path.lineTo(13.5 * s, 3.5 * s)
        path.lineTo(13.5 * s, 8 * s)
        path.cubicTo(13.5 * s, 11.5 * s, 11 * s, 13.5 * s, 8 * s, 14.5 * s)
        path.cubicTo(5 * s, 13.5 * s, 2.5 * s, 11.5 * s, 2.5 * s, 8 * s)
        path.lineTo(2.5 * s, 3.5 * s)
        path.closeSubpath()
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        line(8, 5, 8, 9.5, 1.7)
    elif name == "lock":
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(3.5 * s, 7 * s, 9 * s, 6.5 * s), 1.4 * s, 1.4 * s)
        painter.drawArc(QRectF(5.2 * s, 2.8 * s, 5.6 * s, 6 * s), 0, 180 * 16)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(tint))
        painter.drawEllipse(QRectF(7.3 * s, 9.3 * s, 1.4 * s, 1.4 * s))
    elif name == "theme":
        painter.setPen(QPen(tint, 1.4 * s))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(2.5 * s, 2.5 * s, 11 * s, 11 * s))
        painter.save()
        clip = QPainterPath()
        clip.addEllipse(QRectF(2.5 * s, 2.5 * s, 11 * s, 11 * s))
        clip.addRect(QRectF(8 * s, 2.5 * s, 5.5 * s, 11 * s))
        clip = clip.simplified()
        painter.setClipPath(clip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(tint))
        painter.drawEllipse(QRectF(2.5 * s, 2.5 * s, 11 * s, 11 * s))
        painter.restore()

    painter.end()
    return pixmap


def nav_icon(name, color="#d8d8d8", size=20):
    """Icon for a sidebar row, drawn rather than a service logo."""
    return QIcon(glyph_icon(name, color, size))


def icon_label(text, icon, color="#ffffff", size=14, icon_size=16, bold=False,
               stretch=0):
    """A horizontal icon + text row that behaves as one widget.

    ``stretch`` decides how the row shares horizontal space with its
    neighbours. A heading next to other controls must pass 0, otherwise it
    expands and shoves them out of the row.
    """
    holder = QWidget()
    holder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)

    mark = _plain_label()
    mark.setPixmap(glyph_icon(icon, color, icon_size))
    mark.setFixedSize(icon_size, icon_size)
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    row.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)

    label = _plain_label(text, css=f"color: {color}; font-size: {size}px;"
                                   + (" font-weight: bold;" if bold else ""))
    row.addWidget(label, 1 if stretch else 0)

    if not stretch:
        # Hug the content instead of expanding.
        holder.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return holder


def service_icon(service, size=96):
    """Icon for a service, preferring a user-supplied logo when there is one."""
    logo = _user_logo(service)
    if logo:
        pixmap = QPixmap(logo)
        if not pixmap.isNull():
            if pixmap.width() != size or pixmap.height() != size:
                pixmap = pixmap.scaled(size, size,
                                       Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
            return QIcon(pixmap)
    return QIcon(_draw_service_icon(service, size))


def app_icon(service, size=64):
    """Window / taskbar icon for the app.

    Prefers a user-supplied ``assets/logo.png`` so the app can carry its own
    branding instead of the per-service mark. Falls back to the service icon
    when no logo file is present.
    """
    logo = _user_logo("logo")
    if logo:
        pixmap = QPixmap(logo)
        if not pixmap.isNull():
            if pixmap.width() != size or pixmap.height() != size:
                pixmap = pixmap.scaled(size, size,
                                       Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
            return QIcon(pixmap)
    return service_icon(service, size)


# Light theme mapping for label colours.
#
# Every label stores the stylesheet it was built with. Switching theme swaps
# the near-white greys for dark equivalents and back again, so toggling is
# lossless instead of a one-way trip. Only labels go through this: button
# stylesheets keep white text, because those sit on coloured buttons where
# dark text would be unreadable.
LIGHT_LABEL_SWAP = {
    "#ffffff": "#1c1c1c",
    "#e8e8e8": "#1c1c1c",
    "#dddddd": "#2b2b2b",
    "#cfcfcf": "#333333",
    "#aaaaaa": "#4a4a4a",
    "#9a9a9a": "#666666",
    "#888888": "#666666",
}


def swap_label_colors(css, dark):
    """Return ``css`` with its greys moved to the other theme."""
    def swap_one(match):
        raw = match.group(0)
        key = raw.lower()
        if dark:
            return raw
        return LIGHT_LABEL_SWAP.get(key, raw)

    return re.sub(r"#[0-9a-fA-F]{3,6}", swap_one, css)


# Qt hands a parent's background-color down to child QLabels, so a plain
# QLabel paints an opaque block over whatever is behind it - which is what
# put visible boxes around the card text and logos. Every label states its
# own transparent background instead.
_LABEL_BASE_CSS = "background-color: transparent; border: none;"

# Applied app-wide so no QLabel anywhere can inherit an opaque background.
GLOBAL_LABEL_CSS = "QLabel { background-color: transparent; border: none; }"


def _plain_label(text="", *, css="", align=None, word_wrap=False):
    """A QLabel that cannot be text-selected, never eats hover, never has a box.

    Three separate problems solved here: QLabel allows drag-selecting its
    text, it swallows the mouse hover the card needs, and it inherits a
    background from its parent. All three are turned off explicitly.
    """
    label = QLabel(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    # Any css passed in comes last so it can override the transparent default
    # (the COMING SOON badge needs its own background).
    label.setStyleSheet(_LABEL_BASE_CSS + css)
    # Keep the dark-theme version so the light theme can be derived from it.
    label.setProperty("vexBaseCss", label.styleSheet())
    if align is not None:
        label.setAlignment(align)
    if word_wrap:
        label.setWordWrap(True)
    return label


class ServiceCard(QFrame):
    """One whole tile - logo, title, blurb and badge behave as a single unit.

    Hover is handled on the card itself. The child labels are transparent to
    the mouse so pointing anywhere over the card, logo included, highlights
    the whole thing and clicking anywhere on it activates it.
    """

    def __init__(self, key, name, blurb, coming_soon, on_activate, on_explain):
        super().__init__()
        self.setObjectName("serviceCard")
        # Tall enough for logo + title + two-line blurb + badge without the
        # layout squeezing the widgets into each other.
        self.setFixedHeight(250)
        self._coming_soon = coming_soon
        self._on_activate = on_activate
        self._on_explain = on_explain

        self.setCursor(Qt.CursorShape.ArrowCursor if coming_soon
                      else Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        if coming_soon:
            self.setStyleSheet(
                "#serviceCard { background-color: #171717; "
                "border: 1px solid #242424; border-radius: 14px; } "
                + GLOBAL_LABEL_CSS)
        else:
            self.setStyleSheet(
                "#serviceCard { background-color: #1f1f1f; "
                "border: 1px solid #323232; border-radius: 14px; } "
                "#serviceCard:hover { background-color: #2a2a2a; "
                "border-color: #ff2e63; } " + GLOBAL_LABEL_CSS)

        column = QVBoxLayout(self)
        column.setContentsMargins(20, 22, 20, 18)
        column.setSpacing(12)
        column.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        logo = _plain_label()
        logo.setPixmap(service_icon(key, 72).pixmap(72, 72))
        logo.setFixedSize(72, 72)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Disabled paints the pixmap grey, marking the unbuilt services.
        logo.setEnabled(not coming_soon)
        column.addWidget(logo, 0, Qt.AlignmentFlag.AlignHCenter)

        title = _plain_label(
            name, align=Qt.AlignmentFlag.AlignCenter, word_wrap=True,
            css="font-size: 16px; font-weight: bold; color: "
                + ("#6f6f6f;" if coming_soon else "#ffffff;"))
        column.addWidget(title)

        body = _plain_label(
            blurb, align=Qt.AlignmentFlag.AlignCenter, word_wrap=True,
            css="font-size: 12px; color: "
                + ("#4f4f4f;" if coming_soon else "#9a9a9a;"))
        column.addWidget(body)

        column.addStretch()

        if coming_soon:
            badge = _plain_label(
                "COMING SOON", align=Qt.AlignmentFlag.AlignCenter,
                css="color: #171717; background-color: #6f6f6f; "
                    "border-radius: 4px; padding: 2px 10px; font-size: 10px; "
                    "font-weight: bold;")
            badge.setFixedHeight(20)
            column.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)

    def mousePressEvent(self, event):
        if self._coming_soon:
            self._on_explain()
        else:
            self._on_activate()


class ServicePicker(QWidget):
    """First-launch screen: choose a service, then open the main window."""

    def __init__(self, on_choose):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - Choose a service")
        # Top-left title-bar icon for the launch screen, matching the app icon.
        self.setWindowIcon(app_icon(SERVICE_YT, 64))
        self.setFixedSize(960, 520)
        self._on_choose = on_choose
        self._center()
        self.setStyleSheet("background-color: #141414; " + GLOBAL_LABEL_CSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 26, 28, 22)
        outer.setSpacing(6)

        title = _plain_label("Choose a service", align=Qt.AlignmentFlag.AlignCenter,
                           css="font-size: 26px; font-weight: bold; color: #ffffff;")
        outer.addWidget(title)

        subtitle = _plain_label(
            f"{APP_NAME} by {APP_AUTHOR} — pick where to download from.",
            align=Qt.AlignmentFlag.AlignCenter,
            css="font-size: 13px; color: #8a8a8a;")
        outer.addWidget(subtitle)
        outer.addSpacing(22)

        row = QHBoxLayout()
        row.setSpacing(18)
        self.cards = {}
        for key, name, blurb, coming_soon in SERVICES:
            card = ServiceCard(
                key, name, blurb, coming_soon,
                on_activate=lambda k=key: self._pick(k),
                on_explain=lambda k=key, n=name: self._explain_coming_soon(k, n),
            )
            row.addWidget(card, 1)
            self.cards[key] = card
        outer.addLayout(row)

        outer.addStretch()

        footer = QHBoxLayout()
        quit_button = QPushButton("Quit")
        quit_button.setFixedWidth(110)
        quit_button.setStyleSheet(
            "background-color: #2a2a2a; color: #cccccc; "
            "padding: 8px; border-radius: 6px;")
        quit_button.clicked.connect(self.close)
        footer.addWidget(quit_button)
        footer.addStretch()
        outer.addLayout(footer)

    def _pick(self, key):
        self.close()
        self._on_choose(key)

    def _explain_coming_soon(self, key, name):
        QMessageBox.information(
            self, f"{name} is not ready yet",
            f"{name} support is planned but not built yet.\n\n"
            "Right now only YouTube and YT Music downloads work.\n"
            "This is not a bug and there is no hidden setting to enable it.")

    def _center(self):
        screen = QApplication.primaryScreen().geometry()
        size = self.geometry()
        self.move((screen.width() - size.width()) // 2,
                  (screen.height() - size.height()) // 2)


class ModeSelectDialog(QWidget):
    """Legacy mode picker, kept for anyone importing it directly.

    Launching the app now shows :class:`ServicePicker` instead.
    """

    def __init__(self, on_choose):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - Choose a mode")
        self.setFixedSize(760, 460)
        self._on_choose = on_choose
        self.center_window()
        self.setStyleSheet(f"""
            background-color: #1E1E1E; color: #ffffff; font-size: 14px;
            QPushButton {{
                background-color: #2a2a2a; color: #ffffff;
                border: 1px solid #3a3a3a; border-radius: 8px;
                padding: 12px;
            }}
            QPushButton:hover {{ background-color: #ff2e63; border-color: #ff2e63; }}
            QLabel {{ color: #ffffff; }}
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("What do you want to download?")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #ff2e63;")
        layout.addWidget(title)

        subtitle = QLabel("Created by Vex — pick a mode to get started.")
        subtitle.setStyleSheet("font-size: 13px; color: #aaaaaa;")
        layout.addWidget(subtitle)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.buttons = {}
        for index, (key, icon, heading, blurb) in enumerate(MODE_CARDS):
            button = QPushButton(f"{icon}  {heading}\n{blurb}")
            button.setMinimumHeight(110)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key: self._on_choose(k))
            grid.addWidget(button, index // 2, index % 2)
            self.buttons[key] = button
        layout.addLayout(grid)

        layout.addStretch()

        footer = QHBoxLayout()
        quit_button = QPushButton("Quit")
        quit_button.setFixedWidth(120)
        quit_button.clicked.connect(self.close)
        footer.addWidget(quit_button)
        footer.addStretch()
        layout.addLayout(footer)

    def center_window(self):
        screen = QApplication.primaryScreen().geometry()
        size = self.geometry()
        self.move((screen.width() - size.width()) // 2,
                  (screen.height() - size.height()) // 2)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class VexDownloader(QWidget):
    def __init__(self, mode=MODE_VIDEO, service=SERVICE_YT):
        super().__init__()

        self.service = service
        self.setWindowTitle(self._window_title(service))
        self.setWindowIcon(app_icon(service, 64))
        self.setGeometry(100, 100, 980, 680)
        self.center_window()
        self.setStyleSheet("background-color: #1E1E1E; color: white; font-size: 14px; "+ GLOBAL_LABEL_CSS)

        # --- runtime state -------------------------------------------------
        ensure_clean_path()
        self.mode = mode
        self.download_path = default_download_dir()
        self.list_name = ""
        self._threads = []              # keeps worker threads alive
        self._active_downloads = 0
        # YouTube URL -> Spotify metadata, so a downloaded file can be tagged
        # with the Spotify title/artist/album after the fact.
        self._spotify_meta = {}
        self._cancel_event = threading.Event()  # set to stop all downloads
        self.has_ffmpeg = ffmpeg_available()
        self.signals = DownloaderSignals()
        self.signals.list_fetched.connect(self.on_list_fetched)
        self.signals.fetch_failed.connect(self.on_fetch_failed)
        self.signals.status_changed.connect(self.update_status)
        self.signals.log_message.connect(self.on_log_message)
        self.signals.history_saved.connect(self.record_history)
        self.signals.download_finished.connect(self.on_download_finished)
        self.signals.auth_tested.connect(self.on_auth_tested)
        self.signals.progress_updated.connect(self.on_progress_updated)
        self.signals.spotify_tested.connect(self._on_spotify_tested)
        self.signals.spotify_auth_done.connect(self._on_spotify_auth_done)

        # Overall download progress bar, shown at the bottom while busy.
        self._progress = ProgressTracker()
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setStyleSheet(
            "QProgressBar { background-color: #2a2a2a; border: none; "
            "border-radius: 5px; color: #ffffff; }"
            "QProgressBar::chunk { background-color: #ff2e63; "
            "border-radius: 5px; }")
        self._progress_eta = QLabel("")
        self._progress_eta.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        self._progress_eta.setMinimumWidth(340)

        # Main Layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        layout = QHBoxLayout()

        # Sidebar Navigation
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setStyleSheet(
            "background-color: #252525; color: white; border: none; font-weight: bold;"
            " QListWidget::item { padding: 8px 6px; }"
            " QListWidget::item:selected { background-color: #3a3a3a;"
            " border-left: 3px solid #ff2e63; }")
        # Each row gets its own drawn mark. Reusing the YouTube logo for all
        # three download tabs made them impossible to tell apart at a glance,
        # so they are purpose-drawn instead. Service logos stay on the
        # launcher cards and the window icon, where they belong.
        for label, mark in (
            ("Downloader", "download"),
            ("Playlist", "playlist"),
            ("Channel", "channel"),
            ("History", "history"),
            ("Settings", "settings"),
            ("About", "book"),
        ):
            self.sidebar.addItem(self._nav_item(nav_icon(mark), label))
        layout.addWidget(self.sidebar)

        # Platform switcher - change service without relaunching.
        self.service_switcher = QComboBox()
        for key, label, _blurb, coming_soon in SERVICES:
            if not coming_soon:
                self.service_switcher.addItem(label, key)
        self.service_switcher.setCurrentIndex(
            max(0, self.service_switcher.findData(service)))
        self.service_switcher.currentIndexChanged.connect(self._on_service_picked)
        self.service_switcher.setFixedWidth(190)
        self.service_switcher.setStyleSheet(
            "background-color: #2a2a2a; color: white; padding: 6px; "
            "border: 1px solid #3a3a3a; border-radius: 5px;")
        layout.addWidget(self.service_switcher)

        # Page Manager (Stacked Widget)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)

        main_layout.addLayout(layout)

        # Footer: overall progress bar + ETA, shown only while downloading.
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 4, 12, 8)
        footer.addWidget(self._progress_bar, 1)
        footer.addWidget(self._progress_eta)
        main_layout.addLayout(footer)
        self._progress_bar.setVisible(False)
        self._progress_eta.setVisible(False)

        # Add Pages - download modes come first, in MODE_INDEX
        # order, so a mode row always lines up with its page.
        self.downloader_page = self.create_downloader_page(MODE_VIDEO)
        self.playlist_page = self.create_downloader_page(MODE_PLAYLIST)
        self.channel_page = self.create_downloader_page(MODE_CHANNEL)
        self.history_page = self.create_history_page()
        self.settings_page = self.create_settings_page()
        self.about_page = self.create_about_page()

        for page in (self.downloader_page, self.playlist_page, self.channel_page,
                     self.history_page, self.settings_page,
                     self.about_page):
            self.pages.addWidget(page)

        self.sidebar.currentRowChanged.connect(self.switch_page)
        self.select_mode(mode)

    def _set_auth_busy(self):
        """Grey out the test button while a session check is in flight."""
        for widget in self.findChildren(QPushButton):
            if widget.text() == "Test signed-in session":
                widget.setEnabled(False)
                # Re-enable defensively in case the worker dies without
                # emitting anything back.
                QTimer.singleShot(8000, lambda: widget.setEnabled(True))
                break

    # ------------------------------------------------------------------ #
    # Generic helpers
    # ------------------------------------------------------------------ #
    def center_window(self):
        """Centers the application window on the screen."""
        screen = QApplication.primaryScreen().geometry()
        window_size = self.geometry()
        x = (screen.width() - window_size.width()) // 2
        y = (screen.height() - window_size.height()) // 2
        self.move(x, y)

    @staticmethod
    def _nav_item(icon, text):
        """A sidebar row carrying an optional icon."""
        item = QListWidgetItem(icon, text)
        return item

    @staticmethod
    def _window_title(service):
        platform = SERVICE_LABELS.get(service, service)
        return f"{APP_NAME} - {platform} (v{APP_VERSION}) - by {APP_AUTHOR}"

    def set_service(self, service):
        """Switch platform without restarting the app.

        Everything that depends on the platform - the window title, the URL
        box placeholders and the accept/reject rules for pasted links - is
        refreshed here. Any link already in the boxes is deliberately left
        alone: it is still there if the user switches back.
        """
        if service not in SERVICE_LABELS or service == self.service:
            return

        self.service = service
        self.setWindowTitle(self._window_title(service))
        if self.service_switcher is not None:
            index = self.service_switcher.findData(service)
            if index >= 0 and index != self.service_switcher.currentIndex():
                self.service_switcher.blockSignals(True)
                self.service_switcher.setCurrentIndex(index)
                self.service_switcher.blockSignals(False)

        # Every mode's URL box now expects a different kind of link.
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            if hasattr(page, "url_input") and hasattr(page, "mode"):
                page.url_input.setPlaceholderText(
                    url_hint(page.mode, service))
        log(f"[service] switched to {service}")

    def switch_page(self, index):
        """Switch between pages when sidebar is clicked."""
        self.pages.setCurrentIndex(index)

    def _on_service_picked(self, _index):
        """Platform combo box changed."""
        self.set_service(self.service_switcher.currentData())

    def select_mode(self, mode):
        """Point the sidebar at the page matching mode and focus its URL box."""
        if mode not in MODE_INDEX:
            mode = MODE_VIDEO
        self.mode = mode
        index = MODE_INDEX[mode]
        self.sidebar.setCurrentRow(index)
        page = self.pages.widget(index)
        if hasattr(page, "url_input"):
            page.url_input.setFocus()
            page.url_input.setPlaceholderText(url_hint(mode, self.service))

    def _start_worker(self, target, *args):
        """Run ``target`` on a daemon thread and keep a reference to it."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()
        # Drop finished threads so the list does not grow forever.
        self._threads = [t for t in self._threads if t.is_alive()]

    def _run_queued(self, jobs):
        """Run download jobs through a small pool instead of all at once.

        ``jobs`` is a list of argument tuples for :meth:`download_video`.
        Returns immediately; each job emits its own progress signals.
        """
        queue: "Queue[tuple]" = Queue()
        for job in jobs:
            queue.put(job)

        def worker():
            while True:
                try:
                    job = queue.get(timeout=0.5)
                except Empty:
                    return
                try:
                    page, row, title = job[0], job[1], job[2]
                    if self._cancel_event.is_set():
                        # Already-cancelled batch: never start new work. Still
                        # emit the signals so the busy counter unwinds and the
                        # UI unlocks once every job has been drained.
                        self.signals.status_changed.emit(page, row, "Cancelled \u23F9")
                        self.signals.history_saved.emit(title, "Cancelled \u23F9", "")
                        self.signals.download_finished.emit()
                        continue
                    # Stagger the very first few requests as well, otherwise
                    # every worker hits YouTube in the same instant.
                    time.sleep(DELAY_BETWEEN_DOWNLOADS)
                    # download_video emits download_finished itself when it
                    # completes, so it must not be emitted here as well.
                    self.download_video(*job)
                except Exception:  # noqa: BLE001 - a worker must never die
                    log(traceback.format_exc())
                finally:
                    queue.task_done()

        worker_count = min(MAX_PARALLEL_DOWNLOADS, len(jobs))
        for _ in range(worker_count):
            thread = threading.Thread(target=worker, daemon=True)
            self._threads.append(thread)
            thread.start()

    def on_log_message(self, message):
        log(message)

    def update_status(self, page, row, status):
        """Set the Status column of ``row`` (must run on the GUI thread)."""
        if page is None or not hasattr(page, "table"):
            return
        item = page.table.item(row, 1)
        if item is None:
            item = QTableWidgetItem()
            page.table.setItem(row, 1, item)
        item.setText(status)

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    def create_downloader_page(self, mode=MODE_VIDEO):
        """Build a download page.

        One page exists per mode; they all share this layout and only differ in
        the placeholder text of the URL box.
        """
        page = QWidget()
        page.mode = mode
        page.list_name = ""
        page.table = None
        layout = QVBoxLayout()

        # URL Input & Fetch Button
        url_layout = QHBoxLayout()
        page.url_input = QLineEdit()
        page.url_input.setPlaceholderText(url_hint(mode, self.service))
        page.url_input.returnPressed.connect(lambda: self.fetch_list_thread(page))
        url_layout.addWidget(page.url_input)

        page.fetch_button = QPushButton("Load")
        page.fetch_button.setStyleSheet(
            "background-color: #3498db; color: white; padding: 8px; border-radius: 5px;")
        page.fetch_button.clicked.connect(lambda: self.fetch_list_thread(page))
        url_layout.addWidget(page.fetch_button)

        layout.addLayout(url_layout)

        # Save Path & Browse Button
        path_layout = QHBoxLayout()
        page.path_label = icon_label(
            f"Save To: {self.download_path}", "folder", "#dddddd", 13, stretch=1)
        # Keep a handle on the text half so Browse can update it.
        page.path_label_text = page.path_label.findChild(QLabel)
        path_layout.addWidget(page.path_label)

        page.browse_button = QPushButton("Browse")
        page.browse_button.setStyleSheet(
            "background-color: #f39c12; color: white; padding: 8px; border-radius: 5px;")
        page.browse_button.clicked.connect(lambda: self.browse_folder(page))
        path_layout.addWidget(page.browse_button)

        layout.addLayout(path_layout)

        # Format & Quality Selection
        format_quality_layout = QHBoxLayout()
        page.format_dropdown = QComboBox()
        page.format_dropdown.addItems(["mp4", "mp3"])
        page.quality_dropdown = QComboBox()
        page.quality_dropdown.addItems(["best", "1080", "720", "480"])
        format_quality_layout.addWidget(icon_label("Format:", "film", "#dddddd", 13))
        format_quality_layout.addWidget(page.format_dropdown)
        format_quality_layout.addWidget(icon_label("Quality:", "quality", "#dddddd", 13))
        format_quality_layout.addWidget(page.quality_dropdown)
        page.format_dropdown.currentTextChanged.connect(
            lambda text, p=page: self._on_format_changed(p, text))
        layout.addLayout(format_quality_layout)

        # Limit - only meaningful for playlists/channels, but shown everywhere
        # so the control never moves around between pages.
        limit_layout = QHBoxLayout()
        page.limit_spin = QSpinBox()
        page.limit_spin.setRange(0, 5000)
        page.limit_spin.setValue(200)
        page.limit_spin.setSpecialValueText("All")
        page.limit_spin.setEnabled(mode in (MODE_PLAYLIST, MODE_CHANNEL))
        limit_layout.addWidget(_plain_label(
            "Max videos:", css="color: #dddddd; font-size: 13px;"))
        limit_layout.addWidget(page.limit_spin)
        limit_layout.addStretch()
        layout.addLayout(limit_layout)

        # FFmpeg availability hint. The container is added to the layout exactly once;
        # only its contents are swapped, so the hint can never drift out of
        # position when the format changes.
        page.ffmpeg_label = None
        page.ffmpeg_hint_box = QWidget()
        page.ffmpeg_hint_layout = QVBoxLayout(page.ffmpeg_hint_box)
        page.ffmpeg_hint_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(page.ffmpeg_hint_box)
        self._set_ffmpeg_hint(
            page, "check", "#2ecc71",
            "FFmpeg detected - MP4 merging and MP3 extraction are available."
            if self.has_ffmpeg else
            "FFmpeg is NOT installed. Only pre-muxed MP4 streams can be saved.")

        # Table
        page.table = QTableWidget(0, 3)
        page.table.setHorizontalHeaderLabels(["Title", "Status", "URL"])
        page.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        page.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        page.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        page.table.setStyleSheet(
            "QHeaderView::section { background-color: #444; color: white; "
            "padding: 5px; font-weight: bold; }")
        layout.addWidget(page.table)

        # Buttons
        button_layout = QHBoxLayout()

        page.download_button = QPushButton("Download Selected")
        page.download_button.clicked.connect(lambda: self.start_download_thread(page))
        page.download_button.setStyleSheet(
            "background-color: #2ecc71; color: white; padding: 8px; border-radius: 5px;")
        button_layout.addWidget(page.download_button)

        page.download_all_button = QPushButton("Download All")
        page.download_all_button.clicked.connect(lambda: self.download_everything(page))
        page.download_all_button.setStyleSheet(
            "background-color: #27ae60; color: white; padding: 8px; border-radius: 5px;")
        button_layout.addWidget(page.download_all_button)

        page.select_all_button = QPushButton("Select All")
        page.select_all_button.clicked.connect(lambda: page.table.selectAll())
        page.select_all_button.setStyleSheet(
            "background-color: #1abc9c; color: white; padding: 8px; border-radius: 5px;")
        button_layout.addWidget(page.select_all_button)

        page.clear_button = QPushButton("Clear List")
        page.clear_button.clicked.connect(lambda: self.clear_list(page))
        page.clear_button.setStyleSheet(
            "background-color: #e67e22; color: white; padding: 8px; border-radius: 5px;")
        button_layout.addWidget(page.clear_button)

        page.open_folder_button = QPushButton("Open Folder")
        page.open_folder_button.clicked.connect(lambda: self.open_save_folder(page))
        page.open_folder_button.setStyleSheet(
            "background-color: #9b59b6; color: white; padding: 8px; border-radius: 5px;")
        button_layout.addWidget(page.open_folder_button)

        page.cancel_button = QPushButton("Cancel")
        page.cancel_button.clicked.connect(self.cancel_downloads)
        page.cancel_button.setStyleSheet(
            "background-color: #c0392b; color: white; padding: 8px; border-radius: 5px;")
        page.cancel_button.setEnabled(False)
        button_layout.addWidget(page.cancel_button)

        layout.addLayout(button_layout)
        page.setLayout(layout)
        self._on_format_changed(page, page.format_dropdown.currentText())
        return page

    def create_history_page(self):
        """Creates the Download History Page."""
        page = QWidget()
        layout = QVBoxLayout()

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            ["When", "Title", "Status", "File Path"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.setStyleSheet(
            "QHeaderView::section { background-color: #444; color: white; "
            "padding: 5px; font-weight: bold; }")
        # Double clicking a row opens whatever it points at.
        self.history_table.cellDoubleClicked.connect(
            lambda row, _col: self.open_history_row(row))
        layout.addWidget(self.history_table)

        self.history_hint = _plain_label(
            "Newest first. Double-click a row to open the saved file.\n"
            "History is kept between sessions and cleared only by you.",
            word_wrap=True, css="color: #aaaaaa; font-size: 12px;")
        layout.addWidget(self.history_hint)

        buttons = QHBoxLayout()
        self.open_history_button = QPushButton("Open Selected")
        self.open_history_button.clicked.connect(
            lambda: self.open_history_row(self.history_table.currentRow()))
        self.open_history_button.setStyleSheet(
            "background-color: #9b59b6; color: white; padding: 8px; border-radius: 5px;")
        buttons.addWidget(self.open_history_button)

        self.clear_history_button = QPushButton("Clear History")
        self.clear_history_button.clicked.connect(self.clear_history)
        self.clear_history_button.setStyleSheet(
            "background-color: #e74c3c; color: white; padding: 8px; border-radius: 5px;")
        buttons.addWidget(self.clear_history_button)
        layout.addLayout(buttons)

        self._load_history()
        page.setLayout(layout)
        return page

    def clear_history(self):
        """Clears the download history, on screen and on disk."""
        self.history_table.setRowCount(0)
        self._save_history()

    def record_history(self, title, status, path=""):
        """Add one entry to the history, newest at the top.

        Called on the GUI thread via the history_saved signal, for every
        finished download - successful or not.
        """
        stamp = datetime.now().strftime("%d/%m %H:%M")
        self.history_table.insertRow(0)
        self.history_table.setItem(0, 0, QTableWidgetItem(stamp))
        self.history_table.setItem(0, 1, QTableWidgetItem(title))
        self.history_table.setItem(0, 2, QTableWidgetItem(status))
        self.history_table.setItem(0, 3, QTableWidgetItem(path))
        self._save_history()

    def open_history_row(self, row):
        """Open the file (or its folder) for a history row."""
        if row < 0 or row >= self.history_table.rowCount():
            return
        item = self.history_table.item(row, 3)
        path = item.text().strip() if item else ""
        if not path:
            QMessageBox.information(
                self, "Nothing to open",
                "This entry has no saved file - the download did not finish.")
            return
        if os.path.isfile(path):
            os.startfile(path)  # noqa: S606 - Windows only, intentional
        elif os.path.isdir(path):
            os.startfile(path)  # noqa: S606
        else:
            QMessageBox.warning(
                self, "File missing",
                f"That file is no longer on disk:\n{path}\n\n"
                "It may have been moved or deleted.")

    def _history_path(self):
        return os.path.join(CONFIG_DIR, "history.json")

    def _save_history(self):
        """Persist history so it survives closing the app."""
        rows = []
        for r in range(self.history_table.rowCount()):
            rows.append([
                self.history_table.item(r, c).text() if self.history_table.item(r, c) else ""
                for c in range(4)
            ])
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            temp = self._history_path() + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(rows, handle)
            os.replace(temp, self._history_path())
        except Exception as exc:  # noqa: BLE001 - history is not critical
            log(f"[history] Could not save: {exc}")

    def _load_history(self):
        """Restore history saved by a previous session."""
        try:
            with open(self._history_path(), "r", encoding="utf-8") as handle:
                rows = json.load(handle)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - a corrupt file must not block startup
            log("[history] Could not read saved history.")
            return
        for entry in rows:
            if not isinstance(entry, list) or len(entry) != 4:
                continue
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            for col, value in enumerate(entry):
                self.history_table.setItem(row, col, QTableWidgetItem(str(value)))

    def clear_list(self, page):
        page.table.setRowCount(0)
        page.list_name = ""

    def browse_folder(self, page):
        """Allows user to select a save folder."""
        global download_path
        chosen = QFileDialog.getExistingDirectory(self, "Select Save Folder", self.download_path)
        if not chosen:
            return  # user cancelled - keep the previous folder
        self.download_path = chosen
        download_path = chosen
        page.path_label_text.setText(f"Save To: {chosen}")

    def open_save_folder(self, page):
        """Open the folder this page saves into, in Explorer."""
        base = os.path.join(self.download_path, sanitize_name(page.list_name or "Vex_Download"))
        target = base
        while target and not os.path.isdir(target):
            parent = os.path.dirname(target)
            if parent == target:
                target = self.download_path
                break
            target = parent
        if not target or not os.path.isdir(target):
            QMessageBox.information(self, "Nothing yet",
                                    "No downloads have been saved to that folder yet.")
            return
        os.startfile(target)  # noqa: S606 - Windows only, intentional

    def _on_format_changed(self, page, text):
        """Quality only applies to MP4 downloads; MP3 is audio only."""
        page.quality_dropdown.setEnabled(text != "mp3")

        if self.has_ffmpeg:
            self._set_ffmpeg_hint(page, "check", "#2ecc71",
                                  "FFmpeg detected - MP4 merging and MP3 "
                                  "extraction are available.")
        elif text == "mp3":
            self._set_ffmpeg_hint(page, "cross", "#e74c3c",
                                  "FFmpeg is NOT installed. MP3 extraction "
                                  "requires it - install it and restart the "
                                  "app, otherwise downloads will fail.")
        else:
            self._set_ffmpeg_hint(page, "warning", "#f39c12",
                                  "FFmpeg is NOT installed. Only pre-muxed "
                                  "MP4 streams can be saved (no separate "
                                  "audio/video merging).")

    @staticmethod
    def _set_ffmpeg_hint(page, icon, color, text):
        """Install the FFmpeg hint as an icon + message row.

        Called once at page build and again whenever the format changes. Only
        the contents of the hint box are replaced, so the row stays put.
        """
        box_layout = page.ffmpeg_hint_layout
        while box_layout.count():
            stale = box_layout.takeAt(0).widget()
            if stale is not None:
                stale.setParent(None)
                stale.deleteLater()

        page.ffmpeg_label = icon_label(text, icon, color, 12, icon_size=15)
        box_layout.addWidget(page.ffmpeg_label)

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #
    def fetch_list_thread(self, page):
        """GUI side of 'Load': validate input then hand off to a worker."""
        if self._active_downloads:
            QMessageBox.information(
                self, "Downloads running",
                "Wait for the current downloads to finish before loading a new list "
                "(loading a new list would discard the running rows)."
            )
            return

        url = page.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please paste a YouTube URL.")
            return

        url, error = normalize_url(url, self.service)
        if error:
            QMessageBox.warning(self, "Invalid URL", error)
            return

        page.url_input.setText(url)

        # Pasting the wrong kind of link is the most common mistake, so bounce
        # the user to the page that actually fits the URL instead of failing.
        guessed = classify_url(url)
        if guessed and guessed != page.mode and guessed in MODE_INDEX:
            self.select_mode(guessed)
            page = self.pages.widget(MODE_INDEX[guessed])
            page.url_input.setText(url)
            log(f"[fetch] switched to {guessed}")
            log(f"[fetch] URL looks like a {guessed} - switched page automatically.")

        page.fetch_button.setEnabled(False)
        self._start_worker(self._fetch_worker, page, url)

    def _fetch_worker(self, page, url):
        """Background extraction (never touches the GUI)."""
        try:
            mode = page.mode

            # Spotify is not downloadable, so it takes a different route
            # entirely: read the track list from Spotify, then locate each one
            # on YouTube and hand those URLs to the normal download pipeline.
            if self.service == SERVICE_SPOTIFY:
                self._fetch_spotify(page, url)
                return

            # "Will this expand into many videos?" decides whether yt-dlp needs
            # to walk a list. Asking for extract_flat on a *single* video makes
            # yt-dlp treat the video as an unfinished playlist and it never
            # returns, so single items must always be fully extracted.
            many = is_list_url(url)
            use_flat = many

            opts = {
                "quiet": True,             # progress handled by us / the logger
                "no_warnings": False,
                "extract_flat": "in_playlist" if use_flat else False,
                "ignoreerrors": True,      # a single dead entry must not kill the batch
                "noplaylist": not many,
                "logger": ConsoleLogger(),
                **js_runtime_opts(),
                **auth_opts(),
            }

            # A hard cap keeps a 4000 upload channel from hanging the UI
            # forever; 0 means "everything".
            limit = getattr(page, "limit_spin", None)
            cap = limit.value() if limit is not None else 0
            if use_flat and cap > 0:
                opts["playlistend"] = cap

            # A "channel" is resolved differently per service.
            #
            # YouTube: a bare channel root returns the page tabs (Home, Videos,
            # Playlists) rather than uploads, so the /videos tab has to be found
            # first.
            #
            # SoundCloud: an artist profile root already resolves to the
            # track list, so running the YouTube tab resolver would append
            # "/videos" and produce a 404.
            is_channel = mode == MODE_CHANNEL or classify_url(url) == MODE_CHANNEL
            target = url
            if is_channel:
                if service_of_host(urlsplit(url).hostname or "") == SERVICE_SOUNDCLOUD:
                    log("[fetch] SoundCloud artist profile - using it directly.")
                else:
                    log("[fetch] Resolving the channel's video tab...")
                    with yt_dlp.YoutubeDL({**opts, "extract_flat": False,
                                           "playlistend": 1}) as probe:
                        target = resolve_channel_tab(probe, url)
                    log(f"[fetch] Channel tab: {target}")
                opts["extract_flat"] = "in_playlist"
                opts["noplaylist"] = False

            with yt_dlp.YoutubeDL(opts) as ydl:
                info_dict = ydl.extract_info(target, download=False)

            if not info_dict:
                raise ValueError("yt-dlp could not retrieve any information for that URL.")

            if info_dict.get("_type") == "playlist" and not info_dict.get("entries"):
                # Most often this is a SoundCloud artist with no public tracks,
                # rather than something the user did wrong, so say both.
                if self.service == SERVICE_SOUNDCLOUD:
                    raise ValueError(
                        "That SoundCloud artist returned no tracks.\n\n"
                        "SoundCloud only exposes tracks it can still serve, "
                        "so some artists look empty even though the profile "
                        "exists. Check the profile in a browser - if it shows "
                        "tracks there, they are not currently available."
                    )
                raise ValueError(
                    "That playlist is private, empty or has no downloadable entries.")

            raw_title = info_dict.get("title") or "Unknown_List"

            entries = info_dict.get("entries")
            if entries is None:
                # Single video: the result IS the video.
                entries = [info_dict]

            items = []
            skipped = 0
            for entry in entries:
                if not entry:
                    skipped += 1
                    continue
                if entry.get("_type") == "url" and not entry.get("webpage_url") \
                        and not entry.get("formats"):
                    entry = ydl.sanitize_info(entry)
                video_url = self._extract_video_url(entry)
                if not video_url:
                    skipped += 1
                    log(f"[fetch] Skipped unusable entry: "
                        f"{(entry.get('title') or entry.get('id') or 'unknown')}")
                    continue
                title = entry.get("title") or f"Video {len(items) + 1}"
                items.append((title, video_url))

            if skipped:
                log(f"[fetch] Skipped {skipped} unavailable/invalid entry/entries.")
            if not items:
                raise ValueError(
                    "No downloadable videos were found here.\n\n"
                    "If this is a channel, try pasting the channel's /videos tab, "
                    "or raise the limit above."
                )

            log(f"[fetch] Fetched {len(items)} video(s) from '{raw_title}'.")
            self.signals.list_fetched.emit(items, raw_title)

        except DownloadError as exc:
            log(traceback.format_exc())
            self.signals.fetch_failed.emit(self._friendly_error(exc))
        except Exception as exc:  # noqa: BLE001 - report anything to the user
            log(traceback.format_exc())
            self.signals.fetch_failed.emit(str(exc))

    def _fetch_spotify(self, page, url):
        """Turn a Spotify link into a list of matched YouTube URLs."""
        kind, ident = spotify_urls(url)
        if not kind or not ident:
            self.signals.fetch_failed.emit(
                "That does not look like a Spotify link.\n\n"
                "Try an open.spotify.com/track, /playlist, /album or "
                "/artist link.")
            return

        limit = getattr(page, "limit_spin", None)
        cap = limit.value() if limit is not None else 0

        tracks = spotify_tracks(kind, ident, cap)
        if not tracks:
            self.signals.fetch_failed.emit(
                "Spotify returned no tracks for that link. It may be empty or "
                "private.")
            return

        log(f"[spotify] {kind}/{ident}: {len(tracks)} track(s) from Spotify.")

        items, unmatched = [], 0
        for index, track in enumerate(tracks):
            youtube_url, confidence, matched_title = find_youtube_for_track(track)
            if not youtube_url or confidence < 0.55:
                # Never silently substitute a different song.
                unmatched += 1
                log(f"[spotify] no confident match for "
                    f"{track.get('artist')} - {track.get('title')}")
                continue

            artist = ", ".join(track.get("artists") or [track.get("artist") or ""])
            display = f"{artist} - {track.get('title')}".strip(" -")
            items.append((display, youtube_url))
            self._spotify_meta[youtube_url] = track
            # Progress so a long playlist does not look frozen.
            self.signals.log_message.emit(
                f"[spotify] Matched {index + 1}/{len(tracks)}: {display}")

        if not items:
            self.signals.fetch_failed.emit(
                f"None of the {len(tracks)} Spotify track(s) could be "
                "matched confidently on YouTube.\n\n"
                "This usually means the audio is not on YouTube under that "
                "name, or a live/cover version was found instead."
            )
            return

        if unmatched:
            log(f"[spotify] {unmatched} track(s) had no confident YouTube "
                f"match and were skipped.")

        raw_title = kind.capitalize()
        try:
            raw_title = (spotify_api(f"{kind}s/{ident}").get("name")
                         or raw_title) if kind != "artist" else "Spotify artist"
        except Exception:  # noqa: BLE001 - the name is cosmetic
            pass
        raw_title = f"Spotify - {raw_title}"

        log(f"[spotify] Matched {len(items)}/{len(tracks)} track(s) on YouTube.")
        self.signals.list_fetched.emit(items, raw_title)

    @staticmethod
    def _friendly_error(exc):
        """Turn the most common yt-dlp failures into something actionable."""
        text = str(exc)
        lowered = text.lower()
        if "404" in lowered and "soundcloud" in lowered:
            return (
                "That SoundCloud profile could not be found.\n\n"
                "Check the spelling of the artist name in the URL - "
                "SoundCloud profiles are case-sensitive in the address bar."
            )
        if is_drm_error(exc):
            return (
                "This track is DRM-protected and cannot be downloaded.\n\n"
                "SoundCloud marks some uploads as DRM-protected, and no tool "
                "can save them. Try a different track."
            )
        if "private" in lowered and _auth_browser == "none":
            return (
                "That playlist is private.\n\n"
                "Signing in would let the app see it, since it uses the YouTube "
                "session already saved in your browser.\n\n"
                "Go to Settings -> Account, pick your browser, make sure you are "
                "logged into YouTube there, then try again."
            )
        if "members-only" in lowered or "join this channel" in lowered:
            return (
                "That content is members-only, so it cannot be downloaded.\n\n"
                "Joining with a paid membership unlocks it in the browser, but "
                "the download tool cannot bypass that - you would need to use the "
                "channel's own download feature."
            )
        if "sign in to confirm" in lowered or "bot" in lowered:
            return (
                "YouTube asked this computer to prove it is not a bot.\n\n"
                "Install Deno (winget install DenoLand.Deno) and try again - it "
                "gives yt-dlp the JavaScript runtime YouTube now requires."
            )
        if "private" in lowered:
            return (
                "That content is private and the signed-in session could not "
                "reach it.\n\n"
                "Check you are actually logged into YouTube in the browser you "
                "selected under Settings -> Account, and close that browser so "
                "its cookies can be read."
            )
        if "unavailable" in lowered or "removed" in lowered:
            return "That video or playlist is no longer available."
        if "age" in lowered and "confirm" in lowered:
            return "That video is age restricted and cannot be fetched without a login."
        return text

    @staticmethod
    def _extract_video_url(entry):
        """Build a valid, complete watch URL for an entry."""
        candidates = [
            entry.get("webpage_url"),
            entry.get("original_url"),
            entry.get("url"),
        ]

        video_id = entry.get("id") or ""

        for candidate in candidates:
            if not candidate or not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
            # With extract_flat, 'url' holds the bare 11 character video id.
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                video_id = candidate
                break

        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return None

    def on_list_fetched(self, items, name):
        """GUI thread: fill the active page's table with the fetched entries."""
        page = self.pages.currentWidget()
        if not hasattr(page, "table"):
            page = self.downloader_page

        page.list_name = name
        self.list_name = name

        page.table.setRowCount(0)
        for title, url in items:
            row = page.table.rowCount()
            page.table.insertRow(row)
            page.table.setItem(row, 0, QTableWidgetItem(title))
            page.table.setItem(row, 1, QTableWidgetItem("Pending"))
            page.table.setItem(row, 2, QTableWidgetItem(url))

        page.fetch_button.setEnabled(True)

        # A single video needs no selection: select its one row so the user can
        # just press Download. Multi-item lists keep everything selected too,
        # since picking hundreds of rows by hand is not reasonable.
        page.table.selectAll()

        if len(items) == 1:
            page.download_button.setText("Download This Video")
            message = (
                f"Ready to download:\n{items[0][0]}\n\n"
                "Press 'Download This Video', or pick a different format on the left."
            )
        else:
            page.download_button.setText("Download Selected")
            message = (
                f"{len(items)} video(s) loaded from:\n{sanitize_name(name)}\n\n"
                "All of them are selected. Press 'Download All' to grab every "
                "one, or click rows to pick just some of them."
            )

        QMessageBox.information(self, "Loaded", message)

    def on_fetch_failed(self, message):
        page = self.pages.currentWidget()
        if hasattr(page, "fetch_button"):
            page.fetch_button.setEnabled(True)
        QMessageBox.critical(self, "Load Failed", f"Could not load that URL:\n{message}")

    # ------------------------------------------------------------------ #
    # Downloading
    # ------------------------------------------------------------------ #
    def download_everything(self, page):
        """Select every row, then download - the one-click path."""
        if page.table.rowCount() == 0:
            QMessageBox.information(
                self, "Nothing loaded",
                "Load a video or playlist first, then press Download All.")
            return
        page.table.selectAll()
        self.start_download_thread(page)

    def cancel_downloads(self):
        """Stop the current batch: in-flight transfers abort, queued rows drop.

        In-flight downloads die because the progress hook raises CancelledError
        the next time yt-dlp reports a chunk, which propagates out and kills the
        transfer. Rows still waiting in the queue are skipped by the worker and
        marked Cancelled. Each skipped row still emits download_finished so the
        busy counter unwinds and the buttons unlock once everything drains.
        """
        if not self._active_downloads:
            return
        self._cancel_event.set()
        log("[cancel] cancel requested - aborting active downloads")

        # Immediate feedback: the Cancel button greys out so a second press
        # does not queue another cancel, and the ETA bar drops to a hint.
        for index in (0, 1, 2, 3):
            widget = self.pages.widget(index)
            if hasattr(widget, "cancel_button"):
                widget.cancel_button.setEnabled(False)
        self._progress_eta.setText("Cancelling\u2026")

    def start_download_thread(self, page):
        """Entry point of the 'Download Selected' button (GUI thread)."""
        selected_rows = sorted({index.row() for index in
                                page.table.selectionModel().selectedRows()})

        if not selected_rows:
            QMessageBox.warning(
                self, "No Selection",
                "Please select at least one video first.\n"
                "Click a row to select it, or press 'Select All' for everything."
            )
            return

        if self._active_downloads:
            QMessageBox.information(self, "Busy",
                                    "Downloads are still running. Please wait for them to finish.")
            return

        file_format = page.format_dropdown.currentText()
        if file_format == "mp3" and not self.has_ffmpeg:
            answer = QMessageBox.question(
                self, "FFmpeg required",
                "FFmpeg was not found on your PATH, so the MP3 conversion will fail.\n\n"
                "Install FFmpeg and restart the app, or continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        # Read every widget value here, on the GUI thread, and pass plain data
        # to the workers.
        quality = page.quality_dropdown.currentText()
        base_folder = self.download_path
        name = page.list_name or "Vex_Download"

        jobs = []
        for row in selected_rows:
            item = page.table.item(row, 2)
            video_url = item.text().strip() if item else ""
            title_item = page.table.item(row, 0)
            title = title_item.text() if title_item else video_url
            if not video_url:
                self.update_status(page, row, "Failed \u274C")
                continue
            self.update_status(page, row, "Queued")
            jobs.append((page, row, title, video_url,
                         file_format, quality, base_folder, name))

        if not jobs:
            page.download_button.setEnabled(True)
            return

        self._active_downloads = len(jobs)
        page.download_button.setEnabled(False)
        page.download_all_button.setEnabled(False)
        page.table.setEnabled(False)
        page.cancel_button.setEnabled(True)

        # Fresh progress window for this batch.
        self._cancel_event.clear()
        self._progress.reset()
        self._progress_bar.setVisible(True)
        self._progress_eta.setVisible(True)
        self._progress_bar.setValue(0)
        self._progress_eta.setText("Starting\u2026")

        log(f"[queue] {len(jobs)} video(s) queued, "
            f"{MAX_PARALLEL_DOWNLOADS} at a time, "
            f"{DELAY_BETWEEN_DOWNLOADS}s between each.")
        self._run_queued(jobs)

    def download_video(self, page, row, title, video_url, file_format, quality,
                       base_folder, name):
        """Download one video, serialised against other copies of the same URL.

        A playlist may legitimately contain the same video more than once. Two
        workers writing the same target file at the same time makes yt-dlp
        delete its temp file out from under the other one, so exactly one of
        them fails for no real reason. The lock keeps duplicates in sequence
        while unrelated videos still download in parallel.
        """
        with lock_for(video_url):
            self._download_video(page, row, title, video_url, file_format,
                                 quality, base_folder, name)

    def _download_video(self, page, row, title, video_url, file_format, quality,
                        base_folder, name):
        """Download a single video. Runs on a worker thread.

        Emits status updates through :class:`DownloaderSignals` only.
        """
        try:
            playlist_folder = os.path.join(base_folder, sanitize_name(name))
            output_folder = os.path.join(
                playlist_folder, "Videos" if file_format == "mp4" else "Audio"
            )
            os.makedirs(output_folder, exist_ok=True)

            self.signals.status_changed.emit(page, row, "Queued")

            def progress_hook(status):
                # Abort the transfer the moment the user asks to cancel. yt-dlp
                # does not swallow exceptions raised from progress hooks, so
                # this propagates out of extract_info and kills the download.
                if self._cancel_event.is_set():
                    raise CancelledError()

                if status.get("status") == "downloading":
                    total = status.get("total_bytes") or status.get("total_bytes_estimate")
                    done = status.get("downloaded_bytes") or 0
                    if total:
                        percent = min(100.0, done * 100.0 / total)
                        self.signals.status_changed.emit(
                            id(page), row, f"\u2B07 Downloading\u2026 {percent:.0f}%")
                    else:
                        speed = status.get("speed")
                        suffix = f" ({speed / 1024 / 1024:.1f} MB/s)" if speed else ""
                        self.signals.status_changed.emit(
                            id(page), row, f"\u2B07 Downloading\u2026{suffix}")
                    # Feed the shared tracker so the footer bar + ETA stay accurate
                    # across every parallel download.
                    self._progress.update((id(page), row), done, total or 0)
                    pct, eta, spd = self._progress.snapshot()
                    self.signals.progress_updated.emit(
                        pct if pct is not None else -1.0,
                        eta if eta is not None else -1.0,
                        spd if spd is not None else -1.0)
                elif status.get("status") == "finished":
                    self.signals.status_changed.emit(page, row, "\u23F3 Processing\u2026")

            ydl_opts = {
                "outtmpl": os.path.join(output_folder, "%(title).150s [%(id)s].%(ext)s"),
                "format": build_format_selector(file_format, quality, self.has_ffmpeg),
                "noplaylist": True,
                "quiet": True,            # our logger + progress hook do the reporting
                "no_warnings": False,
                "noprogress": True,
                "logger": ConsoleLogger(),
                "progress_hooks": [progress_hook],
                "retries": 5,
                "fragment_retries": 5,
                # Back off politely instead of hammering a rate-limited endpoint.
                "sleep_interval_requests": 1,
                "continuedl": True,
                "trim_file_name": 200,
                "windowsfilenames": True,
                **js_runtime_opts(),
                **auth_opts(),
            }

            if self.has_ffmpeg and file_format == "mp4":
                # Only meaningful when we actually ask for a video+audio merge;
                # setting it otherwise would reject valid non-mp4 fallbacks.
                ydl_opts["merge_output_format"] = "mp4"

            if file_format == "mp3":
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]

            # Already have it? A duplicate row in a playlist, or a re-run after a crash,
            # should not spend bandwidth again.
            existing = self._find_existing(video_url, output_folder)
            if existing:
                log(f"[download] already saved, skipping: {existing}")
                self.signals.status_changed.emit(page, row, "Already saved ⏭")
                self.signals.log_message.emit(f"[download] Skipped (already saved): {existing}")
                self.signals.history_saved.emit(title, "Already saved ⏭", existing)
                return

            self.signals.log_message.emit(f"[download] {video_url} -> {output_folder}")

            # Retry with a growing pause. YouTube throttles by telling you to
            # prove you are not a bot, and that clears up on its own given
            # enough time - so a slow retry beats losing the download.
            last_error = None
            info = None
            total_attempts = len(BACKOFF_SECONDS) + 1  # + the first try

            for attempt in range(total_attempts):
                # Sleep *before* retrying, never on the first attempt, and never
                # on the last one - there is no further attempt left to save.
                if attempt and attempt < len(BACKOFF_SECONDS):
                    wait = BACKOFF_SECONDS[attempt - 1]
                    self.signals.status_changed.emit(
                        page, row, f"Retry {attempt}/{len(BACKOFF_SECONDS)}…")
                    self.signals.log_message.emit(
                        f"[download] Waiting {wait}s before retry {attempt}.")
                    time.sleep(wait)
                elif attempt == len(BACKOFF_SECONDS):
                    self.signals.status_changed.emit(
                        page, row, f"Final retry {attempt}/{len(BACKOFF_SECONDS)}…")

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(video_url, download=True)
                    last_error = None
                    break
                except DownloadError as exc:
                    last_error = exc
                    if not is_throttle_error(exc):
                        # Not a rate limit - retrying will not help.
                        raise
                    log(f"[download] throttled on {video_url}: {exc}")

            if last_error is not None:
                raise last_error

            saved = self._resolve_downloaded_file(info, output_folder)

            if not saved:
                raise RuntimeError("yt-dlp reported success but no file was found on disk.")

            # A Spotify-sourced file gets the Spotify tags written onto it.
            spot = self._spotify_meta.pop(video_url, None)
            if spot:
                apply_spotify_tags(saved, spot)

            self.signals.status_changed.emit(page, row, "Completed \u2705")
            self.signals.log_message.emit(f"[download] Saved: {saved}")
            self.signals.history_saved.emit(title, "Completed \u2705", saved)

        except CancelledError:
            # The user pressed Cancel - this is not a failure, so say so.
            log(f"[download] cancelled by user: {video_url}")
            self.signals.status_changed.emit(page, row, "Cancelled \u23F9")
            self.signals.history_saved.emit(title, "Cancelled \u23F9", "")
            self.signals.log_message.emit(f"[download] Cancelled: {video_url}")
        except DownloadError as exc:
            if is_drm_error(exc):
                # A DRM-protected upload cannot be saved by any tool, and
                # retrying will never help. Say exactly that.
                log(f"[download] DRM-protected, cannot save: {video_url}")
                self.signals.status_changed.emit(
                    page, row, "DRM protected \u2013 cannot download")
                self.signals.history_saved.emit(
                    title, "DRM protected \u2013 cannot download", "")
                self.signals.log_message.emit(
                    f"[download] DRM protected (not downloadable): {video_url}")
                return
            # A 404 on SoundCloud means the track is gone or the link was
            # mistyped. "Failed" hides that, so say what actually happened.
            if "404" in str(exc).lower() and "soundcloud" in str(exc).lower():
                log(f"[download] not found: {video_url}")
                self.signals.status_changed.emit(
                    page, row, "Not found \u26D4")
                self.signals.history_saved.emit(title, "Not found \u26D4", "")
                self.signals.log_message.emit(
                    f"[download] Track not found (check the link): {video_url}")
                return

            log(f"[download] FAILED {video_url}: {exc}")
            status = "Failed (blocked) \u274C" if is_throttle_error(exc) else "Failed \u274C"
            self.signals.status_changed.emit(page, row, status)
            self.signals.history_saved.emit(title, status, "")
            self.signals.log_message.emit(f"[download] FAILED {video_url}: {exc}")
        except Exception as exc:  # noqa: BLE001 - always report, never fake success
            log(traceback.format_exc())
            self.signals.status_changed.emit(page, row, "Failed \u274C")
            self.signals.history_saved.emit(title, "Failed \u274C", "")
            self.signals.log_message.emit(f"[download] FAILED {video_url}: {exc}")
        finally:
            self._progress.finish((id(page), row))
            self.signals.download_finished.emit()

    @staticmethod
    def _find_existing(video_url, output_folder):
        """Return an already-saved file for this video, or None.

        The output name embeds the video id (``[dQw4w9WgXcQ]``), so the id from
        the URL is enough to recognise a file we saved before.
        """
        video_id = ""
        try:
            query = urlsplit(video_url).query
            match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", query)
            if match:
                video_id = match.group(1)
        except ValueError:
            return None
        if not video_id:
            return None

        marker = f"[{video_id}]"
        try:
            matches = [
                os.path.join(output_folder, name)
                for name in os.listdir(output_folder)
                if marker in name and os.path.isfile(os.path.join(output_folder, name))
            ]
        except OSError:
            return None
        return max(matches, key=os.path.getmtime) if matches else None

    @staticmethod
    def _resolve_downloaded_file(info, output_folder):
        """Return the path of the file this video wrote, or None.

        Never falls back to "newest file in the folder": several videos may be
        downloading into the same folder at once, so that would let one row
        report success because of another row's file.
        """
        candidates = []
        if isinstance(info, dict):
            if info.get("filepath"):
                candidates.append(info["filepath"])
            for requested in info.get("requested_downloads") or []:
                if isinstance(requested, dict) and requested.get("filepath"):
                    candidates.append(requested["filepath"])

        for path in candidates:
            if path and os.path.exists(path):
                return path

        # Safety net: look for the "[<video id>]" marker our template adds.
        video_id = info.get("id") if isinstance(info, dict) else None
        if video_id:
            marker = f"[{video_id}]"
            try:
                matches = [
                    os.path.join(output_folder, name)
                    for name in os.listdir(output_folder)
                    if marker in name and os.path.isfile(os.path.join(output_folder, name))
                ]
            except OSError:
                matches = []
            if matches:
                return max(matches, key=os.path.getmtime)

        return None

    def on_progress_updated(self, percent, eta_seconds, speed):
        """GUI thread: move the footer bar and refresh the ETA label."""
        if percent is not None and percent >= 0:
            self._progress_bar.setValue(int(round(percent)))

        if eta_seconds is None or eta_seconds < 0:
            # No usable estimate yet (total size still unknown).
            if speed and speed > 0:
                self._progress_eta.setText(
                    f"{speed / 1024 / 1024:.1f} MB/s")
            else:
                self._progress_eta.setText("Starting\u2026")
            return

        self._progress_eta.setText(
            f"{self._format_duration(eta_seconds)} remaining"
            + (f" \u00b7 {speed / 1024 / 1024:.1f} MB/s" if speed and speed > 0 else ""))

    @staticmethod
    def _format_duration(seconds):
        """Human readable duration: '4m 32s', '1h 2m', '38s'."""
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"

    def on_download_finished(self):
        """GUI thread: re-enable the download buttons once everything is done."""
        self._active_downloads = max(0, self._active_downloads - 1)
        if self._active_downloads == 0:
            self._progress_bar.setVisible(False)
            self._progress_eta.setVisible(False)
            for index in (0, 1, 2, 3):
                widget = self.pages.widget(index)
                if hasattr(widget, "download_button"):
                    widget.download_button.setEnabled(True)
                if hasattr(widget, "download_all_button"):
                    widget.download_all_button.setEnabled(True)
                if hasattr(widget, "cancel_button"):
                    widget.cancel_button.setEnabled(False)
                if hasattr(widget, "table"):
                    widget.table.setEnabled(True)
                    # Anything still marked "Queued" never started, so it is not
                    # a failure - say so instead of leaving it looking stuck.
                    for row in range(widget.table.rowCount()):
                        item = widget.table.item(row, 1)
                        if item is not None and item.text() == "Queued":
                            item.setText("Not started")

    # ------------------------------------------------------------------ #
    # Static pages
    # ------------------------------------------------------------------ #
    def create_about_page(self):
        """Creates the About page.

        Every line is its own label with explicit spacing, and a stretch at the
        end pins everything to the top. Without that stretch Qt hands the spare
        vertical space to every item in turn, which opens big random gaps.
        """
        page = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)
        layout.setContentsMargins(14, 14, 14, 14)

        def heading(text, icon=None, color="#ff2e63", size=18):
            if icon is None:
                return _plain_label(
                    text, css=f"font-size: {size}px; font-weight: bold; "
                                   f"color: {color};")
            return icon_label(text, icon, color, size,
                              icon_size=int(size * 1.15), bold=True)

        def body(text, color="#ffffff", size=13):
            return _plain_label(text, word_wrap=True,
                                css=f"color: {color}; font-size: {size}px;")

        layout.addWidget(heading("About " + APP_NAME, "book"))
        layout.addSpacing(4)
        layout.addWidget(body(
            "A free desktop tool for saving YouTube videos, playlists and whole "
            "channels as MP4 or MP3. Built on yt-dlp and PyQt6."))
        layout.addSpacing(16)

        layout.addWidget(heading("Why is it free?", "spark", "#2ecc71", 15))
        layout.addSpacing(4)
        layout.addWidget(body(
            "Downloading your own content should not cost anything. "
            "No subscriptions, no hidden fees."))
        layout.addSpacing(16)

        layout.addWidget(heading("What it does", "tools", "#3498db", 15))
        layout.addSpacing(4)
        for line in (
            "Single Video - download one video from a watch link.",
            "Playlist - load a playlist, then choose which videos to save.",
            "Channel - fetch every upload from a channel or handle.",
            "History - every finished download, kept between sessions.",
        ):
            layout.addWidget(body("    " + line, "#dddddd", 13))
        layout.addSpacing(16)

        layout.addWidget(heading(f"Created by {APP_AUTHOR}", "person", "#ffffff", 15))
        layout.addSpacing(10)
        layout.addWidget(body("Powered by yt-dlp and PyQt6.", "#aaaaaa", 12))
        layout.addWidget(body(
            "Please only download content you have the rights to.", "#aaaaaa", 12))
        layout.addSpacing(14)

        change = QPushButton("← Back to service selection")
        change.setStyleSheet(
            "background-color: #2a2a2a; color: #cccccc; "
            "padding: 8px; border-radius: 6px;")
        change.clicked.connect(self.go_to_service_picker)
        layout.addWidget(change)

        # Pushes every widget up to the top instead of spreading them out.
        layout.addStretch()

        page.setLayout(layout)
        return page

    def create_settings_page(self):
        """Creates the Settings Page."""
        page = QWidget()
        layout = QVBoxLayout()

        self.theme_toggle = QPushButton("Toggle Light/Dark Mode")
        self.theme_toggle.setStyleSheet(
            "background-color: #f1c40f; color: black; padding: 8px; border-radius: 5px;")
        self.theme_toggle.clicked.connect(self.toggle_theme)
        layout.addWidget(self.theme_toggle)

        layout.addWidget(icon_label(
            "Downloads go to your Downloads folder unless you pick another "
            "one with Browse. Each list is saved in its own subfolder.",
            "info", "#9a9a9a", 12, icon_size=15))

        layout.addSpacing(18)
        layout.addWidget(icon_label("Sign in", "user", "#ff2e63", 17,
                                    icon_size=19, bold=True))
        layout.addWidget(_plain_label(
            "Signing in is optional for every platform. Public links work "
            "without it; signing in only adds private content.",
            word_wrap=True, css="color: #aaaaaa; font-size: 12px;"))
        layout.addSpacing(10)

        layout.addWidget(icon_label("YouTube", "play", "#FF0033", 15,
                                    icon_size=17, bold=True))

        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #444;")
        layout.addWidget(sep)

        rate_header = icon_label("Rate limiting", "shield", "#ff2e63", 15,
                                 icon_size=17, bold=True)
        layout.addWidget(rate_header)

        self.parallel_label = _plain_label(
            word_wrap=True, css="color: #aaaaaa; font-size: 12px;")
        layout.addWidget(self.parallel_label)
        self._refresh_rate_label()

        lower = QPushButton("Use 1 download at a time (safest)")
        lower.setStyleSheet(
            "background-color: #3498db; color: white; padding: 8px; border-radius: 5px;")
        lower.clicked.connect(lambda: self._set_parallel(1))
        layout.addWidget(lower)

        higher = QPushButton("Use 5 downloads at a time (faster, riskier)")
        higher.setStyleSheet(
            "background-color: #e67e22; color: white; padding: 8px; border-radius: 5px;")
        higher.clicked.connect(lambda: self._set_parallel(5))
        layout.addWidget(higher)

        # --- Account / private content ------------------------------------
        layout.addSpacing(10)
        sep2 = QWidget()
        sep2.setFixedHeight(1)
        sep2.setStyleSheet("background-color: #444;")
        layout.addWidget(sep2)

        auth_header = icon_label("Account", "lock", "#ff2e63", 15,
                                icon_size=17, bold=True)
        layout.addWidget(auth_header)

        self.auth_label = _plain_label(
            word_wrap=True, css="color: #aaaaaa; font-size: 12px;")
        layout.addWidget(self.auth_label)

        self.auth_browser_dropdown = QComboBox()
        available = detect_browsers()
        for key, (label, _) in AUTH_BROWSERS.items():
            if key == "none" or key in available:
                self.auth_browser_dropdown.addItem(label, key)
        # Show the restored choice without firing the save handler.
        restored = _auth_browser
        if restored != "none":
            index = self.auth_browser_dropdown.findData(restored)
            if index >= 0:
                self.auth_browser_dropdown.setCurrentIndex(index)
        self.auth_browser_dropdown.currentIndexChanged.connect(self._on_browser_changed)
        layout.addWidget(self.auth_browser_dropdown)

        test_auth = QPushButton("Test signed-in session")
        test_auth.setStyleSheet(
            "background-color: #8e44ad; color: white; padding: 8px; border-radius: 5px;")
        test_auth.clicked.connect(self._test_auth)
        layout.addWidget(test_auth)

        self._refresh_auth_label()

        layout.addSpacing(22)

        # --- Spotify connection ------------------------------------------
        layout.addWidget(_plain_label(
            "Spotify", css="font-size: 16px; font-weight: bold; color: #1ED760;"))
        layout.addWidget(_plain_label(
            "Spotify cannot export audio, so this app only uses it as a "
            "catalogue: it reads the track list and tags from Spotify, then "
            "fetches the audio from YouTube.\n\n"
            "Spotify issues access tokens only to registered apps, so paste "
            "your own free credentials once. No password, no account login.",
            word_wrap=True, css="color: #cfcfcf; font-size: 13px;"))
        layout.addSpacing(8)

        config = load_config()
        self.spotify_id_input = self._credential_field(
            "Client ID", config.get("spotify_client_id", ""))
        self.spotify_secret_input = self._credential_field(
            "Client Secret", config.get("spotify_client_secret", ""))

        spotify_buttons = QHBoxLayout()
        save_spotify = QPushButton("Save")
        save_spotify.setStyleSheet(
            "background-color: #1ED760; color: white; padding: 8px; "
            "border-radius: 5px;")
        save_spotify.clicked.connect(self._save_spotify_credentials)
        spotify_buttons.addWidget(save_spotify)
        self.spotify_save_button = save_spotify

        test_spotify = QPushButton("Test connection")
        test_spotify.setStyleSheet(
            "background-color: #1a1a1a; color: #1ED760; padding: 8px; "
            "border: 1px solid #1ED760; border-radius: 5px;")
        test_spotify.clicked.connect(self._test_spotify)
        spotify_buttons.addWidget(test_spotify)
        self.spotify_test_button = test_spotify

        spotify_help = QPushButton("Get credentials")
        spotify_help.setStyleSheet(
            "background-color: #1a1a1a; color: #cfcfcf; padding: 8px; "
            "border: 1px solid #3a3a3a; border-radius: 5px;")
        spotify_help.clicked.connect(self._show_spotify_dashboard_help)
        spotify_buttons.addWidget(spotify_help)
        spotify_buttons.addStretch()
        layout.addLayout(spotify_buttons)

        self.spotify_label = _plain_label(
            "", word_wrap=True, css="color: #cfcfcf; font-size: 12px;")
        layout.addWidget(self.spotify_label)

        auth_row = QHBoxLayout()
        self.spotify_signin_button = QPushButton("Sign in with Spotify")
        self.spotify_signin_button.setStyleSheet(
            "background-color: #1ED760; color: #06281a; padding: 8px 14px; "
            "border-radius: 5px; font-weight: bold;")
        self.spotify_signin_button.clicked.connect(self._spotify_sign_in)
        auth_row.addWidget(self.spotify_signin_button)

        self.spotify_signout_button = QPushButton("Sign out")
        self.spotify_signout_button.setStyleSheet(
            "background-color: #1a1a1a; color: #cfcfcf; padding: 8px 14px; "
            "border: 1px solid #3a3a3a; border-radius: 5px;")
        self.spotify_signout_button.clicked.connect(self._spotify_sign_out)
        auth_row.addWidget(self.spotify_signout_button)
        auth_row.addStretch()
        layout.addLayout(auth_row)

        layout.addWidget(_plain_label(
            "Signing in is optional. It only adds access to your own library "
            "and private playlists - a shared open.spotify.com link works "
            "without it. Vex never sees your Spotify password.",
            word_wrap=True, css="color: #8a8a8a; font-size: 12px;"))

        self._refresh_spotify_label()
        self._refresh_spotify_auth()

        layout.addStretch()
        page.setLayout(layout)
        return page

    def _spotify_sign_in(self):
        """Start the browser sign-in. The network work runs off-thread."""
        if getattr(self, "_spotify_authing", False):
            return
        self._spotify_authing = True
        for button in (getattr(self, "spotify_signin_button", None),
                       getattr(self, "spotify_signout_button", None)):
            if button is not None:
                button.setEnabled(False)
        self._set_spotify_text(
            "Waiting for Spotify in your browser\u2026", "#8a8a8a")
        self._start_worker(self._spotify_signin_worker)

    def _spotify_signin_worker(self):
        try:
            ok, message = spotify_sign_in_flow()
        except Exception as exc:  # noqa: BLE001 - never let the worker die
            ok, message = False, f"Sign-in failed: {exc}"
        self.signals.spotify_auth_done.emit(ok, message)

    def _spotify_sign_out(self):
        spotify_sign_out()
        self._refresh_spotify_auth()

    def _on_spotify_auth_done(self, ok, message):
        self._spotify_authing = False
        self._set_spotify_text(message, "#2ecc71" if ok else "#e74c3c")
        self._refresh_spotify_auth()

    def _refresh_spotify_auth(self):
        """Show the current sign-in state on the Spotify buttons."""
        signed_in = spotify_signed_in()
        if getattr(self, "spotify_signin_button", None) is not None:
            self.spotify_signin_button.setText(
                "Sign in again" if signed_in else "Sign in with Spotify")
            self.spotify_signin_button.setEnabled(not signed_in)
        if getattr(self, "spotify_signout_button", None) is not None:
            self.spotify_signout_button.setEnabled(signed_in)
        if not getattr(self, "_spotify_authing", False):
            self._refresh_spotify_label()

    def _show_spotify_dashboard_help(self):
        """Explain where the credentials come from without launching a browser.

        Calling webbrowser.open() from here used to steal focus straight out
        of whatever the user was running (it crashed a fullscreen game), so the
        link is shown for copying and opened only if they ask for it.
        """
        box = QMessageBox(self)
        box.setWindowTitle("Getting Spotify credentials")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "1.  Open this page in your normal browser:\n\n"
            "        https://developer.spotify.com/dashboard\n\n"
            "2.  Click 'Create app'. Any name will do.\n"
            "3.  Open the app you made, then 'Settings',\n"
            "    and press 'Show Client Secret'.\n\n"
            "4.  Copy the Client ID and the Client Secret back here."
        )
        box.setInformativeText(
            "Both values are free. This is not a Spotify account login - Vex "
            "never asks for your Spotify username or password.")
        open_button = box.addButton("Open the page", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_button:
            import webbrowser
            try:
                webbrowser.open("https://developer.spotify.com/dashboard")
            except Exception:  # noqa: BLE001 - a browser failure is not fatal
                log("[spotify] Could not open a browser.")

    def _credential_field(self, label, value):
        """A labelled password-style box for a credential."""
        row = QHBoxLayout()
        row.addWidget(_plain_label(
            f"{label}:", css="color: #cfcfcf; font-size: 13px;"))
        field = QLineEdit(value or "")
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setStyleSheet(
            "background-color: #1c1c1c; border: 1px solid #3a3a3a; "
            "border-radius: 5px; padding: 7px; color: #ffffff;")
        row.addWidget(field, 1)
        return field

    def _save_spotify_credentials(self):
        """Store the Spotify app credentials, then re-check the connection."""
        config = load_config()
        config["spotify_client_id"] = self.spotify_id_input.text().strip()
        config["spotify_client_secret"] = self.spotify_secret_input.text().strip()
        save_config(config)
        # Force a fresh token so the saved values are actually exercised.
        with _spotify_token_lock:
            _spotify_token_cache["token"] = None
        self._test_spotify()

    def _test_spotify(self):
        """Probe the Spotify API without freezing the window.

        The request is genuinely slow (and can hang on a bad network), so it
        runs on a worker thread exactly like every other network call in this
        app. Blocking here would freeze the whole UI on a spinner.
        """
        if getattr(self, "_spotify_testing", False):
            return
        self._spotify_testing = True
        self._set_spotify_text("Testing connection...", "#8a8a8a")
        self._start_worker(self._spotify_probe_worker)

    def _spotify_probe_worker(self):
        """Worker: exchange the token and issue one real search."""
        try:
            spotify_credentials()
            spotify_token()
            probe = spotify_api("search", {"q": "daft punk", "type": "track",
                                          "limit": 1})
            count = (probe.get("tracks") or {}).get("total", 0)
            self.signals.spotify_tested.emit(
                f"Connected. Spotify search works "
                f"({count:,} results for 'daft punk').", "#2ecc71")
        except Exception as exc:  # noqa: BLE001 - report whatever went wrong
            self.signals.spotify_tested.emit(str(exc), "#e74c3c")

    def _on_spotify_tested(self, message, colour):
        """GUI thread: show the outcome and re-enable the controls."""
        self._spotify_testing = False
        self._set_spotify_text(message, colour)
        for button in (getattr(self, "spotify_test_button", None),
                       getattr(self, "spotify_save_button", None)):
            if button is not None:
                button.setEnabled(True)

    def _set_spotify_text(self, text, colour):
        self.spotify_label.setText(text)
        self.spotify_label.setStyleSheet(
            f"background: transparent; border: none; color: {colour}; "
            "font-size: 12px;")

    def _refresh_spotify_label(self):
        config = load_config()
        have_app = bool(config.get("spotify_client_id")
                        and config.get("spotify_client_secret"))
        if not have_app:
            self._set_spotify_text(
                "Step 1 of 2 - paste a Client ID and Secret above. "
                "Spotify downloads stay unavailable until you do.",
                "#8a8a8a")
            return
        if spotify_signed_in():
            self._set_spotify_text(
                "Signed in. Public links and your own library both work.",
                "#2ecc71")
        else:
            self._set_spotify_text(
                "Step 2 of 2 (optional) - app credentials saved. Public links "
                "work now; sign in to reach your own library.", "#2ecc71")

    # ------------------------------------------------------------------ #
    # Account / private content
    # ------------------------------------------------------------------ #
    def _on_browser_changed(self):
        """Persist whichever browser the user picked as their session."""
        global _auth_browser
        key = self.auth_browser_dropdown.currentData()
        if not key:
            return
        _auth_browser = key
        config = load_config()
        config["auth_browser"] = key
        save_config(config)
        self._refresh_auth_label()

    def _refresh_auth_label(self):
        """Explain what the account setting does and does not do."""
        if _auth_browser == "none":
            self.auth_label.setText(
                "Signed out.\n\n"
                "Everything public downloads normally. Turn this on only if you "
                "need a private playlist, an age-restricted video you can already "
                "watch, or a backup of your own uploads."
            )
            return

        label = AUTH_BROWSERS.get(_auth_browser, ("your browser", None))[0]
        self.auth_label.setText(
            f"Using the YouTube session already signed into {label}.\n\n"
            "You must already be logged into YouTube in that browser. Nothing is "
            "copied or saved by this app - it reads the session you already have.\n\n"
            "Being signed in does NOT raise any download limit. It only helps reach "
            "content that is otherwise invisible. If YouTube has already blocked "
            "your IP, signing in will not undo that - you still have to wait it out."
        )

    def _test_auth(self):
        """Check whether the signed-in session actually reaches a private resource.

        Only looks at whether authentication itself works; it deliberately does
        not probe for private playlists, since that would mean enumerating your
        account without being asked.
        """
        if _auth_browser == "none":
            QMessageBox.information(
                self, "Signed out",
                "Pick a browser above first. You need to be logged into YouTube "
                "in that browser.")
            return

        label = AUTH_BROWSERS.get(_auth_browser, ("your browser", None))[0]

        def worker():
            try:
                opts = {
                    "quiet": True,
                    "skip_download": True,
                    "logger": ConsoleLogger(),
                    **js_runtime_opts(),
                    **auth_opts(),
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    # is_authenticated is a cheap check that never lists anything.
                    result = ydl.extract_info(
                        "https://www.youtube.com/feed/history", download=False)
                signed_in = bool(result and result.get("account"))
                if result:
                    name = (result.get("account") or {}).get("channel_name") or "your account"
                    message = (f"Session works.\n\n"
                               f"Signed in as: {name}\n"
                               f"Private playlists and your own uploads should now be "
                               f"reachable.")
                else:
                    message = (
                        "Could not read the YouTube feed.\n\n"
                        "That usually means one of:\n"
                        f"  - you are not logged into YouTube in {label}\n"
                        f"  - {label} is open (close it, cookies are locked)\n"
                        "  - you have not accepted the cookie prompt")
                self.signals.auth_tested.emit(signed_in, message)
            except Exception as exc:  # noqa: BLE001 - report, never crash
                log(traceback.format_exc())
                self.signals.auth_tested.emit(False, str(exc))

        self._set_auth_busy()
        self._start_worker(worker)

    def on_auth_tested(self, ok, message):
        """GUI thread: show the result of the session test."""
        self._refresh_auth_label()
        if ok:
            QMessageBox.information(self, "Signed in", message)
        else:
            QMessageBox.warning(self, "Not signed in", message)

    def _set_parallel(self, count):
        """Change how many videos may download at the same time."""
        global MAX_PARALLEL_DOWNLOADS
        MAX_PARALLEL_DOWNLOADS = count
        self._refresh_rate_label()

    def _refresh_rate_label(self):
        self.parallel_label.setText(
            f"Currently downloading up to {MAX_PARALLEL_DOWNLOADS} videos at once, "
            f"with a {DELAY_BETWEEN_DOWNLOADS}s pause between each.\n\n"
            "YouTube throttles bursts rather than publishing a hard limit, so "
            "fewer parallel downloads means you are far less likely to be asked "
            "to 'prove you are not a bot'. 1-3 is safe; above 5 will get you "
            "blocked sooner or later. Takes effect on the next batch."
        )

    def go_to_service_picker(self):
        """Close this window and return to the service chooser.

        ``on_change_service`` is wired up by main(); when it is missing (for
        example when the window is built directly in a test) the button simply
        does nothing rather than blowing up.
        """
        callback = getattr(self, "on_change_service", None)
        if callback is None:
            return
        self.close()
        callback()

    def toggle_theme(self):
        """Switch between light and dark mode.

        The window background flips here; every label also redraws itself from
        its stored dark-theme stylesheet so near-white text does not vanish on
        a white background (the original light mode was unreadable).
        """
        current_style = self.styleSheet()
        if "background-color: #1E1E1E" in current_style:
            dark = False
            self.setStyleSheet(
                "background-color: #FFFFFF; color: black; font-size: 14px; "
                + GLOBAL_LABEL_CSS)
        else:
            dark = True
            self.setStyleSheet(
                "background-color: #1E1E1E; color: white; font-size: 14px; "
                + GLOBAL_LABEL_CSS)

        for label in self.findChildren(QLabel):
            base = label.property("vexBaseCss")
            if not base:
                continue
            label.setStyleSheet(swap_label_colors(str(base), dark))

# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    # Make the console able to print the emoji used all over this app.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - older/redirected streams
            pass

    # Drop unreadable PATH entries, otherwise yt-dlp dies with WinError 1392
    # while looking for a JavaScript runtime.
    ensure_clean_path()

    # Restore the saved account setting before any UI reads it.
    global _auth_browser
    _auth_browser = load_config().get("auth_browser", "none")
    if _auth_browser not in AUTH_BROWSERS:
        _auth_browser = "none"

    log(f"{APP_NAME} v{APP_VERSION} - by {APP_AUTHOR}")
    log(f"Saving to: {default_download_dir()}")
    log(f"yt-dlp version: {yt_dlp.version.__version__}")
    if _auth_browser == "none":
        log("Account: signed out (public content only)")
    else:
        name = AUTH_BROWSERS.get(_auth_browser, (_auth_browser, ""))[0]
        log(f"Account: using the YouTube session from {name}")
    if ffmpeg_available():
        log("FFmpeg: found on PATH (MP4 merging + MP3 extraction available)")
    else:
        log("FFmpeg: NOT found on PATH - MP3 extraction and stream merging are unavailable.")
        log("        Install it with:  winget install Gyan.FFmpeg")
        log("        or download https://www.gyan.dev/ffmpeg/builds/ and add it to PATH.")

    app = QApplication(sys.argv)
    # On Windows the taskbar icon comes from the QApplication, not the window
    # widget, so setting it only on VexDownloader leaves the default Python
    # icon in the taskbar. This is the app-wide (taskbar + Alt-Tab) icon.
    app.setWindowIcon(app_icon(SERVICE_YT, 64))

    splash = SplashScreen()
    splash.show()

    window = {}

    def show_main(service=None):
        """Open the downloader window, closing whichever screen preceded it."""
        picker = window.get("picker")
        if picker is not None:
            picker.close()
            window.pop("picker", None)
        main_window = VexDownloader(MODE_VIDEO, service=service or SERVICE_YT)
        # Lets About -> "Back to service selection" reopen this screen.
        main_window.on_change_service = open_picker
        main_window.show()
        window["main"] = main_window  # keep a reference alive

    def open_picker():
        """Show the service chooser that greets the user on launch."""
        splash.close()
        picker = ServicePicker(show_main)
        picker.show()
        window["picker"] = picker  # keep a reference alive

    QTimer.singleShot(1800, open_picker)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()