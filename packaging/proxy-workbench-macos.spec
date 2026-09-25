# macOS .app: one self-contained bundle that opens the existing browser GUI.
#
#   pyinstaller --noconfirm packaging/proxy-workbench-macos.spec
#   ARCHFLAGS="$(_PYTHON_SYSCONFIGDATA_NAME -A)" pyinstaller ...
#
# The bundle is read-only for a normal user, so nothing may be written next to
# the executable: proxy_workbench.desktop resolves per-user data/cache/logs and
# the worker gets a writable working directory.  See docs/packaging/README.md.
from pathlib import Path
import re

root = Path(SPECPATH).parent
package = root / 'proxy_workbench'
# Read the version from the single place that defines it, so the bundle can
# never advertise a number the program does not report.
version = re.search(r'PRODUCT_VERSION = "([^"]+)"',
                    (package / 'branding.py').read_text(encoding='utf-8')).group(1)

analysis = Analysis(
    [str(root / 'packaging' / 'desktop_launcher.py')],
    pathex=[str(root)],
    datas=[(str(package / 'ui'), 'proxy_workbench/ui'), (str(package / 'sources.json'), 'proxy_workbench')],
    hiddenimports=['socksio', 'proxy_workbench.desktop', 'proxy_workbench.gui', 'proxy_workbench.proxytool'],
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
        'LSUIElement': False,
    },
)
