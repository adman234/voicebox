import os
import argparse
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import gradio as gr

# Gradio 6 moved `theme` and `css` from the Blocks constructor to launch();
# passing them to the wrong one makes them silently do nothing.
try:
    GRADIO_MAJOR = int(gr.__version__.split(".")[0])
except (AttributeError, ValueError):
    GRADIO_MAJOR = 5

from audible_epub3_maker.epub.epub_book import EpubBook
from audible_epub3_maker.config import AZURE_TTS_KEY, AZURE_TTS_REGION
from audible_epub3_maker.utils.constants import (
    APP_NAME, APP_FULLNAME, OUTPUT_DIR, LOG_FILE,
    INGEST_DIR, INGEST_PROCESSED_DIR, INGEST_FAILED_DIR, SETTINGS_FILE,
)
from audible_epub3_maker.utils import helpers
from audible_epub3_maker.automation import runner as runner_mod
from audible_epub3_maker.automation.runner import (
    runner, STAGE_ASSEMBLING, STAGE_SAVED,
)
from audible_epub3_maker.automation.ingest import ingest_queue
from audible_epub3_maker.automation.settings_store import (
    settings_store, DEFAULTS, LOG_LEVELS, NEWLINE_MODES,
)
from audible_epub3_maker import audiobook


# NOTE:
# Global variables defined at the module level are shared across all users and sessions.
# This means that refreshing the page, opening a new browser tab, or accessing from different clients
# will all interact with the same variable.
#
# If you need per-session or per-user isolation (e.g., each user maintains their own counter or state),
# use `gr.State()` within your Gradio app to store and manage session-specific data.

CSS = """
#preview-output,
#log-output {
    background-color: #242b2d;
}

#preview-output span,
#log-output span {
    color: #8ec07c;
}

#preview-output textarea, 
#log-output textarea {
    background-color: rgb(28 33 33 / 90%);
    color: #ebdbb2;
}
"""
BTN_RUN_IDLE = "🚀  Run"
BTN_RUN_RUNNING = "🔁 Running"
BTN_RUN_AUTOMATION = "🤖 Automation running"
BTN_CANCEL = "🛑 Cancel"
LOG_MAX_LINES = 1000  # 最多保留的日志行数

ENGINE_CHOICES = ["Azure", "Kokoro"]

# Label shown to the user -> value stored in settings.
FORMAT_CHOICES = [
    ("EPUB 3 read-along", audiobook.EPUB),
    ("MP3 folder (Audiobookshelf)", audiobook.MP3),
    ("M4B audiobook", audiobook.M4B),
]
FORMAT_INFO = ("What to produce. MP3 and M4B are written as <Author>/<Title>/ for "
               "Audiobookshelf. M4B is re-encoded, so it takes a little longer.")

log_file = LOG_FILE
log_inode = -1
log_offset = -1
log_buffer = []

# engine name -> {language: [voices]}. Fetching the Azure voice list is a
# network round trip, so keep it rather than asking on every dropdown change.
_voices_cache: dict[str, dict[str, list[str]]] = {}
_voices_lock = threading.Lock()


def tail_log_file():
    global log_inode, log_offset, log_buffer
    try:
        if not os.path.exists(log_file):
            log_offset = 0
            log_buffer.clear()
            # Normal on a fresh container: nothing has been converted yet.
            return gr.update(value="No log output yet. Start a conversion, or drop an EPUB into the ingest folder.")
        
        stat = os.stat(log_file)
        current_inode = stat.st_ino
        file_size = stat.st_size

        if log_offset == -1:
            # initialize
            log_inode = current_inode
            log_offset = file_size
            log_buffer.clear()

        if log_inode != current_inode:
            # file rotated
            log_inode = current_inode
            log_offset = 0
        
        elif log_offset > file_size:
            # file cleared
            log_offset = 0
            log_buffer.clear()
        
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(log_offset)          # jump to offset
            new_lines = f.readlines()   # read all lines from offset
            log_offset = f.tell()       # remember new offset

        log_buffer.extend(line.rstrip() for line in new_lines)
        if len(log_buffer) > LOG_MAX_LINES:
            log_buffer = log_buffer[-LOG_MAX_LINES:]
        
        return gr.update(value="\n".join(log_buffer))

    except Exception as e:
        return gr.update(value=f"‼️[Log Error] {e}")


