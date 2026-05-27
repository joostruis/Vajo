#!/usr/bin/env python3
"""
modules/flatpak.py — Optional Flatpak backend for Vajo.

Activated only when vajo is launched with --flatpak.
Provides search results from Flathub by parsing the local appstream XML
cache that flatpak maintains on disk — no network call, no subprocess,
instant results with precise name/id matching.

Cache locations tried (in order):
  /var/lib/flatpak/appstream/<remote>/<arch>/active/appstream.xml(.gz)
  ~/.local/share/flatpak/appstream/<remote>/<arch>/active/appstream.xml(.gz)

Install / remove actions are intentionally disabled for Flatpak entries
(ACTION_FLATPAK_READONLY).

Public surface
--------------
FLATPAK_ENABLED : bool
    True when --flatpak was present on sys.argv.

ACTION_FLATPAK_READONLY : int
    Sentinel value (3) — GUI shows "Flathub" label, action button is a no-op.

AppstreamIndex
    Mirrors the DescriptionIndex pattern: build_async() populates an
    in-memory index from disk; search(query) returns matching package dicts.

FlatpakBackend.merge(luet_result, flatpak_result) -> {"packages": [...]}
    Merge two result dicts, deduplicating by app-id / name.
    Luet packages always win on conflict.
"""

import gzip
import glob
import os
import sys
import threading
import xml.etree.ElementTree as ET

from modules.i18n import _

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

FLATPAK_ENABLED: bool = "--flatpak" in sys.argv

# ---------------------------------------------------------------------------
# Action-ID sentinel
# 0 = ACTION_INSTALL, 1 = ACTION_REMOVE, 2 = ACTION_PROTECTED (luet)
# 3 = ACTION_FLATPAK_READONLY
# ---------------------------------------------------------------------------

ACTION_FLATPAK_READONLY: int = 3

# ---------------------------------------------------------------------------
# Appstream cache locations
# ---------------------------------------------------------------------------

_SYSTEM_APPSTREAM_GLOB = "/var/lib/flatpak/appstream/*/*/active/appstream.xml*"
_USER_APPSTREAM_GLOB   = os.path.expanduser(
    "~/.local/share/flatpak/appstream/*/*/active/appstream.xml*"
)

# XML namespaces used in appstream files (some distros include them)
_NS = {"": ""}   # ElementTree handles default ns via {ns}tag syntax


def _find_appstream_files() -> list:
    """
    Return a list of appstream XML / XML.gz paths found in the standard
    flatpak cache directories, system-wide first then user-local.
    """
    paths = []
    for pattern in (_SYSTEM_APPSTREAM_GLOB, _USER_APPSTREAM_GLOB):
        paths.extend(sorted(glob.glob(pattern)))
    return paths


def _open_appstream(path: str):
    """Open an appstream file for reading, handling .gz transparently."""
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def _text_default(element, tag: str) -> str:
    """
    Return the text of the child element matching `tag` that has no
    xml:lang attribute (the canonical default-language value).

    Appstream XML contains many localised siblings:
        <name>GIMP</name>
        <name xml:lang="ru">ГИМП</name>
        <name xml:lang="de">GIMP</name>
        ...
    element.find(tag) returns whichever comes first in document order,
    which on some systems is a non-English locale entry.

    Priority:
      1. Child with no xml:lang attribute  (unlocalized, always English on Flathub)
      2. Child with xml:lang="en"
      3. First child found (last resort)
    """
    XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
    # Normalise the target tag to its local name for comparison
    target = tag.split("}")[-1] if "}" in tag else tag

    fallback_en  = None
    fallback_any = None

    for child in element:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local != target:
            continue
        lang = child.get(XML_LANG)
        if lang is None:
            # No xml:lang — this is the canonical default
            if child.text:
                return child.text.strip()
        elif lang == "en" and fallback_en is None:
            fallback_en = child
        elif fallback_any is None:
            fallback_any = child

    for candidate in (fallback_en, fallback_any):
        if candidate is not None and candidate.text:
            return candidate.text.strip()
    return ""


