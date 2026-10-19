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
        received.append(ui.input("快捷键合成输入> "))
        return 0

    with create_pipe_input() as pipe:
        ui = TerminalUI(
            worker, input=pipe, output=DummyOutput(), shortcuts={"F2": "help"}
        )

        def send_keys() -> None:
            if not ui._request_ready.wait(5):
                if ui._pending is not None:
                    ui._resolve_pending(eof=True)
                return
            first_request = ui._pending
            pipe.send_text("/退")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = ui._input_buffer.complete_state
                if state is not None and state.completions:
                    break
                time.sleep(0.01)
            pipe.send_text("\t\r")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and ui._pending is first_request:
                time.sleep(0.01)
            if ui._pending is first_request:
                ui._resolve_pending(eof=True)
                return
            if not ui._request_ready.wait(5):
                if ui._pending is not None:
                    ui._resolve_pending(eof=True)
                return
            pipe.send_bytes(b"\x1bOQ")  # xterm F2 sequence
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if ui._input_buffer.text == "/帮助":
                    break
                time.sleep(0.01)
            if ui._input_buffer.text != "/帮助" or received != ["/退出"]:
                if ui._pending is not None:
                    ui._resolve_pending(eof=True)
                return
            pipe.send_text("\r")

        sender = threading.Thread(target=send_keys, daemon=True)
        sender.start()
        result = ui.run()
        sender.join(timeout=5)
        if sender.is_alive() or result != 0 or received != ["/退出", "/帮助"]:
            raise SystemExit("TUI 合成输入自检失败")
    print("TUI 合成输入、命令补全、F2 映射和退出自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