def run_preview(input_file):
    if not input_file:
        return
    epub_path = Path(input_file)
    book = EpubBook(epub_path)
    preview = []
    preview.append(f"Title: {book.title}")
    preview.append(f"Identifier: {book.identifier}")
    preview.append(f"Language: {book.language}")
    
    preview.append("="*20)
    total_chars = 0
    for idx, ch in enumerate(book.get_chapters()):
        chars_count = ch.count_visible_chars()
        total_chars += chars_count
        preview.append(f"ch[{idx}]: {unquote(ch.href)}  ({chars_count:,} characters)")
    
    preview.append("="*20)
    preview.append(f"Total characters: {total_chars:,}")
    
    return "\n".join(preview)


def load_langs_voices(tts_engine: str) -> dict[str, list[str]]:
    """Return {language: [voices]} for an engine, fetching Azure's list once."""
    name = (tts_engine or "").lower()

    with _voices_lock:
        if name in _voices_cache:
            return _voices_cache[name]

    if name == "azure":
        if not AZURE_TTS_KEY or not AZURE_TTS_REGION:
            raise RuntimeError("Set AZURE_TTS_KEY and AZURE_TTS_REGION in your environment to use Azure TTS.")
        langs_voices = helpers.get_langs_voices_azure(AZURE_TTS_KEY, AZURE_TTS_REGION)
    elif name == "kokoro":
        langs_voices = helpers.get_langs_voices_kokoro()
    else:
        return {}

    with _voices_lock:
        _voices_cache[name] = langs_voices
    return langs_voices


def voice_updates(tts_engine, preferred_lang=None, preferred_voice=None, quiet=False):
    """Build (language, voice) dropdown updates, keeping the preferred values when valid."""
    try:
        langs_voices = load_langs_voices(tts_engine)
    except Exception as e:
        if not quiet:
            gr.Warning(message=str(e), title="Failed to load voices")
        return (gr.update(choices=[], value=None), gr.update(choices=[], value=None))

    lang_choices = list(langs_voices.keys())
    if preferred_lang in lang_choices:
        lang = preferred_lang
    elif "en-US" in lang_choices:
        lang = "en-US"
    else:
        lang = next(iter(lang_choices), None)

    voice_choices = langs_voices.get(lang, [])
    voice = preferred_voice if preferred_voice in voice_choices else next(iter(voice_choices), None)

    return (
        gr.update(choices=lang_choices, value=lang),
        gr.update(choices=voice_choices, value=voice),
    )


def on_engine_change(tts_engine):
    return voice_updates(tts_engine)


def on_lang_change(tts_engine, tts_lang):
    try:
        langs_voices = load_langs_voices(tts_engine)
    except Exception:
        return gr.update(choices=[], value=None)
    voices = langs_voices.get(tts_lang, [])
    return gr.update(choices=voices, value=voices[0] if voices else None)


def run_generation(input_file, output_dir, output_filename, title_suffix, log_level, cleanup,
                   output_formats, tts_engine, tts_lang, tts_voice, tts_speed,
                   tts_chunk_len, newline_mode, align_threshold, max_workers):
    args = runner_mod.build_command(
        input_file=input_file,
        output_dir=output_dir,
        output_filename=output_filename,
        title_suffix=title_suffix,
        log_level=log_level,
        cleanup=cleanup,
        tts_engine=tts_engine,
        tts_lang=tts_lang,
        tts_voice=tts_voice,
        tts_speed=tts_speed,
        tts_chunk_len=tts_chunk_len,
        newline_mode=newline_mode,
        align_threshold=align_threshold,
        max_workers=max_workers,
        output_formats=output_formats,
    )
    runner.start(args, runner_mod.MANUAL, Path(str(input_file)).name)


