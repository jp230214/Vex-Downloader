"""Build both distributables:

    1. Vex YouTube Downloader.exe   the portable app (built by build_exe.py)
    2. Vex-Setup.exe                a one-time installer carrying the app

Run:  py build_setup.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_EXE = "Vex Downloader.exe"


def build_app():
    """Build the portable app first - the installer needs it to embed."""
    print("[build] === building the portable app ===")
    result = subprocess.run([sys.executable, os.path.join(ROOT, "build_exe.py")],
                            cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit("App build failed.")
    app_path = os.path.join(ROOT, "dist", APP_EXE)
    if not os.path.isfile(app_path):
        raise SystemExit(f"App build produced no exe at {app_path}")
    return app_path


def build_installer(app_path):
    print("[build] === building Vex-Setup.exe ===")

    icon = os.path.join(ROOT, "assets", "logo.png")
    have_icon = False
    ico = os.path.join(ROOT, "_setup_icon.ico")
    try:
        from PIL import Image
        Image.open(icon).convert("RGBA").save(
            ico, format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        have_icon = True
    except Exception as exc:  # noqa: BLE001
        print(f"[build] no icon ({exc}); using the default.")

    # The app executable travels as *data* so it can be copied out at install
    # time rather than being the frozen installer itself.
    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--clean", "--noconfirm",
        "--name", "Vex-Setup",
        "--add-data", f"{app_path}{sep}.",
        "--add-data", f"{os.path.join(ROOT, 'assets')}{sep}assets",
        "--exclude-module", "tkinter",
        "--exclude-module", "turtle",
        "--exclude-module", "unittest",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "PIL",
        "--exclude-module", "IPython",
        "--exclude-module", "setuptools",
        "--exclude-module", "pip",
    ]
    if have_icon:
        cmd += ["--icon", ico]
    cmd.append(os.path.join(ROOT, "setup_installer.py"))

    result = subprocess.run(cmd, cwd=ROOT)
    if have_icon and os.path.isfile(ico):
        os.remove(ico)
    if result.returncode != 0:
        raise SystemExit("Installer build failed.")

    setup_exe = os.path.join(ROOT, "dist", "Vex-Setup.exe")
    if not os.path.isfile(setup_exe):
        raise SystemExit(f"No installer at {setup_exe}")
    return setup_exe


def main():
    app_path = build_app()
    setup_exe = build_installer(app_path)

    print("\n[build] === done ===")
    for path in (setup_exe, app_path):
        print(f"  {os.path.basename(path):<28} "
              f"{os.path.getsize(path) / (1024 * 1024):>6.1f} MB   {path}")


if __name__ == "__main__":
    main()