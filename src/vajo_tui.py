#!/usr/bin/env python3
"""
luet_pm_ncurses.py — curses-based TUI using vajo_core.py

Implements a full TUI interface with a functional menu, thread-safe command execution,
and robust package listing and log viewing, with full translation support via _() and ngettext.
"""

import collections
import curses
import curses.textpad
import curses.ascii
import threading
import time
import queue
import locale
import traceback
import shutil
import os
import sys
import signal

# -------------------------
# Version-Agnostic Core Discovery
# -------------------------
# Version-Agnostic Core Discovery
# -------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARED_LIB_PATH = "/usr/share/vajo"

# In development all files are siblings inside src/, so vajo_core.py is
# right next to this script. When installed, this script lives in /usr/bin/
# and core is in /usr/share/vajo/ — fall back to that.
LOCAL_CORE = os.path.join(SCRIPT_DIR, "vajo_core.py")
if os.path.exists(LOCAL_CORE):
    sys.path.insert(0, SCRIPT_DIR)
elif os.path.exists(SHARED_LIB_PATH):
    sys.path.insert(0, SHARED_LIB_PATH)

# Import core backend
try:
    from vajo_core import (
        CommandRunner,
        RepositoryUpdater,
        SystemChecker,
        SystemUpgrader,
        CacheCleaner,
        PackageOperations,
        PackageSearcher,
        SyncInfo,
        PackageFilter,
        AboutInfo,
        Spinner,
        PackageDetails,
        PackageState,
        SearchProcessor,
        DescriptionIndex,
        RollbackManager,
        Debug,
        _,
        ngettext,
    )
except ImportError as e:
    print(f"FATAL: Could not import vajo_core.py from {SHARED_LIB_PATH}. Error: {e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Failed to initialize vajo_core: {e}", file=sys.stderr)
    sys.exit(1)

# Import packaging for version comparison
try:
    from packaging import version as pkg_version
except ImportError:
    print("WARNING: 'packaging' library not found. Upgrade check will not be available.")
    print("Please run 'pip install packaging'")
    pkg_version = None

# -------------------------
# Process title
# -------------------------
def set_process_title(title: str) -> None:
    """Set the process name visible in tmux, ps, top, etc."""
    # Method 1: argv[0] — affects some tools
    sys.argv[0] = title

    # Method 2: prctl PR_SET_NAME — affects tmux window title and ps
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        PR_SET_NAME = 15
        libc.prctl(PR_SET_NAME, title.encode()[:15], 0, 0, 0)
    except Exception:
        pass  # Non-Linux or unavailable — silently skip

# -------------------------
# Signal handling for graceful shutdown
# -------------------------
_tui_app_instance = None

def setup_signal_handlers():
    """Set up signal handlers for graceful TUI shutdown"""
    def signal_handler(signum, frame):
        global _tui_app_instance
        print(f"\nReceived signal {signum}, shutting down gracefully...")
        if _tui_app_instance:
            _tui_app_instance.running = False
        else:
            # Restore terminal and exit
            try:
                curses.endwin()
            except:
                pass
            sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

# --- Scheduler for Thread-Safe UI Updates ---
class Scheduler:
    """Marshals function calls from worker threads to the main thread."""
    def __init__(self):
        self.q = queue.Queue()
    def schedule(self, func, *args):
        self.q.put((func, args))
    def drain(self, max_items=200):
        processed = 0
        while processed < max_items:
            try:
                func, args = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args)
            except Exception:
                traceback.print_exc()
            processed += 1

# --- Menu Class for State and Drawing ---
class Menu:
    
    @staticmethod
    def get_menu_titles():
        """Get translated menu titles."""
        return [(_("File"), 0), (_("Help"), 1)]
    
    @staticmethod
    def get_base_menu_items():
        """Get base menu items with placeholders: None=cache, False=rollback."""
        return [
            [_("Update repositories"), _("Full system upgrade"), _("Installed packages"), _("Check system"), None, False, _("Quit")],
            [_("Documentation"), _("About")]
        ]

    def __init__(self, tui_app):
        self.app = tui_app
        self.is_open = False
        self.active_menu = 0
        self.selected_index = 0
        self.cache_menu_label = _("Clear Luet cache")
        self.cache_enabled = False
        self.rollback_enabled = False
        self.is_pinned = False
        self.update_cache_menu_item()
        self.update_rollback_menu_item()
        self._dropdown_box = None
        self._dropdown_item_rows = []

    def update_cache_menu_item(self):
        """Update the cache menu item with current cache info."""
        cache_info = CacheCleaner.get_cache_info()
        self.cache_menu_label = cache_info['menu_label']
        self.cache_enabled = cache_info['has_cache']

    def update_rollback_menu_item(self):
        """Update rollback/pinned state."""
        self.rollback_enabled = RollbackManager.is_stable_system()
        self.is_pinned = RollbackManager.is_pinned()

    def get_menu_items(self):
        """Get the current menu items with updated cache and rollback labels."""
        items = []
        for menu_idx, base_items in enumerate(self.get_base_menu_items()):
            menu = []
            for item in base_items:
                if item is None:
                    menu.append(self.cache_menu_label)
                elif item is False:
                    if self.is_pinned:
                        menu.append(_("View pinned state"))
                    else:
                        menu.append(_("Roll back"))
                else:
                    menu.append(item)
            items.append(menu)
        return items

    def draw(self, stdscr):
        """Draws the currently active dropdown menu."""
        if not self.is_open:
            self._dropdown_box = None
            return

        h, w = stdscr.getmaxyx()
        items = self.get_menu_items()[self.active_menu]

        x = 0
        for i, (title, index) in enumerate(self.get_menu_titles()):
            seg = f"  {title}  "
            if index == self.active_menu:
                break
            x += len(seg)

        width = max(len(it) for it in items) + 4
        height = len(items) + 2
        y = 1

        # Store bounding box and per-item rows for mouse hit-testing
        self._dropdown_box = (y, x, y + height - 1, x + width - 1)
        self._dropdown_item_rows = [y + 1 + i for i in range(len(items))]

        try:
            win = curses.newwin(height, width, y, x)
            win.border()
            attrs = curses.A_REVERSE

            base_items = self.get_base_menu_items()[self.active_menu]

            for idx, it in enumerate(items):
                base = base_items[idx] if idx < len(base_items) else it
                is_update_repos = (self.active_menu == 0 and it == _("Update repositories"))
                is_full_upgrade = (self.active_menu == 0 and it == _("Full system upgrade"))
                is_cache_item = (self.active_menu == 0 and base is None)
                is_rollback_item = (self.active_menu == 0 and base is False)
                item_attr = attrs if idx == self.selected_index else curses.A_NORMAL

                if is_update_repos and self.is_pinned:
                    item_attr |= curses.A_DIM
                if is_full_upgrade and self.is_pinned:
                    item_attr |= curses.A_DIM
                if is_cache_item and not self.cache_enabled:
                    item_attr |= curses.A_DIM
                if is_rollback_item and not self.rollback_enabled and not self.is_pinned:
                    item_attr |= curses.A_DIM

                win.addstr(1 + idx, 2, it[:width - 4], item_attr)

            win.noutrefresh()
        except curses.error:
            pass

    def handle_input(self, ch):
        """Handles key presses when the menu is open."""
        if ch in (27, curses.KEY_F9):
            self.is_open = False
        elif ch in (curses.KEY_LEFT,):
            self.active_menu = (self.active_menu - 1) % len(self.get_menu_titles())
            self.selected_index = 0
        elif ch in (curses.KEY_RIGHT,):
            self.active_menu = (self.active_menu + 1) % len(self.get_menu_titles())
            self.selected_index = 0
        elif ch in (curses.KEY_UP,):
            self.selected_index = max(0, self.selected_index - 1)
        elif ch in (curses.KEY_DOWN,):
            max_idx = len(self.get_menu_items()[self.active_menu]) - 1
            self.selected_index = min(max_idx, self.selected_index + 1)
        elif ch in (10, 13, ord(' ')):
            self.activate_item()
            self.is_open = False

        return True

    def activate_item(self):
        """Executes the action associated with the selected menu item."""
        item = self.get_menu_items()[self.active_menu][self.selected_index]

        if item == _("Quit"):
            self.app.running = False
        elif item == _("Update repositories"):
            if not self.is_pinned:
                self.app.run_update_repositories()
        elif item == _("Full system upgrade"):
            if not self.is_pinned:
                self.app.run_full_upgrade()
        elif item == _("Installed packages"):
            self.app.run_show_installed_packages()
        elif item == _("Check system"):
            self.app.run_check_system()
        elif item == self.cache_menu_label:
            if self.cache_enabled:
                self.app.run_clear_cache()
        elif item == _("Roll back"):
            if self.rollback_enabled and not self.is_pinned:
                self.app.run_rollback()
        elif item == _("View pinned state"):
            self.app.run_view_pinned_state()
        elif item == _("Documentation"):
            self.app.show_message(_("Info"), _("Opening luet documentation (URL TBD)"))
        elif item == _("About"):
            about_text = AboutInfo.get_ncurses_about_text()
            self.app.show_message(_("About"), about_text)

