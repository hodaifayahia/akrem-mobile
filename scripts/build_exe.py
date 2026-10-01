#!/usr/bin/env python3
"""Build AkremMobile executable and distribution packages.

Usage:
    python scripts/build_exe.py [--skip-zip] [--skip-installer]

Features:
    - Automatically generates app/resources/icon.ico from logo.png if missing
    - Cleans previous build/ and dist/ artifacts
    - Compiles executable using PyInstaller and AkremMobile.spec
    - Creates a ready-to-distribute ZIP archive in dist/
    - Compiles Inno Setup installer if ISCC.exe is detected
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
APP_NAME = "AkremMobile"
SPEC_FILE = PROJECT_ROOT / f"{APP_NAME}.spec"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
RESOURCES_DIR = PROJECT_ROOT / "app" / "resources"
INSTALLER_ISS = PROJECT_ROOT / "installer" / f"{APP_NAME}.iss"


def print_step(title: str) -> None:
    """Print a visually distinct step header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def get_app_version() -> str:
    """Read the application version from app/config.py."""
    try:
        config_path = PROJECT_ROOT / "app" / "config.py"
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("APP_VERSION"):
                return line.split("=")[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "0.1.0"


def ensure_icon() -> Path | None:
    """Ensure .ico and installer branding bitmap images exist."""
    logo_path = RESOURCES_DIR / "logo.png"
    ico_path = RESOURCES_DIR / "icon.ico"
    small_bmp = RESOURCES_DIR / "installer_small.bmp"
    large_bmp = RESOURCES_DIR / "installer_large.bmp"

    if not logo_path.is_file():
        return ico_path if ico_path.is_file() else None

    try:
        from PIL import Image

        # 1. Windows Application and Setup Icon (.ico)
        if not ico_path.is_file():
            img = Image.open(logo_path)
            img.save(
                ico_path,
                format="ICO",
                sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
            print(f"  [+] Generated application icon: {ico_path.name}")

        # 2. Inno Setup Wizard Header Logo (55x55 BMP)
        if not small_bmp.is_file():
            img_small = Image.open(logo_path).convert("RGB")
            img_small = img_small.resize((55, 55), Image.Resampling.LANCZOS)
            img_small.save(small_bmp, format="BMP")
            print(f"  [+] Generated installer header logo: {small_bmp.name}")

        # 3. Inno Setup Wizard Welcome/Finish Sidebar (164x314 BMP)
        if not large_bmp.is_file():
            bg = Image.new("RGB", (164, 314), color=(15, 23, 42))  # Slate dark navy
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((134, 134), Image.Resampling.LANCZOS)
            pos_x = (164 - logo.width) // 2
            pos_y = (314 - logo.height) // 2 - 20
            bg.paste(logo, (pos_x, pos_y), logo)
            bg.save(large_bmp, format="BMP")
            print(f"  [+] Generated installer sidebar banner: {large_bmp.name}")

        return ico_path
    except Exception as exc:
        print(f"  [!] Warning: Could not generate branding assets ({exc}). Continuing.")
        return ico_path if ico_path.is_file() else None


def clean_previous_builds() -> None:
    """Remove previous build cache and target folders while preserving installer outputs."""
    print("  [*] Cleaning build artifacts...")
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)

    app_dist = DIST_DIR / APP_NAME
    if app_dist.exists():
        shutil.rmtree(app_dist, ignore_errors=True)
    print("  [+] Cleaned build directory.")


def find_inno_setup_compiler() -> Path | None:
    """Look for ISCC.exe in PATH and standard Windows install locations."""
    # Check PATH first
    iscc_path = shutil.which("ISCC.exe") or shutil.which("iscc")
    if iscc_path:
        return Path(iscc_path)

    # Standard Windows installation paths
    candidate_paths = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 5" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 5" / "ISCC.exe",
        Path(os.environ.get("LocalAppData", r"C:\Users\Default\AppData\Local")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidate_paths:
        if candidate.is_file():
            return candidate
    return None


def run_pyinstaller() -> bool:
    """Execute PyInstaller using the current Python environment."""
    print(f"  [*] Compiling {APP_NAME} using {SPEC_FILE.name}...")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(SPEC_FILE),
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    return result.returncode == 0


def create_zip_distribution(version: str) -> Path | None:
    """Compress the compiled application folder into a zip file."""
    source_folder = DIST_DIR / APP_NAME
    if not source_folder.exists():
        return None

    zip_filename = f"{APP_NAME}-v{version}-windows.zip"
    zip_path = DIST_DIR / zip_filename
    print(f"  [*] Creating portable distribution archive: {zip_filename}...")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, _, files in os.walk(source_folder):
            for file in files:
                abs_path = Path(root) / file
                rel_path = abs_path.relative_to(DIST_DIR)
                zip_file.write(abs_path, rel_path)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  [+] Created ZIP archive: {zip_path.name} ({size_mb:.1f} MB)")
    return zip_path


def build_inno_installer(iscc_path: Path) -> bool:
    """Compile the Inno Setup installer."""
    if not INSTALLER_ISS.is_file():
        print(f"  [!] Installer script not found: {INSTALLER_ISS}")
        return False

    print(f"  [*] Compiling Inno Setup installer using: {iscc_path}...")
    try:
        import tempfile
        content = INSTALLER_ISS.read_text(encoding="utf-8")
        dist_app = DIST_DIR / APP_NAME
        dist_inst = DIST_DIR / "installer"
        dist_inst.mkdir(parents=True, exist_ok=True)

        content = content.replace(r"..\dist\AkremMobile\*", f"{dist_app}\\*")
        content = content.replace(r"OutputDir=..\dist\installer", f"OutputDir={dist_inst}")
        content = content.replace(r"..\app\resources\icon.ico", str(RESOURCES_DIR / "icon.ico"))
        content = content.replace(r"..\app\resources\installer_small.bmp", str(RESOURCES_DIR / "installer_small.bmp"))
        content = content.replace(r"..\app\resources\installer_large.bmp", str(RESOURCES_DIR / "installer_large.bmp"))

        with tempfile.NamedTemporaryFile("w", suffix=".iss", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        result = subprocess.run([str(iscc_path), str(tmp_path)], cwd=str(PROJECT_ROOT))
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return result.returncode == 0
    except Exception as exc:
        print(f"  [!] Inno Setup compilation encountered error: {exc}")
        return False


def main() -> int:
    """Main build process entry point."""
    # The Windows console's default code page cannot encode all of the status
    # glyphs printed below. Keep the build script running on such consoles.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} desktop executable.")
    parser.add_argument("--skip-zip", action="store_true", help="Skip creating a zip archive.")
    parser.add_argument("--skip-installer", action="store_true", help="Skip Inno Setup installer.")
    args = parser.parse_args()

    version = get_app_version()

    print_step(f"Starting Build Process: {APP_NAME} v{version}")
    print(f"  - Platform:     {sys.platform}")
    print(f"  • Python:       {sys.version.split()[0]} ({sys.executable})")
    print(f"  • Project Root: {PROJECT_ROOT}")

    # 1. Ensure resources & icon
    ensure_icon()

    # 2. Clean previous build artifacts
    clean_previous_builds()

    # 3. Run PyInstaller
    print_step("Step 1: Compiling Executable (PyInstaller)")
    success = run_pyinstaller()
    if not success:
        print(f"\n❌ PyInstaller build failed! Check output above.")
        return 1

    exe_target_win = DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
    exe_target_posix = DIST_DIR / APP_NAME / APP_NAME
    exe_target = exe_target_win if exe_target_win.exists() else exe_target_posix

    if not exe_target.exists():
        print(f"\n❌ Build finished, but expected binary was not found at: {exe_target}")
        return 1

    size_mb = exe_target.stat().st_size / (1024 * 1024)
    print(f"\n✅ Executable built successfully: {exe_target.name} ({size_mb:.1f} MB)")

    # 4. Create ZIP distribution
    zip_path = None
    if not args.skip_zip:
        print_step("Step 2: Packaging ZIP Archive")
        zip_path = create_zip_distribution(version)

    # 5. Inno Setup installer
    installer_built = False
    if not args.skip_installer:
        print_step("Step 3: Building Windows Installer (Optional)")
        iscc_path = find_inno_setup_compiler()
        if iscc_path:
            installer_built = build_inno_installer(iscc_path)
        else:
            print("  [i] Inno Setup compiler (ISCC.exe) was not detected.")
            print("      To build an installer, download Inno Setup (free) from: https://jrsoftware.org/isinfo.php")
            print(f"      Or run: ISCC.exe {INSTALLER_ISS}")

    # 6. Final Summary
    print_step("Build Complete — Summary")
    print(f"  ✅ Executable Folder:    dist/{APP_NAME}/")
    print(f"     Main binary:         dist/{APP_NAME}/{exe_target.name}")
    if zip_path:
        print(f"  ✅ Portable ZIP:        dist/{zip_path.name}")
    if installer_built:
        print(f"  ✅ Installer Package:   dist/installer/{APP_NAME}-Setup-{version}.exe")

    print("\n📦 Updating Client Version Without Touching Data:")
    print("  1. Send the client either:")
    print(f"     - The new '{APP_NAME}.exe' (or updated folder)")
    print(f"     - Or the installer 'dist/installer/{APP_NAME}-Setup-{version}.exe'")
    print("  2. When the client runs the update, their database (%APPDATA%\\AkremMobile\\akremmobile.sqlite3)")
    print("     is preserved untouched. The app automatically creates a pre-upgrade safety backup and")
    print("     applies any new schema migrations seamlessly.")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
