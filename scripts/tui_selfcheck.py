from __future__ import annotations

import threading
import time

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cvfx_harness.cli import configure_console_encoding
from cvfx_harness.tui import TerminalUI


def main() -> int:
    configure_console_encoding()
    received: list[str] = []

    def worker(ui: TerminalUI) -> int:
        received.append(ui.input("合成输入> "))
        return 0

    with create_pipe_input() as pipe:
        ui = TerminalUI(worker, input=pipe, output=DummyOutput())

        def send_keys() -> None:
            if not ui._request_ready.wait(5):
                return
            pipe.send_text("/退")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = ui._input_buffer.complete_state
                if state is not None and state.completions:
                    break
                time.sleep(0.01)
            pipe.send_text("\t\r")

        sender = threading.Thread(target=send_keys, daemon=True)
        sender.start()
        result = ui.run()
        sender.join(timeout=5)
        if sender.is_alive() or result != 0 or received != ["/退出"]:
            raise SystemExit("TUI 合成输入自检失败")
    print("TUI 合成输入、命令补全和退出自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