def _parse_appstream_file(path: str) -> list:
    """
    Parse one appstream XML file and return a list of dicts with keys:
        app_id, name, summary, version
    Only components that have a flatpak bundle are included.
    """
    entries = []
    try:
        with _open_appstream(path) as fh:
            # iterparse lets us process large files without loading all into RAM
            context = ET.iterparse(fh, events=("end",))
            for event, elem in context:
                # Strip namespace prefix if present: {http://...}component -> component
                local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if local != "component":
                    continue

                # Must have a flatpak bundle to be installable via flatpak
                bundle = elem.find("bundle[@type='flatpak']")
                if bundle is None:
                    bundle = elem.find("{*}bundle[@type='flatpak']")
                if bundle is None:
                    elem.clear()
                    continue

                app_id  = _text_default(elem, "id")
                name    = _text_default(elem, "name")
                summary = _text_default(elem, "summary")
                project_license = _text_default(elem, "project_license")
                homepage = ""
                screenshots = []
                for child in elem:
                    local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if local == "url" and child.get("type") == "homepage" and child.text:
                        homepage = child.text.strip()
                    elif local == "screenshots":
                        for ss in child:
                            ss_local = ss.tag.split("}")[-1] if "}" in ss.tag else ss.tag
                            if ss_local == "screenshot":
                                best_img = None
                                for img in ss:
                                    img_local = img.tag.split("}")[-1] if "}" in img.tag else img.tag
                                    if img_local == "image" and img.text:
                                        img_url = img.text.strip()
                                        if img.get("type") == "source":
                                            best_img = img_url
                                            break # Take source and stop
                                        if best_img is None:
                                            best_img = img_url
                                if best_img:
                                    screenshots.append(best_img)

                # Version: try releases/release/@version first
                version = ""
                releases = elem.find("releases")
                if releases is None:
                    releases = elem.find("{*}releases")
                if releases is not None:
                    rel = releases.find("release")
                    if rel is None:
                        rel = releases.find("{*}release")
                    if rel is not None:
                        version = rel.get("version", "")
                # Category: try categories/category
                category_name = "Flatpak" # Default fallback if no category is listed
                categories_node = elem.find("categories")
                if categories_node is None:
                    categories_node = elem.find("{*}categories")
                
                if categories_node is not None:
                    cat = categories_node.find("category")
                    if cat is None:
                        cat = categories_node.find("{*}category")
                    if cat is not None and cat.text:
                        category_name = cat.text.strip()

                if app_id and name:
                    entries.append({
                        "app_id":  app_id,
                        "name":    name,
                        "summary": summary,
                        "version": version,
                        "license": project_license,
                        "homepage": homepage,
                        "category": category_name,
                        "screenshots": screenshots,
                    })

                elem.clear()   # free memory as we go

    except Exception as exc:
        print("flatpak appstream parse error ({}): {}".format(path, exc))

    return entries


# ---------------------------------------------------------------------------
# Installed-package detection
# ---------------------------------------------------------------------------

def _get_installed_ids() -> set:
    """
    Return a set of installed Flatpak app-ids
    (e.g. {'org.gimp.GIMP', 'com.spotify.Client'}).
    """
    import subprocess

    def _run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return r.returncode, (r.stdout or ""), (r.stderr or "")
        except Exception as e:
            return -1, "", str(e)

    # Strategy 1: --columns=application (one app-id per line)
    rc, out, err = _run(["flatpak", "list", "--system", "--app", "--columns=application"])
    if rc == 0 and out.strip():
        ids = set()
        for line in out.splitlines():
            app_id = line.strip()
            if app_id and app_id.count(".") >= 2:
                ids.add(app_id)
        if ids:
            return ids

    # Strategy 2: plain flatpak list --app (tab-separated table, app-id in col 1)
    rc, out, err = _run(["flatpak", "list", "--system", "--app"])
    if rc == 0 and out.strip():
        ids = set()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                app_id = parts[1].strip()
                if app_id and app_id.count(".") >= 2:
                    ids.add(app_id)
        return ids

    return set()


