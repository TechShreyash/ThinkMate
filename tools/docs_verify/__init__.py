"""Documentation rewrite verification layer.

This package provides the mechanical preservation checks used by the
``docs-rewrite`` spec to guarantee that the editorial rewrite of the
``Documentation_Set`` never drops technical content, diagrams, tables,
emoji headers, or cross-links, and that ``persona.md`` keeps its meaning.

The application code under ``app/`` is intentionally untouched by this
package — it exists purely to validate Markdown documents against their
committed ``git`` baseline.
"""

from tools.docs_verify.models import (
    FileInventory,
    Heading,
    Link,
    PreservationResult,
    GitBaselineError,
    read_git_baseline,
)

__all__ = [
    "FileInventory",
    "Heading",
    "Link",
    "PreservationResult",
    "GitBaselineError",
    "read_git_baseline",
]
