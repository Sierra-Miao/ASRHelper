import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import torch

from backend.diarization import TurnFirstPipeline, asr_chunks, speaker_turns, speaker_windows
from backend.speakers import configure_clustering


class FineDiarization(unittest.TestCase):
    def test_short_region_is_not_zero_padded_and_tail_is_covered(self):
        regions = [[0, 2000], [2400, 2700], [3000, 4623]]
        windows = speaker_windows(regions, 4623 * 16)
        self.assertIn((2.4, 2.7), windows)
        self.assertEqual(windows[-1][1], 4.623)
        self.assertTrue(all(b > a and b - a <= 1.5 + 1e-6 for a, b in windows))
        self.assertTrue(all(any(a >= start / 1000 and b <= end / 1000 for start, end in regions) for a, b in windows))

    def test_sub_700ms_turn_keeps_its_speaker(self):
        windows = [(0, 1.5), (.5, 2), (1, 2.5), (1.5, 3), (2, 3.5)]
        turns = speaker_turns([[0, 3500]], windows, [0, 0, 1, 0, 0])
        self.assertEqual(turns, [{"start": 0, "end": 1500, "spk": 0},
            {"start": 1500, "end": 2000, "spk": 1}, {"start": 2000, "end": 3500, "spk": 0}])

    def test_short_isolated_reply_and_silence_are_preserved(self):
        regions = [[0, 1500], [2000, 2300], [3000, 4500]]
        windows = speaker_windows(regions, 4500 * 16)
        turns = speaker_turns(regions, windows, [0, 1, 0])
        self.assertEqual([turn["spk"] for turn in turns], [0, 1, 0])
        self.assertEqual(turns[1], {"start": 2000, "end": 2300, "spk": 1})
        self.assertEqual(turns[-1]["end"], 4500)

    def test_long_turn_splitting_includes_last_millisecond(self):
        chunks = asr_chunks([{"start": 45, "end": 60046, "spk": 2}])
        self.assertEqual(chunks[0]["start"], 45)
        self.assertEqual(chunks[-1]["end"], 60046)
        self.assertTrue(all(20000 <= row["end"] - row["start"] <= 30000 for row in chunks))
        self.assertEqual(chunks[0]["end"], chunks[1]["start"])
        self.assertEqual(chunks[1]["end"], chunks[2]["start"])
        self.assertEqual([row["spk"] for row in chunks], [2, 2, 2])

    def test_asr_receives_separate_audio_for_each_speaker_without_native_smoothing(self):
        vad, spk, asr, punc = object(), object(), object(), object()
        asr_crops, speaker_crops = [], []
        pipeline = SimpleNamespace(vad_model=vad, spk_model=spk, model=asr, punc_model=punc,
            kwargs={"device": "cpu"}, vad_kwargs={}, spk_kwargs={}, punc_kwargs={})
        def inference(audio, model, **kwargs):
            if model is vad:
                return [{"value": [[0, 1500], [2000, 2300], [3000, 4500]]}]
            if model is spk:
                speaker_crops.extend(len(crop) for crop in audio)
                return [{"spk_embedding": torch.tensor([[1., 0.]])} for _ in audio]
            if model is asr:
                asr_crops.append(len(audio))
                return [{"text": f"<|zh|>片段{len(asr_crops)}"}]
            if model is punc:
                return [{"text": audio + "。"}]
            raise AssertionError("unexpected model")
        pipeline.inference = inference
        def cluster(features, oracle_num):
            self.assertEqual(oracle_num, 2)
            self.assertEqual(tuple(features.shape), (3, 2))
            return np.array([0, 1, 0])
        pipeline.cb_model = cluster
        with patch("funasr.utils.load_utils.load_audio_text_image_video", return_value=torch.zeros(4500 * 16)):
            result = TurnFirstPipeline(pipeline).generate("meeting.wav", preset_spk_num=2)[0]
        self.assertEqual(asr_crops, [24000, 4800, 24000])
        self.assertEqual(speaker_crops, [24000, 4800, 24000])
        self.assertEqual([row["spk"] for row in result["sentence_info"]], [0, 1, 0])
        self.assertEqual(result["speaker_diagnostics"]["short_turns"], 1)
        self.assertEqual(result["sentence_info"][1]["text"], "片段2。")
        self.assertEqual(pipeline.kwargs, {"device": "cpu"})

    def test_no_speech_and_incomplete_embeddings(self):
        pipeline = SimpleNamespace(vad_model=object(), spk_model=object(), vad_kwargs={}, spk_kwargs={})
        with patch("funasr.utils.load_utils.load_audio_text_image_video", return_value=torch.zeros(16000)):
            pipeline.inference = lambda *args, **kwargs: [{"value": []}]
            self.assertEqual(TurnFirstPipeline(pipeline).generate("silent.wav")[0]["sentence_info"], [])
            pipeline.inference = lambda *args, **kwargs: ([{"value": [[0, 1000]]}] if kwargs["model"] is pipeline.vad_model else [])
            with self.assertRaisesRegex(ValueError, "数量不完整"):
                TurnFirstPipeline(pipeline).generate("meeting.wav")

    def test_cam_keeps_native_pipeline_eres_uses_turn_first(self):
        pipeline = SimpleNamespace()
        self.assertIs(configure_clustering(pipeline, "cam++"), pipeline)
        self.assertIs(configure_clustering(pipeline, "eres2netv2").pipeline, pipeline)
