"""Fixed JSON payloads served by the example's /1k-json and /10k-json routes.

bench/js/ serves the same files so every runtime in the comparison puts
identical bytes on the wire.
"""

import json
from pathlib import Path

data_path = Path(__file__).parent

JSON_1K = json.loads((data_path / "1K.json").read_text())
JSON_10K = json.loads((data_path / "10K.json").read_text())
