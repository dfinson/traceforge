from functools import partial

"""Tests for PowerShell and cmd.exe classifiers."""

from tracemill.classify import (
    classify_cmd_command,
    classify_powershell_command,
    get_default_engine,
)
from tracemill.classify.rules import (
    SHELL_DELIVERY,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
    activity_from_classification,
)


ENGINE = get_default_engine()
classify_powershell_command = partial(classify_powershell_command, engine=ENGINE)
classify_cmd_command = partial(classify_cmd_command, engine=ENGINE)


def _ps_activity(cmd: str):
    """Helper: classify PowerShell command and derive its ShellActivity."""
    return activity_from_classification(classify_powershell_command(cmd))


def _cmd_activity(cmd: str):
    """Helper: classify cmd.exe command and derive its ShellActivity."""
    return activity_from_classification(classify_cmd_command(cmd))


class TestPowerShellClassification:
    """Tests for classify_powershell_command."""

    # ── Empty / edge cases ──

    def test_empty_string(self):
        assert _ps_activity("") == SHELL_IMPLEMENTATION

    def test_whitespace_only(self):
        assert _ps_activity("   ") == SHELL_IMPLEMENTATION

    # ── PowerShell cmdlets ──

    def test_invoke_pester(self):
        assert _ps_activity("Invoke-Pester -Path ./tests") == SHELL_VERIFICATION

    def test_invoke_scriptanalyzer(self):
        assert _ps_activity("Invoke-ScriptAnalyzer -Path .") == SHELL_VERIFICATION

    def test_install_module(self):
        assert _ps_activity("Install-Module Pester") == SHELL_SETUP

    def test_install_package(self):
        assert _ps_activity("Install-Package NuGet") == SHELL_SETUP

    def test_get_childitem(self):
        assert _ps_activity("Get-ChildItem -Recurse") == SHELL_INVESTIGATION

    def test_get_content(self):
        assert _ps_activity("Get-Content ./file.txt") == SHELL_INVESTIGATION

    def test_select_string(self):
        assert (
            _ps_activity("Select-String -Pattern 'error' -Path log.txt")
            == SHELL_INVESTIGATION
        )

    def test_set_content(self):
        assert (
            _ps_activity("Set-Content -Path out.txt -Value 'hello'")
            == SHELL_IMPLEMENTATION
        )

    def test_new_item(self):
        assert (
            _ps_activity("New-Item -Path ./dir -ItemType Directory")
            == SHELL_IMPLEMENTATION
        )

    # ── Case insensitivity ──

    def test_cmdlet_case_insensitive(self):
        assert _ps_activity("invoke-pester") == SHELL_VERIFICATION
        assert _ps_activity("INVOKE-PESTER") == SHELL_VERIFICATION

    # ── Non-cmdlet binaries (same as bash) ──

    def test_pytest(self):
        assert _ps_activity("pytest --verbose") == SHELL_VERIFICATION

    def test_pip_install(self):
        assert _ps_activity("pip install requests") == SHELL_SETUP

    def test_npm_install(self):
        assert _ps_activity("npm install") == SHELL_SETUP

    def test_cargo_test(self):
        assert _ps_activity("cargo test") == SHELL_VERIFICATION

    def test_dotnet_build(self):
        assert _ps_activity("dotnet build") == SHELL_VERIFICATION

    def test_git_commit(self):
        assert _ps_activity("git commit -m 'msg'") == SHELL_DELIVERY

    def test_git_status(self):
        assert _ps_activity("git status") == SHELL_INVESTIGATION

    def test_ruff_check(self):
        assert _ps_activity("ruff check .") == SHELL_VERIFICATION

    # ── Compound commands ──

    def test_semicolon_split(self):
        assert (
            _ps_activity("pip install pytest; Invoke-Pester") == SHELL_VERIFICATION
        )

    def test_pipeline_highest_priority(self):
        # Pipeline: Get-Process | Sort-Object → both investigation
        assert _ps_activity("Get-Process | Sort-Object CPU") == SHELL_INVESTIGATION

    def test_mixed_priority(self):
        # Setup + verification → verification wins
        assert (
            _ps_activity("Install-Module Pester; Invoke-Pester")
            == SHELL_VERIFICATION
        )

    # ── Windows-specific package managers ──

    def test_choco_install(self):
        assert _ps_activity("choco install git") == SHELL_SETUP

    def test_winget_install(self):
        assert _ps_activity("winget install Python.Python.3") == SHELL_SETUP

    def test_scoop_install(self):
        assert _ps_activity("scoop install nodejs") == SHELL_SETUP


