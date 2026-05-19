#!/usr/bin/env python3

import os
import sys
import json
import re
import threading
import time
import subprocess
import shutil
import yaml
import datetime

# -------------------------
# Debug logging
# -------------------------
class Debug:
    """Simple debug logger. Activated by passing --debug on the command line."""
    enabled = "--debug" in sys.argv

    @staticmethod
    def log(msg):
        if Debug.enabled:
            print(f"[DEBUG] {time.time():.3f} {msg}", flush=True)

    @staticmethod
    def timer(label):
        """Returns a context manager that logs elapsed time for a block."""
        import contextlib
        @contextlib.contextmanager
        def _timer():
            t0 = time.time()
            Debug.log(f"{label} started")
            yield
            Debug.log(f"{label} done in {time.time()-t0:.3f}s")
        return _timer()

try:
    from packaging import version as _pkg_version
except ImportError:
    _pkg_version = None

# -------------------------
# Set up locale and translation
# -------------------------
# Ensure the modules/ directory (sibling of this file) is on the path
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from modules.i18n import _, ngettext

# -------------------------
# PackageState Class
# -------------------------

class PackageState:
    @staticmethod
    def get_installed_packages(command_runner_sync):
        """Return a dict of installed packages and their versions."""
        try:
            Debug.log("get_installed_packages: calling luet database get-all-installed")
            _t0 = time.time()
            res = command_runner_sync(["luet", "database", "get-all-installed"], require_root=True)
            Debug.log(f"get_installed_packages: luet call done in {time.time()-_t0:.3f}s")

            if res.returncode != 0 and not res.stdout.strip():
                print(f"luet database get-all-installed failed with return code {res.returncode}. Stderr: {res.stderr}")
                return {}

            # Fast line scanner — avoids yaml.safe_load which takes ~2.5s on 54k lines.
            # We only need category, name, version from each package block.
            Debug.log("get_installed_packages: parsing output")
            _t1 = time.time()
            pkgs = {}
            cat = name = ver = None
            for line in res.stdout.splitlines():
                # Only match top-level fields (2 spaces indent) to avoid
                # picking up nested fields in requires/provides blocks
                if line.startswith('  category:'):
                    cat = line[len('  category:'):].strip().strip('"')
                elif line.startswith('  name:'):
                    name = line[len('  name:'):].strip().strip('"')
                elif line.startswith('  version:'):
                    ver = line[len('  version:'):].strip().strip('"')
                if cat and name and ver:
                    pkgs[f"{cat}/{name}"] = ver
                    cat = name = ver = None
            Debug.log(f"get_installed_packages: parse done in {time.time()-_t1:.3f}s, {len(pkgs)} packages")
            return pkgs

        except Exception as e:
            print("Error fetching installed package list:", e)
            return {}

# -------------------------
# Spinner Class
# -------------------------
class Spinner:
    """
    Centralized spinner animation frames and management.
    Provides a consistent spinner animation across GUI and TUI interfaces.
    """
    
    # Unicode Braille pattern spinner frames
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    
    def __init__(self):
        """Initialize a new spinner instance."""
        self._frame_index = 0
    
    @classmethod
    def get_frames(cls):
        """
        Get the list of spinner frames.
        
        :return: List of spinner frame characters
        """
        return cls.FRAMES

    def get_current_frame(self):
        """
        Get the current spinner frame character.
        
        :return: The current spinner character (string)
        """
        return self.FRAMES[self._frame_index]

    def advance(self):
        """
        Advance the spinner to the next frame in the sequence AND return it.
        """
        self._frame_index = (self._frame_index + 1) % len(self.FRAMES)
        return self.get_current_frame()

# -------------------------
# Application Metadata/About Info
# -------------------------
class AboutInfo:
    """
    Centralized metadata for the luet package manager frontend.
    """
    @staticmethod
    def get_program_name():
        return _("Vajo: a Luet Package Manager Frontend")
        
    @staticmethod
    def get_version():
        return "0.9.3"

    @staticmethod
    def get_copyright():
        return _("© 2023 - 2026 MocaccinoOS. All Rights Reserved")

    @staticmethod
    def get_website():
        return "https://www.mocaccino.org"
        
    @staticmethod
    def get_authors():
        return ["Joost Ruis"]

    @staticmethod
    def get_github_repo_uri():
        return "https://github.com/joostruis/luet_pm_gui"
        
    @staticmethod
    def get_ncurses_about_text():
        """Returns a consolidated, translated string for the ncurses TUI about box."""
        return _("Vajo: a Luet Package Manager TUI (ncurses)\nVersion: {}\nWebsite: {}\nAuthors: {}\n(Uses vajo_core.py)").format(
            AboutInfo.get_version(),
            AboutInfo.get_website(),
            ", ".join(AboutInfo.get_authors())
        )

