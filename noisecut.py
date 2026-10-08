#!/usr/bin/env python3
"""NoiseCut Studio: editor de video de escritorio con reducción de ruido de fondo.
PySide6 (interfaz) + FFmpeg (procesamiento y exportación)."""
import sys, os, json, shutil, subprocess, tempfile, copy, wave, struct, math, hashlib, importlib.util
import urllib.request, urllib.error
from dataclasses import dataclass, field
from PySide6.QtCore import Qt, QUrl, QThread, Signal, QSize, QPointF, QRectF, QTimer
from PySide6.QtGui import QColor, QAction, QIcon, QBrush, QPen, QPainter, QPixmap, QFont, QFontMetrics
from PySide6.QtWidgets import *
from PySide6.QtMultimedia import (QMediaPlayer, QAudioOutput, QMediaCaptureSession,
                                  QAudioInput, QMediaRecorder, QMediaFormat, QMediaDevices)
from PySide6.QtMultimediaWidgets import QVideoWidget

W, H, FPS = 1920, 1080, 30
VID = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}
IMG = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}
AUD = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'}
NOWIN = 0x08000000 if os.name == 'nt' else 0


def resource_path(*parts):
    root = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def ffbin(name):
    here = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'ffmpeg', name)
    for p in (here, here + '.exe'):
        if os.path.exists(p):
            return p
    return shutil.which(name)


