"""Verificações da interface e de um lote iniciado pela janela."""
import os
import json
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from editor import STYLE, Batch, ExportJob, FolderScan, Window
from engine import Options, binary


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.preferences_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.preferences_dir.cleanup)
        self.preferences_file = str(Path(self.preferences_dir.name) / 'preferences.ini')
        self.window = Window(QSettings(self.preferences_file, QSettings.IniFormat))
        self.addCleanup(self.window.close)

    def reopen(self):
        window = Window(QSettings(self.preferences_file, QSettings.IniFormat))
        self.addCleanup(window.close)
        return window

    def test_preferences_round_trip(self):
        w = self.window
        w.fade.setChecked(True)
        w.fade_out_video.setChecked(True)
        w.fade_out_audio.setChecked(True)
        w.fade_out_video_time.setValue(.4)
        w.fade_out_audio_time.setValue(.7)
        w.fade_time.setValue(1)
        w.ramp.setChecked(True)
        w.fast_speed.setCurrentIndex(3)  # 0,75×
        w.fast_time.setValue(2.5)
        w.repetitions.setCurrentIndex(2)
        w.ramp_time.setValue(3.5)
        w.slow_time.setValue(4.5)
        w.limit.setChecked(True)
        w.seconds.setText('12,5')
        w.cut.setCurrentIndex(1)
        w.fluency.setCurrentIndex(1)
        w.speed.setCurrentIndex(2)
        w.workers.setCurrentIndex(2)
        w.codec.setCurrentIndex(1)
        w.watermark.setChecked(False)
        w.finish_action.setCurrentIndex(0)
        w.folder.setText(str(Path(self.preferences_dir.name) / 'saida'))
        w.music.setChecked(True)
        w.music_path = Path(self.preferences_dir.name) / 'trilha.wav'
        w.music_path.touch()
        w.save_preferences()
        reopened = self.reopen()
        for name, widget in w.preference_widgets().items():
            other = reopened.preference_widgets()[name]
            if hasattr(widget, 'isChecked'):
                self.assertEqual(other.isChecked(), widget.isChecked(), name)
            elif hasattr(widget, 'currentData'):
                self.assertEqual(other.currentData(), widget.currentData(), name)
            elif hasattr(widget, 'value'):
                self.assertEqual(other.value(), widget.value(), name)
            else:
                self.assertEqual(other.text(), widget.text(), name)
        self.assertEqual(reopened.music_path, w.music_path)
        self.assertTrue(reopened.fade_time.isEnabled())
        self.assertTrue(reopened.slow_time.isEnabled())
        self.assertTrue(reopened.sq_factor.isEnabled())
        self.assertEqual(reopened.files, [])

    def test_preferences_autosave(self):
        self.window.fluency.setCurrentIndex(2)
        QTest.qWait(450)
        reader = QSettings(self.preferences_file, QSettings.IniFormat)
        self.assertEqual(json.loads(reader.value('choices/v1'))['fluency'], 240)

    def test_sq_migration_enables_once_and_respects_later_choice(self):
        prefs = self.window.preferences
        prefs.remove('migration/sq_auto_v1')
        prefs.setValue('choices/v1', json.dumps({'sq': False, 'fluency': 120}))
        prefs.sync()
        migrated = self.reopen()
        self.assertTrue(migrated.sq.isChecked())
        self.assertEqual(migrated.fluency.currentData(), 120)
        migrated.sq.setChecked(False)
        migrated.save_preferences()
        reopened = self.reopen()
        self.assertFalse(reopened.sq.isChecked())

    def test_invalid_preferences_and_missing_music(self):
        preferences = self.window.preferences
        preferences.setValue('choices/v1', '{invalid')
        defaults = self.reopen()
        self.assertEqual(defaults.fluency.currentData(), 60)
        preferences.setValue('choices/v1', json.dumps({
            'music': True, 'music_path': str(Path(self.preferences_dir.name) / 'ausente.mp3'),
            'repetitions': 99, 'slow_time': -1, 'seconds': 'abc', 'ramp': True}))
        preferences.sync()
        restored = self.reopen()
        self.assertEqual(restored.repetitions.currentData(), 1)
        self.assertEqual(restored.slow_time.value(), 2)
        self.assertEqual(restored.seconds.text(), '15')
        self.assertTrue(restored.music.isChecked())
        self.assertIn('não encontrada', restored.music_button.text())
        self.assertFalse(restored.slow_time.isEnabled())

    def test_conditional_controls(self):
        w = self.window
        self.assertFalse(w.fade_time.isEnabled())
        w.fade.setChecked(True)
        self.assertTrue(w.fade_time.isEnabled())
        w.music.setChecked(True)
        self.assertTrue(w.music_button.isEnabled())
        w.limit.setChecked(True)
        self.assertTrue(w.seconds.isEnabled())
        self.assertTrue(w.cut.isEnabled())
        w.speed.setCurrentIndex(1)
        self.assertTrue(w.sq.isEnabled())
        self.assertTrue(w.sq_factor.isEnabled())

    def test_ramp_controls(self):
        w = self.window
        self.assertFalse(w.fast_speed.isEnabled())
        self.assertFalse(w.fast_time.isEnabled())
        self.assertFalse(w.ramp_time.isEnabled())
        self.assertEqual(w.repetitions.currentData(), 1)
        w.ramp.setChecked(True)
        self.assertTrue(w.fast_speed.isEnabled())
        self.assertTrue(w.fast_time.isEnabled())
        self.assertTrue(w.ramp_time.isEnabled())
        self.assertTrue(w.repetitions.isEnabled())
        self.assertFalse(w.slow_time.isEnabled())
        w.repetitions.setCurrentIndex(1)
        self.assertTrue(w.slow_time.isEnabled())
        w.ramp.setChecked(False)
        self.assertFalse(w.slow_time.isEnabled())
        self.assertFalse(w.repetitions.isEnabled())

    def test_time_sliders_sync_and_restore_older_choices(self):
        w = self.window
        w.ramp.setChecked(True)
        w.repetitions.setCurrentIndex(1)
        w.fast_time.slider.setValue(23)
        self.assertAlmostEqual(w.fast_time.value(), 2.3)
        w.slow_time.setValue(135.6)
        self.assertEqual(w.slow_time.slider.value(), 1356)
        w.fade_out_video.setChecked(True)
        w.fade_out_video_time.spin.setValue(.7)
        self.assertEqual(w.fade_out_video_time.slider.value(), 7)
        w.preferences.setValue('choices/v1', json.dumps({'fade': True, 'fade_time': .2,
                                                          'ramp': True, 'ramp_time': 2.5}))
        w.preferences.sync()
        restored = self.reopen()
        self.assertEqual(restored.fade_time.value(), .2)
        self.assertEqual(restored.ramp_time.value(), 2.5)
        restored.ramp_time.setValue(.2)
        self.assertEqual(restored.ramp_time.value(), .2)
        self.assertEqual(restored.ramp_time.slider.minimum(), 2)

    def test_conflicts_apply_to_all_and_keep_stable_names(self):
        with tempfile.TemporaryDirectory() as folder:
            w = self.window
            output = Path(folder)
            w.files = [output / 'a-b.mov', output / 'a_b.mov']
            for name in ('ABED01.mp4', 'ABED01_2.mp4'):
                (output / name).touch()
            for action in ('overwrite', 'skip'):
                with patch.object(w, 'ask_conflict', return_value=(action, True)) as ask:
                    jobs = w.prepare_jobs(output)
                self.assertEqual(ask.call_count, 1)
                self.assertEqual([job.action for job in jobs], [action, action])
                self.assertEqual([job.target.name for job in jobs], ['ABED01.mp4', 'ABED01_2.mp4'])
            with patch.object(w, 'ask_conflict', side_effect=[('skip', False), ('overwrite', False)]) as ask:
                self.assertEqual([j.action for j in w.prepare_jobs(output)], ['skip', 'overwrite'])
                self.assertEqual(ask.call_count, 2)
            with patch.object(w, 'ask_conflict', return_value=(None, False)):
                self.assertIsNone(w.prepare_jobs(output))

    def test_conflict_dialog_buttons_and_checkbox(self):
        for button_text, action in [('Sobrescrever', 'overwrite'), ('Pular', 'skip'), ('Cancelar lote', None)]:
            def choose(text=button_text):
                dialog = self.app.activeModalWidget()
                dialog.checkBox().setChecked(True)
                next(button for button in dialog.buttons() if button.text() == text).click()
            QTimer.singleShot(0, choose)
            self.assertEqual(self.window.ask_conflict(Path('/tmp/CLIPED01.mp4')), (action, True))

    def test_start_delay_is_cancellable(self):
        with tempfile.TemporaryDirectory() as folder:
            w = self.window
            w.files = [Path(folder) / 'clip.mov']
            w.folder.setText(str(Path(folder) / 'out'))
            w.start_batch()
            self.assertEqual(w.status.text(), 'Iniciando...')
            self.assertTrue(w.start_timer.isActive())
            self.assertEqual(w.start_timer.interval(), 2000)
            self.assertIsNone(w.worker)
            self.assertFalse(w.start.isEnabled())
            w.cancel_batch()
            self.assertFalse(w.start_timer.isActive())
            self.assertFalse(w.busy)
            self.assertTrue(w.start.isEnabled())

    def test_finish_action_dispatch(self):
        w = self.window
        w.output_path = Path('/tmp')
        w.completion_result = (2, 1, 3, False)
        with patch('editor.QMessageBox.information') as popup, patch('editor.QDesktopServices.openUrl') as open_url:
            w.batch_finish_action = 'popup'
            w.perform_finish_action()
            self.assertIn('Pulados: 3', popup.call_args.args[2])
            open_url.assert_not_called()
            w.batch_finish_action = 'folder'
            w.perform_finish_action()
            open_url.assert_called_once()

    def test_skipped_files_are_not_rendered(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'clip.mov', Path(folder) / 'CLIPED01.mp4'
            target.write_bytes(b'preservado')
            worker = Batch([source], Options(Path(folder)), [ExportJob(source, target, 'skip')])
            results = []
            worker.complete.connect(lambda *result: results.append(result))
            with patch('editor.process_video') as render:
                worker.run()
            render.assert_not_called()
            self.assertEqual(target.read_bytes(), b'preservado')
            self.assertEqual(results, [(0, 0, False)])

    def test_seconds_accept_comma_and_dot(self):
        self.window.limit.setChecked(True)
        for text in ('0,5', '1.25', '15'):
            self.window.seconds.clear()
            QTest.keyClicks(self.window.seconds, text)
            self.assertEqual(self.window.seconds.text(), text)
        self.window.seconds.clear()
        QTest.keyClicks(self.window.seconds, 'abc-1s')
        self.assertEqual(self.window.seconds.text(), '1')

    def test_pause_button_toggles_and_cancel_resumes(self):
        w = self.window
        w.worker = Batch([], Options(Path('.')))
        w.busy = True
        try:
            w.toggle_pause()
            self.assertTrue(w.worker.stop.paused)
            self.assertEqual(w.pause.text(), '▶️ Retomar')
            w.toggle_pause()
            self.assertFalse(w.worker.stop.paused)
            self.assertEqual(w.pause.text(), '⏸️ Pausar')
            w.toggle_pause()
            w.cancel_batch()
            self.assertTrue(w.worker.stop.is_set())
            self.assertFalse(w.worker.stop.paused)
            self.assertFalse(w.pause.isEnabled())
        finally:
            w.busy = False
            w.worker = None

    def test_files_filter_and_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            video, other = Path(folder) / 'a.MOV', Path(folder) / 'notes.txt'
            video.touch()
            other.touch()
            self.window.add_files([str(video), str(video), str(other)])
            self.assertEqual(self.window.files, [video.resolve()])

    def test_recursive_scan_finds_only_videos(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / 'a' / 'b'
            nested.mkdir(parents=True)
            for path in (root / 'one.MP4', nested / 'two.mov', nested / 'notes.txt'):
                path.touch()
            scan = FolderScan(str(root))
            found = []
            scan.found.connect(found.extend)
            scan.run()
            self.assertEqual({Path(p).name for p in found}, {'one.MP4', 'two.mov'})

    def test_batch_from_window(self):
        finish = patch.object(self.window, 'perform_finish_action')
        finished = finish.start()
        self.addCleanup(finish.stop)
        warning = patch('editor.QMessageBox.warning', side_effect=lambda *args: self.fail(str(args[2])))
        warning.start()
        self.addCleanup(warning.stop)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'clip.mp4'
            output = Path(folder) / 'output'
            subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=128x96:r=24:d=1', '-c:v', 'libx264', str(source)], check=True)
            w = self.window
            w.add_files([str(source)])
            frames = []
            w.video_preview.videoSink().videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
            w.toggle_preview()
            preview_deadline = time.monotonic() + 5
            while not any(frames) and time.monotonic() < preview_deadline:
                self.app.processEvents()
                QTest.qWait(20)
            self.assertTrue(any(frames), 'A prévia deve decodificar pelo menos um quadro do vídeo.')
            w.player.pause()
            w.folder.setText(str(output))
            w.watermark.setChecked(False)
            w.speed.setCurrentIndex(w.speed.findData(1))
            w.start_batch()
            self.assertTrue(w.busy)
            deadline = time.monotonic() + 30
            while w.busy and time.monotonic() < deadline:
                self.app.processEvents()
                QTest.qWait(20)
            if w.busy:
                w.cancel_batch()
                while w.busy:
                    self.app.processEvents()
                    QTest.qWait(20)
                self.fail('O lote excedeu 30 segundos.')
            self.assertTrue((output / 'CLIPED01.mp4').is_file(), w.logs.toPlainText())
            self.assertEqual(w.general.value(), 1000)
            self.assertIn('1 concluído(s) · 0 falha(s)', w.status.text())
            self.assertTrue(w.start.isEnabled())
            finished.assert_called_once()


if __name__ == '__main__':
    unittest.main()