def check_process():
    busy, source, label = runner.status()

    if busy:
        progress = runner.progress()
        percent = progress.get("percent")
        suffix = f" {percent:.0f}%" if percent is not None else ""
        label = BTN_RUN_AUTOMATION if source == runner_mod.AUTOMATION else BTN_RUN_RUNNING
        return gr.update(value=f"{label}{suffix}", interactive=False)

    return gr.update(value=BTN_RUN_IDLE, interactive=True)

 
def on_run_click(input_file, output_dir, output_filename, title_suffix, log_level, cleanup,
                 output_formats, tts_engine, tts_lang, tts_voice, tts_speed,
                 tts_chunk_len, newline_mode, align_threshold, max_workers):
    # 检查 input_file, output_dir, tts_engine 必须不为空
    if not input_file:
        raise gr.Error(f"Select a EPUB file to process")
        # return ("", gr.update(), gr.update())
    if not output_formats:
        raise gr.Error("Select at least one output format")
    if not tts_engine:
        raise gr.Error(f"Select a TTS engine to continue")
        # return ("", gr.update(), gr.update())
    if not tts_lang:
        raise gr.Error(f"Select a TTS language to continue")
    if not tts_voice:
        raise gr.Error(f"Select a TTS voice to continue")
    
    try:
        run_generation(
            input_file=input_file.name,
            output_dir=output_dir.strip(),
            output_filename=output_filename.strip(),
            title_suffix=title_suffix.strip(),
            log_level=log_level,
            cleanup=cleanup,
            output_formats=output_formats,
            tts_engine=tts_engine,
            tts_lang=tts_lang,
            tts_voice=tts_voice,
            tts_speed=tts_speed,
            tts_chunk_len=tts_chunk_len,
            newline_mode=newline_mode,
            align_threshold=align_threshold,
            max_workers=max_workers
        )
    except Exception as e:
        raise gr.Error(f"{e}")


def on_cancel_click():
    message = runner.cancel()
    gr.Info(message)


## ----------------------------------------------------------------- Automation

def _format_age(timestamp) -> str:
    if not timestamp:
        return "never"
    seconds = int(max(0, datetime.now().timestamp() - timestamp))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _format_duration(seconds) -> str:
    if not seconds:
        return "-"
    return helpers.format_seconds(seconds)


STATUS_ICONS = {
    "queued": "⏳", "running": "🔁", "done": "✅", "failed": "❌", "cancelled": "🚫",
}


def format_progress(progress: dict) -> str:
    """Render the running conversion's progress as a short human phrase."""
    stage = progress.get("stage") or ""
    total = progress.get("total")
    finished = progress.get("finished") or 0

    if not total:
        # No task count yet: still loading the book and the TTS model.
        return f"{stage}…"

    percent = progress.get("percent") or 0.0
    text = f"{percent:.0f}% — chapter {min(finished + 1, total)} of {total}"
    if progress.get("failed"):
        text += f" ({progress['failed']} failed)"

    eta = progress.get("eta_seconds")
    if eta:
        text += f", ~{helpers.format_seconds(eta)} left"
    elif stage in (STAGE_ASSEMBLING, STAGE_SAVED):
        text += f", {stage}"
    return text