def ffmpeg_has_filter(name):
    binary = ffbin('ffmpeg')
    if not binary:
        return False
    try:
        result = subprocess.run([binary, '-hide_banner', '-filters'], capture_output=True,
                                 text=True, creationflags=NOWIN, timeout=12)
        return result.returncode == 0 and any(len(parts := line.split()) > 1 and parts[1] == name
                                               for line in result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return False


def probe(path):
    r = subprocess.run([ffbin('ffprobe'), '-v', 'error', '-show_entries', 'format=duration:stream=codec_type',
                        '-of', 'json', path], capture_output=True, text=True, creationflags=NOWIN)
    j = json.loads(r.stdout or '{}')
    dur = float(j.get('format', {}).get('duration') or 0)
    return dur, any(s.get('codec_type') == 'audio' for s in j.get('streams', []))


@dataclass
class Clip:
    path: str
    kind: str
    src: float
    audio: bool
    start: float = 0.0
    end: float = 0.0
    speed: float = 1.0
    bright: float = 0.0
    contrast: float = 1.0
    sat: float = 1.0
    blur: float = 0.0
    denoise: int = 0
    fade: float = 0.0
    scale: float = 100.0
    pos_x: float = 50.0
    pos_y: float = 50.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    crop_left: float = 0.0
    crop_right: float = 0.0
    crop_top: float = 0.0
    crop_bottom: float = 0.0
    opacity: float = 100.0
    reverse: bool = False
    volume: float = 100.0
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0
    denoise_mode: str = 'standard'
    normalize: bool = False
    voice_enhance: bool = False
    pitch: int = 0
    effects: dict = field(default_factory=dict)
    chroma_color: str = '#00ff00'
    chroma_similarity: float = 0.25
    chroma_enabled: bool = False
    lut_path: str = ''
    transition: str = 'cut'
    transition_duration: float = 0.5

    @property
    def out(self):
        return max(0.1, (self.end - self.start) / self.speed)


def esc(p):
    return p.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")


def sysfont():
    for p in ('C:/Windows/Fonts/arial.ttf', '/System/Library/Fonts/Supplemental/Arial.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(p):
            return p


def atempo(s):
    f = []
    while s > 2:
        f.append('atempo=2'); s /= 2
    while s < 0.5:
        f.append('atempo=0.5'); s *= 2
    f.append(f'atempo={s:.4f}')
    return f


def denoise_f(nr, mode='standard', model=None):
    # Reducción espectral (FFmpeg afftdn) + filtro de graves (retumbos, ventiladores)
    if nr <= 0:
        return None
    if mode == 'ai':
        if not model:
            # Sin modelo local, nunca bloquea el proyecto: vuelve al filtro estándar.
            mode = 'standard'
        else:
            return f"highpass=f=80,arnndn=m='{esc(model)}'"
    return f'highpass=f=80,afftdn=nr={nr}:nf=-30:tn=1' if nr > 0 else None


def audio_fx_filters(nr=0, mode='standard', model=None, normalize=False,
                     voice_enhance=False, pitch=0):
    filters = []
    denoise = denoise_f(nr, mode, model)
    if denoise:
        filters.extend(denoise.split(','))
    if voice_enhance:
        if not denoise:
            filters.append('highpass=f=80')
        filters.extend(['agate=threshold=0.015:ratio=2.5:attack=20:release=250:range=0.02',
                        'equalizer=f=120:t=q:w=1:g=-3',
                        'equalizer=f=3000:t=q:w=1:g=3',
                        'acompressor=threshold=-22dB:ratio=3:attack=10:release=200'])
    if normalize:
        filters.append('loudnorm=I=-16:TP=-1.5:LRA=11')
    if pitch:
        ratio = 2 ** (pitch / 12)
        filters += [f'asetrate=48000*{ratio:.6f}', 'aresample=48000'] + atempo(1 / ratio)
    return filters


def video_fx_filters(effects=None, width=1920, height=1080, chroma=False,
                     chroma_color='#00ff00', chroma_similarity=0.25, lut_path=''):
    effects = effects or {}
    filters = []
    vignette = float(effects.get('vignette', 0))
    grain = int(effects.get('grain', 0))
    sharp = float(effects.get('sharpness', 0))
    pixel = int(effects.get('pixelate', 0))
    if vignette > 0:
        filters.append(f'vignette=angle={min(math.pi / 2, vignette * math.pi / 200):.4f}')
    if grain > 0:
        filters.append(f'noise=alls={min(100, grain)}:allf=t')
    if sharp > 0:
        filters.append(f'unsharp=5:5:{min(5, sharp):.2f}:5:5:0')
    if effects.get('mirror'):
        filters.append('hflip')
    if effects.get('glitch'):
        filters.append('rgbashift=rh=4:bh=-4')
    if effects.get('black_white'):
        filters.append('hue=s=0')
    if effects.get('sepia'):
        filters.append('colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131')
    if pixel > 0:
        factor = max(2, pixel)
        filters.append(f'scale=trunc(iw/{factor}):trunc(ih/{factor}):flags=neighbor,scale={width}:{height}:flags=neighbor')
    if lut_path:
        filters.append(f"lut3d=file='{esc(lut_path)}'")
    if chroma:
        filters.append('format=rgba')
        color = chroma_color.lstrip('#')
        filters.append(f'colorkey=0x{color}:{max(0,min(1,chroma_similarity)):.3f}:0.10')
    return filters


def subtitle_records(segments):
    """Convierte segmentos Whisper en elementos editables de la pista de texto."""
    return [{'text': str(text).strip(), 'start': float(start), 'end': max(float(start) + 0.1, float(end)),
             'x': 50, 'y': 85, 'size': 48, 'color': '#ffffff', 'bold': True,
             'border': True, 'shadow': True, 'background': False, 'animation': 'fade',
             'is_subtitle': True}
            for start, end, text in segments if str(text).strip()]


def stabilization_commands(ffmpeg, source, transform_file, output):
    detect = [ffmpeg, '-y', '-hide_banner', '-nostats', '-i', source,
              '-vf', f'vidstabdetect=shakiness=5:accuracy=15:result={esc(transform_file)}',
              '-f', 'null', '-']
    apply = [ffmpeg, '-y', '-hide_banner', '-nostats', '-i', source,
             '-vf', f'vidstabtransform=input={esc(transform_file)}:smoothing=10:optzoom=1',
             '-map', '0:v:0', '-map', '0:a?', '-c:v', 'libx264', '-preset', 'medium',
             '-crf', '18', '-c:a', 'aac', '-b:a', '192k', output]
    return detect, apply


def canvas_size(settings):
    resolution = int(settings.get('resolution', 1080))
    aspect = settings.get('aspect', '16:9')
    ratios = {'16:9': (16, 9), '9:16': (9, 16), '1:1': (1, 1), '4:5': (4, 5)}
    rw, rh = ratios.get(aspect, ratios['16:9'])
    if rw >= rh:
        height = resolution
        width = int(resolution * rw / rh)
    else:
        width = resolution
        height = int(resolution * rh / rw)
    return width, height


def project_duration(clips, music=None, overlays=None, texts=None, stickers=None):
    clips = list(clips or [])
    active = [i for i in range(1, len(clips))
              if getattr(clips[i], 'transition', 'cut') != 'cut'
              and getattr(clips[i], 'transition_duration', 0) > 0]
    total = sum(c.out for c in clips) - sum(min(clips[i].transition_duration,
                clips[i - 1].out, clips[i].out) for i in active)
    end_times = [m.get('offset', 0) + m.get('duration', 0) for m in music or []]
    end_times += [item.get('end', 0) for collection in (overlays, texts, stickers)
                  for item in collection or []]
    return max(total, max(end_times, default=0), 0)


def preview_fingerprint(state, media_paths=(), audio_mode='after'):
    media = []
    for path in sorted(set(p for p in media_paths if p)):
        try:
            stat = os.stat(path)
            media.append((path, stat.st_size, stat.st_mtime_ns))
        except OSError:
            media.append((path, None, None))
    payload = json.dumps({'state': state, 'media': media, 'audio_mode': audio_mode},
                         ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def build_mp3(clips, music, out, settings):
    a = [ffbin('ffmpeg'), '-y', '-hide_banner', '-nostats', '-progress', 'pipe:1']
    fc, n = [], 0
    total = max(sum(c.out for c in clips), 0.1)
    labels = ['ac']
    for c in clips:
        if c.kind != 'video' or not c.audio:
            continue
        a += ['-i', c.path]
        f = [f'atrim=start={c.start}:end={c.end}', 'asetpts=PTS-STARTPTS']
        if c.reverse:
            f.append('areverse')
        f += atempo(c.speed)
        f += audio_fx_filters(c.denoise, c.denoise_mode, settings.get('rnnoise_model'),
                              c.normalize, c.voice_enhance, c.pitch)
        f.append(f'volume={c.volume / 100:.3f}')
        if c.audio_fade_in > 0:
            f.append(f'afade=t=in:st=0:d={c.audio_fade_in}')
        if c.audio_fade_out > 0:
            f.append(f'afade=t=out:st={max(0, c.out - c.audio_fade_out):.3f}:d={c.audio_fade_out}')
        f.append('aformat=sample_rates=48000:channel_layouts=stereo')
        fc.append(f'[{n}:a]{",".join(f)}[clip{n}]'); labels.append(f'clip{n}'); n += 1
    total = max(total, max((m.get('offset', 0) + m.get('duration', 0) for m in music), default=0), 0.1)
    fc.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[ac]')
    for m in music:
        a += ['-i', m['path']]
        f = []
        if m.get('source_end') is not None:
            f += [f"atrim=start={m.get('source_start', 0)}:end={m['source_end']}", 'asetpts=PTS-STARTPTS']
            f += atempo(m.get('speed', 1))
        f += audio_fx_filters(m.get('denoise', 0), m.get('denoise_mode', 'standard'),
                              settings.get('rnnoise_model'), m.get('normalize', False),
                              m.get('voice_enhance', False), m.get('pitch', 0))
        f.append(f"volume={m['vol']}")
        if m.get('fade_in', 0) > 0:
            f.append(f"afade=t=in:st=0:d={m['fade_in']}")
        if m.get('fade_out', 0) > 0 and m.get('duration', 0) > 0:
            f.append(f"afade=t=out:st={max(0, m['duration'] - m['fade_out']):.3f}:d={m['fade_out']}")
        f += [f"adelay={int(m['offset'] * 1000)}|{int(m['offset'] * 1000)}",
              'aformat=sample_rates=48000:channel_layouts=stereo']
        fc.append(f'[{n}:a]{",".join(f)}[music{n}]'); labels.append(f'music{n}'); n += 1
    fc.append(''.join(f'[{label}]' for label in labels) +
              f'amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0[aout]')
    quality = settings.get('quality', 'alta')
    a += ['-filter_complex', ';'.join(fc), '-map', '[aout]', '-vn', '-c:a', 'libmp3lame',
          '-q:a', str({'baja': 6, 'media': 4, 'alta': 2}.get(quality, 2)), out]
    return a, total


def build(clips, texts, stickers, music, out, tmp, size=None, overlays=None, settings=None):
    settings = settings or {}
    overlays = overlays or []
    width, height = size or canvas_size(settings)
    fps = int(settings.get('fps', FPS))
    quality = settings.get('quality', 'alta')
    background = settings.get('background', 'negro')
    file_format = settings.get('format', 'MP4').upper()
    crf = {'baja': 28, 'media': 23, 'alta': 18}.get(quality, 18)
    if file_format == 'MP3':
        return build_mp3(clips, music, out, settings)
    a = [ffbin('ffmpeg'), '-y', '-hide_banner', '-nostats', '-progress', 'pipe:1']
    fc, n = [], 0
    for i, c in enumerate(clips):
        if c.kind == 'image':
            a += ['-loop', '1', '-framerate', str(fps), '-t', f'{c.out:.3f}', '-i', c.path]
        else:
            a += ['-i', c.path]
        n += 1
        v = [] if c.kind == 'image' else [f'trim=start={c.start}:end={c.end}']
        if any((c.crop_left, c.crop_right, c.crop_top, c.crop_bottom)):
            cw = max(0.1, 1 - (c.crop_left + c.crop_right) / 100)
            ch = max(0.1, 1 - (c.crop_top + c.crop_bottom) / 100)
            v.append(f'crop=iw*{cw:.4f}:ih*{ch:.4f}:iw*{c.crop_left / 100:.4f}:ih*{c.crop_top / 100:.4f}')
        if c.flip_h:
            v.append('hflip')
        if c.flip_v:
            v.append('vflip')
        if c.rotation:
            v.append(f'rotate={c.rotation}*PI/180:ow=rotw(iw):oh=roth(ih):c=black@0')
        if c.reverse:
            v += ['reverse', f'setpts=(PTS-STARTPTS)/{c.speed}']
        else:
            v.append(f'setpts=(PTS-STARTPTS)/{c.speed}')
        target_w = max(2, int(width * c.scale / 100))
        target_h = max(2, int(height * c.scale / 100))
        v += [f'fps={fps}', f'scale={target_w}:{target_h}:force_original_aspect_ratio=decrease',
              'setsar=1', f'eq=brightness={c.bright}:contrast={c.contrast}:saturation={c.sat}']
        if c.blur > 0:
            v.append(f'gblur=sigma={c.blur}')
        v += video_fx_filters(c.effects, width, height, c.chroma_enabled,
                              c.chroma_color, c.chroma_similarity, c.lut_path)
        fo = max(0, c.out - c.fade)
        if c.fade > 0:
            v += [f'fade=t=in:st=0:d={c.fade}', f'fade=t=out:st={fo:.3f}:d={c.fade}']
        v += ['format=rgba', f'colorchannelmixer=aa={max(0, min(100, c.opacity)) / 100:.3f}']
        fc.append(f'[{i}:v]{",".join(v)}[fg{i}]')
        if background == 'desenfocado':
            back = [f'trim=start={c.start}:end={c.end}', f'setpts=(PTS-STARTPTS)/{c.speed}',
                    f'fps={fps}', f'scale={width}:{height}:force_original_aspect_ratio=increase',
                    f'crop={width}:{height}', 'gblur=sigma=24']
            fc.append(f'[{i}:v]{",".join(back)}[bg{i}]')
        else:
            fc.append(f'color=c=black:s={width}x{height}:r={fps}:d={c.out:.3f}[bg{i}]')
        fc.append(f"[bg{i}][fg{i}]overlay=x=(main_w-overlay_w)*{c.pos_x}/100:y=(main_h-overlay_h)*{c.pos_y}/100:shortest=1[v{i}]")
        if c.kind == 'video' and c.audio:
            f = [f'atrim=start={c.start}:end={c.end}']
            if c.reverse:
                f += ['areverse', 'asetpts=PTS-STARTPTS']
            else:
                f.append('asetpts=PTS-STARTPTS')
            f += atempo(c.speed)
            f += audio_fx_filters(c.denoise, c.denoise_mode, settings.get('rnnoise_model'),
                                  c.normalize, c.voice_enhance, c.pitch)
            f.append(f'volume={c.volume / 100:.3f}')
            if c.audio_fade_in > 0:
                f.append(f'afade=t=in:st=0:d={c.audio_fade_in}')
            if c.audio_fade_out > 0:
                f.append(f'afade=t=out:st={max(0, c.out - c.audio_fade_out):.3f}:d={c.audio_fade_out}')
            f.append('aformat=sample_rates=48000:channel_layouts=stereo')
            fc.append(f'[{i}:a]{",".join(f)}[a{i}]')
        else:
            fc.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{c.out:.3f},asetpts=PTS-STARTPTS[a{i}]')
    transition_specs = {'fade', 'dissolve', 'wipeleft', 'wiperight', 'wipeup', 'wipedown',
                        'slideleft', 'slideright', 'slideup', 'slidedown', 'circleopen',
                        'circleclose', 'zoomin', 'radial', 'smoothleft', 'smoothright',
                        'smoothup', 'smoothdown', 'pixelize'}
    active_transitions = [i for i in range(1, len(clips))
                          if clips[i].transition in transition_specs and clips[i].transition_duration > 0]
    T = sum(c.out for c in clips) - sum(min(clips[i].transition_duration,
            clips[i - 1].out, clips[i].out) for i in active_transitions)
    T = max(T, max((m.get('offset', 0) + m.get('duration', 0) for m in music), default=0), 0.1)
    if clips:
        if active_transitions:
            # xfade combina los clips secuencialmente; cada clip trae su transición de entrada.
            for i in range(len(clips)):
                fc.append(f'[v{i}]settb=AVTB,format=yuv420p[xf{i}]')
            current, elapsed = 'xf0', clips[0].out
            for i in range(1, len(clips)):
                c = clips[i]
                if c.transition in transition_specs and c.transition_duration > 0:
                    d = min(c.transition_duration, clips[i - 1].out, c.out)
                    offset = max(0, elapsed - d)
                    outlabel = f'xfade{i}'
                    fc.append(f'[{current}][xf{i}]xfade=transition={c.transition}:duration={d:.3f}:offset={offset:.3f}[{outlabel}]')
                    elapsed = offset + c.out
                else:
                    outlabel = f'xfade{i}'
                    fc.append(f'[{current}][xf{i}]concat=n=2:v=1:a=0[{outlabel}]')
                    elapsed += c.out
                current = outlabel
            audio_cur = 'ac0'
            fc.append('[a0]anull[ac0]')
            for i in range(1, len(clips)):
                d = min(clips[i].transition_duration, clips[i - 1].out, clips[i].out) if i in active_transitions else 0
                label = f'across{i}'
                if d > 0:
                    fc.append(f'[{audio_cur}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{label}]')
                else:
                    fc.append(f'[{audio_cur}][a{i}]concat=n=2:v=0:a=1[{label}]')
                audio_cur = label
            fc.append(f'[{audio_cur}]anull[ac]')
            cur = current
        else:
            cat = ''.join(f'[v{i}][a{i}]' for i in range(len(clips)))
            fc.append(f'{cat}concat=n={len(clips)}:v=1:a=1[vc][ac]')
            cur = 'vc'
    else:
        if file_format != 'MP3':
            raise ValueError('Añade un video o selecciona el formato MP3 para exportar solo audio.')
        fc.append(f'anullsrc=r=48000:cl=stereo,atrim=0:{T:.3f},asetpts=PTS-STARTPTS[ac]')
        cur = ''
    font = sysfont()
    for overlay in overlays if clips else []:
        path, start, end = overlay['path'], overlay['start'], overlay['end']
        if os.path.splitext(path)[1].lower() in IMG:
            a += ['-loop', '1', '-framerate', str(fps), '-t', f'{max(0.1, end - start):.3f}', '-i', path]
        else:
            a += ['-i', path]
        j, k = n, len(fc); n += 1
        source_start = overlay.get('source_start', 0)
        source_end = overlay.get('source_end')
        trim = f'trim=start={source_start}' + (f':end={source_end}' if source_end is not None else '')
        scale = max(1, int(width * overlay.get('scale', 25) / 100))
        fc.append(f'[{j}:v]{trim},setpts=PTS-STARTPTS+{start}/TB,scale={scale}:-1,format=rgba[ov{k}]')
        fc.append(f"[{cur}][ov{k}]overlay=x=(main_w-overlay_w)*{overlay.get('x', 75)}/100:y=(main_h-overlay_h)*{overlay.get('y', 15)}/100:enable='between(t,{start},{end})':eof_action=pass[x{k}]")
        cur = f'x{k}'
    for t in (texts if clips else []):
        if t.get('is_subtitle') and not settings.get('burn_subtitles', True):
            continue
        s0, e0 = t['start'], t['end']
        text_font = font
        if t.get('bold') and font and os.path.basename(font).lower() == 'arial.ttf':
            bold_font = os.path.join(os.path.dirname(font), 'arialbd.ttf')
            if os.path.exists(bold_font):
                text_font = bold_font
        style = []
        if text_font:
            style.append(f"fontfile='{esc(text_font)}'")
        style += [f"fontsize={t['size']}", f"fontcolor={t['color']}",
                  f"borderw={2 if t.get('border', True) else 0}", 'bordercolor=black@0.8']
        if t.get('shadow'):
            style += ['shadowx=3', 'shadowy=3', 'shadowcolor=black@0.8']
        if t.get('background'):
            style += ['box=1', f"boxcolor=0x{t.get('background_color', '#000000').lstrip('#')}@0.75", 'boxborderw=10']
        style = ':'.join(style)
        animation = t.get('animation', 'fade')
        parts = list(enumerate(t['text'], start=1)) if animation == 'write' else [(0, t['text'])]
        for idx, _ in parts:
            k = len(fc)
            fp = os.path.join(tmp, f't{k}.txt')
            with open(fp, 'w', encoding='utf-8') as fh:
                fh.write(t['text'][:idx] if animation == 'write' else t['text'])
            enable_start, enable_end = s0, e0
            xexpr = f'(w-text_w)*{t["x"]}/100'
            alpha = ''
            if animation == 'fade':
                alpha = f":alpha='if(lt(t,{s0}),0,min(1,(t-{s0})/0.4))'"
            elif animation == 'slide':
                xexpr = f'(w-text_w)*{t["x"]}/100-(1-min(1,max(0,(t-{s0})/0.5)))*w'
            elif animation == 'write':
                step = min(0.08, 1.2 / max(1, len(parts)))
                enable_start = s0 + (idx - 1) * step
                enable_end = e0 if idx == len(parts) else s0 + idx * step
            fc.append(f"[{cur}]drawtext=textfile='{esc(fp)}':{style}:x={xexpr}:y=(h-text_h)*{t['y']}/100"
                      f"{alpha}:enable='between(t,{enable_start:.3f},{enable_end:.3f})'[x{k}]")
            cur = f'x{k}'
    for s in stickers if clips else []:
        a += ['-loop', '1', '-framerate', str(fps), '-t', f'{T:.3f}', '-i', s['path']]
        j, k = n, len(fc)
        n += 1
        fc.append(f"[{j}:v]scale={int(width * s['w'] / 100)}:-1,format=rgba[s{k}]")
        fc.append(f"[{cur}][s{k}]overlay=x=(main_w-overlay_w)*{s['x']}/100:y=(main_h-overlay_h)*{s['y']}/100"
                  f":enable='between(t,{s['start']},{s['end']})'[y{k}]")
        cur = f'y{k}'
    labels = ['ac']
    for m in music:
        a += ['-i', m['path']]
        j = n
        n += 1
        f = []
        if m.get('source_end') is not None:
            f += [f"atrim=start={m.get('source_start', 0)}:end={m['source_end']}", 'asetpts=PTS-STARTPTS']
            f += atempo(m.get('speed', 1))
        f += audio_fx_filters(m.get('denoise', 0), m.get('denoise_mode', 'standard'),
                              settings.get('rnnoise_model'), m.get('normalize', False),
                              m.get('voice_enhance', False), m.get('pitch', 0))
        ms = int(m['offset'] * 1000)
        f.append(f"volume={m['vol']}")
        if m.get('fade_in', 0) > 0:
            f.append(f"afade=t=in:st=0:d={m['fade_in']}")
        if m.get('fade_out', 0) > 0 and m.get('duration', 0) > 0:
            f.append(f"afade=t=out:st={max(0, m['duration'] - m['fade_out']):.3f}:d={m['fade_out']}")
        f += [f'adelay={ms}|{ms}', 'aformat=sample_rates=48000:channel_layouts=stereo']
        fc.append(f'[{j}:a]{",".join(f)}[m{j}]')
        labels.append(f'm{j}')
    if len(labels) > 1:
        fc.append(''.join(f'[{l}]' for l in labels) +
                  f'amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0[aout]')
    else:
        fc.append('[ac]anull[aout]')
    a += ['-filter_complex', ';'.join(fc)]
    if file_format == 'MP3':
        a += ['-map', '[aout]', '-vn', '-c:a', 'libmp3lame', '-q:a', str({'baja': 6, 'media': 4, 'alta': 2}.get(quality, 2)), out]
    elif file_format == 'GIF':
        a += ['-map', f'[{cur}]', '-an', '-vf', f'fps={fps},scale={width}:-1:flags=lanczos', '-loop', '0', out]
    else:
        preset = 'ultrafast' if settings.get('preview') else 'medium'
        a += ['-map', f'[{cur}]', '-map', '[aout]', '-c:v', 'libx264', '-preset', preset, '-crf', str(crf),
              '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out]
    return a, T


class Worker(QThread):
    prog = Signal(int)
    done = Signal(str)

    def __init__(s, cmd, total):
        super().__init__()
        s.cmd, s.total = cmd, max(total, 0.1)
        s.process = None; s.cancel_requested = False

    def cancel(s):
        s.cancel_requested = True
        if s.process and s.process.poll() is None:
            s.process.terminate()

    def run(s):
        p = subprocess.Popen(s.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding='utf-8', errors='replace', creationflags=NOWIN)
        s.process = p
        if s.cancel_requested:
            p.terminate()
        tail = []
        for line in p.stdout:
            if line.startswith('out_time_us='):
                try:
                    s.prog.emit(min(99, int(int(line.split('=')[1]) / 1e6 / s.total * 100)))
                except ValueError:
                    pass
            else:
                tail = (tail + [line])[-15:]
        p.wait()
        if s.cancel_requested:
            s.done.emit('__CANCELLED__')
        else:
            s.done.emit('' if p.returncode == 0 else ''.join(tail))


class ModelDownloadWorker(QThread):
    progress = Signal(int)
    completed = Signal(bool, str)

    def __init__(self, url, destination):
        super().__init__()
        self.url, self.destination, self.cancelled = url, destination, False

    def cancel(self):
        self.cancelled = True

    def run(self):
        partial = self.destination + '.part'
        try:
            request = urllib.request.Request(self.url, headers={'User-Agent': 'NoiseCutStudio'})
            with urllib.request.urlopen(request, timeout=30) as response:
                size = int(response.headers.get('Content-Length') or 0)
                received = 0
                with open(partial, 'wb') as output:
                    while True:
                        if self.cancelled:
                            raise InterruptedError('Descarga cancelada.')
                        chunk = response.read(16384)
                        if not chunk:
                            break
                        output.write(chunk); received += len(chunk)
                        if size:
                            self.progress.emit(min(100, int(received * 100 / size)))
            with open(partial, 'rb') as source:
                header = source.readline().decode('ascii', errors='ignore')
            if not header.startswith('rnnoise-nu model file version'):
                raise ValueError('El archivo descargado no parece ser un modelo RNNoise válido.')
            os.replace(partial, self.destination)
            self.completed.emit(True, '')
        except Exception as exc:
            try:
                if os.path.exists(partial):
                    os.remove(partial)
            except OSError:
                pass
            self.completed.emit(False, str(exc))


class WhisperWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(object, str)

    def __init__(self, path, model_size, language, cache_dir):
        super().__init__(); self.path, self.model_size = path, model_size
        self.language, self.cache_dir, self.cancelled = language, cache_dir, False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            from faster_whisper import WhisperModel
            self.progress.emit(3, 'Descargando o preparando Whisper en segundo plano…')
            model = WhisperModel(self.model_size, device='cpu', compute_type='int8', download_root=self.cache_dir)
            if self.cancelled:
                raise InterruptedError('Proceso cancelado.')
            self.progress.emit(10, 'Reconociendo la voz…')
            language = None if self.language == 'auto' else self.language
            segments, info = model.transcribe(self.path, language=language, vad_filter=True)
            found = []
            total = max(1.0, float(getattr(info, 'duration', 1.0) or 1.0))
            for segment in segments:
                if self.cancelled:
                    raise InterruptedError('Proceso cancelado.')
                found.append((segment.start, segment.end, segment.text))
                self.progress.emit(min(99, 10 + int(segment.end / total * 89)), 'Creando subtítulos…')
            self.completed.emit(found, '')
        except InterruptedError as exc:
            self.completed.emit(None, str(exc))
        except Exception as exc:
            self.completed.emit(None, f'{type(exc).__name__}: {exc}')


class BackgroundWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(str, str)

    def __init__(self, source, destination, fps=30):
        super().__init__(); self.source, self.destination = source, destination
        self.fps, self.cancelled = max(1, min(60, int(fps))), False

    def cancel(self):
        self.cancelled = True

    def run(self):
        folder = tempfile.mkdtemp(prefix='noisecut_remove_bg_')
        frames, transparent = os.path.join(folder, 'frames'), os.path.join(folder, 'alpha')
        try:
            from rembg import remove, new_session
            from PIL import Image
            os.makedirs(frames); os.makedirs(transparent)
            ffmpeg = ffbin('ffmpeg')
            if not ffmpeg:
                raise RuntimeError('No se encuentra FFmpeg.')
            result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', self.source,
                '-vsync', '0', os.path.join(frames, '%08d.png')], capture_output=True,
                text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
            paths = sorted(os.path.join(frames, name) for name in os.listdir(frames) if name.endswith('.png'))
            if not paths:
                raise RuntimeError('No se pudieron extraer cuadros del video.')
            self.progress.emit(15, 'Preparando el modelo ligero de segmentación…')
            session = new_session('u2netp')
            for i, path in enumerate(paths):
                if self.cancelled:
                    raise InterruptedError('Proceso cancelado.')
                with Image.open(path) as image:
                    result_png = remove(image.convert('RGBA'), session=session)
                with open(os.path.join(transparent, os.path.basename(path)), 'wb') as output:
                    output.write(result_png)
                self.progress.emit(15 + int((i + 1) * 75 / len(paths)), f'Procesando cuadro {i + 1} de {len(paths)}…')
            silent = os.path.join(folder, 'transparente.mov')
            result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-framerate', str(self.fps),
                '-i', os.path.join(transparent, '%08d.png'), '-c:v', 'qtrle', '-pix_fmt', 'argb', silent],
                capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
            self.progress.emit(95, 'Añadiendo el audio original…')
            result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', silent, '-i', self.source,
                '-map', '0:v:0', '-map', '1:a?', '-c:v', 'copy', '-c:a', 'aac', '-shortest', self.destination],
                capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:])
            self.completed.emit(self.destination, '')
        except Exception as exc:
            self.completed.emit('', f'{type(exc).__name__}: {exc}')
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class StabilizeWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(str, str)

    def __init__(self, source, output):
        super().__init__(); self.source, self.output = source, output

    def run(self):
        folder = tempfile.mkdtemp(prefix='noisecut_stabilize_')
        trf = os.path.join(folder, 'movimiento.trf')
        try:
            detect, apply = stabilization_commands(ffbin('ffmpeg'), self.source, trf, self.output)
            self.progress.emit(10, 'Analizando el movimiento del video (paso 1 de 2)…')
            result = subprocess.run(detect, capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1200:])
            self.progress.emit(55, 'Corrigiendo el movimiento (paso 2 de 2)…')
            result = subprocess.run(apply, capture_output=True, text=True, creationflags=NOWIN)
            if result.returncode:
                raise RuntimeError(result.stderr[-1200:])
            self.completed.emit(self.output, '')
        except Exception as exc:
            self.completed.emit('', f'{type(exc).__name__}: {exc}')
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def pick(b):
    c = QColorDialog.getColor(QColor(b.text()))
    if c.isValid():
        b.setText(c.name()); b.setStyleSheet(f'background:{c.name()};color:#888')


