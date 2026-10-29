"""Motor do Editor Visual 3.2.0, baseado em 00 Editor s corte - 00 Final.py.

3.2.0: fluidez adaptativa por trecho e exportação VFR com timebase precisa.
3.1.0: sobrescrita autorizada e atômica após exportação bem-sucedida.
3.0.0: slow automático FPS/24 do novo script e rampas suaves configuráveis.
2.1.0: pausa e retomada real dos subprocessos do lote no macOS.
2.0.0: interface gráfica, fade com capa preservada, trilha e corte por duração.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.mts', '.m2ts', '.webm', '.flv', '.m4v'}
AUDIO_EXTS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.aiff', '.ogg'}


class Cancelled(Exception):
    pass


class ProcessingControl(threading.Event):
    """Pausa os subprocessos do lote e bloqueia novas etapas até retomar."""

    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._processes = set()
        self.paused = False

    def _signal(self, process, value):
        if process.poll() is None:
            try:
                process.send_signal(value)
            except ProcessLookupError:
                pass

    def set_paused(self, paused):
        with self._lock:
            self.paused = paused and not self.is_set()
            for process in self._processes:
                self._signal(process, signal.SIGSTOP if self.paused else signal.SIGCONT)

    def attach(self, process):
        with self._lock:
            self._processes.add(process)
            if self.paused and not self.is_set():
                self._signal(process, signal.SIGSTOP)

    def detach(self, process):
        with self._lock:
            self._signal(process, signal.SIGCONT)
            self._processes.discard(process)

    def checkpoint(self):
        while not self.is_set():
            with self._lock:
                if not self.paused:
                    return
            self.wait(.05)
        raise Cancelled()

    def set(self):
        # SIGTERM não encerra um processo suspenso até receber SIGCONT.
        with self._lock:
            super().set()
            self.set_paused(False)


def checkpoint(stop):
    if isinstance(stop, ProcessingControl):
        stop.checkpoint()
    elif stop is not None and stop.is_set():
        raise Cancelled()


@dataclass(frozen=True)
class Options:
    output: Path
    fade: float | None = None
    fade_out_video: bool = False
    fade_out_audio: bool = False
    fade_out_video_duration: float = 1.0
    fade_out_audio_duration: float = 1.0
    music: Path | None = None
    speed: float | None = None  # None = slow automático FPS/24 do novo script
    fast_speed: float | None = None  # None = velocidade real; em S&Q relativa à captura
    fast_duration: float = 0.0
    limit: float | None = None
    keep_end: bool = False
    codec: str = 'hevc'
    sq_detection: bool = True
    sq_factor: float = 10.0
    watermark: bool = True
    workers: int = 2
    ramp: bool = False
    ramp_duration: float = 1.0
    slow_duration: float = 2.0
    repetitions: int = 1
    adaptive_fps: int | None = 60  # None = saída fixa em 24 fps


def binary(name):
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / name
    if bundled.is_file():
        return str(bundled)
    for folder in ('/opt/homebrew/bin', '/usr/local/bin'):
        candidate = Path(folder) / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f'{name} não encontrado. Instale o FFmpeg com: brew install ffmpeg')


def probe(path, stop=None):
    checkpoint(stop)
    # Poll para permitir cancelar também a inspeção de arquivos.
    with tempfile.TemporaryFile(mode='w+') as stdout, tempfile.TemporaryFile(mode='w+') as stderr:
        process = subprocess.Popen([binary('ffprobe'), '-v', 'error', '-show_streams',
                                    '-show_format', '-of', 'json', str(path)], stdout=stdout, stderr=stderr)
        try:
            if isinstance(stop, ProcessingControl):
                stop.attach(process)
            while process.poll() is None:
                if stop is not None and stop.wait(0.1):
                    raise Cancelled()
                if stop is None:
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
            stdout.seek(0)
            stderr.seek(0)
            if process.returncode:
                raise RuntimeError(stderr.read()[-2500:] or 'Não foi possível ler o arquivo.')
            return json.load(stdout)
        finally:
            if isinstance(stop, ProcessingControl):
                stop.detach(process)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def metadata(path, stop=None):
    data = probe(path, stop)
    stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if stream is None:
        raise ValueError('O arquivo não contém vídeo.')
    rates = []
    for key in ('r_frame_rate', 'avg_frame_rate'):
        try:
            rates.append(float(Fraction(stream.get(key, '0/1'))))
        except (ValueError, ZeroDivisionError):
            pass
    duration = float(data.get('format', {}).get('duration') or stream.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Duração do vídeo inválida.')
    return duration, max(rates, default=0)


@dataclass(frozen=True)
class Ramp:
    """Velocidade varia linearmente no tempo de saída, sem interpolar quadros."""
    start_speed: float
    end_speed: float
    duration: float

    @property
    def acceleration(self):
        return (self.end_speed - self.start_speed) / self.duration

    def source_time(self, output_time):
        return self.start_speed * output_time + .5 * self.acceleration * output_time ** 2

    def output_time(self, source_time):
        return 2 * source_time / (self.start_speed + math.sqrt(max(0, self.start_speed ** 2 + 2 * self.acceleration * source_time)))


def segment_time(length, factor):
    return factor.output_time(length) if isinstance(factor, Ramp) else length * factor


def plan_duration(plan):
    return sum(segment_time(length, factor) for _, length, factor in plan)


def build_ramp_plan(duration, normal_speed, slow_speed, options):
    if not .2 <= options.ramp_duration <= 5 or not 1 <= options.repetitions <= 4:
        raise ValueError('Rampa: duração de 0,2 a 5 s e repetição de 1 a 4 vezes.')
    if not math.isfinite(options.slow_duration) or not 1 <= options.slow_duration <= 20:
        raise ValueError('O tempo de slow deve ficar entre 1 e 20 segundos.')
    if not math.isfinite(options.fast_duration) or not 0 <= options.fast_duration <= 10:
        raise ValueError('O tempo rápido deve ficar entre 0 e 10 segundos.')
    result, pos = [], 0.0

    def add_transition(start_speed, end_speed):
        nonlocal pos
        curve = Ramp(start_speed, end_speed, options.ramp_duration)
        length = min(duration - pos, curve.source_time(curve.duration))
        if length > 1e-8:
            result.append((pos, length, curve))
            pos += length

    def add_constant(out_time, speed):
        nonlocal pos
        length = min(duration - pos, out_time * speed)
        if length > 1e-8:
            result.append((pos, length, 1 / speed))
            pos += length

    if abs(normal_speed - slow_speed) < 1e-8:
        return [(0, duration, 1 / slow_speed)]
    for _ in range(options.repetitions):
        add_constant(options.fast_duration, normal_speed)
        add_transition(normal_speed, slow_speed)
        if options.repetitions == 1:
            add_constant(duration / slow_speed, slow_speed)
        else:
            add_constant(options.slow_duration, slow_speed)
            add_transition(slow_speed, normal_speed)
    add_constant(duration / normal_speed, normal_speed)
    return result


def speed_plan(duration, fps, options):
    # Novo script de referência: FPS/24 para HFR; até 24 fps, duração intacta.
    is_sq = options.sq_detection and fps > 0 and abs(fps - 24) < 1
    if is_sq:
        slow = 1.0
    elif fps > 24:
        nearest = min([24, 25, 30, 48, 50, 60, 100, 120, 200, 240], key=lambda x: abs(x - fps))
        capture = nearest if abs(nearest - fps) < 1 else fps
        slow = capture / 24
    else:
        slow = 1.0
    slow_speed = options.speed if options.speed is not None else 1 / slow
    if not math.isfinite(slow_speed) or slow_speed <= 0:
        raise ValueError('A velocidade deve ser maior que zero.')
    if options.ramp:
        fast_speed = options.fast_speed if options.fast_speed is not None else 1.0
        if not math.isfinite(fast_speed) or fast_speed <= 0:
            raise ValueError('A velocidade rápida deve ser maior que zero.')
        return build_ramp_plan(duration, fast_speed * (options.sq_factor if is_sq else 1.0), slow_speed, options)
    return [(0, duration, 1 / slow_speed)]


def crop_plan(plan, limit, keep_end=False):
    total = plan_duration(plan)
    if limit is None or limit >= total:
        return plan
    left = max(0, total - limit) if keep_end else 0
    right = left + limit
    result, cursor = [], 0.0
    for start, length, factor in plan:
        end = cursor + segment_time(length, factor)
        a, b = max(left, cursor), min(right, end)
        if b - a > 1e-8:
            if isinstance(factor, Ramp):
                t0, t1 = a - cursor, b - cursor
                s0, s1 = factor.source_time(t0), factor.source_time(t1)
                cropped = Ramp(factor.start_speed + factor.acceleration * t0,
                               factor.start_speed + factor.acceleration * t1, t1 - t0)
                result.append((start + s0, s1 - s0, cropped))
            else:
                result.append((start + (a - cursor) / factor, (b - a) / factor, factor))
        cursor = end
    return result


def segment_fps(factor, source_fps, options):
    if options.adaptive_fps is None:
        return 24.0
    if options.adaptive_fps not in (60, 120, 240):
        raise ValueError('Fluidez adaptativa: escolha 60, 120 ou 240 fps.')
    if isinstance(factor, Ramp):
        fastest = max(factor.start_speed, factor.end_speed)
    else:
        if factor > 1 + 1e-8:
            return 24.0
        fastest = 1 / factor
    # Não aumenta a taxa além da disponibilidade máxima do trecho de origem.
    return max(24.0, min(options.adaptive_fps, (source_fps or 24) * fastest))


def filter_graph(plan, options, source_fps=24.0):
    filters, labels = [], []
    # Split explícito para reutilizar a entrada em múltiplos trechos.
    if len(plan) > 1:
        filters.append('[0:v]split=' + str(len(plan)) + ''.join(f'[s{i}]' for i in range(len(plan))))
    for i, (start, duration, factor) in enumerate(plan):
        source = f'[s{i}]' if len(plan) > 1 else '[0:v]'
        if isinstance(factor, Ramp):
            # Inversa de s=v0*t+a*t²/2, estável também para aceleração pequena.
            pts = (f'2*(T-STARTT)/({factor.start_speed:.12f}+'
                   f'sqrt(max(0,{factor.start_speed ** 2:.12f}+'
                   f'{2 * factor.acceleration:.12f}*(T-STARTT))))/TB')
            pts = "'" + pts + "'"
        else:
            pts = f'{factor:.10f}*(PTS-STARTPTS)'
        resample = ''
        if options.adaptive_fps is not None:
            rate = segment_fps(factor, source_fps, options)
            resample = f',fps={rate:.9f}'
        filters.append(f'{source}trim=start={start:.9f}:duration={duration:.9f},'
                       f'settb=AVTB,setpts={pts}{resample},setsar=1[v{i}]')
        labels.append(f'[v{i}]')
    tail = ''.join(labels) + f'concat=n={len(plan)}:v=1:a=0,setpts=PTS-STARTPTS'
    if options.adaptive_fps is None:
        tail += ',fps=24'
    if options.fade is not None:
        # Quadro 0 intacto para miniatura. Quadro 1 inicia o fade do preto.
        tail += f",fade=t=in:st={1 / 24:.9f}:d={options.fade}:enable='gte(n,1)'"
    if options.watermark:
        font = next((p for p in ('/Library/Fonts/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf') if Path(p).is_file()), None)
        font_arg = f"fontfile='{font}':" if font else ''
        tail += f",drawtext={font_arg}text='@bemcorrido':fontcolor=white@0.5:fontsize=36:x=w-text_w-20:y=h-text_h-20"
    if options.fade_out_video:
        length = plan_duration(plan)
        fade_length = min(options.fade_out_video_duration, length)
        tail += f",fade=t=out:st={max(0.0, length - fade_length):.9f}:d={fade_length:.9f}:enable='gte(n,1)'"
    filters.append(tail + '[vout]')
    return ';'.join(filters)


def reserve_outputs(files, folder, avoid_existing=True):
    used, outputs = set(), []
    for source in files:
        clean = source.stem.replace('_', '').replace('-', '').replace(' ', '').upper() or 'VIDEO'
        base, number = clean + 'ED01', 1
        target = folder / (base + '.mp4')
        while (avoid_existing and target.exists()) or str(target).casefold() in used:
            number += 1
            target = folder / f'{base}_{number}.mp4'
        used.add(str(target).casefold())
        outputs.append(target)
    return outputs


def run_ffmpeg(arguments, duration, stop, progress):
    checkpoint(stop)
    # stderr em disco: não acumula logs de FFmpeg nem bloqueia um pipe cheio.
    with tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen([binary('ffmpeg'), '-hide_banner', '-nostdin', '-y',
                                    '-progress', 'pipe:1', '-nostats', *arguments],
                                   stdout=subprocess.PIPE, stderr=errors, text=True)
        if isinstance(stop, ProcessingControl):
            stop.attach(process)
        def cancel_watch():
            while process.poll() is None:
                if stop.wait(.1):
                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    return
        watcher = threading.Thread(target=cancel_watch, daemon=True)
        watcher.start()
        try:
            for line in process.stdout:
                if line.startswith('out_time_us='):
                    try:
                        progress(min(.99, max(0, int(line.split('=')[1]) / 1_000_000 / duration)))
                    except ValueError:
                        pass
            process.wait()
            if stop.is_set():
                raise Cancelled()
            if process.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read()[-4000:])
        finally:
            if isinstance(stop, ProcessingControl):
                stop.detach(process)
            if process.poll() is None:
                process.kill()
                process.wait()
            watcher.join(timeout=4)
            process.stdout.close()


def process_video(source, target, options, stop, progress, log, overwrite=False):
    checkpoint(stop)
    duration, fps = metadata(source, stop)
    plan = crop_plan(speed_plan(duration, fps, options), options.limit, options.keep_end)
    expected = plan_duration(plan)
    log(f'{source.name}: {fps:.3f} fps · {duration:.2f}s → cerca de {expected:.2f}s')
    if options.sq_detection and fps > 0 and abs(fps - 24) < 1:
        log(f'Vídeo próximo de 24 fps: assumindo Sony S&Q ({options.sq_factor:g}×).')
    elif fps > 24:
        log(f'Vídeo HFR: {fps:.3f} fps detectados; slow automático considera a captura em alta taxa.')
    if options.adaptive_fps is not None:
        rates = sorted({round(segment_fps(factor, fps, options), 3) for _, _, factor in plan})
        log('Fluidez adaptativa · taxas por trecho: ' + ', '.join(f'{rate:g} fps' for rate in rates))
    with tempfile.TemporaryDirectory(prefix='.editor-', dir=options.output) as work:
        video, mux = Path(work) / 'video.mp4', Path(work) / 'final.mp4'
        args = ['-i', str(source), '-filter_complex', filter_graph(plan, options, fps),
                '-map', '[vout]', '-an', '-c:v', f'{options.codec}_videotoolbox',
                '-pix_fmt', 'yuv420p', '-b:v', '12M']
        if options.adaptive_fps is not None:
            args += ['-fps_mode', 'vfr', '-enc_time_base', '1:240000',
                     '-video_track_timescale', '240000']
        if options.codec == 'hevc':
            args += ['-tag:v', 'hvc1']
        args += ['-movflags', '+faststart', str(video)]
        run_ffmpeg(args, expected, stop, lambda p: progress(p * (.9 if options.music else 1)))
        result = video
        if options.music:
            actual, _ = metadata(video, stop)
            audio_filter = 'adelay=300:all=1,apad,'
            if options.fade_out_audio:
                info = probe(options.music, stop)
                try:
                    music_duration = float(info.get('format', {}).get('duration') or 0)
                except (ValueError, TypeError):
                    music_duration = 0
                end = min(actual, music_duration + .3) if math.isfinite(music_duration) and music_duration > 0 else actual
                fade_length = min(options.fade_out_audio_duration, max(0.001, end - .3))
                audio_filter += f'afade=t=out:st={max(.3, end - fade_length):.9f}:d={fade_length:.9f},'
            audio_filter += f'atrim=duration={actual:.9f},asetpts=PTS-STARTPTS'
            run_ffmpeg(['-i', str(video), '-i', str(options.music), '-map', '0:v:0',
                        '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                        '-af', audio_filter,
                        '-t', f'{actual:.9f}', '-movflags', '+faststart', str(mux)],
                       actual, stop, lambda p: progress(.9 + p * .1))
            result = mux
        checkpoint(stop)
        if overwrite:
            # Só troca o destino depois de concluir todas as etapas com sucesso.
            os.replace(result, target)
        else:
            # Um arquivo criado após a verificação nunca é sobrescrito sem autorização.
            os.link(result, target)
        progress(1)
        log(f'Concluído: {target.name}')