def format_automation_status() -> str:
    snapshot = ingest_queue.snapshot()
    progress = snapshot.get("progress") or {}
    lines = []

    state = "🟢 **Enabled**" if snapshot["enabled"] else "⏸️ **Paused** (turn it on in the Settings tab)"
    lines.append(f"{state} — watching `{snapshot['ingest_dir']}` · last scan {_format_age(snapshot['last_scan'])}")

    current = snapshot["current"]
    if current:
        lines.append(f"\n**Now converting:** 🔁 `{current['file']}` · {format_progress(progress)} "
                     f"· running for {_format_duration(current['duration'])}")
    elif snapshot["runner_busy"]:
        lines.append(f"\n**Now converting:** 🔁 {snapshot['runner_label']} (started from the Convert tab) "
                     f"· {format_progress(progress)}")
    else:
        lines.append("\n**Now converting:** nothing, idle")

    pending = snapshot["pending"]
    if pending:
        lines.append(f"\n**Queued ({len(pending)}):**\n")
        lines.append("| # | File | Queued at |")
        lines.append("| --- | --- | --- |")
        for job in pending:
            lines.append(f"| {job['id']} | `{job['file']}` | {job['queued_at']} |")
    else:
        lines.append("\n**Queued:** empty")

    history = snapshot["history"]
    if history:
        lines.append("\n**Recent jobs:**\n")
        lines.append("| # | File | Result | Took | Details |")
        lines.append("| --- | --- | --- | --- | --- |")
        for job in history[:15]:
            icon = STATUS_ICONS.get(job["status"], "")
            details = job["message"].replace("|", "\\|")
            lines.append(f"| {job['id']} | `{job['file']}` | {icon} {job['status']} | "
                         f"{_format_duration(job['duration'])} | {details} |")
    else:
        lines.append("\n**Recent jobs:** none yet")

    if snapshot["blocked"]:
        lines.append("\n**⚠️ Files being ignored:**\n")
        for path, reason in snapshot["blocked"].items():
            lines.append(f"- `{path}` — {reason}")

    if snapshot["last_error"]:
        lines.append(f"\n**⚠️ Last watcher error:** {snapshot['last_error']}")

    return "\n".join(lines)


def format_failure_output() -> str:
    output = ingest_queue.snapshot()["last_output"]
    if not output:
        return "No failed jobs. Output from a failed conversion appears here."
    return "\n".join(output)


def on_scan_now():
    ingest_queue.request_scan()
    gr.Info("Scanning the ingest folder now.")
    return format_automation_status()


def on_cancel_job():
    gr.Info(ingest_queue.cancel_current())
    return format_automation_status()


def on_clear_queue():
    count = ingest_queue.clear_pending()
    gr.Info(f"Removed {count} queued job(s). The files stay in the ingest folder.")
    return format_automation_status()


def on_clear_history():
    ingest_queue.clear_history()
    return format_automation_status()


## ------------------------------------------------------------------- Settings

SETTINGS_KEYS = [
    "automation_enabled", "scan_interval", "stable_checks",
    "output_dir", "output_filename", "title_suffix", "log_level", "cleanup", "output_formats",
    "tts_engine", "tts_lang", "tts_voice", "tts_speed",
    "tts_chunk_len", "newline_mode", "align_threshold", "max_workers",
]


def _settings_component_values(config: dict, quiet: bool = True) -> list:
    """Map a settings dict onto the Settings tab components, in SETTINGS_KEYS order."""
    lang_update, voice_update = voice_updates(
        config["tts_engine"], config["tts_lang"], config["tts_voice"], quiet=quiet
    )
    values = {
        **config,
        "tts_engine": config["tts_engine"].capitalize(),
        "tts_lang": lang_update,
        "tts_voice": voice_update,
    }
    return [values[key] for key in SETTINGS_KEYS]


def on_settings_save(*values):
    config = dict(zip(SETTINGS_KEYS, values))
    config["tts_engine"] = str(config["tts_engine"] or "").lower()
    try:
        saved = settings_store.save(config)
    except Exception as e:
        raise gr.Error(f"Could not save settings: {e}")

    ingest_queue.request_scan()
    gr.Info("Settings saved. They apply to the next queued book.")
    stamp = datetime.now().strftime("%H:%M:%S")
    state = "enabled" if saved["automation_enabled"] else "paused"
    return f"✅ Saved to `{SETTINGS_FILE}` at {stamp} — automation is **{state}**."


def on_settings_reload():
    config = settings_store.load()
    return _settings_component_values(config, quiet=False) + [
        f"↩️ Reloaded from `{SETTINGS_FILE}`."
    ]


def on_settings_reset():
    return _settings_component_values(dict(DEFAULTS), quiet=False) + [
        "↩️ Built-in defaults loaded. Press **Save** to keep them."
    ]


def seed_settings_tab():
    return _settings_component_values(settings_store.all(), quiet=True)