def _get_updateable_ids() -> set:
    """
    Return a set of Flatpak app-ids that have updates available.
    Uses `flatpak remote-ls --updates`.
    """
    import subprocess
    try:
        # --columns=application gives just the IDs of updateable packages
        r = subprocess.run(
            ["flatpak", "remote-ls", "--system", "--updates", "--columns=application"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0 and r.stdout.strip():
            ids = set()
            for line in r.stdout.splitlines():
                app_id = line.strip()
                if app_id and app_id.count(".") >= 2:
                    ids.add(app_id)
            return ids
    except Exception as e:
        if "--debug" in sys.argv:
            print(f"[DEBUG] flatpak: Failed to get updateable IDs: {e}")
    return set()


# ---------------------------------------------------------------------------
# AppstreamIndex — mirrors the DescriptionIndex pattern from vajo_core.py
# ---------------------------------------------------------------------------

class AppstreamIndex:
    """
    Builds an in-memory index of Flatpak apps from the local appstream cache.

    Usage mirrors DescriptionIndex:
        idx = AppstreamIndex()
        idx.build_async(on_ready_callback=lambda: ...)
        results = idx.search("gimp")
    """

    def __init__(self):
        self._index          = {}   # app_id -> dict
        self._installed_ids  = set() # app_ids currently installed
        self._updateable_ids = set() # app_ids with updates available
        self._ready          = False
        self._lock           = threading.Lock()
        self._ready_event    = threading.Event()

    def build_async(self, on_ready_callback=None):
        def worker():
            try:
                import subprocess
                index = {}
                
                # --- 1. SILENT FLATHUB INITIALIZATION ---
                try:
                    # Check if flathub exists
                    res = subprocess.run(["flatpak", "remotes", "--columns=name"], capture_output=True, text=True)
                    if "flathub" not in res.stdout:
                        if "--debug" in sys.argv:
                            print("[DEBUG] flatpak: Flathub remote not found, adding for current user", flush=True)
                        # Using --user prevents background threads from hanging on sudo/polkit prompts
                        subprocess.run(
                            ["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", "https://dl.flathub.org/repo/flathub.flatpakrepo"],
                            capture_output=True
                        )
                except Exception as e:
                    print(f"flatpak: Failed to add Flathub remote: {e}", file=sys.stderr)

                # --- 2. SILENT APPSTREAM REFRESH ---
                paths = _find_appstream_files()
                if not paths:
                    print("flatpak: AppStream cache missing. Fetching silently...")
                    try:
                        subprocess.run(["flatpak", "update", "--appstream"], capture_output=True)
                        paths = _find_appstream_files()
                    except Exception as e:
                        print(f"flatpak: Failed to fetch AppStream cache: {e}")

                # --- 3. STANDARD PARSING ---
                if paths:
                    for path in paths:
                        for entry in _parse_appstream_file(path):
                            app_id = entry["app_id"]
                            if app_id not in index:
                                index[app_id] = entry

                installed_ids  = _get_installed_ids()
                updateable_ids = _get_updateable_ids()

                with self._lock:
                    self._index          = index
                    self._installed_ids  = installed_ids
                    self._updateable_ids = updateable_ids
                    self._ready          = True
                self._ready_event.set()
            except Exception as e:
                print(f"flatpak: unexpected error in build_async worker: {e}")
                with self._lock:
                    self._ready = True
                self._ready_event.set()
            finally:
                if on_ready_callback:
                    on_ready_callback()

        threading.Thread(target=worker, daemon=True).start()

    def refresh_installed(self, on_done=None):
        """
        Re-run `flatpak list` and update the installed set in the background.
        Calls on_done() (no args) on the same background thread when complete.
        Call this after an install or remove operation completes.
        """
        def worker():
            installed_ids  = _get_installed_ids()
            updateable_ids = _get_updateable_ids()
            with self._lock:
                self._installed_ids  = installed_ids
                self._updateable_ids = updateable_ids
            if on_done:
                on_done()
        threading.Thread(target=worker, daemon=True).start()

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def get_installed_packages(self) -> list:
        """Return all installed Flatpak packages in standard GUI shape."""
        with self._lock:
            if not self._ready:
                return []
            installed_ids  = self._installed_ids
            updateable_ids = self._updateable_ids
            # We want to show everything that is installed, even if it's a child/plugin
            results = []
            for app_id in installed_ids:
                entry = self._index.get(app_id)
                if entry:
                    results.append(self._to_pkg(entry, installed_ids, updateable_ids))
                else:
                    # Not in appstream index? Still show it as a basic entry
                    results.append({
                        "category":              _("Flatpak"),
                        "name":                  app_id,
                        "upgrade_symbol":        "↑" if app_id in updateable_ids else "",
                        "version":               "",
                        "repository":            "Flathub",
                        "is_actually_installed": True,
                        "installed_version":     "",
                        "available_version":     "",
                        "protected":             False,
                        "description":           "",
                        "_flatpak_label":        app_id,
                        "_flatpak":              True,
                    })
            return results

    def search(self, query: str) -> list:
        """
        Case-insensitive search over app-id and display name only.
        Every word in the query must match somewhere in app_id or name.

        Intentionally does NOT search the summary/description — that is
        what caused flatpak search to return many results for "gimp".

        Plugin / extension suppression
        --------------------------------
        If the result set contains a top-level app (e.g. org.gimp.GIMP) AND
        sub-entries whose app-id is a strict extension of it
        (e.g. org.gimp.GIMP.Plugin.Resynthesizer, org.gimp.GIMP.Manual),
        the sub-entries are dropped.  They only appear when the user
        searches specifically for them (e.g. "gimp plugin", "resynthesizer").

        Returns a list of package dicts shaped for the GUI liststore.
        """
        words = query.lower().split()
        if not words:
            return []

        # Pass 1: collect all name/id matches, snapshot installed set
        matches = []
        with self._lock:
            if not self._ready:
                return []
            installed_ids  = self._installed_ids
            updateable_ids = self._updateable_ids
            for entry in self._index.values():
                haystack = (entry["app_id"] + " " + entry["name"]).lower()
                if all(w in haystack for w in words):
                    matches.append(entry)

        if not matches:
            return []

        # Pass 2: build a set of top-level app-ids that are present,
        # then drop any entry whose app-id starts with "<parent>."
        top_level_ids = {e["app_id"] for e in matches}
        results = []
        for entry in matches:
            app_id = entry["app_id"]
            is_child = any(
                app_id.startswith(parent + ".")
                for parent in top_level_ids
                if parent != app_id
            )
            if not is_child:
                results.append(self._to_pkg(entry, installed_ids, updateable_ids))

        return results

    def _to_pkg(self, entry: dict, installed_ids: set, updateable_ids: set) -> dict:
        """Convert an index entry to the standard package dict shape."""
        app_id    = entry["app_id"]
        installed = app_id in installed_ids

        # Pull the raw string, defaulting to "Flatpak"
        raw_category = entry.get("category", "Flatpak")

        return {
            # ---- fields the GUI reads from the liststore ----
            "category":              _(raw_category),
            "name":                  app_id,
            "upgrade_symbol":        "↑" if app_id in updateable_ids else "",
            "version":               entry.get("version", ""),
            "repository":            "Flathub",
            # ---- enrichment fields (mirrors SearchProcessor output) ----
            "is_actually_installed": installed,
            "installed_version":     entry.get("version", "") if installed else "",
            "available_version":     entry.get("version", ""),
            "protected":             False,
            # ---- extra fields ----
            "description":           entry.get("summary", ""),
            "license":               entry.get("license", ""),
            "homepage":              entry.get("homepage", ""),
            "screenshots":           entry.get("screenshots", []),
            "_flatpak_label":        entry["name"],
            "_flatpak":              True,
        }



# ---------------------------------------------------------------------------
# FlatpakBackend — merge helper (search is now done via AppstreamIndex)
# ---------------------------------------------------------------------------

class FlatpakBackend:
    """
    Stateless helpers used by the GUI / TUI search flow.
    Actual searching is delegated to AppstreamIndex.
    """

    @staticmethod
    def is_available() -> bool:
        """Return True if the `flatpak` binary can be found on PATH."""
        import shutil
        return shutil.which("flatpak") is not None

    @staticmethod
    def merge(luet_result: dict, flatpak_result: dict) -> dict:
        """
        Merge flatpak_result into luet_result, returning a combined dict.

        Rules
        -----
        - luet errors propagate; flatpak errors become a non-fatal warning.
        - Luet packages always win: any Flatpak entry whose app-id or bare
          name collides with a luet entry is dropped.
        - Deduplication within the Flatpak list itself is by app-id.
        """
        if "error" in luet_result:
            merged_packages = []
        else:
            merged_packages = list(luet_result.get("packages", []))

        existing_keys = set()
        for pkg in merged_packages:
            cat  = pkg.get("category", "")
            name = pkg.get("name", "")
            existing_keys.add("{}/{}".format(cat, name))
            existing_keys.add(name.lower())

        flatpak_error = flatpak_result.get("error")

        for fpkg in flatpak_result.get("packages", []):
            app_id = fpkg.get("name", "")
            if "flatpak/{}".format(app_id) in existing_keys:
                continue
            if app_id.lower() in existing_keys:
                continue
            existing_keys.add("flatpak/{}".format(app_id))
            existing_keys.add(app_id.lower())
            merged_packages.append(fpkg)

        result = {"packages": merged_packages}
        if flatpak_error:
            result["flatpak_warning"] = flatpak_error
        return result


# ---------------------------------------------------------------------------
# FlatpakOperations — install / remove via flatpak CLI
# ---------------------------------------------------------------------------

class FlatpakOperations:
    """
    Install and remove Flatpak apps.

    Unlike luet, flatpak install/remove runs as the current user
    (require_root=False).  The command_runner_realtime injected here is
    the same CommandRunner.run_realtime used for luet operations.
    """

    @staticmethod
    def build_install_command(app_id: str) -> list:
        return ["flatpak", "install", "--system", "-y", "--noninteractive", "flathub", app_id]

    @staticmethod
    def build_remove_command(app_id: str) -> list:
        return ["flatpak", "remove", "--system", "-y", "--noninteractive", app_id]

    @staticmethod
    def run_installation(command_runner_realtime, log_callback, on_finish_callback, app_id: str):
        command_runner_realtime(
            FlatpakOperations.build_install_command(app_id),
            require_root=False,
            on_line_received=log_callback,
            on_finished=on_finish_callback,
        )

    @staticmethod
    def run_removal(command_runner_realtime, log_callback, on_finish_callback, app_id: str):
        command_runner_realtime(
            FlatpakOperations.build_remove_command(app_id),
            require_root=False,
            on_line_received=log_callback,
            on_finished=on_finish_callback,
        )
