import logging

from watch_audio_pipeline.logging_utils import configure_logging


def test_configure_logging_creates_stage_log_files(tmp_path):
    configure_logging(tmp_path)

    logging.getLogger("upload").info("queued upload")
    logging.getLogger("transcription").info("finished transcription")
    logging.getLogger("email").info("sent email")

    assert (tmp_path / "upload.log").is_file()
    assert (tmp_path / "transcription.log").is_file()
    assert (tmp_path / "email.log").is_file()
