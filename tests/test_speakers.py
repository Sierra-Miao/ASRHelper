import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

from asr_config import local_models, models_ready
from backend.app import create_app
from backend.config import SettingsStore
from backend.ensemble import SequentialEnsemble
from backend.service import TranscriptionService
from backend.speakers import MeetingCluster
from speaker_config import selected_speaker, speaker_path
from tests.test_ensemble_and_agent import Fake, RESULT

TEMP = Path(__file__).resolve().parents[1] / "test-results" / "tmp"
TEMP.mkdir(parents=True, exist_ok=True)


class Speakers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_selection_and_cam_fallback_are_independent_of_asr(self):
        self.assertEqual(selected_speaker(self.root), "eres2netv2")
        (self.root / "speaker_settings.json").write_text('{"model":"cam++"}')
        self.assertEqual(selected_speaker(self.root), "cam++")
        (self.root / "speaker_settings.json").write_text('{"model":"not-supported"}')
        with self.assertRaises(ValueError): selected_speaker(self.root)

    def test_ready_checks_do_not_accept_cam_weights_as_eres2net(self):
        model = self.root / "models" / "speakers" / "eres2netv2"
        model.mkdir(parents=True)
        (model / "config.yaml").write_text("model: ProjectERes2NetV2")
        self.assertIsNone(speaker_path("eres2netv2", self.root))
        (model / "model.pt").touch()
        self.assertEqual(speaker_path("eres2netv2", self.root), model)
        self.assertIsNone(speaker_path("cam++", self.root))
        self.assertEqual(local_models("sensevoice", self.root, speaker="eres2netv2")["spk_model"], str(model))

    def test_known_count_works_below_20_windows_and_preserves_a_b_a_identity(self):
        features = np.array([[1, .02], [1, 0], [0, 1], [.02, 1], [1, .01]], dtype=np.float32)
        labels = MeetingCluster()(features, oracle_num=2)
        self.assertEqual(labels.tolist(), [0, 0, 1, 1, 0])
        self.assertEqual(MeetingCluster()(features, oracle_num=1).tolist(), [0] * 5)
        with self.assertRaises(ValueError): MeetingCluster()(features, oracle_num=6)
        with self.assertRaises(ValueError): MeetingCluster()(features * float("nan"), oracle_num=2)

    def test_hour_scale_clustering_keeps_global_ids_and_exact_known_count(self):
        rng = np.random.default_rng(2026)
        # 4800 windows at .75 second shift: one hour of continuous speech.
        centers = np.eye(4, 192, dtype=np.float32)
        identities = np.repeat(np.arange(4), 1200)
        features = centers[identities] + rng.normal(0, .008, (4800, 192)).astype(np.float32)
        labels = MeetingCluster()(features, oracle_num=4)
        self.assertEqual(len(labels), 4800)
        self.assertEqual(len(set(labels)), 4)
        self.assertEqual(labels.tolist(), identities.tolist())

    def test_longer_recording_is_deterministic_without_compressing_away_short_turns(self):
        rng = np.random.default_rng(0)
        features = np.concatenate([np.tile(center, (4000, 1)) for center in np.eye(3)])
        features += rng.normal(0, .005, features.shape)
        cluster = MeetingCluster()
        labels = cluster(features, oracle_num=3)
        self.assertEqual(labels.tolist(), np.repeat(np.arange(3), 4000).tolist())
        np.testing.assert_array_equal(labels, cluster(features, oracle_num=3))

    def test_outlier_does_not_consume_a_cluster_and_merge_two_main_speakers(self):
        # Average linkage at K=2 isolates the remote outlier and merges A/B.
        # The objective over all windows should instead distinguish A and B.
        rng = np.random.default_rng(123)
        a = np.array([1., .35, 0])
        b = np.array([1., -.35, 0])
        features = np.concatenate([a + rng.normal(0, .015, (100, 3)),
                                   b + rng.normal(0, .015, (100, 3)), [[-1., 0, 0]]])
        labels = MeetingCluster()(features, oracle_num=2)
        self.assertEqual(len(set(labels[:100])), 1)
        self.assertEqual(len(set(labels[100:200])), 1)
        self.assertNotEqual(labels[0], labels[100])

    def test_ensemble_passes_the_same_speaker_backend_to_both_models(self):
        with patch("transcribe.load_pipeline", return_value=Fake()) as loader:
            pipeline = SequentialEnsemble(speaker="eres2netv2")
            self.assertEqual(pipeline.generate("sample.wav"), RESULT)
        self.assertEqual([call.kwargs["speaker"] for call in loader.call_args_list], ["eres2netv2", "eres2netv2"])

    def test_web_switch_persists_only_after_success_and_does_not_load_asr(self):
        with patch("backend.service.hardware_plan", return_value={"cpu_threads": 2}):
            service = TranscriptionService(self.root)
        with TestClient(create_app(service, SettingsStore(self.root / "settings.json"))) as client:
            with patch("backend.service.speaker_path", return_value=None), patch("backend.speakers.prepare_speaker") as prepare:
                denied = client.post("/api/speakers/activate", json={"model": "cam++"})
                self.assertEqual(denied.status_code, 409)
                prepare.assert_not_called()
                started = client.post("/api/speakers/activate", json={"model": "cam++", "allow_download": True})
                self.assertEqual(started.status_code, 202)
                for _ in range(100):
                    if service.model_action["status"] != "loading": break
                    time.sleep(.01)
            self.assertEqual(selected_speaker(self.root), "cam++")
            self.assertIsNone(service.model)
            self.assertEqual(client.get("/api/models").json()["speaker"], "cam++")
            with patch("backend.speakers.prepare_speaker", side_effect=ValueError("download failed")), self.assertLogs(level="ERROR"):
                service._activate_speaker("eres2netv2")
            self.assertEqual(selected_speaker(self.root), "cam++")
            self.assertEqual(service.speaker, "cam++")
            self.assertEqual(service.model_action["status"], "failed")
            service.model_action["status"] = "loading"
            self.assertEqual(client.post("/api/speakers/activate", json={"model": "eres2netv2"}).status_code, 409)
