"""Prueba el motor de exportación (sin abrir la interfaz). Requiere ffmpeg en PATH."""
import re, os, subprocess, tempfile, shutil
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'noisecut.py'), encoding='utf-8').read()
head = re.sub(r'^from PySide6[^\n]*\n(?:[ \t]+[^\n]*\n)*', '',
              src.split('class Worker')[0], flags=re.M)
ns = {'__name__': 'x'}; exec(head, ns)
Clip, build, probe, canvas_size = ns['Clip'], ns['build'], ns['probe'], ns['canvas_size']
audio_fx_filters, denoise_f = ns['audio_fx_filters'], ns['denoise_f']
video_fx_filters = ns['video_fx_filters']
subtitle_records = ns['subtitle_records']
stabilization_commands = ns['stabilization_commands']
project_duration = ns['project_duration']
preview_fingerprint = ns['preview_fingerprint']

# Estas comprobaciones no ejecutan FFmpeg: detectan cambios accidentales en
# los filtros y el tamaño de previsualización incluso en equipos sin FFmpeg.
static_dir = tempfile.mkdtemp()
try:
    preview_clip = Clip('entrada.mp4', 'video', 8, True, 0, 4, bright=0.1,
                        contrast=1.2, sat=0.5, blur=2, denoise=15, fade=0.3)
    command, duration = build([preview_clip], [], [],
                              [{'path': 'musica.wav', 'offset': 1, 'vol': 0.5, 'denoise': 12}],
                              'preview.mp4', static_dir, size=(640, 360))
    graph = command[command.index('-filter_complex') + 1]
    for expected in ('scale=640:360:', 'eq=brightness=0.1:contrast=1.2:saturation=0.5',
                     'gblur=sigma=2', 'fade=t=in:st=0:d=0.3',
                     'highpass=f=80,afftdn=nr=15:nf=-30:tn=1',
                     'highpass=f=80,afftdn=nr=12:nf=-30:tn=1', 'volume=0.5', 'adelay=1000|1000'):
        assert expected in graph, f'No se construyó el filtro esperado: {expected}'
    assert duration == 4
    print('OK: filtros y tamaño de vista previa se construyen correctamente')

    transform = Clip('entrada.mp4', 'video', 8, True, 0, 4, scale=75, pos_x=20, pos_y=70,
                     rotation=10, flip_h=True, flip_v=True, crop_left=10, crop_right=10,
                     crop_top=10, crop_bottom=10, opacity=50, reverse=True)
    settings = {'aspect': '9:16', 'resolution': 720, 'fps': 24, 'quality': 'baja',
                'background': 'desenfocado', 'format': 'MP4'}
    command, _ = build([transform], [], [], [], 'transform.mp4', static_dir,
                       overlays=[{'path': 'pip.mp4', 'start': 1, 'end': 3, 'scale': 25, 'x': 80, 'y': 20}],
                       settings=settings)
    graph = command[command.index('-filter_complex') + 1]
    for expected in ('crop=iw*0.8000:ih*0.8000', 'hflip', 'vflip', 'rotate=10*PI/180',
                     'reverse', 'scale=540:960:', 'colorchannelmixer=aa=0.500',
                     'gblur=sigma=24', "enable='between(t,1,3)'", '-crf'):
        assert expected in graph or expected in command, f'No se construyó el ajuste esperado: {expected}'
    assert canvas_size({'aspect': '9:16', 'resolution': 720}) == (720, 1280)

    mp3_command, mp3_duration = build([], [], [],
        [{'path': 'audio.wav', 'offset': 0, 'vol': 1, 'denoise': 0, 'duration': 5}],
        'audio.mp3', static_dir, settings={'format': 'MP3', 'quality': 'media'})
    mp3_graph = mp3_command[mp3_command.index('-filter_complex') + 1]
    assert '-c:a' in mp3_command and 'libmp3lame' in mp3_command and '-vn' in mp3_command and mp3_duration == 5
    assert '[aout]' in mp3_graph and 'adelay=0|0' in mp3_graph and 'volume=1' in mp3_graph
    voiceover = [{'path': 'voz.wav', 'offset': 2, 'vol': 0.8, 'denoise': 10,
                  'duration': 5, 'fade_in': 0.5, 'fade_out': 1.0,
                  'normalize': True, 'voice_enhance': True, 'pitch': 2}]
    voice_cmd, _ = build([], [], [], voiceover, 'voz.mp3', static_dir,
                         settings={'format': 'MP3', 'quality': 'alta'})
    voice_graph = voice_cmd[voice_cmd.index('-filter_complex') + 1]
    for expected in ('afftdn=nr=10', 'loudnorm=I=-16:TP=-1.5:LRA=11',
                     'acompressor=', 'afade=t=in:st=0:d=0.5',
                     'afade=t=out:st=4.000:d=1.0', 'asetrate=48000*1.122462',
                     'adelay=2000|2000', 'volume=0.8'):
        assert expected in voice_graph, f'No se construyó el filtro de voz esperado: {expected}'
    gif_command, _ = build([preview_clip], [], [], [], 'anim.gif', static_dir,
                           settings={'format': 'GIF', 'fps': 24})
    assert 'anim.gif' in gif_command and '-loop' in gif_command
    print('OK: transformaciones, superposición, lienzo y formatos se construyen correctamente')

    audio_filters = audio_fx_filters(15, 'standard', normalize=True, voice_enhance=True, pitch=12)
    for expected in ('highpass=f=80', 'afftdn=nr=15:nf=-30:tn=1', 'loudnorm=I=-16:TP=-1.5:LRA=11',
                     'agate=threshold=0.015:ratio=2.5:attack=20:release=250:range=0.02',
                     'equalizer=f=120:t=q:w=1:g=-3', 'equalizer=f=3000:t=q:w=1:g=3',
                     'acompressor=threshold=-22dB:ratio=3:attack=10:release=200',
                     'asetrate=48000*2.000000', 'aresample=48000', 'atempo=0.5000'):
        assert expected in audio_filters, f'No se construyó el efecto de audio esperado: {expected}'
    clean_cmd, _ = build([], [], [], [{'path': 'voz-sintetica.wav', 'offset': 0, 'vol': 1,
        'denoise': 15, 'denoise_mode': 'standard', 'duration': 6,
        'voice_enhance': True, 'normalize': True}], 'voz-limpia.mp3', static_dir,
        settings={'format': 'MP3', 'quality': 'alta'})
    clean_graph = clean_cmd[clean_cmd.index('-filter_complex') + 1]
    for expected in ('highpass=f=80', 'afftdn=nr=15:nf=-30:tn=1', 'agate=',
                     'equalizer=f=120:', 'equalizer=f=3000:', 'acompressor=', 'loudnorm='):
        assert expected in clean_graph, f'build() no incluyó filtro esperado: {expected}'
    ai_filter = denoise_f(20, 'ai', 'C:/models/rnnoise.rnnn')
    assert ai_filter == "highpass=f=80,arnndn=m='C\\:/models/rnnoise.rnnn'"
    assert denoise_f(0, 'ai', None) is None
    assert 'afftdn=nr=15:nf=-30:tn=1' in audio_fx_filters(15, 'ai', None)
    assert 'arnndn=' in ','.join(audio_fx_filters(15, 'ai', 'rnnoise.rnnn'))
    assert audio_fx_filters(8) != audio_fx_filters(15) != audio_fx_filters(25)
    assert project_duration([Clip('a', 'video', 10, True, 0, 4),
                             Clip('b', 'video', 10, True, 0, 5)]) == 9
    assert preview_fingerprint({'clips': [1]}) == preview_fingerprint({'clips': [1]})
    assert preview_fingerprint({'clips': [1]}) != preview_fingerprint({'clips': [2]})
    preview_cmd, _ = build([preview_clip], [], [], [], 'preview-fast.mp4', static_dir,
                           settings={'format': 'MP4', 'preview': True, 'quality': 'baja'})
    assert preview_cmd[preview_cmd.index('-preset') + 1] == 'ultrafast'
    print('OK: filtros de audio estándar, IA, voz, volumen y tono se construyen correctamente')

    image_fx = video_fx_filters({'vignette': 40, 'grain': 20, 'sharpness': 1.5,
        'mirror': True, 'glitch': True, 'black_white': True, 'sepia': True, 'pixelate': 12},
        1280, 720, chroma=True, chroma_color='#00ff00', chroma_similarity=0.3,
        lut_path='C:/looks/cine.cube')
    image_fx = ';'.join(image_fx)
    for expected in ('vignette=angle=', 'noise=alls=20:allf=t', 'unsharp=5:5:1.50',
                     'hflip', 'rgbashift=rh=4:bh=-4', 'hue=s=0', 'colorchannelmixer=',
                     'scale=trunc(iw/12):trunc(ih/12):flags=neighbor,scale=1280:720:flags=neighbor',
                     'colorkey=0x00ff00:0.300:0.10', "lut3d=file='C\\:/looks/cine.cube'"):
        assert expected in image_fx, f'No se construyó el efecto esperado: {expected}'
    assert len({'fade', 'dissolve', 'wipeleft', 'wiperight', 'wipeup', 'wipedown',
                'slideleft', 'slideright', 'slideup', 'slidedown', 'circleopen',
                'circleclose', 'zoomin', 'radial', 'smoothleft', 'smoothright',
                'smoothup', 'smoothdown', 'pixelize'}) >= 12
    detect_cmd, stabilize_cmd = stabilization_commands('ffmpeg', 'source.mp4', 'motion.trf', 'steady.mp4')
    assert 'vidstabdetect=shakiness=5:accuracy=15:result=motion.trf' in detect_cmd
    assert 'vidstabtransform=input=motion.trf:smoothing=10:optzoom=1' in stabilize_cmd
    assert '-map' in stabilize_cmd and '0:a?' in stabilize_cmd and stabilize_cmd[-1] == 'steady.mp4'
    transition_clips = [Clip('a.mp4', 'video', 8, True, 0, 4),
                        Clip('b.mp4', 'video', 4, True, 0, 4,
                             transition='circleopen', transition_duration=0.7)]
    transition_cmd, transition_duration = build(transition_clips, [], [], [], 'xfade.mp4', static_dir)
    transition_graph = transition_cmd[transition_cmd.index('-filter_complex') + 1]
    assert 'xfade=transition=circleopen:duration=0.700:offset=3.300' in transition_graph
    assert 'acrossfade=d=0.700:c1=tri:c2=tri' in transition_graph
    assert transition_duration == 7.3
    text_cmd, _ = build([preview_clip], [{'text': 'Hola', 'start': 0, 'end': 3, 'x': 50,
        'y': 85, 'size': 64, 'color': '#ffffff', 'bold': True, 'border': True,
        'shadow': True, 'background': True, 'background_color': '#112233', 'animation': 'write'}],
        [], [], 'text.mp4', static_dir)
    text_graph = text_cmd[text_cmd.index('-filter_complex') + 1]
    assert 'drawtext=textfile=' in text_graph and 'shadowx=3:shadowy=3' in text_graph
    assert 'box=1:boxcolor=0x112233@0.75' in text_graph and "enable='between(t," in text_graph
    print('OK: efectos de imagen, transiciones, croma, LUT y texto estilizado se construyen correctamente')

    subtitles = subtitle_records([(0.2, 1.7, '  Hola mundo  '), (2, 2, 'Segunda línea'), (3, 4, '  ')])
    assert len(subtitles) == 2 and subtitles[0]['text'] == 'Hola mundo'
    assert subtitles[0]['start'] == 0.2 and subtitles[0]['end'] == 1.7
    assert subtitles[1]['end'] == 2.1 and subtitles[1]['animation'] == 'fade'
    assert subtitles[0]['is_subtitle'] is True
    burned_cmd, _ = build([preview_clip], subtitles, [], [], 'burned.mp4', static_dir)
    hidden_cmd, _ = build([preview_clip], subtitles, [], [], 'unburned.mp4', static_dir,
                          settings={'burn_subtitles': False})
    assert 'drawtext=textfile=' in burned_cmd[burned_cmd.index('-filter_complex') + 1]
    assert 'drawtext=textfile=' not in hidden_cmd[hidden_cmd.index('-filter_complex') + 1]
    print('OK: segmentos automáticos se convierten en subtítulos editables')
