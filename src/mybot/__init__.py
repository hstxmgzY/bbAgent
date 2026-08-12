"""mybot runtime package defaults."""

import os


# Keep imports deterministic and offline-safe unless the operator opts in otherwise.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
