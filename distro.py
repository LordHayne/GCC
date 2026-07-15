#!/usr/bin/env python3
"""Distro detection + package manager abstraction.

Keeps the rest of the app distro-agnostic. Everything that needs to install
a package or print an install hint goes through here.
"""
import os
import shutil
import subprocess

# Package names that differ between distros (gamemode is the main offender).
# Key = our generic name, value = {distro: actual package name}.
PKG_ALIASES = {
    "gamemode":    {"arch": "gamemode",    "debian": "gamemode",     "fedora": "gamemode"},
    "gamescope":   {"arch": "gamescope",  "debian": "gamescope",    "fedora": "gamescope"},
    "python-yaml": {"arch": "python-yaml","debian": "python3-yaml", "fedora": "python3-pyyaml"},
    "lm_sensors":  {"arch": "lm_sensors", "debian": "lm-sensors",   "fedora": "lm_sensors"},
}

# Install commands per distro family.
# Each is a template: fill the package name into {pkg}.
INSTALL_CMDS = {
    "arch":   ["pacman", "-S", "--needed", "--noconfirm", "{pkg}"],
    "debian": ["apt", "install", "-y", "{pkg}"],
    "fedora": ["dnf", "install", "-y", "{pkg}"],
}

# User-facing install hints (what to print when a package is missing).
INSTALL_HINTS = {
    "arch":   "pacman -S {pkg}",
    "debian": "sudo apt install {pkg}",
    "fedora": "sudo dnf install {pkg}",
}


def detect_distro():
    """Returns one of: 'arch', 'debian', 'fedora', or None.

    Uses /etc/os-release (works on every modern distro), falls back to
    checking which package manager binary exists.
    """
    # /etc/os-release ID field
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    distro_id = line.split("=", 1)[1].strip().strip('"').lower()
                    if distro_id in ("arch", "cachyos", "manjaro", "endeavouros",
                                     "garuda", "artix"):
                        return "arch"
                    if distro_id in ("ubuntu", "debian", "linuxmint", "pop",
                                     "elementary", "kali"):
                        return "debian"
                    if distro_id in ("fedora", "centos", "rhel", "rocky",
                                     "alma", "nobara"):
                        return "fedora"
                    if distro_id in ("opensuse", "suse", "sles"):
                        return "fedora"  # zypper, but close enough for hints
    except OSError:
        pass

    # Fallback: which binary exists?
    if shutil.which("pacman"):
        return "arch"
    if shutil.which("apt"):
        return "debian"
    if shutil.which("dnf"):
        return "fedora"
    if shutil.which("zypper"):
        return "fedora"
    return None


def pkg_name(generic_name, distro=None):
    """Translate our generic package name to the distro-specific one."""
    if distro is None:
        distro = detect_distro() or "arch"
    aliases = PKG_ALIASES.get(generic_name)
    if aliases:
        return aliases.get(distro, aliases.get("arch", generic_name))
    return generic_name


def install_hint(generic_name, distro=None):
    """User-facing string: 'pacman -S gamemode' or 'sudo apt install gamemode'."""
    if distro is None:
        distro = detect_distro() or "arch"
    actual = pkg_name(generic_name, distro)
    template = INSTALL_HINTS.get(distro, "pacman -S {pkg}")
    return template.format(pkg=actual)


def install_cmd(generic_name, distro=None):
    """Full command list to install a package, e.g. ['pacman','-S','...','gamemode'].

    Returns None if distro is unknown. Caller is responsible for sudo/pkexec.
    """
    if distro is None:
        distro = detect_distro()
    if distro is None:
        return None
    actual = pkg_name(generic_name, distro)
    template = INSTALL_CMDS.get(distro)
    if template is None:
        return None
    return [arg.format(pkg=actual) for arg in template]


def can_install():
    """True if we know how to install packages on this distro."""
    return detect_distro() is not None


if __name__ == "__main__":
    d = detect_distro()
    print(f"Distro: {d}")
    for pkg in ("gamemode", "gamescope", "python-yaml", "lm_sensors"):
        print(f"  {pkg}: name={pkg_name(pkg)} hint='{install_hint(pkg)}' cmd={install_cmd(pkg)}")