class TestCmdClassification:
    """Tests for classify_cmd_command."""

    # ── Empty / edge cases ──

    def test_empty_string(self):
        assert _cmd_activity("") == SHELL_IMPLEMENTATION

    def test_whitespace_only(self):
        assert _cmd_activity("   ") == SHELL_IMPLEMENTATION

    # ── cmd.exe built-ins ──

    def test_dir(self):
        assert _cmd_activity("dir /s") == SHELL_INVESTIGATION

    def test_type(self):
        assert _cmd_activity("type readme.txt") == SHELL_INVESTIGATION

    def test_findstr(self):
        assert _cmd_activity('findstr /i "error" log.txt') == SHELL_INVESTIGATION

    def test_copy(self):
        assert _cmd_activity("copy src.txt dst.txt") == SHELL_IMPLEMENTATION

    def test_del(self):
        assert _cmd_activity("del /q temp.log") == SHELL_IMPLEMENTATION

    def test_mkdir(self):
        assert _cmd_activity("mkdir build") == SHELL_SETUP

    # ── Non-builtin binaries ──

    def test_pytest(self):
        assert _cmd_activity("pytest --verbose") == SHELL_VERIFICATION

    def test_pip_install(self):
        assert _cmd_activity("pip install flask") == SHELL_SETUP

    def test_npm_test(self):
        assert _cmd_activity("npm test") == SHELL_VERIFICATION

    def test_cargo_build(self):
        assert _cmd_activity("cargo build") == SHELL_VERIFICATION

    def test_git_push(self):
        assert _cmd_activity("git push origin main") == SHELL_DELIVERY

    def test_git_log(self):
        assert _cmd_activity("git log --oneline") == SHELL_INVESTIGATION

    def test_dotnet_test(self):
        assert _cmd_activity("dotnet test") == SHELL_VERIFICATION

    # ── Compound commands ──

    def test_single_ampersand_split(self):
        assert _cmd_activity("pip install pytest & pytest") == SHELL_VERIFICATION

    def test_double_ampersand_split(self):
        assert _cmd_activity("cd src && pytest") == SHELL_VERIFICATION

    def test_mixed_priority_compound(self):
        # Compound command spans multiple phases — verify both are represented
        cls = classify_cmd_command("pip install flask && git commit -m fix")
        phases = {seg.phase for seg in cls.phase_map}
        assert "implementation" in phases  # pip install = setup/implementation
        assert "review" in phases  # git commit = review

    def test_quoted_ampersand_not_split(self):
        # & inside quotes should NOT split
        assert _cmd_activity('echo "a & b"') == SHELL_IMPLEMENTATION

    # ── Extension stripping ──

    def test_exe_extension(self):
        assert _cmd_activity("pytest.exe --verbose") == SHELL_VERIFICATION

    def test_cmd_extension(self):
        assert _cmd_activity("npm.cmd install") == SHELL_SETUP

    # ── Windows package managers ──

    def test_choco_install(self):
        assert _cmd_activity("choco install python") == SHELL_SETUP

    def test_winget_install(self):
        assert _cmd_activity("winget install Git.Git") == SHELL_SETUP

    def test_scoop_install(self):
        assert _cmd_activity("scoop install rust") == SHELL_SETUP