def seed_convert_tab():
    """Pre-fill the Convert tab from the saved automation defaults."""
    config = settings_store.all()
    lang_update, voice_update = voice_updates(
        config["tts_engine"], config["tts_lang"], config["tts_voice"], quiet=True
    )
    return [
        config["output_dir"],
        config["output_filename"],
        config["title_suffix"],
        config["log_level"],
        config["cleanup"],
        config["output_formats"],
        config["tts_engine"].capitalize(),
        lang_update,
        voice_update,
        config["tts_speed"],
        config["tts_chunk_len"],
        config["newline_mode"],
        config["align_threshold"],
        config["max_workers"],
    ]


def build_convert_tab():
    """The manual, one-book-at-a-time view. Returns its components."""
    with gr.Row(equal_height=False):
        with gr.Column(scale=1, min_width=600, elem_id="left-panel"):
            # General settings
            with gr.Accordion("⚙️ General Settings", open=True, elem_id="gen_sets"):
                with gr.Row(equal_height=True):
                    with gr.Column(min_width=160):
                        input_file = gr.File(label="Select a EPUB file to process",
                                             file_types=[".epub"], 
                                             file_count="single",
                                             interactive=True
                                             )
                    
                    with gr.Column(min_width=160):
                        output_dir = gr.Textbox(label="Output Directory",
                                                value=str(OUTPUT_DIR),
                                                interactive=True
                                                )
                        output_filename = gr.Textbox(label="Output Filename",
                                                    placeholder="Leave blank to use original filename",
                                                    interactive=True
                                                    )
                        title_suffix = gr.Textbox(label="Output Title Suffix",
                                                placeholder="e.g. by Voicebox",
                                                interactive=True
                                                )

                with gr.Row(equal_height=True):
                    with gr.Column(min_width=160):
                        log_level = gr.Dropdown(LOG_LEVELS,
                                                value="INFO", 
                                                label="Log Level",
                                                show_label=True,
                                                interactive=True,
                                                )
                    with gr.Column(min_width=160):
                        cleanup = gr.Checkbox(label="Check to delete temporary files after processing",
                                              info="Cleanup",
                                              )
                with gr.Row(equal_height=True):
                    output_formats = gr.CheckboxGroup(choices=FORMAT_CHOICES,
                                                      value=[audiobook.EPUB],
                                                      label="Output Formats",
                                                      info=FORMAT_INFO,
                                                      interactive=True,
                                                      )
            
            # TTS settings
            with gr.Accordion("🎙 TTS Settings", open=True, elem_id="tts_sets"):
                with gr.Row(equal_height=True):
                    tts_engine = gr.Dropdown(choices=ENGINE_CHOICES,
                                            label="TTS Engine",
                                            value=None,
                                            interactive=True
                                            )
                    
                    tts_lang = gr.Dropdown(choices=[], 
                                        label="Language",
                                        interactive=True
                                        )
                    
                    tts_voice = gr.Dropdown(choices=[],
                                            label="Voice",
                                            interactive=True
                                            )
                    
                    tts_speed = gr.Slider(0.5, 2.0, 
                                        step=0.1, 
                                        value=1.0, 
                                        label="Speed",
                                        interactive=True
                                        )

            # Advanced settings
            with gr.Accordion("🛠 Advanced Settings", open=True, elem_id="adv_sets"):
                with gr.Row(equal_height=True):
                    with gr.Column(min_width=160):
                        newline_mode = gr.Dropdown(NEWLINE_MODES, 
                                        value="multi", 
                                        label="Newline Mode",
                                        info="Choose the mode of detecting new paragraphs for TTS",
                                        interactive=True
                                        )
                    with gr.Column(min_width=160):
                        tts_chunk_len = gr.Number(value=0, 
                                            label="Chunk Length (0 = auto)",
                                            info="Set the max characters per TTS request (0 = auto by language)",
                                            interactive=True
                                            )
                with gr.Row(equal_height=True):
                    with gr.Column(min_width=160):
                        align_threshold = gr.Slider(80.0, 100.0, 
                                                step=0.5, 
                                                value=95.0, 
                                                label="Force Alignment Threshold",
                                                info="Set the threshold for force alignment fuzzy matching",
                                                interactive=True)
                    with gr.Column(min_width=160):
                        max_workers = gr.Slider(1, 16, 
                                            step=1, 
                                            value=3, 
                                            label="Max Workers",
                                            info="Set the max number of parallel worker processes",
                                            interactive=True
                                            )
        
        with gr.Column(scale=1, elem_id="right-panel"):
            with gr.Row():
                preview_output = gr.Textbox(label="EPUB Preview", 
                                                lines=10,
                                                max_lines=10,
                                                interactive=False,
                                                elem_id="preview-output",
                                                )
            # Run / Cancel buttons
            with gr.Row():
                run_btn = gr.Button(BTN_RUN_IDLE, variant="primary")
                cancel_btn = gr.Button(BTN_CANCEL)
            
            # Log output
            with gr.Row():
                log_output = gr.Textbox(label="Log Output", 
                                        lines=20,
                                        interactive=False,
                                        elem_id="log-output",
                                        )

    return {
        "input_file": input_file,
        "output_dir": output_dir,
        "output_filename": output_filename,
        "title_suffix": title_suffix,
        "log_level": log_level,
        "cleanup": cleanup,
        "output_formats": output_formats,
        "tts_engine": tts_engine,
        "tts_lang": tts_lang,
        "tts_voice": tts_voice,
        "tts_speed": tts_speed,
        "tts_chunk_len": tts_chunk_len,
        "newline_mode": newline_mode,
        "align_threshold": align_threshold,
        "max_workers": max_workers,
        "preview_output": preview_output,
        "run_btn": run_btn,
        "cancel_btn": cancel_btn,
        "log_output": log_output,
    }


