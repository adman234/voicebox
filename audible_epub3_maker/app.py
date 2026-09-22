import logging, time, sys, signal
import psutil
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, Executor, as_completed

from audible_epub3_maker.config import settings
from audible_epub3_maker.utils import helpers
from audible_epub3_maker.utils import logging_setup
from audible_epub3_maker.utils.constants import APP_FULLNAME, AUDIO_MIMETYPES, EXIT_PARTIAL
from audible_epub3_maker import audiobook
from audible_epub3_maker.utils.types import TaskPayload
from audible_epub3_maker.epub.epub_book import EpubBook, EpubHTML, EpubAudio, LazyLoadFromFile, EpubSMIL
from audible_epub3_maker.worker import init_worker, task_fn_wrap

logger = logging.getLogger(__name__)
executor: Executor | None = None


class AllTasksFailedError(RuntimeError):
    """No chapter could be converted, so nothing was produced."""


class App(object):
    """docstring for App."""
    
    def __init__(self):
        super(App, self).__init__()
        pass

    def prepare_payloads(self, chapters: list[EpubHTML]) -> list[TaskPayload]:
        pass
    
    def export_audiobook(self, book, formats, chapter_titles, success_list, tmp_dir,
                         title=None):
        """Repackage the generated chapter audio for an audiobook library."""
        chapters = []
        for idx in sorted(success_list):
            audio_file = tmp_dir / f"aud{idx}.mp3"
            if audio_file.is_file():
                chapters.append((chapter_titles[idx], audio_file))

        if not chapters:
            logger.warning("😔 No chapter audio to build an audiobook from.")
            return

        title = title or book.title
        author = book.author
        narrator = f"{settings.tts_engine.title()} TTS - {settings.tts_voice}"
        cover_item = book.get_cover_item()
        cover = cover_item.get_raw() if cover_item is not None else None
        destination = audiobook.book_dir(settings.output_dir, author, title)

        if audiobook.MP3 in formats:
            files = audiobook.export_mp3_folder(chapters, destination, title,
                                                author, narrator, cover)
            audiobook.write_metadata_json(destination / "metadata.json", title,
                                          author, narrator, book.language, book.identifier)
            logger.info(f"🎧 Wrote {len(files)} chapter mp3 files to {destination}")

        if audiobook.M4B in formats:
            target = destination / f"{audiobook.sanitize(title)}.m4b"
            audiobook.export_m4b(chapters, target, title, author, narrator,
                                 cover, workdir=tmp_dir, bitrate=settings.m4b_bitrate)
            size = helpers.format_bytes(target.stat().st_size)
            logger.info(f"🎧 Audiobook saved to {target} ({size})")

    @staticmethod
    def _same_voice_as_last_run(tmp_dir: Path) -> bool:
        """True when chapter audio left in `tmp_dir` was made with the current
        voice settings. Records the current ones either way, so a re-run with
        a different voice starts afresh instead of mixing two narrators."""
        import json
        stamp_file = tmp_dir / "voice.json"
        current = {key: str(getattr(settings, key, "")) for key in
                   ("tts_engine", "tts_lang", "tts_voice", "tts_speed",
                    "tts_chunk_len", "newline_mode")}
        current["input_size"] = str(settings.input_file.stat().st_size)
        try:
            previous = json.loads(stamp_file.read_text(encoding="utf-8"))
        except Exception:
            previous = None
        same = previous == current
        if not same:
            for stale in tmp_dir.glob("aud*.mp3"):
                stale.unlink(missing_ok=True)
        stamp_file.write_text(json.dumps(current), encoding="utf-8")
        return same

    def run(self):
        global executor
        setup_signal_handlers()

        # 1. Load EPUB file
        book = EpubBook(settings.input_file)
        logger.info(f"📕 EPUB Book Info: title = [{book.title}], identifier = [{book.identifier}], language = [{book.language}]")

        if book.language != settings.tts_lang:
            msg = (
                f"⚠️ == EPUB language ({book.language}) does not match TTS language setting ({settings.tts_lang}).\n"
                f"Do you want to continue using TTS language '{settings.tts_lang}'?"
            )
            helpers.confirm_or_exit(msg)

        # The suffix marks the generated EPUB. Keep the book's real title for
        # the audiobook, where "Title _voicebox" would just be wrong in a library.
        original_title = book.title
        if settings.title_suffix:
            book.append_title_suffix(settings.title_suffix)
        epub_output_path = settings.output_path

        # 2. Prepare payloads for TTS and Force Alignment tasks
        chapter_list = [ch for ch in book.get_chapters() if ch.count_visible_chars() > 0]
        payload_list: list[TaskPayload] = []
        success_list: list[int] = []
        failed_list: list[int] = []
        # Hidden, because the output folder is usually an Audiobookshelf
        # library and a visible folder of loose chapter mp3s gets scanned as
        # a book of its own.
        tmp_dir = settings.output_dir / f".{settings.input_file.stem}_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        formats = settings.output_formats or [audiobook.EPUB]
        # Without an EPUB a finished chapter is nothing but its mp3, so a run
        # that died part way can pick up where it stopped. (An EPUB would also
        # need each chapter's alignment, which is not kept.)
        same_voice = self._same_voice_as_last_run(tmp_dir)
        can_resume = same_voice and audiobook.EPUB not in formats

        chapter_titles: list[str] = []
        for idx, chapter in enumerate(chapter_list):
            chapter_filename = Path(chapter.href).stem  # filename, not real content Chapter
            # Read the heading now: the text is replaced with segmented HTML later.
            chapter_titles.append(audiobook.chapter_title(chapter.get_text(), idx))
            chapter_audio_output_file = tmp_dir / f"aud{idx}.mp3"
            if can_resume and chapter_audio_output_file.is_file() and chapter_audio_output_file.stat().st_size > 0:
                success_list.append(idx)
                continue
            chapter_audio_metadata = {
                "title": f"{book.title} - {chapter_filename}",
                "album": book.epub_path.stem,
                "publisher": APP_FULLNAME,
            }
            payload_list.append(TaskPayload(idx=idx,
                                             html_text=chapter.get_text(),
                                             audio_output_file=chapter_audio_output_file,
                                             audio_metadata=chapter_audio_metadata,
                                             ))
        
        if success_list:
            logger.info(f"♻️ Resuming: {len(success_list)} chapter(s) already converted in {tmp_dir}, "
                        f"{len(payload_list)} left to do")

        sizes = ",".join(f"{p.idx}={len(p.html_text)}" for p in payload_list)
        logger.info(f"📏 Chapter characters: {sizes}")

        # Ensure model files downloaded (if any) before multiprocessing dispatch
        helpers.ensure_model_downloaded(settings.tts_engine, settings.tts_lang, settings.tts_voice)
        
        # 3. Dispatch tasks and wait for completion
        logger.info(f"🚀 Start processing [{settings.input_file.name}] ... (Total tasks: {len(payload_list)})")
        start_time = time.perf_counter()
        tts_seconds = align_seconds = 0.0
        with ProcessPoolExecutor(max_workers=max(1, min(settings.max_workers, len(payload_list))),
                                 initializer=init_worker,
                                 initargs=(settings.to_dict(),
                                           logging_setup.get_log_queue(), 
                                           )
                                 ) as executor:
            future_to_idx = {}
            futures = []

            for payload in payload_list:
                future = executor.submit(task_fn_wrap, payload)
                futures.append(future)
                future_to_idx[future] = payload.idx
            
            for future in as_completed(futures):
                idx = future_to_idx[future]
                success, task_result = future.result()  # May raise error

                if success:
                    logger.info(f"✅ [Task {idx}] complete. {task_result}")
                    tts_seconds += task_result.tts_seconds
                    align_seconds += task_result.align_seconds

                    if audiobook.EPUB not in formats:
                        # No SMIL to build: the audio file is all that is needed.
                        success_list.append(idx)
                        helpers.log_memory(logger, f"parent after task {idx}")
                        continue

                    # s0. Get the corresponding chapter item
                    chapter: EpubHTML = chapter_list[idx]

                    # s1. Add audio to EPUB
                    aud_id = f"aud_{idx}"
                    aud_suffix = task_result.audio_file.suffix
                    aud_href = f"audio/{aud_id}{aud_suffix}"
                    audio_item = EpubAudio(raw_content = LazyLoadFromFile(task_result.audio_file),
                                           id = aud_id,
                                           href = aud_href,
                                           media_type = AUDIO_MIMETYPES[aud_suffix]
                                           )
                    book.add_item(audio_item)

                    # s2. Add SMIL
                    smil_href = str(chapter.href) + ".smil"
                    smil_text = helpers.generate_smil_content(smil_href, chapter.href, aud_href, task_result.alignments)
                    smil_id = f"sm_{idx}"
                    smil_item = EpubSMIL(raw_content = smil_text.encode(),
                                         id = smil_id,
                                         href = smil_href,
                                         media_type = "application/smil+xml",
                                         )
                    book.add_item(smil_item)

                    # s3. Update the corresponding chapter item
                    chapter.attrs["media-overlay"] = smil_id  # Add overlay property 
                    chapter.set_text(task_result.taged_html)  # Modify HTML text
                    
                    success_list.append(idx)
                    helpers.log_memory(logger, f"parent after task {idx}")
                else:
                    logger.warning(f"❌ [Task {idx}] failed. {task_result}")
                    failed_list.append(idx)
        
        # 4. Report and save
        elapsed = time.perf_counter() - start_time
        logger.info(f"🎉 Processing complete. {len(success_list)} success, {len(failed_list)} failed. (finished in {helpers.format_seconds(elapsed)})")
        worked = tts_seconds + align_seconds
        if worked:
            logger.info(
                f"⏱️ Worker time: TTS {helpers.format_seconds(tts_seconds)} "
                f"({100 * tts_seconds / worked:.0f}%), "
                f"alignment {helpers.format_seconds(align_seconds)} "
                f"({100 * align_seconds / worked:.0f}%)"
            )
        if len(success_list) == 0:
            raise AllTasksFailedError("All tasks failed - nothing could be created.")

        if audiobook.EPUB in formats:
            book.save_epub(epub_output_path)
            logger.info(f"💾 EPUB saved to {epub_output_path}")

        if audiobook.MP3 in formats or audiobook.M4B in formats:
            self.export_audiobook(book, formats, chapter_titles, success_list,
                                  tmp_dir, original_title)

        # 5. Cleanup. Keep the chapter audio after a partial run, so that the
        # next attempt only has to redo the chapters that failed.
        if failed_list:
            logger.warning(f"⚠️ {len(failed_list)} chapter(s) failed: {sorted(failed_list)}. "
                           f"Kept {tmp_dir} so a re-run only redoes those.")
            return EXIT_PARTIAL
        if settings.cleanup:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return 0


