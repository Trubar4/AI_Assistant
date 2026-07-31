"""Pytest-Rootkonfiguration: Repo-Wurzel auf sys.path, damit `import backend.*`
in den Tests unabhängig vom pytest-Import-Modus funktioniert."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
