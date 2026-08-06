# PyInstaller spec — builds a single-file `jobradar` executable.
#
# Build:  pyinstaller jobradar.spec        (run on the OS you want to target)
# Output: dist/jobradar        (Linux/macOS)
#         dist/jobradar.exe     (Windows)
#
# PyInstaller does NOT cross-compile: run it on Windows to get a .exe. The
# GitHub Actions workflow in .github/workflows/build-exe.yml does exactly that
# on a windows-latest runner and uploads the .exe as an artifact.

block_cipher = None

a = Analysis(
    ["jobradar/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    # Collectors register themselves via import side-effects; list them so the
    # frozen build never drops one.
    hiddenimports=[
        "jobradar.collectors.reddit",
        "jobradar.collectors.bluesky",
        "jobradar.collectors.hackernews",
        "jobradar.collectors.rss",
        "jobradar.collectors.telegram",
        "jobradar.collectors.scrape",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="jobradar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # keep a console window so logs/errors are visible
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
