"""Guard: the EA has two version strings and they must never drift.

Found the hard way during acceptance v2: the EA properties dialog shows
`#property version` while the heartbeat reports QP_EA_VERSION. The
dialog said 1.80 while the running build was 1.9.0, because only the
heartbeat constant had been bumped. MQL5 property directives cannot
reference macros, so the two literals sit six lines apart in the source
- this test is the glue that keeps them in sync.
"""

import re
import unittest
from pathlib import Path

_EA_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "mt5"
    / "Experts"
    / "QuantPilotEA"
    / "QuantPilotBridge.mq5"
)

_PROPERTY_VERSION_RE = re.compile(r'#property\s+version\s+"([^"]+)"')
_HEARTBEAT_VERSION_RE = re.compile(r'#define\s+QP_EA_VERSION\s+"([^"]+)"')


def _normalized(version: str) -> str:
    """'1.90' and '1.9.0' are the same release; compare digit sequences."""
    return version.replace(".", "")


class EaVersionSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _EA_SOURCE.read_text(encoding="utf-8")

    def test_both_version_literals_exist(self) -> None:
        """Rename of either constant must fail loudly here, not silently pass."""
        self.assertIsNotNone(
            _PROPERTY_VERSION_RE.search(self.source),
            "#property version missing from QuantPilotBridge.mq5",
        )
        self.assertIsNotNone(
            _HEARTBEAT_VERSION_RE.search(self.source),
            "QP_EA_VERSION missing from QuantPilotBridge.mq5",
        )

    def test_dialog_version_matches_heartbeat_version(self) -> None:
        property_version = _PROPERTY_VERSION_RE.search(self.source).group(1)
        heartbeat_version = _HEARTBEAT_VERSION_RE.search(self.source).group(1)
        self.assertEqual(
            _normalized(property_version),
            _normalized(heartbeat_version),
            f"EA dialog shows {property_version!r} but heartbeats report "
            f"{heartbeat_version!r} - bump #property version too",
        )


if __name__ == "__main__":
    unittest.main()
