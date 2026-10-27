import random
import subprocess
import unittest
from unittest.mock import patch

from backend.audio import detect_silences, pause_chunks


class PauseChunks(unittest.TestCase):
    def test_first_feasible_pause_is_used_even_when_later_pauses_exist(self):
        chunks = pause_chunks(130000, 1000, [(23, 24), (48, 49), (72, 73), (98, 99), (114, 115)])
        self.assertEqual(chunks, [(0, 23500), (23500, 48500), (48500, 72500),
                                  (72500, 98500), (98500, 114500), (114500, 130000)])

    def test_tail_under_60_seconds_is_still_split_at_first_pause(self):
        self.assertEqual(pause_chunks(45000, 1000, [(12, 13), (24, 25)]),
                         [(0, 12500), (12500, 24500), (24500, 45000)])
        self.assertEqual(pause_chunks(20000, 1000, [(9, 11)]), [(0, 10000), (10000, 20000)])
        self.assertEqual(pause_chunks(19000, 1000, [(10, 11)]), [(0, 19000)])

    def test_short_tail_rebalances_without_violating_maximum(self):
        self.assertEqual(pause_chunks(61000, 1000, []), [(0, 51000), (51000, 61000)])
        # The late pause cannot be used if it leaves only two seconds at EOF.
        self.assertEqual(pause_chunks(61000, 1000, [(58, 59)]), [(0, 51000), (51000, 61000)])

    def test_no_pause_continuous_speech_and_subminimum_recordings(self):
        self.assertEqual(pause_chunks(60000, 1000, []), [(0, 60000)])
        self.assertEqual(pause_chunks(120000, 1000, []), [(0, 60000), (60000, 120000)])
        self.assertEqual(pause_chunks(7000, 1000, []), [(0, 7000)])
        self.assertEqual(pause_chunks(0, 1000, []), [])

    def test_random_pauses_always_cover_all_samples_once_within_limits(self):
        rng = random.Random(12)
        for _ in range(300):
            total = rng.randrange(1, 3600 * 16000)
            pauses = sorted((start, start + rng.random() * 5) for start in
                            [rng.random() * total / 16000 for _ in range(50)])
            chunks = pause_chunks(total, 16000, pauses)
            self.assertEqual(chunks[0][0], 0)
            self.assertEqual(chunks[-1][1], total)
            self.assertEqual(sum(end - start for start, end in chunks), total)
            self.assertTrue(all(a[1] == b[0] for a, b in zip(chunks, chunks[1:])))
            self.assertTrue(all(min(total, 160000) <= end - start <= 960000 for start, end in chunks))

    def test_silence_log_parsing_includes_leading_and_unclosed_trailing_pause(self):
        log = "silence_start: 0\nsilence_end: 1.8 | silence_duration: 1.8\nsilence_start: 28.25\nsilence_end: 29\nsilence_start: 64\n"
        with patch("backend.audio.ffmpeg_executable", return_value="ffmpeg"), patch("backend.audio.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", log)):
            self.assertEqual(detect_silences("input.wav", 70), [(0, 1.8), (28.25, 29), (64, 70)])