def ask(parent, title, spec, vals=None):
    d = QDialog(parent); d.setWindowTitle(title)
    f, w, vals = QFormLayout(d), {}, vals or {}
    for key, label, typ, dflt, *rng in spec:
        v = vals.get(key, dflt)
        if typ == 'text':
            e = QLineEdit(str(v))
        elif typ == 'color':
            e = QPushButton(v); e.setStyleSheet(f'background:{v};color:#888')
            e.clicked.connect(lambda _=0, b=e: pick(b))
        elif typ == 'bool':
            e = QCheckBox(); e.setChecked(bool(v))
        elif typ == 'choice':
            e = QComboBox()
            for caption, value in rng[0]:
                e.addItem(caption, value)
            e.setCurrentIndex(max(0, e.findData(v)))
        else:
            e = QDoubleSpinBox(); e.setRange(*rng); e.setDecimals(2 if typ == 'float' else 0); e.setValue(v)
        w[key] = (typ, e); f.addRow(label, e)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept); bb.rejected.connect(d.reject); f.addRow(bb)
    if not d.exec():
        return None
    out = {}
    for k, (typ, e) in w.items():
        if typ in ('text', 'color'):
            out[k] = e.text()
        elif typ == 'bool':
            out[k] = e.isChecked()
        elif typ == 'choice':
            out[k] = e.currentData()
        else:
            out[k] = int(e.value()) if typ == 'int' else e.value()
    return out


TEXT = [('text', 'Texto', 'text', 'Hola'), ('start', 'Inicio (s)', 'float', 0, 0, 3600),
        ('end', 'Fin (s)', 'float', 5, 0, 3600), ('x', 'Posición X %', 'int', 50, 0, 100),
        ('y', 'Posición Y %', 'int', 85, 0, 100), ('size', 'Tamaño', 'int', 64, 8, 400),
        ('color', 'Color', 'color', '#ffffff'), ('bold', 'Negrita', 'bool', False),
        ('border', 'Borde', 'bool', True), ('shadow', 'Sombra', 'bool', False),
        ('background', 'Fondo de color', 'bool', False),
        ('background_color', 'Color de fondo', 'color', '#000000'),
        ('animation', 'Animación', 'choice', 'fade',
         [('Aparecer', 'fade'), ('Deslizar', 'slide'), ('Escribir', 'write'), ('Sin animación', 'none')])]
STICK = [('start', 'Inicio (s)', 'float', 0, 0, 3600), ('end', 'Fin (s)', 'float', 5, 0, 3600),
         ('x', 'Posición X %', 'int', 85, 0, 100), ('y', 'Posición Y %', 'int', 15, 0, 100),
         ('w', 'Ancho (% del video)', 'int', 20, 1, 100)]
MUSIC = [('offset', 'Empieza en (s)', 'float', 0, 0, 3600), ('vol', 'Volumen', 'float', 1.0, 0, 4),
         ('denoise', 'Reducir ruido (dB, 0 = off)', 'int', 0, 0, 40),
         ('denoise_mode', 'Modo de reducción', 'choice', 'standard',
          [('Estándar', 'standard'), ('IA · RNNoise', 'ai')]),
         ('fade_in', 'Fundido de entrada (s)', 'float', 0, 0, 30),
         ('fade_out', 'Fundido de salida (s)', 'float', 0, 0, 30),
         ('normalize', 'Normalizar volumen', 'bool', False),
         ('voice_enhance', 'Mejorar voz', 'bool', False),
         ('pitch', 'Tono (semitonos)', 'int', 0, -12, 12)]
FIELDS = [('start', 'Inicio (s)', 0, 36000, 0.1, 2), ('end', 'Fin / duración (s)', 0.1, 36000, 0.1, 2),
          ('speed', 'Velocidad ×', 0.25, 4, 0.05, 2), ('bright', 'Brillo', -1, 1, 0.05, 2),
          ('contrast', 'Contraste', 0, 3, 0.05, 2), ('sat', 'Saturación', 0, 3, 0.05, 2),
          ('blur', 'Desenfoque', 0, 30, 0.5, 1), ('fade', 'Transición: fundido (s)', 0, 3, 0.1, 1)]
STYLE = """QWidget{background:#121316;color:#e6e9ef;font-size:13px}
QListWidget,QLineEdit,QDoubleSpinBox{background:#1c1d21;border:1px solid #2a2c33;border-radius:6px;padding:5px}
QPushButton{background:#25272d;border:1px solid #2a2c33;border-radius:6px;padding:8px 12px;color:#edf1f7}
QPushButton:hover{background:#343740;border-color:#3b82f6}
QPushButton#primary{background:#2563eb;border:0;font-weight:600}
QPushButton#primary:hover{background:#1d4ed8}
QListWidget::item:selected{background:#1e3a8a;border:1px solid #60a5fa}
QGroupBox{background:#1c1d21;border:1px solid #2a2c33;border-radius:6px;margin-top:10px;padding:10px}
QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px}
QTabWidget::pane{border:1px solid #2a2c33;border-radius:6px;background:#1c1d21}
QTabBar::tab{padding:8px 10px;background:#1c1d21;border:1px solid #2a2c33}
QTabBar::tab:selected{background:#23262d;color:#7dd3fc;border-bottom:2px solid #38bdf8}"""


class MediaGrid(QListWidget):
    hoverChanged = Signal(object, object)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        item = self.itemAt(event.position().toPoint())
        self.hoverChanged.emit(item, self.visualItemRect(item) if item else QRectF())

    def leaveEvent(self, event):
        self.hoverChanged.emit(None, QRectF())
        super().leaveEvent(event)


class TimelineBlock(QGraphicsRectItem):
    def __init__(self, rect, index, color, view):
        super().__init__(rect)
        self.index, self.view = index, view
        self.setBrush(QBrush(QColor(color)))
        self.setPen(QPen(QColor('#8bd5ff'), 2 if index == view.selected else 1))
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.edge = None
        self.original_x = 0.0
        self.original_width = rect.width()
        self.press_x = 0.0

    def mousePressEvent(self, event):
        x, width = event.pos().x(), self.rect().width()
        self.edge = 'left' if x < 8 else ('right' if x > width - 8 else None)
        self.original_x = self.pos().x()
        self.original_width = width
        self.press_x = event.scenePos().x()
        self.view.select_block(self.index)
        if self.edge:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.edge == 'right':
            delta = event.scenePos().x() - self.press_x
            self.setRect(0, 0, max(36, self.original_width + delta), self.rect().height())
            event.accept()
        elif self.edge == 'left':
            delta = max(-self.original_x, min(self.original_width - 36, event.scenePos().x() - self.press_x))
            self.setPos(self.original_x + delta, self.pos().y())
            self.setRect(0, 0, self.original_width - delta, self.rect().height())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.edge:
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        self.view.block_changed(self.index, self.pos().x(), self.rect().width(), self.edge,
                                self.original_x, self.original_width)
        self.edge = None


class TimelinePlayhead(QGraphicsLineItem):
    def __init__(self, view):
        super().__init__(0, 26, 0, 254)
        self.view = view
        self.setPen(QPen(QColor('#38bdf8'), 2))
        self.setFlag(QGraphicsLineItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsLineItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)

    def itemChange(self, change, value):
        if change == QGraphicsLineItem.GraphicsItemChange.ItemPositionChange:
            return QPointF(max(76, value.x()), 0)
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.view.seek_to(self.pos().x())


