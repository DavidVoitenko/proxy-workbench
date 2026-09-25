# Windows GUI build: no console window on double-click, browser GUI as before.
#
#   pyinstaller --noconfirm packaging/proxy-workbench-windows-gui.spec
#
# console=False is the whole point of this file (F23: "GUI без лишней консоли").
# The CLI ships separately as proxy-workbench-windows-cli.spec, because a
# windowed process has nowhere to print.
from pathlib import Path

root = Path(SPECPATH).parent
package = root / 'proxy_workbench'

analysis = Analysis(
    [str(root / 'packaging' / 'desktop_launcher.py')],
    pathex=[str(root)],
    datas=[(str(package / 'ui'), 'proxy_workbench/ui'), (str(package / 'sources.json'), 'proxy_workbench')],
    hiddenimports=['socksio', 'proxy_workbench.desktop', 'proxy_workbench.gui', 'proxy_workbench.proxytool'],
    excludes=['tkinter', 'unittest', 'pydoc'],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name='proxy-workbench-gui',
    console=False,
    upx=False,
)