def build_automation_tab():
    gr.Markdown(
        f"Drop `.epub` files into `{INGEST_DIR}` and Voicebox converts them on its own, "
        f"one at a time, using the defaults from the **Settings** tab.\n\n"
        f"Finished books are written to the output directory; the source file moves to "
        f"`{INGEST_PROCESSED_DIR}` on success or `{INGEST_FAILED_DIR}` on failure."
    )
    with gr.Row():
        scan_btn = gr.Button("🔄 Scan now")
        cancel_job_btn = gr.Button("🛑 Cancel current job")
        clear_queue_btn = gr.Button("🧹 Clear queue")
        clear_history_btn = gr.Button("🗑 Clear history")

    status = gr.Markdown(value=format_automation_status(), elem_id="automation-status")

    failure_output = gr.Textbox(
        label="Last Failed Job — Output",
        info="Everything the conversion printed, including the traceback. This is the real reason it failed.",
        value=format_failure_output(),
        lines=16,
        interactive=False,
        elem_id="log-output",
    )

    scan_btn.click(fn=on_scan_now, inputs=None, outputs=status)
    cancel_job_btn.click(fn=on_cancel_job, inputs=None, outputs=status)
    clear_queue_btn.click(fn=on_clear_queue, inputs=None, outputs=status)
    clear_history_btn.click(fn=on_clear_history, inputs=None, outputs=status)

    return status, failure_output


