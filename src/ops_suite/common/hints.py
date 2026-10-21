"""The command prefix that every user-facing hint prints.

The project runs through ``uv``, so ``ops-suite ...`` alone works only inside an activated
virtual environment; ``uv run ops-suite ...`` runs as typed. Docstrings, ``--help`` text and
the parser's ``prog`` keep the plain ``ops-suite`` name.
"""

RUN = "uv run ops-suite"
