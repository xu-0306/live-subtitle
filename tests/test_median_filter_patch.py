"""The no-triton median filter patch must return exactly what whisperlivekit returns."""
import importlib.util

import torch

from backend import server
from whisperlivekit.simul_whisper import simul_whisper
from whisperlivekit.whisper.timing import median_filter as upstream


def test_patched_median_filter_matches_upstream(monkeypatch):
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *a: None if name == 'triton' else real_find_spec(name, *a))
    monkeypatch.setattr(simul_whisper, 'median_filter', upstream)
    server._patch_whisperlivekit_median_filter()
    patched = simul_whisper.median_filter
    assert patched is not upstream

    torch.manual_seed(0)
    for shape in [(1, 6, 9, 1500), (1, 72, 3, 40), (2, 4, 5, 8), (1, 1, 1, 4)]:
        x = torch.randn(*shape)
        x[..., ::5] = x[..., 1::5][..., : x[..., ::5].shape[-1]]   # ties
        assert torch.equal(patched(x, 7), upstream(x, 7)), shape
    x = torch.randn(1, 2, 3, 3)            # too short to pad: upstream returns it unchanged
    assert torch.equal(patched(x, 7), upstream(x, 7))
    x = torch.randn(1, 2, 3, 30)           # other widths go to upstream
    assert torch.equal(patched(x, 5), upstream(x, 5))


def test_clean_cache_patch_skips_only_empty_cache(monkeypatch):
    from types import SimpleNamespace
    from whisperlivekit.simul_whisper.decoder_state import DecoderState

    def post1_clean_cache(self):           # shape of whisperlivekit 0.2.17.post1
        self.kv_cache.clear()
        torch.cuda.empty_cache()
    monkeypatch.setattr(DecoderState, 'clean_cache', post1_clean_cache)
    calls = []
    monkeypatch.setattr(torch.cuda, 'empty_cache', lambda: calls.append(1))
    server._patch_whisperlivekit_clean_cache()
    assert DecoderState.clean_cache is not post1_clean_cache

    resets = []
    cache = {'k': torch.zeros(1)}
    inference = SimpleNamespace(kv_cache=cache)
    state = SimpleNamespace(kv_cache=cache, decoder_type='beam', inference=inference,
                            token_decoder=SimpleNamespace(reset=lambda: resets.append(1)))
    DecoderState.clean_cache(state)
    assert state.kv_cache is cache and cache == {}
    assert inference.kv_cache == {} and inference.kv_cache is not cache and resets == [1]
    assert calls == []

    patched = DecoderState.clean_cache
    server._patch_whisperlivekit_clean_cache()      # no empty_cache left: not wrapped again
    assert DecoderState.clean_cache is patched
