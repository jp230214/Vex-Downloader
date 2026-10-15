"""Vex-Setup: one-time installer for Vex YouTube Downloader.

Bundles the app executable and drops it into a folder of the user's choosing,
creates Start Menu / Desktop shortcuts, and walks through the one dependency
that genuinely cannot be bundled - FFmpeg.

Run from source (dev):  py setup_installer.py
It expects the app executable next to this file, or bundled inside it.
"""
import os
import shutil
import subprocess
import sys
import tempfile

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QStackedWidget, QVBoxLayout,
    QWidget,
)

APP_NAME = "Vex Downloader"
APP_VERSION = "2.2.0"
APP_AUTHOR = "Vex"
APP_FOLDER_NAME = "Vex Downloader"
# The installed executable is named after the folder, so the taskbar,
# Start Menu and Explorer all read "Vex Downloader" instead of the old
# YouTube branding.
APP_EXE = "Vex Downloader.exe"

# Set this to the raw-download URL of a GitHub release asset named
# exactly APP_EXE to build an installer that fetches the app instead
# of carrying it (which makes this installer far smaller).
GITHUB_APP_URL = ""
FFMPEG_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
FFMPEG_WINGET = "Gyan.FFmpeg"


# --------------------------------------------------------------------------- #
# Docs written at install time
# --------------------------------------------------------------------------- #
#
# Kept here rather than imported from Vex.py: Vex is a GUI module and importing
# it just to read a version string would drag PyQt into the installer's import
# path for no reason.
def read_app_version():
    """Pull APP_VERSION out of Vex.py if it is sitting beside the installer."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "Vex.py"),
                      os.path.join(here, "..", "Vex.py")):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("APP_VERSION"):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return "unknown"


DOC_TEMPLATES = {
    "README.md": """# {name} {version}

A PyQt6 + yt-dlp desktop app for downloading from YouTube, Spotify and
SoundCloud as MP4 or MP3. Created by {author}.

## Running

Just open **{exe}**. All dependencies are bundled inside apart from FFmpeg.

## Platforms

| Platform   | What works                                        |
| ---------- | ------------------------------------------------- |
| YouTube    | Videos, playlists and whole channels              |
| Spotify    | Tracks, albums, playlists and your own library    |
| SoundCloud | Tracks, sets (playlists) and artist profiles      |

Switch platform from the drop-down at the top of the window - no restart.

Some SoundCloud uploads are DRM-protected and cannot be downloaded by any
tool; those show as "DRM protected" rather than failing silently.

## Spotify sign-in

Spotify offers no way to export audio, so Vex uses Spotify only as a
catalogue: it reads the track list and writes the tags, then fetches the audio
from YouTube. That needs a free Client ID and Secret, added once in
Settings -> Spotify. Signing in as a user is optional and only adds your own
library.

## Optional: FFmpeg

FFmpeg cannot be bundled. Without it MP4 still downloads normally; FFmpeg adds
MP3 export and merging separate streams for 1080p and above.

    winget install Gyan.FFmpeg

## Where things are kept

    %APPDATA%\\VexDownloader\\config.json    settings
    %APPDATA%\\VexDownloader\\history.json   download history
    %APPDATA%\\VexDownloader\\vex.log       log file

Delete the `VexDownloader` folder to reset the app.

## Requirements (only for running from source)

    pip install -r requirements.txt
    python Vex.py
""",
    "how to install.txt": """{name} {version} - how to install
=================================

Created by {author}.

PORTABLE INSTALL (what you just did)
-------------------------------------
The installer copied {exe} and created your shortcuts. That is the whole
install - Python, PyQt6 and yt-dlp are bundled inside, so there is nothing
else to download.

The app also runs straight from the file, with no install at all: copy
{exe} to a USB stick or any folder and open it. It writes nothing just by
starting.


OPTIONAL - FFMPEG
-----------------
FFmpeg is the one thing that cannot be bundled. The app works without it -
MP4 downloads normally - but you get two things back by adding it:

    -  MP3 (audio only) export
    -  merging separate video and audio for 1080p and above

    winget install Gyan.FFmpeg

