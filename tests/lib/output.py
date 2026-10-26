from io import StringIO

from pip._internal.cli.terminal import Renderable, Terminal


def render_to_text(
    renderable: str | Renderable,
    *,
    color: bool = False,
) -> str:
    with StringIO() as stream:
        console = Terminal(file=stream, color=color)
        console.print(renderable)
        return stream.getvalue()
