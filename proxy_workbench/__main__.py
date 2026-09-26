"""`proxy-workbench` / `python -m proxy_workbench`: one product, three layers.

The rule is short and every packaged entry point uses it:

* **no arguments** - the desktop host.  The application a user launches by
  double-clicking is the process that owns the menu bar, the single instance,
  the opt-in login item and the sleep/wake hooks (``proxy_workbench.desktop``).
  Going straight to the interface instead would produce a page that dies with
  its tab, which is exactly what F22 forbids;
* **`gui`, or ``--no-desktop``** - the interface alone, with no menu bar and no
  instance lock.  The escape hatch for a second window, a remote session and for
  debugging the host;
* **anything else** - the command line (``proxy_workbench.proxytool``), which is
  the interface the wheel has always exposed and the one a frozen build re-runs
  in a worker process.

F23 asks for a shipped product, and the shipped entry point is this one: before
this routing, a normal launch never reached the background layer at all.
"""
from __future__ import annotations

import sys

#: Arguments the desktop host owns: its own commands, its own flags, and the
#: flags of the interface it hands over to.  An empty command line is the host
#: too - that is the double-click.
DESKTOP_ARGS = frozenset({
    # host commands, answered without ever opening the interface
    '--print-paths', '--portable', '--status', '--update-notice',
    '--autostart', '--autostart-status', '--migrate-preview',
    # host flags
    '--background', '--no-tray',
    # interface flags, forwarded to proxy_workbench.gui
    '--data', '--port', '--api-port', '--no-api', '--no-browser',
    '--gateway-port', '--gateway-host', '--gateway-interface', '--gateway-token', '--no-gateway', '--lan',
})

#: The word that asks for the interface and nothing else.
INTERFACE_ONLY = 'gui'

#: Asks for the interface instead of the desktop host, with the host still
#: available to whoever wants it.
NO_DESKTOP = '--no-desktop'

_CLI_COMMANDS = None
_CLI_VALUE_OPTIONS = frozenset()


def cli_commands():
    """The command words the CLI accepts, read from the parser that defines them.

    Read from the parser rather than copied here on purpose: a verb added to
    ``proxytool`` must reach the CLI on its own, and a frozen build runs the
    same entry point - its worker is this program with ``scan`` in front of it.
    """
    global _CLI_COMMANDS, _CLI_VALUE_OPTIONS
    if _CLI_COMMANDS is None:
        from .proxytool import parser
        actions = parser()._actions
        _CLI_VALUE_OPTIONS = frozenset(option for action in actions if action.nargs != 0
                                       for option in action.option_strings)
        for action in actions:
            if getattr(action, 'dest', '') == 'command' and action.choices:
                _CLI_COMMANDS = frozenset(action.choices) | {'sources'}
                break
        else:
            _CLI_COMMANDS = frozenset()
    return _CLI_COMMANDS


def _has_cli_command(argv):
    """Find the verb without mistaking an option's value for a command."""
    commands = cli_commands()
    values = _CLI_VALUE_OPTIONS | {'--api-port', '--gateway-port', '--gateway-host', '--gateway-interface'}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in values:
            index += 2
            continue
        if not token.startswith('-'):
            return token in commands
        index += 1
    return False


def resolve(argv):
    """Decide which layer this invocation is for: ``('desktop'|'interface'|'cli', argv)``.

    The decision is made on the whole command line, not on its first word: the
    CLI accepts options before the verb (``proxy-workbench --workers 8 run``),
    and routing on the first token would send that to the interface.
    """
    argv = list(argv)
    if argv and argv[0] == INTERFACE_ONLY:
        return 'interface', argv[1:]
    if _has_cli_command(argv):
        return 'cli', argv
    if not argv:
        return 'desktop', argv
    if NO_DESKTOP in argv:
        return 'interface', [token for token in argv if token != NO_DESKTOP]
    if argv[0].split('=', 1)[0] in DESKTOP_ARGS:
        return 'desktop', argv
    return 'cli', argv


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    target, argv = resolve(argv)
    if target == 'interface':
        from . import gui
        return gui.main(argv)
    if target == 'desktop':
        from . import desktop
        return desktop.main(argv)
    from . import proxytool
    return proxytool.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
