import subprocess
import sys


def test_autonomy_packages_import_in_any_order() -> None:
    orders = (
        "import adapters; import planner; import arbiter",
        "import planner; import arbiter; import adapters",
        "import arbiter; import adapters; import planner",
    )
    for imports in orders:
        result = subprocess.run(
            [sys.executable, "-c", imports],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
