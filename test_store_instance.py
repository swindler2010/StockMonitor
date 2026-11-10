"""Real Windows mutex checks using only unique temporary test identities."""
import ast
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'stock_monitor'
sys.path.insert(0, str(ROOT))
from store_release import claim_instance

def child(path, hold=False):
    code = "import sys;sys.path.insert(0,sys.argv[1]);from store_release import claim_instance,AlreadyRunning\ntry:\n claim_instance(sys.argv[2]);print('owned',flush=True)\nexcept AlreadyRunning:\n print('duplicate',flush=True);sys.exit(0)\n"
    if hold:
        code += "input()\n"
    return subprocess.Popen([sys.executable, '-c', code, str(ROOT), str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

with tempfile.TemporaryDirectory(prefix='stockmonitor-instance-', dir=Path(__file__).parent) as directory:
    identity = Path(directory) / 'isolated'
    owner = child(identity, True)
    try:
        assert owner.stdout.readline().strip() == 'owned'
        duplicate = child(identity)
        out, err = duplicate.communicate(timeout=10)
        assert duplicate.returncode == 0 and out.strip() == 'duplicate', (out, err)
        other = child(Path(directory) / 'other')
        out, err = other.communicate(timeout=10)
        assert other.returncode == 0 and out.strip() == 'owned', (out, err)
    finally:
        owner.communicate('\n', timeout=10)
    reopened = child(identity)
    out, err = reopened.communicate(timeout=10)
    assert reopened.returncode == 0 and out.strip() == 'owned', (out, err)
    crashed = child(identity, True)
    assert crashed.stdout.readline().strip() == 'owned'
    crashed.kill()
    crashed.communicate(timeout=10)
    recovered = child(identity)
    out, err = recovered.communicate(timeout=10)
    assert recovered.returncode == 0 and out.strip() == 'owned', (out, err)
    claim_instance(identity)
    claim_instance(identity)  # Same process remains idempotent.

tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name) and n.type.id == 'AlreadyRunning')
assert 'startup-error.log' not in ast.unparse(handler)
print('PASS: duplicate rejected, separate identity allowed, exit releases lock, same-process idempotence, duplicate does not overwrite crash log')
