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
    # Try local 'locale' directory first (development), then system path
    import os
    local_locale = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'locale')
    if os.path.isdir(local_locale):
        localedir = local_locale
    else:
        localedir = '/usr/share/locale'
    gettext.bindtextdomain('vajo_ui', localedir)
    gettext.textdomain('vajo_ui')
    _ = gettext.gettext
    ngettext = gettext.ngettext

    def get_language_code() -> str:
        """Return the current language identifier (e.g. 'zh_CN', 'en')."""
        try:
            import os
            # LANGUAGE can be a colon-separated list (e.g. 'zh_CN:zh:en')
            # It is the standard way to override language without changing the full locale.
            lang_env = os.environ.get("LANGUAGE", "").split(':')[0]
            if not lang_env:
                lang_env = os.environ.get("LANG", "")
            
            if lang_env:
                # Normalize: zh_CN.UTF-8 -> zh_CN, zh-CN -> zh_CN
                return lang_env.split('.')[0].replace('-', '_')
            
            # Fallback to locale module
            l, _ = locale.getlocale()
            if l:
                return l.replace('-', '_')
        except:
            pass
        return "en"

    LANGUAGE_CODE = get_language_code()

except Exception:
    print("Warning: Could not set up locale. Using fallback translations.")
    _ = lambda s: s
    ngettext = lambda s, p, n: s if n == 1 else p
    LANGUAGE_CODE = "en"
