#!/usr/bin/env python3
"""Turn an EPUB 3 Media Overlays book into an audiobook, reusing its audio.

A book produced by Voicebox already contains the narration. This extracts it
and repackages it as an .m4b with chapter markers, or as a folder of tagged
per-chapter mp3s, without generating any speech again.

    python3 epub_to_audiobook.py book.epub /output --format m4b

Standard library only, plus ffmpeg. Runs inside the Voicebox container or on
any machine with python3 and ffmpeg.
"""
import argparse
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "smil": "http://www.w3.org/ns/SMIL",
    "xhtml": "http://www.w3.org/1999/xhtml",
}
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
UNKNOWN_AUTHOR = "Unknown Author"


def sanitize(name, fallback="Untitled"):
    cleaned = UNSAFE.sub("", name or "").strip().strip(".")
    return re.sub(r"\s+", " ", cleaned)[:120] or fallback


def text_of(element):
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


class EpubAudio:
    """The audio, chapter titles and metadata held inside a Media Overlay EPUB."""

    def __init__(self, epub_path):
        self.zip = zipfile.ZipFile(epub_path)
        container = ET.fromstring(self.zip.read("META-INF/container.xml"))
        rootfile = container.find(".//container:rootfile", NS)
        self.opf_path = rootfile.get("full-path")
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.opf = ET.fromstring(self.zip.read(self.opf_path))

        self.manifest = {}
        for item in self.opf.findall("opf:manifest/opf:item", NS):
            self.manifest[item.get("id")] = {
                "href": unquote(item.get("href")),
                "media_type": item.get("media-type", ""),
                "properties": item.get("properties", ""),
                "overlay": item.get("media-overlay"),
            }
        self.spine = [x.get("idref") for x in self.opf.findall("opf:spine/opf:itemref", NS)]

    def _resolve(self, href, base_dir=None):
        """Zip path for an href relative to the OPF (or another file)."""
        base = self.opf_dir if base_dir is None else base_dir
        return posixpath.normpath(posixpath.join(base, href)) if base else href

    def meta(self, tag):
        return text_of(self.opf.find(f"opf:metadata/dc:{tag}", NS))

    @property
    def title(self):
        return self.meta("title") or "Untitled"

    @property
    def author(self):
        return self.meta("creator")

    def cover_bytes(self):
        for entry in self.manifest.values():
            if "cover-image" in entry["properties"].split():
                return self._read(self._resolve(entry["href"])), entry["href"]
        meta = self.opf.find("opf:metadata/opf:meta[@name='cover']", NS)
        if meta is not None:
            entry = self.manifest.get(meta.get("content"))
            if entry and entry["media_type"].startswith("image/"):
                return self._read(self._resolve(entry["href"])), entry["href"]
        return None, None

    def _read(self, zip_path):
        try:
            return self.zip.read(zip_path)
        except KeyError:
            return None

    def chapters(self):
        """(title, audio zip path) per spine item that has narration, in order."""
        found = []
        for index, idref in enumerate(self.spine):
            entry = self.manifest.get(idref)
            if not entry or not entry["overlay"]:
                continue
            smil = self.manifest.get(entry["overlay"])
            if not smil:
                continue

            smil_path = self._resolve(smil["href"])
            raw = self._read(smil_path)
            if raw is None:
                continue
            audio = ET.fromstring(raw).find(".//smil:audio", NS)
            if audio is None or not audio.get("src"):
                continue

            audio_path = self._resolve(unquote(audio.get("src")), posixpath.dirname(smil_path))
            found.append((self._chapter_title(entry["href"], index), audio_path))
        return found

    def _chapter_title(self, href, index):
        raw = self._read(self._resolve(href))
        if raw:
            try:
                root = ET.fromstring(raw)
                for level in range(1, 7):
                    heading = root.find(f".//{{{NS['xhtml']}}}h{level}")
                    if heading is not None:
                        title = text_of(heading)
                        if title:
                            return title[:120]
            except ET.ParseError:
                pass
        return f"Chapter {index + 1}"


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = " | ".join((result.stderr or "").strip().splitlines()[-4:])
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {tail}")
    return result


