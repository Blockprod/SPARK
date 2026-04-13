"""shorts-engine core modules package.

All public symbols are importable from their respective submodules
(e.g. ``from core.script_gen import generate_script_package``).
Eager re-exports are intentionally omitted to avoid pulling in heavy
third-party dependencies (google-genai, torch, ltx-pipelines …) at
package-import time, which would break tests and lightweight tooling.
"""