def build_settings_tab():
    """Defaults applied to every automated conversion. Returns components in SETTINGS_KEYS order."""
    gr.Markdown(
        f"These are the options Voicebox uses when it converts files from the ingest folder by itself. "
        f"They also pre-fill the **Convert** tab when the page loads.\n\n"
        f"Saved to `{SETTINGS_FILE}` — mount that directory to keep them across container updates."
    )

    with gr.Accordion("🤖 Automation", open=True):
        with gr.Row(equal_height=True):
            with gr.Column(min_width=160):
                automation_enabled = gr.Checkbox(
                    value=DEFAULTS["automation_enabled"],
                    label="Watch the ingest folder and convert automatically",
                    info=f"Ingest folder: {INGEST_DIR}",
                )
            with gr.Column(min_width=160):
                scan_interval = gr.Slider(5, 600, step=5,
                                          value=DEFAULTS["scan_interval"],
                                          label="Scan Interval (seconds)",
                                          info="How often the ingest folder is checked for new files",
                                          interactive=True)
            with gr.Column(min_width=160):
                stable_checks = gr.Slider(1, 20, step=1,
                                          value=DEFAULTS["stable_checks"],
                                          label="Stable Scans Before Queueing",
                                          info="A file must be unchanged for this many scans before it is converted",
                                          interactive=True)

    with gr.Accordion("⚙️ General Settings", open=True):
        with gr.Row(equal_height=True):
            with gr.Column(min_width=160):
                output_dir = gr.Textbox(label="Output Directory",
                                        value=DEFAULTS["output_dir"],
                                        interactive=True)
                output_filename = gr.Textbox(label="Output Filename",
                                             value=DEFAULTS["output_filename"],
                                             placeholder="Leave blank to use each book's own filename",
                                             info="Leave blank for automation — a fixed name makes every book overwrite the last",
                                             interactive=True)
            with gr.Column(min_width=160):
                title_suffix = gr.Textbox(label="Output Title Suffix",
                                          value=DEFAULTS["title_suffix"],
                                          placeholder="e.g. by Voicebox",
                                          interactive=True)
                log_level = gr.Dropdown(LOG_LEVELS,
                                        value=DEFAULTS["log_level"],
                                        label="Log Level",
                                        interactive=True)
        with gr.Row(equal_height=True):
            cleanup = gr.Checkbox(value=DEFAULTS["cleanup"],
                                  label="Check to delete temporary files after processing",
                                  info="Cleanup")
        with gr.Row(equal_height=True):
            output_formats = gr.CheckboxGroup(choices=FORMAT_CHOICES,
                                              value=DEFAULTS["output_formats"],
                                              label="Output Formats",
                                              info=FORMAT_INFO,
                                              interactive=True)

    with gr.Accordion("🎙 TTS Settings", open=True):
        with gr.Row(equal_height=True):
            tts_engine = gr.Dropdown(choices=ENGINE_CHOICES,
                                     value=DEFAULTS["tts_engine"].capitalize(),
                                     label="TTS Engine",
                                     interactive=True)
            tts_lang = gr.Dropdown(choices=[], label="Language", interactive=True)
            tts_voice = gr.Dropdown(choices=[], label="Voice", interactive=True)
            tts_speed = gr.Slider(0.5, 2.0, step=0.1,
                                  value=DEFAULTS["tts_speed"],
                                  label="Speed", interactive=True)

    with gr.Accordion("🛠 Advanced Settings", open=True):
        with gr.Row(equal_height=True):
            with gr.Column(min_width=160):
                newline_mode = gr.Dropdown(NEWLINE_MODES,
                                           value=DEFAULTS["newline_mode"],
                                           label="Newline Mode",
                                           info="Choose the mode of detecting new paragraphs for TTS",
                                           interactive=True)
            with gr.Column(min_width=160):
                tts_chunk_len = gr.Number(value=DEFAULTS["tts_chunk_len"],
                                          label="Chunk Length (0 = auto)",
                                          info="Set the max characters per TTS request (0 = auto by language)",
                                          interactive=True)
        with gr.Row(equal_height=True):
            with gr.Column(min_width=160):
                align_threshold = gr.Slider(80.0, 100.0, step=0.5,
                                            value=DEFAULTS["align_threshold"],
                                            label="Force Alignment Threshold",
                                            info="Set the threshold for force alignment fuzzy matching",
                                            interactive=True)
            with gr.Column(min_width=160):
                max_workers = gr.Slider(1, 16, step=1,
                                        value=DEFAULTS["max_workers"],
                                        label="Max Workers",
                                        info="Set the max number of parallel worker processes",
                                        interactive=True)

    with gr.Row():
        save_btn = gr.Button("💾 Save settings", variant="primary")
        reload_btn = gr.Button("↩️ Reload saved")
        reset_btn = gr.Button("🧨 Reset to defaults")

    save_status = gr.Markdown(value="")

    components = {
        "automation_enabled": automation_enabled,
        "scan_interval": scan_interval,
        "stable_checks": stable_checks,
        "output_dir": output_dir,
        "output_filename": output_filename,
        "title_suffix": title_suffix,
        "log_level": log_level,
        "cleanup": cleanup,
        "output_formats": output_formats,
        "tts_engine": tts_engine,
        "tts_lang": tts_lang,
        "tts_voice": tts_voice,
        "tts_speed": tts_speed,
        "tts_chunk_len": tts_chunk_len,
        "newline_mode": newline_mode,
        "align_threshold": align_threshold,
        "max_workers": max_workers,
    }
    ordered = [components[key] for key in SETTINGS_KEYS]

    tts_engine.change(fn=on_engine_change, inputs=tts_engine, outputs=[tts_lang, tts_voice])
    tts_lang.change(fn=on_lang_change, inputs=[tts_engine, tts_lang], outputs=tts_voice)
    save_btn.click(fn=on_settings_save, inputs=ordered, outputs=save_status)
    reload_btn.click(fn=on_settings_reload, inputs=None, outputs=ordered + [save_status])
    reset_btn.click(fn=on_settings_reset, inputs=None, outputs=ordered + [save_status])

    return ordered


