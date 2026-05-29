#!/usr/bin/env python3
"""
modules/rollback.py — RollbackManager extracted from vajo_core.py.

Manages stable repository rollback by reading/writing reference tags
in /etc/luet/repos.conf.d/ and resolving the previous snapshot from
the repository-index git history.
"""

import os
import re
import subprocess
import datetime
import yaml

from modules.i18n import _, ngettext


class RollbackManager:
    """
    Manages stable repository rollback by reading/writing reference tags
    in /etc/luet/repos.conf.d/ and resolving the previous snapshot from
    the repository-index git history.
    """

    REPOS_CONF_DIR = "/etc/luet/repos.conf.d"
    DESKTOP_STABLE_FILE = "mocaccino-desktop-stable.yml"
    COMMUNITY_STABLE_FILE = "mocaccino-community-stable.yml"
    REPO_INDEX_URL = "https://github.com/mocaccinoOS/repository-index"
    CLONE_DEPTH = 50
    PIN_FILE = "/var/luet/mocaccino-rollback-pin"
    ANCHOR_FILE = "/var/luet/mocaccino-rollback-anchor"

    @staticmethod
    def _restore_pin(desktop_version, run_sync_func):
        """
        Re-writes the reference: fields to the stable conf files after a
        package operation may have overwritten them.
        """
        def _make_content(name, desc, priority, url, ref):
            return (
                f'name: "{name}"\n'
                f'description: "{desc}"\n'
                f'type: "docker"\n'
                f'enable: true\n'
                f'cached: true\n'
                f'priority: {priority}\n'
                f'urls:\n'
                f'  - "{url}"\n'
                f'reference: "{ref}-repository.yaml"\n'
            )

        def _escape(s):
            return s.replace("'", "'\\''")

        # Read community version from pin file if present
        community_version = None
        try:
            with open(RollbackManager.PIN_FILE, "r") as f:
                lines = f.read().strip().splitlines()
                if len(lines) >= 2:
                    community_version = lines[1].strip()
        except Exception:
            pass

        desktop_path = os.path.join(
            RollbackManager.REPOS_CONF_DIR, RollbackManager.DESKTOP_STABLE_FILE)
        desktop_content = _make_content(
            "mocaccino-desktop-stable",
            "MocaccinoOS desktop Repository (stable)",
            3, "quay.io/mocaccino/desktop", desktop_version
        )
        cmd = f"printf '%s' '{_escape(desktop_content)}' > {desktop_path}"

        # Restore community if we have a pinned version
        if community_version:
            community_path = os.path.join(
                RollbackManager.REPOS_CONF_DIR, RollbackManager.COMMUNITY_STABLE_FILE)
            # Check luet database to see if community repository package is installed.
            # We cannot rely on the conf file's enable state since our rollback wrote
            # enable: false there, and luet's config-protect pending file gets cleaned
            # up by _restore_pin before mos config-update can merge it.
            community_enabled = False
            try:
                result = run_sync_func(
                    ["luet", "search", "--installed",
                     "repository/mocaccino-community-stable", "-q"],
                    require_root=True
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if "mocaccino-community-stable" in line:
                            community_enabled = True
                            break
            except Exception:
                pass
            community_content = (
                f'name: "mocaccino-community-stable"\n'
                f'description: "MocaccinoOS Community Repository (Stable)"\n'
                f'type: "docker"\n'
                f'enable: {"true" if community_enabled else "false"}\n'
                f'cached: true\n'
                f'priority: 50\n'
                f'urls:\n'
                f'  - "quay.io/mocaccino/mocaccino-community"\n'
                f'reference: "{community_version}-repository.yaml"\n'
            )
            cmd += f" && printf '%s' '{_escape(community_content)}' > {community_path}"

        try:
            run_sync_func(["sh", "-c", cmd], require_root=True)
            # Also remove any pending config-protect files luet may have created
            # that would overwrite our restored references on next mos config-update
            cleanup = (
                f"rm -f {RollbackManager.REPOS_CONF_DIR}/._cfg*_mocaccino-desktop-stable.yml "
                f"{RollbackManager.REPOS_CONF_DIR}/._cfg*_mocaccino-community-stable.yml "
                f"2>/dev/null || true"
            )
            run_sync_func(["sh", "-c", cleanup], require_root=True)
        except Exception as e:
            print("RollbackManager._restore_pin error:", e)

    VAJO_BACKUP_DIR = "/var/luet/vajo-backup"
    VAJO_FILES = [
        ("/usr/share/vajo/vajo_core.py",           "vajo_core.py"),
        ("/usr/bin/vajo_gui.py",                    "vajo_gui.py"),
        ("/usr/bin/vajo_tui.py",                    "vajo_tui.py"),
        ("/usr/share/vajo/modules/__init__.py",     "modules/__init__.py"),
        ("/usr/share/vajo/modules/config.py",       "modules/config.py"),
        ("/usr/share/vajo/modules/flatpak.py",      "modules/flatpak.py"),
        ("/usr/share/vajo/modules/i18n.py",         "modules/i18n.py"),
        ("/usr/share/vajo/modules/rollback.py",     "modules/rollback.py"),
        ("/usr/share/vajo/modules/system.py",       "modules/system.py"),
    ]

    @staticmethod
    def backup_vajo_files(run_sync_func):
        """
        Backs up current vajo files to /var/luet/vajo-backup/ before rollback
        so they can be restored after luet upgrade potentially downgrades them.
        Subdirectories (e.g. modules/) are created automatically.
        """
        cmds = [f"mkdir -p {RollbackManager.VAJO_BACKUP_DIR}"]
        for src, name in RollbackManager.VAJO_FILES:
            dst = f"{RollbackManager.VAJO_BACKUP_DIR}/{name}"
            # Ensure parent directory exists in the backup tree
            dst_dir = os.path.dirname(dst)
            if dst_dir != RollbackManager.VAJO_BACKUP_DIR:
                cmds.append(f"mkdir -p {dst_dir}")
            cmds.append(f"cp -f {src} {dst} 2>/dev/null || true")
        run_sync_func(["sh", "-c", " && ".join(cmds)], require_root=True)

    @staticmethod
    def restore_vajo_files(run_sync_func):
        """
        Restores backed-up vajo files after rollback to ensure the user
        always has the current version with rollback functionality intact.
        Subdirectories at the destination (e.g. modules/) are created if missing.
        """
        cmds = []
        for src, name in RollbackManager.VAJO_FILES:
            backup = f"{RollbackManager.VAJO_BACKUP_DIR}/{name}"
            # Ensure parent directory exists at destination
            src_dir = os.path.dirname(src)
            cmds.append(f"mkdir -p {src_dir}")
            cmds.append(f"cp -f {backup} {src} 2>/dev/null || true")
        if cmds:
            run_sync_func(["sh", "-c", " && ".join(cmds)], require_root=True)

    @staticmethod
    def is_pinned():
        """
        Returns True if the system is currently pinned to a rolled-back snapshot.
        Uses the pin file as the authoritative signal — it is exclusively written
        by the rollback tool and is not affected by luet's autobump mechanism.
        """
        return os.path.exists(RollbackManager.PIN_FILE)

    @staticmethod
    def is_stable_system():
        """
        Returns True if the system is running stable repositories,
        detected by checking whether mocaccino-desktop-stable.yml
        exists in /etc/luet/repos.conf.d/ and has enable: true.
        This avoids needing root for the check.
        """
        path = os.path.join(
            RollbackManager.REPOS_CONF_DIR,
            RollbackManager.DESKTOP_STABLE_FILE
        )
        try:
            if not os.path.exists(path):
                return False
            with open(path, "r") as f:
                data = yaml.safe_load(f)
            return data.get("enable", False) is True
        except Exception:
            return False

    @staticmethod
    def is_community_enabled():
        """
        Returns True if mocaccino-community-stable.yml exists and has enable: true.
        """
        path = os.path.join(
            RollbackManager.REPOS_CONF_DIR,
            RollbackManager.COMMUNITY_STABLE_FILE
        )
        try:
            if not os.path.exists(path):
                return False
            with open(path, "r") as f:
                data = yaml.safe_load(f)
            return data.get("enable", False) is True
        except Exception:
            return False

    @staticmethod
    def get_current_desktop_version():
        """
        Returns the currently installed desktop-stable version.
        First checks the pin file (set by rollback, survives package ops),
        then falls back to the conf file reference:, then to luet search.
        Returns e.g. '20260314010809', or None if not found.
        """
        # First try the pin file (system is actively pinned)
        try:
            if os.path.exists(RollbackManager.PIN_FILE):
                with open(RollbackManager.PIN_FILE, "r") as f:
                    version = f.readline().strip()
                if version:
                    return version
        except Exception:
            pass

        # Try the anchor file (unpinned but rolled back, survives unpin)
        try:
            if os.path.exists(RollbackManager.ANCHOR_FILE):
                with open(RollbackManager.ANCHOR_FILE, "r") as f:
                    version = f.read().strip()
                if version:
                    return version
        except Exception:
            pass

        # Fall back to conf file reference:
        path = os.path.join(
            RollbackManager.REPOS_CONF_DIR,
            RollbackManager.DESKTOP_STABLE_FILE
        )
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = yaml.safe_load(f)
                ref = data.get("reference", "").replace("-repository.yaml", "").strip()
                if ref:
                    return ref
        except Exception:
            pass

        # Fall back to luet search --installed
        prefix = "repository/mocaccino-desktop-stable-"
        cmd = ["luet", "search", "--installed",
               "repository/mocaccino-desktop-stable", "-q"]
        for elevation in [[], ["pkexec"]]:
            try:
                result = subprocess.run(
                    elevation + cmd,
                    capture_output=True, text=True, timeout=15
                )
                line = result.stdout.strip()
                if result.returncode == 0 and line.startswith(prefix):
                    return line[len(prefix):]
            except Exception:
                continue
        return None

    REPO_INDEX_URL = "https://github.com/mocaccinoOS/repository-index"
    CLONE_DEPTHS = [50, 150, 400]

    @staticmethod
    def _parse_collection_yaml(content):
        """Extract stable repo versions from collection.yaml content."""
        versions = {}
        current_name = None
        for line in content.split('\n'):
            # Only match top-level package fields (4 spaces indent)
            # to avoid matching nested label fields like package.version
            nm = re.search(r'^    name:\s*"([^"]+)"', line)
            vm = re.search(r'^    version:\s*"([^"]+)"', line)
            if nm:
                current_name = nm.group(1)
            if vm and current_name:
                versions[current_name] = vm.group(1)
                current_name = None
        return versions

    @staticmethod
    def _build_snapshots_from_clone(clone_dir):
        """
        Walk git log and return list of snapshots where BOTH desktop-stable
        and community-stable changed together — newest first.
        These are the meaningful rollback candidates.
        """
        log = subprocess.run(
            ["git", "--git-dir", clone_dir, "log", "HEAD",
             "--pretty=format:%H|%ad|%s", "--date=short"],
            capture_output=True, text=True
        )
        if log.returncode != 0:
            return []

        snapshots = []
        prev_desktop = None
        prev_community = None

        for line in log.stdout.strip().split('\n'):
            parts = line.split('|', 2)
            if len(parts) != 3:
                continue
            sha, date, _ = parts

            cat = subprocess.run(
                ["git", "--git-dir", clone_dir, "show",
                 f"{sha}:packages/collection.yaml"],
                capture_output=True, text=True
            )
            if cat.returncode != 0:
                continue

            v = RollbackManager._parse_collection_yaml(cat.stdout)
            desktop = v.get("mocaccino-desktop-stable", "").replace(
                "-repository.yaml", "")
            community = v.get("mocaccino-community-stable", "").replace(
                "-repository.yaml", "")

            desktop_changed = desktop and desktop != prev_desktop
            community_changed = community and community != prev_community

            # Only include snapshots where both repos changed together
            if desktop_changed and community_changed:
                snapshots.append({
                    "sha": sha[:10],
                    "date": date,
                    "desktop": desktop,
                    "community": community,
                })

            if desktop_changed:
                prev_desktop = desktop
            if community_changed:
                prev_community = community

        return snapshots

    @staticmethod
    def get_rollback_candidates(current_desktop_version):
        """
        Clones the repository-index and returns rollback candidates:
        the last 10 dual-bump snapshots from the current version, plus
        the closest snapshot to ~1 year ago and ~2 years ago (deduplicated).
        Each entry: {label, desktop, community, date, sha}
        Returns empty list if none found.
        """
        clone_dir = "/tmp/mocaccino-repo-index-cache"
        try:
            if os.path.exists(clone_dir):
                subprocess.run(["rm", "-rf", clone_dir], check=True)

            result = subprocess.run(
                ["git", "clone", "--bare", "--depth", "400",
                 RollbackManager.REPO_INDEX_URL, clone_dir],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                print("RollbackManager clone error:", result.stderr)
                return []

            snapshots = RollbackManager._build_snapshots_from_clone(clone_dir)
            if not snapshots:
                return []

            # Find index of current version (or closest before it)
            current_idx = next(
                (i for i, s in enumerate(snapshots)
                 if s["desktop"] <= current_desktop_version),
                None
            )
            if current_idx is None:
                return []

            older = snapshots[current_idx + 1:]
            if not older:
                return []

            seen = set()
            candidates = []

            def _add(label, snapshot):
                if snapshot["desktop"] not in seen:
                    seen.add(snapshot["desktop"])
                    candidates.append(dict(snapshot, label=label))

            # Last 10 snapshots
            for i, snapshot in enumerate(older[:10]):
                label = _("Previous") if i == 0 else _("{} versions back").format(i + 1)
                _add(label, snapshot)

            # Time-based: ~1 year ago and ~2 years ago
            today = datetime.date.today()
            one_year_ago = (today - datetime.timedelta(days=365)).isoformat()
            two_years_ago = (today - datetime.timedelta(days=730)).isoformat()

            def closest_to(target_date):
                return min(
                    older,
                    key=lambda s: abs(
                        (datetime.date.fromisoformat(s["date"]) -
                         datetime.date.fromisoformat(target_date)).days
                    )
                )

            if older:
                _add(_("~1 year ago"), closest_to(one_year_ago))
            if older:
                _add(_("~2 years ago"), closest_to(two_years_ago))

            return candidates

        except Exception as e:
            print("RollbackManager.get_rollback_candidates error:", e)
            return []
        finally:
            try:
                subprocess.run(["rm", "-rf", clone_dir], check=False)
            except Exception:
                pass

    @staticmethod
    def _write_latest_stable_refs(latest_desktop, latest_community):
        """
        Returns a shell command string that writes the latest stable reference:
        fields to the conf files. Used before a full upgrade to ensure luet
        resolves against the correct stable tag.
        """
        def _escape(s):
            return s.replace("'", "'\\''")

        cmds = []

        if latest_desktop:
            path = os.path.join(
                RollbackManager.REPOS_CONF_DIR, RollbackManager.DESKTOP_STABLE_FILE)
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        data = yaml.safe_load(f)
                    enabled = data.get("enable", True)
                except Exception:
                    enabled = True
                content = (
                    f'name: "mocaccino-desktop-stable"\n'
                    f'description: "MocaccinoOS desktop Repository (stable)"\n'
                    f'type: "docker"\n'
                    f'enable: {"true" if enabled else "false"}\n'
                    f'cached: true\n'
                    f'priority: 3\n'
                    f'urls:\n'
                    f'  - "quay.io/mocaccino/desktop"\n'
                    f'reference: "{latest_desktop}-repository.yaml"\n'
                )
                cmds.append(f"printf '%s' '{_escape(content)}' > {path}")

        if latest_community:
            path = os.path.join(
                RollbackManager.REPOS_CONF_DIR, RollbackManager.COMMUNITY_STABLE_FILE)
            if os.path.exists(path):
                try:
                    with open(path, "r") as f:
                        data = yaml.safe_load(f)
                    enabled = data.get("enable", True)
                except Exception:
                    enabled = True
                content = (
                    f'name: "mocaccino-community-stable"\n'
                    f'description: "MocaccinoOS Community Repository (Stable)"\n'
                    f'type: "docker"\n'
                    f'enable: {"true" if enabled else "false"}\n'
                    f'cached: true\n'
                    f'priority: 50\n'
                    f'urls:\n'
                    f'  - "quay.io/mocaccino/mocaccino-community"\n'
                    f'reference: "{latest_community}-repository.yaml"\n'
                )
                cmds.append(f"printf '%s' '{_escape(content)}' > {path}")

        return " && ".join(cmds)

    @staticmethod
    def _get_latest_stable_versions():
        """
        Fetches the latest stable desktop and community versions from
        the repository-index collection.yaml on GitHub.
        Returns (desktop_version, community_version) or (None, None) on failure.
        """
        url = "https://raw.githubusercontent.com/mocaccinoOS/repository-index/master/packages/collection.yaml"
        try:
            result = subprocess.run(
                ["curl", "-sf", "--max-time", "15", url],
                capture_output=True, text=True
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None, None
            versions = RollbackManager._parse_collection_yaml(result.stdout)
            desktop = versions.get("mocaccino-desktop-stable", "").replace("-repository.yaml", "").strip()
            community = versions.get("mocaccino-community-stable", "").replace("-repository.yaml", "").strip()
            return desktop or None, community or None
        except Exception as e:
            print("RollbackManager._get_latest_stable_versions error:", e)
            return None, None

    @staticmethod
    def unpin_references():
        """
        Returns a shell command string that writes the latest stable reference:
        fields to the conf files and removes the pin file.
        Fetches latest versions from repository-index collection.yaml.
        """
        latest_desktop, latest_community = RollbackManager._get_latest_stable_versions()
        cmds = []

        write_cmds = RollbackManager._write_latest_stable_refs(
            latest_desktop, latest_community)
        if write_cmds:
            cmds.append(write_cmds)

        # Remove the pin file so is_pinned() returns False
        cmds.append(f"rm -f {RollbackManager.PIN_FILE}")

        return " && ".join(cmds)

    @staticmethod
    def run_rollback(
        previous_snapshot,
        command_runner_realtime,
        command_runner_sync,
        log_callback,
        on_finish_callback,
        schedule_callback
    ):
        """
        Rolls back to the previous snapshot by:
        1. Writing pinned reference: tags to the stable repo conf files
        2. Writing a pin file to /var/luet/ that survives package operations
        3. Cleaning up config files that may conflict with older package versions
        4. Running luet upgrade -y which resolves against the pinned snapshot

        All root operations go through command_runner_realtime/sync so pkexec
        handles elevation. Community file is only written if already enabled.
        on_finish_callback(returncode, message) is called when done.
        """
        desktop_ref = previous_snapshot.get("desktop", "")
        community_ref = previous_snapshot.get("community", "")
        community_enabled = RollbackManager.is_community_enabled()

        def _make_content(name, desc, priority, url, ref, enable=True):
            return (
                f'name: "{name}"\n'
                f'description: "{desc}"\n'
                f'type: "docker"\n'
                f'enable: {"true" if enable else "false"}\n'
                f'cached: true\n'
                f'priority: {priority}\n'
                f'urls:\n'
                f'  - "{url}"\n'
                f'reference: "{ref}-repository.yaml"\n'
            )

        def _escape(s):
            return s.replace("'", "'\\''")

        desktop_content = _make_content(
            "mocaccino-desktop-stable",
            "MocaccinoOS desktop Repository (stable)",
            3, "quay.io/mocaccino/desktop", desktop_ref
        )
        desktop_path = os.path.join(
            RollbackManager.REPOS_CONF_DIR, RollbackManager.DESKTOP_STABLE_FILE)
        write_desktop = f"printf '%s' '{_escape(desktop_content)}' > {desktop_path}"

        # Always write community conf with the paired reference, but only
        # enable it if the user currently has community enabled.
        # This ensures if the user enables community later it aligns with desktop.
        community_content = _make_content(
            "mocaccino-community-stable",
            "MocaccinoOS Community Repository (Stable)",
            50, "quay.io/mocaccino/mocaccino-community", community_ref,
            enable=community_enabled
        )
        community_path = os.path.join(
            RollbackManager.REPOS_CONF_DIR, RollbackManager.COMMUNITY_STABLE_FILE)
        write_community = f" && printf '%s' '{_escape(community_content)}' > {community_path}"

        # Step 1: Write the reference files, pin file and anchor file
        # Pin file = system is actively pinned (deleted on unpin)
        # Anchor file = last rolled-back version (deleted only on full upgrade)
        pin_content = f"{desktop_ref}\n{community_ref}"
        write_pin = f" && printf '%s' '{pin_content}' > {RollbackManager.PIN_FILE}"
        write_anchor = f" && printf '%s' '{desktop_ref}' > {RollbackManager.ANCHOR_FILE}"
        write_cmd = ["sh", "-c", f"{write_desktop}{write_community}{write_pin}{write_anchor}"]
        res = command_runner_sync(write_cmd, require_root=True)
        if res.returncode != 0:
            schedule_callback(
                on_finish_callback, -1,
                _("Failed to write repository configuration")
            )
            return

        def _do_rollback():
            RollbackManager.backup_vajo_files(command_runner_sync)

            # Clean up config files that may be incompatible with older package
            # versions (e.g. sshd config format changes, python/bash version changes).
            # These will be restored by luet upgrade from the pinned snapshot.
            # NOTE: /etc/shells and /etc/pam.d/* are intentionally NOT removed —
            # their absence breaks pkexec and PAM authentication immediately,
            # making recovery impossible if the upgrade fails or is interrupted.
            cleanup_cmd = (
                "rm -rf /etc/ssh/sshd_config.d && "
                "rm -f /etc/ssh/sshd_config && "
                "rm -rf /etc/bash_completion* && "
                "rm -rf /usr/share/bash-completion && "
                "rm -f /etc/nsswitch.conf && "
                "rm -f /etc/profile && "
                "rm -rf /etc/profile.d/*"
            )
            command_runner_sync(["sh", "-c", cleanup_cmd], require_root=True)

            def _on_upgrade_done(rc):
                if rc == 0:
                    RollbackManager.restore_vajo_files(command_runner_sync)
                schedule_callback(
                    on_finish_callback,
                    rc,
                    _("Rollback completed successfully") if rc == 0
                    else _("Rollback failed during upgrade")
                )
                return False

            command_runner_realtime(
                ["luet", "upgrade", "-y"],
                require_root=True,
                on_line_received=log_callback,
                on_finished=_on_upgrade_done
            )
            return False

        # _do_rollback is called directly because run_rollback always executes
        # in a worker thread (both GUI and TUI). schedule_callback is still used
        # for the finish callback to marshal results back to the main thread.
        _do_rollback()
