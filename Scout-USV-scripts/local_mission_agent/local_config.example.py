# Example local_config.py for the Scout Local Agent.
#
# Copy this file to local_config.py (same directory) and set OPERATOR_URLS
# for this machine's operator station. local_config.py is gitignored, so
# each Scout/dev machine can point at a different operator without editing
# config.py or opening a PR.
#
# local_config.py is entirely optional -- if it's absent (and the
# OPERATOR_URLS environment variable isn't set), config.py falls back to
# DEFAULT_OPERATOR_URLS.
#
# Precedence (highest wins):
#   1. OPERATOR_URLS environment variable (comma-separated)
#   2. local_config.py (this file, once copied)
#   3. DEFAULT_OPERATOR_URLS in config.py

OPERATOR_URLS = [
    "http://10.0.0.23:8210",  # desktop (backend moved 8200 -> 8210)
    # "http://10.0.0.24:8210",  # laptop
]
