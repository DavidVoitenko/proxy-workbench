"""Entry point for the Windows executable: no arguments open the GUI, anything else runs the CLI."""
from proxy_workbench.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main())
