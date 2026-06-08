#!/usr/bin/env python3
"""
modules/config.py — Persistent user preferences for Vajo.

Stores settings as JSON at ~/.config/vajo/vajo.conf.
Used by the Preferences dialog to enable/disable optional modules.

Public surface
--------------
VajoConfig()
    Load (or create) the config file on instantiation.

    .get(key, default)  — read a value
    .set(key, value)    — write a value and persist to disk
    .save()             — explicit flush to disk

Default config keys
-------------------
  "enable_flatpak"    : bool  — show Flathub results (equiv. to --flatpak flag)
  "enable_rollback"   : bool  — show the Roll back item in File menu
  "prefer_dark_theme" : bool  — force GTK dark theme regardless of system setting
"""

import json
import os

_CONFIG_DIR  = os.path.expanduser("~/.config/vajo")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "vajo.conf")

_DEFAULTS = {
    "enable_flatpak":     False,
    "enable_rollback":    False,
    "prefer_dark_theme":  False,
}


class VajoConfig:
    def __init__(self):
        self._data = dict(_DEFAULTS)
        self._load()

    def _load(self):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            # Only accept known keys; ignore unknown to stay forward-compatible
            for key in _DEFAULTS:
                if key in stored:
                    self._data[key] = stored[key]
        except FileNotFoundError:
            pass  # First run — defaults will be used and written on first save
        except Exception as exc:
            print("vajo config: could not read {}: {}".format(_CONFIG_PATH, exc))

    def save(self):
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
        except Exception as exc:
            print("vajo config: could not write {}: {}".format(_CONFIG_PATH, exc))

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()
