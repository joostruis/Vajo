# Vajo

Vajo is a powerful and intuitive frontend for the [Luet](https://luet.it/) package manager, designed specifically for [MocaccinoOS](https://www.mocaccino.org/). It provides both a Graphical User Interface (GUI) and a Terminal User Interface (TUI), making system management accessible to everyone, from casual users to power users.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a82df409-bfa3-48d1-886c-9ac932bbb4e3" alt="Vajo GUI Screenshot" width="800">
</p>

## ✨ Features

- **Dual Interfaces:** Choose between a modern GTK3 GUI and a lightweight Ncurses TUI.
- **Package Management:** Search, install, and uninstall packages with ease.
- **System Maintenance:**
    - Perform full system upgrades.
    - Refresh repositories.
    - Clean Luet cache to save disk space.
    - Run system integrity checks (`luet oscheck`) and automatic repairs.
- **Rollback Support:** Manage system snapshots and roll back to previous states if something goes wrong.
- **Flatpak Integration:** Optional support for managing Flatpak applications alongside native Luet packages.
- **Internationalization:** Full support for multiple languages.
- **Security:** Seamlessly integrates with Polkit for privileged operations.

## 🚀 Getting Started

### Requirements

To run Vajo, you'll need:

- **Python 3.8+**
- **Luet** package manager
- **GTK 3.0** (for the GUI)
- **PyGObject** (`gi` module)
- **PyYAML**
- **packaging** (Python module)
- **Ncurses** (for the TUI)

### Installation

Vajo is typically pre-installed on MocaccinoOS. If you need to install it manually:

1. Clone the repository:
   ```bash
   git clone https://codeberg.org/MocaccinoOS/Vajo.git
   cd Vajo
   ```

2. Run the installation script (requires root):
   ```bash
   sudo ./install.sh
   ```

*Note: The `install.sh` script is primarily designed for packaging. You may need to adjust paths if installing directly to your system.*

## 🛠 Usage

### Launching the GUI
```bash
vajo-gui
```

### Launching the TUI
```bash
vajo-tui
```

### Debug Mode
Both interfaces support a `--debug` flag for detailed logging:
```bash
vajo-gui --debug
```

## ⚙️ Configuration

Vajo stores user preferences in `~/.config/vajo/vajo.conf`. You can enable optional modules like Flatpak and Rollback through the GUI's Preferences dialog or by editing the config file:

```json
{
  "enable_flatpak": true,
  "enable_rollback": true
}
```

## 🤝 Contributing

Contributions are welcome! Whether it's reporting a bug, suggesting a feature, or submitting a Pull Request, please feel free to reach out via the [Codeberg repository](https://codeberg.org/MocaccinoOS/Vajo).

## 📄 License

Vajo is released under the [GNU GPL v3](LICENSE).

---
© 2023 - 2026 MocaccinoOS. All Rights Reserved.