# -------------------------
# Core Command Runner
# -------------------------
class CommandRunner:
    """
    Handles execution of synchronous and asynchronous commands,
    including elevation and thread-safe callbacks.
    """
    def __init__(self, elevation_cmd, schedule_callback):
        """
        :param elevation_cmd: List like ["pkexec"] or ["sudo"] or None
        :param schedule_callback: A function (like GLib.idle_add or queue.put)
                                  to run callbacks on the main thread.
        """
        self.elevation_cmd = elevation_cmd
        self.schedule_callback = schedule_callback

    def run_sync(self, cmd_list, require_root=False):
        """
        Runs a command synchronously and returns the result object.
        """
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                raise RuntimeError(_("No elevation helper available"))
        try:
            return subprocess.run(final, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            class _Res: pass
            r = _Res()
            r.returncode = 1
            r.stdout = ""
            r.stderr = str(e)
            return r

    def run_realtime(self, cmd_list, require_root, on_line_received, on_finished):
        """
        Runs a command asynchronously in a thread, piping output in real-time.
        """
        final = list(cmd_list)
        if require_root and os.getuid() != 0:
            if self.elevation_cmd:
                final = self.elevation_cmd + final
            else:
                # Schedule the error callback on the main thread
                self.schedule_callback(on_finished, -1)
                return

        def thread_func():
            try:
                process = subprocess.Popen(
                    final,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                for line in iter(process.stdout.readline, ''):
                    # Strip "INFO ", "WARN ", etc.
                    if line.startswith(" INFO "):
                        line = line[5:]
                    elif line.startswith(" WARN "):
                        line = line[5:]
                    elif line.startswith(" ERROR "):
                        line = line[6:]
                    
                    # Schedule the line callback on the main thread
                    self.schedule_callback(on_line_received, line)
                
                process.stdout.close()
                return_code = process.wait()
                # Schedule the final callback on the main thread
                self.schedule_callback(on_finished, return_code)
            except Exception as e:
                error_line = _("\nError executing command: {}\n").format(e)
                self.schedule_callback(on_line_received, error_line)
                self.schedule_callback(on_finished, -1)

        thread = threading.Thread(target=thread_func, daemon=True)
        thread.start()


# -------------------------
# Package Filter
# -------------------------
class PackageFilter:
    """
    Handles filtering of protected and hidden packages.
    """
    # Define dictionaries ONCE as class constants
    _PROTECTED_PKGS = {
        "apps/grub": "This package is protected and can't be removed",
        "system/gcc-base": "This package is protected and can't be removed",
        "system/kernel-updater": "This package is protected and can't be removed",
        "system/luet": "This package is protected and can't be removed",
        "layers/system-x": "This layer is protected and can't be removed",
        "layers/sys-fs": "This layer is protected and can't be removed",
        "layers/X": "This layer is protected and can't be removed",
    }

    _HIDDEN_PKGS = {
            # Devel repositories we hide
            "repository/mocaccino-desktop": "Devel repository",
            "repository/mocaccino-os-commons": "Devel repository",
            "repository/mocaccino-extra": "Devel repository",
            "repository/mocaccino-community": "Devel repository",
            # Crucial repositories users should not remove
            "repository/luet": "This repository is crucial and can't be removed",
            "repository/mocaccino-repository-index": "This repository is crucial and can't be removed",
            # Stable repositories
            "repository/mocaccino-desktop-stable": "Stable repository can't be removed",
            "repository/mocaccino-os-commons-stable": "Stable repository can't be removed",
            "repository/mocaccino-extra-stable": "Stable repository can't be removed",
            # Repositories we just want to hide
            "repository/livecd": "This repository should be hidden",
            "repository/mocaccino-stage3": "Old repository, not in use anymore",
            "repository/mocaccino-portage": "Old repository, not in use anymore",
            "repository/mocaccino-portage-stable": "Old repository, not in use anymore",
            "repository/mocaccino-kernel": "Old repository, not in use anymore",
            "repository/mocaccino-kernel-stable": "Old repository, not in use anymore",
            "repository/mocaccino-extra-arm": "Old repository, not in use anymore",
            "repository/mocaccino-musl-universe": "Hide musl repo",
            "repository/mocaccino-musl-universe-stable": "Hide musl repo",
            "repository/mocaccino-micro": "Hide micro repo",
            "repository/mocaccino-micro-stable": "Hide micro repo",
            "repo-updater/mocaccino-micro-stable": "Hide micro repo-updater",
            "repo-updater/mocaccino-desktop-stable": "Hide desktop repo-updater",
            "repo-updater/mocaccino-community-stable": "Hide desktop repo-updater",
            "kernel-5.9/debian-full": "Old repository, not in use anymore",
    }


    @staticmethod
    def get_protected_packages():
        return PackageFilter._PROTECTED_PKGS
    
    @staticmethod
    def get_hidden_packages():
        return PackageFilter._HIDDEN_PKGS
    
    @staticmethod
    def is_package_hidden(category, name):
        """
        Check if a package should be hidden from the search results.
        
        :param category: Package category
        :param name: Package name
        :return: True if package should be hidden, False otherwise
        """
        if category in ("entity", "buildbase", "layerbase", "acct-group", "acct-user"):
            return True
        
        key = "{}/{}".format(category, name)
        hidden_packages = PackageFilter.get_hidden_packages()
        return key in hidden_packages
    
    @staticmethod
    def is_package_protected(category, name):
        """
        Check if a package is protected from removal.
        
        :param category: Package category
        :param name: Package name
        :return: True if package is protected, False otherwise
        """
        key = "{}/{}".format(category, name)
        protected_packages = PackageFilter.get_protected_packages()
        return key in protected_packages
    
    @staticmethod
    def get_protection_message(category, name):
        """
        Get the protection message for a protected package.
        
        :param category: Package category
        :param name: Package name
        :return: Protection message or None if not protected
        """
        key = "{}/{}".format(category, name)
        protected_packages = PackageFilter.get_protected_packages()
        return protected_packages.get(key)


# -------------------------
# Helpers: Core Logic Classes
# -------------------------
class RepositoryUpdater:
    @staticmethod
    def run_repo_update(
        command_runner_realtime, # Injected: CommandRunner.run_realtime
        inhibit_setter,
        on_log_callback, 
        on_success_callback, 
        on_error_callback, 
        on_finish_callback,
        schedule_callback      # Injected: The main-thread scheduler
    ):
        inhibit_cookie = 0
        try:
            inhibit_cookie = inhibit_setter(True, _("Updating repositories"))

            def on_done(returncode):
                # Handle result on main thread via the scheduler
                if returncode == 0:
                    schedule_callback(on_success_callback)
                else:
                    schedule_callback(on_error_callback)
                
                # Cleanup (Stop spinner, release inhibition)
                schedule_callback(on_finish_callback, inhibit_cookie)

            command_runner_realtime(
                ["luet", "repo", "update"],
                require_root=True,
                on_line_received=on_log_callback,
                on_finished=on_done
            )

        except Exception as e:
            print("Exception during repo update:", e)
            schedule_callback(on_error_callback)
            schedule_callback(on_finish_callback, inhibit_cookie)

class SystemChecker:
    @staticmethod
    def _parse_reinstall_candidates(output):
        candidates = {}
        pkg_id_pattern = re.compile(r"(\S+/\S+)")
        version_suffix_pattern = re.compile(r'-\d+(\.\d+)*.*$')

        for line in output.split('\n'):
            line = line.strip()
            match = pkg_id_pattern.search(line)
            if match:
                full_pkg_id = match.group(1).split(':')[0]
                parts = full_pkg_id.split('/')
                if len(parts) >= 2:
                    category = parts[-2]
                    pkg_name_with_version = parts[-1]
                    pkg_name_only = version_suffix_pattern.sub('', pkg_name_with_version)
                    full_name_for_reinstall = f"{category}/{pkg_name_only}"
                    if full_name_for_reinstall:
                        candidates[full_name_for_reinstall] = True
        return sorted(candidates.keys())

    @staticmethod
    def run_check_system(
        command_runner_sync,            # Injected: CommandRunner.run_sync
        log_callback,
        on_thread_exit_callback,
        on_reinstall_start_callback,
        on_reinstall_status_callback,
        on_reinstall_finish_callback,
        sleep_function,
        translation_function
    ):
        t = threading.Thread(
            target=SystemChecker._do_check_system,
            args=(
                command_runner_sync, 
                log_callback,
                on_thread_exit_callback,
                on_reinstall_start_callback,
                on_reinstall_status_callback,
                on_reinstall_finish_callback,
                sleep_function,
                translation_function
            ),
            daemon=True
        )
        t.start()

    @staticmethod
    def _do_check_system(
        command_runner_sync, 
        log_callback,
        on_thread_exit_callback,
        on_reinstall_start_callback,
        on_reinstall_status_callback,
        on_reinstall_finish_callback,
        sleep_function,
        _
    ):
        status_message = None 
        
        def log_result(command, result):
            full_log = (result.stdout or "") + (result.stderr or "") + "\n"
            if full_log.strip():
                log_callback(full_log)
            sleep_function(0.05)

        try:
            command = ["luet", "oscheck"]
            result = command_runner_sync(command, require_root=True) 
            log_result(command, result) 
            output = result.stdout + result.stderr

            if result.returncode != 0:
                raise Exception(_("luet oscheck failed with return code {}").format(result.returncode))

            if "missing" in output:
                candidates = SystemChecker._parse_reinstall_candidates(output)
                
                if candidates:
                    log_callback(_("Repair sequence started for {} missing packages.\n").format(len(candidates)))
                    found_message = _("Found {} missing packages. Starting repair immediately.\n").format(len(candidates))
                    log_callback(found_message)
                    sleep_function(2.0) 

                    repair_ok = True
                    for pkg in candidates:
                        reinstall_status = _("Reinstalling {}...").format(pkg)
                        log_callback(reinstall_status + "\n")
                        sleep_function(2.0) 
                        
                        reinstall_result = command_runner_sync(
                            ["luet", "reinstall", "-y", pkg],
                            require_root=True,
                        )
                        log_result(["luet", "reinstall", "-y", pkg], reinstall_result)
                        sleep_function(1.0) 

                        if reinstall_result.returncode != 0:
                            repair_ok = False
                            log_callback(_("Failed reinstalling {}").format(pkg) + "\n")
                    
                    # This callback is responsible for main-thread scheduling
                    on_reinstall_finish_callback(repair_ok) 
                    return 
            pass
        except Exception as e:
            print("System check critical exception:", e)
            if isinstance(e, Exception) and "return code" in str(e):
                status_message = str(e)
            else:
                status_message = _("System check failed due to exception")
        finally:
            if status_message:
                final_message = status_message
                # This callback is responsible for main-thread scheduling
                on_thread_exit_callback(final_message)
            elif status_message is None:
                # This callback is responsible for main-thread scheduling
                on_thread_exit_callback(_("Ready"))

class SystemUpgrader:
    def __init__(
        self, 
        command_runner_realtime, 
        log_callback, 
        status_callback, 
        schedule_callback, 
        post_action_callback, 
        on_finish_callback,
        inhibit_cookie,
        translation_func
    ):
        self.command_runner = command_runner_realtime
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.schedule_callback = schedule_callback
        self.post_action_callback = post_action_callback
        self.on_finish_callback = on_finish_callback
        self.inhibit_cookie = inhibit_cookie
        self._ = translation_func
        self.collected_lines = []

    def start_upgrade(self):
        try:
            # Always write the latest stable reference before upgrading.
            # unpin_references() fetches the current stable tag from
            # collection.yaml and writes it to the conf files, ensuring
            # luet resolves against the correct stable tag rather than
            # the latest available tag on quay.io.
            unpin_cmd = RollbackManager.unpin_references()
            remove_pin = f"rm -f {RollbackManager.PIN_FILE} {RollbackManager.ANCHOR_FILE}"
            if unpin_cmd:
                upgrade_cmd = ["sh", "-c",
                               f"{unpin_cmd} && {remove_pin} && luet repo update && luet upgrade -y"]
            else:
                upgrade_cmd = ["sh", "-c",
                               f"{remove_pin} && luet repo update && luet upgrade -y"]
            self.command_runner(
                upgrade_cmd,
                require_root=True,
                on_line_received=self._on_line_first_run,
                on_finished=self._on_first_run_done
            )
        except Exception as e:
            print("Exception during system upgrade:", e)
            self._on_first_run_done(-1, self._("Error starting upgrade process"))
            
    def _on_line_first_run(self, line):
        self.collected_lines.append(line)
        self.log_callback(line)

    def _on_first_run_done(self, returncode, error_message=None):
        if returncode != 0:
            msg = error_message or self._("Error during initial upgrade step")
            self._finalize(returncode, msg)
            return

        needs_second_run = any("Executing finalizer for repo-updater/" in line for line in self.collected_lines)

        if needs_second_run:
            self.schedule_callback(self._run_second_upgrade)
        else:
            self._finalize(returncode, self._("System upgrade completed successfully"))

    def _run_second_upgrade(self):
        status_msg = self._("\n--- Repositories updated, continuing with package upgrade... ---\n\n")
        self.status_callback(self._("Continuing with package upgrade..."))
        self.log_callback(status_msg)

        try:
            second_upgrade_cmd = ["luet", "upgrade", "-y"]
            self.command_runner(
                second_upgrade_cmd,
                require_root=True,
                on_line_received=self.log_callback,
                on_finished=lambda rc: self._finalize(rc, self._("System upgrade completed successfully"))
            )
        except Exception as e:
            print("Exception during second upgrade step:", e)
            self._finalize(-1, self._("Error starting second upgrade step"))

    def _finalize(self, returncode, success_message):
        # Call the GUI's on_finish handler (already on main thread)
        self.on_finish_callback(returncode, success_message)
        if returncode == 0:
            self.post_action_callback()

class CacheCleaner:
    @staticmethod
    def get_cache_size_bytes():
        try:
            res = subprocess.run(
                ["du", "-sb", "/var/luet/db/packages/"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if res.returncode == 0:
                return int(res.stdout.strip().split("\t", 1)[0])
        except Exception as e:
            print("Error checking Luet cache size:", e)
        return None

    @staticmethod
    def get_cache_size_human(size_bytes):
        if not size_bytes or size_bytes <= 4096:
            return None
        try:
            res = subprocess.run(
                ["du", "-hs", "/var/luet/db/packages/"],
                stdout=subprocess.PIPE, text=True
            )
            if res.returncode == 0:
                return res.stdout.split("\t", 1)[0].strip()
        except Exception:
            pass
        return f"{size_bytes}B"

    @staticmethod
    def get_cache_info():
        """
        Get cache information including size and availability.
        
        :return: Dictionary with 'has_cache' (bool), 'size_bytes' (int or None), 
                 'size_human' (str or None), and 'menu_label' (str)
        """
        size_bytes = CacheCleaner.get_cache_size_bytes()
        size_human = CacheCleaner.get_cache_size_human(size_bytes)
        
        has_cache = size_human is not None
        
        if has_cache:
            menu_label = _("Clear Luet cache ({})").format(size_human)
        else:
            menu_label = _("Clear Luet cache")
        
        return {
            'has_cache': has_cache,
            'size_bytes': size_bytes,
            'size_human': size_human,
            'menu_label': menu_label
        }

    @staticmethod
    def run_cleanup_core(command_runner_realtime, log_callback, on_finish):
        command_runner_realtime(
            ["luet", "cleanup"],
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish
        )

class PackageOperations:
    @staticmethod
    def _run_kbuildsycoca6():
        kbuild_path = shutil.which("kbuildsycoca6")
        if kbuild_path:
            try:
                subprocess.run([kbuild_path], capture_output=True, text=True, check=False)
            except Exception:
                pass

    @staticmethod
    def build_install_command(full_package_name):
        """
        Constructs the standard install command.
        """
        return ["luet", "install", "-y", full_package_name]

    @staticmethod
    def build_uninstall_command(category, full_package_name):
        """
        Constructs the uninstall command, handling category-specific flags
        (e.g. extra flags for 'apps' category).
        """
        cmd = ["luet", "uninstall", "-y", full_package_name]
        
        # Centralized logic for specific categories
        if category == "apps":
            cmd.extend(["--solver-concurrent", "--full"])
            
        return cmd

    @staticmethod
    def run_installation(command_runner_realtime, log_callback, on_finish_callback, install_cmd_list):
        command_runner_realtime(
            install_cmd_list,
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish_callback
        )

    @staticmethod
    def run_uninstallation(command_runner_realtime, log_callback, on_finish_callback, uninstall_cmd_list):
        command_runner_realtime(
            uninstall_cmd_list,
            require_root=True,
            on_line_received=log_callback,
            on_finished=on_finish_callback
        )
    
    @staticmethod
    def run_uninstallation_with_fallback(command_runner_realtime, log_callback, on_finish_callback, category, full_package_name):
        """
        Runs uninstallation with automatic fallback to regular uninstall if needed.
        For 'apps' category, first tries with --solver-concurrent --full flags.
        If that produces "Nothing to do", retries with regular uninstall.
        Shows output from first attempt in real-time, suppresses header on retry.
        """
        collected_output = []
        seen_uninstall_header = False
        got_nothing_to_do = False
        
        def collect_and_log(line):
            """Collects output lines and forwards to log callback immediately"""
            nonlocal seen_uninstall_header, got_nothing_to_do
            collected_output.append(line)
            
            # Check if this is the "Nothing to do" line
            if "Nothing to do" in line:
                got_nothing_to_do = True
                # Don't log it yet - we'll suppress it if we retry
                return
            
            # Log the line
            log_callback(line)
            
            # Track if we've seen the ":::> Uninstall" header
            if ":::>" in line and "Uninstall" in line:
                seen_uninstall_header = True
        
        def collect_retry_output(line):
            """For retry: skip header/banner lines, only show actual removal actions"""
            nonlocal seen_uninstall_header
            
            # Skip the Luet banner lines
            if any(skip in line for skip in [
                "Luet 0.0.0",
                "Copyright (C)",
                "This program comes",
                "This is free software",
                "For documentation"
            ]):
                return
            
            # Skip the ":::> Uninstall" header if we already showed it from first attempt
            if ":::>" in line and "Uninstall" in line:
                if seen_uninstall_header:
                    return
                seen_uninstall_header = True
            
            # Forward actual removal/action lines and other content
            log_callback(line)
        
        def on_first_attempt_done(returncode):
            """Handles the result of the first uninstall attempt"""
            nonlocal got_nothing_to_do
            
            # If uninstall succeeded, we're done (output already shown)
            if returncode == 0 and not got_nothing_to_do:
                on_finish_callback(returncode)
                return
            
            # If it's apps category and we got "Nothing to do", try fallback
            if category == "apps" and got_nothing_to_do:
                # Build regular uninstall command without extra flags
                fallback_cmd = ["luet", "uninstall", "-y", full_package_name]
                
                # Retry with regular uninstall - filter output to skip duplicate header
                command_runner_realtime(
                    fallback_cmd,
                    require_root=True,
                    on_line_received=collect_retry_output,
                    on_finished=on_finish_callback
                )
            else:
                # Not apps category or different error - pass through
                # If we had "Nothing to do" but it's not apps category, show it now
                if got_nothing_to_do:
                    for line in collected_output:
                        if "Nothing to do" in line:
                            log_callback(line)
                on_finish_callback(returncode)
        
        # Build initial uninstall command (with category-specific flags)
        initial_cmd = PackageOperations.build_uninstall_command(category, full_package_name)
        
        # Run the first attempt - show output in real-time
        command_runner_realtime(
            initial_cmd,
            require_root=True,
            on_line_received=collect_and_log,
            on_finished=on_first_attempt_done
        )

    @staticmethod
    def run_post_transaction_refresh(run_sync_func, schedule_callback, on_completion_callback):
        """
        Runs post-transaction tasks (cache refresh, system checks) in a background thread
        to prevent UI blocking.
        
        :param run_sync_func: Function to run sync commands (CommandRunner.run_sync)
        :param schedule_callback: Function to schedule work on the main UI thread
        :param on_completion_callback: Callback to receive (new_cache_data)
        """
        def worker():
            # 1. If system is pinned, re-apply the reference: to conf files
            # in case a package install overwrote them
            repo_updated = False
            if RollbackManager.is_pinned():
                pinned_version = RollbackManager.get_current_desktop_version()
                if pinned_version:
                    was_community_enabled = RollbackManager.is_community_enabled()
                    RollbackManager._restore_pin(pinned_version, run_sync_func)
                    # If community was just enabled (wasn't before), update repos
                    # so the pinned community index gets downloaded
                    if not was_community_enabled and RollbackManager.is_community_enabled():
                        run_sync_func(["luet", "repo", "update"], require_root=True)
                        repo_updated = True

            # 2. Refresh the installed packages list (Blocking/Slow)
            try:
                new_cache = PackageState.get_installed_packages(run_sync_func)
            except Exception as e:
                print(f"Error refreshing cache: {e}")
                new_cache = {}

            # 3. Run system updates (Blocking)
            PackageOperations._run_kbuildsycoca6()

            # 4. Return result on the main thread
            schedule_callback(on_completion_callback, new_cache)

        threading.Thread(target=worker, daemon=True).start()

class PackageSearcher:
    @staticmethod
    def run_search_core(command_runner_sync, search_command):
        try:
            res = command_runner_sync(search_command, require_root=True)
            if res.returncode != 0:
                print("Search error:", res.stderr)
                return {"error": _("Error executing the search command")}
            
            output = (res.stdout or "").strip()
            if not output:
                 return {"packages": []}
            
            data = json.loads(output)
            packages = data.get("packages") if isinstance(data, dict) else None
            
            if packages is None:
                return {"packages": []}
                
            return {"packages": packages}
        except json.JSONDecodeError:
            print("Search error: Invalid JSON")
            return {"error": _("Invalid JSON output")}
        except Exception as e:
            print(_("Error running search:"), e)
            return {"error": _("Error executing the search command")}

# -------------------------
# SearchProcessor - unified search result processing
# -------------------------
class SearchProcessor:
    """Unified search result processing for both GUI and TUI"""
    
    @staticmethod
    def process_search_results(search_result, installed_packages_dict, skip_hidden=False):
        """Process and enrich search results with installation status and upgrade info"""
        if "error" in search_result:
            return search_result
        
        processed_packages = []
        for pkg in search_result.get("packages", []):
            pkg = SearchProcessor._enrich_package_info(pkg, installed_packages_dict)
            if skip_hidden or not PackageFilter.is_package_hidden(pkg.get("category", ""), pkg.get("name", "")):
                processed_packages.append(pkg)
        
        search_result["packages"] = processed_packages
        return search_result
    
    @staticmethod
    def _enrich_package_info(pkg, installed_packages_dict):
        """Add installation status and upgrade information to package"""
        category, name = pkg.get("category", ""), pkg.get("name", "")
        key = f"{category}/{name}"
        
        pkg['upgrade_symbol'] = ""
        pkg['is_actually_installed'] = False
        pkg['installed_version'] = ""
        pkg['available_version'] = pkg.get("version", "")
        pkg['protected'] = PackageFilter.is_package_protected(category, name)
        
        if key in installed_packages_dict:
            pkg['is_actually_installed'] = True
            installed_version = installed_packages_dict[key]
            pkg['installed_version'] = installed_version
            
            # Check if packaging module is available for version comparison
            if _pkg_version is not None:
                available_version = pkg.get("version")
                if installed_version and available_version:
                    try:
                        if _pkg_version.parse(available_version) > _pkg_version.parse(installed_version):
                            pkg['upgrade_symbol'] = "↑"
                    except (_pkg_version.InvalidVersion, TypeError):
                        pass
        
        return pkg
        
# -------------------------
# DescriptionIndex - local treefs description search
# -------------------------
class DescriptionIndex:
    """
    Builds an in-memory index of package descriptions by walking the luet
    treefs at /var/luet/db/repos. Used to enable description-based search
    that luet's CLI does not natively support.
    """

    REPOS_PATH = "/var/luet/db/repos"

    def __init__(self):
        self._index = {}  # "category/name" -> dict
        self._ready = False
        self._lock = threading.Lock()

    def build_async(self, run_sync, on_ready_callback=None):
        """
        Walk the treefs in a background thread and build the index.
        Uses a single privileged 'find' call to locate all definition.yaml
        files, then reads them with a privileged Python subprocess so that
        root-only paths under /var/luet/db/repos are accessible.
        """
        def worker():
            index = {}
            Debug.log("DescriptionIndex: build started")
            _t0 = time.time()
            try:
                # Single privileged grep — ~90ms vs ~7s for YAML parsing
                # Derives category/name/version/repo from path structure:
                # .../repos/<repo>/treefs/<cat>/<name>/<version>/definition.yaml
                res = run_sync(
                    ["grep", "-rH", "--include=definition.yaml",
                     "^description:", self.REPOS_PATH],
                    require_root=True
                )
                Debug.log(f"DescriptionIndex: grep done in {time.time()-_t0:.3f}s")

                if res.returncode != 0 or not res.stdout.strip():
                    print("DescriptionIndex: grep returned no results")
                    return

                for line in res.stdout.splitlines():
                    try:
                        # line format: /path/to/definition.yaml:description: some text
                        colon_idx = line.index(':description:')
                        filepath = line[:colon_idx]
                        desc = line[colon_idx + len(':description:'):].strip()
                        if not desc:
                            continue

                        parts = filepath.split('/')
                        ri = parts.index('repos')
                        repo = parts[ri + 1]
                        cat  = parts[ri + 3]
                        name = parts[ri + 4]
                        ver  = parts[ri + 5]

                        if not (cat and name and desc):
                            continue

                        key = f"{cat}/{name}"
                        if key not in index:
                            index[key] = {
                                "category":    cat,
                                "name":        name,
                                "version":     ver,
                                "description": desc,
                                "repository":  repo,
                            }
                    except Exception:
                        continue

            except Exception as e:
                print(f"DescriptionIndex: error building index: {e}")

            Debug.log(f"DescriptionIndex: build complete, {len(index)} packages indexed in {time.time()-_t0:.3f}s")
            with self._lock:
                self._index = index
                self._ready = True
            if on_ready_callback:
                on_ready_callback()

        threading.Thread(target=worker, daemon=True).start()

    @property
    def is_ready(self):
        with self._lock:
            return self._ready

    def search(self, query):
        """
        Case-insensitive multi-word search over package name and description.
        All words in the query must match somewhere in the name or description.
        Returns a list of package dicts matching the query.
        """
        words = query.lower().split()
        if not words:
            return []
        with self._lock:
            if not self._ready:
                return []
            results = []
            for pkg in self._index.values():
                haystack = (pkg.get("name", "") + " " + pkg.get("description", "")).lower()
                if all(w in haystack for w in words):
                    results.append(dict(pkg))
            return results


from modules.rollback import RollbackManager
class SyncInfo:
    @staticmethod
    def parse_timestamp(ts):
        try:
            if ts.endswith('Z'):
                ts = ts[:-1]
            try:
                return datetime.datetime.fromisoformat(ts)
            except Exception:
                return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None

    @staticmethod
    def humanize_time_ago(dt):
        if dt.tzinfo is None:
            now = datetime.datetime.now()
        else:
            now = datetime.datetime.now(dt.tzinfo)
        delta = now - dt

        if delta.days > 0:
            return ngettext("%d day ago", "%d days ago", delta.days) % delta.days
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return ngettext("%d hour ago", "%d hours ago", hours) % hours
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return ngettext("%d minute ago", "%d minutes ago", minutes) % minutes
        else:
            return _("just now")

    @staticmethod
    def get_last_sync_time():
        sync_file_path = "/var/luet/db/repos/luet/SYNCTIME"
        try:
            with open(sync_file_path, 'r') as f:
                timestamp = f.read().strip()
                sync_dt = SyncInfo.parse_timestamp(timestamp)
                if sync_dt:
                    time_ago = SyncInfo.humanize_time_ago(sync_dt)
                    return {"datetime": sync_dt.strftime("%Y-%m-%dT%H:%M:%S"), "ago": time_ago}
        except (IOError, ValueError):
            pass
        return {"datetime": "N/A", "ago": _("repositories not synced")}


# -------------------------
# PackageDetails - shared logic for GUI + TUI
# -------------------------
class PackageDetails:
    """
    Retrieve Luet package metadata, file lists, and reverse dependencies.
    All methods accept a run_command_sync function (CommandRunner.run_sync) so the
    caller controls elevation and scheduling.
    """

    @staticmethod
    def get_definition_yaml(run_command_sync, repository, category, name, version):
        """Load definition.yaml content for the package via run_command_sync.
        Returns a dict or None. If repository is empty, searches all repos.
        """
        try:
            if repository:
                repos = [repository]
            else:
                # No repository known — search all repos (e.g. from installed packages list)
                res = run_command_sync(
                    ["find", "/var/luet/db/repos", "-mindepth", "1", "-maxdepth", "1", "-type", "d"],
                    require_root=True
                )
                if res.returncode != 0:
                    return None
                repos = [os.path.basename(p.strip()) for p in res.stdout.splitlines() if p.strip()]

            for repo in repos:
                path = f"/var/luet/db/repos/{repo}/treefs/{category}/{name}/{version}/definition.yaml"
                res = run_command_sync(["cat", path], require_root=True)
                if res.returncode == 0 and res.stdout:
                    return yaml.safe_load(res.stdout)
            return None
        except Exception:
            return None

    @staticmethod
    def get_files(run_command_sync, category, name):
        try:
            cmd = ["luet", "search", f"{category}/{name}", "-o", "json"]
            res = run_command_sync(cmd, require_root=True)
            if res.returncode != 0:
                return []
            data = json.loads(res.stdout or "{}")
            if isinstance(data, dict) and data.get("packages"):
                return data["packages"][0].get("files", [])
            return []
        except Exception:
            return []

    @staticmethod
    def get_required_by(run_command_sync, category, name):
        try:
            cmd = ["luet", "search", "--revdeps", f"{category}/{name}", "-q", "--installed", "-o", "json"]
            res = run_command_sync(cmd, require_root=True)
            if res.returncode != 0:
                return []
            data = json.loads(res.stdout or "{}")
            if isinstance(data, dict) and data.get("packages"):
                return [f"{p.get('category','')}/{p.get('name','')}" for p in data.get("packages", [])]
            return []
        except Exception:
            return []

    @staticmethod
    def format_for_tui(details, files, revdeps, repository=None, version=None, installed=None):
        if not details:
            return _("No metadata found for this package.")
        
        # --- Labels for alignment ---
        # Fixed width for the left column alignment
        align_width_left = 10 
        
        # Left Column Labels
        version_label = _("Version")
        installed_label = _("Installed")
        license_label = _("License")
        
        # Right Column Labels
        repo_label = _("Repository")
        desc_label = _("Description")
        homepage_label = _("Homepage")
        
        # Dynamic width calculation for the right column to ensure perfect colon alignment
        align_width_right = max(
            len(repo_label),
            len(desc_label),
            len(homepage_label)
        )
        # --- End Labels ---
        
        # 1. Prepare data variables
        installed_text = _("Yes") if installed else _("No") if installed is not None else _("(unknown)")
        
        uri = details.get("uri") or details.get("source") or ""
        if isinstance(uri, list):
            uri = uri[0] if uri else ""
        
        license_ = details.get("license") or details.get("licenses") or ""
        if isinstance(license_, list):
            license_ = ", ".join(license_)
        
        # 2. Build the left and right columns (as aligned strings)
        left_column = []
        right_column = []

        # LEFT Column: Labels use fixed align_width_left
        if version:
            left_column.append(f"{version_label:>{align_width_left}}: {version}")
        
        left_column.append(f"{installed_label:>{align_width_left}}: {installed_text}")
        
        # --- License Wrapping Logic ---
        wrap_width_left = 30 # Safe width for the left column's data
        license_indent = " " * (align_width_left + 2) 
        
        license_lines = []
        license_value = license_ if license_ else _('(none)')

        if license_value != _('(none)'):
            words = license_value.split()
            current_line = ""
            for word in words:
                # Check if adding the word exceeds the wrap width
                if len(current_line) + len(word) + (1 if current_line else 0) > wrap_width_left:
                    license_lines.append(current_line.strip())
                    current_line = word + " "
                else:
                    current_line += (word + " ")
            if current_line: # Append the last line
                license_lines.append(current_line.strip())
        else:
            license_lines.append(license_value)
        
        # Add the first line with the label
        left_column.append(f"{license_label:>{align_width_left}}: {license_lines[0]}")

        # Add subsequent lines with indentation
        for line in license_lines[1:]:
            left_column.append(f"{license_indent}{line}")
        # --- End License Wrapping Logic ---

        # RIGHT Column: Labels now use dynamically calculated align_width_right
        if repository:
            right_column.append(f"{repo_label:>{align_width_right}}: {repository}")
        
        # --- Description Wrapping Logic ---
        desc = details.get("description") or details.get("long_description") or ""
        wrap_width_right = 33 # Width for the right column's data
        desc_indent = " " * (align_width_right + 2) 

        desc_lines = []
        if desc:
            words = desc.split()
            current_line = ""
            for word in words:
                if len(current_line) + len(word) + (1 if current_line else 0) > wrap_width_right:
                    desc_lines.append(current_line.strip())
                    current_line = word + " "
                else:
                    current_line += (word + " ")
            if current_line:
                desc_lines.append(current_line.strip())
        else:
            desc_lines.append(_('(none)'))
        
        # Add the first line with the label
        right_column.append(f"{desc_label:>{align_width_right}}: {desc_lines[0]}")

        # Add subsequent lines with indentation
        for line in desc_lines[1:]:
            right_column.append(f"{desc_indent}{line}")
        # --- End Description Wrapping Logic ---
        
        right_column.append(f"{homepage_label:>{align_width_right}}: {uri if uri else _('(none)')}")
        
        # 3. Interleave the columns and format output
        out = []
        
        # Combine the lists, padding the shorter one with empty strings
        max_lines = max(len(left_column), len(right_column))
        
        left_column.extend([""] * (max_lines - len(left_column)))
        right_column.extend([""] * (max_lines - len(right_column)))
        
        # Calculate padding for the left column to create separation.
        left_width = align_width_left + 2 + 30 

        for left_line, right_line in zip(left_column, right_column):
            # Pad the left line with spaces to create separation between columns
            padded_left = left_line.ljust(left_width)
            out.append(f"{padded_left}{right_line}")
        
        # NOTE: Removed 'Required by:' and 'Files:' sections here.
        return "\n".join(out)
