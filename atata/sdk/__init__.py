"""Слой SketchUp C SDK.

Разбор ``model.dat`` — геометрия, компоненты, слои — возможен только через
официальный SDK от Trimble. SDK поставляется **только под Windows x64 и
macOS**; сборки под Linux не существует, поэтому этот слой не работает
внутри Linux-контейнера и вынесен в отдельный воркер.

SDK в репозиторий не кладётся — это десятки мегабайт чужих бинарников.
Положите распакованный архив рядом и укажите путь через ``ATATA_SDK_PATH``
— см. README.
"""

from .capi import SdkError, SdkUnavailable, load_sdk, sdk_status
from .inspect import ModelFacts, DefinitionInfo, inspect_model
from .runner import RunnerConfig, analyze_geometry, can_open, detect, repair_geometry

__all__ = [
    "can_open",
    "repair_geometry",
    "SdkError",
    "SdkUnavailable",
    "load_sdk",
    "sdk_status",
    "ModelFacts",
    "DefinitionInfo",
    "inspect_model",
    "RunnerConfig",
    "analyze_geometry",
    "detect",
]
