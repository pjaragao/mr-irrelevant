"""Single layout for every scheduled WhatsApp message.

Layout rules (WhatsApp renders *bold*, _italic_, no tables/headers):
    *🏈 TÍTULO EM CAIXA ALTA*
    _subtítulo curto_
    (linha em branco)
    *Seção*
    • item
       ↳ detalhe
    (linha em branco entre seções)
    _rodapé / assinatura_

Long messages are split into parts of at most `max_lines` lines, never inside a section when avoidable;
parts are joined with MSG_SPLIT and sent as separate WhatsApp messages (continuations get "(2/3)").
"""

from __future__ import annotations

from typing import List, Optional

MSG_SPLIT = "\n<<<MSG>>>\n"
MAX_LINES = 28


class Msg:
    def __init__(self, title: str, subtitle: str = "", emoji: str = ""):
        self.title = f"{emoji} {title}".strip() if emoji else title
        self.subtitle = subtitle
        self.blocks: List[List[str]] = []
        self.footer_line = ""

    def section(self, title: str, lines: List[str], emoji: str = "") -> "Msg":
        lines = [line for line in lines if line is not None and line != ""]
        if not lines:
            return self
        head = f"*{emoji} {title}*" if emoji else f"*{title}*"
        self.blocks.append([head, *lines])
        return self

    def text(self, *lines: str) -> "Msg":
        lines = [line for line in lines if line]
        if lines:
            self.blocks.append(list(lines))
        return self

    def footer(self, line: str) -> "Msg":
        self.footer_line = line
        return self

    def _header(self, part: int = 1, total: int = 1) -> List[str]:
        suffix = f" ({part}/{total})" if total > 1 else ""
        head = [f"*{self.title.upper()}*{suffix}"]
        if self.subtitle and part == 1:
            head.append(f"_{self.subtitle}_")
        return head

    def render(self, max_lines: int = MAX_LINES) -> str:
        parts: List[List[List[str]]] = [[]]
        used = len(self._header())
        last_idx = len(self.blocks) - 1
        for bi, block in enumerate(self.blocks):
            # A block bigger than a whole message is cut into chunks; the header line is repeated.
            chunks = [block]
            if len(block) + 1 > max_lines - 2:
                body = block[1:]
                size = max_lines - 4
                chunks = [[block[0] + ("" if i == 0 else " _(cont.)_"), *body[i:i + size]]
                          for i in range(0, len(body), size)]
            for chunk in chunks:
                need = len(chunk) + 1
                # A short closing block (signature, one-liner) may overflow a little instead of
                # becoming a lonely extra message.
                tolerate = 4 if (bi == last_idx and len(chunk) <= 3) else 0
                if parts[-1] and used + need > max_lines + tolerate:
                    parts.append([])
                    used = 1
                parts[-1].append(chunk)
                used += need
        total = len(parts)
        out = []
        for i, blocks in enumerate(parts, 1):
            lines = self._header(i, total)
            for b in blocks:
                lines.append("")
                lines.extend(b)
            if i == total and self.footer_line:
                lines.extend(["", f"_{self.footer_line}_"])
            out.append("\n".join(lines))
        return MSG_SPLIT.join(out)


def split_parts(text: str) -> List[str]:
    return [p.strip() for p in (text or "").split("<<<MSG>>>") if p.strip()]


def bullet(text: str, detail: Optional[str] = None) -> List[str]:
    return [f"• {text}"] + ([f"   ↳ {detail}"] if detail else [])
