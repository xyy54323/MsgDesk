import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from msgdesk.services import qq_bridge


class QrSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qr = Path(self.tmp.name) / "qrcode.png"
        self.qr.write_bytes(b"png")
        patcher = mock.patch.object(qq_bridge, "QR_PATH", self.qr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _age(self, seconds):
        stamp = time.time() - seconds
        os.utime(self.qr, (stamp, stamp))

    def test_fresh_qr_reports_remaining_time(self):
        self._age(10)
        snap = qq_bridge._snapshot(None, {"loginPhase": "waiting_qrcode"}, True, False)
        self.assertTrue(snap["qr_ready"])
        self.assertGreaterEqual(snap["qr_left"], qq_bridge.QR_TTL - 11)
        self.assertLessEqual(snap["qr_left"], qq_bridge.QR_TTL - 10)
        self.assertFalse(snap["scanned"])

    def test_stale_qr_is_never_served(self):
        self._age(qq_bridge.QR_TTL + qq_bridge.QR_GRACE + 5)
        snap = qq_bridge._snapshot(None, {}, True, False)
        self.assertFalse(snap["qr_ready"])
        self.assertEqual(snap["qr_left"], 0)

    def test_detached_service_does_not_serve_qr(self):
        self._age(5)
        snap = qq_bridge._snapshot(None, {}, False, False)
        self.assertFalse(snap["qr_ready"])
        self.assertFalse(snap["running"])

    def test_scanned_phase_skips_auto_refresh(self):
        self._age(110)
        with mock.patch.object(qq_bridge, "running", return_value=True), \
                mock.patch.object(qq_bridge, "login_info", return_value=None), \
                mock.patch.object(
                    qq_bridge, "login_phase",
                    return_value={"loginPhase": "qrcode_scanned"},
                ), \
                mock.patch.object(qq_bridge, "_auto_refresh") as auto:
            snap = qq_bridge.state()
        auto.assert_not_called()
        self.assertTrue(snap["scanned"])

    def test_expiring_qr_is_rotated(self):
        self._age(qq_bridge.QR_TTL - 10)
        with mock.patch.object(qq_bridge, "running", return_value=True), \
                mock.patch.object(qq_bridge, "login_info", return_value=None), \
                mock.patch.object(
                    qq_bridge, "login_phase",
                    return_value={"loginPhase": "waiting_qrcode"},
                ), \
                mock.patch.object(qq_bridge, "_auto_refresh", return_value=True) as auto:
            qq_bridge.state()
        auto.assert_called_once()

    def test_comfy_qr_is_left_alone(self):
        self._age(10)
        with mock.patch.object(qq_bridge, "running", return_value=True), \
                mock.patch.object(qq_bridge, "login_info", return_value=None), \
                mock.patch.object(
                    qq_bridge, "login_phase",
                    return_value={"loginPhase": "waiting_qrcode"},
                ), \
                mock.patch.object(qq_bridge, "_auto_refresh") as auto:
            qq_bridge.state()
        auto.assert_not_called()

    def test_snapshot_reports_install_state(self):
        with mock.patch.object(qq_bridge, "installed", return_value=True):
            snap = qq_bridge._snapshot(None, {}, True, False)
        self.assertTrue(snap["installed"])
        with mock.patch.object(qq_bridge, "installed", return_value=False):
            snap = qq_bridge._snapshot(None, {}, False, False)
        self.assertFalse(snap["installed"])


if __name__ == "__main__":
    unittest.main()
