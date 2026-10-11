"""Read-only catalogue of saved maps in the operator-configured world allowlist."""
from pathlib import Path
import re
import stat

WORLD_ID = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')


def saved_world_exists(runtime):
    """Inspect metadata only; stopped maps need no process or initialized inbox."""
    world = runtime.parent
    try:
        for directory in reversed((world, *world.parents)):
            if not stat.S_ISDIR(directory.lstat().st_mode):
                return False
        settings = (world / 'world.mt').lstat()
        return stat.S_ISREG(settings.st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def catalogue(config):
    """Never scan unconfigured directories or disclose server filesystem paths."""
    worlds = config.get('worlds')
    if not isinstance(worlds, dict) or len(worlds) > 1000:
        raise ValueError('Invalid world catalogue configuration')
    result = []
    for world_id, runtime_path in sorted(worlds.items()):
        if not isinstance(world_id, str) or not WORLD_ID.fullmatch(world_id):
            raise ValueError('Invalid world ID')
        if not isinstance(runtime_path, str):
            raise ValueError('Invalid world runtime path')
        runtime = Path(runtime_path)
        if not runtime.is_absolute() or '..' in runtime.parts:
            raise ValueError('Invalid world runtime path')
        if saved_world_exists(runtime):
            result.append({'world_id': world_id, 'name': world_id})
    return {'version': 1, 'worlds': result}
