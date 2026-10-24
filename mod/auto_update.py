"""Stage verified GitHub Releases; apply them after this launcher exits."""
import hashlib
import contextlib
import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

REPOSITORY = 'VladXon/azeroth-survivors-mod-releases'
LATEST = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
ROOT_FILES = ('Azeroth Survivors MODDED.bat', 'Azeroth Survivors MODDED Онлайн.bat',
              'ПРОЧИТАЙ - мод.txt', 'УСТАНОВКА И ОБНОВЛЕНИЕ.txt')
MAX_ZIP = 64 * 1024 * 1024
VERSION_RE = re.compile(r'^(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d+))?-[0-9a-f]{7,40}$')


def version_key(value):
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError('Unsupported release version')
    return tuple(int(v or 0) for v in match.groups())


def asset_url(url, tag, name):
    expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
    if urllib.parse.unquote(url) != expected:
        raise ValueError('Release asset is outside the configured repository')
    return url


def select_release(payload, current):
    if payload.get('draft') or payload.get('prerelease'):
        return None
    tag = payload.get('tag_name', '')
    version = tag.removeprefix('v')
    target = version_key(version)
    try:
        installed = version_key(current)
    except ValueError:
        installed = (0, 0, 0, 0)  # old portable builds used only a commit SHA
    if current == version or installed >= target:
        return None
    assets = payload.get('assets', [])
    candidates = [a for a in assets if re.fullmatch(
        re.escape(f'AzerothSurvivors-mod-{version}-game-') + r'[0-9a-f]{8}\.zip', a.get('name', ''))]
    if len(candidates) != 1:
        raise ValueError('Expected one portable Windows ZIP')
    archive = candidates[0]
    checksums = [a for a in assets if a.get('name') == archive['name'] + '.sha256']
    if len(checksums) != 1:
        raise ValueError('Release checksum is missing or ambiguous')
    checksum = checksums[0]
    return {'version': version, 'name': archive['name'], 'digest': archive.get('digest'),
            'url': asset_url(archive['browser_download_url'], tag, archive['name']),
            'checksum_url': asset_url(checksum['browser_download_url'], tag, checksum['name'])}


