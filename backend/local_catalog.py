"""Pinned download catalog. Models are downloaded, never committed or converted.

Sizes and SHA256 values come from Hugging Face LFS metadata and GitHub release
asset digests. Memory estimates are conservative planning inputs, not benchmarks.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote


@dataclass(frozen=True)
class Artifact:
    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    repo: str
    revision: str
    files: tuple[Artifact, ...]
    estimated_vram_mb: int
    license: str = 'Apache-2.0'
    notes: str = ''
    disable_thinking: bool = False

    @property
    def size(self) -> int:
        return sum(item.size for item in self.files)


def _hf(repo: str, revision: str, filename: str, size: int, sha: str) -> Artifact:
    return Artifact(filename, f'https://huggingface.co/{repo}/resolve/{revision}/{filename}', size, sha)


RUNTIME_VERSION = 'b10919'
_RELEASE = f'https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_VERSION}'


def _release(name: str, size: int, sha: str) -> Artifact:
    return Artifact(name, f'{_RELEASE}/{name}', size, sha)


RUNTIMES = {
    'windows-cpu': (
        _release('llama-b10919-bin-win-cpu-x64.zip', 18429028,
                 '443eb9e591395e22d8b588d46235ae496a478f06bee6f1692135f38642c097de'),
    ),
    'windows-cuda': (
        _release('llama-b10919-bin-win-cuda-12.4-x64.zip', 254085460,
                 '7d2d0059c0b19c51f820f7b416265de5253936b3708485afa81ea5c94b72d747'),
        _release('cudart-llama-bin-win-cuda-12.4-x64.zip', 391443627,
                 '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6'),
    ),
}


def model_from_dict(value: dict) -> ModelSpec:
    """Validate the storage contract, not a list of known vendors/model names."""
    model_id = str(value['id'])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', model_id):
        raise ValueError('Invalid model id')
    files = tuple(Artifact(**item) for item in value['files'])
    if not files or len({a.filename.casefold() for a in files}) != len(files):
        raise ValueError('Model must have unique GGUF files')
    for item in files:
        if (not re.fullmatch(r'[\w .()-]+\.gguf', item.filename, re.IGNORECASE)
                or item.filename.startswith('.') or item.size <= 0
                or not re.fullmatch(r'[a-f0-9]{64}', item.sha256)
                or not item.url.startswith('https://huggingface.co/')):
            raise ValueError('Invalid GGUF artifact metadata')
    estimate = int(value['estimated_vram_mb'])
    if estimate <= 0:
        raise ValueError('VRAM estimate must be positive')
    return ModelSpec(model_id, str(value['name']), str(value['repo']),
                     str(value['revision']), files, estimate,
                     str(value.get('license') or 'See model card'),
                     str(value.get('notes') or ''), bool(value.get('disable_thinking', False)))


MODELS = tuple(model_from_dict(item) for item in json.loads(
    Path(__file__).with_name('local_models.json').read_text(encoding='utf-8'))['models'])


def list_models(config: dict | None = None) -> tuple[ModelSpec, ...]:
    models = {model.id: model for model in MODELS}
    for entry in (config or {}).get('local_llama', {}).get('custom_models', []):
        model = model_from_dict(entry)
        if model.id in models:
            raise ValueError(f'Duplicate model id: {model.id}')
        models[model.id] = model
    return tuple(models.values())


def get_model(model_id: str, config: dict | None = None) -> ModelSpec:
    for model in list_models(config):
        if model.id == model_id:
            return model
    raise ValueError(f'Unknown local model: {model_id}')


def resolve_hf_model(repo: str, filename: str, revision: str = 'main', *, fetch=None) -> ModelSpec:
    """Resolve a public HF GGUF to immutable, hash-verified files (including shards).

    Only weights are fetched. No repository Python code or conversion is executed.
    Special-purpose chat templates may require a future provider adapter.
    """
    from urllib.request import urlopen
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('Use a Hugging Face repository in owner/name format')
    if PurePosixPath(filename).is_absolute() or '..' in PurePosixPath(filename).parts or '\\' in filename:
        raise ValueError('Use a relative GGUF filename from the repository')
    url = f'https://huggingface.co/api/models/{repo}/revision/{quote(revision, safe="")}?blobs=true'
    if fetch is None:
        def fetch(address):
            with urlopen(address, timeout=30) as response:
                return json.load(response)
    metadata = fetch(url)
    if metadata.get('gated'):
        raise ValueError('This repository requires access approval. Use a public GGUF or an external API.')
    commit = str(metadata['sha'])
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Repository did not return a pinned commit')
    entries = {item['rfilename']: item for item in metadata['siblings']}
    if filename not in entries or not filename.lower().endswith('.gguf'):
        raise ValueError('GGUF file not found in this revision')
    shard = re.fullmatch(r'(.*)-(\d{5})-of-(\d{5})\.gguf', filename)
    names = [filename]
    if shard:
        count = int(shard[3])
        if not 1 <= count <= 999 or not 1 <= int(shard[2]) <= count:
            raise ValueError('Invalid GGUF shard count')
        names = [f'{shard[1]}-{index:05}-of-{count:05}.gguf' for index in range(1, count + 1)]
    files = []
    for name in names:
        entry = entries.get(name, {})
        lfs = entry.get('lfs') or {}
        if not lfs.get('sha256') or not entry.get('size'):
            raise ValueError(f'Missing file or SHA256 metadata: {name}')
        files.append(_hf(repo, commit, PurePosixPath(name).name, entry['size'], lfs['sha256']))
        # The local basename stays flat; download URL preserves repository subfolders.
        files[-1] = Artifact(files[-1].filename,
                             f'https://huggingface.co/{repo}/resolve/{commit}/{quote(name, safe="/")}',
                             files[-1].size, files[-1].sha256)
    from dataclasses import asdict
    identity = hashlib.sha256(f'{repo}@{commit}/{names[0]}'.encode()).hexdigest()[:24]
    model = ModelSpec('hf-' + identity, f'{repo} · {PurePosixPath(names[0]).name}', repo, commit,
                      tuple(files), int(sum(a.size for a in files) / 1024**2 * 1.25) + 768,
                      str((metadata.get('cardData') or {}).get('license') or 'See model card'),
                      'Custom GGUF: compatibility and translation quality require testing.', True)
    return model_from_dict(asdict(model))
