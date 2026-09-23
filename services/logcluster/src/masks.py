"""可选的脱敏规则。

Drain3 默认 `parametrize_numeric_tokens=True`：把"含至少一个数字"的 token
整体当成参数，所以 IP、UUID、端口、尺寸这些常见高基数字段本来就能聚到一起，
默认不需要额外规则。实测十行样例（IP / IPv6 / UUID / `95%` / `/dev/sda1`）
在无规则下收敛到 5 个模板，符合预期。

因此本模块是"需要时才开"的东西，默认 `MASK_RULES` 为空。典型场景：

- 把 `PARAMETRIZE_NUMERIC` 关掉、改用显式规则（参数提取更可控）；
- 某些高基数字段不含数字（例如 base64 或纯字母的会话 ID），
  数值参数化覆盖不到。

规则用名字选择而不是从环境变量读正则会带来两个好处：正则不会被外部注入，
且 `MASK_RULES` 是 profile 指纹的一部分，改了名字会明确地让旧状态不兼容。
"""

from __future__ import annotations

from collections.abc import Iterable

from drain3.masking import MaskingInstruction

# name -> (正则, 替换成的记号)
#
# 正则写成模块级常量：放在字典里用相邻字符串拼接会被读成“漏了逗号”，
# 这个歧义不值得为少一个变量名冒险。
_IP = r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])"
_UUID = (
    r"(?<![\w-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![\w-])"
)
_HEX = r"(?<![\w])[0-9a-fA-F]{16,}(?![\w])"
_PATH = r"(?<![\w/])/[\w./-]{2,}"

RULES: dict[str, tuple[str, str]] = {
    # 带可选端口的 IPv4。放在数字参数化之外的场景才有意义。
    "ip": (_IP, "IP"),
    # 标准 UUID（常见于请求 ID、trace ID）
    "uuid": (_UUID, "UUID"),
    # 长度 >= 16 的十六进制串（commit、trace、容器 ID）
    "hex": (_HEX, "HEX"),
    # 绝对路径
    "path": (_PATH, "PATH"),
}


def available_rules() -> list[str]:
    return sorted(RULES)


def resolve_rules(names: Iterable[str]) -> list[MaskingInstruction]:
    """把规则名解析成 drain3 的 MaskingInstruction 列表。

    未知名直接报错而不是忽略：漏掉一条脱敏规则会静默改变聚类结果。
    """
    instructions = []
    for name in names:
        try:
            pattern, mask_with = RULES[name]
        except KeyError:
            raise ValueError(
                f"unknown mask rule '{name}'; available: {', '.join(available_rules())}"
            ) from None
        instructions.append(MaskingInstruction(pattern, mask_with))
    return instructions
