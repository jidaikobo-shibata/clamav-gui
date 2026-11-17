"""Entry point for the self-contained macOS application."""
import sys

if '--check-runtime' in sys.argv:
    import os
    os.environ['GDK_BACKEND'] = 'none'
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk
    from clam_gui.core import Store
    from clam_gui.macos_setup import MacSetup
    os.write(1, f'Runtime OK: Python {sys.version_info.major}.{sys.version_info.minor}, GTK {Gtk.get_major_version()}\n'.encode())
    raise SystemExit(0)

if '--smoke-test' in sys.argv or '--hidden-smoke-test' in sys.argv:
    from scripts.smoke_gui_macos import main
    raise SystemExit(main(show_windows='--show-test-windows' in sys.argv,
                         hidden='--hidden-smoke-test' in sys.argv))

from clam_gui.__main__ import Application

raise SystemExit(Application().run())
