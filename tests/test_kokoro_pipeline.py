"""Kokoro loads an 82M-parameter model per KPipeline, so it must be built once
per process and reused, not rebuilt for every chapter."""
import importlib
import sys
import types

import pytest


@pytest.fixture
def kokoro_tts(monkeypatch):
    """Import kokoro_tts with its heavy third-party imports stubbed out."""
    class FakeKPipeline:
        instances = 0

        def __init__(self, lang_code, repo_id=None):
            FakeKPipeline.instances += 1
            self.lang_code = lang_code
            self.repo_id = repo_id

        def __call__(self, text, voice=None, speed=1.0):
            return iter(())

    fake_kokoro = types.ModuleType("kokoro")
    fake_kokoro.KPipeline = FakeKPipeline
    fake_soundfile = types.ModuleType("soundfile")
    fake_soundfile.write = lambda *a, **kw: None

    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)
    monkeypatch.setitem(sys.modules, "soundfile", fake_soundfile)
    monkeypatch.delitem(sys.modules, "audible_epub3_maker.tts.kokoro_tts", raising=False)

    module = importlib.import_module("audible_epub3_maker.tts.kokoro_tts")
    module._pipelines.clear()
    FakeKPipeline.instances = 0
    module.FakeKPipeline = FakeKPipeline
    yield module

    module._pipelines.clear()
    sys.modules.pop("audible_epub3_maker.tts.kokoro_tts", None)


def test_pipeline_is_built_once_per_process(kokoro_tts):
    first = kokoro_tts.get_pipeline("a")
    for _ in range(9):
        kokoro_tts.get_pipeline("a")

    # Ten chapters, one model load - this is the whole point of the change.
    assert kokoro_tts.FakeKPipeline.instances == 1
    assert kokoro_tts.get_pipeline("a") is first


def test_each_language_gets_its_own_pipeline(kokoro_tts):
    british = kokoro_tts.get_pipeline("b")
    american = kokoro_tts.get_pipeline("a")

    assert british is not american
    assert kokoro_tts.FakeKPipeline.instances == 2
    assert kokoro_tts.get_pipeline("b") is british


def test_download_model_does_not_pin_a_model_in_the_parent(kokoro_tts):
    """Preloading runs in the parent, which never synthesises. It must not
    leave a model cached there for the rest of the run."""
    kokoro_tts.KokoroTTS.download_model("a", "af_heart")

    assert kokoro_tts.FakeKPipeline.instances == 1
    assert kokoro_tts._pipelines == {}
