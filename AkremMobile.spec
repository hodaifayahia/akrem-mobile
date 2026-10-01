# PyInstaller one-folder build; run on a Windows machine with the project venv active.
from pathlib import Path

import psycopg  # Ensure psycopg is initialized before collecting psycopg-binary files.
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

project_root = Path(SPECPATH).resolve()
datas = []
for source_root in (
    project_root / "app" / "resources",
    project_root / "app" / "db" / "migrations",
):
    for source in source_root.rglob("*"):
        if source.is_file():
            datas.append((str(source), str(source.parent.relative_to(project_root))))
datas.append((str(project_root / "app" / "db" / "alembic.ini"), "app/db"))

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=collect_dynamic_libs("psycopg_binary"),
    datas=datas,
    hiddenimports=(
        collect_submodules("app.db.migrations.versions")
        + collect_submodules("psycopg")
        + collect_submodules("sqlalchemy.dialects.sqlite")
        + collect_submodules("sqlalchemy.dialects.postgresql")
        + ["logging.config", "logging.handlers", "psycopg_binary._uuid", "xlrd"]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
icon_path = project_root / "app" / "resources" / "icon.ico"
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AkremMobile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(icon_path) if icon_path.exists() else None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AkremMobile",
)
