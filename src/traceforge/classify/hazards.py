"""Semantic hazard detection for shell commands.

Adds high-signal *capability* tags for dangerous command shapes that the
structural classifier (effect × scope) does not by itself gate — raw block-device
writes, filesystem formats/wipes, persistence-mechanism writes, and fork bombs.

These tags are consumed by the governance recommendation rules
(``recommendation_rules.yaml``) to escalate the command regardless of its numeric
risk score. Detection is pure, deterministic regex/binary matching over the raw
command string and the already-parsed :class:`Classification`, so it introduces no
nondeterminism and no new scoring weight (the tags are not in
``risk.yaml: capability_weights``, so the numeric score is unchanged).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from traceforge.classify.core import Classification

# ── Capability tag names (kept in sync with recommendation_rules.yaml) ──
RAW_DEVICE_WRITE = "raw_device_write"
FILESYSTEM_FORMAT = "filesystem_format"
PERSISTENCE_WRITE = "persistence_write"
FORK_BOMB = "fork_bomb"

# ── 1. Raw block-device write ───────────────────────────────────────────────
# dd ... of=/dev/sda, or a redirect (>, >>) into a physical block device.
# Physical device classes only — /dev/null, /dev/zero, /dev/urandom, /dev/tty
# are deliberately excluded so benign `> /dev/null` never trips.
_RAW_DEVICE_WRITE = re.compile(
    r"(?:\bof=|>>?\s*)/dev/(?:sd|nvme|disk|hd|vd|mmcblk|xvd)\w*",
    re.IGNORECASE,
)

# ── 2. Filesystem format / wipe ─────────────────────────────────────────────
# Unambiguous formatter binaries (mkfs, mkfs.<fs>, mke2fs, mkswap, wipefs) plus
# destructive partition-table ops on parted/sfdisk/sgdisk. Read-only inspection
# (`parted -l`, `fdisk -l`, `parted <dev> print`) is NOT matched.
_FORMAT_BINARIES = frozenset({"mkfs", "mke2fs", "mkswap", "wipefs"})
_DISK_PARTITION_TOOLS = frozenset({"parted", "sfdisk", "sgdisk"})
_DISK_DESTRUCTIVE_OP = re.compile(
    r"(?<!\S)(?:mklabel|mkpart|--zap-all|--zap|-Z|--delete|--new)\b",
    re.IGNORECASE,
)

# ── 3. Persistence-mechanism write ──────────────────────────────────────────
# A write mechanism (redirect, tee, cp/mv/install/ln, or the crontab binary)
# targeting a known persistence location: cron, systemd units, or shell rc files.
_PERSIST_PATH = (
    r"(?:"
    r"/etc/cron(?:\.[a-z]+|tab|\.d)?"
    r"|/var/spool/cron"
    r"|/etc/systemd/|/run/systemd/|/lib/systemd/|/usr/lib/systemd/"
    r"|/etc/profile(?:\.d)?|/etc/bash\.bashrc|/etc/zsh/|/etc/zprofile|/etc/zshrc"
    r"|(?:~|\$HOME|/root|/home/[^/\s]+)/"
    r"\.(?:bashrc|bash_profile|bash_login|profile|zshrc|zprofile|zlogin|zshenv)"
    r"|(?:~|\$HOME|/root|/home/[^/\s]+)/\.config/(?:systemd|autostart)"
    r")"
)
_PERSIST_REDIRECT = re.compile(r">>?\s*[\"']?" + _PERSIST_PATH)
_PERSIST_TEE = re.compile(r"\btee\b(?:\s+-{1,2}\S+)*\s+[\"']?" + _PERSIST_PATH)
_PERSIST_COPY = re.compile(r"\b(?:cp|mv|install|ln)\b[^\n]*?" + _PERSIST_PATH)

# ── 4. Fork bomb ────────────────────────────────────────────────────────────
# A function that recursively pipes itself into the background, then invokes
# itself: classic `:(){ :|:& };:` and named variants like `b(){ b|b& };b`.
_FORK_BOMB = re.compile(
    r"(?P<n>[\w:.\-]+)\s*\(\)\s*\{(?P<body>[^{}]*)\}\s*;\s*(?P=n)",
    re.DOTALL,
)


def _is_fork_bomb(command: str) -> bool:
    for match in _FORK_BOMB.finditer(command):
        name = match.group("n")
        body = match.group("body")
        if "|" in body and "&" in body and body.count(name) >= 2:
            return True
    return False


def _has_filesystem_format(command: str, binaries: tuple[str, ...]) -> bool:
    for binary in binaries:
        if binary in _FORMAT_BINARIES or binary.startswith("mkfs."):
            return True
    if any(tool in binaries for tool in _DISK_PARTITION_TOOLS):
        return bool(_DISK_DESTRUCTIVE_OP.search(command))
    return False


def _has_persistence_write(command: str, binaries: tuple[str, ...]) -> bool:
    if "crontab" in binaries:
        return True
    return bool(
        _PERSIST_REDIRECT.search(command)
        or _PERSIST_TEE.search(command)
        or _PERSIST_COPY.search(command)
    )


def detect_command_hazards(command: str, classification: Classification) -> set[str]:
    """Return semantic hazard capability tags for a shell command.

    Pure and deterministic: matches the raw command string and the parsed
    binaries. Tags are score-neutral (no ``capability_weights`` entry) and are
    used only to drive escalation via the recommendation rules.
    """
    hazards: set[str] = set()
    if not command:
        return hazards

    binaries = classification.binaries or ()

    if _RAW_DEVICE_WRITE.search(command):
        hazards.add(RAW_DEVICE_WRITE)
    if _has_filesystem_format(command, binaries):
        hazards.add(FILESYSTEM_FORMAT)
    if _has_persistence_write(command, binaries):
        hazards.add(PERSISTENCE_WRITE)
    if _is_fork_bomb(command):
        hazards.add(FORK_BOMB)

    return hazards
