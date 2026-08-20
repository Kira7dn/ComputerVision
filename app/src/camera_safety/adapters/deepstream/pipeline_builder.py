"""DeepStream graph composition boundary.

The existing SafetyPipeline is retained as the behavior-preserving worker
implementation. New graph code should enter through this module rather than
placing lifecycle or persistence decisions in a pad probe.
"""

from __future__ import annotations

from typing import Any, Protocol


class PipelineBuilder(Protocol):
    def build(self, config: dict[str, Any]) -> Any: ...