finally:
    shutil.rmtree(static_dir, ignore_errors=True)

if not shutil.which('ffmpeg'):
    print('OMITIDO: exportación de audio/video con FFmpeg (no está instalado).')
    raise SystemExit(0)

d = tempfile.mkdtemp(); os.chdir(d)
def ff(*a): subprocess.run(['ffmpeg', '-v', 'error', '-y', *a], check=True)
def noise_db(path):
    result = subprocess.run(['ffmpeg', '-hide_banner', '-ss', '3.2', '-t', '2', '-i', path,
        '-af', 'volumedetect', '-f', 'null', '-'], capture_output=True, text=True)
    match = re.search(r'mean_volume:\s*(-?\d+(?:\.\d+)?) dB', result.stderr)
    assert result.returncode == 0 and match, result.stderr[-800:]
    return float(match.group(1))

# Voz sintética en la primera mitad y ruido blanco en todo el archivo. Se mide
# la ventana final, que contiene ruido sin señal de voz, antes y después del build.
ff('-f', 'lavfi', '-i', 'sine=f=180:d=6:r=48000',
   '-f', 'lavfi', '-i', 'anoisesrc=color=white:a=0.08:d=6:r=48000',
   '-filter_complex', "[0:a]volume='if(lt(t,3),0.25,0)':eval=frame[v];[v][1:a]amix=inputs=2:duration=longest[a]",
   '-map', '[a]', '-c:a', 'pcm_s16le', 'voz_con_ruido.wav')
