from pathlib import Path
import logging


def configure_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    for logger_name in ("upload", "transcription", "email"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        expected_path = logs_dir / f"{logger_name}.log"
        if any(
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename) == expected_path
            for handler in logger.handlers
        ):
            continue

        handler = logging.FileHandler(expected_path, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