def terminate_worker_processes(timeout: int = 1):
    """
    Gracefully terminate child processes (excluding resource_tracker), then force kill if needed.

    Args:
        timeout (int): Time to wait before force killing. Defaults to 1 second.
    """
    try:
        main_process = psutil.Process()
        children = main_process.children(recursive=True)
        worker_processes = [p for p in children if "multiprocessing.resource_tracker" not in " ".join(p.cmdline())]
        
        for child in worker_processes:
            logger.info(f"Terminating child process PID={child.pid}")
            child.terminate()  # grace kill
        
        gone, alive = psutil.wait_procs(worker_processes, timeout)
        for p in alive:
            logger.warning(f"Killing stubborn child process PID={p.pid}")
            p.kill()  # force kill
    except Exception as e:
        logger.exception("Error terminating children:")
        pass


def handle_signal(signum, frame):
    signal_name = signal.Signals(signum).name
    logger.warning(f"🔔 Received signal {signum} ({signal_name}), MainProcess exiting...")

    if executor:
        executor.shutdown(wait=False, cancel_futures=True) 
        # Cancel pending tasks and return immediately (running tasks still continue)
        
    terminate_worker_processes()
    sys.exit(1)


def setup_signal_handlers():
    signal.signal(signal.SIGINT, handle_signal)   # 监听 Ctrl+C （终端中断信号）
    signal.signal(signal.SIGTERM, handle_signal)  # 监听 SIGTERM（通常由 kill 命令或 subprocess.terminate() 发送）
    pass
