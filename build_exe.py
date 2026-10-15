"""Build the portable Windows executable for Vex YouTube Downloader.

Produces a single self-contained ``Vex YouTube Downloader.exe`` with Python,
PyQt6 and yt-dlp bundled inside, so the user only needs to unzip and run it.

    py build_exe.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "Vex Downloader"
ENTRY = os.path.join(ROOT, "Vex.py")
ICO = os.path.join(ROOT, "build_icon.ico")


def make_icon():
    """Turn assets/logo.png into a multi-size .ico for the executable.

    Windows uses the icon for the file itself, the taskbar and Alt-Tab, and a
    multi-size .ico keeps it crisp at every size. Falls back to leaving the
    file absent (PyInstaller then uses its default icon) rather than failing.
    """
    source = os.path.join(ROOT, "assets", "logo.png")
    if not os.path.isfile(source):
        print("[build] assets/logo.png not found - using the default exe icon.")
        return False
    try:
        from PIL import Image
        image = Image.open(source).convert("RGBA")
        image.save(ICO, format="ICO",
                   sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
        print(f"[build] icon built from logo.png -> {ICO}")
        return True
    except Exception as exc:  # noqa: BLE001 - an icon is nice-to-have only
        print(f"[build] Could not build an .ico ({exc}); using the default icon.")
        return False


def main():
    if not os.path.isfile(ENTRY):
        raise SystemExit(f"Entry point not found: {ENTRY}")

    have_icon = make_icon()

    # Trim the bundle of things nothing touches. Do NOT exclude the stdlib
    # networking/parsing modules (http, email, xml, sqlite3): yt-dlp imports
    # http.cookiejar for browser-cookie sign-in, and email/xml for headers and
    # playlist parsing. Excluding them breaks `import yt_dlp` outright.
    excludes = [
        "tkinter", "turtle", "unittest", "pydoc_data", "test", "distutils",
        "numpy", "pandas", "matplotlib", "scipy", "PIL", "IPython",
        "setuptools", "pip",
    ]
    # yt-dlp reaches these lazily; collect them so a frozen build behaves like
    # a source install. (pycryptodomex / doctools are optional and may not be
    # installed at all - yt-dlp degrades gracefully without them.)
    hidden = [
        "yt_dlp.compat._legacy", "yt_dlp.compat", "yt_dlp.utils",
        "yt_dlp.networking", "yt_dlp.aes",
        "certifi", "brotli", "websockets", "mutagen",
    ]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",                 # GUI app: no console window
        "--clean",
        "--noconfirm",
        "--name", NAME,
        "--add-data", f"{os.path.join(ROOT, 'assets')}{os.pathsep}assets",
    ]
    for mod in hidden:
        cmd += ["--hidden-import", mod]
    for mod in excludes:
        cmd += ["--exclude-module", mod]
    if have_icon:
        cmd += ["--icon", ICO]
    cmd.append(ENTRY)

    print("[build] running PyInstaller...")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit("PyInstaller failed.")

    exe = os.path.join(ROOT, "dist", f"{NAME}.exe")
    if not os.path.isfile(exe):
        raise SystemExit(f"Build finished but no exe at {exe}")

    size_mb = os.path.getsize(exe) / (1024 * 1024)
    print(f"\n[build] DONE -> {exe}")
    print(f"[build] size: {size_mb:.1f} MB")

    if os.path.isfile(ICO):
        os.remove(ICO)


if __name__ == "__main__":
    main()