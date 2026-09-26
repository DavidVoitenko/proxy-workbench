# macOS .app: one self-contained bundle with the background layer inside it.
#
#   pyinstaller --noconfirm packaging/proxy-workbench-macos.spec
#   ARCHFLAGS="$(_PYTHON_SYSCONFIGDATA_NAME -A)" pyinstaller ...
#
# The bundle is read-only for a normal user, so nothing may be written next to
# the executable: proxy_workbench.desktop resolves per-user data/cache/logs and
# the worker gets a writable working directory.  See docs/packaging/README.md.
#
# Two things in here are not decoration, and a build that leaves either out
# still produces a runnable application that does not do what it promises:
#
#   * the menu bar helper (F22).  The status item is a native Swift program
#     whose source lives in proxy_workbench.desktop.  On a source checkout the
#     application compiles it on first run, but an installed application has no
#     swiftc and no writable cache to compile into, so the build compiles it
#     here and collects it as data.  Without this the app runs with no menu bar
#     and says so - which is a truthful failure and a missing feature;
#   * the interface assets.  ui/ and sources.json are read at run time, so an
#     app without them starts and shows nothing.
import os
from pathlib import Path
import re
import sys

root = Path(SPECPATH).parent
package = root / 'proxy_workbench'
# This spec is a Python script, so it can import the build helper next to it
# instead of repeating how the menu bar program is compiled.
sys.path.insert(0, str(root))
sys.path.insert(0, str(Path(SPECPATH)))
import tray_helper

# Read the version from the single place that defines it, so the bundle can
# never advertise a number the program does not report.
version = re.search(r'PRODUCT_VERSION = "([^"]+)"',
                    (package / 'branding.py').read_text(encoding='utf-8')).group(1)

# F22: compile the menu bar helper and collect it.  A missing swiftc stops the
# build here rather than shipping an application whose menu bar does not exist.
tray, tray_reason = tray_helper.stage(Path(root) / 'build' / 'tray')
if tray is None:
    raise SystemExit(f'the macOS bundle needs the menu bar helper and it was not built: {tray_reason}\n'
                     'Install the Xcode Command Line Tools (xcode-select --install) and build again.')
os.chmod(tray, 0o755)

analysis = Analysis(
    [str(root / 'packaging' / 'desktop_launcher.py')],
    pathex=[str(root)],
    datas=[(str(package / 'ui'), 'proxy_workbench/ui'),
           (str(package / 'sources.json'), 'proxy_workbench'), (str(package / 'source-catalog.json'), 'proxy_workbench'),
           (str(tray), tray_helper.BUNDLE_SUBFOLDER)],
    hiddenimports=['socksio', 'proxy_workbench.desktop', 'proxy_workbench.gui', 'proxy_workbench.proxytool',
                   'proxy_workbench.__main__'],
    excludes=['tkinter', 'unittest', 'pydoc'],
)
pyz = PYZ(analysis.pure)
# Onedir inside the bundle, not onefile: PyInstaller 6.22 reports that onefile
# combined with a .app bundle "clashes with macOS's security" and will be an
# error in v7.  It also keeps the packaged resources inspectable on disk, which
# is what the build verifies.
exe = EXE(
    pyz,
    analysis.scripts,
    exclude_binaries=True,
    name='Proxy Workbench',
    console=False,
    upx=False,
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name='Proxy Workbench',
)
app = BUNDLE(
    coll,
    name='Proxy Workbench.app',
    bundle_identifier='org.proxyworkbench.app',
    version=version,
    info_plist={
        'CFBundleName': 'Proxy Workbench',
        'CFBundleDisplayName': 'Proxy Workbench',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        'LSMinimumSystemVersion': '11.0',
        'LSApplicationCategoryType': 'public.app-category.developer-tools',
        'NSHighResolutionCapable': True,
        # The app is a launcher for a loopback page: a dock icon points at the
        # only window the user has (their browser), so it stays a normal app.
        # The menu bar is a separate helper process with its own accessory
        # activation policy; this key says nothing about it either way.
        'LSUIElement': False,
    },
)