# --- Main Application Class ---
class LuetTUI:
    
    def __init__(self, stdscr):
        global _tui_app_instance
        _tui_app_instance = self  # Register for signal handler
        self.stdscr = stdscr
        self.running = True
        self.scheduler = Scheduler()
        self.menu = Menu(self)
        self.lock = threading.Lock()
        self.cache_lock = threading.Lock()

        self.status_message = _("Ready")
        self.is_error_status = False
        self.sync_info = _("Not Synced")
        
        self.spinner = Spinner()
        
        self.focus = 'list'
        self.search_query = ""
        self.search_cursor_pos = 0
        
        self.results = []
        self.selected_index = 0
        self.results_scroll_offset = 0
        self.log_lines = collections.deque(maxlen=2000)
        self.log_scroll = 0
        self.log_visible = False

        self.search_win = None
        self.results_win = None
        self.log_win = None
        self.visible_results_count = 0
        self.visible_log_height_lines = 0

        curses.curs_set(0)
        # Use a 30ms timeout instead of nodelay — getch() returns immediately
        # when input arrives, eliminating click latency, while still allowing
        # the spinner and scheduler to tick regularly when idle.
        self.stdscr.timeout(30)
        self.stdscr.keypad(True)
        curses.start_color()

        # Enable mouse: all button events
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

        # Menu bar hit regions: list of (start_x, end_x, menu_index)
        # Populated during draw() so they stay in sync with actual rendering
        self._menu_bar_regions = []

        # Results area geometry — updated each draw() for mouse hit-testing
        self._results_top_y = 0
        self._results_bottom_y = 0

        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK) # Error color

        elevation_cmd = self._get_elevation_cmd()
        self.command_runner = CommandRunner(elevation_cmd, self.scheduler.schedule)
        
        # FIX: Initialize cache as empty and populate asynchronously
        self.installed_packages_cache = {}
        self.cache_initialized = False  # Track cache initialization status
        self._index_ready = False

        # Description index for treefs-based description search
        self.desc_index = DescriptionIndex()

        self.init_app()

        # Show "Initializing..." so is_busy=True blocks all input until ready
        self.status_message = _("Initializing...")

        # Start async cache population
        Debug.log("TUI: starting cache refresh")
        self.refresh_installed_packages_cache_async()

        # Start building the description index in the background
        Debug.log("TUI: starting description index build")
        self.desc_index.build_async(self.command_runner.run_sync, on_ready_callback=self._on_index_ready)

    def cleanup(self):
        """Clean up resources before exit"""
        try:
            # Clear windows
            if self.search_win:
                self.search_win.clear()
                self.search_win = None
            if self.log_win:
                self.log_win.clear()
                self.log_win = None
            if self.results_win:
                self.results_win.clear()
                self.results_win = None
            
            # Restore cursor
            curses.curs_set(1)
        except Exception as e:
            print(f"Error during cleanup: {e}")

    def _get_elevation_cmd(self):
        if os.getuid() == 0:
            return None
        elif shutil.which("pkexec"):
            return ["pkexec"]
        elif shutil.which("sudo"):
            return ["sudo", "-n"]
        return None

    # ADDED: Async method to refresh cache
    def refresh_installed_packages_cache_async(self):
        """Refresh the cached list of installed packages asynchronously"""
        def worker():
            try:
                new_cache = PackageState.get_installed_packages(self.command_runner.run_sync)
                self.scheduler.schedule(self._on_cache_updated, new_cache)
            except Exception as e:
                print(f"Error refreshing installed packages cache: {e}")
                self.scheduler.schedule(self._on_cache_updated, {})
        
        threading.Thread(target=worker, daemon=True).start()

    def _on_cache_updated(self, new_cache):
        """Callback when cache update completes"""
        Debug.log("TUI: cache update complete")
        with self.cache_lock:
            self.installed_packages_cache = new_cache
            self.cache_initialized = True
        self._check_startup_complete()

    def _on_index_ready(self):
        """Called from background thread when description index is built."""
        Debug.log("TUI: index ready")
        self.scheduler.schedule(self._on_index_ready_main)

    def _on_index_ready_main(self):
        self._index_ready = True
        self._check_startup_complete()

    def _check_startup_complete(self):
        """Set status to Ready only once both cache and description index are ready."""
        if self.cache_initialized and self._index_ready:
            Debug.log("TUI: startup complete")
            self.set_status(_("Ready"))

    def refresh_installed_packages_cache(self):
        """Refresh the cached list of installed packages"""
        try:
            new_cache = PackageState.get_installed_packages(self.command_runner.run_sync)
            with self.cache_lock:
                self.installed_packages_cache = new_cache
                self.cache_initialized = True
        except Exception as e:
            print(f"Error refreshing installed packages cache: {e}")
            with self.cache_lock:
                self.installed_packages_cache = {}

    def init_app(self):
        si = SyncInfo.get_last_sync_time()
        self.sync_info = si.get("ago", _("repositories not synced"))
        self.update_cache_menu()
        self.update_rollback_menu()

    def update_cache_menu(self):
        """Update the cache menu item with current cache info."""
        self.menu.update_cache_menu_item()

    def update_rollback_menu(self):
        """Update the rollback/pinned menu item state."""
        self.menu.update_rollback_menu_item()

    def run_view_pinned_state(self):
        version = RollbackManager.get_current_desktop_version() or _("unknown")
        msg = _("System is pinned to a previous version.\n\n"
                "Desktop: {}\n\n"
                "Updates and rollbacks are disabled while pinned.").format(version)
        self.draw()
        h, w = self.stdscr.getmaxyx()
        lines = msg.splitlines()
        ww = min(80, w - 4)
        hh = min(len(lines) + 4, h - 4)
        y = max(1, (h - hh) // 2)
        x = max(1, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.border()
        win.nodelay(False)
        try:
            for i, ln in enumerate(lines[:hh - 4]):
                win.addstr(1 + i, 2, ln[:ww - 4])
            win.addstr(hh - 2, 2, _("Press 'u' to unpin, any other key to close.")[:ww - 4])
            win.refresh()
            ch = win.getch()
        except Exception:
            ch = -1
        finally:
            self.draw()

        if ch in (ord('u'), ord('U')):
            self._do_unpin_and_upgrade()

    def _do_unpin_and_upgrade(self):
        if not self.confirm_yes_no(_("Unpin the system?")):
            return
        self.set_status(_("Unpinning..."))
        self.draw()

        unpin_cmd = RollbackManager.unpin_references()
        cmd = ["sh", "-c", unpin_cmd]

        def on_log(line):
            self.append_to_log(line)

        def on_finish(returncode):
            if returncode == 0:
                self.set_status(_("Ready"))
                self.update_rollback_menu()
            else:
                self.set_status(_("Error during unpin"), error=True)

        self.command_runner.run_realtime(
            cmd,
            require_root=True,
            on_line_received=on_log,
            on_finished=on_finish
        )

    def append_to_log(self, text):
        if text is None: return
        with self.lock:
            for ln in str(text).splitlines():
                self.log_lines.append(ln)
            self.log_scroll = 0

    def set_status(self, msg, error=False):
        with self.lock:
            self.status_message = str(msg)
            self.is_error_status = error
            # --- MODIFIED: Automatically open the log on error ---
            if error and not self.log_visible:
                self.log_visible = True
                self.log_scroll = 0 # Ensure we are at the bottom of the log
            # --- END MODIFICATION ---

    def draw(self):
        curses.curs_set(0)
        
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        if h < 20 or w < 80:
            try:
                self.stdscr.addstr(0, 0, _("Terminal too small! Min 20x80."))
            except curses.error: pass
            self.stdscr.refresh()
            return
            
        is_ready = self.status_message == _("Ready")
        
        is_busy = not is_ready and not self.is_error_status
        
        dim_attr = curses.A_DIM if is_busy else curses.A_NORMAL
        header_attr = curses.A_DIM if is_busy else curses.A_BOLD

        try:
            x_pos = 0
            self._menu_bar_regions = []
            for i, (title, index) in enumerate(Menu.get_menu_titles()):
                seg = f"  {title}  "
                attr = curses.A_REVERSE
                if is_busy:
                    attr |= curses.A_DIM
                elif self.menu.is_open and index == self.menu.active_menu:
                    attr |= curses.A_NORMAL
                self.stdscr.addstr(0, x_pos, seg, attr)
                self._menu_bar_regions.append((x_pos, x_pos + len(seg), index))
                x_pos += len(seg)
                
            if x_pos < w:
                self.stdscr.addstr(0, x_pos, " " * (w - x_pos), curses.A_REVERSE)

            self.stdscr.move(1, 0)
            self.stdscr.clrtoeol()
            
            self.stdscr.move(2, 0)
            self.stdscr.clrtoeol()
            
            status_prefix = ""

            if is_busy:
                status_prefix = self.spinner.get_current_frame() + " "
                
            status_text = status_prefix + self.status_message
            status_x = max(0, (w - len(status_text)) // 2)
            
            status_attr = curses.A_NORMAL
            if self.is_error_status:
                status_attr = curses.color_pair(2)
            elif is_busy:
                status_attr = curses.A_DIM
            
            try:
                self.stdscr.addstr(2, status_x, status_text[:w - 1 - status_x], status_attr)
            except curses.error:
                pass # Ignore if color fails
            
            sync_text = _("Last sync: {}").format(self.sync_info)
            if len(sync_text) < w:
                self.stdscr.addstr(2, max(0, w - len(sync_text) - 1), sync_text, curses.A_DIM)

        except curses.error: pass
        
        search_y = 3
        search_h = 3
        results_y = search_y + search_h
        results_x = 0
        win_w = w
        footer_h = 1
        content_bottom = h - footer_h
        
        log_h = 0
        log_y = 0
        
        if self.log_visible:
            total_content_h = content_bottom - results_y
            results_h = max(6, int(total_content_h * 0.55))
            log_h = max(3, total_content_h - results_h)
            log_y = results_y + results_h
        else:
            log_bar_h = 1
            results_h = max(6, content_bottom - results_y - log_bar_h)
            log_y = results_y + results_h
        
        self.visible_results_count = max(0, results_h - 3)
        self.visible_log_height_lines = max(0, log_h - 2)

        # Track results area rows for mouse hit-testing (row 0=border, 1=header, 2+=data)
        self._results_top_y = results_y + 2
        self._results_bottom_y = results_y + 2 + self.visible_results_count - 1

        try:
            if self.search_win is None:
                self.search_win = curses.newwin(search_h, win_w, search_y, results_x)
            else:
                self.search_win.resize(search_h, win_w)
                self.search_win.mvwin(search_y, results_x)

            self.search_win.erase()
            
            base_attr = curses.A_DIM if is_busy else curses.A_BOLD
            
            self.search_win.attrset(base_attr)
            self.search_win.border()
            self.search_win.attrset(curses.A_NORMAL)

            self.search_win.addstr(0, 2, " " + _("Search") + " ", base_attr)

            input_y = 1
            input_x = 1
            max_input_len = win_w - 2

            display_query = self.search_query
            cursor_pos_in_win = 0
            
            self.search_win.addstr(input_y, input_x, " " * max_input_len, base_attr)
            
            if not display_query:
                placeholder_text = _("Enter package name")
                attr = curses.A_DIM
                self.search_win.addstr(input_y, input_x, placeholder_text[:max_input_len], attr)
            else:
                if self.search_cursor_pos >= max_input_len:
                    scroll_offset = self.search_cursor_pos - max_input_len + 1
                else:
                    scroll_offset = 0

                display_query = self.search_query[scroll_offset : scroll_offset + max_input_len]
                
                self.search_win.addstr(input_y, input_x, display_query.ljust(max_input_len), base_attr)
                
                cursor_pos_in_win = self.search_cursor_pos - scroll_offset
                
            if self.focus == 'search' and not is_busy:
                cursor_y = input_y
                cursor_x = input_x + min(cursor_pos_in_win, max_input_len)
                
                curses.curs_set(1)
                self.search_win.move(cursor_y, cursor_x)
            else:
                curses.curs_set(0)
                if is_busy:
                    self.focus = 'list'

        except curses.error: pass

        try:
            if self.results_win is None:
                self.results_win = curses.newwin(results_h, win_w, results_y, results_x)
            else:
                self.results_win.resize(results_h, win_w)
                self.results_win.mvwin(results_y, results_x)
            
            self.results_win.erase()
            
            self.results_win.attrset(dim_attr)
            self.results_win.border()
            self.results_win.attrset(curses.A_NORMAL)
            
            self.results_win.addstr(0, 2, " " + _(" Results ") + " ", dim_attr)
            
            # Updated header with upgrade symbol column
            header = f"{_('Category'):16.16} {_('Name'):28.28} {'':2} {_('Version'):16.16} {_('Repository'):20.20} {_('Action'):8}"
            self.results_win.addstr(1, 1, header[:win_w-2], header_attr)

            if self.selected_index < self.results_scroll_offset:
                self.results_scroll_offset = self.selected_index
            elif self.selected_index >= self.results_scroll_offset + self.visible_results_count:
                self.results_scroll_offset = self.selected_index - self.visible_results_count + 1
            
            for idx in range(self.visible_results_count):
                row_idx = idx + self.results_scroll_offset
                y_in_win = 2 + idx
                if row_idx >= len(self.results):
                    continue
                
                pkg = self.results[row_idx]
                upgrade_symbol = pkg.get("upgrade_symbol", "")
                
                # Determine action based on installed and upgradeable status
                if pkg.get("protected", False):
                    action = _("Protected")
                elif pkg.get("installed", False) or upgrade_symbol == "↑":
                    action = _("Remove")
                else:
                    action = _("Install")
                
                # FIXED: Use the correct version field from processed data
                version_to_display = pkg.get("version", "")
                
                # Updated line format with upgrade symbol
                line = f"{pkg.get('category','')[:16]:16} {pkg.get('name','')[:28]:28} {upgrade_symbol:2} {version_to_display[:16]:16} {pkg.get('repository','')[:20]:20} {action:8}"
                
                attr = dim_attr
                if not is_busy and self.focus == 'list' and row_idx == self.selected_index:
                    attr = curses.A_REVERSE
                
                self.results_win.addstr(y_in_win, 1, line[:win_w-2], attr)

        except curses.error: pass
        
        try:
            if self.log_visible:
                if self.log_win is None:
                    self.log_win = curses.newwin(log_h, win_w, log_y, results_x)
                else:
                    self.log_win.resize(log_h, win_w)
                    self.log_win.mvwin(log_y, results_x)
                
                self.log_win.erase()
                self.log_win.border()
                
                base_header = _("Toggle output log")
                header_text = base_header
                if self.log_scroll > 0:
                    plural_msg = ngettext("Scrolled Up: {} line.", "Scrolled Up: {} lines.", self.log_scroll).format(self.log_scroll)
                    header_text = _("{} ({} PgUp/PgDn to navigate)").format(base_header, plural_msg)
                self.log_win.addstr(0, 2, header_text[:win_w-4])

                if self.log_scroll == 0:
                    start_idx = max(0, len(self.log_lines) - self.visible_log_height_lines)
                else:
                    start_idx = len(self.log_lines) - (self.log_scroll + self.visible_log_height_lines)
                    start_idx = max(0, start_idx)

                visible_log = list(self.log_lines)[start_idx : start_idx + self.visible_log_height_lines]
                
                for i in range(self.visible_log_height_lines):
                    y_in_win = 1 + i
                    ln = visible_log[i] if i < len(visible_log) else ""
                    self.log_win.addstr(y_in_win, 1, ln[:win_w-2])
            
            else:
                if self.log_win:
                    self.log_win.clear()
                    self.log_win.noutrefresh()
                
                indicator_text = _("Toggle output log") + " (Press 'l' to expand)"
                self.stdscr.addstr(log_y, 0, "─" * (w - 1))
                self.stdscr.addstr(log_y, 2, indicator_text[:w-4], curses.A_DIM)

        except curses.error: pass
        
        try:
            footer = ""
            if self.focus == 'list':
                footer = _("Keys: F9=menu | s=search | Enter=details | i=install/uninstall | l=toggle log | PgUp/PgDn=log scroll | q=quit")
            elif self.focus == 'search':
                footer = _("SEARCH: Enter=submit | Esc/Down/Tab=back to list | Backspace/Del/Arrows=edit | q=quit")
            self.stdscr.addstr(h - 1, 0, footer.ljust(w-1)[:w-1], curses.A_DIM)
        except curses.error: pass
        
        try:
            self.stdscr.noutrefresh()
            
            if self.results_win:
                self.results_win.noutrefresh()
            if self.log_visible and self.log_win:
                self.log_win.noutrefresh()

            if self.search_win:
                self.search_win.noutrefresh()

            if self.menu.is_open:
                curses.curs_set(0)
                self.menu.draw(self.stdscr)

            curses.doupdate()
        except curses.error: pass

    def prompt_string(self, prompt, initial=""):
        curses.echo()
        curses.curs_set(1)
        h, w = self.stdscr.getmaxyx()
        win_w = max(40, min(w - 4, len(prompt) + 50))
        win = curses.newwin(3, win_w, 2, 2)
        win.border()
        
        s = None
        try:
            win.addstr(1, 1, prompt + ": ")
            win.refresh()
            s = win.getstr(1, len(prompt) + 3, 300)
            if s is not None:
                s = s.decode(errors="ignore")
        except Exception:
            pass
        finally:
            curses.noecho()
            curses.curs_set(0)
            self.draw()
        return s
    
    def confirm_yes_no(self, message):
        self.draw()
        h, w = self.stdscr.getmaxyx()
        lines = str(message).splitlines()
        ww = min(80, w - 4)
        hh = min(len(lines) + 4, h - 4)
        y = max(1, (h - hh) // 2)
        x = max(1, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.border()
        ch = -1
        try:
            for i, ln in enumerate(lines[:hh - 4]):
                win.addstr(1 + i, 2, ln[:ww - 4])
            win.addstr(hh - 2, 2, _("Press 'y' to confirm, any other key to cancel.")[:ww - 4])
            win.refresh()
            win.nodelay(False)
            ch = win.getch()
        except Exception:
            traceback.print_exc()
        finally:
            self.draw()
        return ch in (ord('y'), ord('Y'))

    def show_message(self, title, message, pause=True):
        self.draw()
        h, w = self.stdscr.getmaxyx()
        ww = min(100, w - 6); hh = min(15, h - 6)
        y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.border()
        try:
            win.addstr(0, 2, f" {title} ")
            lines = str(message).splitlines()
            for i, ln in enumerate(lines[: hh - 4]):
                win.addstr(2 + i, 2, ln[: ww - 4])
            win.addstr(hh - 2, 2, _("Press any key to continue"))
            win.refresh()
            if pause:
                win.getch()
        except Exception:
            pass
        finally:
            self.draw()
            
    def run_update_repositories(self):
            # 1. Start the operation: Set status, open log and activate spinner
            self.log_visible = True
            self.log_scroll = 0
            self.set_status(_("Updating repositories..."))
            self.draw()
            
            def on_log(line): self.append_to_log(line)
            
            # 2. On Success: Set final informational status and refresh data
            def on_success(): 
                self.set_status(_("Repositories updated"))
                self.init_app() # This updates self.sync_info
                
            # 3. On Error: Set error status
            def on_error(): 
                self.set_status(_("Error updating repositories"), error=True)

            # 4. On Finish: STOP the spinner and set final status to 'Ready'
            def on_finish(cookie):
                # self.running_op = False # This flag is not used by draw()
                
                # Use scheduler to set status in the *next* cycle
                # This ensures the user sees the on_success/on_error message first.
                if not self.is_error_status:
                    self.scheduler.schedule(self.set_status, _("Ready"))
                
            # 5. Start the thread
            threading.Thread(
                target=lambda: RepositoryUpdater.run_repo_update(
                    self.command_runner.run_realtime,
                    lambda state, reason: 0,
                    lambda l: self.scheduler.schedule(on_log, l),
                    lambda: self.scheduler.schedule(on_success),
                    lambda: self.scheduler.schedule(on_error),
                    lambda c: self.scheduler.schedule(on_finish, c),
                    self.scheduler.schedule,
                ),
                daemon=True
            ).start()

    def run_full_upgrade(self):
        if not self.confirm_yes_no(_("Perform a full system upgrade?")): return
        
        self.log_visible = True
        self.log_scroll = 0
        self.set_status(_("Performing full system upgrade..."))
        self.append_to_log(_("Full system upgrade initiated."))
        self.draw()

        def on_log(line): self.append_to_log(line)
        def on_status(msg): self.set_status(msg)
        
        def on_finish(returncode, message):
            if returncode == 0:
                self.set_status(_("Finalizing: Updating package cache..."))

                # Refresh the cache asynchronously so the UI stays responsive.
                # on_cache_ready is the single place that finalizes status so
                # "Ready" is set exactly once, after everything is done.
                def on_cache_ready(new_cache):
                    with self.cache_lock:
                        self.installed_packages_cache = new_cache
                        self.cache_initialized = True

                    if self.search_query == _("installed"):
                        self.run_show_installed_packages()
                    elif self.search_query:
                        self.run_search(self.search_query)

                    if not self.is_error_status:
                        self.set_status(_("Ready"))

                def cache_worker():
                    try:
                        new_cache = PackageState.get_installed_packages(self.command_runner.run_sync)
                    except Exception as e:
                        print(f"Error refreshing cache after upgrade: {e}")
                        new_cache = {}
                    self.scheduler.schedule(on_cache_ready, new_cache)

                threading.Thread(target=cache_worker, daemon=True).start()
            else:
                final_msg = message if returncode != 0 else _("Error during system upgrade")
                self.set_status(final_msg, error=True)

        def on_post_action():
            PackageOperations._run_kbuildsycoca6()
            self.init_app()

        def start_worker():
            try:
                upgrader = SystemUpgrader(
                    command_runner_realtime=self.command_runner.run_realtime,
                    log_callback=lambda l: self.scheduler.schedule(on_log, l),
                    status_callback=lambda s: self.scheduler.schedule(on_status, s),
                    schedule_callback=self.scheduler.schedule,
                    post_action_callback=lambda: self.scheduler.schedule(on_post_action),
                    on_finish_callback=on_finish,
                    inhibit_cookie=0,
                    translation_func=_
                )
                upgrader.start_upgrade()
            except Exception as e:
                self.scheduler.schedule(on_log, _("Upgrade initiation failed: {}").format(e))
                self.scheduler.schedule(on_finish, -1, _("Upgrade failed to start"))

        threading.Thread(target=start_worker, daemon=True).start()

    def run_check_system(self):
        self.set_status(_("Checking system for missing files..."))
        self.draw()
        def on_log(line):
            self.scheduler.schedule(self.append_to_log, line)
        def on_exit_status(msg):
            # This is the final status callback from the thread
            self.scheduler.schedule(self.set_status, msg)
            if msg != _("Ready"):
                # If an error occurred, schedule "Ready" for the next cycle
                self.scheduler.schedule(self.set_status, _("Ready"))
        def on_reinstall_start():
            self.scheduler.schedule(self.append_to_log, _("\n--- Missing packages found. Starting repair sequence. ---"))
        def on_reinstall_status(status):
            self.scheduler.schedule(self.set_status, status)
        def on_reinstall_finish(success):
            msg = _("System repair finished successfully.") if success else _("Could not repair some packages")
            self.scheduler.schedule(self.append_to_log, msg)
            if not success:
                 self.scheduler.schedule(self.set_status, msg, error=True)
        SystemChecker.run_check_system(
            self.command_runner.run_sync,
            on_log,
            on_exit_status,
            on_reinstall_start,
            on_reinstall_status,
            on_reinstall_finish,
            time.sleep,
            _
        )
        
    def run_clear_cache(self):
        if not self.menu.cache_enabled:
            self.show_message(_("Info"), _("No cache to clear"))
            return

        if not self.confirm_yes_no(_("Clear Luet cache?")):
            return
        self.set_status(_("Clearing Luet cache..."))
        self.append_to_log(_("--- Running 'luet cleanup' ---"))
        self.draw()
        def on_log(line): self.append_to_log(line)
        def on_done(returncode):
            if returncode == 0:
                self.append_to_log(_("Luet cache cleanup finished successfully."))
                self.set_status(_("Ready"))
                self.update_cache_menu()
            else:
                self.append_to_log(_("Luet cache cleanup finished with errors."))
                self.set_status(_("Error clearing Luet cache"), error=True)
        CacheCleaner.run_cleanup_core(
            self.command_runner.run_realtime,
            lambda ln: self.scheduler.schedule(on_log, ln),
            lambda rc: self.scheduler.schedule(on_done, rc)
        )

    def _select_rollback_candidate(self, candidates):
        """Show a curses selection menu for rollback candidates. Returns selected dict or None."""
        self.draw()
        h, w = self.stdscr.getmaxyx()
        ww = min(80, w - 4)
        hh = min(len(candidates) + 7, h - 4)
        y = max(1, (h - hh) // 2)
        x = max(1, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.keypad(True)
        win.nodelay(False)

        selected = 0
        while True:
            win.erase()
            win.border()
            try:
                win.addstr(0, 2, f" {_('Select rollback target')} ")
                win.addstr(1, 2, _("System will be pinned after rollback — upgrades disabled until unpinned.")[:ww - 4], curses.A_DIM)
                for i, c in enumerate(candidates):
                    label = f"  {c.get('label',''):<18} {c.get('date','')}  {c.get('desktop','')}"
                    attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
                    win.addstr(3 + i, 1, label[:ww - 2], attr)
                win.addstr(hh - 2, 2, _("↑↓ select   Enter confirm   Esc cancel")[:ww - 4])
            except Exception:
                pass
            win.refresh()

            ch = win.getch()
            if ch in (curses.KEY_UP,):
                selected = max(0, selected - 1)
            elif ch in (curses.KEY_DOWN,):
                selected = min(len(candidates) - 1, selected + 1)
            elif ch in (10, 13):
                self.draw()
                return candidates[selected]
            elif ch in (27,):
                self.draw()
                return None

    def run_rollback(self):
        if not self.menu.rollback_enabled:
            self.show_message(_("Info"), _("Roll back is only available on stable repositories."))
            return

        self.set_status(_("Checking rollback availability..."))
        self.draw()

        def _prepare():
            current = RollbackManager.get_current_desktop_version()
            if not current:
                self.scheduler.schedule(self.set_status, _("Ready"))
                self.scheduler.schedule(
                    self.show_message,
                    _("Error"), _("Cannot determine current desktop version.")
                )
                return

            candidates = RollbackManager.get_rollback_candidates(current)
            if not candidates:
                self.scheduler.schedule(self.set_status, _("Ready"))
                self.scheduler.schedule(
                    self.show_message,
                    _("Info"), _("No previous version available to roll back to.")
                )
                return

            self.scheduler.schedule(_do_confirm, candidates)

        def _do_confirm(candidates):
            # Show selection menu
            previous = self._select_rollback_candidate(candidates)
            if not previous:
                self.set_status(_("Ready"))
                return

            msg = _("Roll back to {}?\n\n"
                    "  Desktop:   {}\n"
                    "  Community: {}\n\n"
                    "All packages will be downgraded to this snapshot.\n"
                    "The system will be pinned — updates and repository syncs\n"
                    "will be disabled until you unpin via the Roll back menu.").format(
                previous.get("label", ""),
                previous.get("desktop", ""),
                previous.get("community", "")
            )

            if not self.confirm_yes_no(msg):
                self.set_status(_("Ready"))
                return

            self.set_status(_("Rolling back..."))
            self.draw()

            def on_log(line):
                self.scheduler.schedule(self.append_to_log, line)

            def on_finish(returncode, message):
                def _cb():
                    if returncode == 0:
                        self.set_status(_("Rollback completed successfully"))
                        self.refresh_installed_packages_cache()
                        if self.search_query == _("installed"):
                            self.run_show_installed_packages()
                        elif self.search_query:
                            self.run_search(self.search_query)
                        self.update_rollback_menu()
                        if not self.is_error_status:
                            self.set_status(_("Ready"))
                    else:
                        self.set_status(message, error=True)
                self.scheduler.schedule(_cb)

            def _start_rollback():
                RollbackManager.run_rollback(
                    previous_snapshot=previous,
                    command_runner_realtime=self.command_runner.run_realtime,
                    command_runner_sync=self.command_runner.run_sync,
                    log_callback=on_log,
                    on_finish_callback=on_finish,
                    schedule_callback=self.scheduler.schedule
                )

            threading.Thread(target=_start_rollback, daemon=True).start()

        threading.Thread(target=_prepare, daemon=True).start()
        
    def run_show_installed_packages(self):
        """Show all installed packages as results using the cache — no luet call needed."""
        with self.cache_lock:
            installed = dict(self.installed_packages_cache)

        packages = []
        for key, version in installed.items():
            if '/' not in key:
                continue
            cat, name = key.split('/', 1)
            if PackageFilter.is_package_hidden(cat, name):
                continue
            repo = ""
            if self.desc_index.is_ready:
                indexed = self.desc_index._index.get(key)
                if indexed:
                    repo = indexed.get("repository", "")
            packages.append({
                "category": cat,
                "name": name,
                "version": version,
                "repository": repo,
                "is_actually_installed": True,
                "protected": PackageFilter.is_package_protected(cat, name),
                "upgrade_symbol": "",
            })

        packages.sort(key=lambda p: (p["category"], p["name"]))
        self.search_query = _("installed")
        self.on_search_finished({"packages": packages})

    def run_search(self, query):
        self.set_status(_("Searching for {}...").format(query))
        self.draw()
        
        # FIX: Validate input instead of escaping
        # Remove any null bytes and control characters that could cause issues
        sanitized_query = query.replace('\0', '').replace('\n', '').replace('\r', '')
        
        # Optional: limit length to prevent abuse
        if len(sanitized_query) > 256:
            sanitized_query = sanitized_query[:256]
        
        if not sanitized_query.strip():
            self.scheduler.schedule(self.set_status, _("Invalid search query"), True)
            return

        def worker():
            try:
                # Use cached installed packages
                with self.cache_lock:
                    if not self.cache_initialized:
                        installed_packages_dict = PackageState.get_installed_packages(self.command_runner.run_sync)
                    else:
                        installed_packages_dict = self.installed_packages_cache

                # Name-based search via luet
                search_cmd = ["luet", "search", "-o", "json", "-q", sanitized_query]
                result = PackageSearcher.run_search_core(self.command_runner.run_sync, search_cmd)
                result = SearchProcessor.process_search_results(result, installed_packages_dict)

                # Merge description matches from local treefs index.
                # Block until the index is ready, or at most 2 seconds.
                if not self.desc_index.is_ready:
                    self.desc_index._ready_event.wait(timeout=2.0)

                if self.desc_index.is_ready and "error" not in result:
                    existing_keys = {
                        f"{p.get('category', '')}/{p.get('name', '')}"
                        for p in result.get("packages", [])
                    }
                    for pkg in self.desc_index.search(sanitized_query):
                        key = f"{pkg['category']}/{pkg['name']}"
                        if key in existing_keys:
                            continue
                        if PackageFilter.is_package_hidden(pkg["category"], pkg["name"]):
                            continue
                        enriched = SearchProcessor._enrich_package_info(dict(pkg), installed_packages_dict)
                        result["packages"].append(enriched)

                self.scheduler.schedule(self.on_search_finished, result)
            except Exception as e:
                self.scheduler.schedule(self.append_to_log, _("Search core error: {}").format(e))
                self.scheduler.schedule(self.set_status, _("Error executing the search command"), error=True)
        threading.Thread(target=worker, daemon=True).start()

    def on_search_finished(self, result):
        try:
            if "error" in result:
                self.set_status(result["error"], error=True)
                return
            
            self.results = []
            for pkg in result.get("packages", []):
                # FIX: Use the processed package data from SearchProcessor
                # This includes the upgrade_symbol field
                self.results.append({
                    "category": pkg.get("category", ""),
                    "name": pkg.get("name", ""),
                    "version": pkg.get("version", ""),  # Use the version field from processed data
                    "available_version": pkg.get("version", ""),  # Available version is the same
                    "repository": pkg.get("repository", ""),
                    "installed": pkg.get('is_actually_installed', False),
                    "protected": pkg.get('protected', False),
                    "upgrade_symbol": pkg.get('upgrade_symbol', '')  # This should now contain "↑"
                })
            self.selected_index = 0
            self.results_scroll_offset = 0
            
            self.set_status(_("Found {} results matching '{}'").format(len(self.results), self.search_query))
            self.scheduler.schedule(self.set_status, _("Ready"))
        
        except Exception as e:
            self.results = []
            self.selected_index = 0
            self.results_scroll_offset = 0
            self.append_to_log(_("Search result processing failed: {}").format(e))
            self.set_status(_("Error executing the search command"), error=True)

    def do_install_uninstall_selected(self):
        if not (0 <= self.selected_index < len(self.results)): return
        pkg = self.results[self.selected_index]
        full_name = f"{pkg['category']}/{pkg['name']}"
        installed = pkg.get("installed", False)
        protected = pkg.get("protected", False)
        
        if protected:
            msg = PackageFilter.get_protection_message(pkg['category'], pkg['name'])
            if msg is None:
                msg = _("This package ({}) is protected and can't be removed.").format(full_name)
            self.show_message(_("Protected"), msg)
            return
        
        # Define the callback that runs on the main thread after cache refresh
        def on_refresh_complete(new_cache):
            self.installed_packages_cache = new_cache
            self.cache_initialized = True
            
            self.set_status(_("Ready"))
            if self.search_query == _("installed"):
                self.run_show_installed_packages()
            elif self.search_query:
                self.run_search(self.search_query)

        if installed:
            # Package shows "Remove" in Action column - so uninstall it
            if not self.confirm_yes_no(_("Do you want to uninstall {}?").format(full_name)): 
                return
                
            self.set_status(_("Uninstalling {}...").format(full_name))
            self.draw()
            
            def on_log(line): self.append_to_log(line)
            
            def on_done(returncode):
                if returncode == 0:
                    self.set_status(_("Finalizing: Updating package cache..."))
                    PackageOperations.run_post_transaction_refresh(
                        self.command_runner.run_sync,
                        self.scheduler.schedule,
                        on_refresh_complete
                    )
                else:
                    error_msg = _("Error uninstalling: '{}'").format(full_name)
                    self.set_status(error_msg, error=True)
                    
            # Use the new method with automatic fallback
            PackageOperations.run_uninstallation_with_fallback(
                self.command_runner.run_realtime,
                lambda ln: self.scheduler.schedule(on_log, ln),
                lambda rc: self.scheduler.schedule(on_done, rc),
                pkg['category'],
                full_name
            )
        else:
            # Package shows "Install" in Action column - so install it
            if not self.confirm_yes_no(_("Do you want to install {}?").format(full_name)): return
            
            cmd = PackageOperations.build_install_command(full_name)

            self.set_status(_("Installing {}...").format(full_name))
            self.draw()
            
            def on_log(line): self.append_to_log(line)
            
            def on_done(returncode):
                if returncode == 0:
                    self.set_status(_("Finalizing: Updating package cache..."))
                    PackageOperations.run_post_transaction_refresh(
                        self.command_runner.run_sync,
                        self.scheduler.schedule,
                        on_refresh_complete
                    )
                else:
                    error_msg = _("Error installing: '{}'").format(full_name)
                    self.set_status(error_msg, error=True)
                    
            PackageOperations.run_installation(
                self.command_runner.run_realtime,
                lambda ln: self.scheduler.schedule(on_log, ln),
                lambda rc: self.scheduler.schedule(on_done, rc),
                cmd
            )

    def show_details(self):
        if not (0 <= self.selected_index < len(self.results)):
            return

        pkg = self.results[self.selected_index]

        category = pkg['category']
        name = pkg['name']
        repository = pkg.get('repository', '')
        installed = pkg.get('installed', False)

        version_to_use = (
            pkg.get('available_version', '')
            or pkg.get('version', '')
        )

        details = PackageDetails.get_definition_yaml(
            self.command_runner.run_sync,
            repository,
            category,
            name,
            version_to_use
        )

        display_version = (
            pkg.get('available_version', '')
            or pkg.get('version', '')
        )

        text = PackageDetails.format_for_tui(
            details,
            None,
            None,
            repository,
            display_version,
            installed
        )

        # Add protection notice to details instead of blocking details
        if pkg.get("protected", False):
            msg = PackageFilter.get_protection_message(category, name)
            if msg is None:
                full_name = f"{category}/{name}"
                msg = _("This package ({}) is protected and can't be removed.").format(full_name)

            text += "\n\n" + _("Protection status:") + "\n" + msg

        title = _("Details for {}/{}").format(category, name)

        self.show_package_details_interactive(
            title,
            text,
            category,
            name,
            installed,
            pkg
        )
 
    def show_package_details_interactive(self, title, message, category, name, installed, pkg):
        """Show package details with option to view files ('f') or revdeps ('r')."""
        self.draw()
        h, w = self.stdscr.getmaxyx()
        ww = min(100, w - 6); hh = min(15, h - 6)
        y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
        win = curses.newwin(hh, ww, y, x)
        win.keypad(True)
        win.nodelay(False)
        
        scroll_offset = 0
        lines = str(message).splitlines()
        max_visible = hh - 4
        max_scroll = max(0, len(lines) - max_visible)
        action_requested = False

        # Pre-check revdeps for installed packages to decide if Remove is allowed
        has_revdeps = False
        if installed and not pkg.get("protected", False):
            revdeps = PackageDetails.get_required_by(self.command_runner.run_sync, category, name)
            has_revdeps = len(revdeps) > 0

        while True:
            win.erase()
            win.border()
            try:
                win.addstr(0, 2, f" {title} ")
                
                visible_lines = lines[scroll_offset:scroll_offset + max_visible]
                for i, ln in enumerate(visible_lines):
                    win.addstr(2 + i, 2, ln[: ww - 4])
                
                # Show scroll indicator if needed
                if max_scroll > 0:
                    scroll_info = _("Line {}/{}").format(scroll_offset + 1, len(lines))
                    win.addstr(hh - 2, 2, scroll_info, curses.A_DIM)
                
                hints_parts = ["Keys:"]
                hints_parts.append("f=files")
                if installed:
                    hints_parts.append("r=required by")
                if not pkg.get("protected", False):
                    if has_revdeps and installed:
                        hints_parts.append(_("remove disabled (has revdeps)"))
                    else:
                        hints_parts.append("i={}".format(_("remove") if installed else _("install")))
                hints_parts.append("Up/Down/PgUp/PgDn=scroll")
                hints_parts.append("Any other key=close")

                hints = " | ".join(hints_parts)
                hint_x = max(2, ww - len(hints) - 2)
                win.addstr(hh - 2, hint_x, hints[:ww - hint_x - 2], curses.A_DIM)

                win.refresh()

                ch = win.getch()

                if ch in (ord('f'), ord('F')):
                    self.show_package_files(category, name)
                elif installed and ch in (ord('r'), ord('R')):
                    self.show_package_revdeps(category, name)
                elif ch in (ord('i'), ord('I')) and not pkg.get("protected", False) and not has_revdeps:
                    action_requested = True
                    break
                elif ch == curses.KEY_UP:
                    scroll_offset = max(0, scroll_offset - 1)
                elif ch == curses.KEY_DOWN:
                    scroll_offset = min(max_scroll, scroll_offset + 1)
                elif ch == curses.KEY_PPAGE:
                    scroll_offset = max(0, scroll_offset - max_visible)
                elif ch == curses.KEY_NPAGE:
                    scroll_offset = min(max_scroll, scroll_offset + max_visible)
                else:
                    break
                    
            except curses.error:
                break

        self.draw()

        if action_requested:
            self.perform_action(pkg)

    def perform_action(self, pkg):
        """Trigger install or remove for a package, as if selected from the results list."""
        for i, r in enumerate(self.results):
            if r['category'] == pkg['category'] and r['name'] == pkg['name']:
                self.selected_index = i
                self.do_install_uninstall_selected()
                return
        self.results.append(pkg)
        self.selected_index = len(self.results) - 1
        self.do_install_uninstall_selected()

    def show_package_revdeps(self, category, name):
            """Show a scrollable list of packages that require this package (revdeps) with spinner animation."""
            self.draw()
            h, w = self.stdscr.getmaxyx()
            ww = min(100, w - 6); hh = min(20, h - 6)
            y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
            win = curses.newwin(hh, ww, y, x)
            win.keypad(True)
            win.nodelay(True) # Non-blocking input for animation loop
            
            # --- Dependency Fetching Thread Setup ---
            def fetcher(q):
                # Blocking call runs here
                revdeps = PackageDetails.get_required_by(self.command_runner.run_sync, category, name)
                q.put(revdeps)

            q = queue.Queue()
            thread = threading.Thread(target=fetcher, args=(q,))
            thread.daemon = True
            thread.start()
            
            # --- Animation Loop ---
            title = _("Required by for {}/{}").format(category, name)
            loading_msg = _("Loading required by list...")
            
            while thread.is_alive():
                win.erase()
                win.border()
                
                win.addstr(0, 2, f" {title} "[:ww - 4])
                
                # Advance spinner and get the new frame
                spinner_frame = self.spinner.advance()
                
                display_msg = f"{spinner_frame} {loading_msg}"
                
                win.addstr(2, 2, display_msg, curses.A_DIM) 
                win.refresh()
                
                time.sleep(0.1)

                win.getch() # Keep TUI responsive
            
            # Get results from the thread
            try:
                revdeps = q.get(timeout=0.1)
            except queue.Empty:
                revdeps = []
            
            win.nodelay(False) # Blocking input for scrollable list

            if not revdeps:
                win.erase()
                win.border()
                win.addstr(0, 2, f" {title} ")
                no_revdeps_msg = _("This package is not required by any other package.")
                win.addstr(2, 2, no_revdeps_msg)
                hints = _("Press any key to close")
                win.addstr(hh - 2, 2, hints, curses.A_DIM)
                win.refresh()
                win.getch()
                self.draw()
                return
            
            # Format the revdeps list (e.g., add "- " prefix)
            display_list = ["- " + r for r in revdeps]
            
            # Display scrollable revdeps list
            scroll_offset = 0
            max_visible = hh - 4
            max_scroll = max(0, len(display_list) - max_visible)
            
            while True:
                win.erase()
                win.border()
                try:
                    title_full = _("Required by for {}/{} ({} packages)").format(category, name, len(display_list))
                    win.addstr(0, 2, f" {title_full} "[:ww - 4])
                    
                    visible_revdeps = display_list[scroll_offset:scroll_offset + max_visible]
                    for i, pkg_name in enumerate(visible_revdeps):
                        win.addstr(2 + i, 2, pkg_name[: ww - 4])
                    
                    # Show scroll indicator
                    if max_scroll > 0:
                        scroll_info = _("Package {}/{} | Page {}/{}").format(
                            scroll_offset + 1,
                            len(display_list),
                            (scroll_offset // max_visible) + 1,
                            (len(display_list) + max_visible - 1) // max_visible
                        )
                        win.addstr(hh - 2, 2, scroll_info, curses.A_DIM)
                    
                    # Show key hints
                    hints = _("Keys: Up/Down/PgUp/PgDn=scroll | Any other key=close")
                    hint_x = max(2, ww - len(hints) - 2)
                    win.addstr(hh - 2, hint_x, hints[:ww - hint_x - 2], curses.A_DIM)
                    
                    win.refresh()
                    
                    ch = win.getch()
                    
                    if ch == curses.KEY_UP:
                        scroll_offset = max(0, scroll_offset - 1)
                    elif ch == curses.KEY_DOWN:
                        scroll_offset = min(max_scroll, scroll_offset + 1)
                    elif ch == curses.KEY_PPAGE:
                        scroll_offset = max(0, scroll_offset - max_visible)
                    elif ch == curses.KEY_NPAGE:
                        scroll_offset = min(max_scroll, scroll_offset + max_visible)
                    elif ch == curses.KEY_HOME:
                        scroll_offset = 0
                    elif ch == curses.KEY_END:
                        scroll_offset = max_scroll
                    else:
                        break
                        
                except curses.error:
                    break
            
            self.draw()

    def show_package_files(self, category, name):
            """Show a scrollable list of files for the package."""
            self.draw()
            h, w = self.stdscr.getmaxyx()
            ww = min(100, w - 6); hh = min(20, h - 6)
            y = max(2, (h - hh) // 2); x = max(2, (w - ww) // 2)
            win = curses.newwin(hh, ww, y, x)
            win.keypad(True)
            # Set nodelay to true while loading for the animation loop
            # It will be set back to False later if files are found
            win.nodelay(True) 
            
            # --- File Fetching Thread Setup ---
            # A simple function to run in the background thread
            def fetcher(q):
                # Blocking call runs here
                files = PackageDetails.get_files(self.command_runner.run_sync, category, name)
                q.put(files)

            # Start the background operation
            q = queue.Queue()
            thread = threading.Thread(target=fetcher, args=(q,))
            thread.daemon = True
            thread.start()
            
            # --- Animation Loop ---
            title = _("Files for {}/{}").format(category, name)
            loading_msg = _("Loading file list...")
            
            # Run loop while the thread is still working
            while thread.is_alive():
                win.erase()
                win.border()
                
                # Redraw title and advance spinner frame
                win.addstr(0, 2, f" {title} "[:ww - 4])
                
                # Advance spinner and get the new frame
                spinner_frame = self.spinner.advance()
                
                display_msg = f"{spinner_frame} {loading_msg}"
                
                # Display dimmed loading message
                win.addstr(2, 2, display_msg, curses.A_DIM) 
                win.refresh()
                
                # Control animation speed (0.1s delay between frames)
                time.sleep(0.1)

                # Check for input, but don't process it (just keeps the TUI responsive)
                win.getch() 
            
            # Get results from the thread (should be available now)
            try:
                files = q.get(timeout=0.1)
            except queue.Empty:
                files = None # Should not happen if thread is joined, but safe
            
            # Set nodelay back to False for scrollable list input
            win.nodelay(False)
            # --- End Animation Loop ---

            if not files:
                win.erase()
                win.border()
                win.addstr(0, 2, f" {title} ")
                no_files_msg = _("No files found or package not installed")
                win.addstr(2, 2, no_files_msg)
                hints = _("Press any key to close")
                win.addstr(hh - 2, 2, hints, curses.A_DIM)
                win.refresh()
                win.getch()
                self.draw()
                return
            
            # Display scrollable file list
            scroll_offset = 0
            max_visible = hh - 4
            max_scroll = max(0, len(files) - max_visible)
            
            while True:
                win.erase()
                win.border()
                try:
                    title = _("Files for {}/{} ({} files)").format(category, name, len(files))
                    win.addstr(0, 2, f" {title} "[:ww - 4])
                    
                    visible_files = files[scroll_offset:scroll_offset + max_visible]
                    for i, file_path in enumerate(visible_files):
                        win.addstr(2 + i, 2, file_path[: ww - 4])
                    
                    # Show scroll indicator
                    if max_scroll > 0:
                        scroll_info = _("File {}/{} | Page {}/{}").format(
                            scroll_offset + 1,
                            len(files),
                            (scroll_offset // max_visible) + 1,
                            (len(files) + max_visible - 1) // max_visible
                        )
                        win.addstr(hh - 2, 2, scroll_info, curses.A_DIM)
                    
                    # Show key hints
                    hints = _("Keys: Up/Down/PgUp/PgDn=scroll | Any other key=close")
                    hint_x = max(2, ww - len(hints) - 2)
                    win.addstr(hh - 2, hint_x, hints[:ww - hint_x - 2], curses.A_DIM)
                    
                    win.refresh()
                    
                    ch = win.getch()
                    
                    if ch == curses.KEY_UP:
                        scroll_offset = max(0, scroll_offset - 1)
                    elif ch == curses.KEY_DOWN:
                        scroll_offset = min(max_scroll, scroll_offset + 1)
                    elif ch == curses.KEY_PPAGE:
                        scroll_offset = max(0, scroll_offset - max_visible)
                    elif ch == curses.KEY_NPAGE:
                        scroll_offset = min(max_scroll, scroll_offset + max_visible)
                    elif ch == curses.KEY_HOME:
                        scroll_offset = 0
                    elif ch == curses.KEY_END:
                        scroll_offset = max_scroll
                    else:
                        break
                        
                except curses.error:
                    break
            
            self.draw()

    def _handle_mouse(self, mx, my, bstate):
        """Handle a mouse event. Called from run() when KEY_MOUSE is received."""
        # Accept press, release or click — terminals differ on which they report
        is_click = bool(bstate & (
            curses.BUTTON1_PRESSED |
            curses.BUTTON1_RELEASED |
            curses.BUTTON1_CLICKED
        ))
        is_scroll_up = bool(bstate & curses.BUTTON4_PRESSED)
        is_scroll_down = bool(bstate & getattr(curses, 'BUTTON5_PRESSED', 0x200000))

        # --- Scroll wheel on the results list ---
        if is_scroll_up or is_scroll_down:
            if self._results_top_y <= my <= self._results_bottom_y + 2:
                if is_scroll_up and self.selected_index > 0:
                    self.selected_index -= 1
                elif is_scroll_down and self.selected_index < len(self.results) - 1:
                    self.selected_index += 1
            return

        if not is_click:
            return

        # --- Click on menu bar (row 0) ---
        if my == 0:
            for start_x, end_x, menu_index in self._menu_bar_regions:
                if start_x <= mx < end_x:
                    if self.menu.is_open and self.menu.active_menu == menu_index:
                        self.menu.is_open = False
                    else:
                        self.menu.active_menu = menu_index
                        self.menu.selected_index = 0
                        self.menu.is_open = True
                    return
            if self.menu.is_open:
                self.menu.is_open = False
            return

        # --- Click inside an open dropdown ---
        if self.menu.is_open and self.menu._dropdown_box is not None:
            top_y, left_x, bot_y, right_x = self.menu._dropdown_box
            if top_y <= my <= bot_y and left_x <= mx <= right_x:
                for item_idx, item_y in enumerate(self.menu._dropdown_item_rows):
                    if my == item_y:
                        self.menu.selected_index = item_idx
                        self.menu.activate_item()
                        self.menu.is_open = False
                        return
                return  # click on border — stay open
            else:
                self.menu.is_open = False
                return

        # --- Click on search area (rows 3-5) ---
        if 3 <= my <= 5:
            self.focus = 'search'
            self.search_cursor_pos = len(self.search_query)
            return

        # --- Click on a results list row ---
        if self._results_top_y <= my <= self._results_bottom_y:
            row_offset = my - self._results_top_y
            clicked_index = self.results_scroll_offset + row_offset
            if 0 <= clicked_index < len(self.results):
                if clicked_index == self.selected_index:
                    self.show_details()
                else:
                    self.selected_index = clicked_index
                    self.focus = 'list'

    def run(self):
            try:
                while self.running:
                    self.scheduler.drain()
                    
                    self.spinner.advance()
                    
                    ch = self.stdscr.getch()
                    h, w = self.stdscr.getmaxyx()
                    
                    is_ready = self.status_message == _("Ready")
                    is_busy = not is_ready and not self.is_error_status

                    if ch != -1:
                        if ch in (ord('q'), ord('Q')) and self.focus != 'search':
                            self.running = False
                            break
                        
                        if ch in (ord('l'), ord('L')) and self.focus != 'search':
                            self.log_visible = not self.log_visible
                            self.log_scroll = 0
                        
                        elif ch == curses.KEY_PPAGE:
                            if self.log_visible and self.visible_log_height_lines > 0:
                                log_height_lines = self.visible_log_height_lines
                                max_scroll = max(0, len(self.log_lines) - log_height_lines)
                                self.log_scroll = min(max_scroll, self.log_scroll + log_height_lines)
                        
                        elif ch == curses.KEY_NPAGE:
                            if self.log_visible and self.visible_log_height_lines > 0:
                                log_height_lines = self.visible_log_height_lines
                                self.log_scroll = max(0, self.log_scroll - log_height_lines)

                        if is_busy:
                            if self.menu.is_open and ch in (27, curses.KEY_F9):
                                self.menu.is_open = False
                            
                            self.draw()
                            continue
                    
                        # If an error is displayed, any key press should clear it and return to Ready
                        if self.is_error_status and ch not in (curses.KEY_F9, 27, ord('q'), ord('Q')):
                            self.set_status(_("Ready"))
                            # We continue to process the key press normally

                        # --- Mouse handling ---
                        if ch == curses.KEY_MOUSE:
                            try:
                                _id, mx, my, _z, bstate = curses.getmouse()
                                self._handle_mouse(mx, my, bstate)
                            except curses.error:
                                pass

                        elif self.menu.is_open:
                            self.menu.handle_input(ch)
                        
                        elif self.focus == 'search':
                            if ch in (curses.KEY_ENTER, 10, 13):
                                if self.search_query:
                                    self.run_search(self.search_query)
                                self.focus = 'list'
                            
                            elif ch in (curses.KEY_DOWN, 9, 27):
                                self.focus = 'list'
                            
                            elif ch == curses.KEY_F9:
                                self.focus = 'list'
                                self.menu.is_open = True

                            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                                if self.search_cursor_pos > 0:
                                    self.search_query = self.search_query[:self.search_cursor_pos - 1] + self.search_query[self.search_cursor_pos:]
                                    self.search_cursor_pos -= 1
                            elif ch == curses.KEY_DC:
                                self.search_query = self.search_query[:self.search_cursor_pos] + self.search_query[self.search_cursor_pos + 1:]
                            elif ch == curses.KEY_LEFT:
                                self.search_cursor_pos = max(0, self.search_cursor_pos - 1)
                            elif ch == curses.KEY_RIGHT:
                                self.search_cursor_pos = min(len(self.search_query), self.search_cursor_pos + 1)
                            elif ch == curses.KEY_HOME:
                                self.search_cursor_pos = 0
                            elif ch == curses.KEY_END:
                                self.search_cursor_pos = len(self.search_query)
                            elif curses.ascii.isprint(ch):
                                try:
                                    s = chr(ch)
                                    self.search_query = self.search_query[:self.search_cursor_pos] + s + self.search_query[self.search_cursor_pos:]
                                    self.search_cursor_pos += 1
                                except:
                                    pass
                        
                        elif self.focus == 'list':
                            if ch in (curses.KEY_F9,):
                                self.menu.is_open = True
                            elif ch in (ord('s'), ord('S')):
                                self.focus = 'search'
                                self.search_cursor_pos = len(self.search_query)
                            
                            elif ch == curses.KEY_DOWN:
                                if self.selected_index < len(self.results) - 1:
                                    self.selected_index += 1
                            elif ch == curses.KEY_UP:
                                if self.selected_index > 0:
                                    self.selected_index -= 1
                                else:
                                    self.focus = 'search'
                                    self.search_cursor_pos = len(self.search_query)
                            elif ch in (10, 13):
                                self.show_details()
                            elif ch in (ord('i'), ord('I'), ord(' ')):
                                self.do_install_uninstall_selected()
                                
                    self.draw()
            finally:
                # Always cleanup, even on exception or signal
                self.cleanup()

def main(stdscr):
    # Set up signal handlers before creating app
    setup_signal_handlers()
    
    app = None
    try:
        app = LuetTUI(stdscr)
        app.run()
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        if app:
            app.cleanup()
    except Exception:
        if app and app.search_win: app.search_win = None
        if app and app.log_win: app.log_win = None
        if app and app.results_win: app.results_win = None
        curses.endwin()
        traceback.print_exc()

if __name__ == "__main__":
    set_process_title("vajo-tui")
    try:
        print(_("Starting Vajo: a Luet TUI frontend..."))
        curses.wrapper(main)
    except KeyboardInterrupt:
        print("\nShutdown complete.")
        sys.exit(0)
    except Exception as e:
        print(_("An error occurred outside of curses: {}").format(e), file=sys.stderr)
        sys.exit(1)