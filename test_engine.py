import math
import json
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from engine import (Cancelled, Options, ProcessingControl, binary, Ramp, crop_plan,
                    filter_graph, metadata, process_video, reserve_outputs, run_ffmpeg, speed_plan, plan_duration, segment_time,
                    segment_fps)


def duration(plan):
    return plan_duration(plan)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.options = Options(Path('.'))

    def test_new_script_keeps_slow_for_long_hfr(self):
        self.assertEqual(speed_plan(30, 239.76, self.options), [(0, 30, 10)])

    def test_hfr_short(self):
        self.assertEqual(speed_plan(1, 240, self.options), [(0, 1, 10)])

    def test_sq_short_keeps_existing_slow_motion(self):
        self.assertEqual(speed_plan(10, 23.976, self.options), [(0, 10, 1)])

    def test_24fps_long_is_unchanged_by_default(self):
        self.assertEqual(speed_plan(30, 24, self.options), [(0, 30, 1)])

    def test_regular_24fps(self):
        self.assertEqual(speed_plan(30, 24, replace(self.options, sq_detection=False)), [(0, 30, 1)])

    def test_manual_speed(self):
        self.assertEqual(speed_plan(30, 240, replace(self.options, speed=2)), [(0, 30, .5)])

    def test_ramp_detects_hfr_and_sq(self):
        for fps, multiplier in [(60, 2.5), (120, 5), (240, 10)]:
            plan = speed_plan(20, fps, replace(self.options, ramp=True))
            self.assertAlmostEqual(plan[0][2].start_speed, 1)
            self.assertAlmostEqual(plan[0][2].end_speed, 1 / multiplier)
            self.assertAlmostEqual(plan[-1][2], multiplier)
        plan = speed_plan(30, 23.976, replace(self.options, ramp=True, sq_factor=5))
        self.assertEqual(plan[0][2].start_speed, 5)
        self.assertEqual(plan[0][2].end_speed, 1)
        self.assertEqual(plan[-1][2], 1)
        ordinary = speed_plan(30, 24, replace(self.options, ramp=True, sq_detection=False))
        self.assertEqual(ordinary, [(0, 30, 1)])

    def test_independent_fast_and_slow_speeds(self):
        options = replace(self.options, ramp=True, fast_speed=.75, speed=.25, repetitions=2)
        plan = speed_plan(20, 120, options)
        self.assertAlmostEqual(plan[0][2].start_speed, .75)
        self.assertAlmostEqual(plan[0][2].end_speed, .25)
        self.assertAlmostEqual(plan[1][2], 4)
        self.assertAlmostEqual(plan[2][2].end_speed, .75)
        self.assertAlmostEqual(plan[-1][2], 1 / .75)
        sq_plan = speed_plan(30, 24, replace(options, sq_factor=5))
        self.assertAlmostEqual(sq_plan[0][2].start_speed, 3.75)
        self.assertAlmostEqual(sq_plan[0][2].end_speed, .25)

    def test_fast_duration_before_each_ramp(self):
        options = replace(self.options, ramp=True, fast_speed=.8, speed=.25,
                          fast_duration=2, ramp_duration=1, slow_duration=1, repetitions=2)
        plan = speed_plan(20, 120, options)
        self.assertAlmostEqual(segment_time(plan[0][1], plan[0][2]), 2)
        self.assertAlmostEqual(plan[0][2], 1 / .8)
        self.assertIsInstance(plan[1][2], Ramp)
        self.assertAlmostEqual(segment_time(plan[4][1], plan[4][2]), 2)
        self.assertIsInstance(plan[5][2], Ramp)

    def test_crop_across_phase_boundaries(self):
        plan = [(0, 2, 2), (2, 4, .5)]
        self.assertEqual(crop_plan(plan, 5), [(0, 2, 2), (2, 2, .5)])
        self.assertEqual(crop_plan(plan, 3, True), [(1.5, .5, 2), (2, 4, .5)])

    def test_limit_does_not_extend(self):
        plan = [(0, 3, .5)]
        self.assertEqual(crop_plan(plan, 10, True), plan)

    def test_no_overwrite_or_sanitized_collisions(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / 'ABED01.mp4').touch()
            result = reserve_outputs([Path('a-b.mov'), Path('a_b.mp4')], directory)
            self.assertEqual([p.name for p in result], ['ABED01_2.mp4', 'ABED01_3.mp4'])
            expected = reserve_outputs([Path('a-b.mov'), Path('a_b.mp4')], directory, avoid_existing=False)
            self.assertEqual([p.name for p in expected], ['ABED01.mp4', 'ABED01_2.mp4'])

    def test_fade_optional_and_cover_excluded(self):
        self.assertNotIn('fade=', filter_graph([(0, 1, 1)], self.options))
        graph = filter_graph([(0, 1, 1)], replace(self.options, fade=.5))
        self.assertIn("enable='gte(n,1)'", graph)


