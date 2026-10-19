import time
from pathlib import Path


base = Path(__file__).parent
(base / "child.started").write_text("started", encoding="utf-8")
counter = base / "child.counter"
while True:
    with counter.open("a", encoding="utf-8") as stream:
        stream.write("tick\n")
    time.sleep(0.03)
