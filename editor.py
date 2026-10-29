"""Editor Visual 3.9.0 — aplicativo Qt para macOS."""
from __future__ import annotations

import math
import json
import os
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, Signal, Qt, QUrl, QRegularExpression, QEvent
from PySide6.QtGui import QDesktopServices, QRegularExpressionValidator, QValidator
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QStackedLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from engine import (AUDIO_EXTS, VIDEO_EXTS, Cancelled, Options, ProcessingControl, binary,
                    probe, process_video, reserve_outputs)


@dataclass(frozen=True)
class ExportJob:
    source: Path
    target: Path
    action: str = 'create'


class TimeControl(QWidget):
    """Slider em décimos com entrada numérica para valores precisos."""
    valueChanged = Signal(float)

    def __init__(self, minimum, maximum, initial, slider_max=None):
        super().__init__()
        self.slider_limit = min(maximum, slider_max if slider_max is not None else maximum)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(minimum * 10), round(self.slider_limit * 10))
        self.slider.setMinimumWidth(55)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setDecimals(1)
        self.spin.setSingleStep(.1)
        self.spin.setSuffix(' s')
        self.spin.setFixedWidth(83)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.slider.valueChanged.connect(self._slider_changed)
        self.spin.valueChanged.connect(self._spin_changed)
        self.spin.setValue(initial)
        self._spin_changed(initial)

    def _slider_changed(self, value):
        self.spin.setValue(value / 10)

    def _spin_changed(self, value):
        if value > self.slider_limit:
            self.slider_limit = min(self.spin.maximum(), max(value, self.slider_limit * 2))
            self.slider.setMaximum(round(self.slider_limit * 10))
        self.slider.blockSignals(True)
        self.slider.setValue(round(value * 10))
        self.slider.blockSignals(False)
        self.valueChanged.emit(value)

    def value(self):
        return self.spin.value()

    def setValue(self, value):
        self.spin.setValue(value)

    def minimum(self):
        return self.spin.minimum()

    def maximum(self):
        return self.spin.maximum()


class ClickableVideoWidget(QVideoWidget):
    clicked = Signal()
    doubleClicked = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.playing = False
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def set_playing(self, playing):
        self.playing = playing


class PlaybackDialog(QDialog):
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


class FolderScan(QObject):
    found = Signal(list)
    finished = Signal()
    error = Signal(str)

    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.cancel = threading.Event()

    def run(self):
        paths = []
        try:
            def on_error(error):
                self.error.emit(f'Não foi possível ler uma pasta: {error}')
            for root, _, names in os.walk(self.folder, followlinks=False, onerror=on_error):
                if self.cancel.is_set():
                    break
                for name in sorted(names):
                    if Path(name).suffix.lower() in VIDEO_EXTS:
                        paths.append(str(Path(root) / name))
                        if len(paths) >= 100:
                            self.found.emit(paths)
                            paths = []
            if paths and not self.cancel.is_set():
                self.found.emit(paths)
        finally:
            self.finished.emit()


