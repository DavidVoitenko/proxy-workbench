# PyInstaller build: one executable that opens the GUI when started without arguments.
# Build with: pyinstaller packaging/proxy-workbench.spec
from pathlib import Path

root = Path(SPECPATH).parent
package = root / 'proxy_workbench'

analysis = Analysis(
    [str(root / 'packaging' / 'launcher.py')],
    pathex=[str(root)],
    datas=[(str(package / 'ui'), 'proxy_workbench/ui'), (str(package / 'sources.json'), 'proxy_workbench'), (str(package / 'source-catalog.json'), 'proxy_workbench')],
    hiddenimports=['socksio', 'proxy_workbench.gui', 'proxy_workbench.proxytool'],
    excludes=['tkinter', 'unittest', 'pydoc'],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name='proxy-workbench',
    console=True,
    upx=False,
)
