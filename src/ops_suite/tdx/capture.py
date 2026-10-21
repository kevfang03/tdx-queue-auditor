"""Write a structure capture: skeleton HTML per frame plus a few plain-text indexes.

Only skeletons, ``describe_url`` values and kept header labels are written. Raw HTML and raw
URLs never reach the disk.
"""

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from ops_suite.tdx.browser import FrameHtml, RequestRecord
from ops_suite.tdx.session import describe_url
from ops_suite.tdx.skeleton import skeletonize

CAPTURE_NAME = re.compile(r"[a-z0-9-]{1,40}")

README_TEXT = """\
TDX structure capture: {name}, taken {taken}.

This folder holds the STRUCTURE of the TeamDynamix pages that were open, with all personal data
replaced by placeholders, so a Claude session can build selectors without seeing real tickets.

  frame-P-F.html  skeleton of page P, frame F (frame 0 is the page itself, the rest iframes).
                  Tags, id/class/role/type/colspan/rowspan/name, links as [url], short column
                  headers as written; every other text is a placeholder: [email], [datetime],
                  [number] or [text:N] (N words). In attribute values, runs of 3+ digits show
                  as #, words that also appear in the page text (names) as *, and form
                  controls' ids and names as [id] and [name].
  frames.txt      one line per frame: P-F, host and path (no query), skeleton size.
  requests.txt    the calls the pages made to the TDX host: method, resource type, host and
                  path (no query), and how many times.
  kept-text.txt   every real text string that was kept; it should hold column names only.

Review before sharing it with anyone or any tool:
  1. Open kept-text.txt. Only column names (for example "Modified", "Requestor") may be there.
  2. Search every frame-*.html for names, email addresses, ticket numbers and ticket titles of
     the tickets you had open, and for the at sign of an address. None may appear.
  3. If anything personal is left, delete this folder and report the problem; do not share it.
"""


def is_valid_capture_name(name: str) -> bool:
    return CAPTURE_NAME.fullmatch(name) is not None


def write_capture(
    out_dir: Path,
    name: str,
    frames: list[FrameHtml],
    requests: list[RequestRecord],
    now: datetime,
) -> Path:
    """Create ``out_dir/<name>-<YYYYMMDD-HHMMSS>/`` with the capture files; return the folder."""
    if not is_valid_capture_name(name):
        raise ValueError("the capture name must be 1-40 characters of a-z, 0-9 and -")
    folder = out_dir / f"{name}-{now:%Y%m%d-%H%M%S}"
    folder.mkdir(parents=True, exist_ok=False)

    frame_lines: list[str] = []
    kept: dict[str, None] = {}  # insertion-ordered set
    for frame in frames:
        label = f"{frame.page_index}-{frame.frame_index}"
        skeleton = skeletonize(frame.html)
        _write(folder / f"frame-{label}.html", skeleton.html)
        size_kb = len(skeleton.html.encode("utf-8")) / 1024
        frame_lines.append(f"{label}  {describe_url(frame.url)}  {size_kb:.1f} KB")
        kept.update(dict.fromkeys(skeleton.kept_text))

    counts = Counter(
        f"{request.method}  {request.resource_type}  {describe_url(request.url)}"
        for request in requests
    )
    request_lines = [f"{line}  x{count}" for line, count in sorted(counts.items())]

    _write(folder / "frames.txt", _lines(frame_lines))
    _write(folder / "requests.txt", _lines(request_lines))
    _write(folder / "kept-text.txt", _lines(list(kept)))
    _write(folder / "README.txt", README_TEXT.format(name=name, taken=f"{now:%Y-%m-%d %H:%M:%S}"))
    return folder


def _lines(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