Restart the app afterwards; Settings will confirm it was found.


SIGNING IN (optional, all platforms)
------------------------------------
Nothing needs signing in for public content.

    YouTube   - Settings -> Sign in. Lets the app reuse a browser session you
                already have, for private or age-restricted videos.
    Spotify   - Settings -> Spotify. Paste a free Client ID and Secret from
                developer.spotify.com, then optionally sign in to reach your
                own library.

No password is ever typed into this app or stored by it.


RUNNING FROM SOURCE
-------------------
    pip install -r requirements.txt
    python Vex.py
""",
    "requirements.txt": """# Python packages needed to run Vex from source.
#
# The portable build bundles all of these - do NOT install them by hand when
# using the .exe.
#
#     pip install -r requirements.txt

PyQt6>=6.5
yt-dlp>=2024.1.1

# Already pulled in by yt-dlp; listed because Vex uses it directly to write
# Spotify title/artist/album tags onto downloaded files.
mutagen>=1.46

# Optional, recommended but not required:
#   FFmpeg - not installable via pip. Adds MP3 export and 1080p+ merging.
#            winget install Gyan.FFmpeg
""",
}


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def is_frozen():
    return bool(getattr(sys, "frozen", False))


def bundled_app_exe():
    """Locate the app executable shipped inside this installer."""
    if is_frozen():
        root = getattr(sys, "_MEIPASS", None)
        if root:
            candidate = os.path.join(root, APP_EXE)
            if os.path.isfile(candidate):
                return candidate
    # Running from source: look beside this file, then in dist/.
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, APP_EXE),
                      os.path.join(here, "dist", APP_EXE)):
        if os.path.isfile(candidate):
            return candidate
    return None


def bundled_assets_dir():
    """Locate the assets folder that should ship with the installed app.

    When frozen this sits next to the embedded exe inside _MEIPASS. When run
    from source the app exe lives in dist/ while assets/ is one level up, so
    both layouts are checked.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "assets"))
    source_exe = bundled_app_exe()
    if source_exe:
        exe_dir = os.path.dirname(os.path.abspath(source_exe))
        candidates.append(os.path.join(exe_dir, "assets"))
        candidates.append(os.path.join(os.path.dirname(exe_dir), "assets"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "assets"))
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


def ffmpeg_path():
    """Full path to ffmpeg, or None when it is not installed."""
    return shutil.which("ffmpeg")


def winget_path():
    return shutil.which("winget")


def default_install_dir():
    """A per-user install folder - no admin rights needed."""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "VexDownloader")


def log_line(text):
    try:
        with open(os.path.join(tempfile.gettempdir(), "vex_setup.log"), "a",
                  encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Shortcuts
# --------------------------------------------------------------------------- #
def make_shortcut(shortcut_path, target, working_dir, description, icon):
    """Create a Windows .lnk. Returns True on success.

    Uses PowerShell's WScript.Shell COM object so no extra Python dependency
    (pywin32) is needed inside the installer.
    """
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut('{lnk}'); "
        "$s.TargetPath = '{target}'; "
        "$s.WorkingDirectory = '{wd}'; "
        "$s.Description = '{desc}'; "
        "$s.IconLocation = '{icon},0'; "
        "$s.Save()"
    ).format(lnk=shortcut_path.replace("'", "''"),
             target=target.replace("'", "''"),
             wd=working_dir.replace("'", "''"),
             desc=description.replace("'", "''"),
             icon=icon.replace("'", "''"))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=60)
        return result.returncode == 0 and os.path.isfile(shortcut_path)
    except Exception as exc:  # noqa: BLE001 - a missing shortcut is not fatal
        log_line(f"[shortcut] failed: {exc}")
        return False


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def label(text="", size=13, color="#cfcfcf", bold=False, wrap=False):
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    lab.setStyleSheet(
        "background: transparent; border: none; "
        f"color: {color}; font-size: {size}px;"
        + (" font-weight: bold;" if bold else ""))
    return lab


class SetupWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - Setup")
        self.setFixedSize(720, 600)
        self.setStyleSheet("background-color: #141414;")
        self._source_exe = bundled_app_exe()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 26, 30, 22)
        outer.setSpacing(0)

        # Brand
        brand = QHBoxLayout()
        brand.setSpacing(14)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "assets", "logo.png")
        if os.path.isfile(icon_path):
            mark = QLabel()
            mark.setPixmap(QPixmap(icon_path).scaled(
                60, 60, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            mark.setFixedSize(60, 60)
            brand.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label(f"Setup - {APP_NAME}", 22, "#ffffff", True))
        titles.addWidget(label(f"Version {APP_VERSION} - by {APP_AUTHOR}",
                               12, "#8a8a8a"))
        brand.addLayout(titles)
        brand.addStretch()
        outer.addLayout(brand)
        outer.addSpacing(18)

        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self._build_welcome()
        self._build_ffmpeg()
        self._build_install()
        self._build_done()

        # Navigation
        outer.addSpacing(14)
        nav = QHBoxLayout()
        self.status = label("", 12, "#8a8a8a")
        nav.addWidget(self.status, 1)
        self.back_btn = self._button("Back", "#5a5a5a", self.go_back)
        nav.addWidget(self.back_btn)
        self.next_btn = self._button("Next", "#ff2e63", self.go_next, True)
        nav.addWidget(self.next_btn)
        outer.addLayout(nav)

        self.go(0)

    # -- styling helpers ---------------------------------------------------- #
    @staticmethod
    def _button(text, colour, slot, bold=False):
        btn = QPushButton(text)
        btn.setMinimumHeight(38)
        btn.setStyleSheet(
            f"background-color: {colour}; color: white; padding: 8px 18px; "
            "border: none; border-radius: 5px;"
            + (" font-weight: bold;" if bold else ""))
        btn.clicked.connect(slot)
        return btn

    def _page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.pages.addWidget(page)
        return layout

    # -- page 1: welcome ----------------------------------------------------- #
    def _build_welcome(self):
        layout = self._page()
        layout.addWidget(label("Welcome", 20, "#ffffff", True))
        layout.addSpacing(10)
        layout.addWidget(label(
            "This installer sets up " + APP_NAME + " on this computer.",
            13, "#cfcfcf", wrap=True))
        layout.addSpacing(16)

        layout.addWidget(label("What you get", 15, "#ffffff", True))
        layout.addSpacing(4)
        layout.addWidget(label(
            "All dependencies are bundled inside apart from FFmpeg.\n\n"
            "Python, PyQt6 and yt-dlp are already compiled into the app, so "
            "there is nothing else to download or install. The only thing you "
            "may need separately is FFmpeg - and the next step checks for it.",
            13, "#cfcfcf", wrap=True))
        layout.addSpacing(16)

        layout.addWidget(label("What the installer does", 15, "#ffffff", True))
        layout.addSpacing(4)
        for text in (
            "1.  Checks whether FFmpeg is installed and helps you add it.",
            "2.  Copies the app into a folder you choose.",
            "3.  Adds a Desktop shortcut and a Start Menu entry.",
            "4.  Opens the app so you can start downloading.",
        ):
            layout.addWidget(label(text, 13, "#cfcfcf"))
        layout.addStretch()

        if self._source_exe is None:
            layout.addWidget(label(
                f"Warning: {APP_EXE} was not found next to the installer.",
                13, "#e74c3c", bold=True, wrap=True))

    # -- page 2: ffmpeg ------------------------------------------------------ #
    def _build_ffmpeg(self):
        layout = self._page()
        layout.addWidget(label("FFmpeg", 20, "#ffffff", True))
        layout.addSpacing(10)

        self.ffmpeg_status = label("", 14, "#cfcfcf", bold=True, wrap=True)
        layout.addWidget(self.ffmpeg_status)
        layout.addSpacing(12)

        self.ffmpeg_body = label("", 13, "#cfcfcf", wrap=True)
        layout.addWidget(self.ffmpeg_body)
        layout.addSpacing(14)

        # Tutorial block - only populated when FFmpeg is missing.
        self.tut_box = QWidget()
        self.tut_layout = QVBoxLayout(self.tut_box)
        self.tut_layout.setContentsMargins(14, 14, 14, 14)
        self.tut_layout.setSpacing(8)
        self.tut_box.setStyleSheet(
            "background-color: #1c1c1c; border: 1px solid #333333;"
            "border-radius: 6px;")
        layout.addWidget(self.tut_box)
        layout.addStretch()

        self.ffmpeg_buttons = QHBoxLayout()
        self.install_ffmpeg_btn = self._button(
            "Install FFmpeg now", "#2ecc71", self.install_ffmpeg, True)
        self.ffmpeg_buttons.addWidget(self.install_ffmpeg_btn)
        self.open_ffmpeg_btn = self._button(
            "Open FFmpeg download page", "#e67e22", self.open_ffmpeg_page)
        self.ffmpeg_buttons.addWidget(self.open_ffmpeg_btn)
        self.ffmpeg_buttons.addStretch()
        layout.addLayout(self.ffmpeg_buttons)

        self.recheck_btn = self._button("Check again", "#5a5a5a",
                                        self.refresh_ffmpeg)
        self.recheck_btn.setVisible(False)
        layout.addWidget(self.recheck_btn)

        self.refresh_ffmpeg()

    def refresh_ffmpeg(self):
        """Show either the 'installed' state or the full how-to."""
        found = ffmpeg_path()
        if found:
            self.ffmpeg_status.setText("FFmpeg is already installed.")
            self.ffmpeg_status.setStyleSheet(
                "background: transparent; border: none; color: #2ecc71; "
                "font-size: 14px; font-weight: bold;")
            self.ffmpeg_body.setText(f"Found at: {found}\n\n"
                                     "MP3 export and high quality 1080p+ "
                                     "merging are available. Nothing to do "
                                     "on this screen - press Next.")
            self.tut_box.setVisible(False)
            # Only offer FFmpeg actions when it is actually missing.
            self.install_ffmpeg_btn.setVisible(False)
            self.open_ffmpeg_btn.setVisible(False)
            self.recheck_btn.setVisible(False)
            return

        self.ffmpeg_status.setText("FFmpeg is not installed.")
        self.ffmpeg_status.setStyleSheet(
            "background: transparent; border: none; color: #f39c12; "
            "font-size: 14px; font-weight: bold;")
        self.ffmpeg_body.setText(
            "This is optional - you can skip it and install later.\n\n"
            "Without FFmpeg the app still downloads MP4 video normally. "
            "Installing it adds exactly two things:\n"
            "     -  MP3 (audio only) export\n"
            "     -  merging separate video and audio for 1080p and above")

        while self.tut_layout.count():
            item = self.tut_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detach now, not via deleteLater(): a deferred delete leaves
                # the old widgets on screen until the event loop spins, so they
                # would overlap the freshly built tutorial.
                widget.setParent(None)
        self.tut_layout.addWidget(label("How to install FFmpeg", 14,
                                        "#ffffff", True))
        steps = []
        if winget_path():
            steps.append(
                "EASIEST - press the green button below. It runs this for you:\n"
                "      winget install --id Gyan.FFmpeg -e")
        else:
            steps.append(
                "1.  Open https://www.gyan.dev/ffmpeg/builds/\n"
                "2.  Download the 'essentials' .zip (any of the builds).\n"
                "3.  Extract it somewhere permanent, e.g. C:\\ffmpeg\n"
                "4.  Add C:\\ffmpeg\\bin to your PATH:\n"
                "        -  press Win, type 'environment variables'\n"
                "        -  Edit the environment variables\n"
                "        -  Path -> Edit -> New -> paste C:\\ffmpeg\\bin\n"
                "5.  Close this installer and press 'Check again'.")
        for text in steps:
            self.tut_layout.addWidget(label(text, 12, "#cfcfcf", wrap=True))

        self.tut_box.setVisible(True)
        self.install_ffmpeg_btn.setVisible(bool(winget_path()))
        self.open_ffmpeg_btn.setVisible(True)
        self.recheck_btn.setVisible(True)

    def install_ffmpeg(self):
        if not winget_path():
            return
        self.status.setText("Installing FFmpeg with winget - this can take "
                            "a minute...")
        try:
            subprocess.run(["winget", "install", "--id", FFMPEG_WINGET,
                            "-e", "--accept-source-agreements",
                            "--accept-package-agreements"],
                           capture_output=True, timeout=900)
        except Exception as exc:  # noqa: BLE001 - offer the manual route
            log_line(f"[ffmpeg] winget failed: {exc}")
        self.refresh_ffmpeg()
        self.status.setText("")

    def open_ffmpeg_page(self):
        import webbrowser
        webbrowser.open(FFMPEG_PAGE)

    # -- page 3: install ----------------------------------------------------- #
    def _build_install(self):
        layout = self._page()
        layout.addWidget(label("Install location", 20, "#ffffff", True))
        layout.addSpacing(10)
        layout.addWidget(label(
            "Choose where the app should be installed.\n"
            f"\"{APP_FOLDER_NAME}\" is always added to the end for you, so\n"
            f"picking Downloads gives you Downloads\\{APP_FOLDER_NAME}.\n"
            "Nothing is created until you press Install.", 13, "#cfcfcf",
            wrap=True))
        layout.addSpacing(14)

        row = QHBoxLayout()
        self.dir_edit = QLineEdit(default_install_dir())
        self.dir_edit.setStyleSheet(
            "background-color: #1c1c1c; border: 1px solid #3a3a3a; "
            "border-radius: 5px; padding: 9px; color: #ffffff;")
        row.addWidget(self.dir_edit, 1)
        browse = self._button("Browse...", "#5a5a5a", self.browse)
        row.addWidget(browse)
        layout.addLayout(row)
        layout.addSpacing(18)

        self.desktop_check = QCheckBox("Create a Desktop shortcut")
        self.desktop_check.setChecked(True)
        self.desktop_check.setStyleSheet("color: #cfcfcf; font-size: 13px;")
        layout.addWidget(self.desktop_check)
        self.startmenu_check = QCheckBox("Create a Start Menu entry")
        self.startmenu_check.setChecked(True)
        self.startmenu_check.setStyleSheet("color: #cfcfcf; font-size: 13px;")
        layout.addWidget(self.startmenu_check)

        self.launch_check = QCheckBox(
            "Launch " + APP_NAME + " when the install finishes")
        self.launch_check.setChecked(True)
        self.launch_check.setStyleSheet("color: #cfcfcf; font-size: 13px;")
        layout.addWidget(self.launch_check)

        # Only offered when this installer was built without the app inside it.
        self.github_check = None
        if not bundled_app_exe() and GITHUB_APP_URL:
            self.github_check = QCheckBox("Download the app from GitHub")
            self.github_check.setChecked(True)
            self.github_check.setStyleSheet("color: #2ecc71; font-size: 13px;")
            layout.addWidget(self.github_check)
            layout.addWidget(label(
                "This installer does not carry the app, so it fetches the "
                "latest build from GitHub. An internet connection is needed.",
                12, "#8a8a8a", wrap=True))
        layout.addStretch()

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedHeight(16)
        self.progress.setStyleSheet(
            "QProgressBar { background-color: #2a2a2a; border: none; "
            "border-radius: 4px; color: #ffffff; }"
            "QProgressBar::chunk { background-color: #ff2e63; "
            "border-radius: 4px; }")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

    def browse(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose install folder", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    # -- page 4: done -------------------------------------------------------- #
    def _build_done(self):
        layout = self._page()
        layout.addWidget(label("All done", 20, "#2ecc71", True))
        layout.addSpacing(10)
        self.done_body = label("", 13, "#cfcfcf", wrap=True)
        layout.addWidget(self.done_body)
        layout.addStretch()
        self.launch_btn = self._button("Open " + APP_NAME, "#ff2e63",
                                       self.launch_app, True)
        layout.addWidget(self.launch_btn, 0)

    def done_text(self, target, shortcuts, ffmpeg):
        lines = [f"{APP_NAME} has been installed to:\n    {target}\n"]
        if shortcuts:
            lines.append("Shortcuts created: " + ", ".join(shortcuts) + "\n")
        lines.append("FFmpeg: " + ("installed" if ffmpeg else
                                   "not installed (optional - MP3 export is "
                                   "unavailable until it is added)"))
        return "\n".join(lines)

    # -- navigation ---------------------------------------------------------- #
    def go(self, index):
        self.pages.setCurrentIndex(index)
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setText("Install" if index == 2 else "Next")
        self.next_btn.setEnabled(
            self._source_exe is not None if index == 2 else True)
        if index == 3:
            self.next_btn.setVisible(False)
        else:
            self.next_btn.setVisible(True)
        self.status.setText("")

    def go_next(self):
        index = self.pages.currentIndex()
        if index == 2:
            self.run_install()
            return
        self.go(index + 1)

    def go_back(self):
        self.go(self.pages.currentIndex() - 1)

    # -- the actual work ----------------------------------------------------- #
    def resolve_target_dir(self):
        """Install folder, with the product name always appended.

        Choosing "Downloads" has to give "Downloads\\Vex Downloader" rather
        than scattering files straight into Downloads. Nothing is created
        here - only when Install is pressed.
        """
        chosen = self.dir_edit.text().strip().rstrip("\\/")
        if not chosen:
            return None
        if os.path.basename(chosen).lower() == APP_FOLDER_NAME.lower():
            return chosen
        return os.path.join(chosen, APP_FOLDER_NAME)

    def run_install(self):
        target_dir = self.resolve_target_dir()
        if not target_dir:
            QMessageBox.warning(self, "No folder",
                                "Please choose a folder to install into.")
            return
        if self._source_exe is None and not (self.github_check
                                            and GITHUB_APP_URL):
            QMessageBox.critical(
                self, "Missing app",
                f"{APP_EXE} was not found inside the installer and no "
                "download source is configured. Please re-download the "
                "installer.")
            return

        self.progress.setVisible(True)
        self.progress.setValue(5)
        self.next_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        QApplication.processEvents()

        try:
            os.makedirs(target_dir, exist_ok=True)
            target_exe = os.path.join(target_dir, APP_EXE)
            self.progress.setValue(35)
            QApplication.processEvents()

            if self._source_exe is None:
                # No embedded copy - pull it from GitHub instead.
                self.status.setText("Downloading " + APP_NAME + "...")
                self.progress.setValue(35)
                if not self.download_app_from_github(target_exe):
                    QMessageBox.critical(
                        self, "Download failed",
                        "Could not download the app from GitHub. Check your "
                        "internet connection and try again.")
                    self.next_btn.setEnabled(True)
                    self.back_btn.setEnabled(True)
                    self.progress.setVisible(False)
                    return
            else:
                # Copy in chunks so the bar actually moves on a big file.
                total = os.path.getsize(self._source_exe)
                copied = 0
                step = 4 * 1024 * 1024
                with open(self._source_exe, "rb") as src, \
                        open(target_exe, "wb") as dst:
                    while True:
                        chunk = src.read(step)
                        if not chunk:
                            break
                        dst.write(chunk)
                        copied += len(chunk)
                        self.progress.setValue(
                            35 + int(45 * copied / max(1, total)))
                        QApplication.processEvents()

            # Ship the assets folder so logo.png still works after install.
            self.progress.setValue(85)
            QApplication.processEvents()
            assets_src = bundled_assets_dir()
            assets_dst = os.path.join(target_dir, "assets")
            if assets_src and os.path.isdir(assets_src):
                if os.path.isdir(assets_dst):
                    shutil.rmtree(assets_dst, ignore_errors=True)
                shutil.copytree(assets_src, assets_dst)

            shortcuts = []
            self.progress.setValue(93)
            QApplication.processEvents()
            if self.startmenu_check.isChecked():
                programs = os.path.join(
                    os.environ.get("APPDATA", os.path.expanduser("~")),
                    "Microsoft", "Windows", "Start Menu", "Programs")
                try:
                    os.makedirs(programs, exist_ok=True)
                    if make_shortcut(os.path.join(programs, APP_NAME + ".lnk"),
                                     target_exe, target_dir,
                                     f"{APP_NAME} {APP_VERSION}", target_exe):
                        shortcuts.append("Start Menu")
                except Exception as exc:  # noqa: BLE001
                    log_line(f"[shortcut] start menu failed: {exc}")
            if self.desktop_check.isChecked():
                desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                if make_shortcut(os.path.join(desktop, APP_NAME + ".lnk"),
                                 target_exe, target_dir,
                                 f"{APP_NAME} {APP_VERSION}", target_exe):
                    shortcuts.append("Desktop")

            # The docs are written once here rather than every time the app
            # starts, so a portable copy of the exe stays read-only-friendly.
            self._write_docs(target_dir)

            self.progress.setValue(100)
            self.done_body.setText(
                self.done_text(target_dir, shortcuts, bool(ffmpeg_path())))
            self._installed_exe = target_exe
            self._auto_launch = bool(self.launch_check.isChecked())
            self.go(3)
            self.maybe_launch()
        except Exception as exc:  # noqa: BLE001 - report, never half-install
            log_line(f"[install] failed: {exc}")
            QMessageBox.critical(self, "Install failed", str(exc))
            self.next_btn.setEnabled(True)
            self.back_btn.setEnabled(True)
        finally:
            self.progress.setVisible(False)

    def _write_docs(self, target_dir):
        """Write README.md / how to install.txt / requirements.txt once."""
        exe_name = APP_EXE if bundled_app_exe() else APP_NAME + ".py"
        version = read_app_version()
        values = {"name": APP_NAME, "version": version, "author": APP_AUTHOR,
                  "exe": exe_name}
        for filename, template in DOC_TEMPLATES.items():
            try:
                with open(os.path.join(target_dir, filename), "w",
                          encoding="utf-8") as handle:
                    handle.write(template.format(**values))
            except Exception as exc:  # noqa: BLE001 - docs are not load-bearing
                log_line(f"[install] could not write {filename}: {exc}")

    def download_app_from_github(self, target_exe):
        """Fetch the app exe from GitHub. Returns True on success."""
        if not GITHUB_APP_URL:
            return False
        import urllib.request
        request = urllib.request.Request(
            GITHUB_APP_URL, headers={"User-Agent": APP_NAME})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                step = 4 * 1024 * 1024
                with open(target_exe, "wb") as handle:
                    while True:
                        chunk = response.read(step)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        if total:
                            self.progress.setValue(
                                35 + int(45 * done / total))
                            QApplication.processEvents()
            return True
        except Exception as exc:  # noqa: BLE001 - fall back with a clear reason
            log_line(f"[install] GitHub download failed: {exc}")
            return False

    def launch_app(self):
        target = getattr(self, "_installed_exe", None)
        if target and os.path.isfile(target):
            try:
                subprocess.Popen([target], cwd=os.path.dirname(target))
            except Exception as exc:  # noqa: BLE001
                log_line(f"[launch] failed: {exc}")
        self.close()

    def maybe_launch(self):
        """Honour the 'launch when finished' tick, then close."""
        if getattr(self, "_auto_launch", False):
            QTimer.singleShot(900, self.launch_app)


def main():
    # A console is useful when something goes wrong; keep errors off-screen.
    sys.excepthook = lambda *a: None
    app = QApplication(sys.argv)

    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "logo.png")
    if os.path.isfile(icon):
        app.setWindowIcon(QIcon(icon))

    window = SetupWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()