class Batch(QObject):
    progress = Signal(int, float)
    log = Signal(str)
    complete = Signal(int, int, bool)

    def __init__(self, files, options, jobs=None):
        super().__init__()
        self.files, self.options = files, options
        self.stop = ProcessingControl()
        self.jobs = jobs

    def run(self):
        success = failed = 0
        started = time.monotonic()
        try:
            binary('ffmpeg')
            binary('ffprobe')
            self.options.output.mkdir(parents=True, exist_ok=True)
            if self.options.music:
                info = probe(self.options.music, self.stop)
                if not any(s['codec_type'] == 'audio' for s in info.get('streams', [])):
                    raise ValueError('A trilha selecionada não contém áudio.')
            jobs = self.jobs if self.jobs is not None else [
                ExportJob(source, target) for source, target in
                zip(self.files, reserve_outputs(self.files, self.options.output))]
            for index, job in enumerate(jobs):
                if job.action == 'skip':
                    self.log.emit(f'Pulado: {job.target.name} (já existe no destino).')
                    self.progress.emit(index, 1)
            with ThreadPoolExecutor(max_workers=self.options.workers) as pool:
                futures = {
                    pool.submit(process_video, job.source, job.target, self.options, self.stop,
                                lambda p, n=i: self.progress.emit(n, p), self.log.emit,
                                overwrite=job.action == 'overwrite'): i
                    for i, job in enumerate(jobs) if job.action != 'skip'
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        future.result()
                        success += 1
                    except Cancelled:
                        self.log.emit(f'Cancelado: {self.files[index].name}')
                    except Exception as error:
                        failed += 1
                        self.log.emit(f'ERRO · {self.files[index].name}: {error}')
                    self.progress.emit(index, 1)
            self.log.emit(f'Lote encerrado em {time.monotonic() - started:.1f}s · '
                          f'{success} concluído(s), {failed} falha(s).')
        except Cancelled:
            self.log.emit('Operação cancelada.')
        except Exception as error:
            failed = len(self.files)
            self.log.emit(f'ERRO: {error}')
        finally:
            self.complete.emit(success, failed, self.stop.is_set())


class DropArea(QFrame):
    files = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName('dropArea')
        self.browse_directory = str(Path.home())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        title = QLabel('Arraste seus vídeos para cá')
        title.setObjectName('dropTitle')
        title.setAlignment(Qt.AlignCenter)
        caption = QLabel('MP4, MOV e outros formatos de vídeo')
        caption.setAlignment(Qt.AlignCenter)
        caption.setObjectName('muted')
        button = QPushButton('Buscar arquivos')
        button.clicked.connect(self.browse)
        layout.addStretch()
        layout.addWidget(title)
        layout.addWidget(caption)
        layout.addWidget(button, alignment=Qt.AlignCenter)
        layout.addStretch()

    def browse(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Selecionar vídeos', self.browse_directory,
                    'Vídeos (' + ' '.join('*' + e for e in sorted(VIDEO_EXTS)) + ')')
        self.files.emit(paths)

    def dragEnterEvent(self, event):
        if any(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in VIDEO_EXTS
               for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.files.emit([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        event.acceptProposedAction()


class Window(QMainWindow):
    def __init__(self, preferences=None):
        super().__init__()
        self.preferences = preferences if preferences is not None else QSettings('BemCorrido', 'EditorVisual')
        self.preferences_timer = QTimer(self)
        self.preferences_timer.setSingleShot(True)
        self.preferences_timer.setInterval(300)
        self.preferences_timer.timeout.connect(self.save_preferences)
        self.restoring_preferences = True
        self.preferences_error = False
        self.files = []
        self.scan_thread = None
        self.scanner = None
        self.thread = None
        self.worker = None
        self.busy = False
        self.output_path = None
        self.pending_batch = None
        self.completion_result = None
        self.start_timer = QTimer(self)
        self.start_timer.setSingleShot(True)
        self.start_timer.setTimerType(Qt.PreciseTimer)
        self.start_timer.setInterval(2000)
        self.start_timer.timeout.connect(self.launch_batch)
        self.setWindowTitle('Editor Visual · 3.9')
        self.resize(1340, 740)
        self.setMinimumSize(1240, 680)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(6)
        self.setCentralWidget(content)
        heading = QLabel('Editor Visual')
        heading.setObjectName('heading')
        header = QHBoxLayout()
        header.addWidget(heading)
        header.addStretch()
        subtitle = QLabel('Seus vídeos, prontos em poucos passos.')
        subtitle.setObjectName('muted')
        header.addWidget(subtitle)
        root.addLayout(header)

        workspace = QHBoxLayout()
        workspace.setSpacing(12)
        root.addLayout(workspace, 1)
        self.inputs, entry = self.section('01   Entrada', workspace)
        workspace.setStretch(0, 5)
        self.scan_button = QPushButton('Buscar vídeos em pasta e subpastas…')
        self.scan_button.clicked.connect(self.scan_folder)
        entry.addWidget(self.scan_button)
        self.drop = DropArea()
        self.drop.setMinimumHeight(150)
        self.drop.files.connect(self.add_files)
        entry.addWidget(self.drop, 3)
        file_column = QVBoxLayout()
        file_column.setSpacing(4)
        entry.addLayout(file_column, 2)
        queue_title = QLabel('Fila de edição')
        queue_title.setObjectName('section')
        file_column.addWidget(queue_title)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.setMinimumHeight(70)
        self.list.currentRowChanged.connect(self.update_preview)
        self.list.itemDoubleClicked.connect(lambda _item: self.open_current_video())
        file_column.addWidget(self.list)
        row = QHBoxLayout()
        self.count = QLabel('Nenhum vídeo selecionado')
        remove = QPushButton('Remover')
        remove.clicked.connect(self.remove_files)
        clear = QPushButton('Limpar')
        clear.clicked.connect(self.clear_files)
        row.addWidget(self.count)
        row.addStretch()
        row.addWidget(remove)
        row.addWidget(clear)
        file_column.addLayout(row)

        self.settings, settings = self.section('02   Edição', workspace)
        settings.setAlignment(Qt.AlignTop)
        workspace.setStretch(1, 9)
        editing_columns = QHBoxLayout()
        editing_columns.setSpacing(12)
        settings.addLayout(editing_columns)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)
        grid.setAlignment(Qt.AlignTop)
        editing_columns.addLayout(grid, 1)
        self.fade = QCheckBox('Fade in · preservar capa')
        self.fade.setToolTip('Mantém o primeiro quadro visível e inicia o fade no quadro seguinte.')
        self.fade_time = TimeControl(.1, 1, .5)
        self.fade_time.setEnabled(False)
        self.fade.toggled.connect(self.fade_time.setEnabled)
        grid.addWidget(self.fade, 0, 0)
        grid.addWidget(self.fade_time, 0, 1)
        self.music = QCheckBox('Adicionar trilha sonora')
        self.music_button = QPushButton('Selecionar trilha…')
        self.music_button.setEnabled(False)
        self.music.toggled.connect(self.music_button.setEnabled)
        self.music_button.clicked.connect(self.pick_music)
        self.music_path = None
        grid.addWidget(self.music, 1, 0)
        grid.addWidget(self.music_button, 1, 1)
        self.music_label = QLabel('Trilha começa em 0,3 s; áudio original removido, como no script.')
        self.music_label.setWordWrap(True)
        self.music_label.setObjectName('muted')
        self.music_label.hide()
        self.music.setToolTip(self.music_label.text())
        grid.addWidget(QLabel('Velocidade lenta'), 2, 0)
        self.speed = QComboBox()
        self.speed.addItem('Automática · FPS → 24 fps', None)
        for speed in [.25, .5, .75, .8, 1, 1.5, 2, 4, 8, 12]:
            self.speed.addItem(f'{speed:g}×'.replace('.', ','), speed)
        self.speed.setToolTip('Automática usa o FPS; valores manuais são relativos ao arquivo de entrada.')
        speed_row = QHBoxLayout()
        speed_row.setSpacing(6)
        speed_row.addWidget(self.speed, 1)
        speed_row.addWidget(QLabel('Rápida'))
        self.fast_speed = QComboBox()
        self.fast_speed.addItem('Automática · 1×', None)
        for fast in (.25, .5, .75, .8, 1, 1.25, 1.5, 2, 4):
            self.fast_speed.addItem(f'{fast:g}×'.replace('.', ','), fast)
        self.fast_speed.setToolTip('Velocidade da parte rápida relativa ao movimento real; em Sony S&Q, considera o fator de captura selecionado.')
        speed_row.addWidget(self.fast_speed, 1)
        grid.addLayout(speed_row, 2, 1)
        grid.addWidget(QLabel('Tempo rápido'), 3, 0)
        self.fast_time = TimeControl(0, 10, 0)
        self.fast_time.setToolTip('Tempo no vídeo final antes de cada rampa; 0 s inicia a rampa imediatamente.')
        grid.addWidget(self.fast_time, 3, 1)
        self.ramp = QCheckBox('Rampa de velocidade')
        self.ramp_time = TimeControl(.2, 5, 1)
        grid.addWidget(self.ramp, 4, 0)
        grid.addWidget(self.ramp_time, 4, 1)
        grid.addWidget(QLabel('Tempo do slow'), 5, 0)
        self.slow_time = TimeControl(1, 20, 2)
        self.slow_time.setToolTip('Tempo no vídeo final, usado com 2× a 4×. Com 1×, o slow continua até o fim.')
        grid.addWidget(self.slow_time, 5, 1)
        grid.addWidget(QLabel('Repetir bloco'), 6, 0)
        self.repetitions = QComboBox()
        for count in range(1, 5):
            self.repetitions.addItem(f'{count}×', count)
        grid.addWidget(self.repetitions, 6, 1)
        self.ramp.toggled.connect(self.update_ramp)
        self.repetitions.currentIndexChanged.connect(self.update_ramp)
        self.sq = QCheckBox('Sony S&Q · vídeos de 24 fps')
        self.sq.setChecked(True)
        self.sq.setToolTip('Por padrão, assume Sony S&Q em vídeos próximos de 24 fps. Desmarque para vídeos comuns nessa taxa.')
        grid.addWidget(self.sq, 7, 0)
        self.sq_factor = QComboBox()
        for label, value in [('240 → 24 fps · 10×', 10), ('120 → 24 fps · 5×', 5), ('60 → 24 fps · 2,5×', 2.5)]:
            self.sq_factor.addItem(label, value)
        grid.addWidget(self.sq_factor, 7, 1)
        self.sq.toggled.connect(self.update_speed)
        self.speed.currentIndexChanged.connect(self.update_speed)
        self.limit = QCheckBox('Limitar duração final')
        limit_row = QHBoxLayout()
        self.seconds = QLineEdit('15')
        validator = QRegularExpressionValidator(QRegularExpression(r'\d{0,5}([.,]\d{0,3})?'), self)
        self.seconds.setValidator(validator)
        self.seconds.setPlaceholderText('Segundos')
        self.seconds.setMaximumWidth(64)
        self.seconds.setToolTip('Somente segundos. Aceita vírgula ou ponto decimal.')
        self.cut = QComboBox()
        self.cut.addItem('Manter início', False)
        self.cut.addItem('Manter final', True)
        self.cut.setToolTip('Manter início corta o final. Manter final corta o início.')
        limit_row.addWidget(self.seconds)
        limit_row.addWidget(QLabel('s'))
        limit_row.addWidget(self.cut)
        self.seconds.setEnabled(False)
        self.cut.setEnabled(False)
        self.limit.toggled.connect(self.seconds.setEnabled)
        self.limit.toggled.connect(self.cut.setEnabled)
        grid.addWidget(self.limit, 8, 0)
        grid.addLayout(limit_row, 8, 1)
        self.watermark = QCheckBox('Marca-d’água @bemcorrido')
        self.watermark.setChecked(True)
        grid.addWidget(self.watermark, 9, 0)
        self.codec = QComboBox()
        self.codec.addItem('HEVC · menor tamanho', 'hevc')
        self.codec.addItem('H.264 · maior compatibilidade', 'h264')
        grid.addWidget(self.codec, 9, 1)
        grid.addWidget(QLabel('Fluidez'), 10, 0)
        self.fluency = QComboBox()
        self.fluency.addItem('Adaptativa · até 60 fps', 60)
        self.fluency.addItem('Adaptativa · até 120 fps', 120)
        self.fluency.addItem('240 fps · experimental', 240)
        self.fluency.addItem('Fixa · 24 fps', None)
        self.fluency.setToolTip('Slow em 24 fps; rampas e trechos normais/acelerados com taxa maior, limitada pela origem. Sem interpolação de movimento.')
        grid.addWidget(self.fluency, 10, 1)
        grid.addWidget(QLabel('Vídeos em paralelo'), 11, 0)
        self.workers = QComboBox()
        for n in (1, 2, 3, 4):
            self.workers.addItem(str(n), n)
        self.workers.setCurrentIndex(1)
        grid.addWidget(self.workers, 11, 1)
        self.fade_out_video = QCheckBox('Fade out do vídeo')
        self.fade_out_video.setToolTip('Escurece o final do vídeo preservando o primeiro quadro.')
        self.fade_out_video_time = TimeControl(.1, 3600, 1, slider_max=10)
        self.fade_out_audio = QCheckBox('Fade out da trilha')
        self.fade_out_audio.setToolTip('Reduz a trilha no final do trecho audível.')
        self.fade_out_audio_time = TimeControl(.1, 3600, 1, slider_max=10)
        self.fade_out_video_time.setEnabled(False)
        self.fade_out_audio_time.setEnabled(False)
        self.fade_out_video.toggled.connect(self.fade_out_video_time.setEnabled)
        self.fade_out_audio.setEnabled(False)
        self.music.toggled.connect(self.fade_out_audio.setEnabled)
        self.fade_out_audio.toggled.connect(self.update_audio_fade)
        self.music.toggled.connect(self.update_audio_fade)
        fade_out_row = QHBoxLayout()
        fade_out_row.setSpacing(6)
        fade_out_row.addWidget(self.fade_out_video)
        fade_out_row.addWidget(self.fade_out_video_time)
        fade_out_row.addSpacing(10)
        fade_out_row.addWidget(self.fade_out_audio)
        fade_out_row.addWidget(self.fade_out_audio_time)
        fade_out_row.addStretch()
        grid.addLayout(fade_out_row, 12, 0, 1, 2)

        preview_card = QFrame()
        preview_card.setObjectName('sectionCard')
        preview_card.setMinimumWidth(245)
        preview = QVBoxLayout(preview_card)
        preview.setContentsMargins(10, 10, 10, 10)
        preview.setSpacing(4)
        workspace.addWidget(preview_card)
        preview_title = QLabel('Pré-visualização · original')
        preview_title.setObjectName('muted')
        preview_title.setToolTip('Exibe o vídeo selecionado na fila. As opções de edição são aplicadas na exportação.')
        preview.addWidget(preview_title)
        self.player = QMediaPlayer(self)
        self.preview_audio = QAudioOutput(self)
        self.preview_audio.setMuted(True)
        self.player.setAudioOutput(self.preview_audio)
        self.video_preview = ClickableVideoWidget()
        self.video_preview.setStyleSheet('background: #090d14;')
        self.video_preview.clicked.connect(self.toggle_preview)
        self.video_preview.doubleClicked.connect(self.open_playback_window)
        self.video_preview.setMinimumSize(220, 130)
        preview_stack = QWidget()
        stack = QStackedLayout(preview_stack)
        stack.setStackingMode(QStackedLayout.StackAll)
        stack.addWidget(self.video_preview)
        overlay = QWidget()
        overlay_layout = QVBoxLayout(overlay)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.addStretch()
        overlay_row = QHBoxLayout()
        overlay_row.addStretch()
        self.play_overlay = QPushButton('▶')
        self.play_overlay.setFixedSize(56, 56)
        self.play_overlay.setStyleSheet('QPushButton { color: white; background: rgba(0,0,0,35); border: 3px solid white; border-radius: 28px; font-size: 24px; padding-left: 4px; }')
        self.play_overlay.clicked.connect(self.toggle_preview)
        overlay_row.addWidget(self.play_overlay)
        overlay_row.addStretch()
        overlay_layout.addLayout(overlay_row)
        overlay_layout.addStretch()
        stack.addWidget(overlay)
        preview.addWidget(preview_stack)
        self.player.setVideoOutput(self.video_preview)
        self.playback_dialog = PlaybackDialog(self)
        self.playback_dialog.setWindowTitle('Playback do vídeo original')
        self.playback_dialog.resize(900, 600)
        playback_layout = QVBoxLayout(self.playback_dialog)
        self.playback_view = QVideoWidget()
        self.playback_view.setStyleSheet('background: #090d14;')
        self.playback_view.installEventFilter(self)
        playback_layout.addWidget(self.playback_view)
        self.playback_dialog.finished.connect(self.restore_preview_output)
        self.preview_caption = QLabel('Selecione um vídeo na fila')
        self.preview_caption.setObjectName('muted')
        preview.addWidget(self.preview_caption)
        self.player.playbackStateChanged.connect(self.preview_state_changed)
        self.player.errorOccurred.connect(self.preview_error)
        self.ramp_hint = QLabel('1×: slow até o fim do vídeo')
        self.ramp_hint.setWordWrap(True)
        self.ramp_hint.setObjectName('muted')
        preview.addWidget(self.ramp_hint)
        preview.addStretch()
        self.update_speed()
        self.update_ramp()

        self.destination, output = self.section('03   Saída', root)
        destination_row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.cwd() / '00 Final'))
        choose = QPushButton('Escolher pasta…')
        choose.clicked.connect(self.pick_folder)
        destination_row.addWidget(self.folder)
        destination_row.addWidget(choose)
        output.addLayout(destination_row)
        hint = QLabel('MP4 · Apple VideoToolbox')
        hint.setWordWrap(False)
        hint.setObjectName('muted')
        output_actions = QHBoxLayout()
        output_actions.addWidget(hint)
        output_actions.addStretch()
        output_actions.addWidget(QLabel('Ao terminar:'))
        self.finish_action = QComboBox()
        self.finish_action.addItem('Abrir a pasta destino', 'folder')
        self.finish_action.addItem('Abrir um pop-up', 'popup')
        self.finish_action.setCurrentIndex(1)
        output_actions.addWidget(self.finish_action)
        output.addLayout(output_actions)

        controls = QHBoxLayout()
        self.status = QLabel('Pronto para editar')
        self.cancel = QPushButton('❌ Cancelar')
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_batch)
        self.start = QPushButton('▶️ Iniciar edição')
        self.start.setObjectName('primary')
        self.start.clicked.connect(self.start_batch)
        controls.addWidget(self.status, 1)
        self.pause = QPushButton('⏸️ Pausar')
        self.pause.setEnabled(False)
        self.pause.clicked.connect(self.toggle_pause)
        controls.addWidget(self.start)
        controls.addWidget(self.pause)
        controls.addWidget(self.cancel)
        root.addLayout(controls)
        self.general_label = QLabel('Progresso geral')
        progress = QVBoxLayout()
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(3)
        root.addLayout(progress)
        progress.addWidget(self.general_label)
        self.general = QProgressBar()
        self.general.setRange(0, 1000)
        self.general.setFixedHeight(4)
        self.general.setTextVisible(False)
        progress.addWidget(self.general)
        self.current_label = QLabel('Vídeo atual')
        self.current_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        progress.addWidget(self.current_label)
        self.current = QProgressBar()
        self.current.setRange(0, 1000)
        self.current.setFixedHeight(4)
        self.current.setTextVisible(False)
        progress.addWidget(self.current)
        logs_header = QHBoxLayout()
        logs_header.addWidget(QLabel('Logs de processamento'))
        logs_header.addStretch()
        root.addLayout(logs_header)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        self.logs.setMinimumHeight(48)
        self.logs.setMaximumHeight(72)
        root.addWidget(self.logs)
        bottom = QHBoxLayout()
        bottom.addStretch()
        self.exit_button = QPushButton('Sair')
        self.exit_button.clicked.connect(self.close)
        bottom.addWidget(self.exit_button)
        root.addLayout(bottom)
        self.open_output = QPushButton('Abrir pasta de saída')
        self.open_output.clicked.connect(self.reveal_output)
        logs_header.addWidget(self.open_output)
        tips = {
            self.scan_button: 'Procura arquivos de vídeo nesta pasta e em todas as subpastas.',
            self.list: 'Selecione um vídeo para assistir no preview. Dê duplo clique no arquivo para abrir no player do macOS.',
            remove: 'Remove da fila os vídeos selecionados.', clear: 'Remove todos os vídeos da fila.',
            self.fade: 'Aplica fade in depois do primeiro quadro, preservando a miniatura.',
            self.music: 'Adiciona uma trilha sonora aos vídeos editados.', self.music_button: 'Escolhe o arquivo de áudio da trilha sonora.',
            self.speed: 'Define a velocidade da parte lenta; o modo automático considera os quadros por segundo do vídeo.',
            self.fast_speed: 'Define a velocidade da parte rápida da rampa.', self.fast_time: 'Duração da parte rápida antes de cada rampa, em segundos.',
            self.ramp: 'Ativa a transição gradual entre as velocidades rápida e lenta.', self.ramp_time: 'Duração de cada transição da rampa, em segundos.',
            self.slow_time: 'Duração do trecho lento em cada bloco repetido, em segundos.', self.repetitions: 'Quantidade de blocos de rampa e slow no vídeo.',
            self.sq: 'Ative para tratar vídeos Sony S&Q gravados em 24 fps usando o fator de captura escolhido.',
            self.sq_factor: 'Fator de captura usado no modo Sony S&Q.', self.limit: 'Ativa o corte do vídeo para uma duração final específica.',
            self.seconds: 'Digite a duração final em segundos.', self.cut: 'Escolhe se o corte preserva o início ou o fim do vídeo.',
            self.watermark: 'Ativa a marca-d’água no vídeo final.', self.codec: 'Escolhe o formato de compressão do vídeo exportado.',
            self.fluency: 'Escolhe a fluidez e a taxa de quadros usada na exportação.', self.workers: 'Quantidade de vídeos processados ao mesmo tempo.',
            self.fade_out_video: 'Aplica fade out de imagem no final do vídeo.', self.fade_out_audio: 'Aplica fade out de áudio no final da trilha.',
            self.folder: 'Pasta onde os vídeos editados serão salvos.', choose: 'Escolhe a pasta de destino dos vídeos editados.',
            self.finish_action: 'Escolhe o que acontece quando a edição terminar.', self.start: 'Inicia a edição da fila.',
            self.pause: 'Pausa ou retoma o processamento.', self.cancel: 'Cancela o lote atual.', self.exit_button: 'Fecha o editor.',
            self.open_output: 'Abre a pasta de destino no Finder.', self.play_overlay: 'Reproduz ou pausa o vídeo original selecionado.',
        }
        for control, tip in tips.items():
            control.setToolTip(tip)
        for control in (self.fade_time, self.fast_time, self.ramp_time, self.slow_time,
                        self.fade_out_video_time, self.fade_out_audio_time):
            control.setToolTip('Ajuste o tempo pelo controle deslizante ou digite o valor em segundos.')
            control.slider.setToolTip(control.toolTip())
            control.spin.setToolTip(control.toolTip())
        self.restore_preferences()
        migrate_sq = self.preferences.value('migration/sq_auto_v1') != 'done'
        if migrate_sq:
            self.sq.setChecked(True)
            self.update_speed()
            self.preferences.setValue('migration/sq_auto_v1', 'done')
        self.restoring_preferences = False
        for widget in self.preference_widgets().values():
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.schedule_preferences_save)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self.schedule_preferences_save)
            elif isinstance(widget, (QDoubleSpinBox, TimeControl)):
                widget.valueChanged.connect(self.schedule_preferences_save)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.schedule_preferences_save)
        if migrate_sq:
            self.save_preferences()

    def preference_widgets(self):
        names = ('fade', 'fade_time', 'fade_out_video', 'fade_out_video_time', 'fade_out_audio', 'fade_out_audio_time', 'music', 'speed', 'fast_speed', 'fast_time', 'ramp', 'ramp_time', 'slow_time',
                 'repetitions', 'sq', 'sq_factor', 'limit', 'seconds', 'cut', 'watermark',
                 'codec', 'workers', 'fluency', 'folder', 'finish_action')
        return {name: getattr(self, name) for name in names}

    def schedule_preferences_save(self, *_):
        if not self.restoring_preferences:
            self.preferences_timer.start()

    def save_preferences(self):
        self.preferences_timer.stop()
        values = {}
        for name, widget in self.preference_widgets().items():
            if isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            elif isinstance(widget, (QDoubleSpinBox, TimeControl)):
                values[name] = widget.value()
            else:
                values[name] = widget.text()
        values['music_path'] = str(self.music_path) if self.music_path else ''
        values['browse_directory'] = self.drop.browse_directory
        self.preferences.setValue('choices/v1', json.dumps(values, ensure_ascii=False))
        self.preferences.setValue('window/geometry', self.saveGeometry())
        self.preferences.sync()
        if self.preferences.status() != QSettings.NoError:
            if not self.preferences_error:
                self.logs.appendPlainText('Não foi possível salvar as preferências. Verifique as permissões do usuário.')
            self.preferences_error = True

    def restore_preferences(self):
        try:
            values = json.loads(self.preferences.value('choices/v1', '{}'))
        except (TypeError, ValueError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        for name, widget in self.preference_widgets().items():
            if name not in values:
                continue
            value = values[name]
            if isinstance(widget, QCheckBox) and isinstance(value, bool):
                widget.setChecked(value)
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif isinstance(widget, (QDoubleSpinBox, TimeControl)) and type(value) in (float, int):
                if math.isfinite(value) and widget.minimum() <= value <= widget.maximum():
                    widget.setValue(value)
            elif isinstance(widget, QLineEdit) and isinstance(value, str):
                if widget.validator() is None or widget.validator().validate(value, 0)[0] == QValidator.Acceptable:
                    widget.setText(value)
        music = values.get('music_path')
        if isinstance(music, str) and music:
            self.music_path = Path(music).expanduser()
            suffix = '' if self.music_path.is_file() else ' (não encontrada)'
            self.music_button.setText(self.music_path.name + suffix)
            self.music_button.setToolTip(str(self.music_path))
        directory = values.get('browse_directory')
        if isinstance(directory, str) and Path(directory).is_dir():
            self.drop.browse_directory = directory
        geometry = self.preferences.value('window/geometry')
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except (TypeError, ValueError):
                pass
        self.update_speed()
        self.update_ramp()

    def section(self, title, root):
        frame = QFrame()
        frame.setObjectName('card')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)
        label = QLabel(title)
        label.setObjectName('section')
        layout.addWidget(label)
        root.addWidget(frame)
        return frame, layout

    def update_speed(self):
        self.sq_factor.setEnabled(self.sq.isChecked())

    def update_audio_fade(self):
        self.fade_out_audio_time.setEnabled(self.music.isChecked() and self.fade_out_audio.isChecked())

    def update_ramp(self):
        enabled = self.ramp.isChecked()
        self.fast_speed.setEnabled(enabled)
        self.fast_time.setEnabled(enabled)
        self.ramp_time.setEnabled(enabled)
        self.repetitions.setEnabled(enabled)
        self.slow_time.setEnabled(enabled and self.repetitions.currentData() > 1)
        self.ramp_hint.setText('1×: slow até o fim do vídeo' if self.repetitions.currentData() == 1
                               else 'Cada bloco: rampa → slow → retorno ao normal.')
        self.ramp_hint.setVisible(enabled)

    def update_preview(self, row):
        self.player.stop()
        if 0 <= row < len(self.files):
            path = self.files[row]
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.preview_caption.setText(path.name)
            self.preview_caption.setToolTip(str(path))
        else:
            self.player.setSource(QUrl())
            self.preview_caption.setText('Selecione um vídeo na fila')
            self.preview_caption.setToolTip('')

    def toggle_preview(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def preview_state_changed(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.video_preview.set_playing(playing)
        self.play_overlay.setText('Ⅱ' if playing else '▶')

    def open_current_video(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.files):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.files[row])))

    def open_playback_window(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.files):
            self.player.setVideoOutput(self.playback_view)
            self.playback_dialog.show()
            self.player.play()

    def restore_preview_output(self):
        self.player.setVideoOutput(self.video_preview)
        self.video_preview.set_playing(self.player.playbackState() == QMediaPlayer.PlayingState)

    def eventFilter(self, watched, event):
        if watched is self.playback_view and event.type() == QEvent.MouseButtonDblClick:
            self.playback_dialog.close()
            return True
        return super().eventFilter(watched, event)

    def preview_error(self, error, message):
        self.preview_caption.setText('Não foi possível abrir a prévia')
        self.preview_caption.setToolTip(message)
        self.video_preview.set_playing(False)

    def add_files(self, paths):
        for path in paths:
            source = Path(path).resolve()
            if source.is_file() and source.suffix.lower() in VIDEO_EXTS and source not in self.files:
                self.files.append(source)
                self.list.addItem(str(source))
                self.drop.browse_directory = str(source.parent)
                self.schedule_preferences_save()
        self.count.setText(f'{len(self.files)} vídeo(s) selecionado(s)')
        if self.files and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def scan_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Buscar vídeos em pasta e subpastas',
                                                   self.drop.browse_directory)
        if not folder:
            return
        self.drop.browse_directory = folder
        self.schedule_preferences_save()
        self.scan_button.setEnabled(False)
        self.start.setEnabled(False)
        self.status.setText('Buscando vídeos nas subpastas…')
        self.scan_thread = QThread(self)
        self.scanner = FolderScan(folder)
        self.scanner.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scanner.run)
        self.scanner.found.connect(self.add_files)
        self.scanner.error.connect(self.logs.appendPlainText)
        self.scanner.finished.connect(self.scan_thread.quit)
        self.scanner.finished.connect(self.scanner.deleteLater)
        self.scan_thread.finished.connect(self.scan_finished)
        self.scan_thread.start()

    def scan_finished(self):
        self.scan_button.setEnabled(True)
        self.start.setEnabled(not self.busy)
        self.status.setText(f'Busca concluída · {len(self.files)} vídeo(s) na fila')
        self.scan_thread.deleteLater()
        self.scan_thread = None
        self.scanner = None

    def remove_files(self):
        for row in sorted((self.list.row(i) for i in self.list.selectedItems()), reverse=True):
            self.files.pop(row)
            self.list.takeItem(row)
        self.count.setText(f'{len(self.files)} vídeo(s) selecionado(s)')
        self.update_preview(self.list.currentRow())

    def clear_files(self):
        self.files.clear()
        self.list.clear()
        self.count.setText('Nenhum vídeo selecionado')
        self.update_preview(-1)

    def pick_music(self):
        directory = str(self.music_path.parent) if self.music_path else self.drop.browse_directory
        path, _ = QFileDialog.getOpenFileName(self, 'Selecionar trilha sonora', directory,
                    'Áudio (' + ' '.join('*' + e for e in sorted(AUDIO_EXTS)) + ')')
        if path:
            self.music_path = Path(path)
            self.music_button.setText(self.music_path.name)
            self.music_button.setToolTip(path)
            self.schedule_preferences_save()

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'Pasta de saída', self.folder.text())
        if path:
            self.folder.setText(path)

    def start_batch(self):
        if self.busy:
            return
        self.save_preferences()
        try:
            if not self.files:
                raise ValueError('Adicione pelo menos um vídeo.')
            if not self.folder.text().strip():
                raise ValueError('Selecione a pasta de saída.')
            limit = None
            if self.limit.isChecked():
                limit = float(self.seconds.text().replace(',', '.'))
                if not math.isfinite(limit) or not .001 <= limit <= 86400:
                    raise ValueError('Informe uma duração entre 0,001 e 86400 segundos.')
            if self.music.isChecked() and (not self.music_path or not self.music_path.is_file()):
                raise ValueError('Selecione um arquivo de trilha sonora válido.')
            options = Options(output=Path(self.folder.text()).expanduser().resolve(),
                              fade=self.fade_time.value() if self.fade.isChecked() else None,
                              fade_out_video=self.fade_out_video.isChecked(),
                              fade_out_audio=self.fade_out_audio.isChecked(),
                              fade_out_video_duration=self.fade_out_video_time.value(),
                              fade_out_audio_duration=self.fade_out_audio_time.value(),
                              music=self.music_path if self.music.isChecked() else None,
                              speed=self.speed.currentData(), fast_speed=self.fast_speed.currentData(),
                              fast_duration=self.fast_time.value(), limit=limit,
                              keep_end=self.cut.currentData(), codec=self.codec.currentData(),
                              sq_detection=self.sq.isChecked(), sq_factor=self.sq_factor.currentData(),
                              watermark=self.watermark.isChecked(), workers=self.workers.currentData(),
                              ramp=self.ramp.isChecked(), ramp_duration=self.ramp_time.value(),
                              slow_duration=self.slow_time.value(), repetitions=self.repetitions.currentData(),
                              adaptive_fps=self.fluency.currentData())
            binary('ffmpeg')
            binary('ffprobe')
            jobs = self.prepare_jobs(options.output)
            if jobs is None:
                return
        except (ValueError, RuntimeError) as error:
            QMessageBox.warning(self, 'Verifique as opções', str(error))
            return
        self.values = [0.0] * len(self.files)
        self.player.stop()
        self.finished_indices = set()
        self.skipped_count = sum(job.action == 'skip' for job in jobs)
        self.completion_result = None
        self.batch_finish_action = self.finish_action.currentData()
        self.output_path = options.output
        self.busy = True
        for widget in (self.inputs, self.settings, self.destination, self.start):
            widget.setEnabled(False)
        self.cancel.setEnabled(True)
        self.pause.setEnabled(False)
        self.pause.setText('⏸️ Pausar')
        self.general.setValue(0)
        self.current.setValue(0)
        self.status.setText('Iniciando...')
        self.logs.appendPlainText('\nIniciando... (aguarde 2 segundos)')
        self.pending_batch = (list(self.files), options, jobs)
        self.start_timer.start()

    def ask_conflict(self, target):
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Arquivo já existe')
        dialog.setIcon(QMessageBox.Question)
        dialog.setText(f'O arquivo {target.name} já existe na pasta destino.')
        dialog.setInformativeText('Deseja sobrescrever ou pular este vídeo?')
        dialog.setDetailedText(str(target))
        apply_all = QCheckBox('Aplicar a todos os conflitos deste lote')
        dialog.setCheckBox(apply_all)
        overwrite = dialog.addButton('Sobrescrever', QMessageBox.AcceptRole)
        skip = dialog.addButton('Pular', QMessageBox.RejectRole)
        dialog.addButton('Cancelar lote', QMessageBox.DestructiveRole)
        dialog.setDefaultButton(skip)
        dialog.exec()
        clicked = dialog.clickedButton()
        action = 'overwrite' if clicked == overwrite else 'skip' if clicked == skip else None
        return action, apply_all.isChecked()

    def prepare_jobs(self, folder):
        targets = reserve_outputs(self.files, folder, avoid_existing=False)
        jobs, all_action = [], None
        protected = {path.resolve() for path in self.files}
        if self.music.isChecked() and self.music_path:
            protected.add(self.music_path.resolve())
        for source, target in zip(self.files, targets):
            if target.resolve() in protected:
                raise ValueError(f'O destino {target.name} coincide com um arquivo de entrada. Escolha outra pasta.')
            action = 'create'
            if target.exists():
                if not target.is_file():
                    raise ValueError(f'O destino {target.name} não é um arquivo. Escolha outra pasta.')
                if all_action is None:
                    action, apply_all = self.ask_conflict(target)
                    if action is None:
                        return None
                    if apply_all:
                        all_action = action
                else:
                    action = all_action
            jobs.append(ExportJob(source, target, action))
        return jobs

    def launch_batch(self):
        if self.pending_batch is None:
            return
        files, options, jobs = self.pending_batch
        self.pending_batch = None
        self.status.setText('Processando…')
        self.pause.setEnabled(True)
        self.logs.appendPlainText('Iniciando lote · Editor Visual 3.9.0')
        self.thread = QThread(self)
        self.worker = Batch(files, options, jobs)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.logs.appendPlainText)
        self.worker.progress.connect(self.update_progress)
        self.worker.complete.connect(self.batch_complete)
        self.worker.complete.connect(self.thread.quit)
        self.worker.complete.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread_finished)
        self.thread.start()

    def update_progress(self, index, value):
        self.values[index] = max(self.values[index], value)
        self.general.setValue(round(sum(self.values) / len(self.values) * 1000))
        self.current.setValue(round(value * 1000))
        self.current_label.setText(f'Vídeo atual · {self.files[index].name}')
        self.current_label.setToolTip(str(self.files[index]))
        if value >= 1:
            self.finished_indices.add(index)
        self.general_label.setText(f'Progresso geral · {len(self.finished_indices)} de {len(self.files)} encerrados')

    def batch_complete(self, success, failed, cancelled):
        skipped = getattr(self, 'skipped_count', 0)
        self.completion_result = (success, failed, skipped, cancelled)
        self.pause.setEnabled(False)
        self.pause.setText('⏸️ Pausar')
        self.status.setText('Cancelado' if cancelled else f'{success} concluído(s) · {failed} falha(s) · {skipped} pulado(s)')
        if cancelled:
            self.current_label.setText('Processamento cancelado')
        elif failed:
            self.current_label.setText('Lote encerrado · consulte os logs das falhas')
        else:
            self.current_label.setText('Lote encerrado')

    def thread_finished(self):
        self.busy = False
        for widget in (self.inputs, self.settings, self.destination, self.start):
            widget.setEnabled(True)
        self.cancel.setEnabled(False)
        self.pause.setEnabled(False)
        self.pause.setText('⏸️ Pausar')
        if self.thread:
            self.thread.deleteLater()
        self.thread = None
        self.worker = None
        if self.completion_result and not self.completion_result[3]:
            self.perform_finish_action()

    def perform_finish_action(self):
        success, failed, skipped, _ = self.completion_result
        if self.batch_finish_action == 'folder':
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_path)))
        else:
            QMessageBox.information(self, 'Edição finalizada',
                f'Lote finalizado.\n\nConcluídos: {success}\nPulados: {skipped}\nFalhas: {failed}'
                f'\n\nPasta destino:\n{self.output_path}')

    def toggle_pause(self):
        if self.worker and self.busy and not self.worker.stop.is_set():
            paused = not self.worker.stop.paused
            self.worker.stop.set_paused(paused)
            self.pause.setText('▶️ Retomar' if paused else '⏸️ Pausar')
            self.status.setText('Pausado' if paused else 'Processando…')
            self.logs.appendPlainText('Lote pausado.' if paused else 'Processamento retomado.')

    def cancel_batch(self):
        if self.pending_batch is not None:
            self.start_timer.stop()
            self.pending_batch = None
            self.status.setText('Cancelado antes de iniciar')
            self.logs.appendPlainText('Lote cancelado durante a espera inicial.')
            self.thread_finished()
            return
        if self.worker:
            self.worker.stop.set()
            self.pause.setEnabled(False)
            self.pause.setText('⏸️ Pausar')
            self.cancel.setEnabled(False)
            self.status.setText('Cancelando e limpando temporários…')

    def reveal_output(self):
        folder = self.output_path if self.busy else Path(self.folder.text()).expanduser()
        if folder and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def closeEvent(self, event):
        self.save_preferences()
        if self.scan_thread is not None:
            self.scanner.cancel.set()
            event.ignore()
            self.status.setText('Interrompendo busca; feche novamente em instantes.')
            return
        if self.busy:
            self.cancel_batch()
            event.ignore()
            self.logs.appendPlainText('Aguarde o cancelamento antes de fechar a janela.')
        else:
            self.player.stop()
            event.accept()


