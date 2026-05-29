#!/usr/bin/env python3

import os
import socket
import subprocess
import shutil
from .i18n import _

class SystemInfoProvider:
    """
    Provides hardware and operating system information.
    Designed to be used by both GUI and TUI frontends.
    """

    @staticmethod
    def gather_info():
        """
        Gathers system information into a list of (label, value) tuples.
        """
        items = []
        
        # 1. Hostname
        try:
            items.append((_("Hostname:"), socket.gethostname()))
        except Exception: pass

        # 2. OS Info
        os_name = _("Unknown")
        if os.path.exists("/etc/os-release"):
            try:
                with open("/etc/os-release", "r") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            os_name = line.split("=")[1].strip().strip('"')
                            break
            except Exception: pass
        items.append((_("Operating System:"), os_name))

        # 3. Kernel
        try: items.append((_("Kernel Version:"), os.uname().release))
        except Exception: pass

        # 4. CPU Info
        try:
            cpu_model = _("Unknown")
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("model name"):
                        cpu_model = line.split(":", 1)[1].strip()
                        break
            items.append((_("CPU:"), cpu_model))
        except Exception: pass

        # 5. RAM Info
        try:
            mem_total = 0
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1])
                        break
            if mem_total:
                total_gb = mem_total / (1024**2)
                items.append((_("RAM:"), f"{total_gb:.1f} GB"))
        except Exception: pass

        # 6. Uptime
        try:
            with open("/proc/uptime", "r") as f:
                uptime_seconds = float(f.readline().split()[0])
                days = int(uptime_seconds // 86400)
                hours = int((uptime_seconds % 86400) // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                uptime_str = f"{days}d {hours}h {minutes}m"
                items.append((_("Uptime:"), uptime_str))
        except Exception: pass

        # 7. Local IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('10.255.255.255', 1))
            ip_addr = s.getsockname()[0]
            s.close()
            items.append((_("Local IP Address:"), ip_addr))
        except Exception: pass

        # 8. Disk Space (/)
        try:
            usage = shutil.disk_usage("/")
            free_gb = usage.free / (1024**3)
            total_gb = usage.total / (1024**3)
            disk_info = f"{free_gb:.1f} GB " + _("free of") + f" {total_gb:.1f} GB"
            items.append((_("Disk Space (/):"), disk_info))
        except Exception: pass

        # 9. GPU Card & Driver
        gpu_card = _("Unknown")
        gpu_driver = _("Unknown")
        try:
            # Try glxinfo -B
            res = subprocess.run(["glxinfo", "-B"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "Device:" in line:
                        gpu_card = line.split(":", 1)[1].strip()
                    elif "OpenGL renderer string:" in line:
                        if gpu_card == _("Unknown"):
                            gpu_card = line.split(":", 1)[1].strip()
                    elif "OpenGL version string:" in line:
                        gpu_driver = line.split(":", 1)[1].strip()
        except Exception: pass

        if gpu_card == _("Unknown") or gpu_driver == _("Unknown"):
            try:
                # Get Card Name
                res_card = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=2)
                if res_card.returncode == 0:
                    gpu_card = res_card.stdout.strip()
                
                # Get Driver Version
                res_drv = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], capture_output=True, text=True, timeout=2)
                if res_drv.returncode == 0:
                    gpu_driver = f"NVIDIA {res_drv.stdout.strip()}"
            except Exception: pass

        if gpu_card == _("Unknown"):
            try:
                res = subprocess.run(["sh", "-c", "lspci | grep -i vga"], capture_output=True, text=True, timeout=2)
                if res.returncode == 0 and res.stdout:
                    gpu_card = res.stdout.split(":", 2)[-1].strip()
            except Exception: pass

        items.append((_("GPU Card:"), gpu_card))
        items.append((_("GPU Driver:"), gpu_driver))

        return items
