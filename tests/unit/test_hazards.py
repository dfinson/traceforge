"""Unit tests for semantic shell-command hazard detection.

Covers the four detectors in :mod:`traceforge.classify.hazards`
(``raw_device_write``, ``filesystem_format``, ``persistence_write``,
``fork_bomb``): each must fire on the dangerous shape and stay silent on the
close-but-benign forms that would otherwise cause over-gating. A small
integration block confirms the tags actually land on ``Classification.capability``
when routed through :func:`classify_shell`.
"""

from __future__ import annotations

import pytest

from traceforge.classify import classify_shell, get_default_engine
from traceforge.classify.core import Classification
from traceforge.classify.hazards import (
    FILESYSTEM_FORMAT,
    FORK_BOMB,
    PERSISTENCE_WRITE,
    RAW_DEVICE_WRITE,
    detect_command_hazards,
)

ENGINE = get_default_engine()


def _detect(command: str, *binaries: str) -> set[str]:
    """Run the detectors against a raw command + explicit parsed binaries."""
    cls = Classification(mechanism="process.shell", binaries=tuple(binaries))
    return detect_command_hazards(command, cls)


# ── raw_device_write ─────────────────────────────────────────────────────────
class TestRawDeviceWrite:
    @pytest.mark.parametrize(
        "command",
        [
            "dd if=/dev/zero of=/dev/sda",
            "dd if=/dev/zero of=/dev/nvme0n1 bs=1M",
            "dd if=/dev/zero of=/dev/mmcblk0",
            "cat payload > /dev/sda",
            "echo x >> /dev/sdb1",
            "dd if=/dev/zero of=/dev/disk2",
        ],
    )
    def test_fires_on_raw_device_write(self, command: str) -> None:
        assert RAW_DEVICE_WRITE in _detect(command, "dd")

    @pytest.mark.parametrize(
        "command",
        [
            "echo done > /dev/null",
            "cat noise > /dev/zero",
            "dd if=/dev/urandom of=/dev/null",
            "dd if=input.img of=output.img",
            "cp disk.img backup.img",
            "echo hello > out.txt",
        ],
    )
    def test_silent_on_benign(self, command: str) -> None:
        assert RAW_DEVICE_WRITE not in _detect(command, "dd")


# ── filesystem_format ────────────────────────────────────────────────────────
class TestFilesystemFormat:
    @pytest.mark.parametrize(
        ("command", "binary"),
        [
            ("mkfs.ext4 /dev/sda1", "mkfs.ext4"),
            ("mkfs -t ext4 /dev/sda1", "mkfs"),
            ("mke2fs /dev/sdb1", "mke2fs"),
            ("mkswap /dev/sdb2", "mkswap"),
            ("wipefs -a /dev/sdb", "wipefs"),
        ],
    )
    def test_fires_on_formatter_binaries(self, command: str, binary: str) -> None:
        assert FILESYSTEM_FORMAT in _detect(command, binary)

    @pytest.mark.parametrize(
        ("command", "binary"),
        [
            ("parted /dev/sda mklabel gpt", "parted"),
            ("parted /dev/sda mkpart primary 0% 100%", "parted"),
            ("sgdisk --zap-all /dev/sda", "sgdisk"),
            ("sfdisk --delete /dev/sda", "sfdisk"),
        ],
    )
    def test_fires_on_destructive_partition_ops(self, command: str, binary: str) -> None:
        assert FILESYSTEM_FORMAT in _detect(command, binary)

    @pytest.mark.parametrize(
        ("command", "binary"),
        [
            ("parted -l", "parted"),
            ("parted /dev/sda print", "parted"),
            ("fdisk -l", "fdisk"),
            ("fdisk -l /dev/sda", "fdisk"),
        ],
    )
    def test_silent_on_inspection(self, command: str, binary: str) -> None:
        assert FILESYSTEM_FORMAT not in _detect(command, binary)


# ── persistence_write ────────────────────────────────────────────────────────
class TestPersistenceWrite:
    @pytest.mark.parametrize(
        ("command", "binaries"),
        [
            ("echo malware > /etc/cron.d/x", ("echo",)),
            ("echo '* * * * * root sh' >> /etc/crontab", ("echo",)),
            ("echo evil >> ~/.bashrc", ("echo",)),
            ("echo evil >> /root/.profile", ("echo",)),
            ("cp payload /etc/systemd/system/evil.service", ("cp",)),
            ("tee /etc/cron.d/x", ("tee",)),
            ("crontab -", ("crontab",)),
            ("install -m 0644 unit /lib/systemd/system/x.service", ("install",)),
        ],
    )
    def test_fires_on_persistence_write(self, command: str, binaries: tuple[str, ...]) -> None:
        assert PERSISTENCE_WRITE in _detect(command, *binaries)

    @pytest.mark.parametrize(
        ("command", "binaries"),
        [
            ("cat /etc/crontab", ("cat",)),
            ("source ~/.bashrc", ("source",)),
            ("echo hello > out.txt", ("echo",)),
            ("cat ~/.bashrc", ("cat",)),
            ("ls /etc/cron.d", ("ls",)),
        ],
    )
    def test_silent_on_reads_and_benign(self, command: str, binaries: tuple[str, ...]) -> None:
        assert PERSISTENCE_WRITE not in _detect(command, *binaries)


# ── fork_bomb ────────────────────────────────────────────────────────────────
class TestForkBomb:
    @pytest.mark.parametrize(
        "command",
        [
            ":(){ :|:& };:",
            ":(){ :|: & };:",
            "bomb(){ bomb|bomb& };bomb",
            "b() { b | b & }; b",
        ],
    )
    def test_fires_on_fork_bomb(self, command: str) -> None:
        assert FORK_BOMB in _detect(command)

    @pytest.mark.parametrize(
        "command",
        [
            "greet() { echo hi; }; greet",
            "f() { a | b & }; f",  # backgrounded pipe but not self-recursive
            "build() { make | tee log; }; build",
            "echo :(){ hello }",  # no trailing self-invocation
        ],
    )
    def test_silent_on_benign_functions(self, command: str) -> None:
        assert FORK_BOMB not in _detect(command)


# ── integration: tags land on Classification.capability via classify_shell ────
class TestClassifyShellIntegration:
    @pytest.mark.parametrize(
        ("command", "tag"),
        [
            ("dd if=/dev/zero of=/dev/sda", RAW_DEVICE_WRITE),
            ("mkfs.ext4 /dev/sda1", FILESYSTEM_FORMAT),
            ("echo malware > /etc/cron.d/x", PERSISTENCE_WRITE),
            (":(){ :|:& };:", FORK_BOMB),
        ],
    )
    def test_capability_tag_present(self, command: str, tag: str) -> None:
        cls = classify_shell(command, engine=ENGINE)
        assert tag in cls.capability

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "cat README.md",
            "git status",
            "echo hello",
            "dd if=input.img of=output.img",
        ],
    )
    def test_benign_commands_gain_no_hazard_tag(self, command: str) -> None:
        cls = classify_shell(command, engine=ENGINE)
        hazard_tags = {RAW_DEVICE_WRITE, FILESYSTEM_FORMAT, PERSISTENCE_WRITE, FORK_BOMB}
        assert hazard_tags.isdisjoint(cls.capability)
