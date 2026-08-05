#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

# Collector output (campaign subjects, page titles, search queries) can
# contain emoji or other non-ASCII characters. On Windows the console
# defaults to the legacy 'cp1252' code page, so an unguarded print() of
# that text raises UnicodeEncodeError - which previously got caught by a
# collector's row-level try/except and miscounted a successfully-saved
# record as a failure. Force UTF-8 stdout/stderr for every command.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'medvolt_analytics.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
