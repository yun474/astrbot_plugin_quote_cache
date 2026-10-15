"""Expose the checkout as ``astrbot_plugin_quote_cache`` whatever its folder name."""

import importlib.util
import io
import logging
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_quote_cache"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package


@pytest.fixture
def image_module(monkeypatch):
    """Load image_cache.py with only the framework imports stubbed.

    Set ASTRBOT_MESSAGE_SOURCE to AstrBot's ``astrbot/core/agent/message.py`` to
    exercise the real message models instead of the TextPart stand-in.
    """

    def module(name, **attrs):
        value = types.ModuleType(name)
        value.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    module("astrbot")
    module("astrbot.api", logger=logging.getLogger("image-cache-test"))
    module("astrbot.core")
    module("astrbot.core.agent")

    @dataclass
    class TextPart:
        text: str
        type: str = "text"

    source = os.environ.get("ASTRBOT_MESSAGE_SOURCE")
    if source:
        spec = importlib.util.spec_from_file_location(
            "astrbot.core.agent.message", source
        )
        models = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, models)
        spec.loader.exec_module(models)
    else:
        models = module("astrbot.core.agent.message", TextPart=TextPart)

    name = PACKAGE + ".image_cache"
    spec = importlib.util.spec_from_file_location(name, ROOT / "image_cache.py")
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, loaded)
    spec.loader.exec_module(loaded)
    loaded.test_models = models
    return loaded


@pytest.fixture
def chat_module(monkeypatch):
    """加载 chat_context.py，只替换框架的 TextPart。"""

    @dataclass
    class TextPart:
        text: str
        type: str = "text"

    for name in ("astrbot", "astrbot.core", "astrbot.core.agent"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    models = types.ModuleType("astrbot.core.agent.message")
    models.TextPart = TextPart
    monkeypatch.setitem(sys.modules, models.__name__, models)
    name = PACKAGE + ".chat_context"
    spec = importlib.util.spec_from_file_location(name, ROOT / "chat_context.py")
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(output, format="PNG")
    return output.getvalue()
