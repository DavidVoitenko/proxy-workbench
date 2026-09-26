"""The menu bar helper the macOS bundle has to contain.

F22 is implemented in ``proxy_workbench.desktop``: the status item is a small
native Swift program whose source is embedded there, compiled once per machine
and cached under a digest of that source.  A build that does not compile it
first produces a bundle that starts, serves its page and has **no menu bar at
all** - and the application would honestly say "this build has no menu bar
helper" instead of failing, which is the worst possible outcome for a release:
the artifact works, and half of what it promises is missing.

So the helper is compiled by the build, staged under the plain name
``desktop.resource_path('tray', TRAY_HELPER_NAME)`` looks for, and collected
into the bundle as data.  Compilation itself is the product's own
``ensure_tray_helper`` - the build does not get a second, drifting copy of how
the helper is built, and a missing ``swiftc`` produces the product's own
sentence about it.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil

#: Where the helper goes inside a macOS bundle: ``datas=[(helper, 'tray')]``
#: collects it at ``sys._MEIPASS/tray/<name>``, and ``sys._MEIPASS`` is what
#: ``desktop.resource_path`` is built from.  For a bundle that directory is
#: ``Contents/Frameworks``.
BUNDLE_SUBFOLDER = 'tray'
BUNDLE_DATA_DIR = Path('Contents') / 'Frameworks'


def build(cache, *, finder=None, runner=None):
    """Compile the helper once into ``cache/tray`` and return ``(path, reason)``.

    ``path`` is ``None`` when this machine cannot produce the helper; the
    reason is the product's own, so the build log and the running application
    say the same thing about why there is no menu bar.
    """
    from proxy_workbench import desktop
    cache = Path(cache)
    layout = desktop.Layout(data=cache, cache=cache, logs=cache, mode='build',
                            root=cache, reason='macOS artifact build')
    return desktop.ensure_tray_helper(layout, finder=finder, runner=runner)


def stage(cache, staging=None):
    """The helper under the plain name the bundle looks for.

    The cached file carries the source digest in its name, so a changed source
    never reuses a stale binary - but the bundle has to contain exactly one
    path, and that path must not change when the source does.  Returns
    ``(path, reason)``; ``path`` is ``None`` when the helper cannot be built.
    """
    from proxy_workbench import desktop
    path, reason = build(cache)
    if path is None:
        return None, reason
    staging = Path(staging) if staging is not None else Path(cache) / 'staged'
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / desktop.TRAY_HELPER_NAME
    shutil.copy2(path, target)
    os.chmod(target, 0o755)
    return target, reason


def in_bundle(bundle):
    """The helper inside a built bundle, or None when the spec forgot it."""
    from proxy_workbench import desktop
    candidate = Path(bundle) / BUNDLE_DATA_DIR / BUNDLE_SUBFOLDER / desktop.TRAY_HELPER_NAME
    return candidate if candidate.is_file() else None
