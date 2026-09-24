#!/usr/bin/env python3
"""Command line from a source checkout: `python proxytool.py run ...` (same as `proxy-workbench run ...`)."""
from proxy_workbench.proxytool import main

if __name__ == '__main__':
    raise SystemExit(main())