def launch_gui(host: str = "127.0.0.1", port: int = 7860):
    ingest_queue.start()

    styling = {"theme": gr.themes.Base(), "css": CSS}
    blocks_kwargs = {"title": APP_NAME}
    launch_kwargs = {"server_name": host, "server_port": port}
    (launch_kwargs if GRADIO_MAJOR >= 6 else blocks_kwargs).update(styling)

    with gr.Blocks(**blocks_kwargs) as demo:
        gr.Markdown(f"# 🎧 {APP_FULLNAME}")
        gr.Markdown("---")

        with gr.Tabs():
            with gr.Tab("🎬 Convert"):
                convert = build_convert_tab()
            with gr.Tab("🤖 Automation"):
                automation_status, automation_failure = build_automation_tab()
            with gr.Tab("⚙️ Settings"):
                settings_components = build_settings_tab()

        run_inputs = [
            convert["input_file"], convert["output_dir"], convert["output_filename"],
            convert["title_suffix"], convert["log_level"], convert["cleanup"],
            convert["output_formats"], convert["tts_engine"], convert["tts_lang"], convert["tts_voice"], convert["tts_speed"],
            convert["tts_chunk_len"], convert["newline_mode"], convert["align_threshold"],
            convert["max_workers"],
        ]

        # Events
        convert["input_file"].change(fn=run_preview,
                                     inputs=convert["input_file"],
                                     outputs=convert["preview_output"])
        convert["tts_engine"].change(fn=on_engine_change,
                                     inputs=convert["tts_engine"],
                                     outputs=[convert["tts_lang"], convert["tts_voice"]])
        convert["tts_lang"].change(fn=on_lang_change,
                                   inputs=[convert["tts_engine"], convert["tts_lang"]],
                                   outputs=convert["tts_voice"])
        convert["run_btn"].click(fn=on_run_click, inputs=run_inputs, outputs=None)
        convert["cancel_btn"].click(fn=on_cancel_click, inputs=None, outputs=None)

        # gr.Timer is triggered from the frontend browser
        gr.Timer(0.5).tick(
            fn=check_process,
            inputs=None,
            outputs=[convert["run_btn"]]
        ) 
        gr.Timer(0.5).tick(
            fn=tail_log_file,
            inputs=None,
            outputs=[convert["log_output"]]
        )
        gr.Timer(2.0).tick(
            fn=lambda: (format_automation_status(), format_failure_output()),
            inputs=None,
            outputs=[automation_status, automation_failure]
        )

        # Seed both tabs from the saved defaults once the page is open.
        demo.load(fn=seed_settings_tab, inputs=None, outputs=settings_components)
        demo.load(fn=seed_convert_tab, inputs=None, outputs=run_inputs[1:])
    
    demo.launch(**launch_kwargs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"{APP_NAME} Web GUI (powered by Gradio)",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind the Gradio web server")
    parser.add_argument("--port", type=int, default=7860, help="Port to bind the Gradio web server")
    args = parser.parse_args()

    launch_gui(host=args.host, port=args.port)