class TimelineView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setMinimumHeight(220)
        self.setMaximumHeight(300)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor('#121316')))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.owner = None
        self.selected = -1
        self.scale = 72.0
        self.waveform_cache = {}
        self._items = []
        self.current_time = 0.0
        self.playhead = None

    def refresh(self):
        self.scene().clear()
        self._items = []
        self.playhead = None
        if not self.owner:
            return
        clips = [self.owner.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.owner.tl.count())]
        total = project_duration(clips, self.owner.music, self.owner.overlays,
                                 self.owner.texts, self.owner.stickers)
        total = max(total, 1)
        width = max(self.viewport().width(), int(76 + (total + 4) * self.scale))
        self.scene().setSceneRect(0, 0, width, 300)
        scene, font = self.scene(), self.font()
        font.setPointSize(8)
        for sec in range(int(width / self.scale) + 1):
            x = 76 + sec * self.scale
            scene.addLine(x, 24, x, 244, QPen(QColor('#2a2c33'), 1))
            label = scene.addText(f'{sec}s', font)
            label.setDefaultTextColor(QColor('#9ca3af')); label.setPos(x + 3, 2)
        for name, y in (('VIDEO', 36), ('VIDEO 2', 92), ('AUDIO', 137), ('TEXTO', 215), ('STICKERS', 265)):
            label = scene.addText(name, font); label.setDefaultTextColor(QColor('#a3aab8')); label.setPos(4, y)
        x = 76.0
        for i, c in enumerate(clips):
            w = max(36.0, c.out * self.scale)
            block = TimelineBlock(QRectF(0, 0, w, 42), i, '#2563eb' if c.kind == 'video' else '#0f766e', self)
            block.setPos(x, 50)
            scene.addItem(block)
            clean_tag = '🔇 ' if c.denoise > 0 or c.voice_enhance else ''
            text_x = 7
            if w >= 112:
                icon = self.owner.media_icon(c.path)
                thumb = icon.pixmap(44, 34)
                if not thumb.isNull():
                    thumb_item = scene.addPixmap(thumb); thumb_item.setParentItem(block); thumb_item.setPos(4, 4)
                    text_x = 53
            available = max(8, int(w - text_x - 8))
            name = clean_tag + os.path.basename(c.path)
            label = scene.addText(QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideRight, available), font)
            label.setDefaultTextColor(QColor('white')); label.setPos(text_x, 13); label.setParentItem(block)
            self._items.append(block)
            x += w + 4
        if not self.owner.overlays:
            hint = scene.addText('Arrastra aquí', font); hint.setDefaultTextColor(QColor('#505661')); hint.setPos(86, 103)
        for j, overlay in enumerate(self.owner.overlays):
            bx = 76 + overlay['start'] * self.scale
            bw = max(16, (overlay['end'] - overlay['start']) * self.scale)
            scene.addRect(bx, 98 + (j % 2) * 12, bw, 10, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#38bdf8')))
        for j, track in enumerate(self.owner.music):
            bx = 76 + track['offset'] * self.scale
            bw = max(36, min(width - bx, track.get('duration', 8) * self.scale))
            y = 137 + j * 48
            scene.addRect(bx, y, bw, 44, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#14594f')))
            waveform = self.waveform_for(track.get('path', ''), int(max(100, min(1200, bw))))
            if waveform and not waveform.isNull():
                pix = waveform.scaled(max(1, int(bw - 4)), 38, Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                item = scene.addPixmap(pix); item.setPos(bx + 2, y + 2)
        if not self.owner.music:
            hint = scene.addText('Arrastra aquí', font); hint.setDefaultTextColor(QColor('#505661')); hint.setPos(86, 151)
        if not self.owner.texts:
            hint = scene.addText('Arrastra aquí', font); hint.setDefaultTextColor(QColor('#505661')); hint.setPos(86, 221)
        for j, text in enumerate(self.owner.texts):
            bx = 76 + text['start'] * self.scale
            bw = max(16, (text['end'] - text['start']) * self.scale)
            scene.addRect(bx, 229 + (j % 2) * 12, bw, 10, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#a855f7')))
        for j, sticker in enumerate(self.owner.stickers):
            bx = 76 + sticker['start'] * self.scale
            bw = max(16, (sticker['end'] - sticker['start']) * self.scale)
            scene.addRect(bx, 279 + (j % 2) * 12, bw, 10, QPen(Qt.PenStyle.NoPen), QBrush(QColor('#f59e0b')))
        self.playhead = TimelinePlayhead(self)
        scene.addItem(self.playhead)
        self.set_global_time(self.current_time)

    def fit_project(self):
        if not self.owner:
            return
        clips = [self.owner.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.owner.tl.count())]
        total = max(1, project_duration(clips, self.owner.music, self.owner.overlays,
                                        self.owner.texts, self.owner.stickers))
        available = max(120, self.viewport().width() - 100)
        self.scale = max(2.0, min(240.0, available / (total + 4)))
        self.refresh()

    def waveform_for(self, path, width):
        if not path or not os.path.isfile(path):
            return QPixmap()
        key = (os.path.abspath(path), width, os.path.getmtime(path))
        if key in self.waveform_cache:
            return self.waveform_cache[key]
        pix = QPixmap()
        if os.path.splitext(path)[1].lower() == '.wav':
            try:
                with wave.open(path, 'rb') as audio:
                    count, channels = audio.getnframes(), audio.getnchannels()
                    raw = audio.readframes(count)
                    sample_width = audio.getsampwidth()
                if sample_width == 2 and count:
                    samples = struct.unpack('<' + 'h' * (len(raw) // 2), raw)
                    pix = QPixmap(width, 36); pix.fill(QColor('#14594f'))
                    painter = QPainter(pix); painter.setPen(QPen(QColor('#99f6e4'), 1))
                    per_pixel = max(1, count // width)
                    for x in range(width):
                        start, end = x * per_pixel * channels, min(len(samples), (x + 1) * per_pixel * channels)
                        peak = max((abs(v) for v in samples[start:end]), default=0) / 32768
                        half = max(1, int(peak * 17)); painter.drawLine(x, 18 - half, x, 18 + half)
                    painter.end()
            except (OSError, EOFError, wave.Error, struct.error):
                pass
        if pix.isNull() and ffbin('ffmpeg'):
            cache = os.path.join(tempfile.gettempdir(), 'noisecutstudio_waveforms')
            os.makedirs(cache, exist_ok=True)
            target = os.path.join(cache, f'{abs(hash(key))}.png')
            if not os.path.exists(target):
                try:
                    subprocess.run([ffbin('ffmpeg'), '-y', '-v', 'error', '-i', path,
                                    '-filter_complex', f'showwavespic=s={width}x36:colors=0x99f6e4',
                                    '-frames:v', '1', target], capture_output=True,
                                   creationflags=NOWIN, timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if os.path.exists(target):
                pix.load(target)
        self.waveform_cache[key] = pix
        return pix

    def select_block(self, index):
        if self.owner and 0 <= index < self.owner.tl.count():
            self.selected = index
            self.owner.tl.setCurrentRow(index)
            self.set_selected(index)

    def set_selected(self, index):
        self.selected = index
        for block in self._items:
            block.setPen(QPen(QColor('#8bd5fc'), 2 if block.index == index else 1))

    def set_global_time(self, seconds):
        self.current_time = max(0, seconds)
        if self.playhead:
            self.playhead.setPos(76 + self.current_time * self.scale, 0)

    def seek_to(self, x):
        if not self.owner:
            return
        target = max(0, (x - 76) / self.scale)
        elapsed = 0.0
        for i in range(self.owner.tl.count()):
            clip = self.owner.tl.item(i).data(Qt.ItemDataRole.UserRole)
            if target <= elapsed + clip.out or i == self.owner.tl.count() - 1:
                self.owner.tl.setCurrentRow(i)
                local = max(0, min(clip.out, target - elapsed))
                self.owner.player.setPosition(int((clip.start + local * clip.speed) * 1000))
                self.owner.player.pause()
                self.set_global_time(target)
                return
            elapsed += clip.out

    def block_changed(self, index, x, width, edge, original_x, original_width):
        if not self.owner or not 0 <= index < self.owner.tl.count():
            return
        if edge:
            clip = self.owner.tl.item(index).data(Qt.ItemDataRole.UserRole)
            if edge == 'right':
                delta = (width - original_width) / self.scale * clip.speed
                clip.end = max(clip.start + 0.1, min(clip.src, clip.end + delta))
            else:
                delta = (x - original_x) / self.scale * clip.speed
                clip.start = max(0, min(clip.end - 0.1, clip.start + delta))
            self.owner.renumber()
        else:
            ordered = sorted(self._items, key=lambda item: item.pos().x())
            new_index = ordered.index(next(item for item in ordered if item.index == index))
            if new_index != index:
                item = self.owner.tl.takeItem(index)
                self.owner.tl.insertItem(new_index, item)
                self.owner.tl.setCurrentRow(new_index)
                for n, block in enumerate(ordered):
                    block.index = n
            self.owner.renumber()
        self.refresh()


class Main(QMainWindow):
    def __init__(s):
        super().__init__()
        s.setWindowTitle('NoiseCut Studio'); s.resize(1400, 860)
        s.setMinimumSize(1100, 720)
        icon = resource_path('assets', 'icon.png')
        if os.path.exists(icon):
            s.setWindowIcon(QIcon(icon))
        s.texts, s.stickers, s.music, s.cur = [], [], [], None
        s.overlays = []
        s.export_settings = {'aspect': '16:9', 'resolution': 1080, 'fps': 30,
                             'quality': 'alta', 'format': 'MP4', 'background': 'negro',
                             'burn_subtitles': True}
        s.track_refresh = []
        s.project_path = None
        s.history, s.history_index = [], -1
        s.player, s.aout, s.video = QMediaPlayer(), QAudioOutput(), QVideoWidget()
        s.player.setAudioOutput(s.aout); s.player.setVideoOutput(s.video)
        s.timeline_preview_path = None; s.timeline_preview_hash = None
        s.preview_cache_dir = os.path.join(tempfile.gettempdir(), 'noisecutstudio_timeline_previews')
        os.makedirs(s.preview_cache_dir, exist_ok=True)
        s.timeline_preview_worker = None; s.pending_preview = False; s.fallback_playback = False
        appdata = os.environ.get('LOCALAPPDATA', tempfile.gettempdir())
        s.ai_model_dir = os.path.join(appdata, 'NoiseCutStudio', 'models')
        s.ai_model_path = os.path.join(s.ai_model_dir, 'rnnoise-conjoined-burgers.rnnn')
        s.ai_model_url = 'https://raw.githubusercontent.com/GregorR/rnnoise-models/master/conjoined-burgers-2018-08-28/cb.rnnn'
        s.whisper_model_dir = os.path.join(appdata, 'NoiseCutStudio', 'whisper')
        s.has_vidstab = ffmpeg_has_filter('vidstabtransform')
        s.capture_session = QMediaCaptureSession(s)
        s.audio_input = QAudioInput()
        s.recorder = QMediaRecorder()
        s.capture_session.setAudioInput(s.audio_input); s.capture_session.setRecorder(s.recorder)
        s.recording_path = None; s.was_recording = False
        s.recorder.recorderStateChanged.connect(s.on_record_state)
        s.tl = QListWidget(s); s.tl.hide()
        s.tl.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        s.tl.currentItemChanged.connect(s.select)

        # Panel izquierdo: biblioteca y pistas auxiliares.
        left_tabs = QTabWidget()
        media = QWidget(); media_l = QVBoxLayout(media)
        imp = QPushButton('＋  Importar'); imp.clicked.connect(s.import_media)
        add_media = QPushButton('Añadir seleccionado a la línea'); add_media.clicked.connect(s.add_selected_media)
        s.bin = MediaGrid(); s.bin.setViewMode(QListWidget.ViewMode.IconMode)
        s.bin.setIconSize(QSize(88, 56)); s.bin.setGridSize(QSize(108, 88))
        s.bin.setResizeMode(QListWidget.ResizeMode.Adjust); s.bin.setMovement(QListWidget.Movement.Static)
        s.bin.setWordWrap(True); s.bin.setMouseTracking(True); s.bin.itemDoubleClicked.connect(s.add_to_timeline)
        s.hovered_media = None
        s.media_plus = QToolButton(s.bin.viewport()); s.media_plus.setText('+')
        s.media_plus.setStyleSheet('background:#2563eb;color:white;border:0;border-radius:14px;font-size:18px;font-weight:bold')
        s.media_plus.resize(28, 28); s.media_plus.hide()
        s.media_plus.clicked.connect(lambda: s.add_to_timeline(s.hovered_media) if s.hovered_media else None)
        s.bin.hoverChanged.connect(s.media_hover)
        media_l.addWidget(imp); media_l.addWidget(add_media); media_l.addWidget(QLabel('Doble clic en un elemento para añadirlo.'))
        media_l.addWidget(s.bin, 1)
        left_tabs.addTab(media, s.label_icon('M'), 'Medios')
        s.audio_track = s.track(s.music, MUSIC, 'Audio', lambda x: f"{os.path.basename(x['path'])}  ·  {x['vol']}×  ·  ruido {x['denoise']} dB",
                                'Audio (*.mp3 *.wav *.m4a *.aac *.flac *.ogg)')
        s.text_track = s.track(s.texts, TEXT, 'Texto', lambda x: f"{x['text']}  ·  {x['start']}–{x['end']} s")
        s.sticker_track = s.track(s.stickers, STICK, 'Sticker', lambda x: f"{os.path.basename(x['path'])}  ·  {x['start']}–{x['end']} s",
                                  'Imágenes (*.png *.webp *.jpg *.jpeg)')
        audio_tools = QWidget(); audio_tools_l = QVBoxLayout(audio_tools)
        audio_tools_l.addWidget(s.audio_track, 1)
        s.record_button = QPushButton('●  Grabar voz en off'); s.record_button.clicked.connect(s.record_voiceover)
        s.record_status = QLabel('Graba con el micrófono y añade la toma como pista de audio.')
        audio_tools_l.addWidget(s.record_button); audio_tools_l.addWidget(s.record_status)
        left_tabs.addTab(audio_tools, s.label_icon('♫'), 'Audio')
        left_tabs.addTab(s.make_ai_panel(), s.label_icon('IA'), 'IA')
        text_page = QWidget(); text_l = QVBoxLayout(text_page)
        auto_subtitles = QPushButton('Generar subtítulos con IA local'); auto_subtitles.clicked.connect(s.auto_subtitles)
        s.burn_subtitles = QCheckBox('Grabar los subtítulos en el video al exportar'); s.burn_subtitles.setChecked(True)
        s.burn_subtitles.toggled.connect(lambda value: s.set_export_setting('burn_subtitles', value))
        text_l.addWidget(auto_subtitles); text_l.addWidget(s.burn_subtitles); text_l.addWidget(s.text_track, 1)
        left_tabs.addTab(text_page, s.label_icon('T'), 'Texto')
        left_tabs.addTab(s.sticker_track, s.label_icon('◇'), 'Stickers')
        left_tabs.addTab(s.make_filter_panel(), s.label_icon('✦'), 'Filtros')
        settings_page = QWidget(); settings_l = QVBoxLayout(settings_page); settings_form = QFormLayout()
        s.setting_boxes = {}
        for key, label, options in (
            ('aspect', 'Lienzo', [('16:9', '16:9'), ('9:16', '9:16'), ('1:1', '1:1'), ('4:5', '4:5')]),
            ('resolution', 'Resolución', [('720p', 720), ('1080p', 1080), ('4K', 2160)]),
            ('fps', 'Fotogramas por segundo', [('24 fps', 24), ('30 fps', 30), ('60 fps', 60)]),
            ('quality', 'Calidad', [('Alta', 'alta'), ('Media', 'media'), ('Baja', 'baja')]),
            ('format', 'Formato', [('MP4', 'MP4'), ('Solo audio · MP3', 'MP3'), ('GIF', 'GIF')]),
            ('background', 'Fondo del lienzo', [('Negro', 'negro'), ('Desenfocado', 'desenfocado')])):
            combo = QComboBox()
            for caption, value in options:
                combo.addItem(caption, value)
            current = s.export_settings[key]
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.currentIndexChanged.connect(lambda _i, k=key, box=combo: s.set_export_setting(k, box.currentData()))
            s.setting_boxes[key] = combo; settings_form.addRow(label, combo)
        settings_l.addLayout(settings_form); settings_l.addStretch()
        left_tabs.addTab(settings_page, s.label_icon('⚙'), 'Ajustes')
        left_tabs.addTab(s.make_transition_panel(), s.label_icon('⇢'), 'Transiciones')

        # Centro: reproducción y vista previa de efectos.
        mid = QWidget(); ml = QVBoxLayout(mid)
        s.video.setMinimumHeight(360); s.video.setStyleSheet('background:#000;border-radius:6px')
        ml.addWidget(s.video, 1)
        ctl = QHBoxLayout(); s.play = QPushButton('▶  Reproducir todo'); s.play.clicked.connect(s.toggle)
        s.time_label = QLabel('00:00 / 00:00'); s.time_label.setMinimumWidth(100)
        s.seek = QSlider(Qt.Orientation.Horizontal); s.seek.sliderMoved.connect(s.seek_project_slider)
        s.player.positionChanged.connect(s.on_pos); s.player.durationChanged.connect(lambda d: s.seek.setRange(0, d))
        s.player.mediaStatusChanged.connect(s.on_media_status)
        first = QPushButton('|◀'); first.setToolTip('Ir al inicio'); first.clicked.connect(lambda: s.seek_project(0))
        back = QPushButton('−5 s'); back.clicked.connect(lambda: s.seek_project(s.timeline.current_time - 5))
        forward = QPushButton('+5 s'); forward.clicked.connect(lambda: s.seek_project(s.timeline.current_time + 5))
        last = QPushButton('▶|'); last.setToolTip('Ir al final'); last.clicked.connect(lambda: s.seek_project(s.project_total()))
        fullscreen = QPushButton('Pantalla completa'); fullscreen.clicked.connect(s.video.showFullScreen)
        s.preview_effects = QPushButton('Vista previa con efectos'); s.preview_effects.clicked.connect(s.render_preview)
        ctl.addWidget(first); ctl.addWidget(back); ctl.addWidget(s.play); ctl.addWidget(forward); ctl.addWidget(last)
        ctl.addWidget(s.seek, 1); ctl.addWidget(s.time_label); ctl.addWidget(fullscreen); ctl.addWidget(s.preview_effects)
        ml.addLayout(ctl)
        cleanbar = QHBoxLayout()
        s.clean_button = QPushButton('✦  Limpiar voz'); s.clean_button.setObjectName('primary')
        s.clean_button.setMinimumHeight(42); s.clean_button.clicked.connect(lambda: s.clean_voice())
        s.clean_scope = QComboBox(); s.clean_scope.addItem('Todos los clips', 'all'); s.clean_scope.addItem('Solo el seleccionado', 'selected')
        s.clean_level = QComboBox()
        for label, value in (('Suave · 8 dB', 8), ('Normal · 15 dB', 15), ('Fuerte · 25 dB', 25)):
            s.clean_level.addItem(label, value)
        s.clean_level.setCurrentIndex(1)
        s.preview_mode = QComboBox(); s.preview_mode.addItem('Escuchar: Después', 'after'); s.preview_mode.addItem('Escuchar: Antes', 'before')
        s.preview_mode.currentIndexChanged.connect(lambda _i: s.render_timeline_preview() if s.tl.count() else None)
        cleanbar.addWidget(s.clean_button); cleanbar.addWidget(s.clean_scope); cleanbar.addWidget(s.clean_level)
        cleanbar.addStretch(1); cleanbar.addWidget(QLabel('Vista de audio')); cleanbar.addWidget(s.preview_mode)
        ml.addLayout(cleanbar)

        # Inspector derecho: solo muestra ajustes ya soportados por el motor.
        right_tabs = QTabWidget(); s.sp = {}
        video_page = QWidget(); video_l = QVBoxLayout(video_page); g = QGroupBox('Ajustes de imagen'); f = QFormLayout(g)
        for k, lab, lo, hi, st, dec in FIELDS:
            if k == 'speed':
                continue
            spn = QDoubleSpinBox(); spn.setRange(lo, hi); spn.setSingleStep(st); spn.setDecimals(dec)
            spn.valueChanged.connect(lambda v, k=k: s.setf(k, v)); s.sp[k] = spn; f.addRow(lab, spn)
        for key, label, low, high, step, decimals in (
            ('scale', 'Escala (%)', 10, 200, 5, 0), ('pos_x', 'Posición X (%)', 0, 100, 2, 0),
            ('pos_y', 'Posición Y (%)', 0, 100, 2, 0), ('rotation', 'Rotación (°)', -180, 180, 5, 0),
            ('crop_left', 'Recorte izquierda (%)', 0, 45, 1, 0), ('crop_right', 'Recorte derecha (%)', 0, 45, 1, 0),
            ('crop_top', 'Recorte arriba (%)', 0, 45, 1, 0), ('crop_bottom', 'Recorte abajo (%)', 0, 45, 1, 0),
            ('opacity', 'Opacidad (%)', 0, 100, 5, 0)):
            spin = QDoubleSpinBox(); spin.setRange(low, high); spin.setSingleStep(step); spin.setDecimals(decimals)
            spin.valueChanged.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = spin; f.addRow(label, spin)
        for key, label in (('flip_h', 'Voltear horizontalmente'), ('flip_v', 'Voltear verticalmente'), ('reverse', 'Revertir reproducción')):
            check = QCheckBox(label); check.toggled.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = check; f.addRow(check)
        revert = QPushButton('Restablecer ajustes del clip'); revert.clicked.connect(s.revert_clip); f.addRow(revert)
        effects_box = QGroupBox('Efectos de imagen'); effects_form = QFormLayout(effects_box)
        for key, label, low, high in (('vignette', 'Viñeta', 0, 100), ('grain', 'Grano', 0, 100),
                                      ('sharpness', 'Nitidez', 0, 5), ('pixelate', 'Pixelar', 0, 40)):
            control = QSpinBox(); control.setRange(low, high)
            control.valueChanged.connect(lambda value, k='effect_' + key: s.setf(k, value))
            s.sp['effect_' + key] = control; effects_form.addRow(label, control)
        for key, label in (('mirror', 'Espejo'), ('glitch', 'Glitch de color'),
                           ('black_white', 'Blanco y negro'), ('sepia', 'Sepia')):
            control = QCheckBox(label); control.toggled.connect(lambda value, k='effect_' + key: s.setf(k, value))
            s.sp['effect_' + key] = control; effects_form.addRow(control)
        chroma = QCheckBox('Quitar fondo verde'); chroma.toggled.connect(lambda value: s.setf('chroma_enabled', value))
        color_button = QPushButton('#00ff00'); color_button.setStyleSheet('background:#00ff00;color:#111')
        color_button.clicked.connect(lambda: (pick(color_button), s.setf('chroma_color', color_button.text())))
        tolerance = QDoubleSpinBox(); tolerance.setRange(0.01, 1); tolerance.setSingleStep(0.05); tolerance.setValue(0.25)
        tolerance.valueChanged.connect(lambda value: s.setf('chroma_similarity', value))
        lut_button = QPushButton('Importar LUT (.cube)'); lut_button.clicked.connect(s.choose_lut)
        bg_button = QPushButton('Quitar fondo de persona · IA local'); bg_button.clicked.connect(s.remove_background)
        stabilize_button = QPushButton('Estabilizar video')
        stabilize_button.clicked.connect(s.stabilize_clip)
        stabilize_button.setVisible(s.has_vidstab)
        effects_form.addRow(chroma); effects_form.addRow('Color a quitar', color_button); effects_form.addRow('Tolerancia', tolerance)
        effects_form.addRow(lut_button); effects_form.addRow(bg_button)
        if s.has_vidstab:
            effects_form.addRow(stabilize_button)
        video_l.addWidget(g); video_l.addStretch()
        video_l.addWidget(effects_box)
        video_scroll = QScrollArea(); video_scroll.setWidgetResizable(True); video_scroll.setWidget(video_page)
        right_tabs.addTab(video_scroll, 'Video')
        audio_page = QWidget(); audio_l = QVBoxLayout(audio_page)
        noise = QGroupBox('Reducción de ruido de fondo'); nf = QFormLayout(noise)
        nf.addRow(QLabel('Estándar elimina ruido constante. IA usa un modelo RNNoise gratuito.'))
        nz = QSpinBox(); nz.setRange(0, 40); nz.setSuffix(' dB'); nz.valueChanged.connect(lambda v: s.setf('denoise', v)); s.sp['denoise'] = nz
        nf.addRow('Intensidad (0 = desactivada)', nz)
        mode = QComboBox(); mode.addItem('Estándar (afftdn)', 'standard'); mode.addItem('IA (RNNoise)', 'ai')
        mode.currentIndexChanged.connect(lambda _i: s.setf('denoise_mode', mode.currentData()))
        s.sp['denoise_mode'] = mode; nf.addRow('Método', mode)
        nf.addRow(QLabel('Para voz, prueba 12–20 dB. Valores altos pueden sonar metálicos.'))
        download = QPushButton('Descargar modelo IA (solo la primera vez)'); download.clicked.connect(s.download_model)
        nf.addRow(download)
        audio_form = QFormLayout()
        for key, label, low, high, step, decimals in (
            ('volume', 'Volumen del clip (%)', 0, 200, 5, 0),
            ('audio_fade_in', 'Fundido de entrada (s)', 0, 30, 0.1, 1),
            ('audio_fade_out', 'Fundido de salida (s)', 0, 30, 0.1, 1),
            ('pitch', 'Tono (semitonos)', -12, 12, 1, 0)):
            control = QDoubleSpinBox(); control.setRange(low, high); control.setSingleStep(step); control.setDecimals(decimals)
            control.valueChanged.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = control; audio_form.addRow(label, control)
        for key, label in (('normalize', 'Normalizar volumen'), ('voice_enhance', 'Mejorar voz (EQ + compresor)')):
            control = QCheckBox(label); control.toggled.connect(lambda v, k=key: s.setf(k, v)); s.sp[key] = control; audio_form.addRow(control)
        audio_l.addWidget(noise); audio_l.addLayout(audio_form); audio_l.addStretch(); right_tabs.addTab(audio_page, 'Audio')
        speed_page = QWidget(); speed_l = QVBoxLayout(speed_page); sf = QFormLayout()
        speed = QDoubleSpinBox(); speed.setRange(0.25, 4); speed.setSingleStep(0.05); speed.setDecimals(2); speed.setSuffix('×')
        speed.valueChanged.connect(lambda v: s.setf('speed', v)); s.sp['speed'] = speed
        sf.addRow('Velocidad del clip', speed); speed_l.addLayout(sf); speed_l.addStretch(); right_tabs.addTab(speed_page, 'Velocidad')

        body = QSplitter(Qt.Orientation.Horizontal); body.addWidget(left_tabs); body.addWidget(mid); body.addWidget(right_tabs)
        body.setSizes([380, 700, 340]); body.setStretchFactor(1, 1)
        # Barra superior con acciones frecuentes.
        header = QWidget(); header_l = QHBoxLayout(header); header_l.setContentsMargins(8, 4, 8, 4)
        brand = QLabel('  NoiseCut Studio'); brand.setStyleSheet('font-size:18px;font-weight:700;color:#f8fafc')
        icon_small = QIcon(icon) if os.path.exists(icon) else QIcon()
        icon_label = QLabel(); icon_label.setPixmap(icon_small.pixmap(32, 32))
        s.project_title = QLabel('Proyecto sin guardar'); s.project_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        undo = QPushButton('↶  Deshacer'); undo.clicked.connect(s.undo)
        redo = QPushButton('↷  Rehacer'); redo.clicked.connect(s.redo)
        save = QPushButton('Guardar'); save.clicked.connect(s.save_project)
        open_ = QPushButton('Abrir'); open_.clicked.connect(s.open_project)
        export = QPushButton('Exportar'); export.setObjectName('primary'); export.clicked.connect(s.export)
        header_l.addWidget(icon_label); header_l.addWidget(brand); header_l.addStretch(1); header_l.addWidget(s.project_title, 2); header_l.addStretch(1)
        for button in (undo, redo, save, open_, export):
            header_l.addWidget(button)

        timeline_box = QWidget(); timeline_l = QVBoxLayout(timeline_box); timeline_l.setContentsMargins(0, 2, 0, 0)
        timeline_bar = QHBoxLayout(); timeline_bar.addWidget(QLabel('LÍNEA DE TIEMPO'))
        for label, fn in (('− Zoom', lambda: s.zoom_timeline(0.8)), ('+ Zoom', lambda: s.zoom_timeline(1.25)),
                          ('Ajustar a ventana', lambda: s.timeline.fit_project()),
                          ('✂ Dividir', s.split), ('⧉ Duplicar', s.dup), ('Congelar fotograma', s.freeze_frame),
                          ('Separar audio', s.separate_audio), ('＋ Superpuesto', s.add_overlay), ('Eliminar', s.rm)):
            button = QPushButton(label); button.clicked.connect(fn); timeline_bar.addWidget(button)
        timeline_bar.addStretch(); timeline_l.addLayout(timeline_bar)
        s.timeline = TimelineView(); s.timeline.owner = s; timeline_l.addWidget(s.timeline)

        root = QWidget(); root_l = QVBoxLayout(root); root_l.setContentsMargins(10, 8, 10, 8)
        root_l.addWidget(header); root_l.addWidget(body, 1); root_l.addWidget(timeline_box)
        s.setCentralWidget(root)
        menu = s.menuBar().addMenu('Proyecto')
        for label, fn, shortcut in (('Nuevo proyecto', s.new_project, 'Ctrl+N'),
                                    ('Abrir proyecto…', s.open_project, 'Ctrl+O'),
                                    ('Guardar proyecto', s.save_project, 'Ctrl+S'),
                                    ('Guardar proyecto como…', s.save_project_as, 'Ctrl+Shift+S')):
            act = QAction(label, s); act.setShortcut(shortcut); act.triggered.connect(fn); menu.addAction(act)
        edit = s.menuBar().addMenu('Editar')
        for label, fn, shortcut in (('Deshacer', s.undo, 'Ctrl+Z'), ('Rehacer', s.redo, 'Ctrl+Y')):
            act = QAction(label, s); act.setShortcut(shortcut); act.triggered.connect(fn); edit.addAction(act)
        s.remember()
        if not ffbin('ffmpeg') or not ffbin('ffprobe'):
            QMessageBox.warning(s, 'Falta FFmpeg', 'No se encuentran FFmpeg y FFprobe. Instala FFmpeg (Windows: winget install ffmpeg) y reinicia la app.')

    def label_icon(s, text):
        pix = QPixmap(24, 24); pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor('#7dd3fc')); font = QFont(); font.setBold(True); font.setPointSize(12)
        painter.setFont(font); painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, text); painter.end()
        return QIcon(pix)

    def track(s, items, spec, label, fmt, filt=None):
        w = QWidget(); l = QVBoxLayout(w); lst = QListWidget(); l.addWidget(lst); row = QHBoxLayout()

        def redraw():
            lst.clear(); lst.addItems([fmt(x) for x in items])

        def new(path=None):
            base = {}
            if filt:
                path = path or QFileDialog.getOpenFileName(s, label, '', filt)[0]
                if not path:
                    return
                base['path'] = path
            r = ask(s, label, spec)
            if r is not None:
                if label == 'Audio' and base.get('path'):
                    try:
                        base['duration'] = probe(base['path'])[0] if ffbin('ffprobe') else 0
                    except (OSError, ValueError, json.JSONDecodeError):
                        base['duration'] = 0
                s.remember(); r.update(base); items.append(r); redraw(); s.timeline.refresh()
                if r.get('denoise_mode') == 'ai':
                    s.download_model()

        def edit(it):
            i = lst.row(it); r = ask(s, label, spec, items[i])
            if r is not None:
                s.remember(); items[i].update(r); redraw()

        def delete():
            if lst.currentRow() >= 0:
                s.remember(); items.pop(lst.currentRow()); redraw()
        add, rmb = QPushButton('＋ Añadir'), QPushButton('Eliminar')
        add.clicked.connect(lambda: new()); rmb.clicked.connect(delete); lst.itemDoubleClicked.connect(edit)
        row.addWidget(add); row.addWidget(rmb); l.addLayout(row)
        s.track_refresh.append(redraw)
        w.add_path = new
        if filt and label == 'Audio':
            s.add_music = new
        return w

    def import_media(s):
        ps, _ = QFileDialog.getOpenFileNames(s, 'Importar', '', 'Medios (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.png *.jpg *.jpeg *.bmp *.webp *.mp3 *.wav *.m4a *.aac *.flac *.ogg)')
        for p in ps:
            ext = os.path.splitext(p)[1].lower()
            duration = ''
            if ext in VID and ffbin('ffprobe'):
                try:
                    duration = f'\n{probe(p)[0]:.1f} s'
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            it = QListWidgetItem(s.media_icon(p), os.path.basename(p) + duration)
            it.setData(Qt.ItemDataRole.UserRole, p); it.setToolTip(p); s.bin.addItem(it)

    def media_icon(s, path):
        ext = os.path.splitext(path)[1].lower()
        cache = os.path.join(tempfile.gettempdir(), 'noisecutstudio_thumbnails')
        os.makedirs(cache, exist_ok=True)
        thumb = os.path.join(cache, f'{abs(hash(os.path.abspath(path)))}.png')
        if not os.path.exists(thumb):
            if ext in IMG:
                pix = QPixmap(path)
                if not pix.isNull():
                    pix.scaled(224, 144, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(thumb)
            elif ext in VID and ffbin('ffmpeg'):
                try:
                    subprocess.run([ffbin('ffmpeg'), '-y', '-ss', '1', '-i', path, '-frames:v', '1',
                                    '-vf', 'scale=224:144:force_original_aspect_ratio=decrease', thumb],
                                   capture_output=True, creationflags=NOWIN, timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        pix = QPixmap(thumb) if os.path.exists(thumb) else QPixmap()
        if pix.isNull():
            return s.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        return QIcon(pix)

    def add_selected_media(s):
        it = s.bin.currentItem()
        if it:
            s.add_to_timeline(it)
        else:
            QMessageBox.information(s, 'Añadir medios', 'Selecciona un archivo de la biblioteca.')

    def media_hover(s, item, rect):
        s.hovered_media = item
        if item:
            s.media_plus.move(rect.right() - 32, rect.top() + 4)
            s.media_plus.show(); s.media_plus.raise_()
        else:
            s.media_plus.hide()

    def make_filter_panel(s):
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(QLabel('Presets creados con los ajustes disponibles del editor.'))
        for name, values in (('Vívido', (0.04, 1.18, 1.45)), ('Cálido', (0.06, 1.08, 1.12)),
                             ('Frío', (-0.02, 1.12, 0.88)), ('Blanco y negro', (0, 1.05, 0)),
                             ('Cine', (-0.04, 1.22, 0.82))):
            button = QPushButton(name); button.setMinimumHeight(44)
            button.clicked.connect(lambda _=False, n=name, v=values: s.apply_preset(n, v)); layout.addWidget(button)
        layout.addStretch(); return w

    def make_ai_panel(s):
        page = QWidget(); layout = QVBoxLayout(page)
        layout.addWidget(QLabel('Herramientas locales: los modelos se descargan cuando los usas.'))
        s.ai_cards = {}
        cards = (
            ('whisper', '▤  Subtítulos automáticos', 'Reconoce voz sin enviar el audio a internet.',
             lambda: s.auto_subtitles(), 'faster_whisper', 'Falta instalar el complemento IA.'),
            ('background', '◉  Quitar fondo', 'Segmenta a la persona cuadro a cuadro; puede tardar.',
             lambda: s.remove_background(), 'rembg', 'Falta instalar el complemento IA.'),
            ('rnnoise', '♫  Limpiar voz con IA (RNNoise)', 'Reduce ruido con un modelo local gratuito.',
             lambda: s.clean_voice(ai=True), None, 'El modelo se descarga la primera vez.'),
        )
        for key, title, description, action, dependency, absent in cards:
            card = QGroupBox(title); card_l = QVBoxLayout(card)
            card_l.addWidget(QLabel(description))
            status = QLabel(); status.setStyleSheet('color:#9ca3af')
            if key == 'rnnoise':
                ready = os.path.exists(s.ai_model_path)
                status.setText('Modelo listo' if ready else absent)
            elif importlib.util.find_spec(dependency):
                status.setText('Complemento instalado; modelo local bajo demanda.')
            else:
                status.setText('No disponible: instala requirements-ai.txt y reinicia la app.')
            progress = QProgressBar(); progress.setRange(0, 100); progress.setValue(0)
            button = QPushButton('Abrir herramienta'); button.clicked.connect(action)
            cancel = QPushButton('Cancelar'); cancel.setEnabled(False)
            cancel.clicked.connect(lambda _=False, k=key: s.cancel_ai_worker(k))
            card_l.addWidget(status); card_l.addWidget(progress); card_l.addWidget(button); card_l.addWidget(cancel)
            layout.addWidget(card); s.ai_cards[key] = {'status': status, 'progress': progress, 'cancel': cancel}
        layout.addStretch(1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
        return scroll

    def cancel_ai_worker(s, key):
        worker_name = {'whisper': 'speech_worker', 'background': 'bg_worker', 'rnnoise': 'model_worker'}[key]
        worker = getattr(s, worker_name, None)
        if worker and worker.isRunning():
            worker.cancel()
        progress = getattr(s, {'whisper': 'speech_progress', 'background': 'bg_progress',
                              'rnnoise': 'model_progress'}[key], None)
        if progress:
            progress.cancel()

    def clean_voice(s, ai=False):
        selected_only = s.clean_scope.currentData() == 'selected'
        if selected_only:
            current = s.tl.currentItem()
            if not current:
                return QMessageBox.information(s, 'Limpiar voz', 'Selecciona primero un clip con audio.')
            clips = [current.data(Qt.ItemDataRole.UserRole)]
        else:
            clips = [s.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(s.tl.count())]
        targets = [c for c in clips if c.kind == 'video' and c.audio]
        music_targets = [] if selected_only else s.music
        if not targets and not music_targets:
            return QMessageBox.information(s, 'Limpiar voz', 'No hay clips con audio en la selección.')
        level = int(s.clean_level.currentData())
        use_ai = ai and os.path.exists(s.ai_model_path)
        s.remember()
        for clip in targets:
            clip.denoise = level
            clip.denoise_mode = 'ai' if use_ai else 'standard'
            clip.voice_enhance = True
            clip.normalize = True
        for track in music_targets:
            track.update(denoise=level, denoise_mode='ai' if use_ai else 'standard',
                         voice_enhance=True, normalize=True)
        if music_targets and s.track_refresh:
            s.track_refresh[0]()
        s.renumber()
        if s.tl.currentItem():
            s.select(s.tl.currentItem())
        if ai and not use_ai:
            card = s.ai_cards.get('rnnoise')
            if card:
                card['status'].setText('Descargando modelo… mientras tanto se aplica el método estándar.')
            s.download_model()
        elif ai:
            card = s.ai_cards.get('rnnoise')
            if card:
                card['status'].setText('Modelo listo; limpieza IA aplicada.')
        s.statusBar().showMessage(
            f"Limpieza {('IA' if use_ai else 'estándar')} aplicada a "
            f"{len(targets)} clip(s) y {len(music_targets)} pista(s) de audio · {level} dB.", 6000)

    def apply_preset(s, name, values):
        if not s.cur:
            return QMessageBox.information(s, 'Filtros', 'Selecciona primero un clip de video.')
        s.remember(); s.cur.bright, s.cur.contrast, s.cur.sat = values
        for key in ('bright', 'contrast', 'sat'):
            widget = s.sp[key]; widget.blockSignals(True); widget.setValue(getattr(s.cur, key)); widget.blockSignals(False)
        s.statusBar().showMessage(f'Filtro aplicado: {name}', 3000); s.renumber()

    def make_transition_panel(s):
        w = QWidget(); layout = QVBoxLayout(w)
        layout.addWidget(QLabel('Elige la transición de entrada del clip seleccionado.'))
        s.transition_choice = QComboBox()
        for label, value in (('Sin transición', 'cut'), ('Fundido', 'fade'), ('Disolver', 'dissolve'),
                             ('Cortinilla izquierda', 'wipeleft'), ('Cortinilla derecha', 'wiperight'),
                             ('Cortinilla arriba', 'wipeup'), ('Cortinilla abajo', 'wipedown'),
                             ('Deslizar izquierda', 'slideleft'), ('Deslizar derecha', 'slideright'),
                             ('Deslizar arriba', 'slideup'), ('Deslizar abajo', 'slidedown'),
                             ('Círculo abre', 'circleopen'), ('Círculo cierra', 'circleclose'),
                             ('Zoom', 'zoomin'), ('Radial', 'radial'), ('Suave izquierda', 'smoothleft'),
                             ('Suave derecha', 'smoothright'), ('Pixelar', 'pixelize')):
            s.transition_choice.addItem(label, value)
        layout.addWidget(s.transition_choice)
        s.transition_duration = QDoubleSpinBox(); s.transition_duration.setRange(0.1, 5); s.transition_duration.setValue(0.5); s.transition_duration.setSuffix(' s')
        layout.addWidget(QLabel('Duración')); layout.addWidget(s.transition_duration)
        apply_transition = QPushButton('Aplicar al clip seleccionado'); apply_transition.clicked.connect(s.set_transition)
        layout.addWidget(apply_transition)
        layout.addStretch(); return w

    def set_transition(s):
        if not s.cur:
            return QMessageBox.information(s, 'Transiciones', 'Selecciona primero un clip.')
        s.remember(); s.cur.transition = s.transition_choice.currentData()
        s.cur.transition_duration = s.transition_duration.value()
        s.renumber(); s.statusBar().showMessage('Transición aplicada al clip seleccionado.', 3000)

    def choose_lut(s):
        if not s.cur:
            return QMessageBox.information(s, 'LUT', 'Selecciona primero un clip de video.')
        path, _ = QFileDialog.getOpenFileName(s, 'Importar LUT', '', 'LUT 3D (*.cube)')
        if path:
            s.setf('lut_path', path)

    def auto_subtitles(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Subtítulos automáticos', 'Selecciona primero un clip de video con voz.')
        options = [('Idioma', 'choice', 'auto', [('Automático', 'auto'), ('Español', 'es'),
                   ('Inglés', 'en'), ('Francés', 'fr'), ('Alemán', 'de'), ('Portugués', 'pt')]),
                   ('Modelo', 'choice', 'base', [('Tiny · rápido', 'tiny'), ('Base · equilibrado', 'base'),
                   ('Small · más preciso', 'small')])]
        values = ask(s, 'Subtítulos automáticos', [(k, k, typ, default, choices) for k, typ, default, choices in options])
        if not values:
            return
        clip = it.data(Qt.ItemDataRole.UserRole)
        s.speech_progress = QProgressDialog('Whisper descargará el modelo y analizará el audio en segundo plano…', 'Cancelar', 0, 100, s)
        s.speech_progress.setWindowTitle('Subtítulos automáticos'); s.speech_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.speech_progress.setMinimumDuration(0); s.speech_progress.show()
        s.speech_worker = WhisperWorker(clip.path, values['Modelo'], values['Idioma'], s.whisper_model_dir)
        card = s.ai_cards['whisper']; card['cancel'].setEnabled(True); card['progress'].setValue(0)
        card['status'].setText('Descargando/preparando modelo y analizando…')
        s.speech_worker.progress.connect(lambda pct, msg: (s.speech_progress.setLabelText(msg), s.speech_progress.setValue(pct),
            card['progress'].setValue(pct), card['status'].setText(msg)))
        s.speech_progress.canceled.connect(s.speech_worker.cancel)
        s.speech_worker.completed.connect(s.subtitles_finished); s.speech_worker.start()

    def subtitles_finished(s, segments, error):
        s.speech_progress.close()
        s.ai_cards['whisper']['cancel'].setEnabled(False)
        if error:
            if 'cancel' in error.lower():
                s.ai_cards['whisper']['status'].setText('Generación cancelada.')
                s.statusBar().showMessage('Generación de subtítulos cancelada.', 5000)
            else:
                s.ai_cards['whisper']['status'].setText('No se pudo completar; revisa el complemento/conexión.')
                QMessageBox.critical(s, 'Subtítulos automáticos', 'No se pudo completar Whisper. Instala el complemento opcional requirements-ai.txt y revisa tu conexión para descargar el modelo.\n\n' + error)
            return
        records = subtitle_records(segments or [])
        if records:
            s.remember(); s.texts.extend(records); s.track_refresh[1](); s.timeline.refresh()
            s.ai_cards['whisper']['progress'].setValue(100)
            s.ai_cards['whisper']['status'].setText(f'{len(records)} segmentos listos para editar.')
            s.statusBar().showMessage(f'{len(records)} subtítulos añadidos a la pista Texto; puedes editarlos con doble clic.', 8000)
        else:
            QMessageBox.information(s, 'Subtítulos automáticos', 'No se reconoció voz en este clip.')

    def remove_background(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Quitar fondo', 'Selecciona primero un clip de video.')
        if not ffbin('ffmpeg'):
            return QMessageBox.information(s, 'Quitar fondo', 'Para procesar el video, instala FFmpeg; el modelo de IA se descarga al utilizarlo.')
        clip = it.data(Qt.ItemDataRole.UserRole)
        suggested = os.path.splitext(clip.path)[0] + '_sin_fondo.mov'
        output, _ = QFileDialog.getSaveFileName(s, 'Guardar video con fondo transparente', suggested, 'Video QuickTime con transparencia (*.mov)')
        if not output:
            return
        if not output.lower().endswith('.mov'):
            output += '.mov'
        QMessageBox.information(s, 'Quitar fondo', 'El procesamiento cuadro a cuadro puede tardar. El primer uso descarga el modelo ligero U²-NetP. Puedes cancelar durante el análisis.')
        fps = 30
        try:
            result = subprocess.run([ffbin('ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=avg_frame_rate', '-of', 'default=nw=1:nk=1', clip.path],
                capture_output=True, text=True, creationflags=NOWIN, timeout=8)
            numerator, denominator = result.stdout.strip().split('/')
            fps = round(float(numerator) / float(denominator))
        except (OSError, ValueError, subprocess.TimeoutExpired, ZeroDivisionError):
            pass
        s.bg_progress = QProgressDialog('Preparando la eliminación del fondo…', 'Cancelar', 0, 100, s)
        s.bg_progress.setWindowTitle('Quitar fondo'); s.bg_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.bg_progress.setMinimumDuration(0); s.bg_progress.show()
        s.bg_worker = BackgroundWorker(clip.path, output, fps)
        card = s.ai_cards['background']; card['cancel'].setEnabled(True); card['progress'].setValue(0)
        card['status'].setText('Preparando modelo y procesando…')
        s.bg_worker.progress.connect(lambda pct, msg: (s.bg_progress.setLabelText(msg), s.bg_progress.setValue(pct),
            card['progress'].setValue(pct), card['status'].setText(msg)))
        s.bg_progress.canceled.connect(s.bg_worker.cancel)
        s.bg_worker.completed.connect(s.background_finished); s.bg_worker.start()

    def stabilize_clip(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Estabilizar video', 'Selecciona primero un clip de video.')
        if not s.has_vidstab:
            return QMessageBox.information(s, 'Estabilizar video', 'Esta versión de FFmpeg no incluye vidstab; la opción se oculta automáticamente.')
        clip = it.data(Qt.ItemDataRole.UserRole)
        output, _ = QFileDialog.getSaveFileName(s, 'Guardar video estabilizado',
            os.path.splitext(clip.path)[0] + '_estabilizado.mp4', 'Video MP4 (*.mp4)')
        if not output:
            return
        QMessageBox.information(s, 'Estabilizar video', 'La estabilización analiza el video y luego lo corrige; tardará un tiempo.')
        s.stab_progress = QProgressDialog('Analizando el movimiento…', None, 0, 100, s)
        s.stab_progress.setWindowTitle('Estabilización'); s.stab_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.stab_progress.setMinimumDuration(0); s.stab_progress.show()
        s.stab_worker = StabilizeWorker(clip.path, output)
        s.stab_worker.progress.connect(lambda pct, msg: (s.stab_progress.setLabelText(msg), s.stab_progress.setValue(pct)))
        s.stab_worker.completed.connect(s.stabilization_finished); s.stab_worker.start()

    def stabilization_finished(s, path, error):
        s.stab_progress.close()
        if error:
            QMessageBox.critical(s, 'Estabilizar video', 'No se pudo estabilizar el video.\n\n' + error)
            return
        it = s.tl.currentItem()
        if it:
            s.remember(); clip = it.data(Qt.ItemDataRole.UserRole); clip.path = path
            s.player.setSource(QUrl.fromLocalFile(path)); s.renumber()
            s.statusBar().showMessage('Video estabilizado; el archivo original se conservó.', 8000)

    def background_finished(s, path, error):
        s.bg_progress.close()
        s.ai_cards['background']['cancel'].setEnabled(False)
        if error:
            if 'cancel' in error.lower():
                s.ai_cards['background']['status'].setText('Procesamiento cancelado.')
                s.statusBar().showMessage('Eliminación del fondo cancelada.', 5000)
            else:
                s.ai_cards['background']['status'].setText('No se pudo procesar; revisa el complemento/FFmpeg.')
                QMessageBox.critical(s, 'Quitar fondo', 'No se pudo procesar el video. Instala el complemento opcional requirements-ai.txt y revisa FFmpeg.\n\n' + error)
            return
        it = s.tl.currentItem()
        if it:
            s.remember(); clip = it.data(Qt.ItemDataRole.UserRole); clip.path = path
            s.tl.setCurrentItem(it); s.renumber()
            s.ai_cards['background']['progress'].setValue(100)
            s.ai_cards['background']['status'].setText('Video con fondo transparente listo.')
            s.statusBar().showMessage('Video con fondo transparente creado; el archivo de origen se conservó.', 8000)

    def zoom_timeline(s, factor):
        s.timeline.scale = max(24, min(240, s.timeline.scale * factor)); s.timeline.refresh()

    def set_export_setting(s, key, value):
        if s.export_settings.get(key) != value:
            s.remember(); s.export_settings[key] = value; s.timeline.refresh()

    def revert_clip(s):
        it = s.tl.currentItem()
        if not it:
            return QMessageBox.information(s, 'Restablecer clip', 'Selecciona primero un clip.')
        c = it.data(Qt.ItemDataRole.UserRole)
        s.remember()
        clean = Clip(c.path, c.kind, c.src, c.audio, 0, c.src)
        it.setData(Qt.ItemDataRole.UserRole, clean); s.tl.setCurrentItem(it); s.renumber()

    def freeze_frame(s):
        it = s.tl.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole).kind != 'video':
            return QMessageBox.information(s, 'Congelar fotograma', 'Selecciona un clip de video.')
        c = it.data(Qt.ItemDataRole.UserRole)
        if not ffbin('ffmpeg'):
            return QMessageBox.critical(s, 'Falta FFmpeg', 'No se puede extraer el fotograma porque falta FFmpeg.')
        source = s.player.source().toLocalFile()
        pos = s.player.position() / 1000
        t = pos if source == c.path else c.start + pos * c.speed
        t = max(c.start, min(c.end - 0.01, t))
        seconds, ok = QInputDialog.getDouble(s, 'Congelar fotograma', 'Duración de la imagen fija (segundos):', 2, 0.1, 60, 1)
        if not ok:
            return
        folder = tempfile.mkdtemp(prefix='noisecut_freeze_'); image = os.path.join(folder, 'fotograma.png')
        try:
            result = subprocess.run([ffbin('ffmpeg'), '-y', '-ss', str(t), '-i', c.path,
                                     '-frames:v', '1', image], capture_output=True, text=True,
                                    creationflags=NOWIN, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return QMessageBox.critical(s, 'Fotograma', f'No se pudo extraer el fotograma.\n{e}')
        if result.returncode != 0 or not os.path.exists(image):
            return QMessageBox.critical(s, 'Fotograma', 'FFmpeg no pudo extraer el fotograma seleccionado.')
        s.push(Clip(image, 'image', seconds, False, 0, seconds), s.tl.row(it) + 1)

    def separate_audio(s):
        it = s.tl.currentItem()
        if not it:
            return QMessageBox.information(s, 'Separar audio', 'Selecciona un clip de video.')
        c = it.data(Qt.ItemDataRole.UserRole)
        if c.kind != 'video' or not c.audio:
            return QMessageBox.information(s, 'Separar audio', 'El clip seleccionado no contiene audio.')
        offset = sum(s.tl.item(i).data(Qt.ItemDataRole.UserRole).out for i in range(s.tl.row(it)))
        s.remember()
        s.music.append({'path': c.path, 'offset': offset, 'vol': c.volume / 100,
                        'denoise': c.denoise, 'duration': c.out, 'source_start': c.start,
                        'source_end': c.end, 'speed': c.speed})
        s.track_refresh[0](); s.timeline.refresh()

    def add_overlay(s):
        path, _ = QFileDialog.getOpenFileName(s, 'Añadir video superpuesto', '',
            'Video e imagen (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.png *.jpg *.jpeg *.webp)')
        if not path:
            return
        duration = 5.0
        if os.path.splitext(path)[1].lower() in VID:
            if not ffbin('ffprobe'):
                return QMessageBox.critical(s, 'Falta FFmpeg', 'Se necesita FFprobe para añadir el video superpuesto.')
            try:
                duration = probe(path)[0]
            except (OSError, ValueError, json.JSONDecodeError):
                return QMessageBox.critical(s, 'Video superpuesto', 'No se pudo leer la duración del archivo.')
        maximum = max(1.0, sum(s.tl.item(i).data(Qt.ItemDataRole.UserRole).out for i in range(s.tl.count())))
        spec = [('start', 'Inicio en el proyecto (s)', 'float', 0, 0, 3600),
                ('end', 'Fin en el proyecto (s)', 'float', min(maximum, duration), 0.1, 3600),
                ('x', 'Posición X (%)', 'int', 75, 0, 100), ('y', 'Posición Y (%)', 'int', 15, 0, 100),
                ('scale', 'Tamaño (% del ancho)', 'int', 25, 5, 100)]
        params = ask(s, 'Video superpuesto', spec)
        if params is None:
            return
        s.remember(); params['path'] = path; params['duration'] = duration
        s.overlays.append(params); s.timeline.refresh()

    def add_to_timeline(s, it):
        p = it.data(Qt.UserRole); ext = os.path.splitext(p)[1].lower()
        if ext in AUD:
            return s.add_music(p)
        if ext in IMG:
            c = Clip(p, 'image', 5, False, 0, 5)
        else:
            try:
                dur, has_audio = probe(p)
            except (OSError, ValueError, json.JSONDecodeError):
                return QMessageBox.critical(s, 'No se pudo abrir el medio',
                    'No se pudo leer este archivo. Comprueba que FFprobe esté instalado y que el video sea compatible.')
            if dur <= 0:
                return QMessageBox.critical(s, 'Video no compatible', 'El archivo no tiene una duración válida o está dañado.')
            c = Clip(p, 'video', dur, has_audio, 0, dur)
        s.push(c)

    def push(s, c, row=None):
        s.remember()
        it = QListWidgetItem(); it.setData(Qt.UserRole, c)
        s.tl.insertItem(s.tl.count() if row is None else row, it); s.renumber(); s.tl.setCurrentItem(it)
        s.timeline.fit_project()

    def renumber(s, *_):
        for i in range(s.tl.count()):
            it = s.tl.item(i); c = it.data(Qt.UserRole)
            it.setText(f"{i + 1}. {os.path.basename(c.path)} · {c.out:.1f}s{'  🔇' if c.denoise or c.voice_enhance else ''}")
        if hasattr(s, 'timeline'):
            s.timeline.refresh()

    def select(s, it, _=None):
        s.cur = it.data(Qt.UserRole) if it else None
        if not s.cur:
            return
        for k, w in s.sp.items():
            w.blockSignals(True)
            if k.startswith('effect_'):
                value = s.cur.effects.get(k[7:], False if isinstance(w, QCheckBox) else 0)
                w.setChecked(bool(value)) if isinstance(w, QCheckBox) else w.setValue(value)
            elif isinstance(w, QCheckBox):
                w.setChecked(getattr(s.cur, k))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(getattr(s.cur, k))))
            else:
                w.setValue(getattr(s.cur, k))
            w.blockSignals(False)
        v = s.cur.kind == 'video'
        audio_enabled = v and s.cur.audio
        s.sp['start'].setEnabled(v); s.sp['speed'].setEnabled(v)
        for key in ('denoise', 'denoise_mode', 'volume', 'audio_fade_in', 'audio_fade_out',
                    'normalize', 'voice_enhance', 'pitch'):
            s.sp[key].setEnabled(audio_enabled)
        if v:
            s.player.setSource(QUrl.fromLocalFile(s.cur.path)); s.player.setPosition(int(s.cur.start * 1000)); s.player.pause()
        s.timeline.selected = s.tl.row(it) if it else -1
        s.timeline.set_selected(s.timeline.selected)

    def setf(s, k, v):
        if s.cur:
            if k.startswith('effect_'):
                name = k[7:]
                if s.cur.effects.get(name, False if isinstance(v, bool) else 0) == v:
                    return
                s.remember(); s.cur.effects[name] = v; s.renumber(); return
            if getattr(s.cur, k) == (int(v) if k == 'denoise' else v):
                return
            s.remember()
            setattr(s.cur, k, int(v) if k == 'denoise' else v); s.renumber()
            if k == 'denoise_mode' and v == 'ai':
                s.download_model()

    def needs_ai_model(s):
        return (any(c.denoise > 0 and c.denoise_mode == 'ai'
                    for c in (s.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(s.tl.count()))) or
                any(m.get('denoise', 0) > 0 and m.get('denoise_mode') == 'ai' for m in s.music))

    def export_settings_with_model(s):
        settings = copy.deepcopy(s.export_settings)
        settings['rnnoise_model'] = s.ai_model_path if os.path.exists(s.ai_model_path) else None
        return settings

    def download_model(s):
        if os.path.exists(s.ai_model_path):
            s.statusBar().showMessage('Modelo RNNoise disponible para la reducción IA.', 5000)
            return
        if getattr(s, 'model_worker', None) and s.model_worker.isRunning():
            return
        os.makedirs(s.ai_model_dir, exist_ok=True)
        s.model_progress = QProgressDialog('Descargando el modelo gratuito RNNoise…', 'Cancelar', 0, 100, s)
        s.model_progress.setWindowTitle('Reducción de ruido IA'); s.model_progress.setWindowModality(Qt.WindowModality.WindowModal)
        s.model_progress.setMinimumDuration(0); s.model_progress.show()
        s.model_worker = ModelDownloadWorker(s.ai_model_url, s.ai_model_path)
        s.model_worker.progress.connect(s.model_progress.setValue)
        card = getattr(s, 'ai_cards', {}).get('rnnoise')
        if card:
            card['cancel'].setEnabled(True); card['progress'].setValue(0)
            card['status'].setText('Descargando modelo RNNoise…')
            s.model_worker.progress.connect(card['progress'].setValue)
        s.model_progress.canceled.connect(s.model_worker.cancel)
        s.model_worker.completed.connect(s.model_download_finished)
        s.model_worker.start()

    def model_download_finished(s, success, message):
        s.model_progress.close()
        card = getattr(s, 'ai_cards', {}).get('rnnoise')
        if card:
            card['cancel'].setEnabled(False)
            card['progress'].setValue(100 if success else 0)
            card['status'].setText('Modelo listo; vuelve a pulsar Limpiar voz con IA.' if success
                                   else 'No se descargó; se usa el método estándar.')
        if success:
            s.statusBar().showMessage('Modelo RNNoise descargado y listo.', 7000)
        elif 'cancelada' in message.lower():
            s.statusBar().showMessage('Descarga del modelo cancelada.', 5000)
        else:
            QMessageBox.critical(s, 'No se pudo descargar el modelo',
                'No se pudo descargar RNNoise. Revisa la conexión a internet y vuelve a intentarlo.\n\n' + message)

    def record_voiceover(s):
        state = s.recorder.recorderState()
        if state == QMediaRecorder.RecorderState.RecordingState:
            s.recorder.stop()
            s.record_status.setText('Guardando la grabación…')
            return
        if not QMediaDevices.audioInputs():
            return QMessageBox.information(s, 'Grabar voz', 'No se encontró un micrófono disponible.')
        path, _ = QFileDialog.getSaveFileName(s, 'Guardar voz en off', 'voz-en-off.wav', 'Audio WAV (*.wav)')
        if not path:
            return
        if not path.lower().endswith('.wav'):
            path += '.wav'
        s.recording_path = path
        s.recorder.setMediaFormat(QMediaFormat.FileFormat.Wave)
        s.recorder.setOutputLocation(QUrl.fromLocalFile(path))
        s.recorder.record()

    def on_record_state(s, state):
        recording = state == QMediaRecorder.RecorderState.RecordingState
        s.record_button.setText('■  Detener grabación' if recording else '●  Grabar voz en off')
        if recording:
            s.was_recording = True
            s.record_status.setText('Grabando… Pulsa detener cuando termines.')
        elif s.was_recording:
            s.was_recording = False
            s.record_status.setText('Finalizando archivo de audio…')
            QTimer.singleShot(700, s.finish_recording)

    def finish_recording(s):
        path = s.recording_path
        s.recording_path = None
        if not path or not os.path.isfile(path) or os.path.getsize(path) < 44:
            s.record_status.setText('No se pudo guardar la grabación. Revisa el micrófono e inténtalo de nuevo.')
            return
        try:
            with wave.open(path, 'rb') as wav:
                duration = wav.getnframes() / max(1, wav.getframerate())
        except (OSError, EOFError, wave.Error):
            duration = 0
        s.remember()
        s.music.append({'path': path, 'offset': 0, 'vol': 1.0, 'denoise': 0,
                        'denoise_mode': 'standard', 'duration': duration,
                        'fade_in': 0, 'fade_out': 0, 'normalize': False,
                        'voice_enhance': False, 'pitch': 0})
        for redraw in s.track_refresh:
            redraw()
        s.timeline.refresh()
        s.record_status.setText(f'Grabación añadida a Audio ({duration:.1f} s).')

    def project_clips(s):
        return [s.tl.item(i).data(Qt.ItemDataRole.UserRole) for i in range(s.tl.count())]

    def project_total(s):
        return project_duration(s.project_clips(), s.music, s.overlays, s.texts, s.stickers)

    def seek_project_slider(s, value):
        s.seek_project(value / 1000)

    def seek_project(s, target, playing=None):
        total = s.project_total()
        target = max(0, min(float(target), max(0, total - 0.02)))
        if playing is None:
            playing = s.player.playbackState() == QMediaPlayer.PlayingState
        source = s.player.source().toLocalFile()
        if s.timeline_preview_path and source == s.timeline_preview_path and os.path.isfile(source):
            s.player.setPosition(int(target * 1000))
            if playing: s.player.play()
            else: s.player.pause()
            s.timeline.set_global_time(target)
            return
        elapsed = 0.0
        for index, clip in enumerate(s.project_clips()):
            if target <= elapsed + clip.out or index == s.tl.count() - 1:
                local = max(0, min(clip.out, target - elapsed))
                s.fallback_playback = bool(playing)
                s.tl.setCurrentRow(index)
                s.player.setSource(QUrl.fromLocalFile(clip.path))
                s.player.setPosition(int((clip.start + local * clip.speed) * 1000))
                if playing:
                    QTimer.singleShot(120, s.player.play)
                else:
                    s.player.pause()
                s.timeline.set_global_time(target)
                return
            elapsed += clip.out
        s.timeline.set_global_time(target)

    def toggle(s):
        source = s.player.source().toLocalFile()
        if s.timeline_preview_path and source == s.timeline_preview_path:
            if s.player.playbackState() == QMediaPlayer.PlayingState:
                s.player.pause(); s.play.setText('▶  Reproducir todo')
            else:
                if s.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                    s.player.setPosition(0)
                s.player.play(); s.play.setText('❚❚  Pausar')
            return
        if s.fallback_playback and s.player.playbackState() == QMediaPlayer.PlayingState:
            s.player.pause(); s.play.setText('▶  Reproducir todo')
            return
        s.render_timeline_preview()

    def start_fallback_playback(s):
        clips = s.project_clips()
        if not clips:
            return
        target = s.timeline.current_time
        if target >= s.project_total() - 0.05:
            target = 0
        s.fallback_playback = True
        s.seek_project(target, playing=True)
        s.play.setText('❚❚  Pausar')

    def render_timeline_preview(s):
        if not s.project_clips():
            return QMessageBox.information(s, 'Reproducción', 'Añade un clip de video a la línea de tiempo.')
        state = s.project_data()
        paths = ([c.path for c in s.project_clips()] + [m.get('path', '') for m in s.music] +
                 [o.get('path', '') for o in s.overlays] + [x.get('path', '') for x in s.stickers] +
                 [s.ai_model_path])
        audio_mode = s.preview_mode.currentData()
        key = preview_fingerprint(state, paths, audio_mode)
        cache_file = os.path.join(s.preview_cache_dir, key + '.mp4')
        if os.path.isfile(cache_file) and os.path.getsize(cache_file) > 0:
            s.timeline_preview_path = cache_file; s.timeline_preview_hash = key
            s.fallback_playback = False
            s.player.setSource(QUrl.fromLocalFile(cache_file))
            s.player.setPosition(int(s.timeline.current_time * 1000)); s.player.play()
            s.play.setText('❚❚  Pausar')
            return
        if s.timeline_preview_hash != key:
            s.timeline_preview_path = None
        if not ffbin('ffmpeg'):
            s.start_fallback_playback()
            return QMessageBox.critical(s, 'Falta FFmpeg',
                'No se puede generar la vista previa completa sin FFmpeg. Se reproducirán los clips uno por uno.')
        if s.timeline_preview_worker and s.timeline_preview_worker.isRunning():
            s.pending_preview = True
            s.timeline_preview_worker.cancel()
            return
        s.pending_preview = False
        s.start_fallback_playback()
        clips = copy.deepcopy(s.project_clips())
        music = copy.deepcopy(s.music)
        if audio_mode == 'before':
            for clip in clips:
                clip.denoise = 0; clip.denoise_mode = 'standard'; clip.voice_enhance = False; clip.normalize = False
            for track in music:
                track.update(denoise=0, denoise_mode='standard', voice_enhance=False, normalize=False)
        settings = s.export_settings_with_model()
        settings.update(format='MP4', quality='baja', preview=True)
        width, height = canvas_size(settings)
        preview_size = (640, max(2, round(640 * height / width)))
        s.preview_tmp = tempfile.mkdtemp(prefix='noisecut_full_preview_')
        try:
            command, duration = build(clips, copy.deepcopy(s.texts), copy.deepcopy(s.stickers), music,
                cache_file, s.preview_tmp, size=preview_size, overlays=copy.deepcopy(s.overlays), settings=settings)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            shutil.rmtree(s.preview_tmp, ignore_errors=True)
            return QMessageBox.critical(s, 'Vista previa', f'No se pudo preparar el proyecto completo.\n{exc}')
        s.preview_dialog = QProgressDialog('Generando la vista previa de todo el proyecto…', 'Cancelar', 0, 100, s)
        s.preview_dialog.setWindowTitle('Vista previa del proyecto')
        s.preview_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        s.preview_dialog.setMinimumDuration(0); s.preview_dialog.show()
        s.timeline_preview_worker = Worker(command, duration)
        s.timeline_preview_worker.prog.connect(s.preview_dialog.setValue)
        s.preview_dialog.canceled.connect(s.timeline_preview_worker.cancel)
        s.timeline_preview_worker.done.connect(lambda err: s.timeline_preview_finished(err, cache_file, key))
        s.timeline_preview_worker.start()
        s.statusBar().showMessage('Reproducción provisional: los clips avanzan mientras se prepara la vista con efectos.')

    def timeline_preview_finished(s, err, path, key):
        if getattr(s, 'preview_dialog', None):
            s.preview_dialog.close()
        shutil.rmtree(getattr(s, 'preview_tmp', ''), ignore_errors=True)
        if err == '__CANCELLED__':
            if s.pending_preview:
                s.pending_preview = False
                QTimer.singleShot(150, s.render_timeline_preview)
            else:
                s.statusBar().showMessage('Generación de vista previa cancelada; continúa la reproducción provisional.', 5000)
            return
        if err:
            if os.path.exists(path):
                os.remove(path)
            s.statusBar().showMessage('No se pudo generar la vista completa; puedes continuar con la reproducción provisional.', 8000)
            QMessageBox.critical(s, 'Vista previa', 'FFmpeg no pudo generar la vista previa completa.\n' + err[-1200:])
            return
        current_paths = ([c.path for c in s.project_clips()] + [m.get('path', '') for m in s.music] +
                         [o.get('path', '') for o in s.overlays] + [x.get('path', '') for x in s.stickers] +
                         [s.ai_model_path])
        current_key = preview_fingerprint(s.project_data(), current_paths, s.preview_mode.currentData())
        if current_key != key:
            s.timeline_preview_hash = None
            QTimer.singleShot(100, s.render_timeline_preview)
            return
        s.timeline_preview_path = path; s.timeline_preview_hash = key
        target = s.timeline.current_time
        s.fallback_playback = False
        s.player.setSource(QUrl.fromLocalFile(path)); s.player.setPosition(int(target * 1000)); s.player.play()
        s.play.setText('❚❚  Pausar')
        s.statusBar().showMessage('Vista previa completa lista; se conserva hasta que cambie el proyecto.', 6000)

    def on_media_status(s, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if s.timeline_preview_path and s.player.source().toLocalFile() == s.timeline_preview_path:
                s.timeline.set_global_time(s.project_total()); s.fallback_playback = False
                s.play.setText('▶  Reproducir todo')
            elif s.fallback_playback:
                s.fallback_playback = False; s.play.setText('▶  Reproducir todo')

    def on_pos(s, p):
        source = s.player.source().toLocalFile()
        total = s.project_total()
        if s.timeline_preview_path and source == s.timeline_preview_path:
            current = p / 1000
            s.timeline.set_global_time(current)
        else:
            current = p / 1000
            row = s.tl.row(s.tl.currentItem()) if s.tl.currentItem() else -1
            if row >= 0:
                clip = s.tl.item(row).data(Qt.ItemDataRole.UserRole)
                before = sum(s.tl.item(i).data(Qt.ItemDataRole.UserRole).out for i in range(row))
                local = (current - clip.start) / clip.speed if source == clip.path else current
                current = before + max(0, local)
                s.timeline.set_global_time(current)
                if s.fallback_playback and source == clip.path and p / 1000 >= clip.end - 0.03:
                    if row + 1 < s.tl.count():
                        next_start = before + clip.out
                        s.tl.setCurrentRow(row + 1)
                        next_clip = s.tl.currentItem().data(Qt.ItemDataRole.UserRole)
                        s.player.setSource(QUrl.fromLocalFile(next_clip.path))
                        s.player.setPosition(int(next_clip.start * 1000))
                        QTimer.singleShot(120, s.player.play)
                        s.timeline.set_global_time(next_start)
                    else:
                        s.fallback_playback = False; s.play.setText('▶  Reproducir todo')
        s.seek.setRange(0, max(0, int(total * 1000)))
        s.seek.setValue(max(0, min(int(current * 1000), int(total * 1000))))
        s.time_label.setText(f'{int(current // 60):02d}:{int(current % 60):02d} / {int(total // 60):02d}:{int(total % 60):02d}')

    def render_preview(s):
        it = s.tl.currentItem()
        if not it:
            return QMessageBox.information(s, 'Vista previa', 'Selecciona un clip de la línea de tiempo.')
        if not ffbin('ffmpeg'):
            return QMessageBox.critical(s, 'Falta FFmpeg', 'Instala FFmpeg y vuelve a abrir NoiseCut Studio.')
        c = copy.deepcopy(it.data(Qt.UserRole))
        if c.kind == 'video' and s.player.source().toLocalFile() == c.path:
            c.start = max(c.start, min(c.end - 0.1, s.player.position() / 1000))
        c.end = min(c.end, c.start + 8 * c.speed)
        preview_settings = s.export_settings_with_model()
        if s.preview_mode.currentData() == 'before':
            c.denoise = 0; c.denoise_mode = 'standard'; c.voice_enhance = False; c.normalize = False
        s.preview_dir = tempfile.mkdtemp(prefix='noisecut_preview_')
        preview_file = os.path.join(s.preview_dir, 'preview.mp4')
        try:
            cmd, duration = build([c], [], [], [], preview_file, s.preview_dir,
                                  size=(640, 360), settings=preview_settings)
        except (OSError, ValueError) as e:
            shutil.rmtree(s.preview_dir, ignore_errors=True)
            return QMessageBox.critical(s, 'Vista previa', f'No se pudo preparar la vista previa.\n{e}')
        s.preview_effects.setEnabled(False); s.statusBar().showMessage('Generando vista previa con efectos…')
        s.preview_worker = Worker(cmd, duration)
        s.preview_worker.done.connect(lambda err: s.preview_finished(err, preview_file))
        s.preview_worker.start()

    def preview_finished(s, err, path):
        s.preview_effects.setEnabled(True)
        if err:
            s.statusBar().clearMessage()
            QMessageBox.critical(s, 'Vista previa', 'No se pudo generar la vista previa. Comprueba que FFmpeg tenga los códecs necesarios.')
            return
        s.player.setSource(QUrl.fromLocalFile(path)); s.player.play()
        s.statusBar().showMessage('Vista previa de hasta 8 segundos con los efectos del clip.')

    def split(s):
        it = s.tl.currentItem()
        if not it:
            return
        c = it.data(Qt.UserRole)
        t = s.player.position() / 1000
        if c.kind == 'video' and s.player.source().toLocalFile() != c.path:
            t = c.start + t * c.speed
        if c.kind != 'video' or not (c.start + 0.1 < t < c.end - 0.1):
            return QMessageBox.information(s, 'Dividir', 'Mueve el cabezal (barra de reproducción) dentro del clip.')
        s.remember()
        n = copy.copy(c); n.start = t; c.end = t
        s.push(n, s.tl.row(it) + 1)

    def dup(s):
        it = s.tl.currentItem()
        if it:
            s.remember()
            s.push(copy.copy(it.data(Qt.UserRole)), s.tl.row(it) + 1)

    def rm(s):
        r = s.tl.currentRow()
        if r >= 0:
            s.remember()
            s.tl.takeItem(r); s.cur = None; s.renumber()

    def state(s):
        return {'clips': [copy.deepcopy(s.tl.item(i).data(Qt.UserRole)) for i in range(s.tl.count())],
                'texts': copy.deepcopy(s.texts), 'stickers': copy.deepcopy(s.stickers),
                'music': copy.deepcopy(s.music), 'overlays': copy.deepcopy(s.overlays),
                'settings': copy.deepcopy(s.export_settings)}

    def remember(s):
        if not hasattr(s, 'tl'):
            return
        current = s.state()
        if s.history_index >= 0 and current == s.history[s.history_index]:
            return
        s.history = s.history[:s.history_index + 1]
        s.history.append(current); s.history_index = len(s.history) - 1

    def restore(s, state):
        s.tl.clear()
        for c in copy.deepcopy(state['clips']):
            it = QListWidgetItem(); it.setData(Qt.UserRole, c); s.tl.addItem(it)
        s.texts[:] = copy.deepcopy(state['texts'])
        s.stickers[:] = copy.deepcopy(state['stickers'])
        s.music[:] = copy.deepcopy(state['music'])
        s.overlays[:] = copy.deepcopy(state.get('overlays', []))
        s.export_settings.update(copy.deepcopy(state.get('settings', {})))
        if hasattr(s, 'burn_subtitles'):
            s.burn_subtitles.setChecked(s.export_settings.get('burn_subtitles', True))
        for key, box in getattr(s, 'setting_boxes', {}).items():
            index = box.findData(s.export_settings[key]); box.setCurrentIndex(max(0, index))
        for refresh in s.track_refresh:
            refresh()
        s.cur = None; s.renumber()
        if hasattr(s, 'timeline'):
            s.timeline.fit_project()

    def undo(s):
        s.remember()
        if s.history_index > 0:
            s.history_index -= 1; s.restore(s.history[s.history_index])

    def redo(s):
        if s.history_index + 1 < len(s.history):
            s.history_index += 1; s.restore(s.history[s.history_index])

    def project_data(s):
        state = s.state()
        return {'format': 'NoiseCutStudio', 'version': 1,
                'clips': [vars(c) for c in state['clips']], 'texts': state['texts'],
                'stickers': state['stickers'], 'music': state['music'],
                'overlays': state['overlays'], 'settings': state['settings']}

    def save_project_as(s):
        path, _ = QFileDialog.getSaveFileName(s, 'Guardar proyecto', 'mi_proyecto.ncs', 'Proyecto NoiseCut (*.ncs *.json)')
        if path:
            s.project_path = path; s.save_project()

    def save_project(s):
        if not s.project_path:
            return s.save_project_as()
        try:
            with open(s.project_path, 'w', encoding='utf-8') as f:
                json.dump(s.project_data(), f, ensure_ascii=False, indent=2)
            s.project_title.setText(os.path.basename(s.project_path))
            s.statusBar().showMessage(f'Proyecto guardado: {s.project_path}', 5000)
        except (OSError, TypeError) as e:
            QMessageBox.critical(s, 'No se pudo guardar', f'No se pudo guardar el proyecto.\n{e}')

    def open_project(s):
        path, _ = QFileDialog.getOpenFileName(s, 'Abrir proyecto', '', 'Proyecto NoiseCut (*.ncs *.json)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('format') != 'NoiseCutStudio' or data.get('version') != 1:
                raise ValueError('El archivo no es un proyecto compatible de NoiseCut Studio.')
            state = {'clips': [Clip(**c) for c in data.get('clips', [])],
                     'texts': data.get('texts', []), 'stickers': data.get('stickers', []),
                     'music': data.get('music', []), 'overlays': data.get('overlays', []),
                     'settings': data.get('settings', {})}
            paths = [c.path for c in state['clips']] + [x['path'] for x in state['stickers']] + [x['path'] for x in state['music']] + [x['path'] for x in state['overlays']]
            missing = [p for p in paths if not os.path.exists(p)]
            if missing:
                raise ValueError('No se encuentran estos archivos de medios:\n' + '\n'.join(missing[:8]))
            s.restore(state); s.project_path = path; s.project_title.setText(os.path.basename(path)); s.history = []; s.history_index = -1; s.remember()
            QMessageBox.information(s, 'Proyecto abierto', 'Proyecto cargado correctamente. Los medios conservan sus rutas originales.')
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            QMessageBox.critical(s, 'No se pudo abrir', str(e))

    def new_project(s):
        s.restore({'clips': [], 'texts': [], 'stickers': [], 'music': [], 'overlays': [], 'settings': {
            'aspect': '16:9', 'resolution': 1080, 'fps': 30, 'quality': 'alta', 'format': 'MP4',
            'background': 'negro', 'burn_subtitles': True}})
        s.project_path = None; s.project_title.setText('Proyecto sin guardar'); s.history = []; s.history_index = -1; s.remember()

    def export(s):
        clips = [s.tl.item(i).data(Qt.UserRole) for i in range(s.tl.count())]
        file_format = s.export_settings['format']
        if not clips and file_format != 'MP3':
            return QMessageBox.information(s, 'Exportar', 'Añade al menos un clip a la línea de tiempo.')
        if not ffbin('ffmpeg') or not ffbin('ffprobe'):
            return QMessageBox.critical(s, 'Falta FFmpeg', 'No se encuentran FFmpeg y FFprobe. Instálalos y vuelve a abrir NoiseCut Studio.')
        names = {'MP4': ('mi_video.mp4', 'Video MP4 (*.mp4)'),
                 'MP3': ('mi_audio.mp3', 'Audio MP3 (*.mp3)'), 'GIF': ('mi_animacion.gif', 'Imagen GIF (*.gif)')}
        default_name, file_filter = names.get(file_format, names['MP4'])
        out, _ = QFileDialog.getSaveFileName(s, 'Exportar', default_name, file_filter)
        if not out:
            return
        tmp = tempfile.mkdtemp()
        try:
            cmd, T = build(clips, s.texts, s.stickers, s.music, out, tmp,
                           overlays=s.overlays, settings=s.export_settings_with_model())
        except (OSError, ValueError, KeyError, TypeError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return QMessageBox.critical(s, 'No se pudo preparar la exportación', f'Revisa los ajustes y los archivos de medios.\n{e}')
        s.dlg = QProgressDialog('Exportando…', None, 0, 100, s); s.dlg.setWindowModality(Qt.WindowModal); s.dlg.show()
        s.wk = Worker(cmd, T); s.wk.prog.connect(s.dlg.setValue)
        s.wk.done.connect(lambda err: s.finished(err, out, tmp)); s.wk.start()

    def finished(s, err, out, tmp):
        s.dlg.close(); shutil.rmtree(tmp, ignore_errors=True)
        if err:
            QMessageBox.critical(s, 'Error al exportar', err)
        else:
            QMessageBox.information(s, 'Listo', f'Archivo exportado:\n{out}')


if __name__ == '__main__':
    app = QApplication(sys.argv); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    w = Main(); w.show(); sys.exit(app.exec())
