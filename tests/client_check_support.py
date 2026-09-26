"""Small native command fixtures for exercising client checks on every OS."""
import os
from pathlib import Path


def client_checker(directory, *, accepts):
    """Write an actual command, including a Windows command file when needed."""
    directory = Path(directory)
    if os.name == 'nt':
        path = directory / 'fake-sing-box.cmd'
        body = '@echo off\n'
        if not accepts:
            body += 'echo unsupported outbound 1>&2\n'
        body += f'exit /b {0 if accepts else 1}\n'
    else:
        path = directory / 'fake-sing-box'
        body = '#!/bin/sh\n'
        if not accepts:
            body += 'echo "unsupported outbound" >&2\n'
        body += f'exit {0 if accepts else 1}\n'
    path.write_text(body, encoding='utf-8')
    path.chmod(0o755)
    return path
