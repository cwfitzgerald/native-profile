"""native-profile: end-to-end sampled profiling of native binaries for LLMs.

Pipeline: ensure a debug-enabled cargo `profiling` profile -> build -> record with
samply -> symbolicate with llvm-symbolizer -> aggregate into a digestible report.
"""

__version__ = "0.1.0"
