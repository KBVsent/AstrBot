"""UTF-16 code unit 口径的文本工具。

LINE 的文本长度上限（5000）与 mention 的 index / length 偏移都以 UTF-16 code
unit 计，而 Python str 以码点计。含 emoji（BMP 外字符，占 2 个 code unit）的文本上
两者不等 —— 按码点切片会切出错误的 mention 目标，按码点截断会漏过超限文本。
"""

from __future__ import annotations

_HIGH_SURROGATE_RANGE = range(0xD800, 0xDC00)


def utf16_length(text: str) -> int:
    """返回文本的 UTF-16 code unit 数量。"""
    return len(text.encode("utf-16-le")) // 2


def _units(text: str) -> bytes:
    return text.encode("utf-16-le")


def _decode_units(units: bytes) -> str:
    return units.decode("utf-16-le", errors="ignore")


def _trim_trailing_high_surrogate(units: bytes) -> bytes:
    """丢掉结尾处孤立的高位代理项，避免切断代理对。"""
    if len(units) < 2:
        return units
    last = int.from_bytes(units[-2:], "little")
    if last in _HIGH_SURROGATE_RANGE:
        return units[:-2]
    return units


def truncate_utf16(text: str, limit: int) -> str:
    """按 UTF-16 code unit 截断文本，且不切断代理对。

    Args:
        text: 原文本。
        limit: code unit 上限。

    Returns:
        截断后的文本；未超限时原样返回。
    """
    if limit <= 0:
        return ""
    units = _units(text)
    if len(units) <= limit * 2:
        return text
    return _decode_units(_trim_trailing_high_surrogate(units[: limit * 2]))


def utf16_slice(text: str, index: int, length: int) -> str:
    """按 UTF-16 code unit 偏移取子串（用于换算入站 mention 的 index/length）。

    Args:
        text: 原文本。
        index: 起始 code unit 偏移。
        length: code unit 长度。

    Returns:
        对应的子串；偏移越界时返回可取到的部分。
    """
    if index < 0 or length <= 0:
        return ""
    units = _units(text)
    start = min(index * 2, len(units))
    end = min(start + length * 2, len(units))
    sliced = units[start:end]
    return _decode_units(_trim_trailing_high_surrogate(sliced))


def utf16_split(text: str, index: int) -> tuple[str, str]:
    """在给定 UTF-16 偏移处把文本切成两半。

    Args:
        text: 原文本。
        index: 切点的 code unit 偏移。

    Returns:
        (前半, 后半)。
    """
    units = _units(text)
    cut = min(max(index, 0) * 2, len(units))
    return _decode_units(units[:cut]), _decode_units(units[cut:])