class RampTests(unittest.TestCase):
    def setUp(self):
        self.options = Options(Path('.'), ramp=True, ramp_duration=1)

    def test_single_block_slow_until_end(self):
        plan = speed_plan(10, 240, self.options)
        self.assertEqual(len(plan), 2)
        self.assertIsInstance(plan[0][2], Ramp)
        self.assertEqual(plan[-1][2], 10)
        self.assertAlmostEqual(plan[-1][0] + plan[-1][1], 10)
        self.assertAlmostEqual(segment_time(plan[0][1], plan[0][2]), 1)

    def test_repeated_blocks_and_source_coverage(self):
        for count in (2, 3, 4):
            options = replace(self.options, repetitions=count, slow_duration=2)
            plan = speed_plan(20, 120, options)
            self.assertEqual(sum(isinstance(f, Ramp) for _, _, f in plan), 2 * count)
            self.assertEqual(plan[-1][2], 1)
            self.assertAlmostEqual(sum(p[1] for p in plan), 20)
            cursor = 0
            for start, length, _ in plan:
                self.assertAlmostEqual(start, cursor)
                cursor += length

    def test_short_clip_truncates_transition(self):
        plan = speed_plan(.1, 240, self.options)
        self.assertEqual(len(plan), 1)
        self.assertAlmostEqual(plan[0][1], .1)
        self.assertLess(plan_duration(plan), 1)

    def test_crop_inside_transition(self):
        plan = speed_plan(10, 240, self.options)
        for keep_end in (False, True):
            cropped = crop_plan(plan, .5, keep_end)
            self.assertAlmostEqual(plan_duration(cropped), .5)
        cropped = crop_plan(plan[:1], .5, True)
        curve = cropped[0][2]
        self.assertIsInstance(curve, Ramp)
        self.assertAlmostEqual(curve.end_speed, .1)
        self.assertAlmostEqual(plan_duration(cropped), .5)

    def test_transition_mapping_is_monotonic(self):
        for start, end in [(1, .1), (.1, 1)]:
            curve = Ramp(start, end, 2)
            times = [curve.output_time(curve.source_time(t / 10)) for t in range(21)]
            for i, value in enumerate(times):
                self.assertAlmostEqual(value, i / 10)

    def test_invalid_repetitions_are_rejected(self):
        with self.assertRaises(ValueError):
            speed_plan(10, 240, replace(self.options, repetitions=5))

    def test_ramp_minimum_is_point_two_seconds(self):
        plan = speed_plan(10, 120, replace(self.options, ramp_duration=.2))
        self.assertAlmostEqual(segment_time(plan[0][1], plan[0][2]), .2)
        with self.assertRaises(ValueError):
            speed_plan(10, 120, replace(self.options, ramp_duration=.1))

    def test_adaptive_rates_follow_segment_and_source(self):
        self.assertEqual(segment_fps(10, 240, self.options), 24)
        self.assertEqual(segment_fps(1, 240, self.options), 60)
        self.assertEqual(segment_fps(1, 30, self.options), 30)
        self.assertEqual(segment_fps(Ramp(1, .1, 1), 240, replace(self.options, adaptive_fps=120)), 120)
        self.assertEqual(segment_fps(Ramp(1, .1, 1), 240, replace(self.options, adaptive_fps=None)), 24)