def duration_ms(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    return int(float(out) * 1000)


def export_mp3_folder(chapters, dest, title, author, narrator, cover):
    """One tagged mp3 per chapter. Audiobookshelf reads each file as a chapter."""
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for number, (chapter_title, source) in enumerate(chapters, start=1):
        target = dest / f"{number:03d} - {sanitize(chapter_title, f'Chapter {number}')}.mp3"
        # Copy the stream and rewrite only the tags: no re-encoding, no quality loss.
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
             "-c", "copy", "-id3v2_version", "3",
             "-metadata", f"title={chapter_title}",
             "-metadata", f"album={title}",
             "-metadata", f"artist={author or UNKNOWN_AUTHOR}",
             "-metadata", f"album_artist={author or UNKNOWN_AUTHOR}",
             "-metadata", f"composer={narrator}",
             "-metadata", f"track={number}/{len(chapters)}",
             str(target)])
        written.append(target)

    if cover:
        (dest / "cover.jpg").write_bytes(cover)
    return written


def build_chapter_metadata(chapters, durations, title, author):
    """FFMETADATA describing the book and where each chapter starts."""
    lines = [";FFMETADATA1", f"title={title}", f"artist={author or UNKNOWN_AUTHOR}",
             f"album={title}"]
    start = 0
    for (chapter_title, _), length in zip(chapters, durations):
        end = start + length
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={chapter_title}"]
        start = end
    return "\n".join(lines) + "\n"


def export_m4b(chapters, dest, title, author, narrator, cover, bitrate, workdir):
    """A single .m4b carrying chapter markers and cover art."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    listing = workdir / "concat.txt"
    listing.write_text("".join(
        "file '%s'\n" % str(path).replace("'", r"'\''") for _, path in chapters
    ), encoding="utf-8")

    metadata = workdir / "chapters.txt"
    metadata.write_text(
        build_chapter_metadata(chapters, [duration_ms(p) for _, p in chapters], title, author),
        encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-i", str(metadata)]
    cover_file = None
    if cover:
        cover_file = workdir / "cover.jpg"
        cover_file.write_bytes(cover)
        cmd += ["-i", str(cover_file)]

    cmd += ["-map", "0:a", "-map_metadata", "1"]
    if cover_file:
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    cmd += ["-c:a", "aac", "-b:a", bitrate,
            "-metadata", f"title={title}",
            "-metadata", f"artist={author or UNKNOWN_AUTHOR}",
            "-metadata", f"album={title}",
            "-metadata", f"composer={narrator}",
            "-movflags", "+faststart", str(dest)]
    run(cmd)
    return dest


def write_metadata_json(dest, title, author, narrator, language, identifier):
    data = {
        "title": title,
        "authors": [author] if author else [],
        "narrators": [narrator] if narrator else [],
        "language": language or "",
        "description": "",
        "genres": [], "tags": [], "series": [],
    }
    if identifier and "isbn" in identifier.lower():
        data["isbn"] = identifier.rsplit(":", 1)[-1]
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("epub", type=Path, help="EPUB produced by Voicebox")
    parser.add_argument("output_dir", type=Path, help="Audiobook library root")
    parser.add_argument("--format", choices=["m4b", "mp3", "both"], default="m4b")
    parser.add_argument("--bitrate", default="64k", help="AAC bitrate for m4b (default: 64k)")
    parser.add_argument("--narrator", default="Voicebox (Kokoro)")
    args = parser.parse_args()

    if not args.epub.is_file():
        sys.exit(f"No such file: {args.epub}")

    book = EpubAudio(args.epub)
    chapters = book.chapters()
    if not chapters:
        sys.exit("No narration found in that EPUB. Is it a Media Overlays book?")

    cover, cover_href = book.cover_bytes()
    title, author = book.title, book.author
    print(f"{title} — {author or UNKNOWN_AUTHOR}: {len(chapters)} narrated chapters"
          f"{', cover found' if cover else ''}")

    dest = args.output_dir / sanitize(author, UNKNOWN_AUTHOR) / sanitize(title)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        extracted = []
        for index, (chapter_title, zip_path) in enumerate(chapters):
            target = tmp / f"{index:04d}{Path(zip_path).suffix or '.mp3'}"
            with book.zip.open(zip_path) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append((chapter_title, target))

        if args.format in ("mp3", "both"):
            files = export_mp3_folder(extracted, dest, title, author, args.narrator, cover)
            write_metadata_json(dest / "metadata.json", title, author, args.narrator,
                                book.meta("language"), book.meta("identifier"))
            print(f"Wrote {len(files)} mp3 files to {dest}")

        if args.format in ("m4b", "both"):
            out_file = dest / f"{sanitize(title)}.m4b"
            export_m4b(extracted, out_file, title, author, args.narrator, cover,
                       args.bitrate, tmp)
            size = out_file.stat().st_size / 1e6
            print(f"Wrote {out_file} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
