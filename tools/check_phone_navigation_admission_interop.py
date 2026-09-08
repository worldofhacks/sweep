"""Run an opt-in Python-export-to-Android-admission integration check."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TEST_CLASS = "org.worldofhacks.sweep.bridge.ExportedNavigationAdmissionInteropTest"
_TEST_SOURCE = """package org.worldofhacks.sweep.bridge

import java.nio.file.Files
import java.nio.file.Path
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

class ExportedNavigationAdmissionInteropTest {
    @Test
    fun `exports from the real measured deployment pass Android admission parsing`() {
        val directoryName = requireNotNull(System.getenv(\"SWEEP_EXPORTED_NAVIGATION_ADMISSION\"))
        val keyName = requireNotNull(System.getenv(\"SWEEP_EXPORTED_NAVIGATION_KEY\"))
        val directory = Path.of(directoryName)
        val key = Files.readAllBytes(Path.of(keyName))
        val manifest = Json.parse(
            Files.readAllBytes(directory.resolve(\"navigation-admission.json\")).toString(Charsets.UTF_8),
        ) as JsonObject
        val admission = parseNavigationAdmission(
            manifest,
            \"flight-session\",
            1,
            key,
            readEvidence = { name, maximum ->
                Files.readAllBytes(directory.resolve(name)).also { require(it.size <= maximum) }
            },
            evidenceFile = { name -> directory.resolve(name).toFile() },
        )
        assertNotNull(admission)
        assertEquals(\"wire-navigation-1\", admission?.navigationConfigId)
        assertEquals(6, admission?.approvedEvidenceFiles?.size)
    }
}
"""


def check(android_project: Path) -> None:
    from planner.test_navigation_deployment import _flight_deployment_files
    from tools.export_phone_navigation_admission import export_phone_navigation_admission

    project = android_project.resolve()
    test_source = project / Path(
        "app/src/test/kotlin/org/worldofhacks/sweep/bridge/ExportedNavigationAdmissionInteropTest.kt"
    )
    gradle = project / "gradlew"
    if test_source.exists():
        raise ValueError(f"temporary Kotlin test already exists: {test_source}")
    if not gradle.is_file():
        raise ValueError("android project must contain gradlew")
    with tempfile.TemporaryDirectory(prefix="phone-navigation-admission-") as temporary:
        root = Path(temporary)
        deployment, _, _ = _flight_deployment_files(root)
        key = root / "provenance.key"
        key.write_bytes(b"phone-navigation-provenance-key-0123456789")
        key.chmod(0o600)
        bundle = root / "bundle"
        export_phone_navigation_admission(deployment, 1, key, bundle)
        test_source.write_text(_TEST_SOURCE)
        try:
            environment = {
                **os.environ,
                "SWEEP_EXPORTED_NAVIGATION_ADMISSION": str(bundle),
                "SWEEP_EXPORTED_NAVIGATION_KEY": str(key),
            }
            subprocess.run(
                [
                    str(gradle),
                    ":app:testFakeDebugUnitTest",
                    "--tests",
                    _TEST_CLASS,
                    "--no-daemon",
                    "--rerun-tasks",
                ],
                cwd=project,
                env=environment,
                check=True,
                timeout=600,
            )
        finally:
            test_source.unlink(missing_ok=True)
    print("Measured Python export accepted by Kotlin NavigationAdmissionFile parser.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-project", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        check(arguments.android_project)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