class PauseTests(unittest.TestCase):
    def exercise_pause(self, cancel):
        control = ProcessingControl()
        errors = []
        def render():
            try:
                run_ffmpeg(['-re', '-f', 'lavfi', '-i', 'testsrc2=s=128x96:r=24:d=2',
                            '-f', 'null', '-'], 2, control, lambda _: None)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=render)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            process = None
            while time.monotonic() < deadline:
                with control._lock:
                    process = next(iter(control._processes), None)
                if process:
                    break
                time.sleep(.02)
            self.assertIsNotNone(process)
            control.set_paused(True)
            time.sleep(.2)
            state = subprocess.check_output(['ps', '-o', 'state=', '-p', str(process.pid)], text=True)
            self.assertIn('T', state, 'FFmpeg deve estar suspenso de verdade.')
            self.assertTrue(thread.is_alive())
            if cancel:
                control.set()
            else:
                control.set_paused(False)
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            if cancel:
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], Cancelled)
            else:
                self.assertEqual(errors, [])
        finally:
            control.set()
            thread.join(timeout=5)

    def test_pause_and_resume_real_ffmpeg(self):
        self.exercise_pause(False)

    def test_cancel_while_paused(self):
        self.exercise_pause(True)


class RenderTests(unittest.TestCase):
    """Integração real: FFmpeg e hardware VideoToolbox do Mac."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.source = self.folder / 'source.mp4'
        subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=white:s=128x96:r=24:d=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(self.source)], check=True)
        self.options = Options(self.folder, speed=1, watermark=False, codec='h264')

    def render(self, options, name='result.mp4'):
        target = self.folder / name
        values = []
        process_video(self.source, target, options, threading.Event(), values.append, lambda _: None)
        self.assertEqual(values[-1], 1)
        return target

    def test_cover_frame_then_fade(self):
        target = self.render(replace(self.options, fade=.5))
        frames = subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(target),
                                 '-frames:v', '3', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                                check=True, capture_output=True).stdout
        size = 128 * 96
        levels = [sum(frames[i * size:(i + 1) * size]) / size for i in range(3)]
        self.assertGreater(levels[0], 230)
        self.assertLess(levels[1], 15)
        self.assertGreater(levels[2], levels[1])

    def test_video_fades_to_black_at_end(self):
        target = self.render(replace(self.options, fade_out_video=True, fade_out_video_duration=.4), 'fade-out.mp4')
        frames = subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(target),
                                 '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                                check=True, capture_output=True).stdout
        size = 128 * 96
        levels = [sum(frames[i:i + size]) / size for i in range(0, len(frames), size)]
        self.assertGreater(levels[0], 230)
        self.assertGreater(levels[24], levels[42])
        self.assertLess(levels[-1], 35)

    def test_short_video_preserves_cover_with_both_fades(self):
        target = self.render(replace(self.options, fade=.2, fade_out_video=True, limit=.5), 'short-fade.mp4')
        frame = subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(target), '-frames:v', '1',
                                '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                               check=True, capture_output=True).stdout
        self.assertGreater(sum(frame) / len(frame), 230)

    def test_audio_fades_at_end_of_short_track(self):
        audio = self.folder / 'short.wav'
        subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1.5', str(audio)], check=True)
        target = self.render(replace(self.options, music=audio, fade_out_audio=True, fade_out_audio_duration=.5), 'audio-fade.mp4')
        samples = subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(target),
                                  '-map', '0:a:0', '-ar', '8000', '-ac', '1',
                                  '-f', 's16le', '-'], check=True, capture_output=True).stdout
        import array
        values = array.array('h', samples)
        rms = lambda start, end: math.sqrt(sum(v * v for v in values[int(start * 8000):int(end * 8000)]) / int((end-start) * 8000))
        self.assertGreater(rms(.4, .6), 1000)
        self.assertLess(rms(1.7, 1.78), rms(.4, .6) * .25)

    def test_default_hevc_and_watermark(self):
        target = self.render(replace(self.options, codec='hevc', watermark=True), 'default.mp4')
        self.assertAlmostEqual(metadata(target)[0], 2, delta=1 / 24)

    def test_overwrite_is_atomic_and_requires_authorization(self):
        target = self.folder / 'existing.mp4'
        target.write_bytes(b'original')
        with self.assertRaises(FileExistsError):
            process_video(self.source, target, self.options, threading.Event(), lambda _: None, lambda _: None)
        self.assertEqual(target.read_bytes(), b'original')
        with patch('engine.run_ffmpeg', side_effect=RuntimeError('falha simulada')):
            with self.assertRaises(RuntimeError):
                process_video(self.source, target, self.options, threading.Event(), lambda _: None,
                              lambda _: None, overwrite=True)
        self.assertEqual(target.read_bytes(), b'original')
        process_video(self.source, target, self.options, threading.Event(), lambda _: None,
                      lambda _: None, overwrite=True)
        self.assertAlmostEqual(metadata(target)[0], 2, delta=1 / 24)

    def test_trim_both_directions(self):
        for keep_end in (False, True):
            target = self.render(replace(self.options, limit=.5, keep_end=keep_end), f'trim-{keep_end}.mp4')
            length, fps = metadata(target)
            self.assertAlmostEqual(length, .5, delta=1 / 24)
            self.assertAlmostEqual(fps, 24)

    def test_multiphase_render(self):
        self.source = self.folder / 'hfr.mp4'
        subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=s=128x96:r=240:d=3', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(self.source)], check=True)
        for count in (1, 2, 4):
            options = replace(self.options, speed=None, ramp=True, ramp_duration=.5,
                              slow_duration=1, repetitions=count)
            plan = speed_plan(3, 240, options)
            self.assertGreater(len(plan), 1)
            target = self.render(options, f'ramp-{count}.mp4')
            self.assertAlmostEqual(metadata(target)[0], plan_duration(plan), delta=.15)
        for keep_end in (False, True):
            options = replace(self.options, speed=.25, ramp=True, ramp_duration=1,
                              limit=.5, keep_end=keep_end)
            target = self.render(options, f'ramp-crop-{keep_end}.mp4')
            self.assertAlmostEqual(metadata(target)[0], .5, delta=.08)

    def test_hfr_120_and_sq_24_ramp_exports(self):
        for fps, length, factor in [(120, 3, 10), (24, 10, 5)]:
            self.source = self.folder / f'source-{fps}.mp4'
            subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                            f'testsrc2=s=128x96:r={fps}:d={length}', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', str(self.source)], check=True)
            options = replace(self.options, speed=None, sq_detection=True, sq_factor=factor,
                              ramp=True, ramp_duration=.2, fast_speed=.75, fast_duration=1, limit=3)
            target = self.render(options, f'export-{fps}.mp4')
            self.assertAlmostEqual(metadata(target)[0], 3, delta=.15)

    def test_vfr_timestamps_and_fixed_24_alternative(self):
        self.source = self.folder / 'hfr240.mp4'
        subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=s=128x96:r=240:d=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(self.source)], check=True)
        for codec in ('h264', 'hevc'):
            for cap in (60, 120, 240, None):
                options = replace(self.options, codec=codec, speed=None, ramp=True,
                                  ramp_duration=.5, limit=2, adaptive_fps=cap)
                target = self.render(options, f'vfr-{codec}-{cap}.mp4')
                data = json.loads(subprocess.check_output([
                    binary('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                    '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time',
                    '-of', 'json', str(target)], text=True))
                times = [float(frame['best_effort_timestamp_time']) for frame in data['frames']]
                intervals = [b - a for a, b in zip(times, times[1:])]
                self.assertTrue(all(delta > 0 for delta in intervals))
                slow = [b - a for a, b in zip(times, times[1:]) if a > .65 and b < 1.8]
                self.assertTrue(slow)
                for delta in slow:
                    self.assertAlmostEqual(delta, 1 / 24, delta=.00002)
                if cap:
                    fast = [b - a for a, b in zip(times, times[1:]) if b < .4]
                    self.assertTrue(fast)
                    for delta in fast:
                        self.assertAlmostEqual(delta, 1 / cap, delta=.00002)
                else:
                    for delta in intervals:
                        self.assertAlmostEqual(delta, 1 / 24, delta=.00002)
                self.assertAlmostEqual(metadata(target)[0], 2, delta=.09)

    def test_music_delay_and_duration(self):
        audio = self.folder / 'audio.wav'
        subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=3', str(audio)], check=True)
        target = self.render(replace(self.options, music=audio))
        self.assertAlmostEqual(metadata(target)[0], 2, delta=.05)
        samples = subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(target),
                                  '-map', '0:a:0', '-ar', '8000', '-ac', '1',
                                  '-f', 's16le', '-'], check=True, capture_output=True).stdout
        import array
        values = array.array('h', samples)
        rms = lambda segment: math.sqrt(sum(v * v for v in segment) / len(segment))
        self.assertLess(rms(values[:1600]), 10)
        self.assertGreater(rms(values[4000:6000]), 100)

    def test_cancel_does_not_publish(self):
        stop = threading.Event()
        stop.set()
        target = self.folder / 'cancelled.mp4'
        with self.assertRaises(Cancelled):
            process_video(self.source, target, self.options, stop, lambda _: None, lambda _: None)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
