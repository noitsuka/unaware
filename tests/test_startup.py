import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def probe_python(executable, arguments=()):
    shell = shutil.which("powershell.exe")
    if shell is None:
        pytest.skip("Windows PowerShell unavailable")
    startup = Path(__file__).resolve().parents[1] / "start.ps1"
    prefix = "@(" + ",".join(ps_literal(value) for value in arguments) + ")"
    command = f"""
$ErrorActionPreference = 'Stop'
$tree = [System.Management.Automation.Language.Parser]::ParseFile({ps_literal(startup)}, [ref]$null, [ref]$null)
$probe = $tree.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-Python312'
}}, $true)
if ($null -eq $probe) {{ throw 'Python detection function missing' }}
Invoke-Expression $probe.Extent.Text
if (Test-Python312 -Executable {ps_literal(executable)} -Arguments {prefix}) {{ exit 0 }}
exit 1
"""
    result = subprocess.run([shell, "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode in (0, 1), result.stderr
    assert not result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("arguments,expected", [
    ([], True),
    (["-c", "raise SystemExit(1)"], False),
    (["-c", "import sys; sys.stderr.write('runtime missing'); raise SystemExit(1)"], False),
])
def test_python_detection_handles_native_command_failures(arguments, expected):
    assert probe_python(sys.executable, arguments) is expected


def test_python_detection_rejects_a_missing_executable():
    startup = Path(__file__).resolve().parents[1] / "start.ps1"
    missing = startup.parent / "missing-python-for-startup-test.exe"
    assert not missing.exists()
    assert not probe_python(missing)
