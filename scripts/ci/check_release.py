# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2
"""Check version alignment and the actual built distribution contents."""
import ast
from email.parser import BytesParser
import json
import os
from pathlib import Path
import tarfile
import tomllib
import zipfile

root = Path(__file__).resolve().parents[2]
project = tomllib.loads((root / 'backend/pyproject.toml').read_text())['project']
version = project['version']
frontend = json.loads((root / 'frontend/package.json').read_text())
lock = json.loads((root / 'frontend/package-lock.json').read_text())
module = ast.parse((root / 'backend/mailintel/__init__.py').read_text())
python_version = next(ast.literal_eval(node.value) for node in module.body
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == '__version__'
                          for target in node.targets))
assert version == frontend['version'] == lock['version'] == lock['packages']['']['version'] == python_version
if os.environ.get('GITHUB_REF_TYPE') == 'tag':
    assert os.environ['GITHUB_REF_NAME'] == f'v{version}', 'Release tag must match package versions'
license_text = (root / 'LICENSE').read_bytes()
for path in ('backend/LICENSE', 'LICENSES/EUPL-1.2.txt'):
    assert (root / path).read_bytes() == license_text, path
wheels = list((root / 'dist').glob('*.whl'))
sources = list((root / 'dist').glob('mailintel-*.tar.gz'))
# pack-source.sh produces a separate repository archive.
sources = [path for path in sources if not path.name.startswith('mailintel-source')]
assert len(wheels) == len(sources) == 1, 'Build into a clean dist directory'
with zipfile.ZipFile(wheels[0]) as wheel:
    names = wheel.namelist()
    metadata = BytesParser().parsebytes(wheel.read(next(n for n in names if n.endswith('.dist-info/METADATA'))))
    assert metadata['Version'] == version
    assert metadata['License-Expression'] == 'EUPL-1.2'
    assert 'Milan Pikula' in (metadata.get('Author', '') + metadata.get('Author-email', ''))
    assert wheel.read(next(n for n in names if n.endswith('.dist-info/licenses/LICENSE'))) == license_text
    for path in ('mailintel/web/build/index.html', 'mailintel/resources/prompts.yaml',
                 'mailintel/persistence/migrations/versions/0001_initial_schema.sql'):
        assert path in names, path
with tarfile.open(sources[0]) as source:
    license_member = next(m for m in source.getmembers() if m.name == f'mailintel-{version}/LICENSE')
    assert source.extractfile(license_member).read() == license_text
print(f'Release {version}: versions, license copies, package metadata, and wheel resources verified')