def fetch(url, *, limit=MAX_ZIP, progress=None):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'AzerothSurvivorsMod-Updater/1',
        'Accept': 'application/vnd.github+json' if url == LATEST else 'application/octet-stream'})
    started, chunks, total = time.monotonic(), [], 0
    with urllib.request.urlopen(request, timeout=8) as response:
        length = int(response.headers.get('Content-Length') or 0)
        if length > limit:
            raise ValueError('Update download exceeds size limit')
        while True:
            if time.monotonic() - started > 90:
                raise TimeoutError('Update download timed out')
            chunk = response.read(min(256 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError('Update download exceeds size limit')
            if progress:
                progress(f'Загрузка обновления: {total * 100 // length}%' if length
                         else f'Загрузка обновления: {total // 1024} КБ')
    return b''.join(chunks)


def validate_members(archive):
    entries, seen, size = [], set(), 0
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        parts = path.parts
        canonical = path.as_posix() + ('/' if item.is_dir() else '')
        if (not parts or canonical != item.filename or path.is_absolute() or '\\' in item.filename
                or any(p in ('.', '..') or re.search(r'[<>:"|?*\x00-\x1f]', p)
                       or p.endswith((' ', '.')) or p.casefold() == '.git'
                       or re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p, re.I)
                       for p in parts)
                or stat.S_ISLNK(item.external_attr >> 16)):
            raise ValueError('Unsafe ZIP member')
        if parts[0] != 'mod' and item.filename not in ROOT_FILES:
            raise ValueError('Release ZIP contains unexpected game-root files')
        key = item.filename.rstrip('/').casefold()
        if key in seen:
            raise ValueError('Duplicate ZIP member')
        seen.add(key)
        size += item.file_size
        if len(seen) > 5000 or size > 160 * 1024 * 1024:
            raise ValueError('Expanded update exceeds size limit')
        entries.append(item)
    required = {'mod/version', 'mod/release.json', 'mod/launcher.pyw', 'mod/patch_azeroth.py',
                'mod/auto_update.py', 'mod/apply_update.ps1',
                'mod/runtime/azerothsurvivorsmod.exe'}
    if not required <= seen:
        raise ValueError('Incomplete portable update')
    return entries


def stage_release(mod, selected, progress=lambda message: None):
    mod = Path(mod).resolve()
    checksum = fetch(selected['checksum_url'], limit=4096).decode('ascii').strip().split()
    if len(checksum) != 2 or checksum[1].lstrip('*') != selected['name'] or not re.fullmatch(r'[0-9a-fA-F]{64}', checksum[0]):
        raise ValueError('Invalid release checksum')
    data = fetch(selected['url'], progress=progress)
    digest = hashlib.sha256(data).hexdigest()
    if digest != checksum[0].lower() or selected.get('digest') not in (None, 'sha256:' + digest):
        raise ValueError('Update SHA-256 verification failed')
    stage = mod.parent / ('.azs-update-' + uuid.uuid4().hex[:12])
    stage.mkdir()
    try:
        archive_path = stage / 'release.zip'
        archive_path.write_bytes(data)
        with zipfile.ZipFile(archive_path) as archive:
            entries = validate_members(archive)
            if archive.read('mod/VERSION').decode('utf-8').strip() != selected['version']:
                raise ValueError('Release version disagrees with ZIP')
            metadata = json.loads(archive.read('mod/RELEASE.json'))
            if metadata.get('release_version') != selected['version']:
                raise ValueError('Release metadata disagrees with ZIP')
            archive.extractall(stage, members=entries)
        archive_path.unlink()
        for name in ('pvp_server.txt', 'updater.json'):
            if (mod / name).is_file():
                if name == 'pvp_server.txt' and (mod / name).read_text(encoding='utf-8').strip() in metadata.get('server_migrations', []):
                    continue  # Publisher explicitly migrates an old official default.
                shutil.copy2(mod / name, stage / 'mod' / name)
        shutil.copy2(mod / 'apply_update.ps1', stage / 'apply_update.ps1')
        plan = {'format': 1, 'game': str(mod.parent), 'stage': str(stage),
                'version': selected['version'], 'parent_pid': os.getpid(), 'online': False}
        plan_path = stage / 'plan.json'
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        return plan_path
    except BaseException:
        shutil.rmtree(stage)
        raise


def maybe_stage(mod, progress):
    mod = Path(mod)
    if (os.environ.get('AZS_SKIP_UPDATE') == '1' or (mod / '.git').exists()
            or not (mod / 'VERSION').is_file() or not (mod / 'runtime/AzerothSurvivorsMod.exe').is_file()):
        return None
    try:
        config = mod / 'updater.json'
        if config.exists() and not json.loads(config.read_text(encoding='utf-8')).get('enabled', True):
            return None
        progress('Проверка обновлений на GitHub…')
        selected = select_release(json.loads(fetch(LATEST, limit=1024 * 1024)),
                                  (mod / 'VERSION').read_text(encoding='utf-8').strip())
        return stage_release(mod, selected, progress) if selected else None
    except Exception as exc:
        print(f'[updater] {type(exc).__name__}: {exc}; keeping installed version',
              file=sys.stderr, flush=True)
        progress('Обновление недоступно — запуск установленной версии')
        return None


def start_apply(plan_path, online=False):
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    plan['online'] = bool(online)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    powershell = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    subprocess.Popen([str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass',
                      '-File', str(plan_path.parent / 'apply_update.ps1'),
                      '-PlanPath', str(plan_path)],
                     creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


@contextlib.contextmanager
def single_launcher(mod):
    """Keep two launchers from staging/applying an update to the same folder."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    identity = hashlib.sha256(str(Path(mod).resolve()).casefold().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, 'Local\\AZSLauncher-' + identity)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:
            raise RuntimeError('Запускатель мода уже открыт. Закрой его перед повторным запуском.')
        yield
    finally:
        kernel.CloseHandle(handle)
