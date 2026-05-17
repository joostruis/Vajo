#!/usr/bin/env python3
"""
modules/i18n.py — Shared translation setup for Vajo modules.

Both vajo_core.py and any submodules import _ and ngettext from here,
avoiding circular dependencies while keeping translations consistent.
"""

import gettext
import locale

try:
    locale.setlocale(locale.LC_ALL, '')
    localedir = '/usr/share/locale'
    gettext.bindtextdomain('vajo_ui', localedir)
    gettext.textdomain('vajo_ui')
    _ = gettext.gettext
    ngettext = gettext.ngettext
except Exception:
    print("Warning: Could not set up locale. Using fallback translations.")
    _ = lambda s: s
    ngettext = lambda s, p, n: s if n == 1 else p
