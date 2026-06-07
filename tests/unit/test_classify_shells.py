"""Tests for PowerShell and cmd.exe classifiers."""

from tracemill.classify import (
    classify_cmd_command,
    classify_powershell_command,
)
from tracemill.classify.rules import (
    SHELL_GIT_OPS,
    SHELL_IMPLEMENTATION,
    SHELL_INVESTIGATION,
    SHELL_SETUP,
    SHELL_VERIFICATION,
)


class TestPowerShellClassification:
    """Tests for classify_powershell_command."""

    # ── Empty / edge cases ──

    def test_empty_string(self):
        assert classify_powershell_command("") == SHELL_IMPLEMENTATION

    def test_whitespace_only(self):
        assert classify_powershell_command("   ") == SHELL_IMPLEMENTATION

    # ── PowerShell cmdlets ──

    def test_invoke_pester(self):
        assert classify_powershell_command("Invoke-Pester -Path ./tests") == SHELL_VERIFICATION

    def test_invoke_scriptanalyzer(self):
        assert classify_powershell_command("Invoke-ScriptAnalyzer -Path .") == SHELL_VERIFICATION

    def test_install_module(self):
        assert classify_powershell_command("Install-Module Pester") == SHELL_SETUP

    def test_install_package(self):
        assert classify_powershell_command("Install-Package NuGet") == SHELL_SETUP

    def test_get_childitem(self):
        assert classify_powershell_command("Get-ChildItem -Recurse") == SHELL_INVESTIGATION

    def test_get_content(self):
        assert classify_powershell_command("Get-Content ./file.txt") == SHELL_INVESTIGATION

    def test_select_string(self):
        assert (
            classify_powershell_command("Select-String -Pattern 'error' -Path log.txt")
            == SHELL_INVESTIGATION
        )

    def test_set_content(self):
        assert (
            classify_powershell_command("Set-Content -Path out.txt -Value 'hello'")
            == SHELL_IMPLEMENTATION
        )

    def test_new_item(self):
        assert (
            classify_powershell_command("New-Item -Path ./dir -ItemType Directory")
            == SHELL_IMPLEMENTATION
        )

    # ── Case insensitivity ──

    def test_cmdlet_case_insensitive(self):
        assert classify_powershell_command("invoke-pester") == SHELL_VERIFICATION
        assert classify_powershell_command("INVOKE-PESTER") == SHELL_VERIFICATION

    # ── Non-cmdlet binaries (same as bash) ──

    def test_pytest(self):
        assert classify_powershell_command("pytest --verbose") == SHELL_VERIFICATION

    def test_pip_install(self):
        assert classify_powershell_command("pip install requests") == SHELL_SETUP

    def test_npm_install(self):
        assert classify_powershell_command("npm install") == SHELL_SETUP

    def test_cargo_test(self):
        assert classify_powershell_command("cargo test") == SHELL_VERIFICATION

    def test_dotnet_build(self):
        assert classify_powershell_command("dotnet build") == SHELL_VERIFICATION

    def test_git_commit(self):
        assert classify_powershell_command("git commit -m 'msg'") == SHELL_GIT_OPS

    def test_git_status(self):
        assert classify_powershell_command("git status") == SHELL_INVESTIGATION

    def test_ruff_check(self):
        assert classify_powershell_command("ruff check .") == SHELL_VERIFICATION

    # ── Compound commands ──

    def test_semicolon_split(self):
        assert (
            classify_powershell_command("pip install pytest; Invoke-Pester") == SHELL_VERIFICATION
        )

    def test_pipeline_highest_priority(self):
        # Pipeline: Get-Process | Sort-Object → both investigation
        assert classify_powershell_command("Get-Process | Sort-Object CPU") == SHELL_INVESTIGATION

    def test_mixed_priority(self):
        # Setup + verification → verification wins
        assert (
            classify_powershell_command("Install-Module Pester; Invoke-Pester")
            == SHELL_VERIFICATION
        )

    # ── Windows-specific package managers ──

    def test_choco_install(self):
        assert classify_powershell_command("choco install git") == SHELL_SETUP

    def test_winget_install(self):
        assert classify_powershell_command("winget install Python.Python.3") == SHELL_SETUP

    def test_scoop_install(self):
        assert classify_powershell_command("scoop install nodejs") == SHELL_SETUP


class TestCmdClassification:
    """Tests for classify_cmd_command."""

    # ── Empty / edge cases ──

    def test_empty_string(self):
        assert classify_cmd_command("") == SHELL_IMPLEMENTATION

    def test_whitespace_only(self):
        assert classify_cmd_command("   ") == SHELL_IMPLEMENTATION

    # ── cmd.exe built-ins ──

    def test_dir(self):
        assert classify_cmd_command("dir /s") == SHELL_INVESTIGATION

    def test_type(self):
        assert classify_cmd_command("type readme.txt") == SHELL_INVESTIGATION

    def test_findstr(self):
        assert classify_cmd_command('findstr /i "error" log.txt') == SHELL_INVESTIGATION

    def test_copy(self):
        assert classify_cmd_command("copy src.txt dst.txt") == SHELL_IMPLEMENTATION

    def test_del(self):
        assert classify_cmd_command("del /q temp.log") == SHELL_IMPLEMENTATION

    def test_mkdir(self):
        assert classify_cmd_command("mkdir build") == SHELL_IMPLEMENTATION

    # ── Non-builtin binaries ──

    def test_pytest(self):
        assert classify_cmd_command("pytest --verbose") == SHELL_VERIFICATION

    def test_pip_install(self):
        assert classify_cmd_command("pip install flask") == SHELL_SETUP

    def test_npm_test(self):
        assert classify_cmd_command("npm test") == SHELL_VERIFICATION

    def test_cargo_build(self):
        assert classify_cmd_command("cargo build") == SHELL_VERIFICATION

    def test_git_push(self):
        assert classify_cmd_command("git push origin main") == SHELL_GIT_OPS

    def test_git_log(self):
        assert classify_cmd_command("git log --oneline") == SHELL_INVESTIGATION

    def test_dotnet_test(self):
        assert classify_cmd_command("dotnet test") == SHELL_VERIFICATION

    # ── Compound commands ──

    def test_single_ampersand_split(self):
        assert classify_cmd_command("pip install pytest & pytest") == SHELL_VERIFICATION

    def test_double_ampersand_split(self):
        assert classify_cmd_command("cd src && pytest") == SHELL_VERIFICATION

    def test_mixed_priority_compound(self):
        assert classify_cmd_command("pip install flask && git commit -m fix") == SHELL_GIT_OPS

    def test_quoted_ampersand_not_split(self):
        # & inside quotes should NOT split
        assert classify_cmd_command('echo "a & b"') == SHELL_IMPLEMENTATION

    # ── Extension stripping ──

    def test_exe_extension(self):
        assert classify_cmd_command("pytest.exe --verbose") == SHELL_VERIFICATION

    def test_cmd_extension(self):
        assert classify_cmd_command("npm.cmd install") == SHELL_SETUP

    # ── Windows package managers ──

    def test_choco_install(self):
        assert classify_cmd_command("choco install python") == SHELL_SETUP

    def test_winget_install(self):
        assert classify_cmd_command("winget install Git.Git") == SHELL_SETUP

    def test_scoop_install(self):
        assert classify_cmd_command("scoop install rust") == SHELL_SETUP
