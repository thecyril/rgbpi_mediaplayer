"""Platform auto-detection (core/platform.py).

  python3 -m unittest discover -s tests -p 'test_*.py'
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dvdplayer_python.core import platform as P  # noqa: E402


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._root = mock.patch.object(P, "ROOT", self.root)
        self._root.start()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("DVDPLAYER_PLATFORM", "DVDPLAYER_DISPLAY_W", "DVDPLAYER_DISPLAY_H"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        self._root.stop()

    def _mark(self, rel):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def test_rgbpi_os4_root_is_os4(self):
        self._mark("opt/rgbpi/ui/data/timings.dat")
        self.assertEqual(P.detect().name, P.OS4)

    def test_empty_root_defaults_to_os4(self):
        self.assertEqual(P.detect().name, P.OS4)

    def test_replay_binary_means_replayos(self):
        self._mark("opt/replay/replay")
        plat = P.detect()
        self.assertEqual(plat.name, P.REPLAYOS)
        self.assertEqual(plat.detected_by, "/opt/replay/replay")

    def test_replay_dir_without_binary_is_not_enough(self):
        (self.root / "opt" / "replay").mkdir(parents=True)
        self.assertEqual(P.detect().name, P.OS4)

    def test_env_forces_either_way(self):
        self._mark("opt/replay/replay")
        os.environ["DVDPLAYER_PLATFORM"] = "os4"
        self.assertEqual(P.detect().name, P.OS4)
        os.environ["DVDPLAYER_PLATFORM"] = "ReplayOS"
        (self.root / "opt" / "replay" / "replay").unlink()
        self.assertEqual(P.detect().name, P.REPLAYOS)

    def test_unknown_env_value_falls_back_to_detection(self):
        os.environ["DVDPLAYER_PLATFORM"] = "lakka"
        self.assertEqual(P.detect().name, P.OS4)

    def test_os4_profile_has_no_replayos_behaviour(self):
        plat = P.detect()
        self.assertIsNone(plat.ui_display_size)
        self.assertFalse(plat.release_display_for_playback)
        self.assertFalse(plat.close_audio_mixer)
        self.assertEqual(plat.audio_outputs, ("jack", "optical_5_1"))
        self.assertIsNone(plat.mpv_sws_scaler)

    def test_replayos_display_size_from_launcher_env(self):
        self._mark("opt/replay/replay")
        os.environ["DVDPLAYER_DISPLAY_W"] = "2560"
        os.environ["DVDPLAYER_DISPLAY_H"] = "240"
        self.assertEqual(P.detect().ui_display_size, (2560, 240))


if __name__ == "__main__":
    unittest.main()
