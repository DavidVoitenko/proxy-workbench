"""Verify runtime resources in the wheel, without importing the source checkout."""
import json
from pathlib import Path
import sys
import zipfile


def verify(path):
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        for filename in ('sources.json', 'source-catalog.json', 'openapi.json'):
            value = json.loads(wheel.read(f'proxy_workbench/{filename}'))
            if not value:
                raise ValueError(f'{filename} is empty')
        catalog = json.loads(wheel.read('proxy_workbench/source-catalog.json'))
        if not catalog.get('sources'):
            raise ValueError('the wheel has no built-in source catalog')
        for filename in ('index.html', 'app.js', 'style.css'):
            if not wheel.read(f'proxy_workbench/ui/{filename}'):
                raise ValueError(f'ui/{filename} is empty')
        for language in ('de', 'es', 'fr', 'it', 'ja', 'pl', 'pt', 'tr', 'uk', 'zh'):
            path = f'proxy_workbench/ui/i18n/{language}.js'
            if path not in names or not wheel.read(path):
                raise ValueError(f'{path} is absent or empty')
    return True


if __name__ == '__main__':
    for argument in sys.argv[1:]:
        verify(argument)
        print(f'wheel resources verified: {Path(argument).name}')