noise_before = noise_db('voz_con_ruido.wav')
clean_items = [{'path': 'voz_con_ruido.wav', 'offset': 0, 'vol': 1, 'denoise': 15,
                'denoise_mode': 'standard', 'duration': 6, 'voice_enhance': True,
                'normalize': False}]
clean_cmd, _ = build([], [], [], clean_items, 'voz_limpia.mp3', d,
                     settings={'format': 'MP3', 'quality': 'alta'})
clean_run = subprocess.run(clean_cmd, capture_output=True, text=True)
assert clean_run.returncode == 0, clean_run.stderr[-1500:]
noise_after = noise_db('voz_limpia.mp3')
assert noise_after < noise_before, f'El ruido no bajó: antes {noise_before} dB, después {noise_after} dB'
print('OK: FFmpeg redujo el nivel medido del tramo con ruido sintético (%.1f → %.1f dB)' %
      (noise_before, noise_after))

ff('-f', 'lavfi', '-i', 'testsrc=d=4:s=1280x720:r=30', '-f', 'lavfi', '-i', 'sine=f=440:d=4',
   '-f', 'lavfi', '-i', 'anoisesrc=d=4:a=0.2', '-filter_complex', '[1][2]amix=inputs=2[a]',
   '-map', '0:v', '-map', '[a]', '-c:v', 'libx264', '-c:a', 'aac', 'a.mp4')
ff('-f', 'lavfi', '-i', 'color=c=red:s=640x480:d=1', '-frames:v', '1', 'img.png')
ff('-f', 'lavfi', '-i', 'sine=f=300:d=5', 'm.mp3')
dur, au = probe('a.mp4')
clips = [Clip('a.mp4', 'video', dur, au, 0, 2, speed=1.5, bright=0.1, blur=2, denoise=15, fade=0.3),
         Clip('img.png', 'image', 5, False, 0, 2), Clip('a.mp4', 'video', dur, au, 2, 4)]
texts = [dict(text='Hola: mundo', start=0, end=3, x=50, y=85, size=64, color='#ffffff')]
stick = [dict(path='img.png', start=0, end=2, x=85, y=15, w=20)]
music = [dict(path='m.mp3', offset=1, vol=0.5, denoise=10)]
cmd, T = build(clips, texts, stick, music, 'out.mp4', d)
cmd = [x for x in cmd if x not in ('-progress', 'pipe:1')]
r = subprocess.run(cmd, capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-1500:]
assert os.path.getsize('out.mp4') > 1000
print('OK: exportación correcta, duración esperada %.2fs' % T)
shutil.rmtree(d, ignore_errors=True)
