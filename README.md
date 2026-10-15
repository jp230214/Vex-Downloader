# Vex YouTube Downloader 2.2.0

A PyQt6 + yt-dlp desktop app for downloading YouTube videos, playlists and
whole channels as MP4 or MP3. Created by Vex.

## Running the portable build

Double-click **`Vex.py`**. Nothing to install first - Python, PyQt6 and yt-dlp
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

    %APPDATA%\VexDownloader\config.json    settings
    %APPDATA%\VexDownloader\history.json   download history
    ~/Downloads/Vex_Download/               downloaded files

Deleting the `VexDownloader` folder resets the app to its first-run state.

## Running from source instead

    pip install -r requirements.txt
    python Vex.py

## Requirements (source install only)

    PyQt6>=6.5
    yt-dlp>=2024.1.1
