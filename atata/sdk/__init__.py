"""Слой SketchUp C SDK.

Разбор ``model.dat`` — геометрия, компоненты, слои — возможен только через
официальный SDK от Trimble. SDK поставляется **только под Windows x64 и
macOS**; сборки под Linux не существует, поэтому этот слой не работает
внутри Linux-контейнера и вынесен в отдельный воркер.

SDK в репозиторий не кладётся: он под Trimble Developer Terms, принимать
которые должен лицензиат. Положите распакованный архив рядом и укажите путь
через ``ATATA_SDK_PATH`` — см. README.
"""

from .capi import SdkError, SdkUnavailable, load_sdk, sdk_status
from .inspect import ModelFacts, DefinitionInfo, inspect_model

__all__ = [
    "SdkError",
    "SdkUnavailable",
    "load_sdk",
    "sdk_status",
    "ModelFacts",
    "DefinitionInfo",
    "inspect_model",
]