STYLE = '''
QWidget { font-family: -apple-system, 'Helvetica Neue', sans-serif; font-size: 13px; color: #e7eaf1; }
QMainWindow, QMainWindow > QWidget { background: #11151d; }
QMessageBox { background: #1b2230; }
QFrame#card { background: #1b2230; border: 1px solid #303b4e; border-radius: 12px; }
QLabel#heading { font-size: 23px; font-weight: 700; }
QLabel#section { font-size: 13px; font-weight: 600; color: #a9c8ff; padding-bottom: 2px; }
QLabel#muted { color: #a2aec2; font-size: 12px; }
QFrame#dropArea { border: 2px dashed #526c96; border-radius: 10px; background: #202c40; padding: 0px; }
QLabel#dropTitle { font-size: 15px; font-weight: 600; }
QPushButton { background: #303f57; border: 1px solid #526078; border-radius: 7px; padding: 4px 10px; }
QPushButton:hover { background: #405777; }
QPushButton#primary { background: #3478f6; border-color: #6198ff; font-weight: 600; }
QPushButton#primary:hover { background: #4a88ff; }
QLineEdit, QComboBox, QDoubleSpinBox, QListWidget, QPlainTextEdit { background: #121a27; border: 1px solid #45526a; border-radius: 6px; padding: 1px; selection-background-color: #3478f6; }
QComboBox QAbstractItemView { background: #202c40; selection-background-color: #3478f6; }
QWidget:disabled { color: #657084; }
QComboBox:disabled, QLineEdit:disabled, QDoubleSpinBox:disabled, QPushButton:disabled { background: #252c38; border-color: #333c4c; }
QSlider { background: transparent; }
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: #46546a; }
QSlider::sub-page:horizontal { background: #5596ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #dbe8ff; border: 1px solid #96bfff; width: 12px; margin: -5px 0; border-radius: 6px; }
QSlider:disabled::sub-page:horizontal { background: #536176; }
QSlider:disabled::handle:horizontal { background: #748093; border-color: #748093; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; }
QProgressBar { border: none; border-radius: 2px; background: #2a3445; }
QProgressBar::chunk { border-radius: 2px; background: #3478f6; }
'''


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('Editor Visual')
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
